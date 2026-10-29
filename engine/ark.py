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
    python3 ark.py selftest           # 五项快测（<1min）：改 settlement/qfq/rawstore/config 后必跑
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
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
VENV = os.path.join(ROOT, ".venv")
OUT_DIR = os.path.join(ROOT, "web", "public", "data")
SITE_DATA = os.path.join(ROOT, "site", "data")
# 展示层根目录（与数据层分开）：标的走势弹窗等「只给页面看」的产物。
# 它不参与 L0→L3 重建链，也不持有自己的算法——由结算账簿切片而来（见
# engine/build_etf_webdata.py），因此水位检查里它是「陈旧守卫」而非一层数据。
VIEW_DIR = os.path.join(ROOT, "web", "public", "view")
SITE_VIEW = os.path.join(ROOT, "site", "view")
RAW_ROOT = os.path.join(ROOT, "data", "raw")
AKSHARE_SKILL_VENV = os.path.expanduser(
    "~/.workbuddy/skills/akshare-data/.venv/bin/python")

# config 只有 os 依赖，可以直接导入（本脚本可能在无 numpy 的系统 python 下运行）
sys.path.insert(0, ENGINE)
import config as C  # noqa: E402


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


# 子进程日志目录：cache/ 已在 .gitignore，跨重启可查（/tmp 会随重启清空，
# 「27 分钟那次 daily 慢在哪」这类事后归因需要跨天日志）。
LOG_DIR = os.path.join(ROOT, "cache", "logs")
LOG_KEEP = 40  # 每类日志保留最近 N 份，防止无限膨胀


def _prune_logs(log_name: str) -> None:
    """按日志类别只保留最近 LOG_KEEP 份（文件名自带时间戳，字典序即时间序）。"""
    try:
        pat = f"ark_{log_name}_"
        files = sorted(fn for fn in os.listdir(LOG_DIR) if fn.startswith(pat))
        for fn in files[:-LOG_KEEP] if len(files) > LOG_KEEP else []:
            os.remove(os.path.join(LOG_DIR, fn))
    except OSError:
        pass


def _run(python: str, script: str, args: list, log_name: str) -> int:
    """跑引擎子脚本：子进程输出直接重定向到日志文件（不经本进程泵管道）。

    不用 PIPE + 逐行读的原因：后台/无人值守场景下，任何读端阻塞都会让子进程
    卡死且日志为空，难排查。直接重定向后日志永远反映子进程真实进度，
    控制台只负责给出日志路径，进度用 tail -f 看。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    _prune_logs(log_name)
    log = os.path.join(LOG_DIR,
                       f"ark_{log_name}_{time.strftime('%Y%m%d_%H%M%S')}.log")
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
def _mirror(src_root: str, dst_root: str) -> tuple:
    """把 src_root 镜像到 dst_root：内容不同的复制、源里没有的删掉（含空壳目录）。

    「源里没有 → 目标删掉」是刻意的：站点是对外窗口，宁可少一个文件，也不能
    留一个已经被数据层淘汰的旧文件在线上。
    """
    n_copy, n_del = 0, 0
    os.makedirs(dst_root, exist_ok=True)
    for root, _dirs, files in os.walk(src_root):
        rel = os.path.relpath(root, src_root)
        d = os.path.join(dst_root, rel) if rel != "." else dst_root
        os.makedirs(d, exist_ok=True)
        for fn in files:
            s = os.path.join(root, fn)
            t = os.path.join(d, fn)
            if not os.path.exists(t) or _sha256(s) != _sha256(t):
                shutil.copy2(s, t)
                n_copy += 1
    for root, _dirs, files in os.walk(dst_root, topdown=False):
        rel = os.path.relpath(root, dst_root)
        s_root = os.path.join(src_root, rel) if rel != "." else src_root
        for fn in files:
            if not os.path.exists(os.path.join(s_root, fn)):
                os.remove(os.path.join(root, fn))
                n_del += 1
        if rel != "." and not os.path.isdir(s_root) and not os.listdir(root):
            os.rmdir(root)
    return n_copy, n_del


def sync_site(verbose: bool = True) -> dict:
    """把 web/public 的两个根分别镜像到 site/（部署副本）：

        data/   数据层导出（L3：today / day / review / sim / experiments …）
        view/   展示层导出（标的走势弹窗：etf/<sid>.json…）

    **两层分开镜像、分开存放**，与仓库内的分层一致：数据层由管线产出，
    展示层由结算账簿切片而来，改一层不会误伤另一层。

    只镜像 data/ 与 view/，不碰 site/assets 与 index.html（那是前端构建产物，
    有自己的同步规则：cp -R web/dist/assets/. site/assets/）。
    镜像语义：源里没有的文件在目标里也删掉，避免对外提供过期数据。
    """
    n_copy, n_del = _mirror(OUT_DIR, SITE_DATA)
    v_copy, v_del = _mirror(VIEW_DIR, SITE_VIEW)
    if verbose:
        print(f"  site 镜像完成：data 更新 {n_copy} 个 / 清理 {n_del} 个；"
              f"view 更新 {v_copy} 个 / 清理 {v_del} 个")
    return {"copied": n_copy + v_copy, "removed": n_del + v_del}


# ---------------------------------------------------------------- 水位
# 非管线 L0 kind（fetch_* 手动抓取，不在 daily 链上）的水位键统一用 L0x_ 前缀：
# _last_dates() 据此前缀把它们排除在「各层同日」断言外——它们的更新节奏由人决定，
# 陈旧不报警、也不阻塞 daily/verify。（2026-10-02 架构评审 P1-1）
_PIPELINE_L0_KINDS = {"sw_index", "etf", "bench", "catalog"}


def _l0x_tail_last_date(path: str, nbytes: int = 256) -> str | None:
    """L0 行式 JSON 的最后一个日期：只读文件尾部找最后一个 "date": "..."。

    比全量 json.load 快两个数量级（sw_valuation 162 文件 71M：3.5s → ~0.03s）。
    L0 每行（dict）都带 date 键，且这些 kind 的字段名里没有第二个 "date"，
    所以尾部最后一个匹配就是最新行。找不到时回退全量解析兜底。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            chunk = f.read().decode("utf-8", errors="ignore")
        hits = re.findall(r'"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"', chunk)
        if hits:
            return hits[-1]
        rows = _jload(path)  # 尾部没扫到（如行超长/格式异常）→ 全量兜底
        if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
            return rows[-1].get("date")
    except OSError:
        pass
    return None


