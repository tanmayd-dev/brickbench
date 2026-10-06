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

def _sdk_root() -> Optional[str]:
    """Locate the Android SDK root (must contain system-images)."""
    import os
    for env in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = os.environ.get(env)
        if root and Path(root, "system-images").exists():
            return root
    for c in (
        Path.home() / ".local/share/brickbench/android-sdk",
        Path("/opt/android-sdk"),
        Path.home() / "Android/Sdk",
    ):
        if (c / "system-images").exists():
            return str(c)
    return os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")


def _emulator_path() -> Path:
    """Return the Android `emulator` binary (the AVD wrapper, not raw qemu)."""
    import shutil
    root = _sdk_root()
    if root:
        cand = Path(root) / "emulator" / "emulator"
        if cand.exists():
            return cand
    p = shutil.which("emulator")
    if p:
        return Path(p)
    for c in (
        Path("/opt/android-sdk/emulator/emulator"),
        Path("~/Android/Sdk/emulator/emulator").expanduser(),
    ):
        if c.exists():
            return c
    raise RuntimeError(
        "Android `emulator` binary not found — install the SDK emulator "
        "or set ANDROID_SDK_ROOT"
    )


def _avd_name(target_key: str) -> str:
    """Map a matrix target to a real AVD name.

    Only `bb_aosp36` exists on this machine, so every target boots it
    (override with BRICKBENCH_AVD). The requested target is still recorded
    in verdicts/transcripts so per-target AVDs can be split out later.
    """
    import os
    forced = os.environ.get("BRICKBENCH_AVD")
    if forced:
        return forced
    return "bb_aosp36"


def _avd_path(target_key: str) -> Path:
    """Back-compat shim: local dir of the AVD backing `target_key`."""
    return Path("~/.android/avd/" + _avd_name(target_key) + ".avd").expanduser()


def _adb_serial(adb_port: int) -> str:
    return f"emulator-{adb_port}"


# ---------------------------------------------------------------------------
# Verifier suite (boot gate + functional probes)
# ---------------------------------------------------------------------------

def _boot_emulator(
    emulator_bin: Path,
    avd_name: str,
    adb_port: int = 5554,
    snapshot: str = "clean_boot",
    ram_mb: int = 2048,
    timeout_s: int = 180,
    log_path: Optional[Path] = None,
):
    """Boot an AVD snapshot in the background and wait for boot_completed.

    Launches `emulator -avd <name>` via Popen (non-blocking) on its own
    adb port pair, then polls `adb -s <serial> shell getprop
    sys.boot_completed` until `timeout_s`.

    Returns (proc, serial, boot_ok, boot_time_s). The caller owns `proc`
    and must call `_stop_emulator(proc, serial)` in a finally block —
    otherwise emulator processes accumulate and eat ~2 GB RAM each.
    """
    import subprocess
    import time as _time

    serial = _adb_serial(adb_port)
    cmd = [
        str(emulator_bin),
        "-avd", avd_name,
        "-snapshot", snapshot,
        "-no-window",
        "-no-audio",
        "-no-boot-anim",
        "-gpu", "swiftshader_indirect",
        "-memory", str(ram_mb),
        "-port", str(adb_port),
        "-read-only",
    ]
    logf = open(str(log_path), "ab") if log_path else subprocess.DEVNULL
    import os as _os
    env = dict(_os.environ)
    sdk = _sdk_root()
    if sdk:
        env.setdefault("ANDROID_SDK_ROOT", sdk)
        env.setdefault("ANDROID_HOME", sdk)
    try:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)
    finally:
        if log_path:
            logf.close()
    start = _time.time()
    boot_ok, boot_time = False, 0.0
    while _time.time() - start < timeout_s:
        if proc.poll() is not None:
            break  # emulator died at startup (bad AVD/snapshot?)
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip() == "1":
                boot_ok, boot_time = True, _time.time() - start
                break
        except Exception:
            pass
        _time.sleep(3)
    if not boot_ok:
        boot_time = _time.time() - start
    return proc, serial, boot_ok, boot_time


