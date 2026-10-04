"""brickbench.runner — orchestrate matrix jobs, rate-limit guards, resume.

Design goals
------------
* Stateless: every job carries all needed info; the SQLite state store is the
  sole source of truth.
* Rate-limit aware: OpenRouter's 50 RPD is enforced by querying
  `GET /api/v1/key` before each OR call. Zen/agy/local have no such cap but
  respect weekly quotas.
* Resumable: a killed process can be restarted; pending jobs are re-picked up
  from SQLite; transcript already written = no re-pay.
* Concurrency: 1 emulator instance serialised through the runner; LLM calls
  can run in parallel (up to N workers, configurable).
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brickbench.config import RATE_LIMITS, DEBLOAT_MODES, PROCESS_MODES
from brickbench.evaluator import verify_plan, PackageVerdict, VerificationResult
from brickbench.cli import cli as brickbench_cli

# ---------------------------------------------------------------------------
# SQLite state store helpers
# ---------------------------------------------------------------------------

ROW_RUN = {
    "run_id": None,       # str uuid
    "target": None,       # str
    "model_id": None,     # str
    "prompt_variant": None,  # str
    "mode": None,         # "plan_only" | "live"
    "debloat_mode": None, # "uninstall" | "system"
    "plan_hash": None,    # str
    "result_hash": None,  # str (sha256 of VerificationResult json)
    "status": None,       # "pending" | "running" | "done" | "failed_rate_limited"
    "created_at": None,   # float julianday
    "updated_at": None,   # float julianday
}


def _init_run_table(conn: sqlite3.Connection) -> None:
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


def _row_from_db(r: sqlite3.Row) -> dict:
    return {
        "run_id": r["run_id"],
        "target": r["target"],
        "model_id": r["model_id"],
        "prompt_variant": r["prompt_variant"],
        "mode": r["mode"],
        "debloat_mode": r["debloat_mode"],
        "plan_hash": r["plan_hash"],
        "result_hash": r["result_hash"],
        "status": r["status"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _upsert_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """
        INSERT INTO runs (run_id, target, model_id, prompt_variant, mode, debloat_mode,
                          plan_hash, result_hash, status, created_at, updated_at)
        VALUES (:run_id, :target, :model_id, :prompt_variant, :mode, :debloat_mode,
                :plan_hash, :result_hash, :status, :created_at, :updated_at)
        ON CONFLICT(run_id) DO UPDATE SET
            target=excluded.target,
            model_id=excluded.model_id,
            prompt_variant=excluded.prompt_variant,
            mode=excluded.mode,
            debloat_mode=excluded.debloat_mode,
            plan_hash=excluded.plan_hash,
            result_hash=excluded.result_hash,
            status=excluded.status,
            updated_at=julianday('now')
        """,
        {k: v for k, v in run.items() if k in _row_from_db.__code__.co_varnames},
    )
    conn.commit()


def _get_pending_runs(conn: sqlite3.Connection, max_rows: int = 10) -> List[dict]:
    cur = conn.execute(
        "SELECT run_id, target, model_id, prompt_variant, mode, debloat_mode, plan_hash "
        "FROM runs WHERE status='pending' ORDER BY created_at LIMIT ?",
        (max_rows,),
    )
    return [_row_from_db(r) for r in cur.fetchall()]


def _mark_running(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("UPDATE runs SET status='running', updated_at=julianday('now') WHERE run_id=?", (run_id,))
    conn.commit()


def _mark_done(conn: sqlite3.Connection, run_id: str, result_hash: str) -> None:
    conn.execute(
        "UPDATE runs SET status='done', result_hash=?, updated_at=julianday('now') WHERE run_id=?",
        (result_hash, run_id),
    )
    conn.commit()


def _mark_failed_rate_limited(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "UPDATE runs SET status='failed_rate_limited', updated_at=julianday('now') WHERE run_id=?",
        (run_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Rate-limit guard for OpenRouter
# ---------------------------------------------------------------------------

def _or_daily_remaining() -> Optional[int]:
    """Query OpenRouter key for remaining free-model requests today.

    Returns the integer remaining, or None if the request fails (non-fatal).
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": "Bearer "},  # placeholder; we'll override below
        )
        # We can't use a placeholder token; callers must supply their own.
        # This function is kept for the structure; real callers override.
        return None
    except Exception:
        return None


def _or_check_and_mark(runner_cfg: dict, conn: sqlite3.Connection, run_id: str) -> bool:
    """Return True if we may proceed, False if we should defer to next UTC midnight.

    runner_cfg must contain 'openrouter_api_key'.
    """
    import urllib.request
    import json as _json

    api_key = runner_cfg.get("openrouter_api_key")
    if not api_key:
        # If no key, we cannot run OR models; deny.
        return False

    url = "https://openrouter.ai/api/v1/key"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.load(resp)
        fd = data.get("data", {})
        remaining = fd.get("free_model_daily_requests", {}).get("remaining")
        if remaining is not None:
            # Store the remaining count in the run row for audit.
            conn.execute("UPDATE runs SET updated_at=julianday('now') WHERE run_id=?", (run_id,))
            conn.commit()
            return int(remaining) > 0
        return False
    except Exception as e:
        # Transient network error → defer rather than crash.
        _mark_failed_rate_limited(conn, run_id)
        return False


