"""brickbench.report — query layer + standalone HTML/JSON rendering.

The database is the single source of truth. `collect_report()` turns the
`runs` / `run_results` / `verdicts` tables into one plain dict; `render_html()`
and `render_json()` only format that dict, so both outputs always agree.

Keeping the query layer free of Click/HTML imports also makes it usable from
tests, scripts, and the CI workflow without a terminal.
"""

from __future__ import annotations

import html as _html
import json as _json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote as _quote

from brickbench.config import __version__

__all__ = [
    "RUN_RESULTS_DDL",
    "collect_report",
    "record_run_result",
    "render_html",
    "render_json",
    "render_pdf",
]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Per-run metrics are kept in their own table so `runs` stays a job queue and
# historical rows survive re-enqueues. Written by runner.record_run_result().
RUN_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS run_results (
    run_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'zen',
    prompt_variant TEXT NOT NULL DEFAULT 'baseline',
    mode TEXT NOT NULL DEFAULT 'plan_only',
    debloat_mode TEXT NOT NULL DEFAULT 'uninstall',
    boot_success INTEGER NOT NULL DEFAULT 0,
    boot_time_s REAL NOT NULL DEFAULT 0,
    safety_passed INTEGER NOT NULL DEFAULT 0,
    critical_violations INTEGER NOT NULL DEFAULT 0,
    critical_packages TEXT NOT NULL DEFAULT '[]',
    bytes_freed INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]',
    probes_passed TEXT NOT NULL DEFAULT '{}',
    packages_proposed TEXT NOT NULL DEFAULT '[]',
    recorded_at REAL
)
"""

MAX_FAILURES = 100


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _table_names(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _json_list(raw: Any) -> List[str]:
    """Parse a JSON list column defensively (old/partial rows may be blank)."""
    if raw in (None, ""):
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    try:
        value = _json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


def _json_dict(raw: Any) -> Dict[str, Any]:
    """Parse a JSON dict column defensively."""
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = _json.loads(raw)
        if isinstance(val, dict):
            return val
    except Exception:
        pass
    return {}


def _json_list_of_dicts(raw: Any) -> List[Dict[str, Any]]:
    """Parse a list of dicts defensively."""
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return [x if isinstance(x, dict) else {"pkg": str(x)} for x in raw]
    try:
        val = _json.loads(raw)
        if isinstance(val, list):
            return [x if isinstance(x, dict) else {"pkg": str(x)} for x in val]
    except Exception:
        pass
    return []


def _pct(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _fmt_int(value: int) -> str:
    return f"{int(value):,}"


def _fmt_bytes(bytes_count: int) -> str:
    if bytes_count <= 0:
        return "0 B"
    if bytes_count >= 1_048_576:
        return f"{bytes_count / 1_048_576:.1f} MB"
    if bytes_count >= 1024:
        return f"{bytes_count / 1024:.1f} KB"
    return f"{bytes_count:,} B"


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}s"


def _fmt_delta(value: Optional[float], inline_badge: bool = True) -> str:
    """Format boot delta with unambiguous speedup/regression glyphs and qualitative labels."""
    if value is None:
        return '<span class="delta-pill na">n/a</span>' if inline_badge else "n/a"
    sign = "+" if value > 0 else ""
    raw = f"{sign}{value:.2f}s"
    if not inline_badge:
        return raw

    if value <= -0.05:
        # Faster boot (speedup)
        return f'<span class="delta-pill faster" title="Boot speedup vs baseline">▼ {raw} (faster)</span>'
    elif value >= 0.05:
        # Slower boot (regression)
        return f'<span class="delta-pill slower" title="Boot regression vs baseline">▲ {raw} (slower)</span>'
    else:
        # On par
        return f'<span class="delta-pill onpar" title="Parity with baseline">■ {raw} (on-par)</span>'


def _infer_probes(probes: Dict[str, Any], errors: Sequence[str], boot_success: bool, mode: str) -> Dict[str, bool]:
    """Ensure every row has structured probe health status."""
    if probes and any(k in probes for k in ("pm_list", "dumpsys_settings", "service_check", "launcher_intent")):
        return {
            "pm_list": bool(probes.get("pm_list", True)),
            "dumpsys_settings": bool(probes.get("dumpsys_settings", True)),
            "service_check": bool(probes.get("service_check", True)),
            "launcher_intent": bool(probes.get("launcher_intent", True)),
        }
    errs = [str(e).lower() for e in errors]
    if mode == "live" and not boot_success:
        return {
            "pm_list": False,
            "dumpsys_settings": False,
            "service_check": False,
            "launcher_intent": False,
        }
    return {
        "pm_list": not any("pm list" in e for e in errs),
        "dumpsys_settings": not any("dumpsys" in e for e in errs),
        "service_check": not any("service" in e or "telephony" in e for e in errs),
        "launcher_intent": not any("launcher" in e for e in errs),
    }


def _migrate_run_results(conn: sqlite3.Connection) -> None:
    """Safely migrate legacy run_results tables to include telemetry columns."""
    conn.execute(RUN_RESULTS_DDL)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(run_results)").fetchall()}
        if "probes_passed" not in cols:
            conn.execute("ALTER TABLE run_results ADD COLUMN probes_passed TEXT NOT NULL DEFAULT '{}'")
        if "packages_proposed" not in cols:
            conn.execute("ALTER TABLE run_results ADD COLUMN packages_proposed TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# Persistence (used by the runner)
# ---------------------------------------------------------------------------

def record_run_result(conn: sqlite3.Connection, *, run: Dict[str, Any], result: Any) -> None:
    """Persist one VerificationResult, keyed by run_id (last write wins).

    `result` is duck-typed so this module does not import the evaluator.
    Recording does not change scoring; it only stores what verify_plan returned.
    """
    _migrate_run_results(conn)
    probes_raw = getattr(result, "probes_passed", {}) or {}
    probes_json = _json.dumps(probes_raw) if isinstance(probes_raw, dict) else str(probes_raw)
    pkgs_raw = getattr(result, "packages_proposed", []) or []
    pkgs_json = _json.dumps(pkgs_raw) if isinstance(pkgs_raw, list) else str(pkgs_raw)

    conn.execute(
        """
        INSERT INTO run_results (
            run_id, target, model_id, provider, prompt_variant, mode, debloat_mode,
            boot_success, boot_time_s, safety_passed, critical_violations,
            critical_packages, bytes_freed, tokens_used, errors, probes_passed,
            packages_proposed, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            target=excluded.target,
            model_id=excluded.model_id,
            provider=excluded.provider,
            prompt_variant=excluded.prompt_variant,
            mode=excluded.mode,
            debloat_mode=excluded.debloat_mode,
            boot_success=excluded.boot_success,
            boot_time_s=excluded.boot_time_s,
            safety_passed=excluded.safety_passed,
            critical_violations=excluded.critical_violations,
            critical_packages=excluded.critical_packages,
            bytes_freed=excluded.bytes_freed,
            tokens_used=excluded.tokens_used,
            errors=excluded.errors,
            probes_passed=excluded.probes_passed,
            packages_proposed=excluded.packages_proposed,
            recorded_at=excluded.recorded_at
        """,
        (
            run["run_id"],
            run.get("target", ""),
            run.get("model_id", ""),
            run.get("provider", "zen"),
            run.get("prompt_variant", "baseline"),
            run.get("mode", "plan_only"),
            run.get("debloat_mode", "uninstall"),
            int(bool(getattr(result, "boot_success", False))),
            float(getattr(result, "boot_time_s", 0.0) or 0.0),
            int(bool(getattr(result, "safety_passed", False))),
            len(list(getattr(result, "critical_violations", []) or [])),
            _json.dumps(sorted(str(p) for p in (getattr(result, "critical_violations", []) or []))),
            int(getattr(result, "bytes_freed", 0) or 0),
            int(getattr(result, "tokens_used", 0) or 0),
            _json.dumps(list(getattr(result, "errors", []) or [])),
            probes_json,
            pkgs_json,
            time.time(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Query layer
# ---------------------------------------------------------------------------

def _aggregate(rows: Iterable[Dict[str, Any]], baselines: Dict[str, float]) -> Dict[str, Any]:
    """Aggregate already-normalised result rows."""
    rows = list(rows)
    passed = sum(1 for r in rows if r["safety_passed"])
    critical = sum(1 for r in rows if r["critical_violations"] > 0)
    failed = sum(1 for r in rows if not r["safety_passed"])
    tokens = sum(int(r["tokens_used"]) for r in rows)
    bytes_freed = sum(int(r["bytes_freed"]) for r in rows)

    live = [r for r in rows if r["mode"] == "live"]
    boot_ok = [r for r in live if r["boot_success"] and r["boot_time_s"] > 0]
    deltas = [
        r["boot_time_s"] - baselines[r["target"]]
        for r in boot_ok
        if r["target"] in baselines
    ]

    # Subsystem probe failure counts
    probe_pm = sum(1 for r in rows if r.get("probes_passed", {}).get("pm_list", True))
    probe_settings = sum(1 for r in rows if r.get("probes_passed", {}).get("dumpsys_settings", True))
    probe_service = sum(1 for r in rows if r.get("probes_passed", {}).get("service_check", True))
    probe_launcher = sum(1 for r in rows if r.get("probes_passed", {}).get("launcher_intent", True))

    # Safety breakdown tiers
    probe_regressions = sum(1 for r in rows if (not r["safety_passed"]) and r["critical_violations"] == 0)
    speedups = sum(1 for d in deltas if d <= -0.05)
    regressions = sum(1 for d in deltas if d >= 0.05)
    on_par = sum(1 for d in deltas if -0.05 < d < 0.05)

    return {
        "results": len(rows),
        "safety_passed": passed,
        "safety_pass_pct": _pct(passed, len(rows)),
        "safety_failures": failed,
        "critical_failures": critical,
        "tokens_total": tokens,
        "tokens_mean": _mean([r["tokens_used"] for r in rows]),
        "bytes_freed_total": bytes_freed,
        "bytes_freed_mean": _mean([r["bytes_freed"] for r in rows if r["bytes_freed"] > 0]),
        "live_results": len(live),
        "boot_measured": len(boot_ok),
        "boot_success_pct": _pct(len(boot_ok), len(live)),
        "mean_boot_time_s": _mean([r["boot_time_s"] for r in boot_ok]),
        "boot_delta_s": _mean(deltas),
        "boot_speedups": speedups,
        "boot_regressions": regressions,
        "boot_on_par": on_par,
        "probe_regressions": probe_regressions,
        "probe_regression_pct": _pct(probe_regressions, len(rows)),
        "critical_pct": _pct(critical, len(rows)),
        "probes": {
            "pm_list": {"passed": probe_pm, "total": len(rows), "pct": _pct(probe_pm, len(rows))},
            "dumpsys_settings": {"passed": probe_settings, "total": len(rows), "pct": _pct(probe_settings, len(rows))},
            "service_check": {"passed": probe_service, "total": len(rows), "pct": _pct(probe_service, len(rows))},
            "launcher_intent": {"passed": probe_launcher, "total": len(rows), "pct": _pct(probe_launcher, len(rows))},
        },
    }


def collect_report(db_path: str | Path) -> Dict[str, Any]:
    """Read a brickbench DB and return the canonical report dict.

    Raises
    ------
    FileNotFoundError
        When the database path does not exist.
    ValueError
        When the file is not a brickbench database (no `runs` table).
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"no brickbench database at {path}")
    if path.is_dir():
        raise FileNotFoundError(f"{path} is a directory, not a database")

    uri = f"file:{_quote(path.resolve().as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = _table_names(conn)
        if "runs" not in tables:
            raise ValueError(f"{path} is not a brickbench database (missing `runs` table)")

        runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at, run_id")]
        results: Dict[str, Dict[str, Any]] = {}

        cols = {r[1] for r in conn.execute("PRAGMA table_info(run_results)").fetchall()} if "run_results" in tables else set()
        has_probes = "probes_passed" in cols
        has_pkgs = "packages_proposed" in cols

        if "run_results" in tables:
            for row in conn.execute("SELECT * FROM run_results"):
                item = dict(row)
                item["safety_passed"] = bool(item.get("safety_passed"))
                item["boot_success"] = bool(item.get("boot_success"))
                item["critical_violations"] = int(item.get("critical_violations") or 0)
                item["critical_packages"] = _json_list(item.get("critical_packages"))
                item["errors"] = _json_list(item.get("errors"))
                item["boot_time_s"] = float(item.get("boot_time_s") or 0.0)
                item["tokens_used"] = int(item.get("tokens_used") or 0)
                item["bytes_freed"] = int(item.get("bytes_freed") or 0)
                probes_raw = _json_dict(item.get("probes_passed")) if has_probes else {}
                item["probes_passed"] = _infer_probes(
                    probes_raw,
                    item["errors"],
                    item["boot_success"],
                    item.get("mode", "plan_only"),
                )
                item["packages_proposed"] = _json_list_of_dicts(item.get("packages_proposed")) if has_pkgs else []
                results[item["run_id"]] = item

        verdict_outcomes: Counter = Counter()
        verdicts_list: List[Dict[str, Any]] = []
        if "verdicts" in tables:
            for row in conn.execute("SELECT outcome, count(*) AS n FROM verdicts GROUP BY outcome"):
                verdict_outcomes[str(row["outcome"])] = int(row["n"])
            for row in conn.execute("SELECT target, pkg_name, action, outcome, evidence FROM verdicts"):
                verdicts_list.append(dict(row))
    finally:
        conn.close()

    # Join results onto runs; results whose run row is gone still count as data.
    rows: List[Dict[str, Any]] = []
    seen = set()
    for run in runs:
        res = results.get(run["run_id"])
        if res is None:
            continue
        seen.add(run["run_id"])
        rows.append({
            "run_id": run["run_id"],
            "target": res.get("target") or run.get("target", ""),
            "model_id": res.get("model_id") or run.get("model_id", ""),
            "provider": res.get("provider") or run.get("provider", ""),
            "prompt_variant": res.get("prompt_variant") or run.get("prompt_variant", ""),
            "mode": res.get("mode") or run.get("mode", ""),
            "debloat_mode": res.get("debloat_mode") or run.get("debloat_mode", ""),
            "boot_success": bool(res["boot_success"]),
            "boot_time_s": float(res["boot_time_s"]),
            "safety_passed": bool(res["safety_passed"]),
            "critical_violations": int(res["critical_violations"]),
            "critical_packages": list(res["critical_packages"]),
            "bytes_freed": int(res["bytes_freed"]),
            "tokens_used": int(res["tokens_used"]),
            "errors": list(res["errors"]),
            "probes_passed": dict(res.get("probes_passed", {})),
            "packages_proposed": list(res.get("packages_proposed", [])),
        })
    for run_id, res in results.items():
        if run_id in seen:
            continue
        rows.append({
            "run_id": run_id,
            "target": res.get("target", ""),
            "model_id": res.get("model_id", ""),
            "provider": res.get("provider", ""),
            "prompt_variant": res.get("prompt_variant", ""),
            "mode": res.get("mode", ""),
            "debloat_mode": res.get("debloat_mode", ""),
            "boot_success": bool(res["boot_success"]),
            "boot_time_s": float(res["boot_time_s"]),
            "safety_passed": bool(res["safety_passed"]),
            "critical_violations": int(res["critical_violations"]),
            "critical_packages": list(res["critical_packages"]),
            "bytes_freed": int(res["bytes_freed"]),
            "tokens_used": int(res["tokens_used"]),
            "errors": list(res["errors"]),
            "probes_passed": dict(res.get("probes_passed", {})),
            "packages_proposed": list(res.get("packages_proposed", [])),
        })

    # Boot baseline per target: fastest successful live boot observed for that
    # target. Delta = run boot time - baseline; negative means faster.
    baselines: Dict[str, float] = {}
    for r in rows:
        if r["mode"] == "live" and r["boot_success"] and r["boot_time_s"] > 0:
            current = baselines.get(r["target"])
            if current is None or r["boot_time_s"] < current:
                baselines[r["target"]] = r["boot_time_s"]

    summary = _aggregate(rows, baselines)
    summary["runs"] = len(runs)
    summary["runs_by_status"] = dict(sorted(Counter(str(r.get("status", "unknown")) for r in runs).items()))

    by_target: Dict[str, Dict[str, Any]] = {}
    for key in sorted({r["target"] for r in runs} | {r["target"] for r in rows}):
        subset = [r for r in rows if r["target"] == key]
        job_count = sum(1 for r in runs if r.get("target") == key)
        agg = _aggregate(subset, baselines)
        agg["target"] = key
        agg["runs"] = job_count
        agg["baseline_boot_s"] = baselines.get(key)
        by_target[key] = agg

    by_model: Dict[str, Dict[str, Any]] = {}
    for key in sorted({(r["model_id"], r["provider"]) for r in rows}):
        model_id, provider = key
        subset = [r for r in rows if r["model_id"] == model_id and r["provider"] == provider]
        agg = _aggregate(subset, baselines)
        agg["model_id"] = model_id
        agg["provider"] = provider
        by_model[f"{provider}/{model_id}"] = agg

    failures = [
        {
            "run_id": r["run_id"],
            "target": r["target"],
            "model_id": r["model_id"],
            "provider": r["provider"],
            "mode": r["mode"],
            "boot_success": r["boot_success"],
            "boot_time_s": r["boot_time_s"],
            "safety_passed": r["safety_passed"],
            "critical_packages": r["critical_packages"],
            "errors": r["errors"],
            "probes_passed": r["probes_passed"],
        }
        for r in rows
        if (not r["safety_passed"]) or (not r["boot_success"] and r["mode"] == "live")
        or r["critical_violations"] > 0
    ]
    failures.sort(key=lambda r: (r["target"], r["model_id"], r["run_id"]))

    # Aggregate package frequency & hallucination stats across runs & verdicts
    package_stats: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        for p in r.get("critical_packages", []):
            st = package_stats.setdefault(p, {
                "pkg": p, "proposals": 0, "outcomes": Counter(),
                "targets": set(), "evidence": "Soft-brick risk: removed critical system framework package",
            })
            st["proposals"] += 1
            st["outcomes"]["bricked"] += 1
            st["targets"].add(r.get("target"))

        for prop in r.get("packages_proposed", []):
            p = prop.get("pkg") if isinstance(prop, dict) else str(prop)
            if not p:
                continue
            st = package_stats.setdefault(p, {
                "pkg": p, "proposals": 0, "outcomes": Counter(),
                "targets": set(), "evidence": "",
            })
            st["proposals"] += 1
            outcome = "bricked" if p in r.get("critical_packages", []) else ("safe" if r["safety_passed"] else "risky")
            st["outcomes"][outcome] += 1
            st["targets"].add(r.get("target"))

    for v in verdicts_list:
        p = v["pkg_name"]
        st = package_stats.setdefault(p, {
            "pkg": p, "proposals": 0, "outcomes": Counter(),
            "targets": set(), "evidence": "",
        })
        st["targets"].add(v["target"])
        st["outcomes"][v["outcome"]] += 1
        if v.get("evidence") and not st.get("evidence"):
            st["evidence"] = v["evidence"]

    top_packages: List[Dict[str, Any]] = []
    for p, st in sorted(package_stats.items(), key=lambda kv: (-kv[1]["proposals"], -kv[1]["outcomes"].get("bricked", 0), kv[0])):
        outcomes_cnt = st["outcomes"]
        if outcomes_cnt.get("bricked", 0) > 0:
            dominant = "bricked"
        elif outcomes_cnt.get("risky", 0) > 0:
            dominant = "risky"
        else:
            dominant = "safe"

        evidence = st.get("evidence")
        if not evidence:
            evidence = "Verified clean removal" if dominant == "safe" else "Functional probe regression detected"

        top_packages.append({
            "pkg": p,
            "proposals": max(1, st["proposals"]),
            "targets": sorted(list(st["targets"])),
            "outcome": dominant,
            "evidence": evidence,
        })

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool_version": __version__,
        "database": path.name,
        "run_results_table": "run_results" in tables,
        "summary": summary,
        "targets": list(by_target.values()),
        "models": list(by_model.values()),
        "failures": failures[:MAX_FAILURES],
        "failures_total": len(failures),
        "top_packages": top_packages[:20],
        "verdicts": dict(sorted(verdict_outcomes.items())),
    }


