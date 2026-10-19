"""Install the pinned research profile; normal execution is a write-free plan."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import hashlib
from pathlib import Path

from bootstrap import _reject_symlinks
from common import REPO_ROOT, active_codex_home, atomic_write_json, ensure_within, load_json
from machine_profile import deploy, desired_research_files, profile, run
import notebooklm_compat
from machine_runtime import configure


def python_at(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def environment(home: Path, root: Path, interpreter: str) -> Path:
    _reject_symlinks(root, home)
    if not python_at(root).exists():
        run([interpreter, "-m", "venv", str(root)], timeout=180)
    # Never rebuild an existing environment in place or replace its base version.
    run([str(python_at(root)), "-B", "-c", "import sys; assert sys.prefix != sys.base_prefix"])
    return python_at(root)


def https_bytes(url: str) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("downloads require HTTPS")
    with urllib.request.urlopen(url, timeout=60) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise ValueError("download redirected away from HTTPS")
        return response.read()


def antigravity(home: Path, spec: dict) -> dict:
    destination = home / "tools/agy" / ("agy.exe" if os.name == "nt" else "agy")
    _reject_symlinks(destination, home)
    # Respect an existing authenticated installation; never reinstall or copy auth.
    existing = shutil.which("agy")
    if not existing and os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidate = Path(os.environ["LOCALAPPDATA"]) / "agy/bin/agy.exe"
        existing = str(candidate) if candidate.is_file() else None
    if existing or destination.exists():
        executable = existing or str(destination)
        return {"executable": executable, "version_output": run([executable, "--version"], timeout=30)}
    if os.name != "nt":
        raise ValueError("install Antigravity using its official platform installer, then rerun; automatic binary setup is Windows-only")
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if not arch:
        raise ValueError("unsupported Antigravity architecture")
    url = spec["antigravity_manifest_base"] + "/manifests/windows_" + arch + ".json"
    release = json.loads(https_bytes(url))
    blob = https_bytes(release["url"])
    if hashlib.sha512(blob).hexdigest() != release["sha512"].lower():
        raise ValueError("Antigravity SHA-512 verification failed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(blob)
    return {"executable": str(destination), "release_version": release["version"],
            "sha512": release["sha512"], "version_output": run([str(destination), "--version"], timeout=30)}


def install(home: Path, spec: dict, research_python: str) -> dict:
    print("Preflight pinned sources and deployed files", file=sys.stderr, flush=True)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise ValueError("Node.js/npm is required for Defuddle; install it then rerun")
    candidate = python_at(home / "research-runtime")
    interpreter = str(candidate) if candidate.is_file() else research_python
    version = run([interpreter, "-B", "-c", "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))"])
    if version != "3.12":
        raise ValueError("the research lock requires Python 3.12; supply --research-python before creating the environment")
    # Git can handle extended paths, but Python build backends may still use
    # classic Windows paths. Fail before mutating an overly deep environment.
    temporary = home / "tmp"
    if os.name == "nt" and len(str(temporary).encode("utf-16-le")) // 2 + 80 + spec["research_source_max_path"] >= 248:
        raise ValueError("CODEX_HOME is too deep for this pinned Windows pip build; choose a shorter CODEX_HOME (no registry changes were made)")
    _reject_symlinks(temporary, home)
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                      PIP_CACHE_DIR=str(home / "machine-setup/pip-cache"))
    # Source/conflict preflight precedes package or deployed-file changes.
    desired = desired_research_files(home, spec)
    deploy(home, desired, apply=False, previous=spec.get("previous_agent_sha256"))
    executable = environment(home, home / "research-runtime", research_python)
    entry = next(s for s in spec["sources"] if s["repo"].endswith("/research-hub"))
    package = "research-hub-pipeline[" + ",".join(spec["research_extras"]) + "] @ git+https://github.com/" + entry["repo"] + ".git@" + entry["commit"]
    print("Install/verify the pinned research dependency set", file=sys.stderr, flush=True)
    run([str(executable), "-m", "pip", "install", "--constraint", str(REPO_ROOT / "profiles/research-constraints.txt"),
         package, *spec["python_packages"], str(REPO_ROOT)], timeout=900)
    site = Path(run([str(executable), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]))
    # This patch is opt-in only for known pinned bytes; never overwrite local drift.
    compatibility = notebooklm_compat.apply(home, site / "research_hub/notebooklm/auth.py", spec["notebooklm_upstream_sha256"], spec.get("notebooklm_legacy_patches"))
    actions = deploy(home, desired, apply=True, previous=spec.get("previous_agent_sha256"))
    node_root = home / "research-node"
    _reject_symlinks(node_root, home)
    # Cache and prefix stay inside CODEX_HOME, even when npm has a global default.
    print("Install/verify Node helper tools", file=sys.stderr, flush=True)
    run([npm, "install", "--prefix", str(node_root), "--cache", str(home / "machine-setup/npm-cache"),
         "--no-audit", "--no-fund", "defuddle@" + spec["defuddle"], "docx@" + spec["node_docx"]], timeout=300)
    agy = antigravity(home, spec)
    run([str(executable), "-B", str(REPO_ROOT / "scripts/runtime_probe.py"),
         "--expected", str(home / "research-runtime"), "--research"])
    bins = [home / "research-runtime" / ("Scripts" if os.name == "nt" else "bin"),
            node_root / "node_modules/.bin", Path(agy["executable"]).parent]
    configure(home, bins)
    result = {"schema_version": 1, "profile_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
              "actions": actions, "antigravity": agy, "notebooklm_compatibility": compatibility,
              "path_entries": [str(p) for p in bins],
              "pending": ["Antigravity Google login and authenticated canary", "NotebookLM interactive login and read-only server verification", "Zotero credentials (optional; deliberately unconfigured)"]}
    atomic_write_json(home / "machine-setup/installation.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--research-python", default=sys.executable)
    args = parser.parse_args()
    home = active_codex_home()
    spec = profile()
    try:
        if args.apply:
            result = install(home, spec, args.research_python)
        else:
            # No network, directory creation, fetching or login during a plan.
            result = {"mode": "plan", "codex_home": str(home), "profile": spec,
                      "next": "Use setup.ps1 -Apply or create the harness runtime, then --apply; existing files are preflighted after pinned sources are fetched."}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
