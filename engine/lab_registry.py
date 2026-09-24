# -*- coding: utf-8 -*-
"""实验登记簿（web/public/data/experiments.json）的 schema、校验器与生成器。

为什么需要这个模块
------------------
`experiments.json` 原本是纯手工维护的：没有 schema、没有校验、没有生成器。
后果是字段慢慢漂移——2026-09-24 就出现 3 条"口径变更记录"型条目缺
`hypothesis/params/results/goals`，点击展开直接抛错、整页白屏。

本模块把"实验记录"变成有约束的产出：
    · **唯一 schema**：两类记录（对照实验 / 口径变更登记）+ 7 种状态徽章，
      字段的必填/推荐/类型都在这里定义，前端与文档都以它为准；
    · **校验**：`check` 逐条检查（缺字段、TODO 未填、徽章越界、日期/ID 格式、
      结果表数值缺失、产物文件不存在……），有问题就非零退出；
    · **生成器**：`new` 直接产出**字段齐全**的骨架（必填项以 TODO 占位 →
      校验必然失败，逼你把话写清楚，而不是留个空字段等以后白屏）。
    · 校验已接入 `ark.py verify` 与 `ark.py daily`：坏记录进不了站点。

用法
----
    python3 engine/lab_registry.py check
    python3 engine/lab_registry.py new <id> --kind experiment --title "…" --layer "信号层"
    python3 engine/lab_registry.py new <id> --kind change --title "…" --layer "结算层（记账口径）"
    python3 engine/lab_registry.py backfill-kind      # 给历史条目补 kind（一次性）
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

PATH = f"{C.OUT_DIR}/experiments.json"
ROOT = C.ROOT

# ------------------------------------------------------------------ schema
BADGES = ("已采用", "已落参", "验证通过", "已否决", "部分否决", "待拍板", "已记录")
KINDS = ("experiment", "change")

COMMON_REQUIRED = ("id", "date", "kind", "title", "layer", "status", "conclusion")
KIND_REQUIRED = {
    # 对照实验：必须有假设、参数、对照结果表、两条核心目标、效度检验
    "experiment": ("vs", "hypothesis", "params", "results", "goals", "robustness"),
    # 口径变更登记：只记背景、结果、结论（不硬凑空表）
    "change": ("vs", "why", "result"),
}
RECOMMENDED = ("artifacts",)
LIMITS = {"title": 90, "status": 6, "vs": 220, "layer": 40}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")   # 允许版本号点号，如 baseline-1.6.0
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")   # 允许 YYYY-MM（早期记录只精确到月）

TEMPLATES = {
    "experiment": {
        "vs": "TODO: 对照基准（版本 + 年化/回撤/Sharpe）",
        "hypothesis": "TODO: 一句话写清假设（要可证伪）",
        "params": [{"k": "TODO 参数名", "v": "TODO 值", "note": "TODO 作用"}],
        "results": [{"label": "基准", "cagr": None, "maxdd": None, "sharpe": None, "note": "TODO"},
                    {"label": "变体", "cagr": None, "maxdd": None, "sharpe": None, "note": "TODO"}],
        "goals": {"annualized": "TODO: 对年化收益的结论", "drawdown": "TODO: 对最大回撤的结论"},
        "robustness": "TODO: 统计效度（参数平台/分年度/样本外一致性；跑赢就写跑赢，跑输如实写）",
        "conclusion": "TODO: 实验结论 + 是否落参（若落参，写清 ENGINE_VERSION 与代价披露）",
        "artifacts": ["engine/lab_xxx.py"],
    },
    "change": {
        "vs": "TODO: 变更前的口径与关键指标",
        "why": "TODO: 为什么要改（旧口径错在哪 / 新证据是什么）",
        "result": "TODO: 变更后的口径与关键指标（含全量重放结果）",
        "conclusion": "TODO: 结论与后续动作（版本号、需要复测的地方）",
        "artifacts": ["engine/xxx.py"],
    },
}


# ------------------------------------------------------------------ 校验
def _todo(x) -> bool:
    if isinstance(x, str):
        return "TODO" in x
    if isinstance(x, dict):
        return any(_todo(v) for v in x.values())
    if isinstance(x, (list, tuple)):
        return any(_todo(v) for v in x)
    return False


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(payload: dict) -> Tuple[List[str], List[str]]:
    """返回 (errors, warnings)。errors 非空即视为登记簿不合格。"""
    errs: List[str] = []
    warns: List[str] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("experiments"), list):
        return (["experiments.json 顶层缺少 experiments 列表"], [])

    seen: Dict[str, int] = {}
    for idx, e in enumerate(payload["experiments"]):
        tag = e.get("id") or f"#{idx}"
        if not isinstance(e, dict):
            errs.append(f"{tag}: 记录不是对象")
            continue
        for f in COMMON_REQUIRED:
            if e.get(f) in (None, "", [], {}):
                errs.append(f"{tag}: 缺必填字段 {f}")
        kind = e.get("kind")
        if kind not in KINDS:
            errs.append(f"{tag}: kind 必须是 {KINDS} 之一（历史条目可跑 backfill-kind 补齐）")
            continue
        for f in KIND_REQUIRED[kind]:
            if e.get(f) in (None, "", [], {}):
                errs.append(f"{tag}: {kind} 型缺必填字段 {f}")
        for f in ("title", "status", "layer", "vs"):
            lim = LIMITS.get(f)
            v = e.get(f)
            if lim and isinstance(v, str) and len(v) > lim:
                errs.append(f"{tag}: {f} 过长（{len(v)} > {lim}）——徽章只放 {BADGES}，"
                            f"长说明放 conclusion / caveat")
        if e.get("status") not in BADGES:
            errs.append(f"{tag}: status「{e.get('status')}」不在允许集合 {BADGES} 内")
        if isinstance(e.get("status"), str) and "｜" in e["status"]:
            errs.append(f"{tag}: status 里不要塞说明（发现『｜』）——拆到 caveat 字段")
        if e.get("id") and not ID_RE.match(str(e["id"])):
            errs.append(f"{tag}: id 需为小写 kebab-case（字母数字与连字符）")
        if e.get("date") and not DATE_RE.match(str(e["date"])):
            errs.append(f"{tag}: date 需为 YYYY-MM-DD 或 YYYY-MM")
        if _todo(e):
            errs.append(f"{tag}: 存在 TODO 未填字段")
        # 结构化字段
        if kind == "experiment":
            ps = e.get("params")
            if isinstance(ps, list):
                for j, p in enumerate(ps):
                    if not isinstance(p, dict) or not p.get("k") or p.get("v") in (None, ""):
                        errs.append(f"{tag}: params[{j}] 需要 k 与非空 v")
            rs = e.get("results")
            if isinstance(rs, list):
                if not rs:
                    errs.append(f"{tag}: results 不能为空（至少要有一行基准/变体对照）")
                for j, r in enumerate(rs):
                    if not isinstance(r, dict) or not r.get("label"):
                        errs.append(f"{tag}: results[{j}] 缺 label")
                        continue
                    # 结果行两种合法形态：① 数值型（cagr+maxdd 都是数字）
                    #                  ② 文字型（用 note 承载分布/定性结论）
                    numeric = _is_num(r.get("cagr")) and _is_num(r.get("maxdd"))
                    textual = bool(str(r.get("note") or "").strip())
                    if not (numeric or textual):
                        errs.append(f"{tag}: results[{j}]（{r.get('label')}）要么给数字 cagr+maxdd，"
                                    f"要么用 note 写清结论——不接受空行")
            gl = e.get("goals")
            if isinstance(gl, dict):
                for gk in ("annualized", "drawdown"):
                    if not gl.get(gk):
                        errs.append(f"{tag}: goals 缺 {gk}")
        else:
            if not isinstance(e.get("result"), str):
                errs.append(f"{tag}: result 需为字符串")
        # 推荐字段
        for f in RECOMMENDED:
            if e.get(f) in (None, "", [], {}):
                warns.append(f"{tag}: 建议补 {f}（可复现产物；校验会检查文件是否存在）")
            elif f == "artifacts" and isinstance(e[f], list):
                for a in e[f]:
                    if not isinstance(a, str):
                        warns.append(f"{tag}: artifacts 元素需为字符串")
                        continue
                    path = re.split(r"\s*[(（]", a, 1)[0].strip()   # 允许「路径 (说明)」写法
                    if "*" in path or path.startswith("cache/"):
                        continue                       # 通配/缓存产物不检查（缓存可重建）
                    if path and not os.path.exists(os.path.join(ROOT, path)):
                        warns.append(f"{tag}: artifacts 里 {path} 在仓库中不存在")
        if not e.get("caveat") and e.get("status") in ("已采用", "已落参"):
            pass
        seen[tag] = seen.get(tag, 0) + 1
    dup = [k for k, v in seen.items() if v > 1]
    for d in dup:
        errs.append(f"{d}: id 重复（{seen[d]} 次）")
    return errs, warns


def load() -> dict:
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)


def save(d: dict) -> None:
    json.dump(d, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 命令
def cmd_check(verbose: bool = True) -> int:
    d = load()
    errs, warns = validate(d)
    from collections import Counter
    exps = d.get("experiments") or []
    kinds = Counter(e.get("kind") or "（缺 kind）" for e in exps)
    sts = Counter(e.get("status") or "（缺 state）" for e in exps)
    if verbose:
        print(f"实验登记簿：{len(exps)} 条 · 类型 {dict(kinds)}")
        print(f"  状态分布：{dict(sts)}")
    for w in warns:
        if verbose:
            print(f"  [warn] {w}")
    for e in errs:
        print(f"  ✗ {e}")
    if errs:
        print(f"\n✗ 实验登记簿校验失败：{len(errs)} 个问题（{len(warns)} 个建议）")
        print("  修法：python3 engine/lab_registry.py new <id> --kind experiment|change "
              "--title '…' --layer '…' 生成齐全骨架；或按上面逐条补齐。")
        return 1
    print(f"\n✓ 实验登记簿校验通过（{len(exps)} 条，{len(warns)} 个建议）")
    return 0


def cmd_new(argv: List[str]) -> int:
    if not argv:
        print("用法：lab_registry.py new <id> --kind experiment|change --title '…' --layer '…' "
              "[--status 待拍板] [--date YYYY-MM-DD]")
        return 2
    eid = argv[0]
    def opt(name: str, default: str = "") -> str:
        return argv[argv.index(name) + 1] if name in argv else default
    kind = opt("--kind", "experiment")
    if kind not in KINDS:
        print(f"kind 必须是 {KINDS} 之一")
        return 2
    d = load()
    if any(e.get("id") == eid for e in d.get("experiments") or []):
        print(f"✗ id「{eid}」已存在")
        return 2
    rec = {
        "id": eid, "date": opt("--date", time.strftime("%Y-%m-%d")), "kind": kind,
        "title": opt("--title", "TODO: 实验标题"), "layer": opt("--layer", "TODO: 作用层"),
        "status": opt("--status", "待拍板"),
    }
    rec.update(TEMPLATES[kind])
    d.setdefault("experiments", []).insert(0, rec)
    save(d)
    print(f"✓ 已追加实验记录骨架：{eid}（{kind}）→ {PATH}")
    print("  必填项已用 TODO 占位：**未填完前校验一定失败**（这是刻意的，避免又出现字段不齐）。")
    print("  下一步：填完字段 → python3 engine/lab_registry.py check")
    return 0


def cmd_backfill_kind() -> int:
    """给历史条目按字段形状补 kind（一次性迁移用）。"""
    d = load()
    n = 0
    for e in d.get("experiments") or []:
        if e.get("kind"):
            continue
        e["kind"] = "experiment" if (e.get("params") and e.get("results")) else "change"
        n += 1
    save(d)
    print(f"✓ 已补 kind：{n} 条")
    return cmd_check()


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else "check"
    if mode == "check":
        return cmd_check()
    if mode == "new":
        return cmd_new(args[1:])
    if mode == "backfill-kind":
        return cmd_backfill_kind()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