# ---------------------------------------------------------------------------
# Rendering Engine
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #090d13; --panel: #111722; --panel-2: #172030; --panel-3: #1e2a3e;
  --ink: #f0f6fc; --ink-dim: #cbd5e1; --muted: #8b99aa; --line: #222d3d;
  --line-strong: #33445c; --accent: #5eead4; --good: #34d399; --bad: #f87171;
  --warn: #fbbf24; --good-dim: rgba(52, 211, 153, 0.14); --bad-dim: rgba(248, 113, 113, 0.14);
  --warn-dim: rgba(251, 191, 36, 0.14); --accent-dim: rgba(94, 234, 212, 0.12);
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 24px 64px; background: var(--bg); color: var(--ink);
  font: 14.5px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  letter-spacing: -0.01em;
}
main, header, footer { max-width: 1240px; margin: 0 auto; }
header { padding: 32px 0 20px; border-bottom: 1px solid var(--line); }
.header-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; flex-wrap: wrap; }
h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.03em; color: var(--ink); }
h1 span { color: var(--accent); }
.tagline { margin: 6px 0 0; color: var(--muted); font-size: 13.5px; }
.header-actions { display: flex; align-items: center; gap: 10px; }
.btn-pdf {
  display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px;
  background: var(--panel-2); color: var(--ink); border: 1px solid var(--line);
  border-radius: 8px; text-decoration: none; font-size: 12.5px; font-weight: 500;
  transition: all 0.15s;
}
.btn-pdf:hover { border-color: var(--accent); color: var(--accent); background: var(--panel-3); }
.meta-strip {
  display: flex; flex-wrap: wrap; gap: 16px; margin-top: 14px; padding-top: 12px;
  border-top: 1px dashed var(--line); font-size: 12px; color: var(--muted);
}
.meta-strip span { display: inline-flex; align-items: center; gap: 5px; }
.meta-strip strong { color: var(--ink); }
.meta-strip code { color: var(--accent); }

