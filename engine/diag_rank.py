# -*- coding: utf-8 -*-
"""排名信息量的反事实检验（只读，向量化）：闸门说「不」的那些天，排名里有没有钱？

对每个交易日取 day/<date>.json 里 composite 排名 Top-K 的行业，按生产结算口径
（T+1 开盘买、持有 H 个交易日收盘卖、双边成本 0.14%，用真实 ETF）算持有收益与
相对基准超额，按年聚合；另取「每日随机 20 个行业」作为市场基准线。

读法：
  · Top-1 明显优于随机线 → 排序有正向信息量；
  · 必须看**逐年**一致性（方向跨年稳定）与 Top1/Top3/Top8/Top12 的**单调性**
    （非单调 = 噪声特征），单年耀眼增益默认视为嫌疑。

回答 2024 的具体问题：闸门全年静默，但排名靠前的行业若买入是赚还是亏。

运行：python3 engine/diag_rank.py
"""
import glob
import json
import random
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402

H = 60
TOPS = [1, 2, 3, 5, 8, 12, 20]
RAND_N = 20
COST = C.ROUND_TRIP_COST
random.seed(20240922)


def arrays(book: ST.EtfBook):
    n = len(book.dates)
    op, cl = {}, {}
    for sid, s in book.series.items():
        o = np.full(n, np.nan)
        c = np.full(n, np.nan)
        for i, d in enumerate(book.dates):
            r = s.get(d)
            if not r:
                continue
            v = r.get("open") or r.get("close")
            if v:
                o[i] = v
            v = r.get("close")
            if v:
                c[i] = v
        op[sid], cl[sid] = o, c
    return op, cl


