"""Response-only probe, run by the environment being verified (including in sandbox)."""
from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import sysconfig
from pathlib import Path


def probe(expected: Path, research: bool) -> dict:
    if Path(sys.prefix).resolve() != expected.resolve() or sys.prefix == sys.base_prefix:
        raise RuntimeError("the requested isolated virtual environment is not active")
    executable = expected / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if Path(sys.executable).absolute() != executable.absolute():
        raise RuntimeError("unexpected Python launcher; system fallback is forbidden")
    modules = ["ssl", "sqlite3", "ctypes", "zlib", "agent_collab_harness"]
    if research:
        modules += ["research_hub", "playwright.sync_api", "docx", "pdfplumber", "pyzotero"]
    for name in modules:
        importlib.import_module(name)
    # Exercise the same launcher in a grandchild, not just the current process.
    child = subprocess.run([sys.executable, "-B", "-c", "import sys; print(sys.prefix)"],
                           capture_output=True, text=True, timeout=30)
    if child.returncode or Path(child.stdout.strip()).resolve() != expected.resolve():
        raise RuntimeError("virtual-environment child launcher failed")
    check = subprocess.run([sys.executable, "-B", "-m", "pip", "check"],
                           capture_output=True, text=True, timeout=90)
    if check.returncode:
        raise RuntimeError("pip check found missing or incompatible dependencies")
    return {"status": "PASS", "executable": sys.executable, "prefix": sys.prefix,
            "base_prefix": sys.base_prefix, "version": sys.version.split()[0],
            "purelib": sysconfig.get_path("purelib"),
            "imports": modules, "child_launcher": "PASS", "pip_check": "PASS"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--research", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.expected, args.research)))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
