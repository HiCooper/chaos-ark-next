#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ARK 统一入口：把「取数 → 面板 → 特征 → 重放 → 复盘 → 部署副本」串成一条命令。

为什么需要它：pipeline.py 不触网（只读 cache/panel.json），取数是另外两个脚本，
site/data 又没有任何脚本同步——每天更新要手敲 3 条命令 + 1 条 cp，漏一步就会
静默产出旧结论（见 README「陈旧守卫」一节）。本脚本把这些步骤固化并加上验收。

用法：
    python3 ark.py init               # clone 后一键初始化：建 venv → 装依赖 → 全量抓取 → 全量管线 → 同步
    python3 ark.py daily              # 收盘后更新：增量抓取 + 增量重放 + 同步 + 水位验收
    python3 ark.py daily --full       # 同上，但重放层走全量（改动引擎口径后必须用这个）
    python3 ark.py daily --skip-fetch # 只重跑重放与同步（离线调试）
    python3 ark.py status             # 只读水位报告（唯一可信的「更新到哪天了」）
    python3 ark.py verify             # L0 sha256 校验 + 各层一致性
    python3 ark.py repair 2026-09-22 --yes   # 显式重抓并覆写 L0 的某一天（write-once 的唯一例外）

解释器解析顺序（取数与重放都需要 numpy/pandas/akshare）：
    1. <仓库根>/.venv/bin/python      （ark.py init 创建，推荐）
    2. 当前解释器（若已装齐依赖）
    3. WorkBuddy akshare skill venv    （本机兜底，不属于仓库）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
VENV = os.path.join(ROOT, ".venv")
OUT_DIR = os.path.join(ROOT, "web", "public", "data")
SITE_DATA = os.path.join(ROOT, "site", "data")
RAW_ROOT = os.path.join(ROOT, "data", "raw")
AKSHARE_SKILL_VENV = os.path.expanduser(
    "~/.workbuddy/skills/akshare-data/.venv/bin/python")


# ---------------------------------------------------------------- 基础设施
def _imports_ok(python: str, need_akshare: bool) -> bool:
    mods = ["numpy", "pandas"] + (["akshare"] if need_akshare else [])
    code = "import importlib;" + ";".join(f"importlib.import_module('{m}')" for m in mods)
    r = subprocess.run([python, "-c", code], capture_output=True)
    return r.returncode == 0


def pick_python(need_akshare: bool = True) -> str:
    """选一个能跑引擎的解释器。init 之前依赖可能缺失，这里给出可执行的指引。"""
    candidates = []
    if os.path.exists(os.path.join(VENV, "bin", "python")):
        candidates.append(("项目 venv", os.path.join(VENV, "bin", "python")))
    candidates.append(("当前解释器", sys.executable))
    if os.path.exists(AKSHARE_SKILL_VENV):
        candidates.append(("akshare skill venv(仓库外兜底)", AKSHARE_SKILL_VENV))
    for label, p in candidates:
        if _imports_ok(p, need_akshare):
            return p
    msg = ["找不到同时具备 numpy/pandas" + ("/akshare" if need_akshare else "")
           + " 的解释器。可选方案："]
    for label, p in candidates:
        msg.append(f"  - {label}: {p}")
    msg.append("推荐直接运行  python3 engine/ark.py init  （自动建 .venv 并装依赖）")
    raise SystemExit("\n".join(msg))


