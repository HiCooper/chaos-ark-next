#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布前检查：确认本机 site/ 是「最新产物」，再交给 WorkBuddy 托管发布。

为什么需要它
------------
项目现在有两条发布通道，读的是同一份 site/：

    通道 A（自动）  远程机器跑 nightly → git push → GitHub Pages 自动发布
    通道 B（人工）  本机 site/ → WorkBuddy 托管（ark-console.app.workbuddy.host）

通道 B 有个容易踩的坑：产出发生在远程，本机只是「消费端」。如果本机没先 pull，
它手上的 site/ 就是几天前的旧产物，此时发布等于**用旧数据覆盖线上新数据**，
而且不会有任何报错——页面照常能打开。

本脚本把这一步变成带校验的动作：先比对远程，再打印「即将发布的数据指纹」，
指纹对得上才发布。宁可拒绝发布，也不要静默降级。

用法
----
    python engine/pre_publish.py            # 只检查（默认）
    python engine/pre_publish.py --pull     # 落后远程时先 fast-forward 再检查
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")


def sh(*args: str) -> tuple[int, str]:
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip() + (("\n" + p.stderr.strip()) if p.stderr.strip() else "")


def jload(rel: str):
    try:
        with open(os.path.join(SITE, rel), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true", help="落后远程时先 fast-forward")
    args = ap.parse_args()

    problems: list[str] = []

    # ---- 1. 与远程对齐 ----
    sh("git", "fetch", "origin", "--quiet")
    _, cnt = sh("git", "rev-list", "--left-right", "--count", "origin/main...HEAD")
    behind = ahead = 0
    try:
        behind, ahead = (int(x) for x in cnt.split()[:2])
    except Exception:
        problems.append(f"无法读取与 origin/main 的差距：{cnt!r}")

    dirty = [l for l in sh("git", "status", "--porcelain", "site")[1].splitlines() if l.strip()]

    print(f"仓库状态：落后远程 {behind} 个提交，领先 {ahead} 个，site/ 未提交改动 {len(dirty)} 个")

    if behind and not args.pull:
        problems.append(f"本机落后 origin/main {behind} 个提交 —— 远程已有更新产物，"
                        f"先运行 `git pull` 或本脚本加 --pull，否则会发布旧数据")
    elif behind and args.pull:
        rc, out = sh("git", "pull", "--ff-only", "origin", "main")
        if rc != 0:
            problems.append(f"git pull 失败（本机可能有未提交改动）:\n{out}")
        else:
            print("✓ 已 fast-forward 到远程最新")

    # ---- 2. 必需文件完整性 ----
    must = ["index.html", "data/today.json", "data/review.json", "data/history.json",
            "data/sim.json", "view/etf/_index.json"]
    for rel in must:
        p = os.path.join(SITE, rel)
        if not os.path.exists(p):
            problems.append(f"缺少必需产物 site/{rel}")

    day_dir = os.path.join(SITE, "data", "day")
    n_day = len(os.listdir(day_dir)) if os.path.isdir(day_dir) else 0
    if n_day < 100:
        problems.append(f"site/data/day 只有 {n_day} 个文件（异常，Pages 与前端都依赖它）")

    # ---- 3. index.html 引用的资产必须存在 ----
    try:
        with open(os.path.join(SITE, "index.html"), encoding="utf-8") as f:
            html = f.read()
        import re
        for asset in set(re.findall(r'assets/[A-Za-z0-9._-]+', html)):
            if not os.path.exists(os.path.join(SITE, asset)):
                problems.append(f"index.html 引用了不存在的资产 site/{asset}")
    except Exception as e:
        problems.append(f"index.html 读取失败：{e}")

    # ---- 4. 数据指纹 ----
    today, review, hist = jload("data/today.json"), jload("data/review.json"), jload("data/history.json")
    print()
    print("即将发布的数据指纹：")
    if today:
        print(f"  today.json    结论日 {today.get('date')} | 环境 {today.get('regime')} | "
              f"结论 {len(today.get('picks') or [])} 条 | engine {today.get('engine_version')}")
    if review:
        print(f"  review.json   generated_at {review.get('generated_at')}")
    if hist:
        days = hist.get("dates") or hist.get("days") or []
        if isinstance(days, list) and days:
            last = days[-1]
            last = last.get("date") if isinstance(last, dict) else last
            print(f"  history.json  {len(days)} 个交易日，末尾 {last}")
    print(f"  day 文件      {n_day} 个")
    print(f"  site 体积     {sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(SITE) for fn in fns) / 1048576:.0f} MB")

    print()
    if problems:
        print("✗ 不能发布，需要先处理：")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("✓ 检查通过，site/ 是最新完整产物，可以发布到 WorkBuddy 托管")
    return 0


if __name__ == "__main__":
    sys.exit(main())
