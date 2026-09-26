#!/usr/bin/env python3
"""
orna_model_compare.py - run the SAME /orna requests through two (or more) local
models and compare speed, tool use and correctness.

Reuses orna_test_suite's graded cases rather than inventing new prompts, so
"was the answer right" is decided by the same live-data expectations the
regression suite uses, not by reading the replies by eye. It adds what the
suite does not record: wall-clock per request, step count, and the exact tool
sequence each model chose.

    set -a && source .env && set +a
    MODELS="nemotron-3.5-lightning:30b-mlx,qwen3.8:27b-mlx" \
        CASES="falx-facts,trifecta-classes,ward-formula,sirus-strategy" \
        N=1 python3 orna_model_compare.py

Every run is LOCAL-ONLY (MAX_CLOUD_CALLS=0): comparing local models through a
cloud-first loop would mostly measure the cloud. Results append to
orna_model_compare.json, so a long comparison can be run one model per
invocation and still print one combined table at the end (`REPORT=1` prints the
table from the file without running anything).

Caveats to read the numbers with:
  * The FIRST request against a freshly loaded model pays its cold load (~16s
    was measured for a 30B MLX model), so it is timed but flagged.
  * Timings are wall-clock for the whole ReAct loop, which includes the real
    codex/sheets/Ollama calls the tools make - it is not a tokens/sec figure.
  * One run per case proves nothing about a non-deterministic model; N>=3 if a
    difference looks small. The suite's own thresholds exist for this reason.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time

REPO_ROOT = os.environ.get("ORNA_REPO_ROOT") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

if not os.environ.get("SHEETS_API_KEY"):
    sys.exit("SHEETS_API_KEY is not set - run `set -a && source .env && set +a` first.")

# Pin sampling for the same reason the suite does, so a difference between
# models is less likely to be a difference between two samples of one model.
os.environ.setdefault("ORNA_LLM_TEMPERATURE", "0")
os.environ.setdefault("ORNA_LLM_SEED", "20260925")

import orna_test_suite as S      # noqa: E402  - cases, graders and FakeMessage
import telegram_orna as T        # noqa: E402

RESULTS_PATH = os.path.join(REPO_ROOT, "orna_model_compare.json")


def _load_results() -> list:
    if not os.path.exists(RESULTS_PATH):
        return []
    try:
        with open(RESULTS_PATH) as fh:
            return json.load(fh).get("runs", [])
    except Exception:
        return []


def _save_results(runs: list) -> None:
    with open(RESULTS_PATH, "w") as fh:
        json.dump({"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": runs}, fh, indent=2)


def report(runs: list) -> None:
    """One table per case, one row per model, plus a per-model summary."""
    if not runs:
        print("no results yet")
        return
    models = sorted({r["model"] for r in runs})
    cases = [c for c in dict.fromkeys(r["case"] for r in runs)]

    print(f"\n{'=' * 78}\nPER CASE (t = median wall-clock seconds, steps = tool calls made)\n{'=' * 78}")
    for case in cases:
        print(f"\n  {case}")
        for model in models:
            rows = [r for r in runs if r["case"] == case and r["model"] == model]
            if not rows:
                continue
            ok = sum(1 for r in rows if not r["reason"])
            t = statistics.median(r["seconds"] for r in rows)
            steps = statistics.median(r["steps"] for r in rows)
            tools = rows[0]["tools"] or ["-"]
            print(f"    {model:<34} {ok}/{len(rows)} pass  t={t:6.1f}s  steps={steps:g}  "
                  f"tools={','.join(tools)[:60]}")
            for r in rows:
                if r["reason"]:
                    print(f"        FAIL: {r['reason'][:100]}")

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    for model in models:
        rows = [r for r in runs if r["model"] == model]
        ok = sum(1 for r in rows if not r["reason"])
        times = [r["seconds"] for r in rows]
        print(f"  {model:<34} {ok}/{len(rows)} pass  "
              f"median {statistics.median(times):6.1f}s  total {sum(times):7.1f}s  "
              f"steps/req {statistics.median([r['steps'] for r in rows]):g}")
    # Tool-choice differences are often the real story: two models can both
    # pass while one takes four tool calls and the other eleven.
    print("\n  tool usage (calls per model, all cases):")
    for model in models:
        counts: dict = {}
        for r in runs:
            if r["model"] == model:
                for tool in r["tools"]:
                    counts[tool] = counts.get(tool, 0) + 1
        ordered = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        print(f"    {model:<34} {ordered or '-'}")


async def main() -> int:
    if os.environ.get("REPORT") == "1":
        report(_load_results())
        return 0

    models = [m.strip() for m in (os.environ.get("MODELS") or T.LOCAL_OLLAMA_MODEL).split(",") if m.strip()]
    wanted = {c.strip() for c in (os.environ.get("CASES") or "").split(",") if c.strip()}
    n = int(os.environ.get("N", "1"))
    cases = [c for c in S.build_cases() if not wanted or c.id in wanted]
    if not cases:
        sys.exit(f"no cases matched {sorted(wanted)}")

    # Local only: through a cloud-first loop most steps would measure the cloud.
    T.MAX_CLOUD_CALLS = 0
    runs = _load_results()
    for model in models:
        print(f"\n{'#' * 78}\n# {model}  ({len(cases)} case(s) x N={n}, local only)\n{'#' * 78}")
        T.LOCAL_OLLAMA_MODEL = model
        first = True
        for case in cases:
            for _ in range(n):
                started = time.time()
                try:
                    reason, tools, final = await S.run_case_once(case)
                except Exception as exc:
                    reason, tools, final = f"harness raised {exc!r}", [], ""
                elapsed = time.time() - started
                runs.append({"model": model, "case": case.id, "tier": case.tier,
                             "seconds": round(elapsed, 1), "steps": len(tools), "tools": tools,
                             "reason": reason, "cold": first, "answer": final[:400]})
                mark = "PASS" if not reason else "FAIL"
                print(f"  {mark}  [{case.tier}] {case.id:<20} {elapsed:6.1f}s  "
                      f"{len(tools):2d} steps  {','.join(tools)[:52]}"
                      + ("   (cold load)" if first else ""))
                if reason:
                    print(f"        {reason[:110]}")
                first = False
                _save_results(runs)      # survive a timeout mid-comparison
    report(runs)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