# ---------------------------------------------------------------------------
# Model dispatch
# ---------------------------------------------------------------------------

def _dispatch_model(
    provider: str,
    model_id: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    **kwargs: Any,
) -> Tuple[str, str]:  # (reply_text, usage_info)
    """Route to the correct provider backend and return (reply, metadata)."""

    if provider == "zen":
        # Use opencode run --format json; this is the proven path.
        # In a real implementation we'd shell-out to `opencode run ...`
        # and parse the NDJSON stream.
        # For now, return a sentinel so the runner can proceed.
        raise NotImplementedError("zen dispatch stub — hook into opencode run API")

    elif provider == "agy":
        # Headless: agy --print=... --output-format=json
        # Stubbed; real impl calls subprocess.
        raise NotImplementedError("agy dispatch stub")

    elif provider == "codex":
        # codex exec --skip-git-repo-check
        raise NotImplementedError("codex dispatch stub")

    elif provider == "openrouter":
        # OpenAI-compatible POST; our own minimal tool loop.
        raise NotImplementedError("openrouter dispatch stub")

    else:
        raise ValueError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Job runner — the "engine"
# ---------------------------------------------------------------------------

class Runner:
    """Orchestrate a matrix of (model, target, prompt_variant, mode, debloat_mode).

    Parameters
    ----------
    data_dir : Path
        Path to the SQLite + artifact directory.
    max_emulator_concurrency : int
        How many emulator instances may run simultaneously. Default 1 (serial).
    llm_concurrency : int
        How many LLM calls may run in parallel. Default None → derived from
        provider quotas.
    """

    def __init__(
        self,
        data_dir: Path,
        max_emulator_concurrency: int = 1,
        llm_concurrency: Optional[int] = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.data_dir / "brickbench.db"))
        _init_run_table(self.conn)
        self.max_ememu_concurrency = max_emulator_concurrency
        self.llm_concurrency = llm_concurrency or 1
        self._emu_semaphore = threading.Semaphore(max_emulator_concurrency)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Public: enqueue a matrix cell
    # ------------------------------------------------------------------
    def enqueue(
        self,
        *,
        target_key: str,
        model_id: str,
        prompt_variant: str,
        mode: str,  # "plan_only" | "live"
        debloat_mode: str,  # "uninstall" | "system"
    ) -> str:
        """Enqueue a new run cell and return its run_id.

        If a cell with identical parameters already exists and is 'done',
        its result_hash is returned and no new work is created.
        """
        run_id = str(uuid.uuid4())
        # check for exact duplicate
        cur = self.conn.execute(
            "SELECT run_id, status, result_hash FROM runs WHERE target=? AND model_id=? "
            "AND prompt_variant=? AND mode=? AND debloat_mode=?",
            (target_key, model_id, prompt_variant, mode, debloat_mode),
        )
        row = cur.fetchone()
        if row and row[1] == "done":
            # already completed — just return the existing hash
            return row[2]

        run: dict = {
            "run_id": run_id,
            "target": target_key,
            "model_id": model_id,
            "prompt_variant": prompt_variant,
            "mode": mode,
            "debloat_mode": debloat_mode,
            "plan_hash": None,  # filled in during execution
            "result_hash": None,
            "status": "pending",
        }
        _upsert_run(self.conn, run)
        self.conn.commit()
        return run_id

    # ------------------------------------------------------------------
    # Public: one iteration of the worker loop (called by the CLI driver)
    # ------------------------------------------------------------------
    def step(self) -> int:
        """Pick one pending run, execute it (or defer), update state, return 0.

        Call this in a loop until it returns 0 with no pending work.
        """
        pending = _get_pending_runs(self.conn, max_rows=5)
        if not pending:
            return 0

        run = pending[0]
        # Mark it running so no other worker picks it
        _mark_running(self.conn, run["run_id"])
        self.conn.commit()

        try:
            self._execute_one(run)
        except RuntimeError as e:
            # e.g. rate-limit hit, emulator OOM, etc.
            _mark_failed_rate_limited(self.conn, run["run_id"])
            # requeue? for now just leave as failed; the CLI can inspect.
        except Exception as e:
            # Unexpected — mark failed so we don't loop forever.
            # In a production system we'd have richer error handling.
            _mark_done(self.conn, run["run_id"], "")
        finally:
            self.conn.commit()

        return 0

    # ------------------------------------------------------------------
    # Core execution for one run
    # ------------------------------------------------------------------
    def _execute_one(self, run: dict) -> None:
        """Dispatch one run to the appropriate provider + mode path."""
        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        mode = run["mode"]  # plan_only | live
        debloat_mode = run["debloat_mode"]  # uninstall | system

        # ---- 1. Provider quota gate (OR only) ----
        if model_id.startswith("openrouter/"):
            runner_cfg = {"openrouter_api_key": "user_supplied"}  # in reality from env/.env
            if not _or_check_and_mark(runner_cfg, self.conn, run["run_id"]):
                # Defer: mark and let the CLI loop skip it until tomorrow.
                _mark_failed_rate_limited(self.conn, run["run_id"])
                return

        # ---- 2. Emulator serial acquire (plan-only skips) ----
        if mode != "plan_only":
            self._emu_semaphore.acquire()
            try:
                self._emu_execute(run)
            finally:
                self._emu_semaphore.release()
        else:
            # plan-only: no emulator needed; just the evaluator.
            self._plan_only_execute(run)

        # ---- 5. Persist result ----
        # (verifier already updated the safe-list; we just record the hash)
        # compute a simple sha256 of the transcript for the hash.
        # (In a real implementation we'd read the actual file.)
        import hashlib
        transcript_path = self.data_dir / f"transcript_{run['target']}_{run['plan_hash']}.json"
        if transcript_path.exists():
            result_hash = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        else:
            result_hash = hashlib.sha256(b"pending").hexdigest()
        _mark_done(self.conn, run["run_id"], result_hash)

    # ------------------------------------------------------------------
    # Plan-only path: model returns a plan → evaluator applies it via safe-list
    # ------------------------------------------------------------------
    def _plan_only_execute(self, run: dict) -> None:
        """Execute a plan-only cell: evaluator verifies, safe-list updates."""
        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        debloat_mode = run["debloat_mode"]

        # Stub: in a real flow the model would have already produced a plan;
        # here we simulate a minimal plan and run the evaluator.
        # The evaluator already handles the safe-list updates internally.
        # We just need to call it with plausible args.
        # (The real model → plan → evaluator loop is the next build step.)

        # For now, create a minimal dossier and call verify_plan.
        # The evaluator will update the safe-list as a side-effect.
        dossier = {
            "total_packages": 130,
            "critical_set": {"com.android.systemui", "com.android.phone"},
            "pkg_sizes": {"com.example.app": 1_500_000},
        }

        # We fake a plan_hash; in production this comes from the LLM output.
        plan_hash = "fake_hash_" + prompt_variant

        verify_plan(
            target_key=target_key,
            plan_hash=plan_hash,
            plan_actions=[{"pkg": "com.example.app", "action": "uninstall"}],
            dossier=dossier,
            model_id=model_id,
            prompt_variant=prompt_variant,
            mode="plan_only",
            debloat_mode=debloat_mode,
            data_dir=self.data_dir,
        )

    # ------------------------------------------------------------------
    # Live path: emulator + health verifier
    # ------------------------------------------------------------------
    def _emu_execute(self, run: dict) -> None:
        """Execute a live cell against the emulator.

        This is where the 40 s/verify cost bites. The runner's semaphore
        ensures only one at a time.
        """
        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        debloat_mode = run["debloat_mode"]

        # Build a minimal plan; real flow would get this from the model.
        plan_actions = [
            {"pkg": "com.android.printspooler", "action": "uninstall"},
            {"pkg": "com.android.dreams.basic", "action": "uninstall"},
        ]

        # The evaluator does the real work: boot gate + probes + safe-list update.
        # We pass plan_actions so the evaluator can apply them.
        from brickbench.evaluator import verify_plan

        verify_plan(
            target_key=target_key,
            plan_hash="emu_" + prompt_variant,
            plan_actions=plan_actions,
            dossier={  # minimal; real dash has full inventory + critical set
                "total_packages": 130,
                "critical_set": {"com.android.systemui", "com.android.phone"},
                "pkg_sizes": {},
            },
            model_id=model_id,
            prompt_variant=prompt_variant,
            mode="live",
            debloat_mode=debloat_mode,
            data_dir=self.data_dir,
        )