def water_level() -> dict:
    """各层水位。判定「更新到最新了吗」的唯一口径：这些日期应当同日。"""
    lv: dict = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    man = _jload(os.path.join(RAW_ROOT, "manifest.json")) or {}
    for kind in ("sw_index", "etf", "bench"):
        lasts = sorted({e.get("last") for e in (man.get(kind) or {}).values()
                        if e.get("last")})
        lv[f"L0_{kind}"] = {"files": len(man.get(kind) or {}),
                            "last": lasts[-1] if lasts else None}

    # 非管线 L0 kind 扫描（只加可见性，不加行为）：
    # data/raw/ 下不在 daily 抓取链里的目录（fetch_* 手动抓取的
    # sw_valuation / analyst_rank / sw_component 等）。manifest 有登记的用
    # manifest（如 analyst_rank / sw_component），没有的扫目录取最大日期
    # （如 sw_valuation）。三个月后还能回答「analyst_rank 停在几号」。
    if os.path.isdir(RAW_ROOT):
        for kind in sorted(os.listdir(RAW_ROOT)):
            kdir = os.path.join(RAW_ROOT, kind)
            if (not os.path.isdir(kdir)) or kind in _PIPELINE_L0_KINDS:
                continue
            ents = man.get(kind) or {}
            if ents:
                lasts = sorted({e.get("last") for e in ents.values()
                                if e.get("last")})
                lv[f"L0x_{kind}"] = {"files": len(ents),
                                     "last": lasts[-1] if lasts else None,
                                     "via": "manifest"}
            else:
                last, n = None, 0
                for fn in sorted(os.listdir(kdir)):
                    if not fn.endswith(".json"):
                        continue
                    n += 1
                    d = _l0x_tail_last_date(os.path.join(kdir, fn))
                    if d and (last is None or d > last):
                        last = d
                lv[f"L0x_{kind}"] = {"files": n, "last": last, "via": "dir-scan"}

    panel = _jload(os.path.join(ROOT, "cache", "panel.json"))
    _fill = (panel or {}).get("tail_fill") or (panel or {}).get("realtime_fill")
    if _fill:
        lv["L0_realtime_fill"] = {"last": max((_fill.get("days") or {"": None})),
                                  "days": list((_fill.get("days") or {}).keys()),
                                  "method": _fill.get("method")}
    lv["L1_panel"] = ({"last": panel["dates"][-1], "days": len(panel["dates"]),
                       "targets": len(panel["series"]),
                       "generated_at": panel.get("generated_at")} if panel else None)

    etfp = _jload(os.path.join(ROOT, "cache", "etf_panel.json"))
    lv["L1_etf_panel"] = ({"last": etfp["dates"][-1], "etfs": len(etfp.get("listing", {}))}
                          if etfp else None)

    # 展示层·标的走势弹窗（web/public/view/etf/*.json）：**不是数据层**，
    # 不参与 L0→L3 重建链，必须显式重建。这里登记它只为「陈旧守卫」：
    # 漏掉就会出现「账户已盯市到最新交易日、弹窗里的标的涨跌却停在两天前」。
    etfw = _jload(os.path.join(VIEW_DIR, "etf", "_index.json"))
    lv["VIEW_etf_modal"] = ({"last": etfw.get("last"), "etfs": etfw.get("n"),
                             "days": etfw.get("days")} if etfw else None)

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

    def _same(rel, src_root, dst_root):
        t = _jload(os.path.join(src_root, rel))
        s = _jload(os.path.join(dst_root, rel))
        return (t is not None and s is not None
                and json.dumps(t, sort_keys=True) == json.dumps(s, sort_keys=True))
    lv["site_synced"] = {f: _same(f, OUT_DIR, SITE_DATA)
                         for f in ("today.json", "history.json", "review.json",
                                   "experiments.json") if os.path.exists(os.path.join(OUT_DIR, f))}
    # 展示层也要检查是否同步（它有自己的根目录，不能只看 data/）
    if os.path.exists(os.path.join(VIEW_DIR, "etf", "_index.json")):
        lv["site_synced"]["view/etf/_index.json"] = _same("etf/_index.json", VIEW_DIR, SITE_VIEW)
    return lv


