#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清晨守护：保证「T 日 09:00 前」拿到「T-1 的官方真值结论」。

为什么需要它
------------
`ark.py daily` 本身是对的，但它不知道「源今天到底出没出数」。在错误的时间跑，
它会花 ~40 分钟（申万 120 个指数逐个全量重下）换来一个「水位未推进」，而结论
仍停在 T-2。本脚本把「等待」和「重活」拆开：

    轻量探针（1 个指数，约 15 秒）判断官方日线是否已到目标日
      ├─ 未到  → 睡 30 分钟再探，不跑 daily（不浪费 40 分钟）
      └─ 已到  → 跑 daily → 校验结论日是否 == 目标日 → verify → 可选发布

设计约束（与项目红线一致）
--------------------------
· 只用官方日线，不引入实时兜底 / est 估算 —— 结论必须可靠、不被次日推翻。
· 不改任何策略参数，不写 L0，不执行 repair。
· 未达标只告警退出（非 0），绝不发布半成品。

用法
----
    python engine/nightly.py                  # 探到就跑到今天，不发布
    python engine/nightly.py --publish        # 校验通过后 git push 触发 Pages
    python engine/nightly.py --deadline 08:00 --interval 1800
    python engine/nightly.py --probe-only     # 只探测一次并打印官方最后日期

crontab（北京时间 03:10 起，脚本自己负责重试到 deadline）：
    10 3 * * 2-6 /path/to/.venv/bin/python /path/to/engine/nightly.py --publish >> /path/to/cache/logs/nightly.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))
PY = sys.executable
ENV = dict(os.environ, NO_PROXY="*", no_proxy="*")   # 免疫失效系统代理

TODAY_JSON = os.path.join(ROOT, "web", "public", "data", "today.json")
PROBE_CODE = "801010"      # 农林牧渔，L1 里最稳的一个；只用来判断「日线到哪天」


def log(msg: str) -> None:
    print(f"[nightly {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def prev_trading_day(today: datetime) -> str | None:
    """严格早于 today 的最后一个交易日（YYYY-MM-DD）。取不到日历则返回 None。"""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()["trade_date"]
        ds = sorted(str(d) for d in cal)
        cur = today.strftime("%Y-%m-%d")
        cand = [d for d in ds if d < cur]
        return cand[-1] if cand else None
    except Exception as e:
        log(f"[warn] 交易日历不可用（{type(e).__name__}: {e}），改用「今天的前一个工作日」")
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime("%Y-%m-%d")


def official_last() -> str | None:
    """探针：申万官方日线当前最后一天。单次约 15 秒，不落盘。"""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        df = ak.index_hist_sw(symbol=PROBE_CODE, period="day")
        return str(df["日期"].iloc[-1])
    except Exception as e:
        log(f"[warn] 探针失败：{type(e).__name__}: {e}")
        return None


def etf_last() -> str | None:
    """结算层探针：新浪 ETF 日线最后一天。判断层到了但 ETF 没到 → 结论会被截断。"""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        df = ak.fund_etf_hist_sina(symbol="sh510300")
        return str(df["date"].iloc[-1])
    except Exception as e:
        log(f"[warn] ETF 探针失败：{type(e).__name__}: {e}")
        return None


def conclusion_date() -> str | None:
    try:
        with open(TODAY_JSON, encoding="utf-8") as f:
            return json.load(f).get("date")
    except Exception:
        return None


def run(cmd: list[str], timeout: int = 7200) -> tuple[int, str]:
    log("$ " + " ".join(cmd[1:]))
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=timeout)
    tail = (p.stdout or "")[-1200:] + (("\n[stderr]\n" + p.stderr[-800:]) if p.stderr.strip() else "")
    log(f"  退出码 {p.returncode}，耗时 {time.time()-t0:.0f}s")
    return p.returncode, tail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline", default="08:00", help="最晚等到几点（本地时间 HH:MM）")
    ap.add_argument("--interval", type=int, default=1800, help="探测间隔（秒）")
    ap.add_argument("--publish", action="store_true", help="verify 通过后 git push 触发发布")
    ap.add_argument("--probe-only", action="store_true", help="只探测一次并退出")
    args = ap.parse_args()

    now = datetime.now()
    target = prev_trading_day(now)
    log(f"目标结论日（上一交易日）= {target}；官方申万日线最后 = {official_last()}；"
        f"新浪 ETF 日线最后 = {etf_last()}")

    if args.probe_only:
        return 0
    if not target:
        log("✗ 无法确定目标交易日，退出")
        return 2

    hh, mm = (int(x) for x in args.deadline.split(":"))
    deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if deadline <= now:
        deadline += timedelta(days=1)

    while True:
        now = datetime.now()
        ol, el = official_last(), etf_last()
        log(f"探测：申万 {ol} / ETF {el} / 目标 {target}")

        # 两层都到齐才动手：只要 ETF 没到，结论必被结算层截回，跑了也白跑
        if ol and el and ol >= target and el >= target:
            log("两层数据已到齐，开始跑 daily --official（官方真值，无估算）")
            rc, out = run([PY, "-u", os.path.join(ROOT, "engine", "ark.py"),
                           "daily", "--official"])
            if rc != 0:
                log("✗ daily 失败，最后输出：\n" + out)
            else:
                cd = conclusion_date()
                log(f"daily 完成，结论日 = {cd}")
                if cd == target:
                    rc2, out2 = run([PY, "-u", os.path.join(ROOT, "engine", "ark.py"), "verify"])
                    if rc2 != 0:
                        log("✗ verify 未通过 —— 不发布。最后输出：\n" + out2)
                        return 3
                    log("✓ verify 通过")
                    if args.publish:
                        # site/ 含 data/day（Pages 打包的就是它，缺了 Pages 上全 404）；
                        # web/public/data 是入库的数据层 json，一并提交保持仓库自洽。
                        run(["git", "-C", ROOT, "add", "-A", "site", "web/public/data"], timeout=1800)
                        rcc, outc = run(["git", "-C", ROOT, "commit", "-m",
                                         f"data: {target} 结论（{now:%Y-%m-%d %H:%M} 官方真值）"], timeout=600)
                        if rcc != 0 and "nothing to commit" not in outc:
                            log("✗ commit 失败：\n" + outc)
                            return 4
                        rc4, out4 = run(["git", "-C", ROOT, "push", "origin", "HEAD"], timeout=1800)
                        if rc4 != 0:
                            log("✗ push 失败：\n" + out4)
                            return 4
                        log("✓ 已推送，GitHub Pages 将自动发布")
                    log(f"完成：{target} 的可靠结论已就位")
                    return 0
                log(f"⚠ 结论日 {cd} != 目标 {target}，继续等待")
        else:
            log("官方数据尚未到齐，跳过 daily（省下约 40 分钟空抓）")

        if datetime.now() >= deadline:
            log(f"✗ 已到截止时间 {args.deadline}，仍未拿到 {target} 的结论 —— 请人工检查数据源")
            return 1

        nxt = min(deadline, datetime.now() + timedelta(seconds=args.interval))
        wait = max(60, int((nxt - datetime.now()).total_seconds()))
        log(f"睡眠 {wait//60} 分钟后再探")
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
