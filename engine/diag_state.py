# -*- coding: utf-8 -*-
"""状态条件收益检验（只读）：引擎「看不上」的状态，事后到底赚不赚钱？

候选状态（全部只用当日及以前的特征，无前视）：
  MOMO  「无回调强趋势」：mainline>=65 且 ret_20>-2% 且 ret_60>0
        —— 引擎两种入场语义都不覆盖它（AMBUSH 要回调、BUY_NOW 要跌后转强）
  AMB   「强趋势 + 深回调」：mainline>=52 且 pullback>=52 且 exhaust>=50
        —— 引擎的埋伏状态（pullback 分位门槛另算）
  C56   composite>=56（引擎的入选阈值）

收益口径：T+1 收盘买、持有 60 个交易日收盘卖，用**行业指数**（申万），
超额 = 相对当日行业等权基准（EW）。全区间 + 逐年，另给当日全截面平均作对照。

读法：某状态若只在单一年份好、跨年不成立，就是事后噪音（项目防过拟合准则④）。

运行：python3 engine/diag_state.py
"""
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import scoring as SC  # noqa: E402
from feature_store import EW_CODE, FeatureStore  # noqa: E402

H = 60
COST = C.ROUND_TRIP_COST


def main() -> None:
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    reg = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    dims, comp = SC.build_score_matrix(fs, reg, verbose=False)
    dates = fs.dates
    n = len(dates)
    ew = fs.series(EW_CODE, "close") if EW_CODE in fs.codes else None
    if ew is None:
        print("缺行业等权基准，退出")
        return

    # 预取各行业收盘价数组
    closes = {c: fs.series(c, "close") for c in fs.codes}
    ml = dims["mainline"]
    pb = dims["pullback"]
    ex = dims["exhaust"]
    r20 = {c: fs.series(c, "ret_20") for c in fs.codes}
    r60 = {c: fs.series(c, "ret_60") for c in fs.codes}

    acc = defaultdict(lambda: {"ret": [], "ex": []})
    cidx = {c: i for i, c in enumerate(fs.codes)}
    for i in range(n - H - 2):
        d = dates[i]
        y = d[:4]
        cands = fs.investable(d)
        if not cands:
            continue
        b_i, s_i = i + 1, i + 1 + H
        bb, bs = ew[b_i], ew[s_i]
        if not bb or not bs:
            continue
        ex_ew = bs / bb - 1.0
        for c in cands:
            ci = cidx[c]
            cl = closes[c]
            p0, p1 = cl[b_i], cl[s_i]
            if not p0 or not p1 or p0 <= 0:
                continue
            rr = p1 / p0 - 1.0 - COST
            e = rr - (ex_ew - COST)
            m_, p_, e_, c_ = ml[ci, i], pb[ci, i], ex[ci, i], comp[ci, i]
            r20v, r60v = r20[c][i], r60[c][i]
            acc[(y, "ALL")]["ret"].append(rr)
            acc[(y, "ALL")]["ex"].append(e)
            hit = False
            if (not np.isnan(m_) and not np.isnan(r20v) and not np.isnan(r60v)
                    and m_ >= 65 and r20v > -0.02 and r60v > 0):
                acc[(y, "MOMO")]["ret"].append(rr)
                acc[(y, "MOMO")]["ex"].append(e)
                hit = True
            if (not np.isnan(m_) and not np.isnan(p_) and not np.isnan(e_)
                    and m_ >= 52 and p_ >= 52 and e_ >= 50):
                acc[(y, "AMB")]["ret"].append(rr)
                acc[(y, "AMB")]["ex"].append(e)
                hit = True
            if not np.isnan(c_) and c_ >= 56:
                acc[(y, "C56")]["ret"].append(rr)
                acc[(y, "C56")]["ex"].append(e)
                hit = True
            if not hit:
                acc[(y, "OTHER")]["ret"].append(rr)
                acc[(y, "OTHER")]["ex"].append(e)

    names = {"MOMO": "无回调强趋势", "AMB": "强趋势+深回调", "C56": "综合分≥56",
             "OTHER": "其余(未命中)", "ALL": "全截面平均"}
    years = sorted({k[0] for k in acc})
    for st in ["MOMO", "AMB", "C56", "ALL"]:
        print(f"\n=== 状态 {st}（{names[st]}）逐年：持有 {H} 日超额 / 收益 / 胜率 ===")
        print(f"{'年':<7}{'样本':>7}{'超额':>9}{'收益':>9}{'胜率':>8}{'超额胜率':>10}")
        for y in years:
            v = acc[(y, st)]
            if not v["ret"]:
                continue
            rs = np.array(v["ret"])
            es = np.array(v["ex"])
            print(f"{y:<7}{rs.size:>7}{es.mean()*100:>8.2f}%{rs.mean()*100:>8.2f}%"
                  f"{(rs>0).mean()*100:>7.0f}%{(es>0).mean()*100:>9.0f}%")
        rs = np.concatenate([acc[(y, st)]["ret"] for y in years]).astype(float)
        es = np.concatenate([acc[(y, st)]["ex"] for y in years]).astype(float)
        print(f"{'全区间':<7}{rs.size:>7}{es.mean()*100:>8.2f}%{rs.mean()*100:>8.2f}%"
              f"{(rs>0).mean()*100:>7.0f}%{(es>0).mean()*100:>9.0f}%")

    print("\n=== 2024 专项：各状态 ===")
    print(f"{'状态':<14}{'样本':>7}{'超额':>9}{'收益':>9}{'胜率':>8}")
    for st in ["MOMO", "AMB", "C56", "ALL", "OTHER"]:
        v = acc[("2024", st)]
        if not v["ret"]:
            continue
        rs = np.array(v["ret"])
        es = np.array(v["ex"])
        print(f"{names[st]:<14}{rs.size:>7}{es.mean()*100:>8.2f}%{rs.mean()*100:>8.2f}%"
              f"{(rs>0).mean()*100:>7.0f}%")

    out = {f"{y}|{st}": {"n": len(acc[(y, st)]["ret"]),
                         "ret": float(np.mean(acc[(y, st)]["ret"])),
                         "ex": float(np.mean(acc[(y, st)]["ex"]))}
           for y in years for st in ["MOMO", "AMB", "C56", "ALL", "OTHER"]
           if acc[(y, st)]["ret"]}
    json.dump(out, open(f"{C.CACHE_DIR}/diag_state.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/diag_state.json")


if __name__ == "__main__":
    main()