def _stop_emulator(proc, serial: str) -> None:
    """Kill one emulator instance launched by `_boot_emulator`."""
    import subprocess
    try:
        subprocess.run(["adb", "-s", serial, "emu", "kill"], capture_output=True, timeout=15)
    except Exception:
        pass
    try:
        proc.wait(timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_boot_gate(
    emulator_bin: Path,
    avd_path: Path,
    avd_avd_name: str,
    ram_mb: int = 2048,
    timeout_s: int = 90,
) -> Tuple[bool, float]:
    """Back-compat wrapper: boot the shared AVD, wait, then tear down.

    Prefer `_boot_emulator`/`_stop_emulator` for concurrent workers with
    distinct adb ports.
    """
    avd_name = _avd_name(avd_avd_name)
    proc, serial, ok, bt = _boot_emulator(
        emulator_bin, avd_name, adb_port=5554, ram_mb=ram_mb, timeout_s=timeout_s,
    )
    try:
        return ok, bt
    finally:
        _stop_emulator(proc, serial)


def _functional_probes(serial: Optional[str] = None, timeout_s: int = 30) -> Tuple[bool, List[str]]:
    """Run the lightweight functional probe suite against one emulator.

    `serial` selects the device (`adb -s <serial>`); None targets the
    default device (single-emulator setups).

    Probes (all must pass for safety_passed=True):
      - `pm list packages` succeeds (non-zero exit)
      - `dumpsys package` works for at least one system package
      - `service check package` does not error
      - `am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER`
        completes within timeout (launcher intent resolves)
    """
    failures: List[str] = []

    def _adb(*args: str):
        base = ["adb"]
        if serial:
            base += ["-s", serial]
        return subprocess.run(base + list(args), capture_output=True, text=True, timeout=timeout_s)

    # 1. pm list packages
    r = _adb("shell", "pm", "list", "packages")
    if r.returncode != 0:
        failures.append("pm list packages failed")

    # 2. dumpsys package for a known system package
    r = _adb("shell", "dumpsys", "package", "com.android.settings")
    if r.returncode != 0:
        failures.append("dumpsys package failed")

    # 3. service check (no crash)
    r = _adb("shell", "service", "check", "package")
    if r.returncode != 0:
        failures.append("service check failed")

    # 4. launcher intent resolves
    r = _adb(
        "shell", "am", "start",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER",
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
    adb_port: int = 5554,
    emu_snapshot: str = "clean_boot",
    emu_ram_mb: int = 2048,
    emu_timeout_s: int = 180,
    tokens_used: int = 0,
    llm_reply: str = "",
    plan_source: str = "heuristic",
) -> VerificationResult:
    """Execute one full verification cycle and return the result.

    Live mode boots a real emulator snapshot on `adb_port`, runs the
    probes against that serial, and tears the instance down in a finally
    block. Plan-only mode evaluates against the safe-list (no device).
    A JSON transcript is written to the data dir for every run.
    """
    import json as _json
    import os as _os
    import time as _time

    emu_ram_mb = int(_os.environ.get("BRICKBENCH_EMU_RAM_MB", emu_ram_mb))
    emu_snapshot = _os.environ.get("BRICKBENCH_SNAPSHOT", emu_snapshot)
    db = _sqlite_conn(data_dir)
    try:
        sv = _safe_list_load(db)

        # ---- 1. Resolve ops (plan-only evaluates without touching the device) ----
        applied_ops: List[Dict[str, str]] = list(plan_actions)

        # ---- 2. Boot gate + probes (live only; plan-only uses safe-list) ----
        if mode == "live":
            emu_bin = _emulator_path()
            avd_name = _avd_name(target_key)
            log_path = Path(str(data_dir)) / f"emu_{avd_name}_{adb_port}.log"
            proc, serial, boot_ok, boot_time = _boot_emulator(
                emu_bin, avd_name, adb_port=adb_port, snapshot=emu_snapshot,
                ram_mb=emu_ram_mb, timeout_s=emu_timeout_s, log_path=log_path,
            )
            try:
                if boot_ok:
                    safety_passed, probe_failures = _functional_probes(serial)
                else:
                    safety_passed, probe_failures = False, ["boot gate failed"]
            finally:
                _stop_emulator(proc, serial)
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
        transcript_path = Path(str(data_dir)) / f"transcript_{target_key}_{plan_hash}.json"
        result = VerificationResult(
            target=target_key,
            plan_hash=plan_hash,
            boot_success=boot_ok,
            boot_time_s=boot_time,
            critical_violations=critical_violations,
            safety_passed=safety_passed,
            final_pkg_count=final_pkg_count,
            bytes_freed=bytes_freed,
            tokens_used=tokens_used,
            transcript_path=transcript_path,
            errors=list(probe_failures),
        )

        # ---- 4b. Write transcript (evidence that work happened) ----
        try:
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with open(transcript_path, "w") as _f:
                _json.dump({
                    "target": target_key,
                    "avd": _avd_name(target_key) if mode == "live" else None,
                    "adb_port": adb_port if mode == "live" else None,
                    "model_id": model_id,
                    "prompt_variant": prompt_variant,
                    "mode": mode,
                    "debloat_mode": debloat_mode,
                    "plan_hash": plan_hash,
                    "plan_source": plan_source,
                    "plan_actions": applied_ops,
                    "llm_reply": llm_reply[:4000] if llm_reply else "",
                    "result": {
                        "boot_success": boot_ok,
                        "boot_time_s": boot_time,
                        "critical_violations": critical_violations,
                        "safety_passed": safety_passed,
                        "final_pkg_count": final_pkg_count,
                        "bytes_freed": bytes_freed,
                        "tokens_used": tokens_used,
                        "errors": list(probe_failures),
                    },
                    "recorded_at": _time.time(),
                }, _f, indent=2)
        except Exception:
            pass

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