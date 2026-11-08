"""Deploy an owned maintenance runtime; preserve unrelated config and hook entries."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

from common import REPO_ROOT, active_codex_home, atomic_write_json, sha256
from maintenance import safe

FILES = ("common.py", "maintenance.py", "mcp_lifecycle.py", "test_run.py", "repair_maintenance_acl.ps1", "run_maintenance.ps1")


def patch_mcp(text: str, home: Path, python: str):
    # Narrow line-based patch: all unrelated TOML bytes, comments, and settings stay intact.
    match = re.search(r"(?ms)^\[mcp_servers\.codebase-memory-mcp\]\s*\n(.*?)(?=^\[|\Z)", text)
    if not match:
        return text, None
    section = match.group(1)
    command = re.search(r"(?m)^command\s*=\s*(.+?)\s*$", section)
    if not command:
        raise ValueError("MCP command missing")
    token = command.group(1)
    if token.startswith('"'):
        backend = json.loads(token)
    elif token.startswith("'") and token.endswith("'"):
        backend = token[1:-1]
    else:
        raise ValueError("unsupported MCP command syntax; preserve and inspect")
    if "mcp_lifecycle.py" in section:
        return text, "already-managed"
    # Native server presently has no args. Preserve customized argv via an explicit review.
    if re.search(r"(?m)^args\s*=", section):
        raise ValueError("custom MCP args require an explicit migration")
    args = [str(home / "maintenance/runtime/mcp_lifecycle.py"), "--", backend]
    section = section[:command.start()] + "command = " + json.dumps(python) + "\nargs = " + json.dumps(args) + section[command.end():]
    for key, value in (("startup_timeout_sec", 45), ("tool_timeout_sec", 960)):
        pattern = rf"(?m)^{key}\s*=.*$"
        section = re.sub(pattern, f"{key} = {value}", section) if re.search(pattern, section) else section.rstrip() + f"\n{key} = {value}\n\n"
    return text[:match.start(1)] + section + text[match.end(1):], backend


def deploy(home: Path, apply: bool, python: str):
    state = safe(home / "maintenance", home)
    runtime = safe(state / "runtime", home)
    manifest_path = safe(state / "runtime-ownership.json", home)
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"files": {}}
    for name in FILES:
        target = safe(runtime / name, home)
        if target.exists() and sha256(target) not in (sha256(REPO_ROOT / "scripts" / name), previous["files"].get(name)):
            raise ValueError(f"user-modified runtime conflict: {name}")
    config = safe(home / "config.toml", home)
    original_config = config.read_text(encoding="utf-8") if config.exists() else ""
    changed_config, backend = patch_mcp(original_config, home, python)
    hooks_path = safe(home / "hooks.json", home)
    original_hooks = hooks_path.read_text(encoding="utf-8-sig") if hooks_path.exists() else '{}'
    hooks = json.loads(original_hooks)
    command = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + str(runtime / "repair_maintenance_acl.ps1") + '" -Apply -Hook'
    cleanup = '"' + python + '" -B "' + str(runtime / "test_run.py") + '" --clean --apply'
    cleanup_pattern = r'"[^"\r\n]+" -B "' + re.escape(str(runtime / "test_run.py")) + r'" --clean --apply'
    for event in ("Stop", "SessionEnd"):
        groups = hooks.setdefault("hooks", {}).setdefault(event, [])
        if not any(h.get("command") == command for group in groups for h in group.get("hooks", [])):
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 60}]})
        retained = []
        for group in groups:
            previous_hooks = group.get("hooks", [])
            remaining = [h for h in previous_hooks if not re.fullmatch(cleanup_pattern, h.get("command", ""))]
            group["hooks"] = remaining
            if remaining or remaining == previous_hooks:
                retained.append(group)
        groups[:] = retained
        owned = next(group for group in groups if any(h.get("command") == command for h in group.get("hooks", [])))
        owned["hooks"].append({"type": "command", "command": cleanup, "timeout": 60})
    changed_hooks = json.dumps(hooks, ensure_ascii=False, indent=2) + "\n"
    print(json.dumps({"apply": apply, "runtime": str(runtime), "mcp_backend": backend,
                      "config_change": changed_config != original_config, "hook_change": json.loads(original_hooks) != hooks}))
    if not apply:
        return
    runtime.mkdir(parents=True, exist_ok=True)
    backups = safe(state / "install-backups" / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex), home)
    backups.mkdir(parents=True)
    for path in (config, hooks_path, manifest_path):
        if path.exists():
            shutil.copy2(path, backups / path.name)
    for name in FILES:
        source = REPO_ROOT / "scripts" / name
        target = runtime / name
        temporary = target.with_suffix(target.suffix + ".new")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    atomic_write_json(manifest_path, {"owner": "codex-portable-harness", "files": {name: sha256(runtime / name) for name in FILES}})
    if changed_config != original_config:
        temporary = config.with_suffix(".maintenance-new")
        temporary.write_text(changed_config, encoding="utf-8", newline="")
        if config.read_text(encoding="utf-8") != original_config:
            raise ValueError("config changed concurrently; backup retained")
        os.replace(temporary, config)
    if json.loads(original_hooks) != hooks:
        if hooks_path.exists() and hooks_path.read_text(encoding="utf-8-sig") != original_hooks:
            raise ValueError("hooks changed concurrently; backup retained")
        atomic_write_json(hooks_path, hooks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    deploy(active_codex_home(), args.apply, str(Path(args.python).resolve()))


if __name__ == "__main__":
    main()
