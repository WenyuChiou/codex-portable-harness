"""Deploy an owned maintenance runtime; preserve unrelated config and hook entries."""
from __future__ import annotations

import argparse
import fnmatch
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
from storage_lifecycle import toml_table_boundaries, validate_toml

FILES = ("common.py", "maintenance.py", "storage_lifecycle.py", "workload.py", "task_run.py", "mcp_lifecycle.py", "test_run.py", "repair_maintenance_acl.ps1", "run_maintenance.ps1")

TASK_RULE = """<!-- codex-storage-lifecycle:start -->
## Task storage and load
- Start repository work with git rev-parse --show-toplevel. If it equals the
  account home, stop and report; use a project checkout before continuing.
- Put new scratch in CODEX_HOME/tmp/<task-id> and new clones/worktrees in
  CODEX_HOME/worktrees. Run autonomous commands through maintenance/runtime/task_run.py
  with an explicit timeout; use test_run.py for tests (one heavy suite at a time).
- Before completing or abandoning a task, remove its owned disposable scratch;
  retain failed scratch three days. Report retained paths and their reason.
  Preserve accepted results, required evidence, user changes and pinned data.
- Keep at most three executing autonomous jobs. Idle chats hold no job slot.
  Use bounded GitHub checks on a meaningful change; avoid rapid API polling.
- Maintenance previews first. Apply only a reviewed plan after user approval.
- At task end update the project's handoff: progress, output paths, needed
  worktrees and next step. Use its existing file, or CODEX_HOME/maintenance/handovers/<project>.md.
<!-- codex-storage-lifecycle:end -->
"""


def storage_defaults(home):
    """Locate legacy roots without claiming ownership of their contents."""
    user = Path.home()
    legacy = [user/"AppData/Local/Temp"] if os.name == "nt" else [Path(os.environ.get("TMPDIR", "/tmp"))]
    exact = [user/"temp-archive-2026-10-31"]
    if os.name == "nt":
        drive = Path(os.environ.get("SystemDrive", "C:")+"/")
        legacy.append(drive/"temp")
        patterns = ("stage*", "tmp*", "codex*", "s1*", "s2*", "s3*", "s4*", "pfc-dev-locks", "nonexistent", "node_modules", "home", "mnt")
        with os.scandir(drive) as entries:
            exact.extend(drive/e.name for e in entries if any(fnmatch.fnmatchcase(e.name.casefold(), p) for p in patterns))
    return {"legacy_roots": list(dict.fromkeys(map(str, legacy))), "legacy_paths": list(map(str, exact)),
            "clone_roots": [str(user/"Documents/Codex"), str(home/"worktrees/clones")],
            "owned_paths": [], "clone_backups": {}, "pinned": []}


def patch_mcp(text: str, home: Path, python: str):
    if not text.strip():
        return text, None
    original = validate_toml(text)
    if "codebase-memory-mcp" not in original.get("mcp_servers", {}):
        return text, None
    headers = toml_table_boundaries(text)
    cuts = []
    for index, start in enumerate(headers):
        line_end = text.find("\n", start)
        header = text[start:] if line_end < 0 else text[start:line_end+1]
        table = validate_toml(header)
        if "codebase-memory-mcp" in table.get("mcp_servers", {}):
            cuts.append((start, headers[index+1] if index+1 < len(headers) else len(text)))
    if not cuts:
        raise ValueError("MCP-inline-definition-requires-manual-config-edit; config retained")
    changed = text
    for start, end in reversed(cuts):
        changed = changed[:start]+changed[end:]
    remaining = validate_toml(changed)
    if "codebase-memory-mcp" in remaining.get("mcp_servers", {}):
        raise ValueError("MCP-definition-remains; config retained for manual edit")
    return changed, "removed-disabled-server"


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
    cleanup = '"' + python + '" -B "' + str(runtime / "test_run.py") + '" --clean'
    cleanup_pattern = r'"[^"\r\n]+" -B "' + re.escape(str(runtime / "test_run.py")) + r'" --clean(?: --apply)?'
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
    agents = safe(home/"AGENTS.md", home)
    agents_raw = agents.read_bytes() if agents.exists() else b""
    agents_text = agents_raw.decode("utf-8")
    block = re.compile(r"(?ms)^<!-- codex-storage-lifecycle:start -->.*?^<!-- codex-storage-lifecycle:end -->\r?\n?")
    changed_agents = block.sub(TASK_RULE, agents_text) if block.search(agents_text) else agents_text.rstrip()+"\n\n"+TASK_RULE
    local = safe(state/"local.json", home)
    local_raw = local.read_bytes() if local.exists() else b""
    local_value = json.loads(local_raw) if local_raw else {}
    local_storage = local_value.setdefault("storage", {})
    for key, data in storage_defaults(home).items():
        local_storage.setdefault(key, data)
    print(json.dumps({"apply": apply, "runtime": str(runtime), "mcp_backend": backend,
                      "config_change": changed_config != original_config, "hook_change": json.loads(original_hooks) != hooks}))
    if not apply:
        return
    runtime.mkdir(parents=True, exist_ok=True)
    backups = safe(state / "install-backups" / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex), home)
    backups.mkdir(parents=True)
    for path in (config, hooks_path, manifest_path, agents, local):
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
    current_agents = agents.read_bytes() if agents.exists() else b""
    if current_agents != agents_raw:
        raise ValueError("AGENTS.md changed concurrently")
    if changed_agents != agents_text:
        temporary = safe(home/"AGENTS.md.storage-new", home)
        with temporary.open("xb") as stream:
            stream.write(changed_agents.encode("utf-8")); stream.flush(); os.fsync(stream.fileno())
        if (agents.read_bytes() if agents.exists() else b"") != agents_raw:
            temporary.unlink(); raise ValueError("AGENTS.md changed concurrently")
        os.replace(temporary, agents)
    if (local.read_bytes() if local.exists() else b"") != local_raw:
        raise ValueError("local storage settings changed concurrently")
    if not local_raw or json.loads(local_raw) != local_value:
        atomic_write_json(local, local_value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    deploy(active_codex_home(), args.apply, str(Path(args.python).resolve()))


if __name__ == "__main__":
    main()
