"""Run local tests with owned scratch space and bounded process lifetime."""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from common import active_codex_home, atomic_write_json, sha256
from maintenance import safe, tree
from mcp_lifecycle import WindowsJob, stop
from workload import work_slot
from storage_lifecycle import Guard, snapshot, remove

OWNER = "codex-test-run-v1"
DAY = 86400


class ActiveRun(RuntimeError):
    pass


@contextlib.contextmanager
def lease(path):
    """A crashed runner releases its kernel lock without inventing a stale PID."""
    with path.open("a+b") as guard:
        # Windows byte-range locks also prohibit reading another runner's byte.
        if os.fstat(guard.fileno()).st_size == 0:
            guard.write(b"0")
            guard.flush()
        guard.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                raise ActiveRun("runner still holds its lease") from exc
            raise
        yield guard


def areas(home):
    state = safe(home / "maintenance", home)
    state.mkdir(parents=True, exist_ok=True)
    root = safe(home / "tmp/test-runs", home)
    logs = safe(home / "maintenance/test-logs", home)
    root.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    safe(root, home)
    safe(logs, home)
    return root, logs


def storage_guard(home):
    local = home/"maintenance/local.json"
    data = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    return Guard(home, [*data.get("pinned_worktrees", []), *data.get("storage", {}).get("pinned", [])], data.get("storage", {}).get("protected", []))


def remove_owned(path, home):
    # Serialize only deletion; new runs and their test processes remain parallel.
    guard = safe(home / "maintenance/test-cleanup.lock", home)
    deadline = time.monotonic() + 60
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(lease(guard))
                break
            except ActiveRun:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another test cleanup still holds the deletion lock")
                time.sleep(0.05)
        if not os.path.lexists(path):
            return  # Another session has already cleaned this finished run.
        root = home/"tmp/test-runs" if path.is_relative_to(home/"tmp/test-runs") else home/"maintenance/test-runs"
        safe(path, root)
        guard = storage_guard(home)
        guard.check(path, root)
        tree(path, home)  # A nested junction or unreadable child blocks removal.
        info = snapshot(path, root, guard)
        remove(path, root, guard, info.fingerprint)


def sweep(home, apply=False, now=None, _legacy=False):
    now = time.time() if now is None else now
    root, logs = areas(home)
    if _legacy:
        root = home/"maintenance/test-runs"
        if not root.exists():
            return 0
    failures = 0
    for path in root.iterdir():
        if len(path.name) != 32 or any(c not in "0123456789abcdef" for c in path.name):
            continue
        try:
            storage_guard(home).check(path, root)
            marker = safe(path / "run.json", root)
            value = json.loads(marker.read_text(encoding="utf-8"))
            if value.get("owner") != OWNER or value.get("id") != path.name:
                continue
            successful = value.get("state") == "finished" and value.get("exit_code") == 0
            if not successful and now - value["started"] <= 3 * DAY:
                continue  # Task-end hooks never walk a fresh/active run's payload.
            with lease(safe(path / "run.lock", root)):
                if os.name != "nt" and value.get("state") == "running" and value.get("process_group"):
                    try:
                        os.killpg(value["process_group"], 0)
                    except ProcessLookupError:
                        pass
                    else:
                        continue
                info = snapshot(path, root, storage_guard(home))
                if info.pinned:
                    continue
                latest = info.latest
                expired = now - max(latest, value["started"]) > 3 * DAY
                eligible = successful or expired
            if eligible:
                print(json.dumps({"test_run": path.name, "action": "remove" if apply else "eligible"}), flush=True)
                if apply:
                    log = safe(logs / (path.name + ".log"), logs)
                    receipt = safe(log.with_suffix(".json"), logs)
                    if log.exists() and not receipt.exists():
                        # A hard-killed runner never reached normal finalization.
                        atomic_write_json(receipt, {**value, "state": "abandoned", "exit_code": 125,
                                                    "finished": now, "log_sha256": sha256(log)})
                    remove_owned(path, home)
        except FileNotFoundError:
            pass  # Another finished-run cleanup already removed it.
        except ActiveRun as exc:
            print(json.dumps({"test_run": path.name, "action": "active", "error": str(exc)}), flush=True)
        except (OSError, ValueError, KeyError) as exc:
            failures += 1
            print(json.dumps({"test_run": path.name, "action": "blocked", "error": str(exc)}), flush=True)
    for receipt in logs.glob("*.json"):
        try:
            storage_guard(home).check(receipt, logs)
            data = json.loads(receipt.read_text(encoding="utf-8"))
            if data.get("owner") != OWNER or receipt.stem != data.get("id") or now - data["finished"] <= 30 * DAY:
                continue
            log = safe(logs / (receipt.stem + ".log"), logs)
            guard = storage_guard(home)
            if snapshot(receipt, logs, guard).pinned or (log.exists() and snapshot(log, logs, guard).pinned):
                continue
            if log.exists() and sha256(log) != data["log_sha256"]:
                raise ValueError("test log changed; retained")
            if apply:
                log.unlink(missing_ok=True)
                receipt.unlink()
        except (OSError, ValueError, KeyError) as exc:
            failures += 1
            print(json.dumps({"test_log": receipt.name, "action": "blocked", "error": str(exc)}), flush=True)
    if not _legacy and (home/"maintenance/test-runs").exists():
        failures += sweep(home, apply, now, _legacy=True)
    return 1 if failures else 0


