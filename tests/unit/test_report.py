"""Tests for the report query/rendering layer and the `report` CLI command."""

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from brickbench.cli import cli
from brickbench.report import (
    RUN_RESULTS_DDL,
    collect_report,
    record_run_result,
    render_html,
    render_json,
    render_pdf,
)

FIXTURE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "report_fixture.db"

RUNS_DDL = """
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
)
"""


def _build_db(path: Path, rows):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(RUNS_DDL)
        conn.executescript(RUN_RESULTS_DDL)
        for i, row in enumerate(rows):
            run_id = f"run-{i}"
            conn.execute(
                "INSERT INTO runs (run_id, target, model_id, provider, prompt_variant, mode, "
                "debloat_mode, plan_hash, status) VALUES (?, ?, 'm', 'p', 'baseline', "
                "?, 'uninstall', ?, 'done')",
                (run_id, row["target"], row["mode"], f"h{i}"),
            )
            conn.execute(
                "INSERT INTO run_results (run_id, target, model_id, provider, prompt_variant, "
                "mode, debloat_mode, boot_success, boot_time_s, safety_passed, "
                "critical_violations, critical_packages, bytes_freed, tokens_used, errors, "
                "recorded_at) VALUES (?, ?, 'm', 'p', 'baseline', ?, 'uninstall', ?, ?, ?, ?, "
                "?, 0, ?, '[]', 1.0)",
                (
                    run_id, row["target"], row["mode"], int(row.get("boot_ok", True)),
                    row.get("boot_s", 0.0), int(row.get("safe", True)),
                    len(row.get("critical", [])), json.dumps(row.get("critical", [])),
                    row.get("tokens", 0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


class TestFixtureReport:
    def test_fixture_exists_and_has_scores(self):
        assert FIXTURE.exists(), "run python scripts/make_fixture_db.py"
        report = collect_report(FIXTURE)
        assert report["summary"]["runs"] >= 10
        assert report["summary"]["results"] >= 10
        assert report["targets"]
        assert report["models"]

    def test_fixture_metrics(self):
        s = collect_report(FIXTURE)["summary"]
        assert s["results"] == 12
        assert s["safety_passed"] == 3
        assert s["safety_pass_pct"] == 25.0
        assert s["critical_failures"] == 9
        assert s["tokens_total"] == 7279
        assert s["boot_measured"] == 12
        assert s["boot_delta_s"] is not None

    def test_json_and_html_share_one_report(self):
        report = collect_report(FIXTURE)
        payload = json.loads(render_json(report))
        assert payload == report
        html = render_html(report)
        assert payload["summary"]["safety_pass_pct"] == report["summary"]["safety_pass_pct"]
        assert "25.0%" in html
        assert "7,279" in html


class TestDerivedMetrics:
    def test_boot_delta_uses_median_baseline(self, tmp_path):
        db = tmp_path / "bench.db"
        _build_db(db, [
            {"target": "t", "mode": "live", "boot_ok": True, "boot_s": 10.0, "tokens": 100},
            {"target": "t", "mode": "live", "boot_ok": True, "boot_s": 12.0, "tokens": 200},
            {"target": "t", "mode": "live", "boot_ok": False, "boot_s": 11.0, "tokens": 300},
        ])
        report = collect_report(db)
        s = report["summary"]
        assert s["boot_measured"] == 2
        assert s["live_results"] == 3
        assert s["boot_success_pct"] == 66.7
        assert s["boot_delta_s"] == 0.0  # (10-11 + 12-11) / 2
        assert s["tokens_total"] == 600
        assert report["targets"][0]["baseline_boot_s"] == 11.0

    def test_critical_and_safety_failures_are_distinct(self, tmp_path):
        db = tmp_path / "bench.db"
        _build_db(db, [
            {"target": "t", "mode": "plan_only", "safe": True},
            {"target": "t", "mode": "plan_only", "safe": False,
             "critical": ["com.android.systemui"]},
        ])
        s = collect_report(db)["summary"]
        assert s["safety_pass_pct"] == 50.0
        assert s["safety_failures"] == 1
        assert s["critical_failures"] == 1
        assert collect_report(db)["failures"][0]["critical_packages"] == ["com.android.systemui"]

    def test_missing_db_is_a_friendly_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            collect_report(tmp_path / "nope.db")

    def test_not_a_brickbench_db(self, tmp_path):
        db = tmp_path / "other.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE unrelated (x INTEGER)")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(ValueError):
            collect_report(db)

    def test_record_run_result_round_trips(self, tmp_path):
        db = tmp_path / "bench.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.executescript(RUN_RESULTS_DDL)
            result = SimpleNamespace(
                boot_success=True, boot_time_s=9.5, safety_passed=False,
                critical_violations=["com.android.phone"], bytes_freed=123,
                tokens_used=77, errors=["telephony probe failed"],
            )
            record_run_result(
                conn,
                run={"run_id": "r1", "target": "t", "model_id": "m", "provider": "p",
                     "prompt_variant": "baseline", "mode": "live",
                     "debloat_mode": "uninstall"},
                result=result,
            )
            row = conn.execute("SELECT * FROM run_results WHERE run_id='r1'").fetchone()
        finally:
            conn.close()
        assert row[0] == "r1"
        assert row[9] == 0  # safety_passed -> int
        assert json.loads(row[11]) == ["com.android.phone"]
        assert row[13] == 77

    def test_runner_step_feeds_the_report(self, tmp_path, monkeypatch):
        """A real runner step → evaluator → report round trip, with the LLM stubbed."""
        import brickbench.runner as runner_mod

        monkeypatch.setattr(runner_mod, "_dispatch_model", lambda *a, **k: ("[]", 42))
        runner = runner_mod.Runner(data_dir=tmp_path)
        try:
            runner.enqueue(
                target_key="aosp-vanilla", model_id="stub-model",
                prompt_variant="baseline", mode="plan_only",
                debloat_mode="uninstall", provider="local",
            )
            assert runner.step() == 1
        finally:
            runner.close()

        s = collect_report(tmp_path / "brickbench.db")["summary"]
        assert s["runs"] == 1
        assert s["results"] == 1
        assert s["tokens_total"] == 42


class TestStandaloneHtml:
    def test_no_external_or_absolute_references(self):
        html = render_html(collect_report(FIXTURE))
        low = html.lower()
        assert "<script" not in low
        assert not re.search(r"<link\b", low)
        assert "http://" not in low
        assert "https://" not in low
        assert "/home/" not in html
        assert "/users/" not in low
        assert "file://" not in low
        assert "<style>" in low

    def test_renders_required_metrics(self):
        html = render_html(collect_report(FIXTURE))
        for label in ("Safety pass", "Critical failures", "Boot delta", "Token spend"):
            assert label in html

    def test_print_media_css_included(self):
        html = render_html(collect_report(FIXTURE))
        assert "@media print" in html
        assert "@page" in html
        assert "A4" in html
        assert "counter(page)" in html
        assert "counter(pages)" in html
        assert "#059669" in html

    def test_boot_delta_directional_indicators(self):
        from brickbench.report import _fmt_delta
        assert "faster" in _fmt_delta(-0.85)
        assert "▼" in _fmt_delta(-0.85)
        assert "slower" in _fmt_delta(0.42)
        assert "▲" in _fmt_delta(0.42)
        assert "on-par" in _fmt_delta(0.01)

    def test_svg_visualizations_and_probe_diagnostics_rendered(self):
        report = collect_report(FIXTURE)
        html = render_html(report)
        assert "svg-chart" in html
        assert "Model Safety &amp; Soft-Brick Risk Spectrum" in html
        assert "Net Storage Reclaimed" in html
        assert "Model Benchmark Leaderboard" in html
        assert "Package Risk Wall" in html
        assert "Android System Subsystem Diagnostics" in html
        assert "methodology-drawer" in html
        assert "pm list packages" in html

    def test_probes_and_top_packages_aggregated(self):
        report = collect_report(FIXTURE)
        probes = report["summary"]["probes"]
        assert "pm_list" in probes
        assert "dumpsys_settings" in probes
        assert "service_check" in probes
        assert "launcher_intent" in probes
        assert probes["pm_list"]["total"] == 12
        assert len(report["top_packages"]) > 0

    def test_package_inventory_and_removal_counts(self):
        report = collect_report(FIXTURE)
        s = report["summary"]
        assert s["packages_total"] == 130
        assert s["packages_removed_total"] > 0
        assert s["debloat_ratio_pct"] is not None
        assert s["packages_removed_mean"] is not None

    def test_prompt_variants_and_interactive_switcher(self):
        report = collect_report(FIXTURE)
        pv = report["prompt_variants"]
        assert "all" in pv
        assert "adversarial_bait" in pv
        assert "aggressive_storage" in pv
        html = render_html(report)
        assert 'id="tab-all"' in html
        assert 'id="tab-adversarial_bait"' in html
        assert 'id="tab-aggressive_storage"' in html
        assert "view-adversarial_bait" in html
        assert "SnitchBench-Style Prompt Matrix" in html


class TestRenderPdf:
    def test_render_pdf_without_weasyprint_raises_runtime_error(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "weasyprint", None)
        report = collect_report(FIXTURE)
        out = tmp_path / "report.pdf"
        with pytest.raises(RuntimeError) as exc_info:
            render_pdf(report, out)
        assert "PDF export requires 'weasyprint'" in str(exc_info.value)
        assert "pip install .[pdf]" in str(exc_info.value)

    def test_render_pdf_with_weasyprint(self, tmp_path):
        from unittest.mock import MagicMock, patch
        mock_weasy = MagicMock()
        mock_html_inst = MagicMock()
        mock_weasy.HTML.return_value = mock_html_inst

        report = collect_report(FIXTURE)
        out = tmp_path / "sub" / "report.pdf"

        with patch.dict("sys.modules", {"weasyprint": mock_weasy}):
            render_pdf(report, out)

        mock_weasy.HTML.assert_called_once()
        _, kwargs = mock_weasy.HTML.call_args
        html_str = kwargs.get("string")
        assert "Brick<span>Bench</span> report" in html_str
        assert "@media print" in html_str
        mock_html_inst.write_pdf.assert_called_once_with(target=str(out))


class TestReportCli:
    def test_html_output(self, tmp_path):
        out = tmp_path / "dist" / "index.html"
        result = CliRunner().invoke(
            cli, ["report", "--db", str(FIXTURE), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert "Safety pass" in out.read_text(encoding="utf-8")
        assert "25.0%" in result.output

    def test_json_output(self, tmp_path):
        out = tmp_path / "report.json"
        result = CliRunner().invoke(
            cli, ["report", "--db", str(FIXTURE), "--output", str(out), "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text(encoding="utf-8"))["summary"]["results"] == 12

    def test_pdf_auto_detection(self, tmp_path):
        from unittest.mock import patch
        out = tmp_path / "report.pdf"
        with patch("brickbench.report.render_pdf") as mock_render:
            result = CliRunner().invoke(
                cli, ["report", "--db", str(FIXTURE), "--output", str(out)]
            )
            assert result.exit_code == 0, result.output
            mock_render.assert_called_once()
            args, _ = mock_render.call_args
            assert args[0]["summary"]["results"] == 12
            assert str(args[1]) == str(out)
            assert "Report (pdf) written to" in result.output

    def test_explicit_format_pdf(self, tmp_path):
        from unittest.mock import patch
        out = tmp_path / "custom_named_file"
        with patch("brickbench.report.render_pdf") as mock_render:
            result = CliRunner().invoke(
                cli, ["report", "--db", str(FIXTURE), "--output", str(out), "--format", "pdf"]
            )
            assert result.exit_code == 0, result.output
            mock_render.assert_called_once()
            args, _ = mock_render.call_args
            assert str(args[1]) == str(out)
            assert "Report (pdf) written to" in result.output

    def test_cli_auto_detect_json(self, tmp_path):
        out = tmp_path / "report.json"
        result = CliRunner().invoke(
            cli, ["report", "--db", str(FIXTURE), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert "Report (json) written to" in result.output
        assert json.loads(out.read_text(encoding="utf-8"))["summary"]["results"] == 12

    def test_cli_pdf_missing_weasyprint(self, tmp_path):
        from unittest.mock import patch
        out = tmp_path / "report.pdf"
        with patch("brickbench.report.render_pdf", side_effect=RuntimeError(
            "PDF export requires 'weasyprint'. Install it via 'pip install .[pdf]' or run inside the brickbench container."
        )):
            result = CliRunner().invoke(
                cli, ["report", "--db", str(FIXTURE), "--output", str(out), "--format", "pdf"]
            )
            assert result.exit_code != 0
            assert "PDF export requires 'weasyprint'" in result.output

    def test_missing_db_exits_nonzero(self, tmp_path):
        result = CliRunner().invoke(
            cli, ["report", "--db", str(tmp_path / "nope.db"), "--output", str(tmp_path / "x.html")]
        )
        assert result.exit_code != 0
        assert "no brickbench database" in result.output
