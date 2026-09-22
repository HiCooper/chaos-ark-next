# -*- coding: utf-8 -*-
"""结论闸门专项实验台（L4 一次性产物，默认口径不变）。

## 背景（2026-09-22 诊断，见 engine/diag_2024.py / diag_gate.py / diag_gate2.py）

2024 年组合 −5.5%、基准 +14.9%（−20.4pp）。拆解发现：
  · 1–11 月组合收益恰好 0.0% —— 242 个交易日里 233 天**零结论**（完全空仓）；
  · 9 月基准 +24.8%（924 行情）时，我们的打分反而是全区间最低（日均最高分 45.7）；
  · 12 月首次出手，9 笔结算胜率 22%、平均 −3.4%，且半导体设备连买 5 次全亏。

根因不在「被过滤/回避砍掉」（filt/avoid 几乎为 0），而在**闸门本身过不去**：
AMBUSH 需要 composite≥56 且 mainline≥52 且 **pullback≥52** 且 exhaust≥50 同时成立，
其中 pullback 在打分层被主线闸门乘法挤压：

    pullback = pullback_raw × (0.45 + 0.55 × mainline/100)

2024 年同一份截面：不乘 ml_gate 有 **55.8 个/天** 达 52 分，乘完只剩 **0.40 个/天**
（140 倍压制）。即「埋伏」只在「行业深崩 + 长期主线仍强」的极端组合下才成立，
而 2024 恰好是「强势行业不深调、深调行业主线分低」——两侧都不满足。

## 变体

  B0    生产口径 + 折算复权（对照基准；应为「数据修正后的真基线」）
  G1    pullback 门槛 52 → 42（温和放松）
  G1b   pullback 门槛 52 → 48（剂量响应中间档）
  G2    pullback 条件取消（≈把 ml_gate 视为 1.0）
  G3    主线闸门下限 0.45 → 0.60（软化乘法挤压，机制定向修复）
  G4    AMBUSH 综合分门槛换成「当日截面 P90 且 ≥50」（自适应门槛）
  G5    关闭环境权重调整（全 NEUTRAL），检验 PANIC/DEFEND 压 mainline 的影响

评估按项目防过拟合四条准则：① 先精确复现基线；② 方向跨参数档一致；
③ 参数响应单调；④ 单年耀眼增益默认视为嫌疑。

运行：python3 engine/lab_gate2024.py        （7 次全量重放，串行约 40 分钟）
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ENGINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ENGINE)
import config as C  # noqa: E402

# (tag, 说明, 环境变量)
VARIANTS = [
    ("P0", "生产口径原样(回归校验)", {}),
    ("B0", "生产口径+qfq", {"ARK_QFQ": "1"}),
    ("G1", "pullback>=42", {"ARK_QFQ": "1", "ARK_AMBUSH_PB_MIN": "42"}),
    ("G1b", "pullback>=48", {"ARK_QFQ": "1", "ARK_AMBUSH_PB_MIN": "48"}),
    ("G2", "取消pullback条件", {"ARK_QFQ": "1", "ARK_AMBUSH_PB_MIN": "-1"}),
    ("G3", "ml_gate下限0.60", {"ARK_QFQ": "1", "ARK_PB_GATE_BASE": "0.60"}),
    ("G4", "综合分截面P90", {"ARK_QFQ": "1", "ARK_COMP_PCT": "90", "ARK_COMP_FLOOR": "50"}),
    ("G5", "关闭环境权重", {"ARK_QFQ": "1", "ARK_REGIME_ADJ": "off"}),
    ("G6", "加趋势延续入场65", {"ARK_QFQ": "1", "ARK_MOMO_ML": "65"}),
    ("G6b", "加趋势延续入场75", {"ARK_QFQ": "1", "ARK_MOMO_ML": "75"}),
]
CONCURRENCY = 4

# 只跑指定变体（补跑用）：ARK_LAB_ONLY=G6,G6b
# 补跑时建议带上 B0（逐年基线）与 P0（回归校验），例：ARK_LAB_ONLY=P0,B0,G6,G6b
_ONLY = os.environ.get("ARK_LAB_ONLY")
if _ONLY:
    _keep = {t.strip() for t in _ONLY.split(",") if t.strip()}
    VARIANTS = [v for v in VARIANTS if v[0] in _keep]


def run_variant(tag: str, envs: dict) -> str | None:
    outdir = f"{C.CACHE_DIR}/lab_gate_{tag}"
    env = os.environ.copy()
    env.update(envs)
    env.pop("HTTP_PROXY", None), env.pop("HTTPS_PROXY", None)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {ENGINE!r})\n"
        "import config as C\n"
        f"C.OUT_DIR = {outdir!r}\n"
        "import pipeline\n"
        "sys.argv = ['pipeline.py', '--all']\n"
        "pipeline.main()\n"
    )
    log = f"/tmp/lab_gate_{tag}.log"
    t0 = time.time()
    with open(log, "w") as f:
        r = subprocess.run([sys.executable, "-u", "-c", code], env=env,
                           stdout=f, stderr=subprocess.STDOUT, cwd=ENGINE)
    ok = r.returncode == 0
    print(f"[{tag}] -> {'OK' if ok else 'FAIL'} ({time.time()-t0:.0f}s) log={log}", flush=True)
    return outdir if ok else None


def metrics(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    r = json.load(open(path))
    stl = r.get("settlement")
    if not stl:
        return None
    cur = stl["curve"]
    # 逐年收益 = 本年末净值 / 上年末净值 - 1。
    # 注意勿写成 nav[i] / nav[i-1]：那只是「年末最后一段」的单期收益，多数年份无持仓会恒为
    # 0，无法当年度盈亏读（2026-09-22 首轮曾因此得到一张几乎无效的逐年表，已修正）。
    yr = {}
    for i, d in enumerate(cur["dates"]):
        yr[d[:4]] = i          # 该年最后一个交易日的全局下标
    yearly, prev = {}, None
    for y in sorted(yr):
        i = yr[y]
        yearly[y] = cur["nav"][i] / cur["nav"][prev if prev is not None else 0] - 1
        prev = i
    ev = stl.get("events", [])
    s = stl["stats"]
    # 现象层指标：结论密度（出结论的交易日占比）与 2024 是否恢复出手
    dens = {}
    for y in yearly:
        dens[y] = None
    return {
        "cagr": s["cagr"], "maxdd": s["maxdd"], "sharpe": s.get("sharpe"),
        "calmar": s.get("calmar"), "vol": s.get("vol"),
        "yearly": yearly,
        "n_B": sum(1 for e in ev if e["k"] == "B"),
        "n_S": sum(1 for e in ev if e["k"] == "S"),
        "win_excess": stl.get("win_rate_excess"),
        "n_settled": stl.get("n_settled"),
        "avg_exposure": s.get("avg_exposure"),
        "executed": s.get("executed_trades"),
        "overview": {
            "evaluated": r["overview"]["evaluated"],
            "win_rate": r["overview"]["win_rate"],
        },
    }


def conclusion_density(outdir: str) -> dict:
    """按年统计「出结论的交易日占比」与结论数（现象层，看哑火是否被解除）。"""
    import glob
    per = {}
    for p in glob.glob(f"{outdir}/day/*.json"):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        dt = d.get("date")
        if not dt:
            continue
        v = per.setdefault(dt[:4], {"days": 0, "with": 0, "picks": 0})
        v["days"] += 1
        n = len(d.get("picks") or [])
        v["picks"] += n
        if n:
            v["with"] += 1
    return {y: {"days": v["days"], "act_days": v["with"],
                "picks": v["picks"]} for y, v in per.items()}


def main() -> None:
    t0 = time.time()
    outs = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(run_variant, tag, env): tag for tag, _d, env in VARIANTS}
        for f in futs:
            pass
        for f in futs:
            tag = futs[f]
            try:
                outs[tag] = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"[{tag}] 异常 {e}")
                outs[tag] = None
    print(f"\n全部重放完成，用时 {(time.time()-t0)/60:.1f} 分钟\n", flush=True)

    rows = []
    for tag, desc, _e in VARIANTS:
        od = outs.get(tag)
        m = metrics(f"{od}/review.json") if od else None
        dens = conclusion_density(od) if od else None
        rows.append((tag, desc, m, dens))

    # ---- 回归校验：P0（不加任何 env）必须与生产 day 文件逐字节一致 ----
    if outs.get("P0"):
        import filecmp
        import glob
        prod_dir = f"{C.ROOT}/web/public/data/day"
        p0_dir = f"{outs['P0']}/day"
        files = sorted(glob.glob(f"{p0_dir}/*.json"))
        diff = [f for f in files if not filecmp.cmp(f, f"{prod_dir}/{os.path.basename(f)}", shallow=False)]
        print(f"=== 回归校验 P0 vs 生产：{len(files) - len(diff)}/{len(files)} 个 day 文件逐字节一致"
              f"{' ✓' if not diff else ' ✗ 不一致: ' + str(diff[:5])}")

    base = next((m for t, _d, m, _ in rows if t == "B0"), None)
    print("=== 全区间指标 ===")
    print(f"{'变体':<5}{'说明':<18}{'cagr':>8}{'maxdd':>8}{'sharpe':>7}{'calmar':>7}"
          f"{'信号胜率':>9}{'B':>5}{'S':>5}{'成交':>6}{'暴露':>7}")
    for tag, desc, m, _dn in rows:
        if not m:
            print(f"{tag:<5}{desc:<18}  FAIL")
            continue
        print(f"{tag:<5}{desc:<18}{m['cagr']:>8.4f}{m['maxdd']:>8.4f}{m['sharpe']:>7.3f}"
              f"{m['calmar']:>7.3f}{(m['win_excess'] or 0):>9.3f}{m['n_B']:>5}{m['n_S']:>5}"
              f"{m['executed']:>6}{m['avg_exposure']:>7.3f}")

    print("\n=== 结论密度：出结论的交易日占比（哑火是否解除）===")
    years = sorted({y for _t, _d, _m, dn in rows if dn for y in dn})
    hdr = "".join(f"{y[2:]:>8}" for y in years)
    print(f"{'变体':<5}{'日均结论数':>11}{hdr}")
    for tag, desc, m, dn in rows:
        if not dn:
            continue
        tot_p = sum(v["picks"] for v in dn.values())
        tot_d = sum(v["days"] for v in dn.values())
        line = "".join(f"{(dn[y]['act_days']/dn[y]['days']*100 if y in dn and dn[y]['days'] else 0):>7.0f}%"
                       for y in years)
        print(f"{tag:<5}{tot_p/max(tot_d,1):>11.2f}{line}")

    print("\n=== 逐年组合收益（B0 基线 -> 变体）===")
    for tag, desc, m, _dn in rows:
        if not m or not base:
            continue
        print(f"\n{tag} ({desc})")
        for y in sorted(base["yearly"]):
            a = base["yearly"][y]
            c = m["yearly"].get(y)
            if c is None:
                continue
            mark = " *" if abs(c - a) > 0.005 else ""
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%{mark}")

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "variants": {tag: {"desc": desc, "env": env,
                           "metrics": m, "density": dn}
                     for (tag, desc, env), (_t, _d, m, dn) in zip(VARIANTS, rows)},
    }
    json.dump(out, open(f"{C.CACHE_DIR}/lab_gate2024.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/lab_gate2024.json")


if __name__ == "__main__":
    main()
