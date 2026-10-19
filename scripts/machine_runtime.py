"""Generate runtime guidance and activation files inside CODEX_HOME only."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from bootstrap import _reject_symlinks
from common import atomic_write_json, load_json, sha256

GUIDANCE = """<!-- portable-machine-runtime:start -->
Use the isolated portable-harness-runtime interpreter for portable-harness scripts:
Scripts/python.exe on Windows, bin/python on POSIX, beneath the active CODEX_HOME.
Use research-runtime's interpreter for Research Hub helpers. Do not substitute
system Python when a launcher fails: inspect the sandbox and Python ACLs.
Activate CODEX_HOME/machine-setup/activate.ps1 (or activate.sh) to load tool paths
and the CODEX_HOME-local Research Hub config/NotebookLM profile. Existing external
vaults and logins are not migrated. Zotero credentials are a separate optional step.
Managed skills and native TOML agents live in CODEX_HOME/skills and CODEX_HOME/agents.
Research sources and drift hashes are in machine-setup/research-ownership.json;
five portable baseline agents remain managed by portable-harness/bootstrap.py.
Candidate promotion still requires host-local benchmark evidence.
<!-- portable-machine-runtime:end -->"""


def configure(home: Path, bins: list[Path]) -> None:
    folder = home / "machine-setup"
    for path in (folder, home / "AGENTS.md", folder / "runtime-guidance.json", home / "research-vault"):
        _reject_symlinks(path, home)
    agents = home / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    start, end = "<!-- portable-machine-runtime:start -->", "<!-- portable-machine-runtime:end -->"
    if start in existing or end in existing:
        if existing.count(start) != 1 or existing.count(end) != 1:
            raise ValueError("ambiguous runtime guidance markers")
        current = existing[existing.index(start):existing.index(end) + len(end)]
        if current != GUIDANCE:
            raise ValueError("user-modified runtime guidance; refusing overwrite")
    append_guidance = start not in existing
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / "research-hub-config.json"
    _reject_symlinks(config, home)
    if not config.exists():
        atomic_write_json(config, {"knowledge_base": {"root": str(home / "research-vault")}, "no_zotero": True})
        if os.name != "nt":
            config.chmod(0o600)
    (home / "research-vault").mkdir(exist_ok=True)
    local_bin = folder / "bin"
    _reject_symlinks(local_bin, home)
    local_bin.mkdir(exist_ok=True)
    all_bins = [local_bin, *bins]
    ps_quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    ps = "$env:PATH = " + ps_quote(os.pathsep.join(str(p) for p in all_bins)) + " + [IO.Path]::PathSeparator + $env:PATH\n"
    ps += "$env:RESEARCH_HUB_CONFIG = " + ps_quote(config) + "\n"
    ps += "$env:NOTEBOOKLM_HOME = " + ps_quote(folder / "notebooklm") + "\n"
    ps += "$env:RESEARCH_HUB_ROOT = " + ps_quote(home / "research-vault") + "\n"
    ps += "$env:NODE_PATH = " + ps_quote(home / "research-node/node_modules") + "\n"
    shell = "export PATH=" + shlex.quote(":".join(str(p) for p in all_bins)) + ':"$PATH"\n'
    shell += "export RESEARCH_HUB_CONFIG=" + shlex.quote(str(config)) + "\n"
    shell += "export NOTEBOOKLM_HOME=" + shlex.quote(str(folder / "notebooklm")) + "\n"
    shell += "export NODE_PATH=" + shlex.quote(str(home / "research-node/node_modules")) + "\n"
    # The launcher always selects the local config, without consulting another
    # agent product's credential/config fallback locations during onboarding.
    cmd = '@echo off\nset "RESEARCH_HUB_CONFIG=%~dp0..\\research-hub-config.json"\n'
    cmd += 'set "NOTEBOOKLM_HOME=%~dp0..\\notebooklm"\n'
    cmd += '"%~dp0..\\..\\research-runtime\\Scripts\\research-hub.exe" %*\n'
    launcher = local_bin / "research-hub.cmd"
    manifest = folder / "runtime-owned.json"
    _reject_symlinks(manifest, home)
    owned = load_json(manifest) if manifest.exists() else {"schema_version": 1, "files": {}}
    if owned.get("schema_version") != 1 or not isinstance(owned.get("files"), dict):
        raise ValueError("invalid runtime-file ownership manifest")
    files = {folder / "activate.ps1": ps, folder / "activate.sh": shell, launcher: cmd}
    for path, text in files.items():
        _reject_symlinks(path, home)
        relative = path.relative_to(home).as_posix()
        # Adopt only the exact previous generated activation (before NODE_PATH),
        # never arbitrary local edits in an unowned runtime directory.
        legacy = "".join(line for line in text.splitlines(keepends=True) if "NODE_PATH" not in line)
        if path.exists() and path.read_text(encoding="utf-8") not in {text, legacy} and sha256(path) != owned["files"].get(relative):
            raise ValueError(f"user-modified runtime file: {relative}")
    if append_guidance:
        agents.parent.mkdir(parents=True, exist_ok=True)
        agents.write_text(existing.rstrip() + ("\n\n" if existing else "") + GUIDANCE + "\n", encoding="utf-8")
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    atomic_write_json(manifest, {"schema_version": 1, "files": {path.relative_to(home).as_posix(): sha256(path) for path in files}})
    atomic_write_json(folder / "runtime-guidance.json", {"schema_version": 1, "block": GUIDANCE})
