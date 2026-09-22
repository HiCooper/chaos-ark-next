# -*- coding: utf-8 -*-
"""MATURE 阶段降权信号层实验台（L4，一次性产物，不影响生产口径）。

假设：成熟期（MATURE）主线的结论质量差（追高兑现窗口），对其综合分降权，
让它少出结论 / 在 Top-K 与结算槽位竞争中让位，能否改善下行年份表现
（2026 -15.5%、2017 -7.8%、2024 -9.5%）而不伤进攻年。

与阶段周期实验（lab_stage_horizon）的本质区别：那边只改结算层的持有期，
这里改的是**信号生成本身**——降权后的行业在排名、入选阈值、Top-K 名额、
结算槽位竞争中全部真实让位，被挤掉的结论会由后面的候选补上。

实现：classify.py 读环境变量 ARK_MATURE_W（默认 1.0 = 生产行为），
在 classify_day 里对 stage==MATURE 的候选 composite 乘系数。
每个变体在独立 OUT_DIR 全量重放（--all，离线缓存），产出各自的
review.json（含 settlement），对照组 w=1.0 必须与生产 nav 精确一致。

变体：
  W10  w=1.0  对照（复现校验）
  W07  w=0.7  温和降权
  W05  w=0.5  中度降权
  W00  w=0.0  成熟期完全禁言（composite=0，必然跌出入选阈值）

运行：python lab_mature_weight.py   （4 次全量重放，约 20 分钟）
"""
import json
import os
import subprocess
import sys
import time

ENGINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ENGINE)
import config as C  # noqa: E402

VARIANTS = [("W10", "1.0"), ("W085", "0.85"), ("W07", "0.7"), ("W05", "0.5"), ("W00", "0.0")]


def run_variant(tag: str, w: str) -> str:
    outdir = f"{C.CACHE_DIR}/lab_mature_{tag}"
    env = os.environ.copy()
    env["ARK_MATURE_W"] = w
    env.pop("HTTP_PROXY", None), env.pop("HTTPS_PROXY", None)  # 纯离线，防代理干扰
    env["NO_PROXY"] = env["no_proxy"] = "*"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {ENGINE!r})\n"
        "import config as C\n"
        f"C.OUT_DIR = {outdir!r}\n"
        "import pipeline\n"
        "sys.argv = ['pipeline.py', '--all']\n"
        "pipeline.main()\n"
    )
    log = f"/tmp/lab_mature_{tag}.log"
    t0 = time.time()
    with open(log, "w") as f:
        r = subprocess.run([sys.executable, "-u", "-c", code], env=env,
                           stdout=f, stderr=subprocess.STDOUT, cwd=ENGINE)
    ok = r.returncode == 0
    print(f"[{tag}] w={w} -> {'OK' if ok else 'FAIL'} ({time.time()-t0:.0f}s) log={log}",
          flush=True)
    return outdir if ok else None


def metrics(review_path: str) -> dict | None:
    r = json.load(open(review_path))
    stl = r.get("settlement")
    if not stl:
        return None
    cur = stl["curve"]
    # 逐年组合收益
    yr = {}
    for i, d in enumerate(cur["dates"]):
        y = d[:4]
        v = yr.setdefault(y, {"first": i, "last": i,
                              "b": cur["nav"][i - 1] if i else cur["nav"][0],
                              "bb": cur["bench"][i - 1] if i else cur["bench"][0]})
        v["last"] = i
    yearly = {y: cur["nav"][v["last"]] / v["b"] - 1 for y, v in yr.items()}
    ev = stl.get("events", [])
    return {"cagr": stl["stats"]["cagr"], "maxdd": stl["stats"]["maxdd"],
            "sharpe": stl["stats"].get("sharpe"), "yearly": yearly,
            "n_B": sum(1 for e in ev if e["k"] == "B"),
            "n_S": sum(1 for e in ev if e["k"] == "S"),
            "win_rate": stl.get("win_rate_excess"),
            "overview_win": r["overview"]["win_rate"],
            "n_evaluated": r["overview"]["evaluated"]}


def main() -> None:
    outs = {}
    for tag, w in VARIANTS:
        outs[tag] = run_variant(tag, w)

    print("\n=== 结果对比 ===", flush=True)
    base_ref = metrics(f"{C.ROOT}/web/public/data/review.json")
    rows = []
    for tag, w in VARIANTS:
        if outs[tag]:
            m = metrics(f"{outs[tag]}/review.json")
            rows.append((tag, w, m))
        else:
            rows.append((tag, w, None))

    print(f"{'变体':<6} {'w':>4} {'cagr':>8} {'maxdd':>8} {'sharpe':>7} "
          f"{'信号胜率':>7} {'B':>5} {'S':>5} {'结论数':>6}")
    print(f"{'生产':<6} {'1.0':>4} {base_ref['cagr']:>8.4f} {base_ref['maxdd']:>8.4f} "
          f"{base_ref['sharpe']:>7.3f} {base_ref['overview_win']:>7.1%} "
          f"{base_ref['n_B']:>5} {base_ref['n_S']:>5} {base_ref['n_evaluated']:>6}")
    for tag, w, m in rows:
        if m is None:
            print(f"{tag:<6} {w:>4}  FAIL")
            continue
        print(f"{tag:<6} {w:>4} {m['cagr']:>8.4f} {m['maxdd']:>8.4f} {m['sharpe']:>7.3f} "
              f"{m['overview_win']:>7.1%} {m['n_B']:>5} {m['n_S']:>5} {m['n_evaluated']:>6}")

    print("\n=== 逐年组合收益（生产 -> 变体） ===")
    for tag, w, m in rows:
        if m is None:
            continue
        print(f"\n{tag} (w={w})")
        for y in sorted(base_ref["yearly"]):
            a = base_ref["yearly"][y]
            c = m["yearly"].get(y)
            if c is None:
                continue
            mark = " *" if abs(c - a) > 0.005 else ""
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%{mark}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "production": {k: base_ref[k] for k in ("cagr", "maxdd", "sharpe", "yearly")},
           "variants": {tag: {"w": float(w), **(m or {"fail": True})}
                        for (tag, w), m in zip([(t, w) for t, w, _ in rows],
                                               [r[2] for r in rows])}}
    json.dump(out, open(f"{C.CACHE_DIR}/lab_mature_weight.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/lab_mature_weight.json")


if __name__ == "__main__":
    main()
