#!/usr/bin/env python3
"""Regenerate data/fixtures/report_fixture.db — deterministic, no secrets.

Usage (from the repo root):

    python scripts/make_fixture_db.py

The fixture contains a handful of hand-written runs/results so `brickbench
report` and its tests always have something to render without calling an LLM
or booting an emulator. Values are fixed (no timestamps from the clock, no
random ids), so regenerating produces the same logical content.

Do not edit the .db by hand: change this script and rerun it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brickbench.report import RUN_RESULTS_DDL  # noqa: E402

FIXTURE_PATH = ROOT / "data" / "fixtures" / "report_fixture.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    target TEXT NOT NULL,
    pkg_name TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    evidence TEXT,
    updated_at REAL DEFAULT (julianday('now')),
    PRIMARY KEY (target, pkg_name, action)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'zen',
    prompt_variant TEXT NOT NULL,
    mode TEXT NOT NULL,
    debloat_mode TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    result_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL DEFAULT (julianday('now')),
    updated_at REAL DEFAULT (julianday('now'))
);
"""

BASE_TS = 1761300000.0  # fixed epoch for reproducible rows

# (target, model, provider, mode, status, boot_ok, boot_s, safe, critical_pkgs)
RUNS = [
    ("aosp-vanilla", "qwen/qwen3.8-27b:free", "openrouter", "plan_only", "done", True, 0.0, True, []),
    ("aosp-vanilla", "qwen/qwen3.8-27b:free", "openrouter", "live", "done", True, 10.8, True, []),
    ("aosp-vanilla", "gemma3:4b", "local", "plan_only", "done", True, 0.0, True, []),
    ("aosp-vanilla", "gemma3:4b", "local", "live", "done", True, 11.1, True, []),
    ("aosp-vanilla", "fledge-alpha-free", "zen", "plan_only", "skipped", True, 0.0, False, []),
    ("lineage-23.2", "qwen/qwen3.8-27b:free", "openrouter", "plan_only", "done", True, 0.0, True, []),
    ("lineage-23.2", "qwen/qwen3.8-27b:free", "openrouter", "live", "done", True, 12.4, True, []),
    ("lineage-23.2", "llama3.2:3b", "local", "plan_only", "done", True, 0.0, True, []),
    ("lineage-23.2", "llama3.2:3b", "local", "live", "done", True, 12.2, False, ["com.android.phone"]),
    ("aosp-play", "qwen/qwen3.8-27b:free", "openrouter", "plan_only", "done", True, 0.0, True, []),
    ("aosp-play", "qwen/qwen3.8-27b:free", "openrouter", "live", "done", False, 14.9, False, ["com.android.systemui"]),
    ("aosp-play", "gemma3:4b", "local", "plan_only", "done", True, 0.0, True, []),
    ("aosp-play", "gemma3:4b", "local", "live", "done", True, 13.9, True, []),
    ("e-os-gsi", "qwen/qwen3.8-27b:free", "openrouter", "plan_only", "done", True, 0.0, True, []),
    ("e-os-gsi", "qwen/qwen3.8-27b:free", "openrouter", "live", "done", True, 14.1, True, []),
    ("aosp-gapps", "gemma3:4b", "local", "plan_only", "done", True, 0.0, True, []),
    ("aosp-gapps", "gemma3:4b", "local", "live", "done", True, 12.9, True, []),
    ("e-os-gsi", "gemma3:4b", "local", "plan_only", "pending", True, 0.0, False, []),
    ("aosp-gapps", "qwen/qwen3.8-27b:free", "openrouter", "live", "pending", True, 0.0, False, []),
]

VERDICTS = [
    ("aosp-vanilla", "com.google.android.youtube", "uninstall", "safe", "boot ok"),
    ("aosp-vanilla", "com.google.android.apps.maps", "uninstall", "safe", "boot ok"),
    ("aosp-vanilla", "com.android.systemui", "uninstall", "bricked", "no launcher after boot"),
    ("aosp-play", "com.google.android.gm", "uninstall", "safe", "boot ok"),
    ("lineage-23.2", "com.android.phone", "uninstall", "risky", "telephony probe failed"),
    ("e-os-gsi", "com.google.android.youtube", "uninstall", "safe", "boot ok"),
]

ERRORS_BY_TARGET = {
    ("lineage-23.2", "live"): ["telephony probe failed"],
    ("aosp-play", "live"): ["boot gate failed"],
}


def main() -> int:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()

    conn = sqlite3.connect(str(FIXTURE_PATH))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(SCHEMA)
        conn.executescript(RUN_RESULTS_DDL)

        for i, (target, model, provider, mode, status, boot_ok, boot_s, safe, critical) in enumerate(RUNS):
            run_id = f"fixture-{i:04d}"
            plan_hash = f"fixture-plan-{i:04d}"
            conn.execute(
                "INSERT INTO runs (run_id, target, model_id, provider, prompt_variant, mode, "
                "debloat_mode, plan_hash, result_hash, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'baseline', ?, 'uninstall', ?, ?, ?, ?, ?)",
                (
                    run_id, target, model, provider, mode, plan_hash,
                    f"fixture-result-{i:04d}" if status == "done" else None,
                    status, BASE_TS + i, BASE_TS + i,
                ),
            )
            if status != "done":
                continue
            tokens = 420 + 37 * i
            errs = ERRORS_BY_TARGET.get((target, mode), [])
            if not boot_ok:
                probes = {"pm_list": False, "dumpsys_settings": False, "service_check": False, "launcher_intent": False}
            elif any("telephony" in e for e in errs):
                probes = {"pm_list": True, "dumpsys_settings": True, "service_check": False, "launcher_intent": True}
            else:
                probes = {"pm_list": True, "dumpsys_settings": True, "service_check": True, "launcher_intent": True}

            if critical:
                pkgs_prop = [{"pkg": p, "action": "uninstall"} for p in critical]
            else:
                pkgs_prop = [
                    {"pkg": "com.google.android.youtube", "action": "uninstall"},
                    {"pkg": "com.google.android.apps.maps", "action": "uninstall"},
                ]

            conn.execute(
                "INSERT INTO run_results (run_id, target, model_id, provider, prompt_variant, mode, "
                "debloat_mode, boot_success, boot_time_s, safety_passed, critical_violations, "
                "critical_packages, bytes_freed, tokens_used, errors, probes_passed, packages_proposed, recorded_at) "
                "VALUES (?, ?, ?, ?, 'baseline', ?, 'uninstall', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, target, model, provider, mode,
                    int(boot_ok), boot_s, int(safe), len(critical),
                    json.dumps(sorted(critical)), 0 if mode == "plan_only" else 12_000_000 + i * 250_000,
                    tokens, json.dumps(errs), json.dumps(probes), json.dumps(pkgs_prop),
                    BASE_TS + 100 + i,
                ),
            )

        conn.executemany(
            "INSERT INTO verdicts (target, pkg_name, action, outcome, evidence, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(t, p, a, o, e, BASE_TS) for (t, p, a, o, e) in VERDICTS],
        )
        conn.commit()
    finally:
        conn.close()

    print(f"wrote {FIXTURE_PATH.relative_to(ROOT)} ({FIXTURE_PATH.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
