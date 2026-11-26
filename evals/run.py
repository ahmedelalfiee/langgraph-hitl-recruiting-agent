"""Run the agent's LLM jobs against the golden sets and fail if quality drops.

    python -m evals.run                 # all three suites, model from .env
    python -m evals.run --suite confirm # one suite
    python -m evals.run --limit 5       # quick smoke run

Writes evals/results/<model>.json and prints a summary. Exits with code 1 if any
suite falls below evals/thresholds.json, so CI can block a prompt or model change
that makes the agent worse.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from opzy.config import load_business, settings
from opzy.llm import make_llm

from .score import score_confirm, score_draft, score_triage, summarise

HERE = Path(__file__).parent
# USD per 1M tokens (input, output). Add your model here to get cost per task.
PRICES = {"gpt-5.4-mini": (0.75, 4.50), "gpt-5.4-nano": (0.20, 1.25)}


def load(name: str, limit: int | None) -> list[dict]:
    rows = [json.loads(l) for l in (HERE / "cases" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if l]
    return rows[:limit] if limit else rows


def _email(e: dict) -> dict:
    return {"id": "eval", "thread_id": "eval", "from_email": "", "message_id_header": "", **e}


def run_suite(name: str, llm, biz, limit: int | None = None) -> dict:
    rows = []
    for case in load(name, limit):
        t0 = time.perf_counter()
        try:
            if name == "triage":
                out = llm.triage(_email(case["email"]), biz)
                checks = score_triage(case, out)
            elif name == "confirm":
                out = llm.confirm({"body": case["body"]}, case["slots"], biz)
                checks = score_confirm(case, out)
            else:
                out = llm.draft(_email(case["email"]), case["triage"], case["slots"], biz)
                checks = score_draft(case, out, biz.signature)
        except Exception as err:  # a crash or unparseable output is a failed case, not a crashed run
            out, checks = {"error": f"{type(err).__name__}: {err}"}, {"ran": False}
        rows.append({"id": case["id"], "seconds": round(time.perf_counter() - t0, 2), "checks": checks, "output": out})
    report = summarise(rows)
    secs = sorted(r["seconds"] for r in rows)
    report["p50_seconds"] = secs[len(secs) // 2] if secs else None
    report["failures"] = {r["id"]: [k for k, v in r["checks"].items() if not v] for r in rows
                          if not all(r["checks"].values())}
    return {"summary": report, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["triage", "confirm", "draft"], action="append")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    biz, llm = load_business(), make_llm(settings)
    model = llm.model
    thresholds = json.loads((HERE / "thresholds.json").read_text())
    results, ok = {"model": model}, True
    for name in args.suite or ["triage", "confirm", "draft"]:
        before = dict(llm.usage or {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        res = run_suite(name, llm, biz, args.limit)
        after = llm.usage or before
        s = res["summary"]
        if model in PRICES and s["cases"]:
            pin, pout = PRICES[model]
            cost = ((after["input_tokens"] - before["input_tokens"]) * pin
                    + (after["output_tokens"] - before["output_tokens"]) * pout) / 1e6
            s["cost_per_1k_tasks_usd"] = round(1000 * cost / s["cases"], 3)
        floor = thresholds.get(name, 0)
        passed = s["all_checks_pass_pct"] >= floor
        ok &= passed
        print(f"{name:8s} {s['all_checks_pass_pct']:5.1f}% all-pass (floor {floor}%) "
              f"{'PASS' if passed else 'FAIL'}  p50 {s['p50_seconds']}s  "
              f"{'$' + str(s['cost_per_1k_tasks_usd']) + '/1k' if 'cost_per_1k_tasks_usd' in s else ''}")
        for check, pct in s["check_pass_pct"].items():
            print(f"    {check:22s} {pct:5.1f}%")
        if s["failures"]:
            print("    failed:", ", ".join(f"{k} ({'/'.join(v)})" for k, v in s["failures"].items()))
        results[name] = res
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{model or 'model'}.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
