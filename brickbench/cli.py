"""brickbench CLI — entry point wired by setuptools_scripts."""

from typing import Dict, List, Optional, Tuple

import click

from brickbench.config import __version__

from tqdm import tqdm


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__)
def cli():
    """BrickBench — LLM Android debloat evaluation."""
    pass


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@cli.command("init")
def init_cmd():
    """Scaffold the data directory and SQLite state store."""
    from pathlib import Path
    import sqlite3

    base = Path("data/runs")
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "brickbench.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
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
    )
    conn.close()
    click.echo("Initialized brickbench state at " + str(base / "brickbench.db"))


@cli.command("resume")
@click.option("--max-emulator", default=1, type=int, help="Max concurrent emulator instances (for live mode).")
@click.option("--max-llm", default=None, type=int, help="Max parallel LLM calls.")
@click.option("--max-steps", default=0, type=int, help="Max steps per invocation (0 = drain until no pending work).")
def resume_cmd(max_emulator: int, max_llm: Optional[int], max_steps: int):
    """Scan for pending runs and execute them until none remain.

    Each step is one matrix cell: a real LLM plan call (openrouter/local),
    then safe-list verify (plan_only) or a real emulator boot (live).
    `skipped` = provider with no backend wired (zen/agy/codex).
    """
    from pathlib import Path
    import sqlite3 as sq
    from brickbench.runner import Runner

    if max_emulator > 1:
        import shutil
        mem_gb = 0.0
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        mem_gb = int(line.split()[1]) / 1e6
                        break
        except OSError:
            pass
        if mem_gb and mem_gb < max_emulator * 2.5:
            click.echo(f"⚠️  Only ~{mem_gb:.1f} GB RAM available; "
                       f"{max_emulator} emulators want ~{max_emulator * 2} GB. "
                       f"Consider --max-emulator 1.")

    data_dir = Path("data/runs")
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "brickbench.db"
    conn = sq.connect(str(db_path))
    runner = Runner(data_dir=str(data_dir), max_emulator_concurrency=max_emulator, llm_concurrency=max_llm)

    # Get initial pending count
    initial_pending = conn.execute(
        "SELECT count(*) FROM runs WHERE status='pending'"
    ).fetchone()[0]
    if initial_pending == 0:
        click.echo("No pending runs.")
        runner.close()
        conn.close()
        return
    click.echo(f"📋 Starting: {initial_pending} pending runs")

    import concurrent.futures

    n = 0
    pending_count = initial_pending
    workers = max(max_llm or 1, 1)  # At least 1 worker
    limit = max_steps if max_steps and max_steps > 0 else float("inf")

    with tqdm(total=initial_pending, desc="Resuming runs", unit="run",
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]") as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            while n < limit and pending_count > 0:
                # Keep `workers` steps in flight; top up as each completes.
                futures = {executor.submit(runner.step) for _ in range(min(workers, pending_count))}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                        if result == 1:
                            n += 1
                            pbar.update(1)
                    except Exception as e:
                        click.echo(f"Error in worker: {e}")
                    pending_count = conn.execute(
                        "SELECT count(*) FROM runs WHERE status='pending'"
                    ).fetchone()[0]
                    if pending_count == 0 or n >= limit:
                        break
                if pending_count == 0:
                    break

    click.echo(f"\n✅ Done: {n} steps executed, {pending_count} pending runs left")
    runner.close()
    conn.close()


