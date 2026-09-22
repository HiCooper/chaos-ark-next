# -*- coding: utf-8 -*-
"""2024 专项诊断（只读，不写任何生产数据）。

目的：把「2024 我们跑输基准 20pp」拆成可归因的三层：
  L1 机会层：2024 全市场/我们可交易池里，哪些方向真的涨了（有没有机会）
  L2 信号层：我们的结论方向对不对（单笔净收益 / 相对基准超额）
  L3 组合层：方向就算对，钱有没有落到净值上（暴露、容量、重复接飞刀）

输出：缓存 cache/diag_2024.json + stdout 报表
运行：python3 engine/diag_2024.py
"""
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import settlement as ST  # noqa: E402

YEAR = "2024"


def monthly_curve(dates, nav, bench):
    """按月取末值，返回 {月份: (组合收益, 基准收益)}（月度相对上月最后一日）。"""
    marks = {}
    for i, d in enumerate(dates):
        if d[:4] < YEAR:
            prev = i
        if d[:4] == YEAR:
            marks.setdefault(d[:7], [i, i, prev])[1] = i
    out = {}
    for m in sorted(marks):
        lo, hi, _ = marks[m]
        a = nav[lo - 1] if lo > 0 else nav[lo]
        b = bench[lo - 1] if lo > 0 else bench[lo]
        out[m] = (nav[hi] / a - 1.0, bench[hi] / b - 1.0)
    return out


