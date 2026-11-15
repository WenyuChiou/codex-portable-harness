"""Install the portable global Codex rules without touching user configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from typing import Any

from bootstrap import _reject_symlinks
from common import REPO_ROOT, active_codex_home, atomic_write_json, sha256
from workload import lock as lease


SOURCE = REPO_ROOT / "templates" / "AGENTS.md"
TARGET_RELATIVE = Path("AGENTS.md")
MANIFEST_RELATIVE = Path("maintenance") / "global-rules-ownership.json"
OWNER = "codex-portable-harness-global-rules"


def _manifest(home: Path) -> tuple[dict[str, Any], str | None]:
    path = home / MANIFEST_RELATIVE
    _reject_symlinks(path, home)
    if not path.exists():
        return {"schema_version": 1, "owner": OWNER, "files": {}}, None
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid global-rules ownership manifest: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("owner") != OWNER
        or not isinstance(value.get("files"), dict)
        or set(value["files"]) - {TARGET_RELATIVE.as_posix()}
    ):
        raise ValueError(f"invalid global-rules ownership manifest: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def plan(home: Path) -> dict[str, Any]:
    """Return exact source, target, and manifest pins without writing."""
    home = Path(os.path.abspath(home))
    _reject_symlinks(home, home)
    _reject_symlinks(SOURCE, REPO_ROOT)
    target = home / TARGET_RELATIVE
    _reject_symlinks(target, home)
    manifest, manifest_sha256 = _manifest(home)
    source_sha256 = sha256(SOURCE)
    target_sha256 = sha256(target) if target.exists() else None
    owned_sha256 = manifest["files"].get(TARGET_RELATIVE.as_posix())
    if target_sha256 is None:
        action = "create"
    elif target_sha256 == source_sha256:
        action = "same" if owned_sha256 == source_sha256 else "adopt"
    elif owned_sha256 == target_sha256:
        action = "update"
    else:
        raise ValueError("unmanaged or user-modified global rules conflict: AGENTS.md")
    return {
        "schema_version": 1,
        "owner": OWNER,
        "target_home": str(home),
        "action": action,
        "source": "templates/AGENTS.md",
        "source_sha256": source_sha256,
        "target": TARGET_RELATIVE.as_posix(),
        "target_sha256": target_sha256,
        "manifest": MANIFEST_RELATIVE.as_posix(),
        "manifest_sha256": manifest_sha256,
    }


def _current_hash(path: Path) -> str | None:
    return sha256(path) if path.exists() else None


def _atomic_publish(raw: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_plan(home: Path, reviewed: dict[str, Any]) -> dict[str, Any]:
    """Apply one already-pinned plan, rejecting any intervening byte change."""
    home = Path(os.path.abspath(home))
    target = home / TARGET_RELATIVE
    manifest_path = home / MANIFEST_RELATIVE
    for path in (target, manifest_path):
        _reject_symlinks(path, home)
    source_bytes = SOURCE.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != reviewed["source_sha256"]:
        raise ValueError("global rules source changed since reviewed plan")
    if _current_hash(target) != reviewed["target_sha256"]:
        raise ValueError("global rules target changed since reviewed plan")
    if _current_hash(manifest_path) != reviewed["manifest_sha256"]:
        raise ValueError("global rules ownership changed since reviewed plan")

    backup: Path | None = None
    if reviewed["action"] == "update":
        backup = home / "maintenance" / "global-rules-backups" / (
            time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
        )
        _reject_symlinks(backup, home)
        backup.mkdir(parents=True)
        shutil.copy2(target, backup / TARGET_RELATIVE.name)
        if manifest_path.exists():
            shutil.copy2(manifest_path, backup / manifest_path.name)

    if reviewed["action"] in {"create", "update"}:
        if _current_hash(target) != reviewed["target_sha256"]:
            raise ValueError("global rules target changed before publication")
        _atomic_publish(source_bytes, target)
    if sha256(target) != reviewed["source_sha256"]:
        raise ValueError("global rules publication hash mismatch; backup retained")
    desired_manifest = {
        "schema_version": 1,
        "owner": OWNER,
        "files": {TARGET_RELATIVE.as_posix(): reviewed["source_sha256"]},
    }
    current, _ = _manifest(home)
    if current != desired_manifest:
        if _current_hash(manifest_path) != reviewed["manifest_sha256"]:
            raise ValueError("global rules ownership changed before publication")
        atomic_write_json(manifest_path, desired_manifest)
    result = dict(reviewed)
    result["backup"] = str(backup) if backup else None
    return result


def plan_digest(reviewed: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(reviewed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def deploy(home: Path, apply: bool, *, expected_plan_sha256: str | None = None,
           allow_test_root: bool = False) -> dict[str, Any]:
    home = Path(os.path.abspath(home))
    reviewed = plan(home)
    if not apply:
        return dict(reviewed, plan_sha256=plan_digest(reviewed))
    if expected_plan_sha256 != plan_digest(reviewed):
        raise ValueError("reviewed global-rules plan hash missing or changed; preview again")
    if not allow_test_root and home.resolve() != active_codex_home():
        raise ValueError("deployment is restricted to the active CODEX_HOME")
    state = home / "maintenance"
    _reject_symlinks(state, home)
    state.mkdir(parents=True, exist_ok=True)
    with lease(state / "runtime-install.lock"):
        return apply_plan(home, reviewed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the managed global AGENTS.md rules.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show the exact pinned plan (default).")
    mode.add_argument("--apply", action="store_true", help="Apply the pinned plan.")
    parser.add_argument("--expected-plan-sha256", help="Exact plan_sha256 from the reviewed preview.")
    args = parser.parse_args()
    if args.apply and not args.expected_plan_sha256:
        parser.error("--apply requires --expected-plan-sha256 from a reviewed preview")
    result = deploy(active_codex_home(), apply=args.apply, expected_plan_sha256=args.expected_plan_sha256)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
