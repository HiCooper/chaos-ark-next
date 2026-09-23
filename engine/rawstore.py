# -*- coding: utf-8 -*-
"""L0 原始数据层：外部数据源的一手快照，落盘后不可变。

分层约定（详见 docs/DATA-LAYERS.md）：
    L0  data/raw/     外部 API 的一手数据，只增不改（write-once + merge-append）
    L1  cache/        派生面板（panel.json / etf_panel.json），可随时从 L0 重算
    L2  cache/        特征层（features.npz / regime.json），可从 L1 重算
    L3  web/public/data  导出层（history / today / day / review），可从 L2 重算
    L4  cache/        实验台一次性产物（lab_* / ic_diag*），可删

不可变语义：
    - 同一 (kind, key) 重复抓取时按「日期」合并追加：新日期追加，重叠日期
      保留首采值（先到先得），绝不用新值覆盖旧值。
    - 每次写入更新 manifest.json：来源、抓取时间、行数、覆盖区间、sha256，
      事后可校验文件是否被人为改动。
    - 已知口径风险：前复权价在除权后会整体重述，重叠日期保留首采值意味着
      除权日附近可能出现微小断层。重放不变式（同一份 panel => 同一份结论）
      由 L1 冻结面板保证，不依赖 L0 的逐字节复现。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import List, Optional

import config as C  # noqa: E402

RAW_ROOT = f"{C.ROOT}/data/raw"
MANIFEST_PATH = f"{RAW_ROOT}/manifest.json"


def _manifest() -> dict:
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_manifest(m: dict):
    os.makedirs(RAW_ROOT, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def raw_path(kind: str, key: str) -> str:
    d = os.path.join(RAW_ROOT, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{key}.json")


def put(kind: str, key: str, records: List[dict], source: str,
        merge: bool = True, note: str = "", overwrite_est: bool = False) -> dict:
    """把一手数据写入 L0。records 必须含 'date' 字段；按日期 merge-append。

    overwrite_est=True 时，带 `est` 标记（估算值，如官方源滞后时的实时兜底）
    的旧行可以被**不带 est 的官方值**覆盖——这是"估算可被真值替换"的唯一通道，
    正常的一手数据之间仍然先到先得、绝不覆盖。
    """
    path = raw_path(kind, key)
    final = records
    n_replaced = 0
    if merge and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if old:
                inc_by_date = {r["date"]: r for r in records}
                kept = []
                for r in old:
                    inc = inc_by_date.get(r["date"])
                    if (overwrite_est and inc is not None and r.get("est")
                            and not inc.get("est")):
                        n_replaced += 1
                        continue          # 官方值替换估算值
                    kept.append(r)
                new_rows = [r for r in records if r["date"] not in {x["date"] for x in kept}]
                final = sorted(kept + new_rows, key=lambda r: r["date"])
        except Exception:
            final = records
    with open(path, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False)

    m = _manifest()
    dates = [r["date"] for r in final if r.get("date")]
    entry = {
        "source": source,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rows": len(final),
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
        "sha256": _sha256(path),
        "appended": len(final) - (len(records) if merge and final is not records else 0),
        "note": note,
        "replaced_est": n_replaced,
    }
    m.setdefault(kind, {})[key] = entry
    _save_manifest(m)
    return entry


def get(kind: str, key: str) -> Optional[List[dict]]:
    path = raw_path(kind, key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def verify(kind: Optional[str] = None) -> dict:
    """校验 L0 文件哈希与 manifest 是否一致。返回 {path: ok/FAIL}。"""
    m = _manifest()
    out = {}
    for k, entries in m.items():
        if kind and k != kind:
            continue
        for key, e in entries.items():
            p = raw_path(k, key)
            if not os.path.exists(p):
                out[f"{k}/{key}"] = "MISSING"
            else:
                out[f"{k}/{key}"] = "ok" if _sha256(p) == e.get("sha256") else "HASH_MISMATCH"
    return out


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=1))
