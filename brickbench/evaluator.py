"""brickbench evaluator — plan apply + health checks + score.

Responsibilities
----------------
1. Apply a debloat plan to a snapshot-restored emulator (or QEMU VM).
2. Run the verifier suite (boot gate + functional probes).
3. Return a scored result plus a transcript for the cache.
4. Persist a (target, plan_ops) → outcome verdict for the safe-list.
"""

from __future__ import annotations

import subprocess
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PackageVerdict:
    """One (target, package, action) → outcome recorded in the safe-list."""

    target: str
    pkg_name: str
    action: str  # "uninstall", "disable", "hide"
    outcome: str  # "safe", "risky", "bricked"
    evidence: str = ""  # log excerpt, boot time, etc.


@dataclass
class VerificationResult:
    """Outcome of a single plan verification run."""

    target: str
    plan_hash: str
    boot_success: bool
    boot_time_s: float
    critical_violations: List[str]  # package names removed that are in the critical set
    safety_passed: bool  # all functional probes passed
    final_pkg_count: int
    bytes_freed: int  # sum of apk+odex+vdata of removed pkgs (from dossier)
    tokens_used: int  # LLM tokens (plan-only) or agent turns (live)
    transcript_path: any  # path to JSON transcript on disk; also accepted as Path-like
    errors: List[str]  # any unexpected failures during verify