def main() -> None:
    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    # 只用「全池收益中位数」作参照做折算复权，不加载 panel.json（70MB JSON →
    # 数 GB 常驻内存，与并发实验台同跑会 OOM 被 kill）。中位数参照足以识别
    # 2024 年的三处折算（煤炭 -50.3%/房地产 +170.1%/新能源 +221.3%）。
    adj, _ = qfq.apply_qfq(ep, None, verbose=False)
    book = ST.EtfBook(adj)
    op, cl = arrays(book)
    n = len(book.dates)
    bidx = book.idx
    bench = book.bench_etf

    agg = defaultdict(lambda: {"ret": [], "ex": []})
    rnd = defaultdict(lambda: {"ret": [], "ex": []})

    files = sorted(glob.glob(f"{C.OUT_DIR}/day/*.json"))
    used = 0
    for p in files:
        d0 = json.load(open(p))
        date = d0.get("date")
        rank = d0.get("ranking") or []
        if not date or not rank:
            continue
        i0 = bidx.get(date)
        if i0 is None:
            continue
        b_i, s_i = i0 + 1, i0 + 1 + H
        if s_i >= n:
            continue
        y = date[:4]
        order = sorted(rank, key=lambda x: -(x.get("composite") or 0))
        bd = book.dates[b_i]

        def ret_of(name):
            sid = book.etf_of(name, bd)
            if not sid or sid not in op:
                return None
            # 买入日开盘 -> 卖出日收盘；缺 bar 时退化为收盘
            bp = op[sid][b_i]
            if not (bp and bp > 0):
                bp = cl[sid][b_i]
            sp = cl[sid][s_i]
            if not sp:
                k = s_i
                while k > b_i and not cl[sid][k]:
                    k -= 1
                sp = cl[sid][k]
            if not (bp and bp > 0 and sp):
                return None
            net = sp / bp - 1.0 - COST
            ex = None
            if bench:
                bb = op[bench][b_i] or cl[bench][b_i]
                bs = cl[bench][s_i]
                if bb and bb > 0 and bs:
                    ex = net - (bs / bb - 1.0 - COST)
            return net, ex

        for K in TOPS:
            for x in order[:K]:
                r = ret_of(x["name"])
                if r:
                    agg[(y, K)]["ret"].append(r[0])
                    if r[1] is not None:
                        agg[(y, K)]["ex"].append(r[1])
        for x in random.sample(order, min(RAND_N, len(order))):
            r = ret_of(x["name"])
            if r:
                rnd[y]["ret"].append(r[0])
                if r[1] is not None:
                    rnd[y]["ex"].append(r[1])
        used += 1
    print(f"扫描 {used} 个交易日（H={H}，随机线每日 {RAND_N} 个行业）\n")

    years = sorted({k[0] for k in agg})
    print("=== 逐年：Top-K 平均超额（对照：随机线） ===")
    print(f"{'年':<7}{'随机线':>9}" + "".join(f"{'Top'+str(k):>9}" for k in TOPS)
          + f"{'样本/年':>9}")
    for y in years:
        line = f"{y:<7}{np.mean(rnd[y]['ex'])*100:>8.2f}%"
        for K in TOPS:
            line += f"{np.mean(agg[(y, K)]['ex'])*100:>8.2f}%"
        line += f"{len(agg[(y, 1)]['ret']):>9}"
        print(line)

    print("\n=== 全区间汇总 ===")
    print(f"{'':<7}{'平均超额':>10}{'平均收益':>10}{'胜率':>8}{'超额胜率':>10}{'样本':>9}")
    for K in TOPS:
        rs = np.concatenate([agg[(y, K)]["ret"] for y in years]).astype(float)
        es = np.concatenate([agg[(y, K)]["ex"] for y in years]).astype(float)
        print(f"{'Top'+str(K):<7}{es.mean()*100:>9.2f}%{rs.mean()*100:>9.2f}%"
              f"{(rs>0).mean()*100:>7.0f}%{(es>0).mean()*100:>9.0f}%{rs.size:>9}")
    rs = np.concatenate([rnd[y]["ret"] for y in years]).astype(float)
    es = np.concatenate([rnd[y]["ex"] for y in years]).astype(float)
    print(f"{'随机':<7}{es.mean()*100:>9.2f}%{rs.mean()*100:>9.2f}%"
          f"{(rs>0).mean()*100:>7.0f}%{(es>0).mean()*100:>9.0f}%{rs.size:>9}")

    print("\n=== 2024 专项：Top-K 平均收益/超额/胜率 ===")
    for K in TOPS:
        rs = np.array(agg[("2024", K)]["ret"], dtype=float)
        es = np.array(agg[("2024", K)]["ex"], dtype=float)
        if rs.size:
            print(f"  Top{K:<3} 样本 {rs.size:>4}  净收益 {rs.mean()*100:+6.2f}%  "
                  f"胜率 {(rs>0).mean()*100:>3.0f}%  超额 {es.mean()*100:+6.2f}%  "
                  f"超额胜率 {(es>0).mean()*100:>3.0f}%")
    rs = np.array(rnd["2024"]["ret"], dtype=float)
    es = np.array(rnd["2024"]["ex"], dtype=float)
    print(f"  随机  样本 {rs.size:>4}  净收益 {rs.mean()*100:+6.2f}%  "
          f"胜率 {(rs>0).mean()*100:>3.0f}%  超额 {es.mean()*100:+6.2f}%  "
          f"超额胜率 {(es>0).mean()*100:>3.0f}%")

    out = {"H": H,
           "by_year": {f"{y}|Top{K}": {"n": len(agg[(y, K)]["ret"]),
                                       "ret": float(np.mean(agg[(y, K)]["ret"])),
                                       "ex": float(np.mean(agg[(y, K)]["ex"]))}
                       for y in years for K in TOPS},
           "random": {y: {"n": len(rnd[y]["ret"]),
                          "ret": float(np.mean(rnd[y]["ret"])),
                          "ex": float(np.mean(rnd[y]["ex"]))} for y in years}}
    json.dump(out, open(f"{C.CACHE_DIR}/diag_rank.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/diag_rank.json")


if __name__ == "__main__":
    main()
