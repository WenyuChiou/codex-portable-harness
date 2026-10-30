"""Offline integration fixtures for installed skill helpers; writes only to CODEX_HOME."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

from bootstrap import _reject_symlinks
from common import active_codex_home, atomic_write_json
from setup_machine import python_at


def bash_executable() -> str:
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidate = Path(git).parent.parent / "bin/bash.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash") or "bash"


def run(command: list[str], env: dict) -> subprocess.CompletedProcess:
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, env=env)
    if process.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed, exit {process.returncode}")
    return process


def check(home: Path) -> dict:
    folder = home / "machine-setup/functional" / uuid.uuid4().hex
    _reject_symlinks(folder, home)
    folder.mkdir(parents=True)
    env = {**os.environ, "NODE_PATH": str(home / "research-node/node_modules"),
           "RESEARCH_HUB_CONFIG": str(home / "machine-setup/research-hub-config.json"),
           "NOTEBOOKLM_HOME": str(home / "machine-setup/notebooklm")}
    outcomes = []
    for skill, name in (("gap-to-topic", "dossier_to_docx.js"), ("research-design-helper", "brief_to_docx.js")):
        stem = folder / skill
        stem.with_suffix(".md").write_text("# Installation fixture\n\nLocal deterministic evidence marker.\n", encoding="utf-8")
        run(["node", str(home / "skills" / skill / "scripts" / name), str(stem), "--no-toc"], env)
        with zipfile.ZipFile(stem.with_suffix(".docx")) as archive:
            if b"Local deterministic evidence marker" not in archive.read("word/document.xml"):
                raise ValueError("Word exporter did not preserve fixture text")
        outcomes.append({"check": skill + "-docx", "status": "PASS"})
    html = folder / "article.html"
    html.write_text('<html><head><title>Offline fixture</title></head><body><article><p>Local extraction evidence marker. This article verifies the extraction tool without a network request.</p><p>It contains enough local text to exercise extraction and keeps the account integrations separate.</p></article></body></html>', encoding="utf-8")
    defuddle = home / "research-node/node_modules/.bin" / ("defuddle.cmd" if os.name == "nt" else "defuddle")
    article = run([str(defuddle), "parse", str(html), "--json"], env)
    if "Local extraction evidence marker" not in json.loads(article.stdout).get("content", ""):
        raise ValueError("Defuddle fixture extraction failed")
    outcomes.append({"check": "defuddle-extraction", "status": "PASS"})
    state = folder / "workflow.json"
    hub = [str(python_at(home / "research-runtime")), "-B", "-m", "research_hub"]
    run([*hub, "workflow", "init", "--project-root", str(folder), "--state", str(state), "--json"], env)
    validation = run([*hub, "workflow", "validate", "--state", str(state), "--json"], env)
    if not json.loads(validation.stdout).get("ok"):
        raise ValueError("Research Hub workflow validation failed")
    outcomes.append({"check": "workflow-init-validate", "status": "PASS"})
    bash = bash_executable()
    # Do not run delegated wrappers: the AGY write wrapper has broader native
    # effects. Only syntax and the bounded GNU timeout prerequisite are checked.
    for relative in ("codex-delegate/scripts/run_codex.sh", "antigravity-delegate/scripts/run_agy.sh"):
        run([bash, "-n", str(home / "skills" / relative)], env)
    run([bash, "-c", "timeout --version"], env)
    outcomes.append({"check": "bash-wrappers-syntax-and-gnu-timeout", "status": "PASS"})
    result = {"status": "PASS", "checks": outcomes, "fixture_directory": str(folder),
              "wrapper_delegation": "NOT_RUN; syntax checks do not establish authenticated wrapper execution"}
    atomic_write_json(folder / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        print(json.dumps(check(active_codex_home()), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