def main() -> None:
    r = json.load(open(f"{C.OUT_DIR}/review.json"))
    stl = r["settlement"]
    dates = stl["curve"]["dates"]
    nav = np.array(stl["curve"]["nav"], dtype=float)
    bench = np.array(stl["curve"]["bench"], dtype=float)
    events = stl["events"]

    rep = {}

    # ---------------------------------------------------- L3 组合层：月度净值
    mc = monthly_curve(dates, nav, bench)
    print(f"=== [{YEAR}] 组合 vs 基准（沪深300ETF华泰柏瑞）月度 ===")
    print(f"{'月份':<9}{'组合':>9}{'基准':>9}{'超额':>9}")
    cum_a = cum_b = 1.0
    for m, (a, b) in mc.items():
        cum_a *= 1 + a
        cum_b *= 1 + b
        print(f"{m:<9}{a*100:>8.1f}%{b*100:>8.1f}%{(a-b)*100:>8.1f}%")
    print(f"{'全年':<9}{(cum_a-1)*100:>8.1f}%{(cum_b-1)*100:>8.1f}%{(cum_a-cum_b)*100:>8.1f}%")
    rep["monthly"] = {m: {"port": round(a, 5), "bench": round(b, 5)} for m, (a, b) in mc.items()}
    rep["year"] = {"port": round(cum_a - 1, 5), "bench": round(cum_b - 1, 5)}

    # ---------------------------------------------------- L2 信号层：2024 结算单笔
    sells = [e for e in events if e["k"] == "S" and e.get("bd", "")[:4] == YEAR]
    by_m_sell = defaultdict(list)
    for e in sells:
        by_m_sell[e["bd"][:7]].append(e)
    print(f"\n=== 信号层：{YEAR} 年内买入且已结算 {len(sells)} 笔 ===")
    rets = np.array([e["ret"] for e in sells], dtype=float)
    exs = np.array([e["ex"] for e in sells if e.get("ex") is not None], dtype=float)
    print(f"平均净收益 {rets.mean()*100:+.2f}%  胜率 {(rets>0).mean()*100:.1f}%  "
          f"平均超额 {exs.mean()*100:+.2f}%  超额胜率 {(exs>0).mean()*100:.1f}%")
    rep["signal"] = {"n": len(sells), "avg_ret": round(float(rets.mean()), 5),
                     "win": round(float((rets > 0).mean()), 4),
                     "avg_ex": round(float(exs.mean()), 5),
                     "win_ex": round(float((exs > 0).mean()), 4)}

    # 按买入月看「什么时候开的仓是好的」
    print(f"\n{'买入月':<9}{'笔数':>5}{'净收益':>9}{'胜率':>8}{'超额':>9}{'组合当月':>10}")
    for m in sorted(by_m_sell):
        g = by_m_sell[m]
        rr = np.array([e["ret"] for e in g], dtype=float)
        ee = np.array([e["ex"] for e in g if e.get("ex") is not None], dtype=float)
        pm = mc.get(m, (float("nan"), float("nan")))[0]
        print(f"{m:<9}{len(g):>5}{rr.mean()*100:>8.2f}%{(rr>0).mean()*100:>7.0f}%"
              f"{ee.mean()*100:>8.2f}%{pm*100:>9.1f}%")
    rep["by_buy_month"] = {
        m: {"n": len(g), "avg_ret": round(float(np.mean([e["ret"] for e in g])), 5)}
        for m, g in by_m_sell.items()}

    # ---------------------------------------------------- L2b 标的重犯：同一行业反复进
    by_sw = defaultdict(list)
    for e in sells:
        by_sw[e["sw"]].append(e)
    print(f"\n=== {YEAR} 亏损贡献榜（行业 × 笔数 × 累计净收益，按累计升序）===")
    print(f"{'行业':<14}{'笔数':>5}{'均净收益':>10}{'胜率':>7}{'累计':>10}")
    rows = []
    for sw, g in by_sw.items():
        rr = np.array([e["ret"] for e in g], dtype=float)
        rows.append((sw, len(g), rr.mean(), (rr > 0).mean(), rr.sum()))
    for sw, n, mu, wr, tot in sorted(rows, key=lambda x: x[4])[:14]:
        print(f"{sw:<14}{n:>5}{mu*100:>9.2f}%{wr*100:>6.0f}%{tot*100:>9.1f}%")
    print("  ...")
    for sw, n, mu, wr, tot in sorted(rows, key=lambda x: -x[4])[:8]:
        print(f"{sw:<14}{n:>5}{mu*100:>9.2f}%{wr*100:>6.0f}%{tot*100:>9.1f}%")
    rep["by_sw"] = {sw: {"n": n, "avg": round(float(mu), 5), "sum": round(float(tot), 5),
                         "win": round(float(wr), 4)} for sw, n, mu, wr, tot in rows}

    # 单笔最惨 15 笔
    print(f"\n=== {YEAR} 单笔最差 15 笔 ===")
    print(f"{'买入日':<12}{'行业':<14}{'持有':>5}{'净收益':>9}{'超额':>9}{'动作':>9}")
    for e in sorted(sells, key=lambda x: x["ret"])[:15]:
        print(f"{e['bd']:<12}{e['sw']:<14}{e['h']:>5}{e['ret']*100:>8.1f}%"
              f"{(e.get('ex') or 0)*100:>8.1f}%{str(e.get('a')):>9}")
    rep["worst"] = [{"bd": e["bd"], "sw": e["sw"], "h": e["h"], "ret": e["ret"],
                     "ex": e.get("ex"), "a": e.get("a")}
                    for e in sorted(sells, key=lambda x: x["ret"])[:15]]

    # ---------------------------------------------------- L1 机会层：可交易池 2024 涨跌
    book = ST.EtfBook(ST.load_etf_panel(f"{C.CACHE_DIR}/etf_panel.json"))
    i0 = next(i for i, d in enumerate(book.dates) if d[:4] == YEAR)
    i1 = max(i for i, d in enumerate(book.dates) if d[:4] == YEAR)
    uni = []
    for sid in book.series:
        p0, p1 = book.close(sid, i0), book.close(sid, i1)
        if p0 and p1 and p0 > 0:
            nm = book.listing.get(sid, {}).get("name", sid)
            uni.append((p1 / p0 - 1.0, nm, sid))
    uni.sort(reverse=True)
    print(f"\n=== L1 可交易 ETF 池 {YEAR} 涨跌（共 {len(uni)} 只，年内数据完整）===")
    print(f"{'涨幅前 12':<14}{'收益':>9}   |  {'跌幅前 8':<14}{'收益':>9}")
    top, bot = uni[:12], uni[-8:]
    for k in range(max(len(top), len(bot))):
        s = ""
        if k < len(top):
            s += f"{top[k][1][:12]:<14}{top[k][0]*100:>8.1f}%"
        else:
            s += " " * 23
        s += "   |  "
        if k < len(bot):
            s += f"{bot[k][1][:12]:<14}{bot[k][0]*100:>8.1f}%"
        print(s)
    med = float(np.median([x[0] for x in uni]))
    print(f"中位数 {med*100:+.1f}%  上涨 {(np.array([x[0] for x in uni])>0).mean()*100:.0f}%")
    rep["universe"] = {"n": len(uni), "median": round(med, 5),
                       "top": [(nm, round(v, 4)) for v, nm, _ in uni[:15]],
                       "bottom": [(nm, round(v, 4)) for v, nm, _ in uni[-10:]]}

    # ---------------------------------------------------- 我们是否覆盖了涨的方向
    traded = {e["sw"] for e in sells}
    hit = [x for x in uni if any(nm and nm[:2] in x[1] for nm in [])]
    print(f"\n=== 方向覆盖：{YEAR} 我们实际交易了 {len(traded)} 个行业 ===")
    print("  " + "、".join(sorted(traded)))
    topn = [nm for _, nm, _ in uni[:15]]
    print(f"  池内涨幅前 15（ETF 名）：{'、'.join(topn)}")
    rep["traded"] = sorted(traded)
    rep["pool_top"] = topn

    # ---------------------------------------------------- 逐日结论密度 + 环境
    print(f"\n=== 逐日结论与环境（按月汇总）===")
    print(f"{'月份':<9}{'交易日':>6}{'出结论日':>8}{'结论数':>7}{'入手':>6}{'埋伏':>6}{'回避':>6}"
          f"{'环境分布':>28}")
    mstat = defaultdict(lambda: {"days": 0, "pdays": 0, "n": 0, "bn": 0, "am": 0,
                                 "avoid": 0, "env": defaultdict(int)})
    for d in dates:
        if d[:4] != YEAR:
            continue
        p = f"{C.OUT_DIR}/day/{d}.json"
        try:
            dd = json.load(open(p))
        except FileNotFoundError:
            continue
        s = mstat[d[:7]]
        s["days"] += 1
        picks = dd.get("picks") or []
        if picks:
            s["pdays"] += 1
        s["n"] += len(picks)
        s["bn"] += sum(1 for x in picks if x.get("action") == "BUY_NOW")
        s["am"] += sum(1 for x in picks if x.get("action") == "AMBUSH")
        s["avoid"] += len(dd.get("avoid") or [])
        s["env"][dd.get("regime")] += 1
    for m in sorted(mstat):
        s = mstat[m]
        envs = " ".join(f"{k}:{v}" for k, v in sorted(s["env"].items(), key=lambda x: -x[1]))
        print(f"{m:<9}{s['days']:>6}{s['pdays']:>8}{s['n']:>7}{s['bn']:>6}{s['am']:>6}"
              f"{s['avoid']:>6}{envs:>28}")
    rep["daily"] = {m: {k: (dict(v) if isinstance(v, defaultdict) else v)
                        for k, v in s.items()} for m, s in mstat.items()}

    json.dump(rep, open(f"{C.CACHE_DIR}/diag_2024.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/diag_2024.json")


if __name__ == "__main__":
    main()
