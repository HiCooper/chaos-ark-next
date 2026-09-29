# -*- coding: utf-8 -*-
"""分析师预期修正因子（方向 2）—— 价格之外的第 2 个信息源。

数据
    L0 `analyst_rank`   巨潮个股评级流水（一天一文件，2016→今）
    L0 `sw_component`   申万一级行业成分股（把个股映射到行业）

⚠️ 三个必须披露的口径问题
    1. **前视风险（成分映射）**：`sw_component` 取的是**当前**成分股，回溯 2016 年的
       历史时点存在**幸存者偏差**（后来退出该行业的个股不计入）。用一级行业
       （成分多、单只进出影响小）可缓解，**不能消除**。
    2. **稀疏性**：每天只有 60~390 条评级、覆盖 60~230 只个股（A 股 5000+）。
       所以必须做滚动窗口聚合（20/60 交易日），窗口内样本量是因子有效性的前提，
       脚本会打印逐行业的窗口样本量。
    3. **评级时点**：窗口取 [t-W, t-1]，**不含当日** —— 评级发布时点不确定，
       保守按 T-1 之前可见处理（宁可少用一天，不要引入前视）。

因子（行业层面，日频，都在 31 个申万一级行业上）
    net_up      = (上调 − 下调) / 全部评级数      ← 核心「预期修正」
    up_share    = 上调 / 全部
    buy_share   = 买入类(≥增持) / 全部            ← 绝对乐观度
    cov_int     = 全部 / 成分股数                 ← 关注度（机构覆盖强度）
    first_share = 首次覆盖 / 全部

对照口径
    上一轮的教训是「候选因子有值的子样本上必须重算基线」。本脚本所有 IC 表都配
    同口径列：在因子有值的日子上重算 L1 池的 composite。
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import lab_valuation as LV  # noqa: E402
import rawstore as RS  # noqa: E402
from feature_store import EW_CODE  # noqa: E402

HZS = (20, 80, 100)
WINDOWS = (20, 60)
MIN_CNT = 5          # 窗口内至少多少条评级才算有效
LINE = "=" * 108

RATING_LEVEL = {
    "买入": 5, "强烈推荐": 5, "强推": 5, "强力买入": 5, "推荐": 4,
    "增持": 4, "优于大市": 4, "跑赢行业": 4,
    "中性": 3, "持有": 3, "同步大市": 3, "观望": 3,
    "减持": 2, "弱于大市": 2, "回避": 2, "跑输行业": 2, "卖出": 1,
}
PLACEHOLDER = {"不评级", "无评级", "未评级", "未知", "未公布", "", "None", "nan"}


def lvl(x):
    if x is None:
        return None
    s = str(x).strip()
    if s in PLACEHOLDER:
        return None
    return RATING_LEVEL.get(s)


def _f(d, key="mean", nd=3) -> str:
    if not d or d.get(key) is None:
        return "n/a"
    v = d[key]
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _pct(d, key="spread") -> str:
    v = (d or {}).get(key)
    return f"{v * 100:+.2f}%" if v is not None else "n/a"


def same_mask(mat, fw) -> np.ndarray:
    return np.array([np.sum(~np.isnan(mat[:, i]) & ~np.isnan(fw[:, i])) >= LV.MIN_N
                     for i in range(fw.shape[1])])


def roll_sum(mat: np.ndarray, W: int) -> np.ndarray:
    """每行「过去 W 行（不含当前行）」的求和。mat 形状 (n_d, n_i)。"""
    c = np.cumsum(np.vstack([np.zeros((1, mat.shape[1])), mat]), axis=0)
    out = np.zeros_like(mat)
    for k in range(mat.shape[0]):
        out[k] = c[k] - c[max(0, k - W)]
    return out


def main() -> None:
    LV.MIN_N = 5                     # 一级行业只有 31 个，8 太严
    ctx = PL.load_context()
    fs = ctx["fs"]
    dims = ctx["dims"]
    regime = ctx["regime"]
    l1 = fs.l1_codes()
    n_i, n_d = len(l1), len(fs.dates)
    row_idx = [fs.codes.index(c) for c in l1]
    comp = dims["composite"][row_idx, :]
    li = {c: j for j, c in enumerate(l1)}

    print(LINE)
    print(f"行业池：申万一级 {n_i} 个 × {n_d} 交易日（{fs.dates[0]} → {fs.dates[-1]}）")
    print(LINE)

    # ---------------- 个股 -> 行业映射 ----------------
    code2ind = {}
    n_min = 0
    for c in l1:
        recs = RS.get("sw_component", c)
        if not recs:
            n_min += 1
            continue
        for r in recs:
            code2ind.setdefault(r["code"], c)
    print(f"成分映射：{len(code2ind)} 只个股 -> {n_i - n_min} 个一级行业"
          f"（{n_min} 个行业无成分数据）")
    print("  ⚠️ 当前成分回溯历史 ⇒ 幸存者偏差，见脚本 docstring")

    # ---------------- 摊平评级流水 -> 逐日逐行业计数 ----------------
    didx = {d: k for k, d in enumerate(fs.dates)}
    cnt = np.zeros((n_d, n_i))
    up = np.zeros_like(cnt)
    down = np.zeros_like(cnt)
    first = np.zeros_like(cnt)
    buy = np.zeros_like(cnt)
    n_rec_total = 0
    miss_codes: set = set()
    rating_seen: dict = {}
    change_seen: dict = {}
    yr_all: dict = {}

    for d in fs.dates:
        recs = RS.get("analyst_rank", d)
        if not recs:
            continue
        k = didx[d]
        y = d[:4]
        yr_all[y] = yr_all.get(y, 0) + len(recs)
        n_rec_total += len(recs)
        for r in recs:
            rating_seen[r.get("rating")] = rating_seen.get(r.get("rating"), 0) + 1
            change_seen[r.get("change")] = change_seen.get(r.get("change"), 0) + 1
            c = code2ind.get(r["code"])
            if c is None:
                miss_codes.add(r["code"])
                continue
            j = li[c]
            cnt[k, j] += 1
            lv, pl = lvl(r.get("rating")), lvl(r.get("prev_rating"))
            ch = str(r.get("change") or "")
            # 实测 change 的取值是「维持 / 未知 / **调高 / 调低**」，不是「上调/下调」。
            # 两种写法都接受；change 为「未知/维持」时才退回用 rating 等级比较。
            if ch in ("调高", "上调"):
                up[k, j] += 1
            elif ch in ("调低", "下调"):
                down[k, j] += 1
            elif lv is not None and pl is not None:
                if lv > pl:
                    up[k, j] += 1
                elif lv < pl:
                    down[k, j] += 1
            if str(r.get("is_first") or "") in ("是首次评级", "首次") or ch == "首次":
                first[k, j] += 1
            if lv is not None and lv >= 4:
                buy[k, j] += 1

    print(f"\n评级流水：{n_rec_total} 条记录；未能映射到行业的个股 "
          f"{len(miss_codes)} 只（多为非申万一级成分/退市）")
    print(f"逐年记录数：" + "  ".join(f"{y}:{yr_all[y]}" for y in sorted(yr_all)))
    print("\n[取值分布] rating（top8）：")
    for k_, v_ in sorted(rating_seen.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {str(k_):<10} {v_:>8}")
    print("[取值分布] change（top8）：")
    for k_, v_ in sorted(change_seen.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {str(k_):<10} {v_:>8}")

    # ---------------- 构造因子 ----------------
    close = np.vstack([fs.series(c, "close") for c in l1]).astype(float)
    ew = fs.series(EW_CODE, "close").astype(float)
    fwd = {}
    for H in HZS:
        r = close[:, H:] / close[:, :n_d - H] - 1.0
        ewr = ew[H:] / ew[:n_d - H] - 1.0
        M = np.full((n_i, n_d), np.nan)
        M[:, :n_d - H] = r - ewr[None, :]
        fwd[H] = M

    n_cons = np.array([max(1, len(RS.get("sw_component", c) or [])) for c in l1], dtype=float)

    fac = {}
    for W in WINDOWS:
        rc, ru, rd = roll_sum(cnt, W), roll_sum(up, W), roll_sum(down, W)
        rfi, rb = roll_sum(first, W), roll_sum(buy, W)
        ok = rc >= MIN_CNT
        div = np.where(ok, np.maximum(rc, 1.0), np.nan)
        fac[f"net_up_W{W}"] = ((ru - rd) / div).T
        fac[f"up_share_W{W}"] = (ru / div).T
        fac[f"buy_share_W{W}"] = (rb / div).T
        fac[f"cov_int_W{W}"] = np.where(ok, rc / n_cons[None, :], np.nan).T
        fac[f"first_share_W{W}"] = (rfi / div).T

    # 覆盖诊断
    print()
    print(LINE)
    print("0. 因子覆盖（窗口 20 日）—— 每格是「有值的行业-日」数")
    print("-" * 108)
    yr_of = np.array([d[:4] for d in fs.dates])
    print(f"{'因子':<18}" + "".join(f"{y[2:]:>7}" for y in sorted(set(yr_of))))
    s20 = fac["net_up_W20"]
    for nm in ("net_up_W20", "cov_int_W20"):
        row = ""
        for y in sorted(set(yr_of)):
            mk = yr_of == y
            row += f"{int(np.sum(~np.isnan(fac[nm][:, mk]))):>7}"
        print(f"{nm:<18}{row}")

    # ---------------- IC 表 ----------------
    print()
    print(LINE)
    print("1. 全样本 Rank IC（一级行业池，31 个行业）")
    print("-" * 108)
    print(f"{'因子':<18}{'IC@20':>9}{'IC@80':>9}{'IC@100':>9}{'t@100':>9}"
          f"{'组内IC@100':>12}{'t':>8}{'分组差@100':>12}{'分组差@20':>12}")
    rows = [("composite（基线）", comp)] + sorted(fac.items())
    for nm, mat in rows:
        gi = LV.group_ic(mat, fwd[100], comp)
        sp100, sp20 = LV.group_spread(mat, fwd[100]), LV.group_spread(mat, fwd[20])
        print(f"{nm:<18}{_f(LV.ic_table(mat, fwd[20])):>9}"
              f"{_f(LV.ic_table(mat, fwd[80])):>9}{_f(LV.ic_table(mat, fwd[100])):>9}"
              f"{_f(LV.ic_table(mat, fwd[100]), 't'):>9}{_f(gi):>12}{_f(gi, 't'):>8}"
              f"{_pct(sp100):>12}{_pct(sp20):>12}")

    # ---------------- 同口径 Δ ----------------
    print()
    print(LINE)
    print("2. 同口径 Δ：在「因子有值」的日子上重算 composite 基线（上轮教训）")
    print("-" * 108)
    print(f"{'因子':<18}{'IC@100(因子)':>15}{'IC@100(comp同日)':>19}{'Δ':>11}{'共同日数':>10}")
    for nm, mat in sorted(fac.items()):
        mk = same_mask(mat, fwd[100])
        a = LV.ic_table(mat, fwd[100], mk)
        b = LV.ic_table(comp, fwd[100], mk)
        d = (a["mean"] - b["mean"]) if (a and b) else None
        print(f"{nm:<18}{_f(a):>15}{_f(b):>19}"
              f"{(f'{d:+.4f}' if d is not None else 'n/a'):>11}{int(mk.sum()):>10}")

    # ---------------- 逐年 ----------------
    print()
    print(LINE)
    print("3. 逐年组内 IC（控 composite，@100）—— 防单年耀眼")
    print("-" * 108)
    yrs = [y for y in sorted(set(yr_of)) if y >= "2016"]
    print(f"{'因子':<18}" + "".join(f"{y[2:]:>7}" for y in yrs) + f"{'正/负':>8}")
    for nm in ("net_up_W20", "net_up_W60", "buy_share_W20", "cov_int_W20"):
        mat = fac[nm]
        vals = []
        for y in yrs:
            mk = yr_of == y
            g = LV.group_ic(mat, fwd[100], comp, mask_i=mk)
            vals.append(None if not g else g["mean"])
        cells = "".join(f"{(v if v is not None else np.nan):>7.3f}" for v in vals)
        npos = sum(1 for v in vals if v is not None and v > 0)
        nneg = sum(1 for v in vals if v is not None and v <= 0)
        print(f"{nm:<18}{cells}{f'{npos}/{nneg}':>8}")

    # ---------------- 分环境 ----------------
    print()
    print(LINE)
    print("4. 分环境组内 IC（@100，控 composite）")
    print("-" * 108)
    envs = ["ATTACK", "NEUTRAL", "DEFEND", "PANIC"]
    print(f"{'因子':<18}" + "".join(f"{e:>14}" for e in envs))
    for nm in ("net_up_W20", "net_up_W60", "buy_share_W20"):
        cells = ""
        for e in envs:
            mk = np.array([(regime.get(d) or {}).get("regime") == e for d in fs.dates])
            n = int(mk[C.WARMUP:].sum())
            cells += f"{(f'{_f(LV.group_ic(fac[nm], fwd[100], comp, mask_i=mk))}({n})'):>14}"
        print(f"{nm:<18}{cells}")

    print()
    print(LINE)
    print("完成。⚠️ 成分映射是当前值 ⇒ 有幸存者偏差；结论须带此披露。")


if __name__ == "__main__":
    main()
