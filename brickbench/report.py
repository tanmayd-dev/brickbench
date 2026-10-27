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


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}s"


def _fmt_delta(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}s"


# ---------------------------------------------------------------------------
# Persistence (used by the runner)
# ---------------------------------------------------------------------------

def record_run_result(conn: sqlite3.Connection, *, run: Dict[str, Any], result: Any) -> None:
    """Persist one VerificationResult, keyed by run_id (last write wins).

    `result` is duck-typed so this module does not import the evaluator.
    Recording does not change scoring; it only stores what verify_plan returned.
    """
    conn.execute(RUN_RESULTS_DDL)
    conn.execute(
        """
        INSERT INTO run_results (
            run_id, target, model_id, provider, prompt_variant, mode, debloat_mode,
            boot_success, boot_time_s, safety_passed, critical_violations,
            critical_packages, bytes_freed, tokens_used, errors, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    return {
        "results": len(rows),
        "safety_passed": passed,
        "safety_pass_pct": _pct(passed, len(rows)),
        "safety_failures": failed,
        "critical_failures": critical,
        "tokens_total": tokens,
        "tokens_mean": _mean([r["tokens_used"] for r in rows]),
        "bytes_freed_total": bytes_freed,
        "live_results": len(live),
        "boot_measured": len(boot_ok),
        "boot_success_pct": _pct(len(boot_ok), len(live)),
        "mean_boot_time_s": _mean([r["boot_time_s"] for r in boot_ok]),
        "boot_delta_s": _mean(deltas),
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
                results[item["run_id"]] = item

        verdict_outcomes: Counter = Counter()
        if "verdicts" in tables:
            for row in conn.execute("SELECT outcome, count(*) AS n FROM verdicts GROUP BY outcome"):
                verdict_outcomes[str(row["outcome"])] = int(row["n"])
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
        }
        for r in rows
        if (not r["safety_passed"]) or (not r["boot_success"] and r["mode"] == "live")
        or r["critical_violations"] > 0
    ]
    failures.sort(key=lambda r: (r["target"], r["model_id"], r["run_id"]))

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
        "verdicts": dict(sorted(verdict_outcomes.items())),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #0e1116; --panel: #161b22; --panel-2: #1b2230; --ink: #e6edf3;
  --muted: #8b949e; --line: #2a3240; --accent: #6ee7b7; --warn: #fbbf24;
  --bad: #f87171; --good: #34d399;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 20px 48px; background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main, header, footer { max-width: 1120px; margin: 0 auto; }
header { padding: 36px 0 18px; border-bottom: 1px solid var(--line); }
h1 { margin: 0; font-size: 26px; letter-spacing: -0.02em; }
h1 span { color: var(--accent); }
.sub { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
h2 { font-size: 16px; margin: 34px 0 12px; letter-spacing: 0.02em; text-transform: uppercase; color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-top: 24px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px; }
.card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
.card strong { display: block; font-size: 30px; margin: 6px 0 2px; font-variant-numeric: tabular-nums; }
.card .meta { color: var(--muted); font-size: 12px; }
.card.good strong { color: var(--good); }
.card.bad strong { color: var(--bad); }
.card.warn strong { color: var(--warn); }
table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { padding: 9px 12px; text-align: right; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; }
th { background: var(--panel-2); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
tr:last-child td { border-bottom: 0; }
td.ok { color: var(--good); } td.bad { color: var(--bad); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.empty { color: var(--muted); background: var(--panel); border: 1px dashed var(--line); border-radius: 10px; padding: 18px; }
.header-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; flex-wrap: wrap; }
.tagline { margin: 6px 0 0; color: var(--muted); font-size: 13.5px; }
.btn-pdf { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; background: var(--panel-2); color: var(--ink); border: 1px solid var(--line); border-radius: 8px; text-decoration: none; font-size: 12.5px; font-weight: 500; transition: border-color 0.15s; }
.btn-pdf:hover { border-color: var(--accent); color: var(--accent); }
.meta-strip { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--line); font-size: 12px; color: var(--muted); }
.meta-strip span { display: inline-flex; align-items: center; gap: 4px; }
.meta-strip strong { color: var(--ink); }
.callout { background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--accent); border-radius: 8px; padding: 14px 18px; margin-top: 20px; font-size: 13.5px; line-height: 1.5; }
.callout.warn { border-left-color: var(--bad); }
.callout strong { color: var(--ink); }
.info-section { margin-top: 28px; }
.info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-top: 12px; }
.info-card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; font-size: 13px; color: var(--muted); line-height: 1.5; }
.info-card h3 { margin: 0 0 6px; font-size: 14px; color: var(--ink); }
.info-card p { margin: 0; }
.card-tag { display: inline-block; font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent); margin-bottom: 6px; font-weight: 600; }
.probe-list { margin: 8px 0 0; padding-left: 18px; }
.probe-list li { margin-bottom: 4px; }
.target-tags { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-top: 12px; }
.target-tag-item { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; font-size: 12px; }
.target-tag-item strong { display: block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--accent); margin-bottom: 2px; }
.target-tag-item span { color: var(--muted); }
footer { margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; line-height: 1.6; }
@media (max-width: 640px) { body { padding: 0 12px 32px; } .card strong { font-size: 24px; } th, td { padding: 7px 8px; } }
@media print {
  @page { size: A4; margin: 1.4cm 1.2cm; @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #64748b; } }
  body { background: #ffffff !important; color: #0f172a !important; font-size: 10.5pt; padding: 0 !important; }
  header { border-bottom: 1px solid #cbd5e1 !important; padding: 12px 0 !important; }
  h1 { color: #0f172a !important; font-size: 19pt !important; }
  h1 span { color: #059669 !important; }
  .sub, .tagline { color: #64748b !important; }
  .btn-pdf, .header-actions { display: none !important; }
  .meta-strip { border-top-color: #cbd5e1 !important; color: #64748b !important; }
  .meta-strip strong { color: #0f172a !important; }
  .callout { background: #f8fafc !important; border: 1px solid #cbd5e1 !important; border-left: 4px solid #059669 !important; color: #1e293b !important; page-break-inside: avoid; break-inside: avoid; }
  .callout.warn { border-left-color: #dc2626 !important; }
  h2 { color: #475569 !important; font-size: 11pt !important; margin: 18px 0 8px !important; }
  .card { background: #f8fafc !important; border: 1px solid #cbd5e1 !important; page-break-inside: avoid; break-inside: avoid; padding: 10px !important; }
  .card .label { color: #64748b !important; }
  .card strong { font-size: 18pt !important; color: #0f172a !important; }
  .card.good strong { color: #059669 !important; }
  .card.bad strong { color: #dc2626 !important; }
  .card.warn strong { color: #d97706 !important; }
  .info-card { background: #f8fafc !important; border: 1px solid #cbd5e1 !important; page-break-inside: avoid; break-inside: avoid; color: #334155 !important; }
  .info-card h3 { color: #0f172a !important; }
  .card-tag { color: #059669 !important; }
  .target-tag-item { background: #f8fafc !important; border: 1px solid #cbd5e1 !important; page-break-inside: avoid; break-inside: avoid; }
  .target-tag-item strong { color: #059669 !important; }
  .target-tag-item span { color: #475569 !important; }
  table { background: #ffffff !important; border: 1px solid #cbd5e1 !important; page-break-inside: auto; break-inside: auto; }
  tr { page-break-inside: avoid; break-inside: avoid; }
  th { background: #f1f5f9 !important; color: #475569 !important; border-bottom: 1px solid #cbd5e1 !important; }
  td { border-bottom: 1px solid #e2e8f0 !important; color: #0f172a !important; font-size: 8.5pt !important; padding: 5px 7px !important; }
  td.ok { color: #059669 !important; }
  td.bad { color: #dc2626 !important; }
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


def _render_targets(targets: List[Dict[str, Any]]) -> str:
    if not targets:
        return '<p class="empty">No targets in this database.</p>'
    head = (
        "<tr><th>Target</th><th>Runs</th><th>Scored</th><th>Safety pass</th>"
        "<th>Critical</th><th>Safety fails</th><th>Mean boot</th><th>Boot delta</th>"
        "<th>Tokens</th><th>Freed</th></tr>"
    )
    body = []
    for t in targets:
        body.append(
            "<tr>"
            f"<td><code>{_html.escape(str(t['target']))}</code></td>"
            f"<td>{_fmt_int(t['runs'])}</td>"
            f"<td>{_fmt_int(t['results'])}</td>"
            f"<td>{_fmt_pct(t['safety_pass_pct'])}</td>"
            f"<td>{_fmt_int(t['critical_failures'])}</td>"
            f"<td>{_fmt_int(t['safety_failures'])}</td>"
            f"<td>{_fmt_seconds(t['mean_boot_time_s'])}</td>"
            f"<td>{_fmt_delta(t['boot_delta_s'])}</td>"
            f"<td>{_fmt_int(t['tokens_total'])}</td>"
            f"<td>{_fmt_int(t['bytes_freed_total'])} B</td>"
            "</tr>"
        )
    return f"<table>{head}{''.join(body)}</table>"


def _render_models(models: List[Dict[str, Any]]) -> str:
    if not models:
        return '<p class="empty">No scored runs yet.</p>'
    head = (
        "<tr><th>Model</th><th>Provider</th><th>Scored</th><th>Safety pass</th>"
        "<th>Critical</th><th>Tokens</th><th>Tokens / run</th><th>Boot delta</th></tr>"
    )
    body = []
    for m in models:
        tokens_each = "n/a" if m["tokens_mean"] is None else f"{m['tokens_mean']:.0f}"
        body.append(
            "<tr>"
            f"<td><code>{_html.escape(str(m['model_id']))}</code></td>"
            f"<td>{_html.escape(str(m['provider']))}</td>"
            f"<td>{_fmt_int(m['results'])}</td>"
            f"<td>{_fmt_pct(m['safety_pass_pct'])}</td>"
            f"<td>{_fmt_int(m['critical_failures'])}</td>"
            f"<td>{_fmt_int(m['tokens_total'])}</td>"
            f"<td>{tokens_each}</td>"
            f"<td>{_fmt_delta(m['boot_delta_s'])}</td>"
            "</tr>"
        )
    return f"<table>{head}{''.join(body)}</table>"


def _render_failures(failures: List[Dict[str, Any]], total: int) -> str:
    if not failures:
        return '<p class="empty">No safety or boot failures recorded.</p>'
    head = (
        "<tr><th>Target</th><th>Model</th><th>Mode</th><th>Boot</th>"
        "<th>Safety</th><th>Critical packages / errors</th></tr>"
    )
    body = []
    for f in failures:
        reasons = list(f["critical_packages"])
        if f["errors"]:
            reasons.extend(str(e) for e in f["errors"])
        reason_text = ", ".join(reasons) if reasons else "probe failed"
        body.append(
            "<tr>"
            f"<td><code>{_html.escape(str(f['target']))}</code></td>"
            f"<td><code>{_html.escape(str(f['model_id']))}</code></td>"
            f"<td>{_html.escape(str(f['mode']))}</td>"
            f"<td class=\"{'ok' if f['boot_success'] else 'bad'}\">"
            f"{'pass' if f['boot_success'] else 'fail'}</td>"
            f"<td class=\"{'ok' if f['safety_passed'] else 'bad'}\">"
            f"{'pass' if f['safety_passed'] else 'fail'}</td>"
            f"<td>{_html.escape(reason_text)}</td>"
            "</tr>"
        )
    note = ""
    if total > len(failures):
        note = f'<p class="sub">Showing first {len(failures)} of {total} failures.</p>'
    return f"<table>{head}{''.join(body)}</table>{note}"


def render_html(report: Dict[str, Any]) -> str:
    """Render a report dict as one standalone HTML document.

    Deliberately inlines all CSS and loads no scripts, fonts, or images so the
    file opens from disk (and from a Pages artifact) without network access.
    """
    s = report["summary"]
    safety = _fmt_pct(s["safety_pass_pct"])
    safety_meta = (
        f"{_fmt_int(s['safety_passed'])} of {_fmt_int(s['results'])} scored runs"
        if s["safety_pass_pct"] is not None
        else "no scored runs"
    )
    safety_tone = "good" if (s["safety_pass_pct"] or 0) >= 90 else ("warn" if s["safety_pass_pct"] is not None else "")
    critical_tone = "bad" if s["critical_failures"] else "good"
    boot_delta = _fmt_seconds(s["boot_delta_s"])
    boot_meta = (
        f"vs fastest clean boot per target · {_fmt_int(s['boot_measured'])} live boots"
        if s["boot_measured"]
        else "no live boot measurements"
    )
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
        _card("Boot delta", boot_delta, boot_meta),
        _card("Token spend", _fmt_int(s["tokens_total"]), tokens_meta),
    ])

    callout_cls = "callout warn" if s["critical_failures"] > 0 else "callout"
    if s["results"] == 0:
        callout_html = '<div class="callout">No scored evaluation runs recorded in this database yet.</div>'
    elif s["critical_failures"] > 0:
        callout_html = (
            f'<div class="{callout_cls}"><strong>Safety Alert:</strong> Detected <strong>{_fmt_int(s["critical_failures"])}</strong> '
            f'critical package removal attempts across {_fmt_int(s["results"])} scored evaluations. These plans attempted removing '
            'essential OS-level dependencies (such as Settings, SystemUI, or PackageInstaller), which trigger soft-bricked bootloops.</div>'
        )
    else:
        callout_html = (
            f'<div class="{callout_cls}"><strong>Optimal Safety Profile:</strong> 100% of the {_fmt_int(s["results"])} scored runs '
            'preserved critical ROM boundaries with zero soft-brick package removals detected.</div>'
        )

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
      <p class="tagline">Autonomous Safety &amp; Correctness Benchmark for LLM-Recommended Android Debloat Plans</p>
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
  <section class="info-section">
    <h2>Evaluation Methodology &amp; Architecture</h2>
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
        <span class="card-tag">Tier 1 · Static Check</span>
        <h3>Static Safe-List (<code>plan_only</code>)</h3>
        <p>
          Fast static analysis validating proposed actions against ROM critical package sets and an authoritative SQLite
          safe-list. Plans proposing removal of system-critical components are flagged immediately with zero compute overhead.
          Verified verdicts are hashed with SHA-256 for fast-path reuse.
        </p>
      </article>
      <article class="info-card">
        <span class="card-tag">Tier 2 · Dynamic Probes</span>
        <h3>Snapshot Emulator Boot (<code>live</code>)</h3>
        <p>
          Restores an Android QEMU snapshot on an isolated ADB port, applies package removals, measures boot duration to
          <code>sys.boot_completed=1</code> against clean baselines, and executes four health probes:
        </p>
        <ul class="probe-list">
          <li><code>pm list packages</code> — Package manager daemon IPC responsiveness.</li>
          <li><code>dumpsys package com.android.settings</code> — System framework binding integrity.</li>
          <li><code>service check package</code> — Core service stability without fault crashes.</li>
          <li><code>am start -a ... LAUNCHER</code> — Resolution and rendering of default home launcher.</li>
        </ul>
      </article>
    </div>
  </section>
  <section class="info-section">
    <h2>Metrics Guide &amp; Scoring Interpretation</h2>
    <div class="info-grid">
      <article class="info-card">
        <span class="card-tag">Pass Criteria</span>
        <h3>Safety Pass %</h3>
        <p>
          The core benchmark score: scored runs where all 4 functional health probes passed and zero packages from the
          ROM's critical set were uninstalled. A single broken probe or critical package deletion marks the run as failed.
        </p>
      </article>
      <article class="info-card">
        <span class="card-tag">Soft-Brick Risk</span>
        <h3>Critical Failures</h3>
        <p>
          The count of evaluation runs that recommended deleting core operating system components. Indicates high risk of
          unrecoverable bootloop or system crash for end users.
        </p>
      </article>
      <article class="info-card">
        <span class="card-tag">Performance</span>
        <h3>Boot Delta (&Delta;s)</h3>
        <p>
          Mean difference between post-debloat boot time and the fastest clean snapshot boot on that ROM target. Negative
          values indicate genuine startup speedups; positive deltas denote boot regressions.
        </p>
      </article>
      <article class="info-card">
        <span class="card-tag">Efficiency &amp; Space</span>
        <h3>Tokens &amp; Storage Freed</h3>
        <p>
          Cumulative and mean token usage tracks model reasoning cost per proposal. Storage freed calculates combined
          APK, Odex, and data size reclaimed from system partitions for safe actions.
        </p>
      </article>
    </div>
  </section>
  <section class="info-section">
    <h2>Target ROM Profiles</h2>
    <div class="target-tags">
      <div class="target-tag-item"><strong>aosp-vanilla</strong><span>Pure Android Open Source Project baseline</span></div>
      <div class="target-tag-item"><strong>aosp-gapps</strong><span>AOSP integrated with Google Mobile Services (GMS)</span></div>
      <div class="target-tag-item"><strong>aosp-play</strong><span>Consumer Google Play Store production image</span></div>
      <div class="target-tag-item"><strong>lineage-23.2</strong><span>LineageOS community distribution with privacy tweaks</span></div>
      <div class="target-tag-item"><strong>e-os-gsi</strong><span>/e/OS Generic System Image with microG core services</span></div>
    </div>
  </section>
  <section><h2>Per target breakdown</h2>{_render_targets(report['targets'])}</section>
  <section><h2>Per model breakdown</h2>{_render_models(report['models'])}</section>
  <section><h2>Failures &amp; Violations</h2>{_render_failures(report['failures'], report['failures_total'])}</section>
</main>
<footer>
  <p>
    <strong>Reproducibility:</strong> Every evaluation run is hashed with SHA-256 (<code>plan_hash</code> and <code>result_hash</code>).
    Evaluations can be executed deterministically in Podman via <code>podman play kube pod.yaml</code> or <code>./scripts/run_pod.sh</code>.
  </p>
  <p>
    Boot baseline represents the fastest verified clean boot for each ROM target; boot delta is the mean change (negative is faster).
    Safety pass requires functional probe completion and zero critical package violations.
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

