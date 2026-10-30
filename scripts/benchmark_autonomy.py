"""Non-promotable offline evaluator for autonomy routing experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "fixtures" / "autonomy" / "cases.json"
ROUTING_POLICY = ROOT / "policies" / "model-routing.json"
MEASUREMENT_SCOPE = "model_process_and_external_fixture_verifier_only"


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = (
        document.get("cases")
        if isinstance(document, dict) and document.get("schema_version") == 1
        else None
    )
    required = {"case_id", "task_type", "prompt", "files"}
    if not isinstance(cases, list) or len(cases) != 8:
        raise ValueError("exactly eight fixture cases are required")
    if any(
        not isinstance(case, dict)
        or set(case) != required
        or not case["prompt"].strip()
        or not isinstance(case["files"], dict)
        or not case["files"]
        for case in cases
    ):
        raise ValueError("invalid fixture case")
    if len({case["case_id"] for case in cases}) != 8:
        raise ValueError("fixture case ids must be unique")
    for case in cases:
        for relative, content in case["files"].items():
            _validate_fixture_entry(relative, content)
    counts = {
        kind: sum(case["task_type"] == kind for case in cases)
        for kind in (
            "mechanical",
            "read_only_exploration",
            "multifile_implementation",
            "interrupted_review_correction",
        )
    }
    if set(counts.values()) != {2}:
        raise ValueError("fixtures must contain two cases of each task type")
    return cases


def _validate_fixture_entry(relative: Any, content: Any) -> None:
    if not isinstance(relative, str) or not relative or not isinstance(content, str):
        raise ValueError("fixture names and contents must be strings")
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or relative.startswith(("/", "\\"))
        or "\\" in relative
        or ":" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
        or any(ord(char) < 32 or char in '<>"|?*' for char in relative)
        or any(PureWindowsPath(part).is_reserved() for part in posix.parts)
        or any(part.rstrip(" .") != part for part in posix.parts)
    ):
        raise ValueError(f"unsafe or platform-ambiguous fixture path: {relative!r}")


def build_command(
    executable: str, *, model: str, effort: str, sandbox: str
) -> list[str]:
    return [
        executable,
        "exec",
        "--ephemeral",
        "--sandbox",
        sandbox,
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{effort}"',
        "--json",
        "-",
    ]


def probe_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("unable to verify Codex version")
    return result.stdout.strip()


def normalize_usage(raw: Any) -> dict[str, int | None] | None:
    if not isinstance(raw, dict):
        return None
    values = [
        raw.get(key) for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    ]
    if (
        any(type(value) is not int or value < 0 for value in values)
        or values[1] > values[0]
    ):
        return None
    return {
        "uncached_input_tokens": values[0] - values[1],
        "cached_input_tokens": values[1],
        "output_tokens": values[2],
        "cost_microusd": None,
    }


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fixture_bytes(case: dict[str, Any]) -> bytes:
    return json.dumps(
        case["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _materialize(case: dict[str, Any], destination: Path) -> None:
    for relative, content in case["files"].items():
        _validate_fixture_entry(relative, content)
    destination.mkdir()
    for relative, content in case["files"].items():
        path = destination / relative
        if not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("fixture path escapes materialization root")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _status(root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("cannot capture exact source status")
    return result.stdout


def _source_fingerprint(root: Path) -> str:
    """Hash tracked and non-ignored source, including uncommitted changes.

    Runtime evidence is deliberately outside this source fingerprint. Tracked
    files remain covered even if an ignore pattern later matches them. Git's
    source inventory also avoids opening live locks, checkpoints and virtual
    environments merely to prove that source code was not changed.
    """
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("cannot inventory source files for fingerprint")
    entries: set[Path] = set()
    listed: set[Path] = set()
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        relative = Path(item.decode("utf-8"))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("source inventory escapes repository")
        entries.add(relative)
        listed.add(relative)
        entries.update(parent for parent in relative.parents if parent != Path("."))
    digest = hashlib.sha256()
    for relative in sorted(entries, key=lambda value: value.as_posix()):
        path = root / relative
        digest.update(relative.as_posix().encode("utf-8"))
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            digest.update(b"missing")
            continue
        digest.update(stat.S_IFMT(info.st_mode).to_bytes(4, "big"))
        if stat.S_ISDIR(info.st_mode) and relative in listed:
            raise RuntimeError("nested repository source requires an explicit inventory")
        if stat.S_ISLNK(info.st_mode):
            digest.update(os.readlink(path).encode("utf-8"))
        elif stat.S_ISREG(info.st_mode):
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError("source file resolves outside repository")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def output_lock(output: Path) -> Iterator[None]:
    descriptor = os.open(
        output.with_name(output.name + ".lock"),
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("output lock is not regular")
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, 0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("benchmark output lock conflict") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, 0)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _exclusive_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_final(snapshot: Path, output: Path) -> None:
    """Publish one completed snapshot without replacing an existing run."""
    try:
        os.link(snapshot, output)
    except FileExistsError as exc:
        raise RuntimeError(
            f"benchmark output already exists; choose a unique output: {output.name}"
        ) from exc


def evaluate_gate(
    result: dict[str, Any], policy: dict[str, Any] | None = None
) -> dict[str, Any]:
    minimum = (policy or json.loads(ROUTING_POLICY.read_text(encoding="utf-8")))[
        "minimum_attestation"
    ]
    runs = result.get("runs", [])
    pairs = {
        (run.get("case_id"), run.get("arm")) for run in runs if isinstance(run, dict)
    }
    expected = {
        (case["case_id"], arm)
        for case in load_cases()
        for arm in ("control", "treatment")
    }
    structurally_valid = (
        result.get("complete") is True
        and result.get("source_unchanged") is True
        and pairs == expected
        and len(runs) == len(expected)
    )
    if not structurally_valid or any(run.get("valid") is not True for run in runs):
        return {
            "exploratory_pass": False,
            "promotable": False,
            "reason": "incomplete or invalid paired run",
        }
    if any(run.get("passed") is not True for run in runs if run["arm"] == "treatment"):
        return {
            "exploratory_pass": False,
            "promotable": False,
            "reason": "treatment fixture failure",
        }
    if any(
        run["fixture_sha256"]
        != next(
            item["fixture_sha256"]
            for item in runs
            if item["case_id"] == run["case_id"] and item["arm"] != run["arm"]
        )
        for run in runs
    ):
        return {
            "exploratory_pass": False,
            "promotable": False,
            "reason": "paired fixture hash mismatch",
        }

    def aggregate(arm: str, key: str) -> float:
        return sum(run[key] for run in runs if run["arm"] == arm)

    def tokens(arm: str) -> int:
        return sum(
            sum(
                run["usage"][key]
                for key in (
                    "uncached_input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                )
            )
            for run in runs
            if run["arm"] == arm
        )

    control_tokens, treatment_tokens = tokens("control"), tokens("treatment")
    token_gain = (
        None
        if control_tokens <= 0
        else (control_tokens - treatment_tokens) * 100 / control_tokens
    )
    control_wall, treatment_wall = (
        aggregate("control", "wall_seconds"),
        aggregate("treatment", "wall_seconds"),
    )
    wall = (
        None
        if control_wall <= 0
        else (treatment_wall - control_wall) * 100 / control_wall
    )
    exploratory = (
        token_gain is not None
        and token_gain
        >= minimum["minimum_cost_or_comparable_token_improvement_percent"]
        and wall is not None
        and wall <= minimum["maximum_wall_time_regression_percent"]
    )
    return {
        "exploratory_pass": exploratory,
        "promotable": False,
        "reason": "measurement scope excludes full lifecycle orchestration",
        "token_improvement_percent": token_gain,
        "wall_time_regression_percent": wall,
        "cost_improvement_percent": None,
    }


def run_offline(
    *,
    source_root: Path,
    cases_path: Path,
    verifier_path: Path,
    output: Path,
    runner: Callable[..., dict[str, Any]],
    verifier: Callable[..., bool],
    version_probe: Callable[[str], str],
    control: dict[str, str],
    treatment_models: dict[str, str],
    treatment_effort: str,
    expected_version: str,
    seed: int,
) -> dict[str, Any]:
    source_root, verifier_path, output = (
        source_root.resolve(),
        verifier_path.resolve(),
        output.resolve(),
    )
    if verifier_path.is_relative_to(source_root) or output.is_relative_to(source_root):
        raise ValueError("verifier and output must be outside source workspace")
    cases = load_cases(cases_path)
    verifier_initial = _hash_bytes(verifier_path.read_bytes())
    actual_version = version_probe(control["executable"])
    if actual_version != expected_version:
        raise RuntimeError("Codex version mismatch")
    missing = {case["task_type"] for case in cases} - set(treatment_models)
    if missing:
        raise ValueError("missing per-type treatment model")
    output.parent.mkdir(parents=True, exist_ok=True)
    before = _status(source_root)
    fingerprint_before = _source_fingerprint(source_root)
    progress = output.with_name(output.name + ".progress")
    if output.exists() or progress.exists():
        raise RuntimeError(
            "benchmark output run already exists; choose a unique output"
        )
    progress.mkdir()
    result = {
        "schema_version": 1,
        "complete": False,
        "promotable": False,
        "measurement_scope": MEASUREMENT_SCOPE,
        "limitations": [
            "Full lifecycle checkpoint, review, intervention, and native orchestration measurements are unavailable."
        ],
        "seed": seed,
        "warmups_per_arm": 2,
        "codex_version": actual_version,
        "tier": "unverified_unchanged_not_overridden",
        "authority_verifier_sha256": verifier_initial,
        "progress_directory": progress.name,
        "latest_snapshot": None,
        "source_status_before": before,
        "source_fingerprint_before": fingerprint_before,
        "functional_replay": {
            "available": False,
            "reason": "no public v1/v2 replay fixture is executed",
        },
        "runs": [],
    }
    with output_lock(output):
        snapshot_number = 0

        def snapshot() -> Path:
            nonlocal snapshot_number
            snapshot_number += 1
            name = f"{snapshot_number:04d}.json"
            result["latest_snapshot"] = name
            path = progress / name
            _exclusive_json(path, result)
            return path

        snapshot()
        try:
            order = list(cases)
            random.Random(seed).shuffle(order)
            for arm in ("control", "treatment"):
                for case in order[:2]:
                    with tempfile.TemporaryDirectory() as temporary:
                        workspace = Path(temporary) / arm
                        _materialize(case, workspace)
                        model = (
                            control["model"]
                            if arm == "control"
                            else treatment_models[case["task_type"]]
                        )
                        runner(
                            command=build_command(
                                control["executable"],
                                model=model,
                                effort=control["effort"]
                                if arm == "control"
                                else treatment_effort,
                                sandbox="read-only"
                                if case["task_type"] == "read_only_exploration"
                                else "workspace-write",
                            ),
                            prompt=case["prompt"],
                            cwd=workspace,
                            warmup=True,
                        )
            for case in order:
                expected_fixture = _hash_bytes(_fixture_bytes(case))
                pair = {}
                for arm in ("control", "treatment"):
                    with tempfile.TemporaryDirectory() as temporary:
                        workspace = Path(temporary) / arm
                        _materialize(case, workspace)
                        pair[arm] = _tree_hash(workspace)
                        model = (
                            control["model"]
                            if arm == "control"
                            else treatment_models[case["task_type"]]
                        )
                        effort = (
                            control["effort"] if arm == "control" else treatment_effort
                        )
                        start = time.monotonic()
                        raw = runner(
                            command=build_command(
                                control["executable"],
                                model=model,
                                effort=effort,
                                sandbox="read-only"
                                if case["task_type"] == "read_only_exploration"
                                else "workspace-write",
                            ),
                            prompt=case["prompt"],
                            cwd=workspace,
                            warmup=False,
                        )
                        verified = verifier(
                            verifier_path=verifier_path, workspace=workspace, case=case
                        )
                        wall = time.monotonic() - start
                        usage = normalize_usage(raw.get("usage"))
                        valid = (
                            raw.get("valid") is True
                            and raw.get("returned_model") == model
                            and usage is not None
                            and isinstance(wall, float)
                            and math.isfinite(wall)
                            and wall >= 0
                        )
                        result["runs"].append(
                            {
                                "case_id": case["case_id"],
                                "task_type": case["task_type"],
                                "arm": arm,
                                "fixture_sha256": pair[arm],
                                "fixture_definition_sha256": expected_fixture,
                                "model": model,
                                "effort": effort,
                                "valid": valid,
                                "passed": bool(
                                    valid and raw.get("passed") is True and verified
                                ),
                                "usage": usage,
                                "wall_seconds": wall,
                                "retries": None,
                                "timeouts": None,
                                "interventions": None,
                                "review_measurement": None,
                            }
                        )
                        snapshot()
            result["complete"] = True
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            result["source_status_after"] = _status(source_root)
            result["source_fingerprint_after"] = _source_fingerprint(source_root)
            result["source_unchanged"] = (
                result["source_status_after"] == before
                and result["source_fingerprint_after"] == fingerprint_before
            )
            result["verifier_unchanged"] = (
                _hash_bytes(verifier_path.read_bytes()) == verifier_initial
            )
            if result["complete"] and result["verifier_unchanged"]:
                result["gate"] = evaluate_gate(result)
            else:
                result["gate"] = {
                    "exploratory_pass": False,
                    "promotable": False,
                    "reason": "incomplete run or verifier mutation",
                }
            final_snapshot = snapshot()
            _publish_final(final_snapshot, output)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preflight the non-promotable autonomy evaluator."
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    if args.preflight == args.run:
        parser.error("choose exactly one mode")
    if args.run:
        parser.error(
            "live execution remains unsupported until tier and full lifecycle measurement are verified"
        )
    version = probe_version(args.codex)
    ready = (
        version == args.expected_version
        and len(load_cases(args.cases)) == 8
        and not args.verifier.resolve().is_relative_to(args.source_root.resolve())
        and not args.output.resolve().is_relative_to(args.source_root.resolve())
    )
    print(
        json.dumps(
            {
                "ready": ready,
                "codex_version": version,
                "live_calls": False,
                "promotable": False,
            }
        )
    )
    return 0 if ready else 4


if __name__ == "__main__":
    raise SystemExit(main())