def execute(home, command, timeout, pytest_args=None, heavy=True):
    if not command or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("a command and positive finite timeout are required")
    if command[1:3] in (["-m", "pytest"], ["-m", "unittest"]):
        heavy = True
    with work_slot(home, heavy=heavy, wait=timeout):
        return _execute(home, command, timeout, pytest_args)


def _execute(home, command, timeout, pytest_args=None):
    if not command or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("a command and positive finite timeout are required")
    if pytest_args is None and len(command) >= 3 and command[1:3] == ["-m", "pytest"]:
        command, pytest_args = command[:3], command[3:]
    if pytest_args is not None:
        if any(x.startswith(("--basetemp", "-o", "--override-ini")) for x in pytest_args):
            raise ValueError("pytest scratch/config overrides belong to the wrapper")
    root, logs = areas(home)
    run_id = uuid.uuid4().hex
    path = safe(root / run_id, root)
    path.mkdir()
    payload = path / "scratch"
    payload.mkdir()
    value = {"owner": OWNER, "id": run_id, "started": time.time(), "state": "running"}
    atomic_write_json(path / "run.json", value)
    env = dict(os.environ, TEMP=str(payload), TMP=str(payload), TMPDIR=str(payload),
               PYTHONDONTWRITEBYTECODE="1", CODEX_TEST_SCRATCH=str(payload),
               COVERAGE_FILE=str(payload / ".coverage"), MYPY_CACHE_DIR=str(payload / "mypy"),
               RUFF_CACHE_DIR=str(payload / "ruff"))
    if pytest_args is not None:
        command = [*command, "--basetemp", str(payload / "pytest"), "-o", "cache_dir=" + str(payload / "pytest-cache"), *pytest_args]
    value.update(command=command, cwd=str(Path.cwd()), timeout_seconds=timeout)
    atomic_write_json(path / "run.json", value)
    log = safe(logs / (run_id + ".log"), logs)
    process = job = None
    code = 125
    cleanup_error = None
    print(json.dumps({"test_run": run_id, "scratch": str(payload), "log": str(log), "command": command}), flush=True)
    with lease(path / "run.lock"):
        try:
            with log.open("xb") as stream:
                try:
                    if os.name == "nt":
                        # A sandbox child inherits current-user access from the run root.
                        sid = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True, timeout=10).strip().split(",")[-1].strip('"')
                        subprocess.run(["icacls", str(path), "/grant", "*" + sid + ":(OI)(CI)F", "/Q"], check=True, stdout=stream, stderr=subprocess.STDOUT, timeout=15)
                    process = subprocess.Popen(command, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                               start_new_session=os.name != "nt", creationflags=0x4 if os.name == "nt" else 0)
                    value["process_group"] = process.pid
                    atomic_write_json(path / "run.json", value)
                    job = WindowsJob(process)
                    job.resume(process)
                    try:
                        code = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        code = 124
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    code = 125
                    value["error"] = str(exc)
                    stream.write(("HARNESS_ERROR: " + str(exc) + "\n").encode("utf-8"))
                finally:
                    if job:
                        job.close()
                    if process:
                        stop(process)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if job:
                job.close()
            if process and process.poll() is None:
                stop(process)
            value.update(state="finished", exit_code=code, finished=time.time())
            atomic_write_json(path / "run.json", value)
    if log.exists():
        atomic_write_json(log.with_suffix(".json"), {**value, "log_sha256": sha256(log)})
        with log.open("rb") as stream:
            shutil.copyfileobj(stream, sys.stdout.buffer)
        sys.stdout.flush()
    if code == 0:
        try:
            remove_owned(path, home)
        except (OSError, ValueError) as exc:
            cleanup_error = str(exc)
    print(json.dumps({"test_run": run_id, "exit_code": code, "scratch_removed": not path.exists(),
                      "cleanup_error": cleanup_error}), flush=True)
    return 2 if cleanup_error else code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--pytest", action="store_true")
    parser.add_argument("--light", action="store_true", help="general work: max three processes, no full-suite overlap permission")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    home = active_codex_home()
    if args.clean:
        return sweep(home, args.apply)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.timeout is None:
        parser.error("--timeout is required")
    if args.pytest:
        if args.light:
            parser.error("pytest uses the heavy I/O slot")
        return execute(home, [sys.executable, "-m", "pytest"], args.timeout, pytest_args=command)
    return execute(home, command, args.timeout, heavy=not args.light)


if __name__ == "__main__":
    raise SystemExit(main())