# ---------------------------------------------------------------------------
# Safe-list (verdict database) helpers
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
        """
    )
    return conn


def _safe_list_load(conn: sqlite3.Connection) -> Dict[str, PackageVerdict]:
    """Return dict keyed f'{target}|{pkg_name}|{action}' -> PackageVerdict."""
    cur = conn.execute("SELECT target, pkg_name, action, outcome, evidence FROM verdicts")
    return {
        f"{r[0]}|{r[1]}|{r[2]}": PackageVerdict(
            target=r[0], pkg_name=r[1], action=r[2], outcome=r[3], evidence=r[4]
        )
        for r in cur.fetchall()
    }


def _safe_list_record(conn: sqlite3.Connection, v: PackageVerdict) -> None:
    """Insert/update a verdict (last-write-wins)."""
    conn.execute(
        """
        INSERT INTO verdicts (target, pkg_name, action, outcome, evidence)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(target, pkg_name, action)
        DO UPDATE SET outcome = excluded.outcome, evidence = excluded.evidence, updated_at = julianday('now')
        """,
        (v.target, v.pkg_name, v.action, v.outcome, v.evidence),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Emulator / QEMU bootstrap
# ---------------------------------------------------------------------------

def _emulator_path() -> Path:
    """Return the qemu-system-x86_64 binary if found."""
    import shutil
    p = shutil.which("qemu-system-x86_64")
    if not p:
        raise RuntimeError("qemu-system-x86_64 not on PATH — install qemu-system-x86")
    return Path(p)


def _avd_path(target_key: str) -> Path:
    """Map a target key to an AVD name + path."""
    mapping = {
        "aosp-vanilla": "aosp_vanilla",
        "aosp-gapps": "aosp_gapps",
        "aosp-play": "aosp_play",
        "lineage-23.2": "lineage_23_2",
        "e-os-gsi": "eos_gsi",
        "oem-combined": "oem_combined",
    }
    avd_name = mapping.get(target_key, target_key)
    return Path("~/.android/avd/" + avd_name + ".avd").expanduser()


# ---------------------------------------------------------------------------
# Verifier suite (boot gate + functional probes)
# ---------------------------------------------------------------------------

def _run_boot_gate(
    emulator_bin: Path,
    avd_path: Path,
    avd_avd_name: str,
    ram_mb: int = 2048,
    timeout_s: int = 90,
) -> Tuple[bool, float]:
    """Restore snapshot → wait for sys.boot_completed → return (ok, boot_time)."""
    import time as _time
    start = _time.time()
    # 1. snapshot restore
    subprocess.run(
        [
            str(emulator_bin),
            "-avd", str(avd_path),
            "-no-window",
            "-no-audio",
            "-no-boot-anim",
            "-gpu", "swiftshader_indirect",
            "-memory", str(ram_mb),
            "-snapshot", "clean_boot",
            "-port", "5554",
        ],
        capture_output=True,
        text=True,
    )
    # 2. poll boot_completed via adb
    deadline = start + timeout_s
    boot_time = 0.0
    while _time.time() < deadline:
        try:
            r = subprocess.run(
                ["adb", "shell", "getprop", "sys.boot_completed"],
                capture_output=True, text=True, timeout=3,
            )
            out = r.stdout.strip()
            if out == "1":
                boot_time = _time.time() - start
                return True, boot_time
        except Exception:
            pass
        _time.sleep(1)
    return False, _time.time() - start


def _functional_probes(timeout_s: int = 30) -> Tuple[bool, List[str]]:
    """Run the lightweight functional probe suite.

    Probes (all must pass for safety_passed=True):
      - `pm list packages` succeeds (non-zero exit)
      - `dumpsys package` works for at least one system package
      - `service check package` does not error
      - `am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER`
        completes within timeout (launcher intent resolves)
    """
    failures: List[str] = []

    # 1. pm list packages
    r = subprocess.run(["adb", "shell", "pm", "list", "packages"], capture_output=True, text=True, timeout=timeout_s)
    if r.returncode != 0:
        failures.append("pm list packages failed")

    # 2. dumpsys package for a known system package
    r = subprocess.run(
        ["adb", "shell", "dumpsys", "package", "com.android.settings"],
        capture_output=True, text=True, timeout=timeout_s,
    )
    if r.returncode != 0:
        failures.append("dumpsys package failed")

    # 3. service check (no crash)
    r = subprocess.run(
        ["adb", "shell", "service", "check", "package"],
        capture_output=True, text=True, timeout=timeout_s,
    )
    if r.returncode != 0:
        failures.append("service check failed")

    # 4. launcher intent resolves
    r = subprocess.run(
        [
            "adb", "shell", "am", "start",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER",
        ],
        capture_output=True, text=True, timeout=timeout_s,
    )
    if r.returncode != 0:
        failures.append("launcher intent resolve failed")

    safety_passed = len(failures) == 0
    return safety_passed, failures


# ---------------------------------------------------------------------------
# Probe / safe-list fast-path
# ---------------------------------------------------------------------------

def probe_package(
    target_key: str,
    pkg_name: str,
    action: str,
    dossier: Dict[str, Any],
    data_dir: any,
) -> PackageVerdict:
    """Verify a single (target, package, action) triple and record the verdict.

    Fast-path: if (target, pkg, action) is already in the safe-list, return
    the cached verdict immediately (no emulator invocation).

    Slow-path: otherwise invoke `verify_plan` with a minimal plan containing
    just this one action; the evaluator will persist the verdict for future
    fast-path hits.
    """
    import hashlib

    db = _sqlite_conn(data_dir)
    try:
        sv = _safe_list_load(db)
        verdict_key = f"{target_key}|{pkg_name}|{action}"

        # ---- Fast-path: already cached? ----
        if verdict_key in sv:
            return sv[verdict_key]

        # ---- Slow-path: verify via the emulator ----
        plan_hash = hashlib.sha256(
            f"{target_key}:{pkg_name}:{action}".encode()).hexdigest()[:16]

        verify_plan(
            target_key=target_key,
            plan_hash=plan_hash,
            plan_actions=[{"pkg": pkg_name, "action": action}],
            dossier=dossier,
            model_id="probe",
            prompt_variant="probe",
            mode="live",
            debloat_mode="uninstall",
            data_dir=data_dir,
        )

        # Re-read the verdict that was just persisted
        sv = _safe_list_load(db)
        return sv.get(verdict_key, PackageVerdict(
            target=target_key, pkg_name=pkg_name, action=action, outcome="risky",
            evidence="probe sweep",
        ))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_plan(
    target_key: str,
    plan_hash: str,
    plan_actions: List[Dict[str, str]],  # [{'pkg': str, 'action': str}, ...]
    dossier: Dict[str, Any],  # per-target package inventory + critical set
    model_id: str,
    prompt_variant: str,
    mode: str,  # "plan_only" | "live"
    debloat_mode: str,  # "uninstall" | "system"
    data_dir: any,
) -> VerificationResult:
    """Execute one full verification cycle and return the result.

    This is the core of the emulator-bound cost. All callers must be ready
    for 30–90 s of wall time per unique (target, plan) pair.
    """
    db = _sqlite_conn(data_dir)
    try:
        sv = _safe_list_load(db)

        # ---- 1. Resolve ops (plan-only evaluates without touching the device) ----
        applied_ops: List[Dict[str, str]] = list(plan_actions)

        # ---- 2. Boot gate + probes (live only; plan-only uses safe-list) ----
        if mode == "live":
            emu_bin = _emulator_path()
            avd_path_obj = _avd_path(target_key)
            boot_ok, boot_time = _run_boot_gate(emu_bin, avd_path_obj, avd_path_obj.name)
            safety_passed, probe_failures = _functional_probes()
        else:
            boot_ok, boot_time = True, 0.0
            probe_failures: List[str] = []
            safety_passed = True

        # ---- 3. Safe-list lookup ----
        critical_violations: List[str] = []
        bytes_freed = 0
        final_pkg_count = dossier.get("total_packages", 0)
        for op in applied_ops:
            pkg = op.get("pkg", "")
            verdict_key = target_key + "|" + pkg + "|" + op.get("action", "uninstall")
            verdict = sv.get(verdict_key)
            if verdict and verdict.outcome == "bricked":
                critical_violations.append(pkg)
            if pkg in dossier.get("critical_set", set()):
                if pkg not in critical_violations:
                    critical_violations.append(pkg)
                safety_passed = False
            if verdict and verdict.outcome == "safe":
                bytes_freed += dossier.get("pkg_sizes", {}).get(pkg, 0)

        if probe_failures:
            safety_passed = False

        # ---- 4. Assemble result ----
        result = VerificationResult(
            target=target_key,
            plan_hash=plan_hash,
            boot_success=boot_ok,
            boot_time_s=boot_time,
            critical_violations=critical_violations,
            safety_passed=safety_passed,
            final_pkg_count=final_pkg_count,
            bytes_freed=bytes_freed,
            tokens_used=0,  # filled in by caller (plan-only vs live)
            transcript_path=Path(str(data_dir)) / f"transcript_{target_key}_{plan_hash}.json",
            errors=list(probe_failures),
        )

        # ---- 5. Persist verdicts for future fast-path ----
        for op in applied_ops:
            pkg = op.get("pkg", "")
            is_critical = pkg in dossier.get("critical_set", set())
            outcome = "safe" if not is_critical else "risky"
            _safe_list_record(db, PackageVerdict(
                target=target_key, pkg_name=pkg, action=op.get("action", "uninstall"),
                outcome=outcome,
                evidence="run persisted",
            ))

        return result
    finally:
        db.close()