def _last_dates(lv: dict) -> list:
    out = []
    for k, v in lv.items():
        if k.startswith("L0x_"):
            # 非管线 L0 kind：水位报告里展示，但不参与「各层同日」断言
            continue
        if isinstance(v, dict) and (v.get("last") or v.get("date")):
            out.append((k, v.get("last") or v.get("date")))
    return out


def _l0_partial_tail():
    """L0 尾部有零星数据、但还没构成「交易日」的那一天（盘后陆续写入的过渡态）。

    交易日的权威定义是 panel 的 L1 并集；某天源端只有极少数指数有 bar 时，panel 不会
    接受这天，于是 L0 的 last 会比 panel / 结论都新一天。这不是错位，是过渡态——
    18:00 后抓数时必然出现。返回 (日期, 已有该日的指数只数)，否则 (None, 0)。
    """
    pl = (_jload(os.path.join(ROOT, "cache", "panel.json")) or {}).get("dates") or []
    man = _jload(os.path.join(RAW_ROOT, "manifest.json")) or {}
    sw = man.get("sw_index") or {}
    if not pl or not sw:
        return None, 0
    last = max(((v.get("last") or "") for v in sw.values()), default="")
    if not last or last <= pl[-1]:
        return None, 0
    return last, sum(1 for v in sw.values() if (v.get("last") or "") >= last)


def _judge_calendar_gaps():
    """判断层「整日缺口」：基准（新浪，最全）是交易日、panel 里却没有的那些天。

    成因不是抓取失败，而是**申万官方当天整日无数据**（同一日 ETF 与上证指数都有行情）。
    危害是静默的：panel 没有这一天 → 没有 day 文件、没有结论、交易日历上是一格空白，
    看上去像「当天没出结论」，实际是「这一天不在系统里」；结算层按 panel 交易日计数，
    到期日也会整体少算。

    只比较「基准交易日集合 − panel 日期集合」，两个文件即可，不逐只指数扫。
    返回 (未登记缺口, 已登记缺口)；登记表见 config.KNOWN_JUDGE_GAPS。
    """
    panel = _jload(os.path.join(ROOT, "cache", "panel.json")) or {}
    dates = panel.get("dates") or []
    bench = _jload(os.path.join(RAW_ROOT, "bench", "1.000001.json"))
    rows = (bench or {}).get("rows") if isinstance(bench, dict) else bench
    if not dates or not rows:
        return [], []
    have = set(dates)
    lo, hi = dates[0], dates[-1]
    holes = sorted(r["date"] for r in rows
                   if r.get("date") and lo <= r["date"] <= hi and r["date"] not in have)
    known = getattr(C, "KNOWN_JUDGE_GAPS", {}) or {}
    return ([d for d in holes if d not in known], [d for d in holes if d in known])


