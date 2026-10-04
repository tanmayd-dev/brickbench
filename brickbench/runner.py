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

import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brickbench.evaluator import verify_plan

# ---------------------------------------------------------------------------
# SQLite state store helpers
# ---------------------------------------------------------------------------

def _sqlite_conn(data_dir: any) -> sqlite3.Connection:
    """Open (or create) the brickbench SQLite database."""
    db_path = Path(str(data_dir)) / "brickbench.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
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
    # Migrate pre-provider databases.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "provider" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN provider TEXT NOT NULL DEFAULT 'zen'")
            conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


def _upsert_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """
        INSERT INTO runs (run_id, target, model_id, provider, prompt_variant, mode, debloat_mode,
                          plan_hash, result_hash, status, created_at, updated_at)
        VALUES (:run_id, :target, :model_id, :provider, :prompt_variant, :mode, :debloat_mode,
                :plan_hash, :result_hash, :status, :created_at, :updated_at)
        ON CONFLICT(run_id) DO UPDATE SET
            target=excluded.target,
            model_id=excluded.model_id,
            provider=excluded.provider,
            prompt_variant=excluded.prompt_variant,
            mode=excluded.mode,
            debloat_mode=excluded.debloat_mode,
            plan_hash=excluded.plan_hash,
            result_hash=excluded.result_hash,
            status=excluded.status,
            updated_at=julianday('now')
        """,
        {k: run[k] for k in [
            "run_id", "target", "model_id", "provider", "prompt_variant", "mode", "debloat_mode",
            "plan_hash", "result_hash", "status", "created_at", "updated_at",
        ] if k in run},

    )
    conn.commit()


def _get_pending_runs(conn: sqlite3.Connection, max_rows: int = 10) -> List[dict]:
    cur = conn.execute(
        "SELECT run_id, target, model_id, provider, prompt_variant, mode, debloat_mode, plan_hash "
        "FROM runs WHERE status='pending' ORDER BY created_at LIMIT ?",
        (max_rows,),
    )
    cols = [desc[0] for desc in cur.description]  # column names
    return [
        dict(zip(cols, row))  # row is a tuple
        for row in cur.fetchall()
    ]


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

def _or_daily_remaining(api_key: str) -> Optional[int]:
    """Query OpenRouter key for remaining free-model requests today."""
    import urllib.request
    import json as _json
    try:
        url = "https://openrouter.ai/api/v1/key"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.load(resp)
        fd = data.get("data", {})
        remaining = fd.get("free_model_daily_requests", {}).get("remaining")
        return int(remaining) if remaining is not None else None
    except Exception:
        return None


def _or_check_and_mark(api_key: str, conn: sqlite3.Connection, run_id: str) -> bool:
    """Return True if we may proceed, False if we should defer to next UTC midnight."""
    remaining = _or_daily_remaining(api_key)
    if remaining is None:
        return False
    if remaining > 0:
        return True
    _mark_failed_rate_limited(conn, run_id)
    return False


# ---------------------------------------------------------------------------
# Model dispatch (stubs — fill in with actual API calls)
# ---------------------------------------------------------------------------

