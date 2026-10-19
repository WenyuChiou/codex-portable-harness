"""Pinned source deployment; no third-party Python dependencies or shell commands."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from bootstrap import _atomic_manifest, _reject_symlinks
from common import REPO_ROOT, ensure_within, load_json, sha256

ADAPTATION = """Codex host adaptation. Follow the bounded task assigned by the parent; return evidence, not changes. This agent is report-only: do not edit files, invoke remediation, install packages, start schedules, commit, or spawn further agents. Use Codex native read/search/exec tools rather than Claude-specific tool names. Inherit the parent model, reasoning effort, permissions, and available integrations. Instructions requesting proactive invocation do not grant independent permission to dispatch. A read-only instruction is not a separate native sandbox; the parent must set the session's permissions appropriately. Do not read credentials, authentication files, session transcripts, databases, or caches. Never infer usage from file modification time. Missing sources or tooling must be reported as unavailable, never treated as a successful check.
Resolve CODEX_HOME as the active setting or ~/.codex. Installed skills live in CODEX_HOME/skills. Legacy academic-writing paths in the source below refer to CODEX_HOME/skills/academic-writing-skills/references. Use banned_words.md for its banned-word corpus; use prose-and-citation-editing.md for the current plain-language and paragraph-flow rules (the legacy writing_principles.md is not shipped). Use the installed academic-writing-skills SKILL.md as the current manuscript-integrity contract. Apply paper-specific overrides only to the identified paper; advisor-specific sources require established context and must be reported unavailable if absent. Do not enter another agent product's home directory to satisfy a legacy reference.
For harness health, inspect only the active Codex installation and task-supplied evidence: AGENTS.md, config structure with sensitive values redacted, deployed agents and skills, portable-harness/bootstrap-ownership.json and doctor results. Claude hooks/settings, legacy ledgers, sync telemetry and Claude invocation metadata from the source are not Codex checks; mark those signals not applicable or unavailable. Do not invent equivalent measurements, thresholds or legacy ledger IDs. Do not declare the entire harness healthy when required checks were unavailable.
The following source instructions apply only where compatible with the Codex adaptation above and the parent's current task."""
DESCRIPTIONS = {
    "code-reviewer": "Report-only correctness review, including changed-value parity across consumers.",
    "banned-word-auditor": "Report-only academic prose audit against the installed corpus and current prose rules.",
    "harness-drift-monitor": "Read-only Codex harness health and deployment-drift audit with evidence-cited findings; report unavailable legacy signals explicitly.",
    "silent-failure-hunter-py": "Report-only Python silent-failure investigation with located downstream consequences.",
}


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 180) -> str:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR"):
        environment.pop(name, None)
    if os.name == "nt":
        # pip's VCS checkout inherits this process-local setting. A deep
        # CODEX_HOME otherwise fails before wheel construction on Windows.
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
        environment.update(GIT_CONFIG_COUNT=str(count + 1))
        environment[f"GIT_CONFIG_KEY_{count}"] = "core.longpaths"
        environment[f"GIT_CONFIG_VALUE_{count}"] = "true"
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            env=environment)
    if result.returncode:
        # Expose only a bounded error summary; never raw provider/build logs.
        candidates = [line for line in (result.stdout + "\n" + result.stderr).splitlines()
                      if line.strip().startswith(("ERROR:", "fatal:", "error:"))]
        specific = [line for line in candidates if "subprocess-exited-with-error" not in line]
        detail = specific[-1] if specific else candidates[-1] if candidates else "no safe diagnostic; inspect the failed tool locally"
        if re.search(r"token|credential|secret|authorization|password", detail, re.I):
            detail = "authentication-related failure; verify local access without exposing credentials"
        detail = re.sub(r"https?://\S+", "<URL>", detail)
        detail = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "<opaque>", detail)
        raise RuntimeError(f"{Path(command[0]).name} failed (exit {result.returncode}): {detail[:600]}")
    return result.stdout.strip()


def relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError(f"unsafe relative path: {value}")
    return path


def profile() -> dict:
    return load_json(REPO_ROOT / "profiles/research.json")


def fetch_source(home: Path, repo: str, commit: str) -> Path:
    if not re.fullmatch(r"[\w-]+/[\w.-]+", repo) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("source must name a GitHub repository and full commit")
    cache = home / "machine-setup/sources" / (repo.replace("/", "--") + "-" + commit)
    _reject_symlinks(cache, home)
    git_dir = cache / ".git"
    _reject_symlinks(git_dir, home)
    if git_dir.exists() and not git_dir.is_dir():
        raise ValueError("source cache must be a standalone Git repository, not an external gitdir")
    if any((git_dir / name).exists() for name in ("commondir", "objects/info/alternates")):
        raise ValueError("source cache cannot reference external Git object directories")
    url = "https://github.com/" + repo + ".git"
    if not git_dir.exists():
        cache.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet", str(cache)])
    try:
        found = run(["git", "-C", str(cache), "rev-parse", "--verify", commit + "^{commit}"])
    except RuntimeError:
        run(["git", "-C", str(cache), "fetch", "--quiet", "--depth=1", url, commit])
        found = run(["git", "-C", str(cache), "rev-parse", "FETCH_HEAD"])
    if found != commit:
        raise ValueError("fetched source does not match the pinned commit")
    return cache


