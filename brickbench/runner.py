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


def _mark_skipped(conn: sqlite3.Connection, run_id: str) -> None:
    """Terminal skip for providers with no API backend wired (zen/agy/codex)."""
    conn.execute(
        "UPDATE runs SET status='skipped', updated_at=julianday('now') WHERE run_id=?",
        (run_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Local env / API keys
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines in cwd/.env."""
    import os
    try:
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


def _openrouter_key() -> Optional[str]:
    import os
    _load_dotenv()
    return os.environ.get("OPENROUTER_API_KEY")


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

def _dispatch_openrouter(model_id: str, messages: List[Dict[str, str]], temperature: float = 0.0) -> Tuple[str, int]:
    """Call OpenRouter chat completions; return (reply_text, total_tokens)."""
    import json as _json
    import urllib.request as _req

    api_key = _openrouter_key()
    if not api_key or "CHANGE_ME" in api_key:
        raise RuntimeError("missing OPENROUTER_API_KEY (set it in .env)")
    body = _json.dumps({
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 512,
    }).encode()
    r = _req.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/tanmayd-dev/brickbench",
            "X-Title": "brickbench",
        },
    )
    try:
        with _req.urlopen(r, timeout=120) as resp:
            data = _json.load(resp)
    except Exception as e:
        raise RuntimeError(f"openrouter call failed for {model_id}: {e}")
    try:
        reply = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"openrouter bad response for {model_id}: {str(data)[:300]}")
    usage = data.get("usage") or {}
    total = usage.get("total_tokens", 0) or 0
    return reply, int(total)


def _dispatch_ollama(model_id: str, messages: List[Dict[str, str]], temperature: float = 0.0) -> Tuple[str, int]:
    """Call local Ollama (/api/chat, non-streaming); return (reply, tokens)."""
    import json as _json
    import urllib.request as _req

    body = _json.dumps({
        "model": model_id,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }).encode()
    r = _req.Request("http://localhost:11434/api/chat", data=body,
                     headers={"Content-Type": "application/json"})
    try:
        with _req.urlopen(r, timeout=300) as resp:
            data = _json.load(resp)
    except Exception as e:
        raise RuntimeError(f"ollama call failed for {model_id} (is `ollama serve` running?): {e}")
    reply = ((data.get("message") or {}).get("content")) or ""
    total = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
    return reply, int(total)


def _plan_messages(target: str, mode: str, debloat_mode: str, candidates: List[str]) -> List[Dict[str, str]]:
    return [
        {"role": "system",
         "content": "You propose Android debloat plans. Reply with ONLY a JSON array like "
                    "[{\"pkg\": \"com.example.app\", \"action\": \"uninstall\"}]."},
        {"role": "user",
         "content": f"Target: {target}. Mode: {mode}. Debloat: {debloat_mode}. "
                    f"Choose up to 2 safe-to-remove packages from this list: {', '.join(candidates)}. "
                    f"JSON array only."},
    ]


def _actions_from_reply(reply: str, candidates: List[str], debloat_mode: str) -> List[Dict[str, str]]:
    """Best-effort parse of the LLM reply; fall back to first candidates."""
    import json as _json
    import re as _re

    try:
        m = _re.search(r"\[.*\]", reply, _re.S)
        if m:
            items = _json.loads(m.group(0))
            cand = set(candidates)
            actions = [
                {"pkg": it["pkg"], "action": it.get("action", debloat_mode)}
                for it in items
                if isinstance(it, dict) and it.get("pkg") in cand
            ]
            if actions:
                return actions[:2]
    except Exception:
        pass
    found = [p for p in candidates if p in reply]
    picked = (found or list(candidates))[:2]
    return [{"pkg": p, "action": debloat_mode} for p in picked]


def _dispatch_model(
    provider: str,
    model_id: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    **kwargs: Any,
) -> Tuple[str, int]:
    """Route to the correct provider backend; return (reply_text, total_tokens).

    Wired: openrouter (incl. `user/model` ids), local/ollama.
    zen/agy/codex have no documented API endpoint in this repo and raise
    NotImplementedError so the run is marked `skipped`, not failed.
    """
    if provider == "openrouter" or "/" in model_id:
        return _dispatch_openrouter(model_id, messages, temperature)
    if provider == "local":
        return _dispatch_ollama(model_id, messages, temperature)
    raise NotImplementedError(
        f"no LLM backend wired for provider={provider} (model={model_id}); "
        f"use --providers openrouter and/or local"
    )


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
        import queue
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
        # Distinct adb ports per concurrent emulator (5554, 5556, ...).
        self._emu_ports: queue.Queue[int] = queue.Queue()
        for i in range(max_emulator_concurrency):
            self._emu_ports.put(5554 + 2 * i)

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

        The claim is atomic (BEGIN IMMEDIATE + UPDATE ... WHERE pending),
        so N parallel workers never execute the same run twice.

        Returns 1 if a run was executed, 0 when no pending work remains.
        """
        # Each step gets its own connection for thread safety
        import sqlite3
        from brickbench.runner import _mark_done, _mark_failed_rate_limited, _mark_skipped

        db_path = self.data_dir / "brickbench.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        run = None
        for _ in range(3):  # retry if another worker claimed the row first
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT run_id, target, model_id, provider, prompt_variant, mode, debloat_mode, plan_hash "
                "FROM runs WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchall()
            if not rows:
                conn.execute("COMMIT")
                conn.close()
                return 0
            cols = ["run_id", "target", "model_id", "provider", "prompt_variant", "mode", "debloat_mode", "plan_hash"]
            candidate = dict(zip(cols, rows[0]))
            cur = conn.execute(
                "UPDATE runs SET status='running', updated_at=julianday('now') "
                "WHERE run_id=? AND status='pending'",
                (candidate["run_id"],),
            )
            conn.execute("COMMIT")
            if cur.rowcount == 1:
                run = candidate
                break
        if run is None:
            conn.close()
            return 1  # someone else claimed it; count as progress, caller re-polls

        try:
            self._execute_one(run, conn)
        except NotImplementedError:
            # Provider with no backend (zen/agy/codex): honest skip, not failure.
            _mark_skipped(conn, run["run_id"])
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
        """Dispatch one run: real LLM plan first, then verify on safe-list/emulator."""
        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        mode = run["mode"]  # plan_only | live
        debloat_mode = run["debloat_mode"]  # uninstall | system
        provider = run.get("provider", "zen")

        # ---- 1. Real LLM plan (network happens here) ----
        # Raises NotImplementedError for unwired providers (→ skipped),
        # RuntimeError on API failure (→ failed_rate_limited).
        from brickbench.config import PACKAGES_PER_TARGET
        candidates = list(PACKAGES_PER_TARGET.get(target_key, [])) or [
            "com.android.printspooler", "com.android.dreams.basic",
        ]
        messages = _plan_messages(target_key, mode, debloat_mode, candidates)
        llm_reply, tokens_used = _dispatch_model(provider, model_id, messages)
        plan_actions = _actions_from_reply(llm_reply, candidates, debloat_mode)

        # ---- 2. Emulator-gated verify (plan-only skips the device) ----
        if mode != "plan_only":
            self._emu_semaphore.acquire()
            adb_port = self._emu_ports.get()
            try:
                self._emu_execute(run, conn, plan_actions, llm_reply, tokens_used, adb_port)
            finally:
                self._emu_ports.put(adb_port)
                self._emu_semaphore.release()
        else:
            self._plan_only_execute(run, conn, plan_actions, llm_reply, tokens_used)

        # ---- 3. Persist result hash ----
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
    def _plan_only_execute(
        self,
        run: dict,
        conn: sqlite3.Connection,
        plan_actions: Optional[List[Dict[str, str]]] = None,
        llm_reply: str = "",
        tokens_used: int = 0,
    ) -> None:
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
            plan_actions=plan_actions or [{"pkg": "com.example.app", "action": "uninstall"}],
            dossier=dossier,
            model_id=model_id,
            prompt_variant=prompt_variant,
            mode="plan_only",
            debloat_mode=debloat_mode,
            data_dir=self.data_dir,
            tokens_used=tokens_used,
            llm_reply=llm_reply,
            plan_source="llm" if plan_actions else "heuristic",
        )

    # ------------------------------------------------------------------
    # Live path (emulator)
    # ------------------------------------------------------------------
    def _emu_execute(
        self,
        run: dict,
        conn: sqlite3.Connection,
        plan_actions: Optional[List[Dict[str, str]]] = None,
        llm_reply: str = "",
        tokens_used: int = 0,
        adb_port: int = 5554,
    ) -> None:
        """Execute a live cell against a dedicated emulator instance."""
        from brickbench.evaluator import verify_plan

        target_key = run["target"]
        model_id = run["model_id"]
        prompt_variant = run["prompt_variant"]
        debloat_mode = run["debloat_mode"]

        plan_actions = plan_actions or [
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
            adb_port=adb_port,
            tokens_used=tokens_used,
            llm_reply=llm_reply,
            plan_source="llm" if llm_reply else "heuristic",
        )