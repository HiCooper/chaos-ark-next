# -*- coding: utf-8 -*-
"""抄底兜底通道（DIP）的候选生成器。

**它不产生结论**——只产生「可执行候选」，塞进结算层的候选池，与主策略共享
K=24 槽位与现金。方向上它回答的是另一个问题：主策略在恐慌期会连续零结论、
资金闲置（近期实测均投出 51%、连续 11 个交易日 0 结论），这时候要不要用
「超跌 + 止跌回升」的板块补位。

规则（与 calibration-log.md 的统计口径一致）：
    T 日  ① 20 日跌幅 ≤ -15%
          ② 较近 5 日最低点回升 > 3%（"跌不动了"这个二阶导条件，是胜率与尾部
             同时改善的关键：72.8% vs 63.5%，最差 -21% vs -35%）
          →  T+1 开盘买对应行业 ETF，持有 20 个交易日，到期收盘卖

默认只在**主策略当日零候选**的空窗日触发（config.DIP["only_blank_days"]）：
实测这样能让单边牛市里的伤害减半，同时保住熊市的收益。

默认关闭（config.DIP["enabled"]，可用 ARK_DIP=1 临时打开做对照）。
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Set

import numpy as np

import config as C


def _load_panel() -> dict:
    path = f"{C.CACHE_DIR}/panel.json"
    if not os.path.exists(path):
        return {}
    return json.load(open(path, encoding="utf-8")) or {}


def build_candidates(book, dates: List[str], busy_dates: Set[str],
                     panel: Optional[dict] = None,
                     cfg: Optional[dict] = None) -> List[dict]:
    """返回抄底候选（与生产候选同构，action=DIP），供结算层与 sim 导出一并消费。

    book        EtfBook（提供 sw_to_etf / open_ / idx）
    dates       信号日历（与 panel 交易日轴同源，回测窗口内）
    busy_dates  主策略「当日有可执行候选」的日期集合（only_blank_days 用）
    """
    cfg = dict(cfg or getattr(C, "DIP", {}) or {})
    if not cfg.get("enabled"):
        return []
    panel = panel if panel is not None else _load_panel()
    meta = panel.get("meta") or {}
    series = panel.get("series") or {}
    sw_to_etf = book.sw_to_etf or {}
    dd_w = int(cfg.get("dd_window", 20))
    dd_t = float(cfg.get("dd_trigger", -0.15))
    rb_w = int(cfg.get("reb_window", 5))
    rb_t = float(cfg.get("reb_trigger", 0.03))
    hold = int(cfg.get("hold_days", 20))
    score = float(cfg.get("composite", 45.0))
    only_blank = bool(cfg.get("only_blank_days", True))
    cooldown = int(cfg.get("same_sid_cooldown", 0) or 0)
    dset = set(dates)

    out: List[dict] = []
    seen: Set[tuple] = set()
    sid_last: Dict[str, int] = {}       # sid → 上次触发日的账本下标（同标冷却用）
    for code, s in series.items():
        m = meta.get(code) or {}
        if m.get("level") not in ("L1", "L2"):
            continue
        name = m.get("name")
        sid = sw_to_etf.get(name)
        if not sid:
            continue
        ds = [d for d in dates if d in s]      # 与信号日历对齐
        if len(ds) < dd_w + 2:
            continue
        closes = np.array([s[d]["close"] for d in ds], dtype=float)
        for i in range(dd_w, len(ds) - 1):
            d = ds[i]
            if d not in dset:
                continue
            if only_blank and d in busy_dates:
                continue                        # 主策略这天有候选 → 不抢
            if closes[i] / closes[i - dd_w] - 1.0 > dd_t:
                continue
            lo = closes[max(0, i - rb_w):i + 1].min()
            if closes[i] / lo - 1.0 <= rb_t:
                continue
            bi = book.idx.get(d)
            if bi is None:
                continue
            buy_i = bi + 1
            if buy_i >= len(book.dates) or not book.open_(sid, buy_i):
                continue                        # 次日没有开盘价 → 不可执行
            key = (d, sid)
            if key in seen:                     # 同日同一 ETF 被多个行业指向 → 只留一条
                continue
            if cooldown and sid in sid_last and (bi - sid_last[sid]) < cooldown:
                continue                        # 同标冷却：冷却期内不重复建仓同一只 ETF
            seen.add(key)
            sid_last[sid] = bi
            out.append({
                "date": d, "sw_name": name, "horizon": hold, "composite": score,
                "etf_sid": sid, "action": "DIP", "dip": True, "sell_date": None,
                "dd20": round(float(closes[i] / closes[i - dd_w] - 1.0), 5),
                "reb": round(float(closes[i] / lo - 1.0), 5),
            })
    out.sort(key=lambda x: x["date"])
    return out
