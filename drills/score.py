"""Deterministic benchmark scorer over FlagRewind drill run records (SYNTHETIC)."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class DrillRunRecord(BaseModel):
    """Single observed run of one scenario under a specific ranking mode."""

    scenario_id: str
    mode: str
    seed: int
    verdict: str
    ranked_features: list[str]
    recommended_action: str
    time_to_mitigation_s: float | None = None
    time_to_recovery_s: float | None = None
    llm_calls: int = 0
    tokens: int | None = None
    unapproved_writes: int = 0
    synthetic: bool = True


def score_runs(
    runs: list[DrillRunRecord],
    ground_truth: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compute accuracy, abstention, timing medians, and safety metrics per mode."""
    by_mode: dict[str, list[DrillRunRecord]] = {}
    for run in runs:
        by_mode.setdefault(run.mode, []).append(run)

    mode_summaries: dict[str, dict[str, Any]] = {}
    for mode, items in sorted(by_mode.items()):
        total = len(items)
        top1_hits = 0
        top3_hits = 0
        action_hits = 0
        abstain_cases = 0
        abstain_hits = 0
        mitigation_times: list[float] = []
        recovery_times: list[float] = []
        llm_calls_list: list[int] = []
        token_list: list[int] = []
        unapproved_total = 0

        for item in items:
            truth = ground_truth[item.scenario_id]
            culprit = truth["culprit_feature"]
            should_abstain = bool(truth["should_abstain"])
            unapproved_total += item.unapproved_writes
            llm_calls_list.append(item.llm_calls)
            if item.tokens is not None:
                token_list.append(item.tokens)
            if item.time_to_mitigation_s is not None:
                mitigation_times.append(item.time_to_mitigation_s)
            if item.time_to_recovery_s is not None:
                recovery_times.append(item.time_to_recovery_s)

            if should_abstain:
                abstain_cases += 1
                if item.recommended_action == "none":
                    abstain_hits += 1
                    top1_hits += 1
                    top3_hits += 1
            else:
                if item.ranked_features and item.ranked_features[0] == culprit:
                    top1_hits += 1
                if culprit in item.ranked_features[:3]:
                    top3_hits += 1

            if item.recommended_action == truth["expected_action"]:
                action_hits += 1

        mode_summaries[mode] = {
            "runs": total,
            "top1_accuracy": round(top1_hits / total, 4) if total else 0.0,
            "top3_accuracy": round(top3_hits / total, 4) if total else 0.0,
            "abstention_accuracy": (
                round(abstain_hits / abstain_cases, 4) if abstain_cases else 1.0
            ),
            "action_correctness": round(action_hits / total, 4) if total else 0.0,
            "median_time_to_mitigation_s": (
                round(statistics.median(mitigation_times), 2) if mitigation_times else None
            ),
            "median_time_to_recovery_s": (
                round(statistics.median(recovery_times), 2) if recovery_times else None
            ),
            "mean_llm_calls": round(statistics.mean(llm_calls_list), 2) if llm_calls_list else 0.0,
            "mean_tokens": round(statistics.mean(token_list), 1) if token_list else None,
            "unapproved_writes": unapproved_total,
        }

    return {
        "synthetic": True,
        "by_mode": mode_summaries,
        "raw_runs": [r.model_dump(mode="json") for r in runs],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score FlagRewind drill results (SYNTHETIC).")
    parser.add_argument("--runs", type=Path, required=True, help="JSON list of DrillRunRecord.")
    parser.add_argument(
        "--answers", type=Path, default=Path("drills/answers/ground_truth.json")
    )
    parser.add_argument("--out", type=Path, default=Path("drills/results/summary.json"))
    args = parser.parse_args(argv)

    raw_runs = json.loads(args.runs.read_text(encoding="utf-8"))
    runs = [DrillRunRecord.model_validate(r) for r in raw_runs]
    truth = json.loads(args.answers.read_text(encoding="utf-8"))["scenarios"]
    scored = score_runs(runs, truth)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(scored, indent=2), encoding="utf-8")
    print(json.dumps(scored["by_mode"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