def source_files(cache: Path, commit: str, prefix: str) -> dict[str, bytes]:
    relative_path(prefix)
    listing = subprocess.run(["git", "-C", str(cache), "ls-tree", "-rz", commit, "--", prefix],
                             capture_output=True, check=True).stdout
    files = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        header, raw_name = record.split(b"\t", 1)
        mode, kind, object_id = header.decode().split()
        name = raw_name.decode("utf-8")
        relative_path(name)
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("symlinks and submodules are not installable sources")
        files[name] = subprocess.run(["git", "-C", str(cache), "cat-file", "blob", object_id],
                                     capture_output=True, check=True).stdout
    if not files:
        raise ValueError(f"empty pinned source path: {prefix}")
    return files


def adapt_agent(name: str, source: bytes, repo: str, commit: str) -> bytes:
    text = source.decode("utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("agent source must have a frontmatter header")
    header, _, body = text[4:].partition("\n---\n")
    description = re.search(r"^description: (.+)$", header, re.MULTILINE).group(1)
    if name in {"code-reviewer", "banned-word-auditor"}:
        description = re.sub(r"Use PROACTIVELY.*?\. (?=Especially|Returns)", "Use when assigned by the parent.  ", description)
    if name == "harness-drift-monitor":
        description = DESCRIPTIONS[name]
    instructions = ADAPTATION + f"\n\nSource: {repo} @ {commit}\n\n" + body.lstrip()
    # JSON string syntax is a compatible subset of TOML basic strings here.
    return (f'name = {json.dumps(name)}\n'
            f'description = {json.dumps(description, ensure_ascii=False)}\n'
            f'developer_instructions = {json.dumps(instructions, ensure_ascii=False)}\n').replace("\n", os.linesep).encode("utf-8")


def desired_research_files(home: Path, spec: dict) -> dict[str, bytes]:
    desired = {}
    for entry in spec["sources"]:
        cache = fetch_source(home, entry["repo"], entry["commit"])
        for prefix in entry["skills"]:
            for name, data in source_files(cache, entry["commit"], prefix).items():
                if name in desired:
                    raise ValueError("duplicate skill destination")
                desired[name] = data
        if entry["repo"].endswith("/zotero-skills"):
            for name in ("zotero_client.py", "add_literature.py", "requirements.txt", "config.example.json"):
                upstream = "scripts/" + name if name.endswith(".py") else name
                data = source_files(cache, entry["commit"], upstream)[upstream]
                desired["skills/zotero-skills/" + ("scripts/" if name.endswith(".py") else "") + name] = data
    entry = spec["extra_agents"]
    cache = fetch_source(home, entry["repo"], entry["commit"])
    for name in entry["names"]:
        source = source_files(cache, entry["commit"], f"agents/{name}.md")[f"agents/{name}.md"]
        desired[f"custom-agent-sources/dotfiles-claude/{name}.md"] = source
        desired[f"agents/{name}.toml"] = adapt_agent(name, source, entry["repo"], entry["commit"])
    return desired


def deploy(home: Path, desired: dict[str, bytes], *, apply: bool, previous: dict | None = None) -> list[str]:
    """Preflight every file before writes; journal every published file for resume."""
    manifest_path = home / "machine-setup/research-ownership.json"
    _reject_symlinks(manifest_path, home)
    manifest = load_json(manifest_path) if manifest_path.exists() else {"schema_version": 1, "files": {}}
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("invalid research ownership manifest")
    owned = manifest["files"]
    actions = []
    for name, data in desired.items():
        relative_path(name)
        path = home / name
        _reject_symlinks(path, home)
        if path.is_dir():
            raise ValueError("managed file collides with a directory")
        digest = hashlib.sha256(data).hexdigest()
        actual = sha256(path) if path.exists() else None
        if actual is not None and actual != digest and actual != owned.get(name):
            legacy = (previous or {}).get(path.stem) if name.startswith("agents/") else None
            if actual != legacy:
                raise ValueError(f"user-modified or unmanaged collision: {name}")
        actions.append(("same" if actual == digest else "update" if actual else "create") + " " + name)
    if not apply:
        return actions
    # Do not silently delete files omitted by a later profile version.
    for name, data in desired.items():
        path = home / name
        digest = hashlib.sha256(data).hexdigest()
        if not path.exists() or sha256(path) != digest:
            path.parent.mkdir(parents=True, exist_ok=True)
            import tempfile
            descriptor, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        owned[name] = digest
        _atomic_manifest(manifest_path, {"schema_version": 1, "files": owned})
    return actions
