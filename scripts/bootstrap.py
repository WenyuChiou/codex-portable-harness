from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

from common import REPO_ROOT, active_codex_home, ensure_within, sha256


SKILLS = ("adaptive-response", "bounded-agent-harness")


def desired_files() -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for skill in SKILLS:
        source_root = REPO_ROOT / "skills" / skill
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            pairs.append((source, Path("skills") / skill / source.relative_to(source_root)))
    return pairs


def deploy(target_home: Path, apply: bool, *, allow_test_root: bool = False) -> list[str]:
    target_home = target_home.resolve()
    if apply and not allow_test_root and target_home != active_codex_home():
        raise ValueError("skill deployment is restricted to the active ~/.codex")
    actions: list[str] = []
    for source, relative in desired_files():
        destination = target_home / relative
        ensure_within(destination, target_home)
        if destination.exists() and sha256(source) == sha256(destination):
            actions.append(f"same {relative.as_posix()}")
            continue
        action = "update" if destination.exists() else "create"
        actions.append(f"{action} {relative.as_posix()}")
        if not apply:
            continue
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
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy allowlisted Codex harness skills.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show changes without writing (default).")
    mode.add_argument("--apply", action="store_true", help="Apply the displayed skill changes.")
    args = parser.parse_args()

    home = active_codex_home()
    actions = deploy(home, apply=args.apply)
    print(f"mode={'apply' if args.apply else 'dry-run'} codex_home={home}")
    for action in actions:
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
