# -*- coding: utf-8 -*-
"""ARK 主管线：把 8 步工作流串成一次可重复执行的过程。

    数据快照 -> 特征库 -> 环境识别 -> 六维打分 -> 共振分类 -> 周期估计 -> 结论 -> 复盘

用法：
    python pipeline.py --backfill 400     # 重放最近 400 个交易日，产出历史结论库
    python pipeline.py --as-of 2026-09-21 # 只跑一天
    python pipeline.py --all              # backfill + today + review 全量刷新
    python pipeline.py --all --tail 12    # 增量：只重算尾部 12 天，历史日复用 day/<date>.json

不变式：同一份 panel + 同一份 config => 同一份结论（逐字可重放）。

增量重放的前提（已逐一验证，改动对应模块时必须复查）：
    indicators / feature_store / regime / scoring / horizon 全部只向后看
    （rolling / pct_change / 递推 EMA / 滞回 / state[:i-max(H)-1] 历史类比），
    因此「追加新交易日」不会改变历史上任何一天的结论。
    运行时用 canary 自检兜底：尾部重算日若与上一次落盘结论不一致，自动回退全量。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import datasource as DS  # noqa: E402
from feature_store import FeatureStore, build as build_fs  # noqa: E402
import regime as RG  # noqa: E402
import scoring as SC  # noqa: E402
import horizon as HZ  # noqa: E402
import classify as CL  # noqa: E402
import review as RV  # noqa: E402
import settlement as ST  # noqa: E402
import rules as RULES  # noqa: E402

# 版本号 = 口径版本。改动任何影响结论的模块（信号/结算/数据源/面板口径）都必须 bump，
# 它同时是 day/<date>.json 的有效性标记（不匹配即失效 → 全量重放）。
# 演进：ark-1.2.0-ranking → ark-1.3.0-stage（阶段标注，展示层）
#       → ark-1.4.0-mature-weight（MATURE 降权落参，信号层口径变更）
ENGINE_VERSION = "ark-1.4.0-mature-weight"


def config_hash() -> str:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]


FP_PATH = lambda: f"{C.CACHE_DIR}/features.fp.json"  # noqa: E731


def panel_fingerprint(panel: dict) -> str:
    """面板指纹：日期轴长度 + 尾部 5 日 + 标的集合 + 各标的最新一根 bar。

    用途：检测 features.npz 是否由「另一份 panel」算出（典型场景：panel 已追加了
    新交易日，但忘了 --refresh，特征里根本没有新的一天 → 静默产出与昨天相同的结论）。
    只取尾部而不取全量，是为了让指纹对「历史段不变 + 追加新日」敏感、对无关重排不敏感。
    """
    dates = panel["dates"]
    tail: Dict[str, list] = {}
    for c, s in panel["series"].items():
        r = s.get(dates[-1]) or {}
        tail[c] = [r.get("close"), r.get("amount")]
    payload = {"n": len(dates), "last5": dates[-5:],
               "codes": sorted(panel["series"].keys()), "tail": tail}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def load_etf_book() -> Optional[ST.EtfBook]:
    """加载 ETF 账簿（真实结算用）。缺失时返回 None，收益退化为信号口径。

    ARK_QFQ=1 时先做份额折算复权（engine/qfq.py）：面板是不复权价，ETF 发生
    份额折算（拆分/合并）时价格按整数倍跳变而持有人价值不变，不复权直接算收益
    会记成 ±50%~+220% 的虚假盈亏。生产默认为关（未确认前不改基线口径）。
    """
    path = f"{C.CACHE_DIR}/etf_panel.json"
    if not os.path.exists(path):
        print("      [warn] 未找到 etf_panel.json，收益将只按行业指数口径计算")
        return None
    panel = ST.load_etf_panel(path)
    if os.environ.get("ARK_QFQ") == "1":
        import qfq
        sw_panel = DS.load_panel() if os.path.exists(f"{C.CACHE_DIR}/panel.json") else None
        panel, rep = qfq.apply_qfq(panel, sw_panel, verbose=False)
        n = sum(len(v) for v in rep.values())
        print(f"      [qfq] 份额折算复权已启用：修正 {n} 处断点 / {len(rep)} 只 ETF")
    return ST.EtfBook(panel)


def load_context(refresh: bool = False, last_n: int = 3400) -> dict:
    """加载（必要时重建）特征库 / 环境序列 / 打分矩阵 / 前瞻收益。

    两个守卫：
      1. 陈旧守卫：cache 里有 features.npz 但它是由「另一份 panel」算出的
         （面板指纹不符）→ 视为陈旧，自动重建并大声说明。
      2. 窗口锚定：特征窗口起点一旦确定就冻结在 features.fp.json 里，
         不随追加新交易日漂移 —— 这是「历史结论逐字节稳定」的前提。
    """
    fs_path = f"{C.CACHE_DIR}/features.npz"
    fp_info = None
    if os.path.exists(FP_PATH()):
        try:
            fp_info = json.load(open(FP_PATH()))
        except Exception:
            fp_info = None
    anchor = (fp_info or {}).get("window_start")

    fp = None
    if not refresh and os.path.exists(fs_path):
        panel_now = DS.load_panel()
        fp = panel_fingerprint(panel_now)
        old_fp = (fp_info or {}).get("panel_fp")
        if old_fp and old_fp != fp:
            print(f"[guard] features.npz 与当前 panel 不一致 "
                  f"(特征指纹 {old_fp} != 面板指纹 {fp}) → 自动重建特征层")
            refresh = True
    if refresh or not os.path.exists(fs_path):
        panel = DS.load_panel()
        # 窗口起点：优先沿用上次的锚点；首次构建时取尾部 last_n 天的起点并冻结
        if not anchor:
            anchor = panel["dates"][-last_n] if len(panel["dates"]) > last_n \
                else panel["dates"][0]
        print(f"      特征窗口锚定起点: {anchor}")
        fs = build_fs(panel, start=anchor)
        fs.save(fs_path)
        fp = panel_fingerprint(panel)
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        json.dump({"panel_fp": fp, "window_start": anchor,
                   "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "engine_version": ENGINE_VERSION, "config_hash": config_hash()},
                  open(FP_PATH(), "w"))
    else:
        fs = FeatureStore.load(fs_path)

    regime_path = f"{C.CACHE_DIR}/regime.json"
    if refresh or not os.path.exists(regime_path):
        rs = RG.build_regime_series(fs)
        json.dump(rs, open(regime_path, "w"), ensure_ascii=False)
    else:
        rs = json.load(open(regime_path, "r", encoding="utf-8"))

    dims, comp = SC.build_score_matrix(fs, rs)
    stages = SC.compute_stage(fs)
    fwd = HZ.precompute_forward(fs)
    state = HZ.build_state_matrix(fs, dims, rs)
    return {"fs": fs, "regime": rs, "dims": dims, "comp": comp, "fwd": fwd, "state": state,
            "stages": stages}


def run_day(ctx: dict, date: str, book: Optional[ST.EtfBook] = None) -> dict:
    day = CL.classify_day(ctx["fs"], ctx["dims"], ctx["comp"], ctx["regime"],
                          ctx["state"], ctx["fwd"], date, stages=ctx.get("stages"))
    if not day.get("error"):
        day["summary"] = CL.summarize(day)
        # 给每条结论绑定「能真正买到的标的」，避免结论落在一个不可交易的指数上
        if book is not None:
            for p in day.get("picks", []):
                sid = book.etf_of(p["name"], date)
                p["tradable"] = sid is not None
                p["etf"] = book.listing.get(sid, {}).get("name") if sid else None
                p["etf_code"] = sid.split(".")[-1] if sid else None
                proxy = None if sid is None else (
                    p["name"] if p["name"] in book.sw_to_etf else None)
                p["etf_proxy"] = bool(sid and proxy is None and p["name"] not in book.sw_to_etf)
            for p in day.get("watch", []):
                sid = book.etf_of(p["name"], date)
                p["tradable"] = sid is not None
                p["etf"] = book.listing.get(sid, {}).get("name") if sid else None
            # 全景表行也绑定可交易标的（仅标注，不参与结论）
            for r in day.get("ranking", []):
                sid = book.etf_of(r["name"], date)
                if sid:
                    r["etf"] = book.listing.get(sid, {}).get("name")
                    r["etf_code"] = sid.split(".")[-1]
    day["engine_version"] = ENGINE_VERSION
    day["config_hash"] = config_hash()
    return day


def run_range(ctx: dict, dates: List[str], verbose: bool = True,
              book: Optional[ST.EtfBook] = None) -> Dict[str, dict]:
    out = {}
    t0 = time.time()
    for i, d in enumerate(dates):
        out[d] = run_day(ctx, d, book=book)
        if verbose and (i + 1) % 50 == 0:
            print(f"  重放进度 {i+1}/{len(dates)}  ({time.time()-t0:.0f}s)")
    return out


def _load_cached_conclusion(date: str) -> Optional[dict]:
    """读回 day/<date>.json 里的结论（剥掉 action_today 等附加层）。

    day_payload = conclusions + action_today，且 run_day 已写入 engine_version /
    config_hash，天然是一份带版本标记的结论缓存。仅当版本与口径都一致时才可信。
    """
    p = f"{C.OUT_DIR}/day/{date}.json"
    if not os.path.exists(p):
        return None
    try:
        day = json.load(open(p))
    except Exception:
        return None
    if day.get("engine_version") != ENGINE_VERSION or day.get("config_hash") != config_hash():
        return None
    day.pop("action_today", None)
    day.pop("_offline", None)
    return day if day.get("date") == date else None


def run_range_incremental(ctx: dict, dates: List[str], tail_n: int,
                          verbose: bool = True,
                          book: Optional[ST.EtfBook] = None
                          ) -> Tuple[Dict[str, dict], Dict[str, dict], int]:
    """增量重放：只重算尾部 tail_n 天，其余日期复用已落盘的结论。

    返回 (conclusions, cached_exits, n_reused)：
      conclusions  全部日期的结论（复用 + 新算）
      cached_exits 命中缓存的日期 -> 该日已落盘的 action_today（免重算 open_positions）
      n_reused     复用的天数

    canary 自检：尾部重算日若上一次也有落盘结论，则逐字段比对；任何不一致都说明
    「追加新交易日不改变历史结论」这个前提被打破（改了某个模块的前视口径、或 L0
    被回填修订），此时放弃缓存、整体回退全量重放。
    """
    day_dir = f"{C.OUT_DIR}/day"
    tail = set(dates[-tail_n:]) if tail_n > 0 else set()
    out: Dict[str, dict] = {}
    cached_exits: Dict[str, dict] = {}
    n_reused = 0
    t0 = time.time()
    for i, d in enumerate(dates):
        cached = None
        at = None
        if d not in tail:
            p = f"{day_dir}/{d}.json"
            if os.path.exists(p):
                try:
                    raw = json.load(open(p))
                except Exception:
                    raw = None
                if (raw and raw.get("date") == d
                        and raw.get("engine_version") == ENGINE_VERSION
                        and raw.get("config_hash") == config_hash()):
                    at = raw.get("action_today")
                    raw.pop("action_today", None)
                    raw.pop("_offline", None)
                    cached = raw
        if cached is not None:
            out[d] = cached
            if at:
                cached_exits[d] = at
            n_reused += 1
            continue
        out[d] = run_day(ctx, d, book=book)
        if verbose and (i + 1) % 20 == 0:
            print(f"  增量重放进度 {i+1}/{len(dates)}  ({time.time()-t0:.0f}s)")

    # ---- canary：尾部重算日 vs 上一次落盘结论
    bad = []
    for d in sorted(tail):
        cached = _load_cached_conclusion(d)
        if cached is None:
            continue
        if json.dumps(cached, sort_keys=True) != json.dumps(out[d], sort_keys=True):
            bad.append(d)
    if bad:
        print(f"[canary] {len(bad)} 个尾部重算日与历史落盘结论不一致（如 {bad[:3]}）")
        print("         「历史结论不随新交易日改变」的前提被打破 → 回退全量重放")
        out = run_range(ctx, dates, verbose=verbose, book=book)
        cached_exits = {}
        n_reused = 0
    return out, cached_exits, n_reused


def build_action_today(book: Optional[ST.EtfBook], conclusions: Dict[str, dict],
                       dates: List[str]) -> Optional[dict]:
    """「今日出手」——重建截至最新交易日仍在场的持仓，挑出命中交易规则要出手的标的。

    与结算曲线同源：建仓规则（T+1 开盘买、容量上限、按综合分取前 N）完全复用
    settlement.open_positions，因此这里列出的持仓就是那条资金曲线上真实存在的仓位。
    差别只在于允许「持有期尚未走完」的结论进入——今天该卖什么，看的是当下持仓。
    """
    if book is None:
        return None
    signals: List[dict] = []
    for d in dates:
        day = conclusions.get(d) or {}
        if day.get("error"):
            continue
        for p in day.get("picks", []):
            hz = (p.get("horizon") or {}).get("horizon")
            if not hz:
                continue
            signals.append({
                "date": d, "sw_name": p["name"], "horizon": int(hz),
                "composite": p.get("composite"), "action": p.get("action"),
            })
    if not signals:
        return None
    # soon_days=10：建议周期是 20~100 个交易日量级，5 个交易日的预览窗几乎总是空的，
    # 反而让「即将出手」这一节看起来像坏了。10 个交易日≈两周，与持仓节奏相称。
    return ST.open_positions(book, signals, dates[-1],
                             max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                             soon_days=10)


def build_hist_exits(book: Optional[ST.EtfBook], conclusions: Dict[str, dict],
                     dates: List[str], cached_exits: Optional[Dict[str, dict]] = None
                     ) -> Dict[str, dict]:
    """为每个交易日重建「当日出手判断」，供历史日历详情展示。

    与 build_action_today 同源：复用 settlement.open_positions，逐日把 as_of 指到
    那个交易日，得到截至当日仍在场的持仓里「命中交易规则需要动手」的标的。
    返回结构与 action_today **完全同构**（as_of/n_open/n_sell_today/n_warn/
    open/sell_today/sell_soon/warn/...），前端历史日详情直接复用今日页的
    出手判断组件，无需为历史日另写一套渲染。

    cached_exits：增量模式下，命中结论缓存的日期直接复用已落盘的 action_today
    （open_positions 单次 0.04s × 2598 天 ≈ 113s，是增量路径最大的一块固定开销）。
    复杂度 O(n²)，但 open_positions 每次只做轻量 dict 重放，2605 天量级可接受。
    """
    if book is None:
        return {}
    cached_exits = cached_exits or {}
    # 建一次 signal 列表（复用 build_action_today 的字段口径），逐日只换 as_of
    signals: List[dict] = []
    for d in dates:
        day = conclusions.get(d) or {}
        if day.get("error"):
            continue
        for p in day.get("picks", []):
            hz = (p.get("horizon") or {}).get("horizon")
            if not hz:
                continue
            signals.append({
                "date": d, "sw_name": p["name"], "horizon": int(hz),
                "composite": p.get("composite"), "action": p.get("action"),
            })
    if not signals:
        return {}

    out: Dict[str, dict] = {}
    for d in dates:
        if d in cached_exits and cached_exits[d].get("as_of") == d:
            out[d] = cached_exits[d]
            continue
        at = ST.open_positions(book, signals, d,
                               max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                               soon_days=10)
        # open_positions 在 as_of 早于首笔建仓日时返回空回执（n_open=0），
        # 此时仍保留完整结构，前端据此显示「空仓等待」而非「数据缺失」。
        out[d] = at if at else {
            "as_of": d, "n_open": 0, "n_sell_today": 0, "n_warn": 0,
            "open": [], "sell_today": [], "sell_soon": [], "warn": [],
        }
    return out


def slim(day: dict, exits: Optional[dict] = None) -> dict:
    """历史结论索引精简版（供日历热力图 + 复盘「每日结论带」）。

    exits = 该日的出手判断（build_hist_exits 的产物），只取 n_sell_today
    一个整数进索引，供日历格渲染「当日有出手」标记。
    """
    if day.get("error"):
        return {"date": day["date"], "error": day["error"], "picks": []}
    return {
        "date": day["date"],
        "regime": day["regime"],
        "env_score": round(day["regime_info"]["env_score"], 1),
        "summary": day.get("summary", ""),
        "n_sell": int((exits or {}).get("n_sell_today") or 0),
        "picks": [{
            "code": p["code"], "name": p["name"], "level": p["level"],
            "action": p["action"], "composite": p["composite"],
            "horizon": p["horizon"]["horizon"],
            "low_confidence": p["horizon"]["low_confidence"],
            "win_rate": p["horizon"].get("win_rate"),
            "mean_excess": p["horizon"].get("mean_excess"),
            "tradable": p.get("tradable"),
            "etf": p.get("etf"), "etf_code": p.get("etf_code"),
            "dims": p["dims"],
        } for p in day["picks"]],
    }


def day_payload(day: dict, exits: Optional[dict]) -> dict:
    """单个交易日的完整研判载荷（day/<date>.json）。

    与 history.json 的精简索引不同，这里保留 run_day 的完整结论（picks 全字段、
    regime_info、avoid / watch_top 等），再附加该日的「出手判断」action_today ——
    历史日详情与今日页共用同一套卡片渲染（字段名与 today.json 完全一致），
    点开任意历史日看到的信息和今日一致。
    """
    if day.get("error"):
        return {"date": day["date"], "error": day["error"],
                "picks": [], "action_today": None}
    payload = dict(day)  # 浅拷贝，避免污染 conclusions 里的原始 dict
    payload["action_today"] = exits
    return payload


def main():
    args = sys.argv[1:]
    refresh = "--refresh" in args
    do_all = "--all" in args
    # `--all` 的语义是「全量刷新」（见模块 docstring）。若不显式给 --backfill，
    # 必须走全量区间；否则会静默按默认 400 日截断，把全量产物覆盖成短窗口结果，
    # 而两次运行的输出文件同名，事后很难察觉。这里把默认值对齐文档。
    backfill = 400
    if "--backfill" in args:
        backfill = int(args[args.index("--backfill") + 1])
    elif do_all:
        backfill = 0
    as_of = args[args.index("--as-of") + 1] if "--as-of" in args else None
    # --tail N：增量模式。只重算尾部 N 天（含 canary 重叠自检），历史日复用
    # day/<date>.json 里的结论与 action_today。N=0 表示「只算最新一天」。
    tail_n = None
    if "--tail" in args:
        tail_n = max(0, int(args[args.index("--tail") + 1]))

    os.makedirs(C.OUT_DIR, exist_ok=True)
    print("[1/6] 载入上下文 ...")
    ctx = load_context(refresh=refresh)
    fs = ctx["fs"]
    print(f"      特征库：{len(fs.codes)} 标的 x {len(fs.dates)} 天 "
          f"({fs.dates[0]} -> {fs.dates[-1]})")

    book = load_etf_book()
    if book is not None:
        print(f"      ETF 结算账簿：{len(book.sw_to_etf)} 个行业可交易标的，"
              f"基准 {book.listing.get(book.bench_etf, {}).get('name')}")

    if as_of:
        day = run_day(ctx, as_of, book=book)
        print(json.dumps(day, ensure_ascii=False, indent=2)[:6000])
        return

    # 回测窗口：跨越牛熊，起点由 BACKTEST_START 固定（不用 backfill 截断历史）
    if "--full" in args or backfill <= 0:
        start_i = C.WARMUP
    else:
        start_i = max(C.WARMUP, len(fs.dates) - backfill)
    if C.BACKTEST_START:
        bi = next((i for i, d in enumerate(fs.dates) if d >= C.BACKTEST_START), None)
        if bi is not None:
            start_i = max(start_i, bi)
    dates = fs.dates[start_i:]
    if tail_n is not None:
        print(f"[2/6] 增量重放：尾部 {tail_n} 天重算，"
              f"其余 {max(0, len(dates) - tail_n)} 天复用缓存 "
              f"({dates[0]} -> {dates[-1]}) ...")
        conclusions, cached_exits, n_reused = run_range_incremental(
            ctx, dates, tail_n, book=book)
    else:
        print(f"[2/6] 重放 {len(dates)} 个交易日 ({dates[0]} -> {dates[-1]}) ...")
        conclusions = run_range(ctx, dates, book=book)
        cached_exits, n_reused = {}, 0

    print("[3/6] 逐日重建出手判断 ...")
    exits_by_date = build_hist_exits(book, conclusions, dates,
                                     cached_exits=cached_exits)
    n_exit_days = sum(1 for e in exits_by_date.values() if e["n_open"] > 0)

    print("      落盘历史索引 + 逐日完整研判 ...")
    history = [slim(conclusions[d], exits_by_date.get(d)) for d in dates]
    json.dump({
        "engine_version": ENGINE_VERSION, "config_hash": config_hash(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "universe": {"L1": len(fs.l1_codes()), "total": len(fs.codes)},
        "days": history,
    }, open(f"{C.OUT_DIR}/history.json", "w"), ensure_ascii=False)

    day_dir = f"{C.OUT_DIR}/day"
    os.makedirs(day_dir, exist_ok=True)
    # 增量模式只重写重算过的日期：缓存命中的日期其结论与出手判断都与盘上
    # 逐字节相同，重写是纯 IO 浪费（2598 个文件 ≈ 数十秒）。
    write_dates = dates[-tail_n:] if tail_n is not None else dates
    for d in write_dates:
        json.dump(day_payload(conclusions[d], exits_by_date.get(d)),
                  open(f"{day_dir}/{d}.json", "w"), ensure_ascii=False)
    print(f"      day/<date>.json：重写 {len(write_dates)} / 共 {len(dates)}"
          f"（复用 {n_reused} 天缓存，其中 {n_exit_days} 日有在场持仓）")

    print("[4/6] 落盘今日结论 ...")
    today = conclusions[dates[-1]]
    at = build_action_today(book, conclusions, dates)
    if at:
        today["action_today"] = at
        print(f"      今日出手：需卖出 {at['n_sell_today']} 笔，"
              f"在场持仓 {at['n_open']} 笔（平均持有 {at['avg_held']} 个交易日），"
              f"失效预警 {at['n_warn']} 笔")
    json.dump(today, open(f"{C.OUT_DIR}/today.json", "w"), ensure_ascii=False)
    print("      " + today.get("summary", ""))

    print("[5/6] 运行复盘引擎 ...")
    rep = RV.build_review(fs, conclusions, dates, ctx["fwd"], ctx["regime"], book=book)
    # 生效规则快照：数值全部从 config.py 直读，供「引擎」页展示，
    # 避免前端硬编码导致配置改了页面不跟着变。
    rep["rules"] = RULES.build_rules(settlement=rep.get("settlement"), today=today)
    rep["rules"]["engine_version"] = ENGINE_VERSION
    rep["rules"]["config_hash"] = config_hash()
    json.dump(rep, open(f"{C.OUT_DIR}/review.json", "w"), ensure_ascii=False)
    print(f"      已评估 {rep['overview']['evaluated']} 条结论，"
          f"信号口径胜率 {rep['overview']['win_rate']:.1%}")
    if rep.get("settlement"):
        s = rep["settlement"]
        hp = (s.get("holding") or {}).get("portfolio") or {}
        print(f"      ETF 结算：{s['n_settled']}/{s['n_total']} 条可交易，"
              f"胜率(跑赢基准) {s['win_rate_excess']:.1%}，"
              f"年化 {s['stats'].get('cagr', 0):.2%}，"
              f"最大回撤 {s['stats'].get('maxdd', 0):.2%}，"
              f"平均持仓 {hp.get('avg_days')} 个交易日")

    print("[6/6] 完成")
    for f in ("history.json", "today.json", "review.json"):
        p = f"{C.OUT_DIR}/{f}"
        print(f"      {p}  {os.path.getsize(p)/1024:.0f} KB")
    print(f"      {day_dir}/  {len(dates)} 个逐日文件（完整研判 + 出手判断）")


if __name__ == "__main__":
    main()