def cmd_status() -> int:
    lv = water_level()
    print("ARK 水位报告（唯一可信口径：以下日期应当同日）")
    print(f"  检查时间: {lv['checked_at']}")
    for k, v in lv.items():
        if k in ("checked_at", "L2_features_fp", "site_synced", "L0_realtime_fill"):
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
    # 「结算层尾部落后判断层」是正常状态：新浪 ETF 日线常比申万晚一晚出数。
    # 此时 pipeline 已把结论截到结算层末日（T+1 建仓需要 ETF 开盘价），不应算不一致。
    _pl = (_jload(os.path.join(ROOT, "cache", "panel.json")) or {}).get("dates") or []
    _el = (_jload(os.path.join(ROOT, "cache", "etf_panel.json")) or {}).get("dates") or []
    tail_lag = bool(_pl and _el and _pl[:len(_el)] == _el and len(_pl) > len(_el))
    if len(uniq) == 1:
        print(f"\n  ✓ 各层水位一致：{uniq[0]}")
    elif tail_lag:
        print(f"\n  ⚠ 结算层尾部落后 {len(_pl) - len(_el)} 天："
              f"ETF 日线到 {_el[-1]}，判断层（申万）到 {_pl[-1]}")
        print(f"    结论已截到 {_el[-1]}（T+1 开盘建仓需要 ETF 开盘价；缺价会让建仓被静默跳过）；"
              f"新浪出数后重跑 daily 即补齐")
    elif (pt := _l0_partial_tail())[0] and set(uniq) <= {_pl[-1], pt[0]}:
        # 过渡态：L0 尾部只有零星几只指数有 bar，尚未构成交易日，panel/结论停在 last
        print(f"\n  ⚠ L0 尾部 {pt[0]} 只有 {pt[1]} 只指数有数据，未构成交易日 → "
              f"结论停在 {_pl[-1]}（源端出全后重跑 daily）")
    else:
        print(f"\n  ✗ 水位不一致：{uniq}")
        print("    排查顺序：L0 抓取是否成功 → panel/etf_panel 是否重建 → pipeline 是否带 --refresh")
    rf = lv.get("L0_realtime_fill")
    if rf:
        _m = rf.get("method") or "realtime"
        _src = ("收盘价取自申万实时行情（真值）" if _m == "realtime"
                else "收盘价由对应 ETF 日收益前推（代理；收益口径真实，点位为推算）")
        print(f"\n  ⚠ 含兜底日：{rf['days']}（官方申万日线停在 "
              f"{(panel := _jload(os.path.join(ROOT, 'cache', 'panel.json'))) and (panel.get('tail_fill') or {}).get('official_last')}，"
              f"方式 {_m}）——{_src}、量额为估算；")
        print("    官方出数后重跑 ark.py daily 会自动用真值覆盖（L0 overwrite_est）")
    sync = lv.get("site_synced") or {}
    bad = [k for k, ok in sync.items() if not ok]
    if bad:
        print(f"  ✗ site 与 web/public 不一致（{', '.join(bad)}）→ 运行 python3 engine/ark.py sync")
    else:
        print("  ✓ site 已同步（data 数据层 + view 展示层）")

    # 判断层日历缺口：基准有、panel 无 = 申万当天整日没数据，系统把这一天整日跳过了
    new_gaps, known_gaps = _judge_calendar_gaps()
    print("\n  判断层日历缺口（基准交易日有、panel 无 → 申万当整日无数据）：")
    if not new_gaps and not known_gaps:
        print("    ✓ 无缺口")
    for d in new_gaps:
        print(f"    ✗ {d}  未登记：核对 ETF/基准当日是否真有行情 →"
              f" 补数据，或登记到 config.KNOWN_JUDGE_GAPS")
    for d in known_gaps:
        print(f"    · {d}  {C.KNOWN_JUDGE_GAPS.get(d, '已登记')}")
    if known_gaps:
        print(f"    （已登记 {len(known_gaps)} 个，说明见 docs/数据缺口-判断层整日缺失.md）")
    return 0 if ((len(uniq) == 1 or tail_lag) and not bad and not new_gaps) else 1


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

    # 展示层只依赖 cache/etf_panel.json（与结算层同一次 load_etf_book 调用的结果），
    # 先于管线重建，这样管线写 review.json 时引擎页「数据分层」的数字已是真数据。
    print("[init/4] 展示层·标的走势数据（弹窗用）...")
    if _run(py, "build_etf_webdata.py", [], "init_etf_web") != 0:
        raise SystemExit("标的走势数据构建失败")

    print("[init/4] 全量管线（L2 特征 + L3 重放 ≈5min）...")
    if _run(py, "pipeline.py", ["--refresh", "--all"], "init_pipeline") != 0:
        raise SystemExit("管线运行失败")

    print("[init/4] 同步部署副本 ...")
    sync_site()
    print("\n初始化完成。日常更新只需：python3 engine/ark.py daily")
    return cmd_status()


def _today_sig(day: dict) -> dict:
    """今日结论的复核指纹：日期 / 环境 / 每条的（名称, 动作, 综合分）。"""
    if not day:
        return {}
    return {
        "date": day.get("date"),
        "regime": day.get("regime"),
        "picks": {p.get("name"): (p.get("action"), round(float(p.get("composite") or 0), 1))
                  for p in (day.get("picks") or [])},
    }