/* Vitals Cards */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-top: 22px; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 18px 20px; position: relative; overflow: hidden;
}
.card .label { color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
.card strong {
  display: block; font-size: 30px; margin: 8px 0 4px; font-variant-numeric: tabular-nums;
  font-weight: 700; letter-spacing: -0.02em;
}
.card .meta { color: var(--muted); font-size: 12px; line-height: 1.4; }
.card.good { border-top: 3px solid var(--good); }
.card.good strong { color: var(--good); }
.card.bad { border-top: 3px solid var(--bad); }
.card.bad strong { color: var(--bad); }
.card.warn { border-top: 3px solid var(--warn); }
.card.warn strong { color: var(--warn); }

/* Directional delta pills */
.delta-pill {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
  border-radius: 6px; font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums;
}
.delta-pill.faster { background: var(--good-dim); color: var(--good); border: 1px solid rgba(52, 211, 153, 0.3); }
.delta-pill.slower { background: var(--bad-dim); color: var(--bad); border: 1px solid rgba(248, 113, 113, 0.3); }
.delta-pill.onpar { background: var(--panel-2); color: var(--muted); border: 1px solid var(--line); }
.delta-pill.na { color: var(--muted); }

/* Callout banner */
.callout {
  background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--accent);
  border-radius: 10px; padding: 14px 18px; margin-top: 20px; font-size: 13.5px; line-height: 1.5;
}
.callout.warn { border-left-color: var(--bad); background: rgba(248, 113, 113, 0.04); }
.callout strong { color: var(--ink); }

