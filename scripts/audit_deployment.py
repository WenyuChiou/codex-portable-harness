"""Response-only ownership audit usable even under PowerShell constrained language."""
from __future__ import annotations

import json

from bootstrap import _reject_symlinks
from common import active_codex_home, load_json, sha256
from machine_profile import relative_path


def audit(home):
    manifest = home / "portable-harness/bootstrap-ownership.json"
    _reject_symlinks(manifest, home)
    data = load_json(manifest)
    if data.get("schema_version") != 1 or not data.get("files"):
        raise ValueError("invalid baseline ownership manifest")
    mismatches = []
    checked = 0
    for name, expected in data["files"].items():
        relative_path(name)
        target = home / name
        _reject_symlinks(target, home)
        if not target.is_file() or sha256(target) != expected:
            mismatches.append(name)
        checked += 1
    return {"agent_count": len(list((home / "agents").glob("*.toml"))),
            "managed_file_count": len(data["files"]), "checked_file_count": checked,
            "managed_mismatches": len(mismatches), "mismatched_paths": mismatches,
            "complete": checked == len(data["files"]),
            "checked_by_kind": {kind: sum(name.startswith(kind + "/") for name in data["files"]) for kind in ("agents", "skills")},
            "scope": "Every entry in bootstrap-ownership.json, both agents AND skills, was checked. Independent research ownership, unlisted files, legacy signals and usage are outside this task."}


if __name__ == "__main__":
    try:
        result = audit(active_codex_home())
        print(json.dumps(result))
        raise SystemExit(1 if result["managed_mismatches"] else 0)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        raise SystemExit(1)