def _report_today_diff(prev: dict, prev_sig: dict, now: dict) -> None:
    """盘前复核：把「本次运行前后今日结论的变化」如实打出来（数据补齐后结论是否移动）。"""
    if not now:
        return
    now_sig = _today_sig(now)
    print("\n[daily] 盘前复核（结论变更）：")
    if not prev_sig:
        print(f"    首次生成：{now_sig['date']}，{len(now_sig['picks'])} 条结论，环境 {now_sig['regime']}")
        return
    if prev_sig.get("date") != now_sig.get("date"):
        print(f"    结论日推进：{prev_sig.get('date')} → {now_sig.get('date')}"
              f"（{len(now_sig['picks'])} 条结论，环境 {now_sig['regime']}）")
    else:
        moved = []
        for name, (act, sc) in now_sig["picks"].items():
            old = prev_sig["picks"].get(name)
            if old is None:
                moved.append(f"新增 {name}（{act} {sc}）")
            elif abs(old[1] - sc) >= 1.0 or old[0] != act:
                moved.append(f"{name}：{old[0]} {old[1]} → {act} {sc}")
        for name, (act, sc) in prev_sig["picks"].items():
            if name not in now_sig["picks"]:
                moved.append(f"移除 {name}（原 {act} {sc}）")
        if prev_sig.get("regime") != now_sig.get("regime"):
            moved.append(f"环境档位 {prev_sig['regime']} → {now_sig['regime']}")
        print(f"    结论日不变（{now_sig['date']}）：" + ("无变化 ✓" if not moved else ""))
        for m in moved:
            print(f"      · {m}")
    est = sorted((now.get("tail_fill") or {}).get("days") or {})
    if est:
        _m = (now.get("tail_fill") or {}).get("method")
        print(f"    ⚠ 其中 {est} 用了兜底数据（方式 {_m}）——官方出数后重跑本命令即自动覆盖并复核")


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

    # 展示层·标的走势弹窗：它**只依赖 cache/etf_panel.json + panel.json**（切片自
    # 「与结算层同一次 load_etf_book 调用」的结果），不依赖管线重放，所以放在管线
    # 之前重建。这样管线写 review.json 时它已是最新，引擎页「数据分层」上的规模数字
    # 不会比真数据旧一天。漏跑会表现成「账户已盯市到最新交易日、点开 ETF 却停在两天前」。
    print("[daily/4] 重建展示层·标的走势数据（弹窗用）...")
    if _run(py, "build_etf_webdata.py", [], "daily_etf_web") != 0:
        raise SystemExit("标的走势数据构建失败")

    # 运行前快照今日结论：跑完后对比，回答「盘前复核：结论变了吗」
    # （两段式节奏：当晚 15:05 用实时兜底出预备结论 → 次晨官方出数后重跑复核）
    _prev_today = _jload(os.path.join(ROOT, "web", "public", "data", "today.json")) or {}
    _prev_sig = _today_sig(_prev_today)

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

    print("[daily/4] 校验实验登记簿 ...")
    if _run(py, "lab_registry.py", ["check"], "daily_labreg") != 0:
        raise SystemExit("实验登记簿校验失败（web/public/data/experiments.json）——"
                         "修好再发布；生成齐全骨架：python3 engine/lab_registry.py new <id> "
                         "--kind experiment|change --title '…' --layer '…'")

    print("[daily/4] 同步部署副本 ...")
    sync_site()

    _now_today = _jload(os.path.join(ROOT, "web", "public", "data", "today.json")) or {}
    _report_today_diff(_prev_today, _prev_sig, _now_today)

    lv1 = water_level()
    print("\n[daily] 更新后水位:")
    for k, d in _last_dates(lv1):
        print(f"    {k:18s} {d}")
    moved = sorted({d for _, d in _last_dates(lv1)} - {d for _, d in _last_dates(lv0)})
    print(f"\n  水位推进: {moved if moved else '无（源未出数 / 已是最新）'}")
    if not all(lv1.get("site_synced", {}).values()):
        print("  ✗ site 未同步，请检查（data 数据层 / view 展示层）")
        return 1
    print("  ✓ site 已同步（data 数据层 + view 展示层）")

    # 每次更新都自查判断层日历：源端漏一天，系统就会静默跳过一整天
    new_gaps, _known = _judge_calendar_gaps()
    if new_gaps:
        print(f"\n  ⚠ 判断层日历缺口（基准有、panel 无）：{new_gaps}")
        print("    这些交易日申万整日无数据（ETF/基准当日有行情），系统已整天跳过——"
              "核对后补数据或登记到 config.KNOWN_JUDGE_GAPS")
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

    print("[verify] 实验登记簿 schema 校验 ...")
    r = subprocess.run([pick_python(need_akshare=False), "-u",
                        os.path.join(ENGINE, "lab_registry.py"), "check"],
                       cwd=ENGINE, capture_output=True, text=True)
    print((r.stdout or r.stderr or "").strip()[-1500:])
    if r.returncode != 0:
        print("  ✗ 实验登记簿校验失败")
        return 1
    print("  ✓ 实验登记簿合规")

    print("[verify] 账本可执行性对账（净值必须能由事件日志重放）...")
    r = subprocess.run([pick_python(need_akshare=False), "-u",
                        os.path.join(ENGINE, "verify_ledger.py")],
                       cwd=ENGINE, capture_output=True, text=True)
    print((r.stdout or "").rstrip() or (r.stderr or "").rstrip()[:500])
    if r.returncode != 0:
        return 1

    # 弹窗红线必须就是结算取价。展示层已改成**只消费结算账簿**（同一次
    # load_etf_book 调用，连 config.ETF_QFQ 开关语义都共用），所以红线在结构上
    # 已不可能违反——这项检查保留为**回归断言**：万一以后有人给展示层加回
    # 「自己算一遍复权」，这里会立刻拦住。
    print("[verify] 弹窗走势与结算取价一致性（展示层回归断言）...")
    bad = _etf_webdata_vs_settlement()
    if bad:
        print(f"  ✗ {len(bad)} 处不一致（弹窗走势 ≠ 结算取价，复权口径可能不同源）")
        for b in bad[:8]:
            print("      " + b)
        print("     修：python3 engine/build_etf_webdata.py"
              "（展示层只允许消费 pipeline.load_etf_book 的结果，不得自行复权）")
        return 1
    print("  ✓ 弹窗复权价 = 结算取价（逐位相同）")

    lv = water_level()
    fp = lv.get("L2_features_fp") or {}

    # 数据层各层必须同日；展示层（VIEW_etf_modal）也必须同日——ETF 面板更新了、
    # 但走势切片没重建，就会让页面上的标的涨跌停在旧日期（账户已盯市到最新交易日）。
    # 展示层不是一层数据，这里对它做的是「陈旧守卫」而不是依赖链校验。
    print("[verify] 判断层 / 结算层交易日集合一致 ...")
    pl = (_jload(os.path.join(ROOT, "cache", "panel.json")) or {}).get("dates") or []
    el = (_jload(os.path.join(ROOT, "cache", "etf_panel.json")) or {}).get("dates") or []
    if pl and el:
        # 允许「结算层尾部落后」（新浪 ETF 比申万晚一晚出数）：结算层日历必须是判断层的**前缀**。
        # 不允许中间有洞、也不允许结算层多出判断层没有的日期——那才是真错位。
        if pl[:len(el)] != el:
            only_p, only_e = sorted(set(pl) - set(el)), sorted(set(el) - set(pl))
            print(f"  ✗ 交易日集合不是前缀关系：panel {len(pl)} 天 / etf_panel {len(el)} 天")
            if only_e:
                print(f"     结算层多出（判断层没有）：{only_e[:8]}{' …' if len(only_e) > 8 else ''}")
                print("     修：python3 engine/build_etf_panel.py（它按 panel.dates 重建）→ daily --full")
                return 1
            print(f"     判断层多出（结算层缺）：{only_p[:8]}{' …' if len(only_p) > 8 else ''}")
            print("     这不是尾部滞后而是中间洞 → 必须先补齐 ETF 数据")
            return 1
        lag = len(pl) - len(el)
        if lag:
            print(f"  ⚠ 结算层尾部落后 {lag} 天：ETF 日线到 {el[-1]}，判断层到 {pl[-1]}"
                  f"（{'、'.join(pl[-lag:])}）")
            print("     pipeline 已把结论截到结算层末日；新浪出数后重跑 daily 即可补齐")
        else:
            print(f"  ✓ 两层同日历：{len(pl)} 个交易日")

    print("[verify] 各层水位一致性（含展示层陈旧守卫）...")
    wd = dict(_last_dates(lv))
    wd.pop("L0_realtime_fill", None)          # 兜底日期提示，不是一层数据
    uniq = sorted({d for d in wd.values() if d})
    # 「结算层尾部落后判断层」是正常状态（新浪 ETF 晚一晚出数）：pipeline 已把结论截到
    # 结算层末日，所以水位只会落在 {结算末日, 判断末日} 这两个值上。
    _pl = (_jload(os.path.join(ROOT, "cache", "panel.json")) or {}).get("dates") or []
    _el = (_jload(os.path.join(ROOT, "cache", "etf_panel.json")) or {}).get("dates") or []
    tail_lag = bool(_pl and _el and _pl[:len(_el)] == _el and len(_pl) > len(_el))
    if len(uniq) <= 1:
        print(f"  ✓ 各层同日：{uniq[0] if uniq else '—'}")
    elif tail_lag and set(uniq) <= {_el[-1], _pl[-1]}:
        print(f"  ⚠ 结算层尾部落后 {len(_pl) - len(_el)} 天（ETF 到 {_el[-1]}，申万到 {_pl[-1]}）——"
              f"结论已截到 {_el[-1]}，不算不一致；新浪出数后重跑 daily 补齐")
    elif (pt := _l0_partial_tail())[0] and set(uniq) <= {_pl[-1], pt[0]}:
        # 过渡态：L0 尾部那天只有零星几只指数有 bar，还没构成交易日（panel 不接收），
        # 所以结论停在 panel 的 last。等源端把这天出全，重跑 daily 自然推进。
        print(f"  ⚠ L0 尾部 {pt[0]} 只有 {pt[1]} 只指数有数据，未构成交易日 → "
              f"结论停在 {_pl[-1]}（源端出全后重跑 daily）")
    else:
        print(f"  ✗ 水位不一致：{uniq}")
        for k, d in sorted(wd.items()):
            print(f"      {k:18s} {d}")
        print("     排查：L0 抓取 → panel/etf_panel 重建 → pipeline --refresh → "
              "build_etf_webdata.py → ark.py daily")
        return 1

    # 判断层日历缺口：源端申万当天整日没数据 → 系统整天跳过（无 day 文件、无结论）。
    # 已核对的登记在 config.KNOWN_JUDGE_GAPS；未登记的一律失败，禁止静默多出一个洞。
    print("[verify] 判断层日历缺口（基准交易日 vs panel）...")
    new_gaps, known_gaps = _judge_calendar_gaps()
    if new_gaps:
        print(f"  ✗ {len(new_gaps)} 个未登记缺口：{new_gaps[:10]}"
              f"{' …' if len(new_gaps) > 10 else ''}")
        print("     这些交易日基准有行情、panel 没有 = 申万当日整日无数据，系统已整天跳过；")
        print("     核对 ETF 当日覆盖 → 补数据（口径变更，需批准），或登记到 config.KNOWN_JUDGE_GAPS")
        return 1
    print("  ✓ 无未登记缺口" + (f"（已登记 {len(known_gaps)} 个已知空洞，见 "
                                f"config.KNOWN_JUDGE_GAPS / docs/数据缺口-判断层整日缺失.md）"
                                if known_gaps else ""))

    # 两层日历必须是同一套交易日：etf_panel.dates 直接取自 panel.dates（build_etf_panel.py）。
    # 若「判断层补了一天」之后没重建 etf_panel，结算层就少一天——表现为我的持仓的
    # 「下一交易日」跨过那天、与结论日历对不上；水位只比最后一天，看不出这种错位。
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