def _run(python: str, script: str, args: list, log_name: str) -> int:
    """跑引擎子脚本：子进程输出直接重定向到日志文件（不经本进程泵管道）。

    不用 PIPE + 逐行读的原因：后台/无人值守场景下，任何读端阻塞都会让子进程
    卡死且日志为空，难排查。直接重定向后日志永远反映子进程真实进度，
    控制台只负责给出日志路径，进度用 tail -f 看。
    """
    log = f"/tmp/ark_{log_name}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    cmd = [python, "-u", os.path.join(ENGINE, script)] + args
    print(f"  $ {' '.join(cmd)}")
    print(f"    日志: {log}   （进度: tail -f {log}）")
    with open(log, "w") as fh:
        r = subprocess.run(cmd, cwd=ENGINE, stdout=fh, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        print(f"    ✗ 退出码 {r.returncode}，日志末尾：")
        with open(log) as f:
            for ln in f.readlines()[-15:]:
                print("      " + ln.rstrip())
    return r.returncode


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _jload(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- site 同步
def sync_site(verbose: bool = True) -> dict:
    """把 web/public/data 镜像到 site/data（部署副本）。

    只镜像 data/ 这一层，不碰 site/assets 与 index.html（那是前端构建产物，
    有自己的同步规则：cp -R web/dist/assets/. site/assets/）。
    镜像语义：源里没有的文件在目标里也删掉，避免对外提供过期数据。
    """
    n_copy, n_del = 0, 0
    os.makedirs(SITE_DATA, exist_ok=True)
    for root, _dirs, files in os.walk(OUT_DIR):
        rel = os.path.relpath(root, OUT_DIR)
        dst_root = os.path.join(SITE_DATA, rel) if rel != "." else SITE_DATA
        os.makedirs(dst_root, exist_ok=True)
        for fn in files:
            src = os.path.join(root, fn)
            dst = os.path.join(dst_root, fn)
            if not os.path.exists(dst) or _sha256(src) != _sha256(dst):
                shutil.copy2(src, dst)
                n_copy += 1
    # 反向清理：site/data 里源已不存在的文件
    for root, _dirs, files in os.walk(SITE_DATA):
        rel = os.path.relpath(root, SITE_DATA)
        src_root = os.path.join(OUT_DIR, rel) if rel != "." else OUT_DIR
        for fn in files:
            dst = os.path.join(root, fn)
            if not os.path.exists(os.path.join(src_root, fn)):
                os.remove(dst)
                n_del += 1
    if verbose:
        print(f"  site/data 镜像完成：更新 {n_copy} 个，清理 {n_del} 个过期文件")
    return {"copied": n_copy, "removed": n_del}


# ---------------------------------------------------------------- 水位
def water_level() -> dict:
    """各层水位。判定「更新到最新了吗」的唯一口径：这些日期应当同日。"""
    lv: dict = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    man = _jload(os.path.join(RAW_ROOT, "manifest.json")) or {}
    for kind in ("sw_index", "etf", "bench"):
        lasts = sorted({e.get("last") for e in (man.get(kind) or {}).values()
                        if e.get("last")})
        lv[f"L0_{kind}"] = {"files": len(man.get(kind) or {}),
                            "last": lasts[-1] if lasts else None}

    panel = _jload(os.path.join(ROOT, "cache", "panel.json"))
    lv["L1_panel"] = ({"last": panel["dates"][-1], "days": len(panel["dates"]),
                       "targets": len(panel["series"]),
                       "generated_at": panel.get("generated_at")} if panel else None)

    etfp = _jload(os.path.join(ROOT, "cache", "etf_panel.json"))
    lv["L1_etf_panel"] = ({"last": etfp["dates"][-1], "etfs": len(etfp.get("listing", {}))}
                          if etfp else None)

    fmeta = _jload(os.path.join(ROOT, "cache", "features.npz.meta.json"))
    lv["L2_features"] = ({"last": fmeta["dates"][-1], "days": len(fmeta["dates"]),
                          "codes": len(fmeta["codes"])} if fmeta else None)
    fp = _jload(os.path.join(ROOT, "cache", "features.fp.json"))
    lv["L2_features_fp"] = fp

    today = _jload(os.path.join(OUT_DIR, "today.json"))
    hist = _jload(os.path.join(OUT_DIR, "history.json"))
    lv["L3_today"] = ({"date": today.get("date"), "engine": today.get("engine_version")}
                      if today else None)
    lv["L3_history"] = ({"days": len(hist.get("days", [])),
                         "last": hist["days"][-1]["date"] if hist.get("days") else None}
                        if hist else None)

    n_day = sum(1 for _ in os.listdir(os.path.join(OUT_DIR, "day"))) \
        if os.path.isdir(os.path.join(OUT_DIR, "day")) else 0
    n_site_day = sum(1 for _ in os.listdir(os.path.join(SITE_DATA, "day"))) \
        if os.path.isdir(os.path.join(SITE_DATA, "day")) else 0
    lv["L3_day_files"] = n_day
    lv["site_day_files"] = n_site_day

    def _same(a, b):
        t = _jload(os.path.join(OUT_DIR, a))
        s = _jload(os.path.join(SITE_DATA, a))
        return (t is not None and s is not None
                and json.dumps(t, sort_keys=True) == json.dumps(s, sort_keys=True))
    lv["site_synced"] = {f: _same(f, f)
                         for f in ("today.json", "history.json", "review.json",
                                   "experiments.json") if os.path.exists(os.path.join(OUT_DIR, f))}
    return lv


def _last_dates(lv: dict) -> list:
    out = []
    for k, v in lv.items():
        if isinstance(v, dict) and (v.get("last") or v.get("date")):
            out.append((k, v.get("last") or v.get("date")))
    return out


def cmd_status() -> int:
    lv = water_level()
    print("ARK 水位报告（唯一可信口径：以下日期应当同日）")
    print(f"  检查时间: {lv['checked_at']}")
    for k, v in lv.items():
        if k in ("checked_at", "L2_features_fp", "site_synced"):
            continue
        if isinstance(v, dict):
            d = v.get("last") or v.get("date")
            extra = {kk: vv for kk, vv in v.items()
                     if kk not in ("last", "date", "site_synced")}
            print(f"  {k:18s} last={d}  {extra if extra else ''}")
        else:
            print(f"  {k:18s} {v}")
    fp = lv.get("L2_features_fp") or {}
    if fp:
        print(f"  {'feature 指纹':15s} {fp.get('panel_fp')}  "
              f"(建于 {fp.get('built_at')}, engine {fp.get('engine_version')})")
    dates = [d for _, d in _last_dates(lv)]
    uniq = sorted(set(dates))
    if len(uniq) == 1:
        print(f"\n  ✓ 各层水位一致：{uniq[0]}")
    else:
        print(f"\n  ✗ 水位不一致：{uniq}")
        print("    排查顺序：L0 抓取是否成功 → panel/etf_panel 是否重建 → pipeline 是否带 --refresh")
    sync = lv.get("site_synced") or {}
    bad = [k for k, ok in sync.items() if not ok]
    if bad:
        print(f"  ✗ site/data 与 web/public/data 不一致：{bad} → 运行 python3 engine/ark.py sync")
    else:
        print("  ✓ site/data 已同步")
    return 0 if (len(uniq) == 1 and not bad) else 1


# ---------------------------------------------------------------- 命令
def cmd_init() -> int:
    print("[init/4] 创建项目虚拟环境 .venv 并安装依赖 ...")
    if not os.path.exists(os.path.join(VENV, "bin", "python")):
        r = subprocess.run([sys.executable, "-m", "venv", VENV])
        if r.returncode != 0:
            raise SystemExit("venv 创建失败")
    pip = os.path.join(VENV, "bin", "pip")
    subprocess.run([pip, "install", "--upgrade", "pip", "-q"], check=True)
    subprocess.run([pip, "install", "-r", os.path.join(ENGINE, "requirements.txt"), "-q"],
                   check=True)
    print("  依赖安装完成")

    py = os.path.join(VENV, "bin", "python")
    print("[init/4] 全量抓取 L0/L1（申万指数 + 基准 ≈3min，ETF ≈4min）...")
    if _run(py, "datasource_sw.py", ["--refresh"], "init_panel") != 0:
        raise SystemExit("面板抓取失败")
    if _run(py, "build_etf_panel.py", ["--refresh"], "init_etf") != 0:
        raise SystemExit("ETF 面板构建失败")

    print("[init/4] 全量管线（L2 特征 + L3 重放 ≈5min）...")
    if _run(py, "pipeline.py", ["--refresh", "--all"], "init_pipeline") != 0:
        raise SystemExit("管线运行失败")

    print("[init/4] 同步部署副本 ...")
    sync_site()
    print("\n初始化完成。日常更新只需：python3 engine/ark.py daily")
    return cmd_status()


def cmd_daily(tail: int | None, full: bool, skip_fetch: bool) -> int:
    py = pick_python()
    print(f"[daily] 解释器: {py}")
    lv0 = water_level()
    print("[daily] 更新前水位:")
    for k, d in _last_dates(lv0):
        print(f"    {k:18s} {d}")

    if not skip_fetch:
        print("[daily/4] 抓取申万指数 + 基准（全量重下 + merge-append）...")
        if _run(py, "datasource_sw.py", ["--refresh"], "daily_panel") != 0:
            raise SystemExit("面板抓取失败（数据源未出数时属正常，可稍后重跑）")
        print("[daily/4] 抓取 ETF 日线 ...")
        if _run(py, "build_etf_panel.py", ["--refresh"], "daily_etf") != 0:
            raise SystemExit("ETF 面板构建失败")
    else:
        print("[daily/4] 跳过抓取（--skip-fetch）")

    print("[daily/4] 运行管线 ...")
    # 不硬性传 --refresh：panel 变了特征层守卫会自动重建，没变就复用
    # （--skip-fetch 离线调试时不必白付一次特征构建）。
    if full:
        args = ["--all"]
    elif tail is None:
        args = ["--all", "--tail", "12"]
    else:
        args = ["--all", "--tail", str(tail)]
    if _run(py, "pipeline.py", args, "daily_pipeline") != 0:
        raise SystemExit("管线运行失败")

    print("[daily/4] 同步部署副本 ...")
    sync_site()

    lv1 = water_level()
    print("\n[daily] 更新后水位:")
    for k, d in _last_dates(lv1):
        print(f"    {k:18s} {d}")
    moved = sorted({d for _, d in _last_dates(lv1)} - {d for _, d in _last_dates(lv0)})
    print(f"\n  水位推进: {moved if moved else '无（源未出数 / 已是最新）'}")
    if not all(lv1.get("site_synced", {}).values()):
        print("  ✗ site/data 未同步，请检查")
        return 1
    print("  ✓ site/data 已同步")
    return 0


def cmd_verify() -> int:
    print("[verify] L0 manifest sha256 校验 ...")
    r = subprocess.run([pick_python(), "-u", os.path.join(ENGINE, "rawstore.py")],
                       cwd=ENGINE, capture_output=True, text=True)
    out = r.stdout or ""
    bad = [ln for ln in out.splitlines() if "HASH_MISMATCH" in ln or "MISSING" in ln]
    print(out.strip()[:2000] or "(无条目)")
    if bad:
        print(f"  ✗ {len(bad)} 个 L0 文件校验失败（禁止手改 L0，请用 ark.py repair）")
        return 1
    print("  ✓ L0 完整")
    lv = water_level()
    fp = lv.get("L2_features_fp") or {}
    panel = _jload(os.path.join(ROOT, "cache", "panel.json"))
    if panel and fp:
        # 指纹检查必须在带 numpy 的解释器里跑：pipeline.py 顶层 import numpy，
        # 而 ark.py 本身可能由系统 python3（无依赖）启动，不能在本进程内
        # exec_module。与取数/重放同理，交给子进程执行。
        pipe = os.path.join(ENGINE, "pipeline.py")
        code = (
            "import importlib.util, json\n"
            f"spec = importlib.util.spec_from_file_location('arkpipe', {pipe!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            f"panel = json.load(open({os.path.join(ROOT, 'cache', 'panel.json')!r}))\n"
            "print('FP=' + m.panel_fingerprint(panel))\n"
        )
        r = subprocess.run([pick_python(need_akshare=False), "-u", "-c", code],
                           cwd=ENGINE, capture_output=True, text=True)
        now_fp = ""
        for ln in (r.stdout or "").splitlines():
            if ln.startswith("FP="):
                now_fp = ln[3:].strip()
        if r.returncode != 0 or not now_fp:
            print("  ✗ 特征层一致性检查执行失败（解释器无法加载 pipeline）")
            for ln in ((r.stderr or r.stdout or "").strip().splitlines()[-3:]):
                print("      " + ln)
            return 1
        ok = fp.get("panel_fp") == now_fp
        print(f"  {'✓' if ok else '✗'} features.npz 与 panel.json 指纹{'一致' if ok else '不一致（陈旧，需 --refresh）'}")
        if not ok:
            return 1
    return 0


def cmd_repair(date: str, yes: bool) -> int:
    """write-once L0 的唯一例外通道：显式重抓并覆写指定日期，重记 manifest。"""
    if not yes:
        print("repair 会覆写 L0（不可变层）中指定日期的数据，必须加 --yes 确认。")
        print("用法: python3 engine/ark.py repair <YYYY-MM-DD> --yes")
        return 2
    py = pick_python()
    code = f'''
import json, sys
sys.path.insert(0, {ENGINE!r})
import rawstore as RS, datasource_sw as DSW
DATE = {date!r}
n = 0
for kind in ("sw_index", "etf", "bench"):
    import os
    d = os.path.join(RS.RAW_ROOT, kind)
    if not os.path.isdir(d):
        continue
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        key = fn[:-5]
        rows = RS.get(kind, key) or []
        keep = [r for r in rows if r.get("date") != DATE]
        if len(keep) == len(rows):
            continue
        if kind == "sw_index":
            fresh = DSW.fetch_sw_daily(key, refresh=True) or []
        elif kind == "bench":
            fresh = DSW.fetch_bench_daily(key, refresh=True) or []
        else:
            fresh = DSW.fetch_etf_daily(key, refresh=True) or []
        by_date = {{r["date"]: r for r in fresh}}
        if DATE not in by_date:
            print(f"  [warn] {{kind}}/{{key}} 源头没有 {{DATE}}，保留删除后的状态")
            RS.put(kind, key, keep, "repair", merge=False,
                   note=f"repair {{DATE}}: 源头无此日，删除该行")
        else:
            merged = [r for r in keep] + [by_date[DATE]]
            RS.put(kind, key, merged, "repair", merge=False,
                   note=f"repair {{DATE}}: 显式覆写")
        n += 1
print(f"repair 完成：{{n}} 个文件被改写，manifest 已重记")
'''
    r = subprocess.run([py, "-u", "-c", code], cwd=ENGINE)
    if r.returncode != 0:
        return r.returncode
    print("下一步：重跑  python3 engine/ark.py daily --full  （repair 改了历史，必须全量重放）")
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "status":
        raise SystemExit(cmd_status())
    if cmd == "init":
        raise SystemExit(cmd_init())
    if cmd == "daily":
        tail = int(args[args.index("--tail") + 1]) if "--tail" in args else None
        raise SystemExit(cmd_daily(tail, "--full" in args, "--skip-fetch" in args))
    if cmd == "verify":
        raise SystemExit(cmd_verify())
    if cmd == "sync":
        sync_site()
        return
    if cmd == "repair":
        date = args[1] if len(args) > 1 else ""
        raise SystemExit(cmd_repair(date, "--yes" in args))
    print(__doc__)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