/* Section layouts */
h2 {
  font-size: 15px; margin: 36px 0 14px; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--muted); font-weight: 700; display: flex; align-items: center; gap: 8px;
}
h2::after { content: ""; flex: 1; height: 1px; background: var(--line); }

.charts-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(540px, 1fr)); gap: 18px; margin-top: 14px; }
.chart-card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 20px; display: flex; flex-direction: column;
}
.chart-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 14px; }
.chart-title { font-size: 14px; font-weight: 600; color: var(--ink); }
.chart-sub { font-size: 12px; color: var(--muted); }
.chart-legend { display: flex; gap: 14px; font-size: 11.5px; color: var(--muted); margin-bottom: 12px; flex-wrap: wrap; }
.legend-item { display: inline-flex; align-items: center; gap: 5px; }
.legend-dot { width: 10px; height: 10px; border-radius: 2px; }
.svg-chart { width: 100%; height: auto; overflow: visible; }

/* Subsystem Diagnostics Spectrum */
.spectrum-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-top: 14px; }
.spectrum-card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px; display: flex; gap: 14px; align-items: center;
}
.spectrum-info { flex: 1; min-width: 0; }
.spectrum-title { font-size: 13.5px; font-weight: 600; color: var(--ink); margin: 0 0 2px; }
.spectrum-probe { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: var(--accent); }
.spectrum-desc { font-size: 11.5px; color: var(--muted); margin-top: 4px; line-height: 1.35; }
.spectrum-stat { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }
.spectrum-stat.ok { color: var(--good); }
.spectrum-stat.bad { color: var(--bad); }

/* Tables */
table {
  width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; overflow: hidden; margin-top: 8px; font-size: 13px;
}
th, td {
  padding: 10px 14px; text-align: right; border-bottom: 1px solid var(--line);
  font-variant-numeric: tabular-nums; vertical-align: middle;
}
th:first-child, td:first-child { text-align: left; }
th {
  background: var(--panel-2); color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.06em; font-weight: 600;
}
tr:last-child td { border-bottom: 0; }
tr:hover td { background: var(--panel-2); }
td.ok { color: var(--good); font-weight: 600; }
td.bad { color: var(--bad); font-weight: 600; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
  color: var(--accent); background: rgba(94, 234, 212, 0.08); padding: 2px 6px; border-radius: 4px;
}
.empty { color: var(--muted); background: var(--panel); border: 1px dashed var(--line); border-radius: 10px; padding: 20px; text-align: center; }

/* Micro visual bar inside tables */
.meter-cell { display: inline-flex; align-items: center; gap: 8px; width: 100%; justify-content: flex-end; }
.micro-bar { width: 54px; height: 6px; background: var(--panel-3); border-radius: 3px; overflow: hidden; display: inline-block; }
.micro-fill { height: 100%; background: var(--good); }
.micro-fill.warn { background: var(--warn); }
.micro-fill.bad { background: var(--bad); }

/* Status pills */
.badge {
  display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
}
.badge-safe { background: var(--good-dim); color: var(--good); border: 1px solid rgba(52, 211, 153, 0.25); }
.badge-risky { background: var(--warn-dim); color: var(--warn); border: 1px solid rgba(251, 191, 36, 0.25); }
.badge-bricked { background: var(--bad-dim); color: var(--bad); border: 1px solid rgba(248, 113, 113, 0.25); }

/* Probe status matrix pills */
.probe-strip { display: inline-flex; gap: 4px; }
.probe-pill {
  font-size: 10.5px; padding: 2px 6px; border-radius: 4px; font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.probe-pill.pass { background: var(--good-dim); color: var(--good); }
.probe-pill.fail { background: var(--bad-dim); color: var(--bad); }

/* Collapsible technical reference drawer */
.methodology-drawer {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  margin-top: 48px; overflow: hidden;
}
.methodology-drawer summary {
  padding: 16px 20px; font-size: 14px; font-weight: 600; cursor: pointer;
  background: var(--panel-2); color: var(--ink); list-style: none; display: flex;
  justify-content: space-between; align-items: center; user-select: none;
}
.methodology-drawer summary::-webkit-details-marker { display: none; }
.methodology-drawer summary::after {
  content: "▾"; font-size: 16px; color: var(--muted); transition: transform 0.2s;
}
.methodology-drawer[open] summary::after { transform: rotate(180deg); }
.drawer-content { padding: 20px; border-top: 1px solid var(--line); }
.info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-top: 12px; }
.info-card { background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px; padding: 16px; font-size: 13px; color: var(--muted); line-height: 1.5; }
.info-card h3 { margin: 0 0 6px; font-size: 14px; color: var(--ink); }
.info-card p { margin: 0; }
.card-tag { display: inline-block; font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent); margin-bottom: 6px; font-weight: 600; }
.probe-list { margin: 8px 0 0; padding-left: 18px; }
.probe-list li { margin-bottom: 4px; }
.target-tags { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-top: 12px; }
.target-tag-item { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; font-size: 12px; }
.target-tag-item strong { display: block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--accent); margin-bottom: 2px; }
.target-tag-item span { color: var(--muted); }

footer {
  margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--line);
  color: var(--muted); font-size: 12px; line-height: 1.6;
}

@media (max-width: 768px) {
  body { padding: 0 12px 32px; }
  .charts-grid { grid-template-columns: 1fr; }
  .card strong { font-size: 24px; }
  th, td { padding: 7px 8px; }
}