def _etf_webdata_vs_settlement() -> list:
    """展示层走势（view/etf/<sid>.json 的 adj）必须与结算层取价（sim.json 的 px.c）逐位相同。

    现在两边**同源**：展示层由 build_etf_webdata.py 通过 pipeline.load_etf_book()
    切片而来，与结算层是同一次调用、同一参数、同一 ETF_QFQ 开关语义。所以这项
    检查是**回归断言**而不是"可能踩的坑"：它拦的是"以后有人给展示层加回一套自己
    的复权实现"（2026-09-24 之前的真实情形——当时展示层自己调 qfq 且无视 ETF_QFQ
    开关，一旦参照不同就相差几个百分点，同一个持仓在页面上和弹窗里显示成两个市值）。

    这里抽查每只结算标的最近 3 个有价日，逐位比对。
    """
    sim = _jload(os.path.join(OUT_DIR, "sim.json"))
    if not sim:
        return []
    sdates = sim.get("dates") or []
    bad = []
    for sid, arr in (sim.get("px", {}).get("c") or {}).items():
        f = _jload(os.path.join(VIEW_DIR, "etf", f"{sid}.json"))
        if not f:
            bad.append(f"{sid} 走势文件缺失（view/etf/{sid}.json）")
            continue
        eidx = {d: i for i, d in enumerate(f.get("dates") or [])}
        checked = 0
        for i in range(len(arr) - 1, -1, -1):
            v = arr[i] if i < len(arr) else None
            if v is None or i >= len(sdates):
                continue
            j = eidx.get(sdates[i])
            if j is None:
                continue
            w = (f.get("adj") or [])[j] if j < len(f.get("adj") or []) else None
            if w is None or abs(w - v) > max(1e-6, abs(v) * 1e-6):
                bad.append(f"{sid} {sdates[i]}：结算 {v} vs 弹窗走势 {w}")
                break
            checked += 1
            if checked >= 3:
                break
    return bad


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