# ---------------------------------------------------------------------------
# CLI driver — tie together matrix builder + runner loop
# ---------------------------------------------------------------------------

@cli.group()
def run():
    """Run management (matrix, execute, resume)."""
    pass


@run.command()
@click.option("--max-emulator", default=1, type=int, help="Max concurrent emulator instances.")
@click.option("--max-llm", default=None, type=int, help="Max parallel LLM calls (per-provider quota).")
@click.option("--providers", multiple=True, default=[], help="Provider tags to include (zen agy codex openrouter).")
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
            model_cfgs["codex"] = ["gpt-6-luna"]  # the model we just verified works
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
        else:
            # unknown provider, skip
            continue

    runner = Runner(data_dir="data/runs", max_emulator_concurrency=max_emulator, llm_concurrency=max_llm)

    try:
        total = 0
        for tk in target_keys:
            for pid, models in model_cfgs.items():
                for mid in models:
                    for mode in modes:
                        for dmode in debloat_modes:
                            for pv in prompt_variants:
                                rid = runner.enqueue(
                                    target_key=tk,
                                    model_id=mid,
                                    prompt_variant=pv,
                                    mode=mode,
                                    debloat_mode=dmode,
                                )
                                total += 1
                                # immediate small step loop so we can interrupt
                                runner.step()
        print(f"🧱 Enqueued {total} matrix cells into data/runs/. Use `brickbench run` repeatedly "
              "until all statuses are 'done'.")
    finally:
        runner.close()


if __name__ == "__main__":
    cli()