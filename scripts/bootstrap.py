from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from common import REPO_ROOT, active_codex_home, ensure_within, sha256
from common import load_json
import model_promotion

SKILLS = ("adaptive-response", "bounded-agent-harness")
AGENTS = ("portable-mechanical.toml", "portable-mechanical-readonly.toml", "portable-explorer.toml", "portable-quality-explorer.toml", "portable-worker.toml")
MANIFEST_RELATIVE = Path("portable-harness") / "bootstrap-ownership.json"
# Exact pre-ownership v0.4 deployment bytes (LF and CRLF). Any other local
# modification remains a conflict; this is not a general overwrite allowance.
KNOWN_BASELINE_HASHES: dict[str, set[str]] = {
    "skills/adaptive-response/SKILL.md": {"7e6bbbc170752ed46f168f3268aeff53c43f91114e74e8be673388e9fe031813"},
    "skills/adaptive-response/agents/openai.yaml": {"6e904dd2b6d717011e560ca0bc287d194a2f096bbabb18a18103ac13b8fb259d"},
    "skills/bounded-agent-harness/agents/openai.yaml": {"444abbef137993594ac178b9ce88db9969c842438ab33c04fafb697c4bcb57d4"},
    "skills/bounded-agent-harness/SKILL.md": {
        "3b875f83a90ced8b53d959052658e5bf8a38ac58f8318957c1a3dc2ef40c3664",
        "f0c08b9b740d3daceeea9ff87d5af1f8ec28bf97bd34b6cf0e9054bd5afaafeb",
    }
}


def desired_files(target_home: Path | None = None) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for skill in SKILLS:
        source_root = REPO_ROOT / "skills" / skill
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            pairs.append((source, Path("skills") / skill / source.relative_to(source_root)))
    pairs.extend((REPO_ROOT / "agents" / name, Path("agents") / name) for name in AGENTS)
    if target_home is not None:
        policy = load_json(REPO_ROOT / "policies" / "model-routing.json")
        for task_class, candidate in policy.get("role_promotion", {}).get("candidates", {}).items():
            _, admitted, _ = model_promotion.host_attestation(target_home, policy, task_class)
            if admitted:
                for role in model_promotion.candidate_roles(candidate):
                    pairs.append((model_promotion.candidate_agent_source(role), Path("agents") / f"{role}.toml"))
    return pairs


def _reject_symlinks(path: Path, root: Path) -> None:
    ensure_within(path, root)
    current = path
    while True:
        info = current.lstat() if current.exists() or current.is_symlink() else None
        if current.is_symlink() or (info is not None and getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError(f"symlink is not a safe managed target: {current}")
        if current == root:
            break
        current = current.parent


def _read_manifest(path: Path, root: Path) -> dict[str, Any]:
    _reject_symlinks(path, root)
    if not path.exists():
        return {"schema_version": 1, "owner": "codex-portable-harness", "files": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid ownership manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("owner") != "codex-portable-harness" or not isinstance(value.get("files"), dict):
        raise ValueError(f"invalid ownership manifest: {path}")
    return value


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary_name)
        os.replace(temporary_name, destination)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _atomic_manifest(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def deploy(target_home: Path, apply: bool, *, allow_test_root: bool = False) -> list[str]:
    target_home = Path(os.path.abspath(target_home))
    _reject_symlinks(target_home, target_home)
    if apply and not allow_test_root and target_home != active_codex_home():
        raise ValueError("deployment is restricted to the active CODEX_HOME")
    manifest_path = target_home / MANIFEST_RELATIVE
    manifest = _read_manifest(manifest_path, target_home)
    owned: dict[str, str] = manifest["files"]
    planned: list[tuple[Path, Path, str, str]] = []
    revocations: list[tuple[Path, str]] = []
    conflicts: list[str] = []
    for source, relative in desired_files(target_home):
        destination = target_home / relative
        _reject_symlinks(destination, target_home)
        desired_hash = sha256(source)
        name = relative.as_posix()
        if not destination.exists():
            action = "create"
        else:
            actual_hash = sha256(destination)
            if actual_hash == desired_hash:
                action = "same" if name in owned else "adopt"
            elif name in owned and owned[name] == actual_hash:
                action = "update"
            elif actual_hash in KNOWN_BASELINE_HASHES.get(name, set()):
                action = "update-baseline"
            else:
                conflicts.append(name)
                continue
        planned.append((source, destination, name, action))
    files = {name: sha256(source) for source, _, name, _ in planned}
    # Revoke only exact, unchanged, manifest-owned candidate files when their
    # local proof expires. Keep ownership history for safe later reactivation.
    candidate_names = {f"agents/{role}.toml" for candidate in
                       load_json(REPO_ROOT / "policies" / "model-routing.json")["role_promotion"]["candidates"].values()
                       for role in model_promotion.candidate_roles(candidate)}
    for name in candidate_names.intersection(owned).difference(files):
        destination = target_home / Path(name)
        _reject_symlinks(destination, target_home)
        if destination.exists():
            if sha256(destination) != owned[name]:
                conflicts.append(name)
            else:
                revocations.append((destination, name))
        files[name] = owned[name]
    for name in candidate_names.difference(owned).difference(files):
        destination = target_home / Path(name)
        _reject_symlinks(destination, target_home)
        if destination.exists():
            conflicts.append(name)
    if conflicts:
        raise ValueError("unmanaged or user-modified deployment conflicts: " + ", ".join(sorted(conflicts)))
    new_manifest = {"schema_version": 1, "owner": "codex-portable-harness", "files": files}
    manifest_same = manifest_path.exists() and manifest == new_manifest
    actions = [f"{action} {name}" for _, _, name, action in planned]
    actions.extend(f"revoke {name}" for _, name in revocations)
    actions.append(f"{'same' if manifest_same else 'update' if manifest_path.exists() else 'create'} {MANIFEST_RELATIVE.as_posix()}")
    if not apply:
        return actions
    for source, destination, _, action in planned:
        if action not in {"same", "adopt"}:
            _atomic_copy(source, destination)
    for destination, _ in revocations:
        destination.unlink()
    if not manifest_same:
        _atomic_manifest(manifest_path, new_manifest)
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy managed portable Codex skills and agents.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show changes without writing (default).")
    mode.add_argument("--apply", action="store_true", help="Apply the displayed changes.")
    args = parser.parse_args()
    home = active_codex_home()
    actions = deploy(home, apply=args.apply)
    print(f"mode={'apply' if args.apply else 'dry-run'} codex_home={home}")
    for action in actions:
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