@cli.command("run")
@click.option("--providers", multiple=True, default=[], help="Provider tags to include (zen agy codex openrouter local).")
@click.option("--targets", multiple=True, default=[], help="Target tags to include.")
@click.option("--modes", multiple=True, default=["plan_only", "live"], help="Eval modes.")
@click.option("--debloat-modes", multiple=True, default=["uninstall"], help="Debloat mode(s).")
@click.option("--prompt-variants", multiple=True, default=["baseline"], help="Prompt variant tags.")
def run_command(
    providers: Tuple[str, ...],
    targets: Tuple[str, ...],
    modes: Tuple[str, ...],
    debloat_modes: Tuple[str, ...],
    prompt_variants: Tuple[str, ...],
):
    """Enqueue the full brickbench matrix (no execution; `resume` runs it)."""
    from brickbench.config import PROCESS_PROVIDERS, PROCESS_MODES, DEBLOAT_MODES, PACKAGES_PER_TARGET

    # Resolve target keys
    # Normalize space-separated arguments when passed as a single string
    def split_options(opts):
        if isinstance(opts, tuple) and len(opts) == 1:
            val = opts[0].strip()
            if val.lower() == "all":
                return None  # "all" means use defaults
            return tuple(val.split())
        return opts

    targets = split_options(targets)
    providers = split_options(providers)
    modes = split_options(modes)
    debloat_modes = split_options(debloat_modes)
    prompt_variants = split_options(prompt_variants)
    # Expand "all" to full lists
    if targets is None:
        targets = PACKAGES_PER_TARGET.keys()
    if providers is None:
        providers = PROCESS_PROVIDERS
    if modes is None:
        modes = PROCESS_MODES
    if debloat_modes is None:
        debloat_modes = DEBLOAT_MODES
    if prompt_variants is None:
        prompt_variants = ["baseline"]


    target_keys = list(targets) if targets else ["aosp-vanilla", "aosp-gapps", "aosp-play", "lineage-23.2", "e-os-gsi"]


    # Resolve model IDs per provider
    model_cfgs: Dict[str, List[str]] = {}
    for p in providers or PROCESS_PROVIDERS:
        if p == "zen":
            model_cfgs["zen"] = [
                "fledge-alpha-free", "ling-3.1-flash-free", "longcat-2.5-preview-free",
                "space-bunny-free", "mimo-v2.6-flash-free", "muse-spark-1.3-contributor-free",
                "ling-3.0-flash-fin-free", "nemotron-3.5-lightning-free", "nemotron-3-ultra-free",
                "big-pickle",
            ]
        elif p == "agy":
            model_cfgs["agy"] = [
                "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
                "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
                "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
                "gemini-3.1-pro-high", "gemini-3.1-pro-low",
                "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
            ]
        elif p == "codex":
            model_cfgs["codex"] = ["gpt-6-luna"]
        elif p == "openrouter":
            model_cfgs["openrouter"] = [
                "qwen/qwen3.8-27b:free", "google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free",
                "nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-lightning:free",
                "nvidia/nemotron-3-super-120b-a12b:free", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
                "thinkingmachines/inkling:free", "thinkingmachines/inkling-small:free",
                "poolside/laguna-s-2.1:free", "poolside/laguna-xs-2.1:free",
                "cohere/north-mini-code:free", "dots-studio/dots-3-note-preview:free",
                "inclusionai/ling-3.0-flash-sante:free", "liquid/lfm-2.5-2.6b:free",
                "apodex/apodex-1.1-mini:free", "nvidia/nemotron-3.5-content-safety:free",
            ]
        elif p == "local":
            model_cfgs["local"] = [
                "qwen3:4b", "phi4-mini:3.8b", "llama3.2:3b", "gemma3:4b",
            ]
        else:
            continue

    from brickbench.runner import Runner

    runner = Runner(data_dir="data/runs")

    try:
        total = 0
        for tk in target_keys:
            for pid, models in model_cfgs.items():
                for mid in models:
                    for mode in modes:
                        for dmode in debloat_modes:
                            for pv in prompt_variants:
                                # Determine the provider tag for this model_id
                                provider_tag = pid  # the tag is the provider name
                                rid = runner.enqueue(
                                    target_key=tk,
                                    model_id=mid,
                                    prompt_variant=pv,
                                    mode=mode,
                                    debloat_mode=dmode,
                                    provider=provider_tag,
                                )
                                total += 1
        click.echo("🧱 Enqueued " + str(total) + " matrix cells into data/runs/")
    finally:
        runner.close()



@cli.command("reset")
@click.option("--confirm", is_flag=True, default=False, help="Require confirmation before resetting")
def reset_cmd(confirm: bool):
    """Reset all brickbench results (discard all data)."""
    from pathlib import Path
    import sqlite3

    base = Path("data/runs")
    db_path = base / "brickbench.db"

    if not confirm:
        response = input("⚠️  WARNING: This will delete ALL brickbench results. Type 'yes' to confirm: ")
        if response.lower() != "yes":
            click.echo("Reset cancelled.")
            return

    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS runs")
        conn.execute("DROP TABLE IF EXISTS verdicts")
        conn.commit()
        conn.close()
        click.echo("✅ Reset complete. All results discarded.")
    else:
        click.echo("No brickbench database found at " + str(db_path))


@cli.command("export")
@click.option("--output", type=str, default="brickbench_export.json", required=False,
              help="Output file path (default: brickbench_export.json)")
@click.option("--format", type=click.Choice(["json", "csv"]), default="json", required=False,
              help="Export format (default: json)")
def export_cmd(output: str, format: str):
    """Export brickbench results to a file (JSON or CSV)."""
    from pathlib import Path
    import sqlite3
    import csv

    base = Path("data/runs")
    db_path = base / "brickbench.db"

    if not db_path.exists():
        click.echo("No brickbench database found at " + str(db_path))
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if format == "json":
        cur = conn.execute("SELECT * FROM runs")
        rows = [{k: row[k] for k in row.keys()} for row in cur]
        import json
        with open(output, "w") as f:
            json.dump(rows, f, indent=2)
        click.echo(f"✅ Exported {len(rows)} run records to {output}")
    elif format == "csv":
        cur = conn.execute("SELECT * FROM runs")
        if cur.rowcount == 0:
            click.echo("No run records to export.")
            conn.close()
            return

        cols = [desc[0] for desc in cur.description]
        with open(output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for row in cur:
                writer.writerow({k: row[k] for k in row.keys()})
        click.echo(f"✅ Exported {cur.rowcount} run records to {output}")

    # Also export verdicts
    cur = conn.execute("SELECT * FROM verdicts")
    if format == "json":
        rows = [{k: row[k] for k in row.keys()} for row in cur]
        verdicts_output = output.replace(".json", "_verdicts.json")
        with open(verdicts_output, "w") as f:
            json.dump(rows, f, indent=2)
        click.echo(f"✅ Exported {len(rows)} verdict records to {verdicts_output}")
    elif format == "csv":
        cols = [desc[0] for desc in cur.description]
        verdicts_output = output.replace(".csv", "_verdicts.csv")
        with open(verdicts_output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for row in cur:
                writer.writerow({k: row[k] for k in row.keys()})
        click.echo(f"✅ Exported {cur.rowcount} verdict records to {verdicts_output}")

    conn.close()


# ---------------------------------------------------------------------------
# Module-level entry points
# ---------------------------------------------------------------------------

if __name__ == "__main__" and __package__ is None:
    cli()
elif __name__ == "__main__":
    cli()