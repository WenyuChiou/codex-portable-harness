"""A hash-gated patch for the pinned Research Hub login implementation."""
from __future__ import annotations

import hashlib
from pathlib import Path

from bootstrap import _atomic_manifest, _reject_symlinks
from common import load_json, sha256


def patched_source(text: str) -> str:
    changes = [
        ('import inspect\n', 'import inspect\nimport os\nfrom urllib.parse import urlsplit\n'),
        ('return _NLM_HOST in url and "/login" not in url',
         'parsed = urlsplit(url)\n    return (parsed.scheme == "https"\n'
         '            and parsed.hostname in {"notebooklm.google.com", "notebook.google.com"}\n'
         '            and not parsed.path.startswith("/login"))'),
        ('    # The homepage URL must hold for this many consecutive polls before we',
         '    # Use installed Chrome on Windows; bundled Chromium may fail SideBySide activation.\n'
         '    if sys.platform == "win32":\n'
         '        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):\n'
         '            base = os.environ.get(variable)\n'
         '            if base:\n'
         '                chrome = Path(base) / "Google/Chrome/Application/chrome.exe"\n'
         '                if chrome.is_file():\n'
         '                    launch_kwargs["executable_path"] = str(chrome)\n'
         '                    break\n'
         '    # The homepage URL must hold for this many consecutive polls before we'),
        ('                    current_url = page.url',
         '                    for candidate_page in context.pages:\n'
         '                        if _is_on_notebooklm_homepage(candidate_page.url):\n'
         '                            page = candidate_page\n'
         '                            break\n'
         '                    current_url = page.url'),
    ]
    for old, new in changes:
        if text.count(old) != 1:
            raise ValueError("NotebookLM upstream shape changed; refusing an unverified patch")
        text = text.replace(old, new)
    compile(text, "research_hub/notebooklm/auth.py", "exec")
    return text


def apply(home: Path, target: Path, upstream_sha256: str, legacy: dict[str, str] | None = None) -> str:
    _reject_symlinks(target, home)
    backup = home / "machine-setup/compatibility/notebooklm-auth.upstream.py"
    manifest = backup.with_suffix(".json")
    _reject_symlinks(backup, home)
    _reject_symlinks(manifest, home)
    current = sha256(target)
    if manifest.exists():
        known = load_json(manifest)
        if current == known["patched_sha256"]:
            return "same"
    if current == upstream_sha256:
        raw = target.read_bytes()
    elif current in (legacy or {}):
        original = home / legacy[current]
        _reject_symlinks(original, home)
        if not original.is_file() or sha256(original) != upstream_sha256:
            raise ValueError("known legacy patch requires its exact original backup")
        raw = original.read_bytes()
    else:
        raise ValueError("unknown NotebookLM auth.py drift; preserve it and inspect before upgrading")
    patched = patched_source(raw.decode("utf-8")).encode("utf-8")
    if backup.exists() and sha256(backup) != upstream_sha256:
        raise ValueError("NotebookLM original backup collision")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_bytes(raw)
    _atomic_manifest(manifest, {"upstream_sha256": upstream_sha256,
                               "patched_sha256": hashlib.sha256(patched).hexdigest()})
    target.write_bytes(patched)
    return "patched"