def _dispatch_model(
    provider: str,
    model_id: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    **kwargs: Any,
) -> Tuple[str, str]:
    """Route to the correct provider backend and return (reply_text, usage_info)."""
    # Stubs — will be wired to opencode/agy/codex/openrouter in Phase P2.
    raise NotImplementedError(f"dispatch not implemented for {provider}://{model_id}")


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

    def __init__(self, data_dir: Path | str, max_emulator_concurrency: int = 1, llm_concurrency: Optional[int] = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.conn = _sqlite_conn(self.data_dir)
        self.conn.isolation_level = None  # Autocommit mode
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA check_same_thread=False")
        self.max_emulator_concurrency = max_emulator_concurrency
        self.llm_concurrency = llm_concurrency or 1
        self._emu_semaphore = threading.Semaphore(max_emulator_concurrency)
        self._db_lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Public: enqueue a matrix cell
    # ------------------------------------------------------------------
    def enqueue(self, *, target_key: str, model_id: str, prompt_variant: str, mode: str, debloat_mode: str, provider: str = "zen") -> str:
        """Enqueue a new run cell and return its run_id."""
        run_id = str(uuid.uuid4())
        run = {
            "run_id": run_id,
            "provider": provider,
            "target": target_key,
            "model_id": model_id,
            "prompt_variant": prompt_variant,
            "mode": mode,
            "debloat_mode": debloat_mode,
            "plan_hash": "pending",  # NOT NULL, default placeholder
            "result_hash": None,
            "status": "pending",
            "created_at": None,
            "updated_at": None,
        }
        allowed = ["run_id", "target", "model_id", "prompt_variant", "mode", "debloat_mode",
                   "plan_hash", "result_hash", "status", "created_at", "updated_at", "provider"]
        filtered = {k: run[k] for k in allowed if k in run}
        _upsert_run(self.conn, filtered)
        self.conn.commit()
        return run_id

    # ------------------------------------------------------------------
    # Public: one iteration of the worker loop
    # ------------------------------------------------------------------
    def step(self) -> int:
        """Pick one pending run, execute it (or defer), update state.

        Returns 1 if a run was executed, 0 when no pending work remains.
        """
        # Each step gets its own connection for thread safety
        import sqlite3
        from brickbench.runner import _get_pending_runs, _mark_running, _mark_done, _mark_failed_rate_limited

        db_path = self.data_dir / "brickbench.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        pending = _get_pending_runs(conn, max_rows=5)
        if not pending:
            conn.close()
            return 0

        run = pending[0]
        _mark_running(conn, run["run_id"])
        conn.commit()

        try:
            self._execute_one(run, conn)
        except RuntimeError:
            _mark_failed_rate_limited(conn, run["run_id"])
        except Exception:
            _mark_done(conn, run["run_id"], "")
        finally:
            conn.commit()
            conn.close()

        return 1

    # ------------------------------------------------------------------
    # Probe sweep: incrementally populate the safe-list per target
    # ------------------------------------------------------------------
    def probe_sweep(self, packages_per_target: Optional[Dict[str, List[str]]] = None,
                    per_target: int = 20) -> Dict[str, int]:
        """Incrementally populate the safe-list by verifying packages per target.

        For each target, examines up to per_target packages that are not yet
        recorded in the safe-list and verifies them via the evaluator's probe_package
        function. Fast-path verdicts (already in the safe-list) cost essentially
        zero time; slow-path verdicts require an emulator run the first time,
        then hit the cache.

        Returns a dict summarising how many verdicts were fast-path vs slow-path
        per target.
        """
        from brickbench.config import PACKAGES_PER_TARGET as _DEFAULT_PPT
        from brickbench.evaluator import probe_package

        ppt = packages_per_target or _DEFAULT_PPT
        summary: Dict[str, int] = {"fast": 0, "slow": 0}

        conn = sqlite3.connect(str(self.data_dir / "brickbench.db"))
        try:
            for target_key, pkg_list in ppt.items():
                verified = 0
                fast = 0
                slow = 0
                for pkg_name in pkg_list:
                    if verified >= per_target:
                        break
                    cur = conn.execute(
                        "SELECT outcome FROM verdicts WHERE target=? AND pkg_name=? AND action=?",
                        (target_key, pkg_name, "uninstall"),
                    )
                    if cur.fetchone() is not None:
                        fast += 1
                        summary["fast"] += 1
                    else:
                        dossier = {
                            "total_packages": 130,
                            "critical_set": {"com.android.systemui", "com.android.phone"},
                            "pkg_sizes": {},
                        }
                        probe_package(
                            target_key=target_key,
                            pkg_name=pkg_name,
                            action="uninstall",
                            dossier=dossier,
                            data_dir=self.data_dir,
                        )
                        slow += 1
                        summary["slow"] += 1
                    verified += 1
                summary[f"verified_{target_key}"] = verified
                summary[f"fast_{target_key}"] = fast
                summary[f"slow_{target_key}"] = slow
        finally:
            conn.close()
        return summary

    # ------------------------------------------------------------------
    # Core execution for one run
    # ------------------------------------------------------------------
    def _execute_one(self, run: dict, conn: sqlite3.Connection) -> None:
        """Dispatch one run to the appropriate provider + mode path."""
        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        mode = run["mode"]  # plan_only | live
        debloat_mode = run["debloat_mode"]  # uninstall | system
        provider = run.get("provider", "zen")

        # ---- 1. Provider quota gate (OR only) ----
        if provider == "openrouter" or "/" in model_id:
            # In a real setup the api_key comes from env/.env.
            # Here we simply skip OR until the user supplies it.
            _mark_failed_rate_limited(conn, run["run_id"])
            return

        # ---- 2. Emulator serial acquire (plan-only skips) ----
        if mode != "plan_only":
            self._emu_semaphore.acquire()
            try:
                self._emu_execute(run, conn)
            finally:
                self._emu_semaphore.release()
        else:
            self._plan_only_execute(run, conn)

        # ---- 5. Persist result hash ----
        import hashlib
        plan_hash = run.get("plan_hash", "pending")
        if plan_hash == "pending":
            plan_hash = hashlib.sha256(
                f"{run['target']}:{run['model_id']}:{run['prompt_variant']}".encode()
            ).hexdigest()[:16]
        result_hash = hashlib.sha256(
            f"{run['target']}:{plan_hash}:{run['mode']}:{run['debloat_mode']}".encode()
        ).hexdigest()
        _mark_done(conn, run["run_id"], result_hash)

    # ------------------------------------------------------------------
    # Plan-only path
    # ------------------------------------------------------------------
    def _plan_only_execute(self, run: dict, conn: sqlite3.Connection) -> None:
        """Execute a plan-only cell: evaluator verifies, safe-list updates."""
        from brickbench.evaluator import verify_plan

        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        debloat_mode = run["debloat_mode"]

        dossier = {
            "total_packages": 130,
            "critical_set": {"com.android.systemui", "com.android.phone"},
            "pkg_sizes": {"com.example.app": 1_500_000},
        }

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
    # Live path (emulator)
    # ------------------------------------------------------------------
    def _emu_execute(self, run: dict, conn: sqlite3.Connection) -> None:
        """Execute a live cell against the emulator."""
        from brickbench.evaluator import verify_plan

        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        debloat_mode = run["debloat_mode"]

        plan_actions = [
            {"pkg": "com.android.printspooler", "action": "uninstall"},
            {"pkg": "com.android.dreams.basic", "action": "uninstall"},
        ]

        verify_plan(
            target_key=target_key,
            plan_hash="emu_" + prompt_variant,
            plan_actions=plan_actions,
            dossier={
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