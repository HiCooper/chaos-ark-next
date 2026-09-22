#!/usr/bin/env python3
"""离线重建历史日研判文件 day/<date>.json。

背景：东财行情接口不可用时，无法全量重跑 pipeline（需抓取 137MB 行情）。
本脚本绕开抓取，直接复用已落盘的 review.json（完整交易流水 events）+ history.json
（精简结论索引），为每个交易日重建一份「当日研判」文件，与 today.json 同构。

能精确重建的（来自 events 真实结算流水）：
  - 在场持仓：谁在场、买入日/买入价、建议周期、综合分、行业、ETF 名称
  - 当日到期出手（sell_today）：到期日=该日的笔，含真实收益 ret、出手原因 why
  - 到期倒计时（sell_soon / due_in / end_date / held）
  - 入手/埋伏结论（picks，来自 history.json 精简索引）

无法离线重建的（需单 ETF 日线，缺失时置 null）：
  - price（当日现价）、ret（持仓浮盈亏）
  - warn（MA20 失效预警）、invalid / invalid_note / beyond_data / level
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "web", "public", "data")
OUT = os.path.join(DATA, "day")
SOON_DAYS = 10
MAX_HOLDINGS = 32

RULE = ("建仓：信号日 T 收盘出结论 → T+1 开盘买入；出手：持有到建议周期结束的当日收盘卖出（主口径）。"
        "失效预警需逐日行情，离线重建无法还原，历史日不展示。")


def load():
    rev = json.load(open(os.path.join(DATA, "review.json")))
    hist = json.load(open(os.path.join(DATA, "history.json")))
    s = rev["settlement"]
    dates = s["curve"]["dates"]  # 2606 个交易日，与 history.days 日期集合一致
    etf_names = s.get("etf_names", {})
    events = s.get("events", [])
    universe = hist.get("universe", {})
    return dates, etf_names, events, hist["days"], universe


def build_position_index(dates, events):
    """把买卖流水整理成按「卖出日索引」和「买入日索引」可查的结构。"""
    buys = []      # 每笔：{sid,sw,a,h,sc,bpx,buy_i}
    sells_by_i = {}  # 卖出日索引 -> [{...S 事件 + 买入信息}]
    for e in events:
        if e["k"] == "B":
            buys.append({
                "sid": e["sid"], "sw": e["sw"], "a": e["a"], "h": int(e["h"]),
                "sc": e.get("sc"), "bpx": e["price"], "buy_i": int(e["i"]),
            })
        else:  # S
            sells_by_i.setdefault(int(e["i"]), []).append(e)
    return buys, sells_by_i


def etf_of(sid, etf_names):
    name = etf_names.get(sid, "")
    return name, sid


def pos_payload(sw, sid, etf, etf_code, action, horizon, buy_date, buy_price,
                held, end_date, due_in, score, exit_hint=None, ret=None, tone=None):
    return {
        "sw": sw, "level": None, "etf": etf, "etf_code": etf_code, "sid": sid,
        "action": action, "horizon": horizon, "buy_date": buy_date,
        "buy_price": buy_price, "price": None, "ret": ret, "held": held,
        "end_date": end_date, "due_in": due_in, "beyond_data": None,
        "score": score, "invalid": False, "invalid_note": None,
        "exit_hint": exit_hint, "_tone": tone,
    }


def build_trade_map(buys, sells_by_i, dates):
    """为每笔买入找到它的卖出事件（按 sid + 买入日匹配），返回 {buy_key: sell_event}。"""
    trade_map = {}
    # 先建 (sid, buy_date) -> sell 事件的索引
    sell_key = {}
    for i, lst in sells_by_i.items():
        for e in lst:
            sell_key[(e["sid"], e["bd"])] = (i, e)
    for b in buys:
        key = (b["sid"], dates[b["buy_i"]])
        trade_map[id(b)] = sell_key.get(key)  # (卖出日索引, S事件) 或 None
    return trade_map


def rebuild_day(iD, date, dates, buys, sells_by_i, etf_names, trade_map):
    """重建某交易日的「出手判断」action_today。

    判定逻辑（以「卖出日」为锚，比「是否已卖」更清晰）：
      - 卖出日 == iD  → 当日到期出手（sell_today）
      - 卖出日 <  iD  → 已卖出，不在场
      - 卖出日 >  iD 或从未卖出 → 仍在场（open / sell_soon，按到期倒计时分）
    """
    open_pos, sell_today, sell_soon = [], [], []
    for b in buys:
        buy_i = b["buy_i"]
        if buy_i > iD:
            continue  # 还没买入
        end_i = buy_i + b["h"]
        etf, etf_code = etf_of(b["sid"], etf_names)
        held = iD - buy_i
        due_in = end_i - iD
        end_date = dates[end_i] if end_i < len(dates) else None
        base = (b["sw"], b["sid"], etf, etf_code, b["a"], b["h"],
                dates[buy_i], b["bpx"], held, end_date, due_in, b["sc"])

        sold = trade_map.get(id(b))
        if sold is not None:
            sell_i, e = sold
            if sell_i == iD:
                # 当日到期出手
                sell_today.append(pos_payload(
                    *base, exit_hint=e.get("why"), ret=e.get("ret"), tone="sell"))
                continue
            elif sell_i < iD:
                continue  # 之前已卖出
            # sell_i > iD：仍在场，落入下方在场分支
        if due_in <= 0:
            sell_soon.append(pos_payload(*base, tone="soon"))
        elif due_in <= SOON_DAYS:
            sell_soon.append(pos_payload(*base, tone="soon"))
        else:
            open_pos.append(pos_payload(*base))

    open_pos.sort(key=lambda x: (x["end_date"] or "9999"))
    sell_soon.sort(key=lambda x: x["due_in"])
    sell_today.sort(key=lambda x: x["due_in"])

    n_open = len(open_pos)
    helds = [p["held"] for p in open_pos + sell_today + sell_soon]
    return {
        "as_of": date,
        "n_open": n_open,
        "n_sell_today": len(sell_today),
        "n_warn": 0,
        "n_unpriced": 0,
        "open": open_pos,
        "sell_today": sell_today,
        "sell_soon": sell_soon,
        "warn": [],
        "avg_held": round(sum(helds) / len(helds), 1) if helds else 0,
        "median_held": (sorted(helds)[len(helds) // 2] if helds else 0),
        "max_held": max(helds) if helds else 0,
        "soon_days": SOON_DAYS,
        "max_holdings": MAX_HOLDINGS,
        "rule": RULE,
        "_offline": True,
    }


def main():
    dates, etf_names, events, days, universe = load()
    buys, sells_by_i = build_position_index(dates, events)
    trade_map = build_trade_map(buys, sells_by_i, dates)
    days_map = {d["date"]: d for d in days}
    latest = days[-1]["date"]  # 最新交易日，由 today.json 承载完整数据（含未结算在场持仓）
    os.makedirs(OUT, exist_ok=True)
    universe_size = universe.get("total")  # 候选池规模（离线只有全量口径，无当日口径）

    n_with_pos = 0
    n_sell = 0
    for iD, date in enumerate(dates):
        d = days_map.get(date)
        if d is None:
            continue  # history.days 未覆盖的日期（如 2015-12-31 初始日）
        if date == latest:
            continue  # 跳过最新日：today.json 已是完整数据，离线流水缺未结算持仓会失真
        at = rebuild_day(iD, date, dates, buys, sells_by_i, etf_names, trade_map)
        if at["n_open"] or at["sell_today"] or at["sell_soon"]:
            n_with_pos += 1
        if at["sell_today"]:
            n_sell += 1
        # 降级 regime_info：环境五维/20日统计需要逐日行情，离线只有 regime + env_score。
        # 构造成 today.json 同构的骨架，让前端「市场环境」区块至少显示热度、不整块空白。
        regime_info = {
            "env_score": d.get("env_score"),
            "confidence": None,
            "forced": None,
            "components": None,   # 五维条：离线缺失，前端降级为占位
            "breadth20": None,
            "median_ret20": None,
            "bench_ret20": None,
            "bench_dd60": None,
            "vol_pct": None,
        }
        payload = {
            "date": date,
            "regime": d.get("regime"),
            "regime_info": regime_info,
            "universe_size": universe_size,
            "summary": d.get("summary", ""),
            "picks": d.get("picks", []),
            "avoid": None,       # 回避清单：离线缺失
            "watch_top": None,   # 观察区：离线缺失
            "action_today": at,
            "_offline": True,
        }
        json.dump(payload, open(os.path.join(OUT, f"{date}.json"), "w"),
                  ensure_ascii=False)

    print(f"完成：{len(dates)} 个交易日 → {OUT}/")
    print(f"  有在场持仓/出手的日子：{n_with_pos}")
    print(f"  有到期出手的日子：{n_sell}")
    # 顺带校验最新日与 today.json 的 n_open 是否接近
    t = json.load(open(os.path.join(DATA, "today.json")))
    at0 = t.get("action_today")
    print(f"  最新日 today.json n_open={at0['n_open'] if at0 else 'N/A'}"
          f"（离线重建可对照）")


if __name__ == "__main__":
    main()
