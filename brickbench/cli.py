"""brickbench CLI — entry point wired by setuptools_scripts."""

import sys
from pathlib import Path

import click

from brickbench.config import __version__


# ---------------------------------------------------------------------------
# Click groups
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__)
def cli():
    """BrickBench — LLM Android debloat evaluation."""
    pass


@click.group()
def target():
    """Target management (build, list, snapshot)."""
    pass


@click.group()
def run_():
    """Run management (matrix, execute, resume)."""
    pass


@click.group()
def provider_():
    """Model provider discovery & quota."""
    pass


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@click.command("version")
def version_cmd():
    """Print the brickbench version."""
    click.echo("brickbench " + __version__)


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


@cli.command("resume")
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


# ---------------------------------------------------------------------------
# Module-level entry points
# ---------------------------------------------------------------------------

if __name__ == "__main__" and __package__ is None:
    # invoked as `python brickbench/cli.py` — dispatch to click CLI
    cli()
elif __name__ == "__main__":
    # invoked as `python -m brickbench.cli` — same
    cli()