# -*- coding: utf-8 -*-
"""信号层突破实验：G4 平台检验 + G3 剂量检验 + 组合（在 ark-1.6.0 基线上）。

背景（experiments.json gate-2024 条目）：G4（AMBUSH 综合分门槛改当日截面 P90
且 ≥50）在 1.4.0 基线上四项全优但只有单档，按准则②③需补平台检验
（P85/P90/P95 × floor 45/50/55）；G3（ml_gate 下限 0.60）只有一档，需补
0.55/0.70 剂量。当时基线未含 qfq 与 F5，本台在 ark-1.6.0 生产口径上重测
（qfq 已是生产默认，不需 ARK_QFQ；F5 在 review 主口径内，重放自动带上）。

变体（P0 为回归校验，必须与生产 day 文件逐字节一致）：
  P0        生产口径原样
  G4_P85    ARK_COMP_PCT=85, FLOOR=50
  G4_P90    ARK_COMP_PCT=90, FLOOR=50   （原 G4 档）
  G4_P95    ARK_COMP_PCT=95, FLOOR=50
  G4_F45    ARK_COMP_PCT=90, FLOOR=45
  G4_F55    ARK_COMP_PCT=90, FLOOR=55
  G3_055    ARK_PB_GATE_BASE=0.55
  G3_070    ARK_PB_GATE_BASE=0.70
  G4G3      P90/F50 + PB_GATE_BASE=0.60

运行：python3 engine/lab_signal_break.py   （9 次全量重放，并发 4，约 25 分钟）
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

ENGINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ENGINE)
import config as C  # noqa: E402
from lab_gate2024 import metrics, run_variant  # noqa: E402

VARIANTS = [
    ("P0", "生产口径原样(回归校验)", {}),
    ("G4_P85", "截面P85/floor50", {"ARK_COMP_PCT": "85", "ARK_COMP_FLOOR": "50"}),
    ("G4_P90", "截面P90/floor50", {"ARK_COMP_PCT": "90", "ARK_COMP_FLOOR": "50"}),
    ("G4_P95", "截面P95/floor50", {"ARK_COMP_PCT": "95", "ARK_COMP_FLOOR": "50"}),
    ("G4_F45", "截面P90/floor45", {"ARK_COMP_PCT": "90", "ARK_COMP_FLOOR": "45"}),
    ("G4_F55", "截面P90/floor55", {"ARK_COMP_PCT": "90", "ARK_COMP_FLOOR": "55"}),
    ("G3_055", "ml_gate下限0.55", {"ARK_PB_GATE_BASE": "0.55"}),
    ("G3_070", "ml_gate下限0.70", {"ARK_PB_GATE_BASE": "0.70"}),
    ("G4G3", "P90F50+ml_gate0.60", {"ARK_COMP_PCT": "90", "ARK_COMP_FLOOR": "50",
                                    "ARK_PB_GATE_BASE": "0.60"}),
]
CONCURRENCY = 4

_ONLY = os.environ.get("ARK_LAB_ONLY")
if _ONLY:
    _keep = {t.strip() for t in _ONLY.split(",") if t.strip()}
    VARIANTS = [v for v in VARIANTS if v[0] in _keep]


def yearly_stats(yearly: dict) -> dict:
    """稳定性指标：负年数 / 最差年 / 逐年 std。"""
    ys = [v for y, v in sorted(yearly.items()) if y not in ("2015",)]
    neg = sum(1 for v in ys if v < 0)
    return {"neg_years": neg, "worst_year": min(ys) if ys else None,
            "year_std": float(np_std(ys)) if len(ys) > 1 else 0.0}


def np_std(xs):
    import numpy as np
    return np.std(xs, ddof=1)


def main() -> None:
    import time
    import json
    t0 = time.time()
    outs = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(run_variant, tag, env): tag for tag, _d, env in VARIANTS}
        for f in futs:
            tag = futs[f]
            try:
                outs[tag] = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"[{tag}] 异常 {e}")
                outs[tag] = None

    # P0 回归校验
    import filecmp
    import glob
    if outs.get("P0"):
        prod_dir = f"{C.ROOT}/web/public/data/day"
        p0_dir = f"{outs['P0']}/day"
        files = sorted(glob.glob(f"{p0_dir}/*.json"))
        diff = [f for f in files
                if not filecmp.cmp(f, f"{prod_dir}/{os.path.basename(f)}", shallow=False)]
        parity = f"{len(files) - len(diff)}/{len(files)}"
        print(f"=== 回归校验 P0 vs 生产：{parity} 个 day 文件逐字节一致"
              f"{' ✓' if not diff else ' ✗ 不一致: ' + str(diff[:3])}")
    else:
        parity = None

    rows = []
    for tag, desc, _e in VARIANTS:
        od = outs.get(tag)
        m = metrics(f"{od}/review.json") if od else None
        if m:
            m.update(yearly_stats(m["yearly"]))
        rows.append((tag, desc, m))

    print(f"\n全部重放完成，用时 {(time.time()-t0)/60:.1f} 分钟\n")
    print("=== 全区间指标 + 稳定性 ===")
    print(f"{'变体':<8}{'cagr':>8}{'maxdd':>9}{'sharpe':>8}{'calmar':>8}"
          f"{'负年':>5}{'最差年':>9}{'年std':>7}{'成交':>6}")
    for tag, desc, m in rows:
        if not m:
            print(f"{tag:<8} FAIL")
            continue
        print(f"{tag:<8}{m['cagr']*100:>7.2f}%{m['maxdd']*100:>8.2f}%"
              f"{m['sharpe']:>8.3f}{m['calmar']:>8.3f}"
              f"{m['neg_years']:>5}{m['worst_year']*100:>8.2f}%"
              f"{m['year_std']*100:>6.1f}%{m['executed']:>6}")

    print("\n=== 逐年 ===")
    years = sorted({y for _t, _d, m in rows if m for y in m["yearly"]})
    hdr = "变体    " + "".join(f"{y:>9}" for y in years)
    print(hdr)
    for tag, _d, m in rows:
        if not m:
            continue
        print(f"{tag:<8}" + "".join(
            f"{m['yearly'].get(y, float('nan'))*100:>8.2f}%" for y in years))

    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "parity": parity,
           "variants": {tag: m for tag, _d, m in rows if m}}
    json.dump(out, open(f"{C.CACHE_DIR}/lab_signal_break.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/lab_signal_break.json")


if __name__ == "__main__":
    main()
