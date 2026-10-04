"""brickbench CLI — entry point wired by setuptools_scripts."""

import sys
from pathlib import Path

import click

from brickbench.config import __version__


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

@click.command("init")
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
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            model_id TEXT NOT NULL,
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


@click.command("resume")
def resume_cmd():
    """Scan for pending runs and execute one step.

    Call repeatedly until it reports 0 pending work.
    """
    import sqlite3 as sq
    from brickbench.runner import Runner

    data_dir = Path("data/runs")
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "brickbench.db"
    conn = sq.connect(str(db_path))
    runner = Runner(data_dir=str(data_dir), max_emulator_concurrency=1)
    n = 0
    while runner.step() == 0 and n < 100:
        n += 1
    pending_count = conn.execute(
        "SELECT count(*) FROM runs WHERE status='pending'"
    ).fetchone()[0]
    click.echo("Executed " + str(n) + " step(s); pending runs left: " + str(pending_count))
    runner.close()
    conn.close()


@click.command("run")
@click.option("--max-emulator", default=1, type=int, help="Max concurrent emulator instances.")
@click.option("--max-llm", default=None, type=int, help="Max parallel LLM calls (per-provider quota).")
@click.option("--providers", multiple=True, default=[], help="Provider tags to include (zen agy codex openrouter local).")
@click.option("--targets", multiple=True, default=[], help="Target tags to include.")
@click.option("--modes", multiple=True, default=["plan_only", "live"], help="Eval modes.")
@click.option("--debloat-modes", multiple=True, default=["uninstall"], help="Debloat mode(s).")
@click.option("--prompt-variants", multiple=True, default=["baseline"], help="Prompt variant tags.")
def run_command(
    max_emulator: int,
    max_llm: Optional[int],
    providers: Tuple[str, ...],
    targets: Tuple[str, ...],
    modes: Tuple[str, ...],
    debloat_modes: Tuple[str, ...],
    prompt_variants: Tuple[str, ...],
):
    """Execute the full brickbench matrix.

    This builds the Cartesian product of the given dimensions and runs each
    cell through the runner, persisting everything to the data dir.
    """
    from brickbench.config import PROCESS_PROVIDERS

    # Resolve target keys
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

    runner = Runner(data_dir="data/runs", max_emulator_concurrency=max_emulator, llm_concurrency=max_llm)

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


# ---------------------------------------------------------------------------
# Ensure commands are registered when the module is imported
# ---------------------------------------------------------------------------

# These must be added to the group before cli() is called.
from brickbench.cli import init_cmd, resume_cmd, run_command  # noqa: F401  # bind names

cli.add_command(init_cmd)
cli.add_command(resume_cmd)
cli.add_command(run_command)


# ---------------------------------------------------------------------------
# Module-level entry points
# ---------------------------------------------------------------------------

if __name__ == "__main__" and __package__ is None:
    cli()
elif __name__ == "__main__":
    cli()