@media print {
  @page { size: A4; margin: 1.4cm 1.2cm; @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #64748b; } }
  body { background: #ffffff !important; color: #0f172a !important; font-size: 10pt; padding: 0 !important; }
  header { border-bottom: 1px solid #cbd5e1 !important; padding: 12px 0 !important; }
  h1 { color: #0f172a !important; font-size: 18pt !important; }
  h1 span { color: #059669 !important; }
  .sub, .tagline { color: #64748b !important; }
  .btn-pdf, .header-actions { display: none !important; }
  .meta-strip { border-top-color: #cbd5e1 !important; color: #64748b !important; }
  .meta-strip strong { color: #0f172a !important; }
  .callout { background: #f8fafc !important; border: 1px solid #cbd5e1 !important; border-left: 4px solid #059669 !important; color: #1e293b !important; page-break-inside: avoid; break-inside: avoid; }
  .callout.warn { border-left-color: #dc2626 !important; }
  h2 { color: #475569 !important; font-size: 11pt !important; margin: 16px 0 8px !important; }
  .card { background: #f8fafc !important; border: 1px solid #cbd5e1 !important; page-break-inside: avoid; break-inside: avoid; padding: 10px !important; }
  .card .label { color: #64748b !important; }
  .card strong { font-size: 17pt !important; color: #0f172a !important; }
  .card.good strong { color: #059669 !important; }
  .card.bad strong { color: #dc2626 !important; }
  .card.warn strong { color: #d97706 !important; }
  .chart-card, .spectrum-card { background: #ffffff !important; border: 1px solid #cbd5e1 !important; page-break-inside: avoid; break-inside: avoid; }
  .chart-title, .spectrum-title { color: #0f172a !important; }
  table { background: #ffffff !important; border: 1px solid #cbd5e1 !important; page-break-inside: auto; break-inside: auto; }
  tr { page-break-inside: avoid; break-inside: avoid; }
  th { background: #f1f5f9 !important; color: #475569 !important; border-bottom: 1px solid #cbd5e1 !important; }
  td { border-bottom: 1px solid #e2e8f0 !important; color: #0f172a !important; font-size: 8.5pt !important; padding: 5px 7px !important; }
  td.ok { color: #059669 !important; }
  td.bad { color: #dc2626 !important; }
  .delta-pill.faster { background: #ecfdf5 !important; color: #059669 !important; border-color: #059669 !important; }
  .delta-pill.slower { background: #fef2f2 !important; color: #dc2626 !important; border-color: #dc2626 !important; }
  .methodology-drawer { background: #ffffff !important; border: 1px solid #cbd5e1 !important; page-break-inside: avoid; }
  .info-card, .target-tag-item { background: #f8fafc !important; border: 1px solid #cbd5e1 !important; }
  footer { border-top: 1px solid #cbd5e1 !important; color: #64748b !important; page-break-inside: avoid; }
}
"""


def _card(label: str, value: str, meta: str, tone: str = "") -> str:
    cls = f"card {tone}".strip()
    return (
        f'<article class="{cls}"><span class="label">{_html.escape(label)}</span>'
        f"<strong>{_html.escape(value)}</strong>"
        f'<span class="meta">{_html.escape(meta)}</span></article>'
    )


# ---------------------------------------------------------------------------
# SVG Chart Generators (Pure inline SVG, standalone, zero JS, print-compatible)
# ---------------------------------------------------------------------------

def _render_safety_chart(models: List[Dict[str, Any]]) -> str:
    """Render a stacked 100% horizontal bar chart for model safety & risk profiles."""
    if not models:
        return '<p class="empty">No scored model runs available.</p>'

    row_height = 42
    header_height = 20
    svg_height = header_height + len(models) * row_height + 10
    svg_width = 760
    bar_x = 220
    bar_width = 440

    bars_svg = []
    for idx, m in enumerate(models):
        y = header_height + idx * row_height
        total = m["results"]
        safe_cnt = m["safety_passed"]
        critical_cnt = m["critical_failures"]
        probe_cnt = total - safe_cnt - critical_cnt
        if probe_cnt < 0:
            probe_cnt = 0

        model_label = m["model_id"]
        if len(model_label) > 24:
            model_label = model_label[:22] + "…"

        if total > 0:
            w_safe = (safe_cnt / total) * bar_width
            w_probe = (probe_cnt / total) * bar_width
            w_crit = (critical_cnt / total) * bar_width

            x_safe = bar_x
            x_probe = x_safe + w_safe
            x_crit = x_probe + w_probe

            rects = (
                f'<rect x="{x_safe:.1f}" y="{y}" width="{w_safe:.1f}" height="20" rx="3" fill="#34d399" />'
                f'<rect x="{x_probe:.1f}" y="{y}" width="{w_probe:.1f}" height="20" fill="#fbbf24" />'
                f'<rect x="{x_crit:.1f}" y="{y}" width="{w_crit:.1f}" height="20" rx="3" fill="#f87171" />'
            )
            pct_text = f"{_fmt_pct(m['safety_pass_pct'])}"
        else:
            rects = f'<rect x="{bar_x}" y="{y}" width="{bar_width}" height="20" rx="3" fill="#1e2a3e" />'
            pct_text = "no data"

        bars_svg.append(
            f'<g class="chart-row">'
            f'<text x="10" y="{y + 14}" fill="#cbd5e1" font-size="12" font-family="ui-monospace, monospace">{_html.escape(model_label)}</text>'
            f'{rects}'
            f'<text x="{bar_x + bar_width + 12}" y="{y + 14}" fill="#f0f6fc" font-size="12" font-weight="600" font-variant-numeric="tabular-nums">{pct_text}</text>'
            f'</g>'
        )

    return f"""
    <div class="chart-card">
      <div class="chart-header">
        <div>
          <div class="chart-title">Model Safety &amp; Soft-Brick Risk Spectrum</div>
          <div class="chart-sub">Proportional breakdown of verification outcomes per model</div>
        </div>
      </div>
      <div class="chart-legend">
        <span class="legend-item"><span class="legend-dot" style="background:#34d399"></span> Clean Debloat (Probes Passed)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#fbbf24"></span> Probe Failure (Regression)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#f87171"></span> Critical Removal (Soft-Brick)</span>
      </div>
      <svg class="svg-chart" viewBox="0 0 {svg_width} {svg_height}">
        {''.join(bars_svg)}
      </svg>
    </div>
    """


def _render_boot_delta_chart(targets: List[Dict[str, Any]]) -> str:
    """Render a diverging bar chart showing boot latency impact (faster vs slower)."""
    live_targets = [t for t in targets if t.get("boot_delta_s") is not None]
    if not live_targets:
        return """
        <div class="chart-card">
          <div class="chart-header">
            <div>
              <div class="chart-title">Boot Latency Impact (&Delta;s)</div>
              <div class="chart-sub">Diverging benchmark against clean ROM baseline</div>
            </div>
          </div>
          <p class="empty">No dynamic live boot runs measured yet.</p>
        </div>
        """

    row_height = 40
    header_height = 28
    svg_height = header_height + len(live_targets) * row_height + 14
    svg_width = 760
    center_x = 440
    max_arm = 180.0
    scale = 1.5  # max scale in seconds

    rows_svg = []
    for idx, t in enumerate(live_targets):
        y = header_height + idx * row_height
        delta = t["boot_delta_s"]
        target_name = t["target"]

        # Clamp arm width
        arm_len = min(abs(delta) / scale * max_arm, max_arm)

        if delta <= -0.05:
            # Faster (green arm extending left)
            bar_x = center_x - arm_len
            bar_w = arm_len
            color = "#34d399"
            val_text = f"▼ {abs(delta):.2f}s faster"
            text_x = center_x + 12
            anchor = "start"
        elif delta >= 0.05:
            # Slower (red arm extending right)
            bar_x = center_x
            bar_w = arm_len
            color = "#f87171"
            val_text = f"▲ +{delta:.2f}s slower"
            text_x = center_x - 12
            anchor = "end"
        else:
            bar_x = center_x - 2
            bar_w = 4
            color = "#8b99aa"
            val_text = f"■ {delta:+.2f}s on-par"
            text_x = center_x + 12
            anchor = "start"

        rows_svg.append(
            f'<g class="chart-row">'
            f'<text x="10" y="{y + 14}" fill="#cbd5e1" font-size="12" font-family="ui-monospace, monospace">{_html.escape(target_name)}</text>'
            f'<rect x="{bar_x:.1f}" y="{y}" width="{max(bar_w, 2):.1f}" height="18" rx="3" fill="{color}" />'
            f'<text x="{text_x}" y="{y + 14}" fill="{color}" font-size="11.5" font-weight="600" text-anchor="{anchor}" font-variant-numeric="tabular-nums">{val_text}</text>'
            f'</g>'
        )

    axis_svg = (
        f'<line x1="{center_x}" y1="{header_height - 6}" x2="{center_x}" y2="{svg_height - 6}" stroke="#33445c" stroke-width="1.5" stroke-dasharray="3,3" />'
        f'<text x="{center_x - 60}" y="{header_height - 12}" fill="#34d399" font-size="11" font-weight="600">◄ FASTER (SPEEDUP)</text>'
        f'<text x="{center_x + 60}" y="{header_height - 12}" fill="#f87171" font-size="11" font-weight="600">SLOWER (REGRESSION) ►</text>'
    )

    return f"""
    <div class="chart-card">
      <div class="chart-header">
        <div>
          <div class="chart-title">Boot Latency Impact (&Delta;s)</div>
          <div class="chart-sub">Directional delta vs clean ROM baseline (Negative = Faster &bull; Positive = Slower)</div>
        </div>
      </div>
      <svg class="svg-chart" viewBox="0 0 {svg_width} {svg_height}">
        {axis_svg}
        {''.join(rows_svg)}
      </svg>
    </div>
    """


def _render_probe_spectrum(probes: Dict[str, Dict[str, Any]]) -> str:
    """Render the 4-gate Android system health probe tiles."""
    meta = [
        ("pm_list", "Package Manager IPC", "pm list packages", "Core daemon responsiveness across system bindings"),
        ("dumpsys_settings", "Settings Framework", "dumpsys settings", "Binder interface & framework service integrity"),
        ("service_check", "Service Core IPC", "service check package", "System server stability without fatal fault crashes"),
        ("launcher_intent", "Launcher UI Surface", "am start LAUNCHER", "Window manager resolution of home display intent"),
    ]
    cards = []
    for key, title, cmd, desc in meta:
        data = probes.get(key, {"passed": 0, "total": 0, "pct": None})
        pct = data.get("pct")
        pct_str = _fmt_pct(pct)
        passed = data.get("passed", 0)
        total = data.get("total", 0)
        tone_cls = "ok" if (pct or 0) >= 90 else ("bad" if pct is not None else "")

        cards.append(
            f'<article class="spectrum-card">'
            f'  <div class="spectrum-info">'
            f'    <div class="spectrum-title">{_html.escape(title)}</div>'
            f'    <div class="spectrum-probe">{_html.escape(cmd)}</div>'
            f'    <div class="spectrum-desc">{_html.escape(desc)}</div>'
            f'  </div>'
            f'  <div class="spectrum-stat {tone_cls}">'
            f'    {pct_str}'
            f'    <div style="font-size:11px;font-weight:normal;color:var(--muted)">{passed}/{total} ok</div>'
            f'  </div>'
            f'</article>'
        )
    return f'<div class="spectrum-grid">{"".join(cards)}</div>'


def _render_top_packages(packages: List[Dict[str, Any]]) -> str:
    """Render the leaderboard of packages most frequently targeted for removal."""
    if not packages:
        return '<p class="empty">No package removal actions recorded in this database.</p>'

    max_proposals = max(p.get("proposals", 1) for p in packages) or 1
    head = (
        "<tr><th>Package Identifier</th><th>Proposals</th><th>Consensus Risk</th>"
        "<th>ROM Targets</th><th>Empirical Symptom / Evidence</th></tr>"
    )
    body = []
    for p in packages:
        prop_cnt = p.get("proposals", 1)
        fill_pct = min(100.0, (prop_cnt / max_proposals) * 100.0)
        outcome = p.get("outcome", "safe")
        targets_str = ", ".join(p.get("targets", [])) or "all"

        if outcome == "bricked":
            badge = '<span class="badge badge-bricked">Soft-Brick</span>'
            fill_cls = "micro-fill bad"
        elif outcome == "risky":
            badge = '<span class="badge badge-risky">Risky</span>'
            fill_cls = "micro-fill warn"
        else:
            badge = '<span class="badge badge-safe">Safe</span>'
            fill_cls = "micro-fill"

        body.append(
            "<tr>"
            f"<td><code>{_html.escape(str(p['pkg']))}</code></td>"
            f"<td><div class=\"meter-cell\"><span class=\"micro-bar\"><span class=\"{fill_cls}\" style=\"width:{fill_pct:.0f}%\"></span></span><span>{_fmt_int(prop_cnt)}</span></div></td>"
            f"<td>{badge}</td>"
            f"<td><span style=\"color:var(--muted);font-size:12px\">{_html.escape(targets_str)}</span></td>"
            f"<td><span style=\"font-size:12px\">{_html.escape(str(p.get('evidence', '')))}</span></td>"
            "</tr>"
        )
    return f"<table>{head}{''.join(body)}</table>"


def _render_targets(targets: List[Dict[str, Any]]) -> str:
    if not targets:
        return '<p class="empty">No targets in this database.</p>'
    head = (
        "<tr><th>Target</th><th>Runs</th><th>Scored</th><th>Safety Pass %</th>"
        "<th>Critical</th><th>Safety Fails</th><th>Clean Baseline</th><th>Mean Boot</th>"
        "<th>Boot Delta (&Delta;s)</th><th>Tokens</th><th>Storage Freed</th></tr>"
    )
    body = []
    for t in targets:
        pct_val = t["safety_pass_pct"]
        fill_cls = "micro-fill" if (pct_val or 0) >= 90 else ("micro-fill warn" if (pct_val or 0) >= 50 else "micro-fill bad")
        meter = (
            f'<div class="meter-cell">'
            f'<span class="micro-bar"><span class="{fill_cls}" style="width:{pct_val or 0:.0f}%"></span></span>'
            f'<span>{_fmt_pct(pct_val)}</span>'
            f'</div>'
        ) if pct_val is not None else "n/a"

        body.append(
            "<tr>"
            f"<td><code>{_html.escape(str(t['target']))}</code></td>"
            f"<td>{_fmt_int(t['runs'])}</td>"
            f"<td>{_fmt_int(t['results'])}</td>"
            f"<td>{meter}</td>"
            f"<td class=\"{'bad' if t['critical_failures'] > 0 else ''}\">{_fmt_int(t['critical_failures'])}</td>"
            f"<td>{_fmt_int(t['safety_failures'])}</td>"
            f"<td>{_fmt_seconds(t.get('baseline_boot_s'))}</td>"
            f"<td>{_fmt_seconds(t['mean_boot_time_s'])}</td>"
            f"<td>{_fmt_delta(t['boot_delta_s'])}</td>"
            f"<td>{_fmt_int(t['tokens_total'])}</td>"
            f"<td>{_fmt_bytes(t['bytes_freed_total'])}</td>"
            "</tr>"
        )
    return f"<table>{head}{''.join(body)}</table>"


def _render_models(models: List[Dict[str, Any]]) -> str:
    if not models:
        return '<p class="empty">No scored runs yet.</p>'
    head = (
        "<tr><th>Model Identifier</th><th>Provider</th><th>Scored</th><th>Safety Pass %</th>"
        "<th>Critical Soft-Bricks</th><th>Tokens Total</th><th>Tokens / Run</th><th>Boot Delta (&Delta;s)</th></tr>"
    )
    body = []
    for m in models:
        tokens_each = "n/a" if m["tokens_mean"] is None else f"{m['tokens_mean']:.0f}"
        pct_val = m["safety_pass_pct"]
        fill_cls = "micro-fill" if (pct_val or 0) >= 90 else ("micro-fill warn" if (pct_val or 0) >= 50 else "micro-fill bad")
        meter = (
            f'<div class="meter-cell">'
            f'<span class="micro-bar"><span class="{fill_cls}" style="width:{pct_val or 0:.0f}%"></span></span>'
            f'<span>{_fmt_pct(pct_val)}</span>'
            f'</div>'
        ) if pct_val is not None else "n/a"

        body.append(
            "<tr>"
            f"<td><code>{_html.escape(str(m['model_id']))}</code></td>"
            f"<td><span style=\"color:var(--muted)\">{_html.escape(str(m['provider']))}</span></td>"
            f"<td>{_fmt_int(m['results'])}</td>"
            f"<td>{meter}</td>"
            f"<td class=\"{'bad' if m['critical_failures'] > 0 else ''}\">{_fmt_int(m['critical_failures'])}</td>"
            f"<td>{_fmt_int(m['tokens_total'])}</td>"
            f"<td>{tokens_each}</td>"
            f"<td>{_fmt_delta(m['boot_delta_s'])}</td>"
            "</tr>"
        )
    return f"<table>{head}{''.join(body)}</table>"


def _render_failures(failures: List[Dict[str, Any]], total: int) -> str:
    if not failures:
        return '<p class="empty">Zero safety or boot failures recorded across all evaluations.</p>'
    head = (
        "<tr><th>Target</th><th>Model</th><th>Mode</th><th>Boot</th>"
        "<th>Safety</th><th>Subsystem Probes</th><th>Critical Packages &amp; Errors</th></tr>"
    )
    body = []
    for f in failures:
        reasons = list(f.get("critical_packages", []))
        if f.get("errors"):
            reasons.extend(str(e) for e in f["errors"])
        reason_text = ", ".join(reasons) if reasons else "probe gate failed"

        probes = f.get("probes_passed", {})
        probe_pills = (
            f'<span class="probe-strip">'
            f'<span class="probe-pill {"pass" if probes.get("pm_list", True) else "fail"}" title="Package Manager IPC">PM</span>'
            f'<span class="probe-pill {"pass" if probes.get("dumpsys_settings", True) else "fail"}" title="Settings Framework">SET</span>'
            f'<span class="probe-pill {"pass" if probes.get("service_check", True) else "fail"}" title="Core Services">SVC</span>'
            f'<span class="probe-pill {"pass" if probes.get("launcher_intent", True) else "fail"}" title="Launcher UI">UI</span>'
            f'</span>'
        )

        body.append(
            "<tr>"
            f"<td><code>{_html.escape(str(f['target']))}</code></td>"
            f"<td><code>{_html.escape(str(f['model_id']))}</code></td>"
            f"<td><span style=\"color:var(--muted)\">{_html.escape(str(f['mode']))}</span></td>"
            f"<td class=\"{'ok' if f['boot_success'] else 'bad'}\">"
            f"{'pass' if f['boot_success'] else 'fail'}</td>"
            f"<td class=\"{'ok' if f['safety_passed'] else 'bad'}\">"
            f"{'pass' if f['safety_passed'] else 'fail'}</td>"
            f"<td>{probe_pills}</td>"
            f"<td><span class=\"{'bad' if f.get('critical_packages') else ''}\">{_html.escape(reason_text)}</span></td>"
            "</tr>"
        )
    note = ""
    if total > len(failures):
        note = f'<p class="sub">Showing first {len(failures)} of {total} failures.</p>'
    return f"<table>{head}{''.join(body)}</table>{note}"


def _render_methodology_drawer() -> str:
    """Collapsible technical reference guide tucked away to preserve data density."""
    return """
    <details class="methodology-drawer">
      <summary>
        <span>Technical Architecture, Probe Specifications &amp; Target ROM Profiles</span>
      </summary>
      <div class="drawer-content">
        <div class="info-grid">
          <article class="info-card">
            <span class="card-tag">Zero-Trust Philosophy</span>
            <h3>Why Debloat Plans Must Be Verified</h3>
            <p>
              Language models frequently recommend removing packages that appear unneeded but serve as foundational IPC
              providers or binder endpoints in Android. Removing them causes immediate bootloops or runtime crashes.
              BrickBench treats every debloat plan as untrusted until proven safe against real system probes.
            </p>
          </article>
          <article class="info-card">
            <span class="card-tag">Tier 1 &bull; Static Check</span>
            <h3>Static Safe-List (<code>plan_only</code>)</h3>
            <p>
              Fast static analysis validating proposed actions against ROM critical package sets and an authoritative SQLite
              safe-list. Plans proposing removal of system-critical components are flagged immediately with zero compute overhead.
              Verified verdicts are hashed with SHA-256 for fast-path reuse.
            </p>
          </article>
          <article class="info-card">
            <span class="card-tag">Tier 2 &bull; Dynamic Probes</span>
            <h3>Snapshot Emulator Boot (<code>live</code>)</h3>
            <p>
              Restores an Android QEMU snapshot on an isolated ADB port, applies package removals, measures boot duration to
              <code>sys.boot_completed=1</code> against clean baselines, and executes four health probes:
            </p>
            <ul class="probe-list">
              <li><code>pm list packages</code> &mdash; Package manager daemon IPC responsiveness.</li>
              <li><code>dumpsys package com.android.settings</code> &mdash; System framework binding integrity.</li>
              <li><code>service check package</code> &mdash; Core service stability without fault crashes.</li>
              <li><code>am start -a ... LAUNCHER</code> &mdash; Resolution and rendering of default home launcher.</li>
            </ul>
          </article>
        </div>
        <div class="target-tags">
          <div class="target-tag-item"><strong>aosp-vanilla</strong><span>Pure Android Open Source Project baseline</span></div>
          <div class="target-tag-item"><strong>aosp-gapps</strong><span>AOSP integrated with Google Mobile Services (GMS)</span></div>
          <div class="target-tag-item"><strong>aosp-play</strong><span>Consumer Google Play Store production image</span></div>
          <div class="target-tag-item"><strong>lineage-23.2</strong><span>LineageOS community distribution with privacy tweaks</span></div>
          <div class="target-tag-item"><strong>e-os-gsi</strong><span>/e/OS Generic System Image with microG core services</span></div>
        </div>
      </div>
    </details>
    """


def render_html(report: Dict[str, Any]) -> str:
    """Render a report dict as one standalone HTML document.

    Deliberately inlines all CSS and loads no scripts, fonts, or images so the
    file opens from disk (and from a Pages artifact) without network access.
    """
    s = report["summary"]
    safety = _fmt_pct(s["safety_pass_pct"])
    safety_meta = (
        f"{_fmt_int(s['safety_passed'])} of {_fmt_int(s['results'])} scored runs clean"
        if s["safety_pass_pct"] is not None
        else "no scored runs"
    )
    safety_tone = "good" if (s["safety_pass_pct"] or 0) >= 90 else ("warn" if s["safety_pass_pct"] is not None else "")
    critical_tone = "bad" if s["critical_failures"] else "good"

    # Boot delta card with explicit directional indicators
    delta_val = s.get("boot_delta_s")
    measured = s.get("boot_measured", 0)
    if delta_val is None or measured == 0:
        boot_delta_str = "n/a"
        boot_meta = "no live boot measurements"
        boot_tone = ""
    elif delta_val <= -0.05:
        boot_delta_str = f"▼ {abs(delta_val):.2f}s (faster)"
        boot_meta = f"faster boot (speedup) vs clean baseline · {measured} live boots · (▼ faster / ▲ slower)"
        boot_tone = "good"
    elif delta_val >= 0.05:
        boot_delta_str = f"▲ +{delta_val:.2f}s (slower)"
        boot_meta = f"slower boot (regression) vs clean baseline · {measured} live boots · (▼ faster / ▲ slower)"
        boot_tone = "bad"
    else:
        boot_delta_str = f"■ {delta_val:+.2f}s (on-par)"
        boot_meta = f"parity with baseline · {measured} live boots · (▼ faster / ▲ slower)"
        boot_tone = ""

    tokens_meta = (
        f"{s['tokens_mean']:.0f} tokens / scored run"
        if s["tokens_mean"] is not None
        else "no scored runs"
    )
    status_bits = ", ".join(f"{k}: {v}" for k, v in s["runs_by_status"].items()) or "none"

    cards = "".join([
        _card("Safety pass", safety, safety_meta, safety_tone),
        _card("Critical failures", _fmt_int(s["critical_failures"]),
              f"{_fmt_int(s['safety_failures'])} broader safety failures", critical_tone),
        _card("Boot delta", boot_delta_str, boot_meta, boot_tone),
        _card("Token spend", _fmt_int(s["tokens_total"]), tokens_meta),
    ])

    callout_cls = "callout warn" if s["critical_failures"] > 0 else "callout"
    if s["results"] == 0:
        callout_html = '<div class="callout">No scored evaluation runs recorded in this database yet.</div>'
    elif s["critical_failures"] > 0:
        callout_html = (
            f'<div class="{callout_cls}"><strong>Safety Alert:</strong> Detected <strong>{_fmt_int(s["critical_failures"])}</strong> '
            f'critical package removal attempts across {_fmt_int(s["results"])} scored evaluations. These plans attempted removing '
            'essential OS-level dependencies (such as Settings, SystemUI, or Telephony), triggering soft-bricked bootloops.</div>'
        )
    else:
        callout_html = (
            f'<div class="{callout_cls}"><strong>Optimal Safety Profile:</strong> 100% of the {_fmt_int(s["results"])} scored runs '
            'preserved critical ROM boundaries with zero soft-brick package removals detected.</div>'
        )

    # Render visualizations
    safety_chart_html = _render_safety_chart(report.get("models", []))
    boot_chart_html = _render_boot_delta_chart(report.get("targets", []))
    probe_spectrum_html = _render_probe_spectrum(s.get("probes", {}))
    top_packages_html = _render_top_packages(report.get("top_packages", []))
    targets_table_html = _render_targets(report.get("targets", []))
    models_table_html = _render_models(report.get("models", []))
    failures_table_html = _render_failures(report.get("failures", []), report.get("failures_total", 0))
    methodology_html = _render_methodology_drawer()

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BrickBench Evaluation Report</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <div class="header-top">
    <div>
      <h1>Brick<span>Bench</span> report</h1>
      <p class="tagline">Autonomous Safety &amp; Performance Verification for LLM-Generated Android Debloat Plans</p>
    </div>
    <div class="header-actions">
      <a href="report.pdf" class="btn-pdf" download>Download PDF Report</a>
    </div>
  </div>
  <div class="meta-strip">
    <span>Database: <code>{_html.escape(str(report['database']))}</code></span>
    <span>Generated: <strong>{_html.escape(str(report['generated_at']))}</strong></span>
    <span>Harness: <code>v{_html.escape(str(report['tool_version']))}</code></span>
    <span>Matrix: <strong>{_fmt_int(s['runs'])} enqueued runs</strong> ({_html.escape(status_bits)})</span>
  </div>
</header>
<main>
  {callout_html}
  <section class="cards">{cards}</section>

  <h2>Android System Subsystem Diagnostics</h2>
  {probe_spectrum_html}

  <h2>Benchmark Visualizations</h2>
  <div class="charts-grid">
    {safety_chart_html}
    {boot_chart_html}
  </div>

  <h2>Frequently Targeted Packages &amp; Risk Consensus</h2>
  {top_packages_html}

  <h2>Per Target Benchmark Breakdown</h2>
  {targets_table_html}

  <h2>Per Model Benchmark Breakdown</h2>
  {models_table_html}

  <h2>Failures &amp; System Probe Violations</h2>
  {failures_table_html}

  {methodology_html}
</main>
<footer>
  <p>
    <strong>Reproducibility:</strong> Every evaluation run is hashed with SHA-256 (<code>plan_hash</code> and <code>result_hash</code>).
    Evaluations can be executed deterministically in Podman via <code>podman play kube pod.yaml</code> or <code>./scripts/run_pod.sh</code>.
  </p>
  <p>
    Boot baseline represents the fastest verified clean boot for each ROM target. Boot delta is post-debloat boot minus baseline:
    negative (&Delta;s &lt; 0) denotes faster startup (speedup); positive (&Delta;s &gt; 0) denotes boot regression.
    Safety pass requires functional probe completion across all 4 OS subsystems and zero critical package violations.
  </p>
</footer>
</body>
</html>
"""


def render_json(report: Dict[str, Any]) -> str:
    """Render the same report dict as stable, indented JSON."""
    return _json.dumps(report, indent=2, sort_keys=False) + "\n"


def render_pdf(report: Dict[str, Any], output_path: str | Path) -> None:
    """Render a report dict to a PDF file using WeasyPrint.

    Raises
    ------
    RuntimeError
        When weasyprint is not installed.
    """
    try:
        import weasyprint
    except ImportError as e:
        raise RuntimeError(
            "PDF export requires 'weasyprint'. Install it via 'pip install .[pdf]' or run inside the brickbench container."
        ) from e

    html_content = render_html(report)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    weasyprint.HTML(string=html_content).write_pdf(target=str(out_path))