# ---------------------------------------------------------------- selftest
def cmd_selftest() -> int:
    """五项快测（全部 <1min）：事后验收（verify/canary）不拦截「下次改动引入的回归」，
    这里收录改动即触发的最小检查。触发时机：改 settlement / qfq / rawstore /
    config 之后跑一次（2026-10-02 架构评审 P2-1）。

    1. 结算器 parity：lab_flex.py 的影子模拟器必须与生产 nav 逐位一致；
    2. qfq：合成份额折算序列，断言检出断点且复权后收益正确；
    3. rawstore：临时目录验证 write-once（首采值保留）与 est 覆盖通道；
    4. lab_registry：登记簿 schema 校验 0 errors / 0 warnings；
    5. config：权重和、候选档合法性。
    """
    import tempfile
    import shutil as _sh
    import subprocess

    results: list[tuple[str, bool, str]] = []
    py = pick_python(need_akshare=False)

    def check(name, fn):
        try:
            detail = fn()
            results.append((name, True, detail or ""))
        except Exception as e:                       # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))

    # 1) 结算器 parity（子进程跑 lab_flex.py verify，读生产输出做影子重放比对）
    def t_parity() -> str:
        r = subprocess.run([py, "-u", "lab_flex.py", "verify"], cwd=ENGINE,
                           capture_output=True, text=True, timeout=300)
        out = r.stdout + r.stderr
        if r.returncode != 0 or "✅ 一致" not in out:
            raise AssertionError(f"lab_flex verify 未通过（rc={r.returncode}）\n"
                                 + "\n".join(out.splitlines()[-8:]))
        line = next((ln for ln in out.splitlines() if "统一取整后" in ln), "")
        return line.strip()

    # 2) qfq：合成一次 50% 份额折算（价值中性），行业指数作参照
    def t_qfq() -> str:
        import qfq
        dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
        closes = [1.000, 1.020, 0.505, 0.510]      # 第 3 天价格腰斩 = 折算
        idx = [1000.0, 1010.0, 1015.0, 1020.0]     # 当日指数 +0.50%
        sid, sw, code = "510000", "煤炭开采", "801950"
        ep = {"dates": dates,
              "series": {sid: {d: {"open": c, "high": c, "low": c, "close": c}
                               for d, c in zip(dates, closes)}},
              "sw_to_etf": {sw: sid}, "listing": {sid: {"name": "selftest"}}}
        pn = {"meta": {code: {"name": sw}},
              "series": {code: {d: {"close": c} for d, c in zip(dates, idx)}}}
        breaks = qfq.find_breaks(ep, pn)
        bl = breaks.get(sid)
        assert bl and len(bl) == 1, f"应检出 1 处断点，实际 {breaks}"
        assert bl[0]["date"] == dates[2] and bl[0]["ref_src"] == "index", bl[0]
        out, rep = qfq.apply_qfq(ep, pn, verbose=False)
        s = out["series"][sid]
        tol = 1e-5                                  # apply_qfq 内部 round(,6)
        r_ref = idx[2] / idx[1] - 1.0               # 断点处真实收益 = 参照收益
        got = s[dates[2]]["close"] / s[dates[1]]["close"] - 1.0
        assert abs(got - r_ref) < tol, f"断点收益 {got:.6f} != 参照 {r_ref:.6f}"
        r_pre = s[dates[1]]["close"] / s[dates[0]]["close"] - 1.0
        assert abs(r_pre - 0.02) < tol, f"断点前收益被改变: {r_pre:.6f}"
        r_post = s[dates[3]]["close"] / s[dates[2]]["close"] - 1.0
        assert abs(r_post - (closes[3] / closes[2] - 1.0)) < tol, "断点后收益被改变"
        return (f"检出 1 断点 factor={rep[sid][0]['factor']:.4f}，"
                f"复权后断点收益 {got:+.4%} = 参照 {r_ref:+.4%}")

    # 3) rawstore：monkeypatch 到临时目录，验证 write-once 与 est 覆盖
    def t_rawstore() -> str:
        import rawstore as RS
        tmp = tempfile.mkdtemp(prefix="ark_selftest_l0_")
        old_root, old_man = RS.RAW_ROOT, RS.MANIFEST_PATH
        RS.RAW_ROOT, RS.MANIFEST_PATH = tmp, os.path.join(tmp, "manifest.json")
        try:
            rows1 = [{"date": "2026-01-02", "close": 1.0},
                     {"date": "2026-01-03", "close": 2.0, "est": True}]
            RS.put("selftest_kind", "k1", rows1, source="selftest", merge=False)
            # 重复 put：重叠日期必须保留首采值，新日期追加
            RS.put("selftest_kind", "k1",
                   [{"date": "2026-01-03", "close": 99.0},
                    {"date": "2026-01-06", "close": 3.0}], source="selftest")
            d = {r["date"]: r for r in RS.get("selftest_kind", "k1")}
            assert d["2026-01-02"]["close"] == 1.0, "首采值被覆盖"
            assert d["2026-01-03"]["close"] == 2.0 and d["2026-01-03"].get("est"), \
                "est 首采值被覆盖"
            assert d["2026-01-06"]["close"] == 3.0, "新日期未追加"
            # est 覆盖通道：官方值可以替换估算值（唯一例外）
            RS.put("selftest_kind", "k1",
                   [{"date": "2026-01-03", "close": 2.5}], source="selftest",
                   overwrite_est=True)
            d = {r["date"]: r for r in RS.get("selftest_kind", "k1")}
            assert d["2026-01-03"]["close"] == 2.5 and not d["2026-01-03"].get("est"), \
                "官方值未能替换 est"
            return "首采值保留 ✓ 新日期追加 ✓ est 官方覆盖 ✓"
        finally:
            RS.RAW_ROOT, RS.MANIFEST_PATH = old_root, old_man
            _sh.rmtree(tmp, ignore_errors=True)

    # 4) lab_registry：登记簿 schema 校验（stderr 必须为空，见 2026-10-02 弃用修复）
    def t_registry() -> str:
        r = subprocess.run([py, "lab_registry.py", "check"], cwd=ENGINE,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"rc={r.returncode}\n{r.stdout[-500:]}")
        assert not r.stderr.strip(), f"stderr 非空: {r.stderr[-300:]}"
        first = r.stdout.splitlines()[0].strip() if r.stdout else ""
        return first

    # 5) config 合法性
    def t_config() -> str:
        import config as C
        s = sum(C.BASE_WEIGHTS.values())
        assert abs(s - 1.0) < 1e-9, f"BASE_WEIGHTS 权重和 {s} != 1"
        assert all(w > 0 for w in C.BASE_WEIGHTS.values()), "存在非正权重"
        hs = C.HORIZON_CANDIDATES
        assert len(hs) >= 2 and hs == sorted(set(hs)), f"候选档非法: {hs}"
        assert all(isinstance(h, int) and h > 0 for h in hs), f"候选档非法: {hs}"
        assert all(v >= 0 for g in C.TRADING_COST.values()
                   for v in (g.values() if isinstance(g, dict) else [g])), \
            "交易成本存在负值"
        return (f"BASE_WEIGHTS 和=1 ✓ HORIZON_CANDIDATES={hs} 严格递增 ✓ "
                f"TRADING_COST 非负 ✓")

    check("1 结算器 parity  (lab_flex vs 生产 nav 逐位)", t_parity)
    check("2 qfq 复权       (合成折算断点)", t_qfq)
    check("3 rawstore       (write-once + est 通道)", t_rawstore)
    check("4 lab_registry   (schema 校验)", t_registry)
    check("5 config 合法性  (权重/候选档/成本)", t_config)

    print("\n[selftest] 结果：")
    for name, ok, detail in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if detail:
            print(f"      {detail}")
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n  {'✅ 全部通过' if n_fail == 0 else f'❌ {n_fail} 项失败'}"
          f"（5 项，触发时机：改 settlement/qfq/rawstore/config 后）")
    return 0 if n_fail == 0 else 1


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
    if cmd == "selftest":
        raise SystemExit(cmd_selftest())
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
