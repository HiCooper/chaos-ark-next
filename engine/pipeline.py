# -*- coding: utf-8 -*-
"""ARK 主管线：把 8 步工作流串成一次可重复执行的过程。

    数据快照 -> 特征库 -> 环境识别 -> 六维打分 -> 共振分类 -> 周期估计 -> 结论 -> 复盘

用法：
    python pipeline.py --backfill 400     # 重放最近 400 个交易日，产出历史结论库
    python pipeline.py --as-of 2026-09-21 # 只跑一天
    python pipeline.py --all              # backfill + today + review 全量刷新

不变式：同一份 panel + 同一份 config => 同一份结论（逐字可重放）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Dict, List, Optional

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

ENGINE_VERSION = "ark-1.3.0-stage"


def config_hash() -> str:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]


def load_etf_book() -> Optional[ST.EtfBook]:
    """加载 ETF 账簿（真实结算用）。缺失时返回 None，收益退化为信号口径。"""
    path = f"{C.CACHE_DIR}/etf_panel.json"
    if not os.path.exists(path):
        print("      [warn] 未找到 etf_panel.json，收益将只按行业指数口径计算")
        return None
    return ST.EtfBook(ST.load_etf_panel(path))


def load_context(refresh: bool = False, last_n: int = 3400) -> dict:
    """加载（必要时重建）特征库 / 环境序列 / 打分矩阵 / 前瞻收益。"""
    fs_path = f"{C.CACHE_DIR}/features.npz"
    if refresh or not os.path.exists(fs_path):
        panel = DS.load_panel()
        fs = build_fs(panel, last_n=last_n)
        fs.save(fs_path)
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
                     dates: List[str]) -> Dict[str, dict]:
    """为每个交易日重建「当日出手判断」，供历史日历详情展示。

    与 build_action_today 同源：复用 settlement.open_positions，逐日把 as_of 指到
    那个交易日，得到截至当日仍在场的持仓里「命中交易规则需要动手」的标的。
    返回结构与 action_today **完全同构**（as_of/n_open/n_sell_today/n_warn/
    open/sell_today/sell_soon/warn/...），前端历史日详情直接复用今日页的
    出手判断组件，无需为历史日另写一套渲染。

    逐日文件独立落盘（day/<date>.json），体积不进入首屏 history.json。
    复杂度 O(n²)，但 open_positions 每次只做轻量 dict 重放，2605 天量级可接受
    （全量管线本身已需重放 2605 天，此步骤为纯增量）。
    """
    if book is None:
        return {}
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
    print(f"[2/6] 重放 {len(dates)} 个交易日 ({dates[0]} -> {dates[-1]}) ...")
    conclusions = run_range(ctx, dates, book=book)

    print("[3/6] 逐日重建出手判断 ...")
    exits_by_date = build_hist_exits(book, conclusions, dates)
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
    for d in dates:
        json.dump(day_payload(conclusions[d], exits_by_date.get(d)),
                  open(f"{day_dir}/{d}.json", "w"), ensure_ascii=False)
    print(f"      day/<date>.json × {len(dates)}（其中 {n_exit_days} 日有在场持仓）")

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
