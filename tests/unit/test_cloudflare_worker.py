"""Unit checks for the read-only Cloudflare Workers showcase generator."""

from __future__ import annotations

import json
import re
from pathlib import Path

from deploy.cloudflare.build_showcase import collect_showcase_data, render_worker_js


def test_showcase_data_matches_real_results_and_constraints() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    data = collect_showcase_data(repo_root)

    assert len(data["scenarios"]) == 14
    assert data["ablation"]["llm_role"] == "advisory_only"
    assert data["ablation"]["heldout_tag"] == "drills-heldout-v1"
    llm_stats = data["ablation"]["by_mode"]["llm (gemini-flash-lite-latest)"]
    assert llm_stats["top1_accuracy"] == 1.0
    assert llm_stats["action_correctness"] == 0.7857
    assert data["proof"]["alert_to_revert_applied_s_automated"] == 31.859
    assert data["proof"]["unapproved_writes"] == 0

    b1 = [r for r in data["run_ledger"] if "Manual UI Baseline" in r["name"]]
    assert len(b1) == 1
    assert b1[0]["sample_size"] == "n=3 target"
    assert "Pending operator run" in b1[0]["result"]


def test_rendered_worker_js_has_no_secrets_and_required_disclosures() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    js_src = render_worker_js(repo_root)

    # Zero secret tokens or leaked key prefixes
    assert not re.search(r"ser\.[A-Za-z0-9]{6,}", js_src)
    assert not re.search(r"AIza[0-9A-Za-z_-]{10,}", js_src)
    assert "N2BfGVTL" not in js_src
    assert "MnTVYxxg" not in js_src

    # Honest labels and security headers
    assert "SYNTHETIC" in js_src
    assert "advisory only" in js_src
    assert "drills-heldout-v1" in js_src
    assert "local run; GitHub Actions blocked by an account billing hold" in js_src
    assert "X-Content-Type-Options" in js_src
    assert "Math.random" not in js_src

    # Generated JSON embeds cleanly without invalid escapes
    m = re.search(r"const SHOWCASE_DATA = (\{.*?\});\n", js_src)
    assert m is not None
    parsed = json.loads(m.group(1))
    assert len(parsed["scenarios"]) == 14
