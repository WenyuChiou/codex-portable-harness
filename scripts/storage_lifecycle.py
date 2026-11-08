"""Ownership-scoped storage plans. Metadata scans never follow reparse points."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

try:
    import tomllib
except ImportError:  # Python 3.10 can preview storage; project pruning stays read-only.
    tomllib = None

from common import atomic_write_json, sha256

DAY = 86400
OWNER = "codex-storage-v1"
PINS = {".codex-pin", ".maintenance-pin", ".lifecycle-pin"}
REBUILDABLE = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
PRESERVATION_CAP = 2 * 1024**3


def absolute(path):
    return Path(os.path.abspath(path))


def native(path):
    value = str(absolute(path))
    if os.name == "nt" and not value.startswith("\\\\"):
        return "\\\\?\\" + value
    return value


def overlaps(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def activity(info):
    # APFS can backdate birthtime when mtime is backdated. ctime also protects
    # recent metadata changes and Windows/Python versions without birthtime.
    return max(info.st_mtime, getattr(info, "st_birthtime", 0.), info.st_ctime)


class Guard:
    def __init__(self, home, pins=(), protected=()):
        self.home = absolute(home)
        self.pins = {absolute(p) for p in pins}
        self.protected = {absolute(Path.home()/".git"), *(absolute(p) for p in protected)}
        model = os.environ.get("OLLAMA_MODELS")
        if model:
            self.protected.add(absolute(model))

    def forbidden(self, path):
        path = absolute(path)
        parts = {p.casefold() for p in re.split(r"[\\/]+", str(path))}
        return bool(parts & {".claude", ".gemini", ".opencode", ".ollama", ".trash-claude",
                             "backup-2026-11-08", "ab-kept-home"}) or any(
            path == p or path.is_relative_to(p) for p in self.protected)

    def check(self, path, root, allow_missing=False):
        path, root = absolute(path), absolute(root)
        if self.forbidden(path) or self.forbidden(root):
            raise ValueError("protected-path")
        if path == root or not path.is_relative_to(root):
            raise ValueError("outside-scope")
        # Check lexical ancestry before any resolution, including the scope root.
        for part in reversed((path, *path.parents)):
            try:
                info = os.lstat(native(part))
            except FileNotFoundError:
                if allow_missing:
                    continue
                raise
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse-point")
        return path

    def pinned(self, path):
        path = absolute(path)
        return any(overlaps(path, p) for p in self.pins)


@dataclass
class Snapshot:
    fingerprint: str
    latest: float
    bytes: int
    files: int
    pinned: bool
    repositories: list


def snapshot(path, root, guard):
    path = guard.check(path, root)
    digest = hashlib.sha256()
    latest, size, count, pinned, repositories = 0., 0, 0, guard.pinned(path), []
    # Ancestor pins protect dated buckets and individual children alike. All
    # ancestors have passed lstat checks before these marker probes.
    for ancestor in path.parents:
        for marker in PINS:
            try:
                os.lstat(native(ancestor/marker))
                pinned = True
            except FileNotFoundError:
                pass

    def visit(item, info=None):
        nonlocal latest, size, count, pinned
        if guard.forbidden(item):
            raise ValueError("protected-descendant")
        info = os.lstat(native(item)) if info is None else info
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("reparse-point")
        birth = activity(info)
        latest = max(latest, info.st_mtime, birth)
        rel = item.relative_to(path).as_posix()
        digest.update(json.dumps([rel, info.st_size, info.st_mtime_ns, birth, info.st_ino, info.st_mode]).encode())
        if item.name in PINS:
            pinned = True
        if stat.S_ISREG(info.st_mode):
            count += 1
            size += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(native(item)) as stream:
                entries = sorted(stream, key=lambda e: e.name)
            names = {entry.name.casefold() for entry in entries}
            if ".git" in names:
                repositories.append(str(item))
            # Bare/mirror repositories and linked metadata have no .git child.
            # A normal clone's already-recognized .git is its metadata, not a
            # second nested repository. Never inspect HEAD/commondir targets.
            if "head" in names and ("objects" in names or "commondir" in names) and not (item.name.casefold() == ".git" and str(item.parent) in repositories):
                repositories.append(str(item))
            for entry in entries:
                # Windows directory enumeration caches metadata that can lag a
                # just-closed write/chmod. Approval fingerprints use fresh lstat.
                child = item/entry.name
                if guard.forbidden(child):
                    raise ValueError("protected-descendant")
                visit(child, os.lstat(native(child)) if os.name == "nt" else entry.stat(follow_symlinks=False))
        else:
            raise ValueError("unsupported-filesystem-object")
    visit(path)
    return Snapshot(digest.hexdigest(), latest, size, count, pinned, repositories)


def remove(path, root, guard, expected):
    current = snapshot(path, root, guard)
    if current.fingerprint != expected or current.pinned:
        raise ValueError("candidate-changed-or-pinned")
    # chmod is restricted to the verified apply candidate, never an ACL reset.
    def readonly(function, name, exc):
        info = os.lstat(name)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("reparse-point-during-removal")
        if not isinstance(exc[1], PermissionError):
            raise exc[1]
        os.chmod(name, info.st_mode | stat.S_IWUSR)
        function(name)
    info = os.lstat(native(path))
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(native(path), onerror=readonly)
    else:
        try:
            os.unlink(native(path))
        except PermissionError:
            os.chmod(native(path), info.st_mode | stat.S_IWUSR)
            os.unlink(native(path))


def units(root, dated=False, guard=None):
    """Split dated output buckets into task units; never traverse a linked bucket."""
    if guard is not None:
        if guard.forbidden(root):
            yield root; return
        guard.check(root, root.parent, allow_missing=True)
    if not os.path.lexists(root):
        return
    info = os.lstat(native(root))
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        yield root
        return
    with os.scandir(native(root)) as stream:
        entries = sorted(stream, key=lambda e: e.name)
    for entry in entries:
        path = root/entry.name
        if guard is not None and guard.forbidden(path):
            yield path; continue
        info = entry.stat(follow_symlinks=False)
        if dated and re.fullmatch(r"\d{2,4}(?:-\d{2}){0,2}", entry.name) and stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400:
            yield from units(path, dated=True, guard=guard)
        else:
            yield path


def repository_units(root, guard, depth=0):
    if guard.forbidden(root) or guard.forbidden(root/".git"):
        yield root; return
    try:
        guard.check(root, root.parent, allow_missing=True)
    except (OSError, ValueError):
        yield root; return
    if not os.path.lexists(root):
        return
    info = os.lstat(native(root))
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        yield root; return
    if not stat.S_ISDIR(info.st_mode):
        return  # Clone discovery considers repositories, not individual task logs.
    if os.path.lexists(root/".git") or depth >= 4:
        yield root; return
    entries = list(units(root, guard=guard))
    if not entries:
        yield root
    for item in entries:
        yield from repository_units(item, guard, depth+1)


def receipt(path, guard):
    marker = path/".lifecycle.json"
    if not os.path.lexists(marker):
        return {}
    guard.check(marker, path)
    if os.lstat(native(marker)).st_size > 1024 * 1024:
        raise ValueError("oversized-ownership-receipt")
    value = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid-ownership-receipt")
    return value


def duplicate_verified(path, target, guard):
    """A manifest/list alone is insufficient: compare every preserved byte."""
    path, target = absolute(path), absolute(target)
    if overlaps(path, target):
        return False
    a, b = snapshot(path, path.parent, guard), snapshot(target, target.parent, guard)
    if a.files != b.files or a.bytes != b.bytes:
        return False
    def hashes(base):
        result = {}
        for directory, dirs, files in os.walk(native(base), followlinks=False):
            for name in files:
                p = Path(directory)/name
                # snapshot already rejects reparse points; recheck before reading.
                relative = os.path.relpath(str(p), native(base))
                guard.check(base/relative, base)
                result[relative] = sha256(p)
        return result
    return hashes(path) == hashes(target) and snapshot(path, path.parent, guard).fingerprint == a.fingerprint and snapshot(target, target.parent, guard).fingerprint == b.fingerprint


def completed_checkpoint(path):
    if os.lstat(native(path)).st_size > 2 * 1024 * 1024:
        return False
    value = json.loads(path.read_text(encoding="utf-8"))
    status = value.get("status", value.get("task", {}).get("status"))
    references = value.get("accepted_evidence", []) or value.get("evidence_refs", []) or value.get("task", {}).get("evidence_refs", [])
    return status in {"complete", "completed", "failed", "abandoned", "cancelled"} and not references


def index_flags_hide_changes(path, git):
    """Ordinary status omits skip-worktree and assume-unchanged payload edits."""
    entries = git(path, "ls-files", "-v", "-z").split(b"\0")
    return any(entry[:1] == b"S" or entry[:1].islower() for entry in entries if entry)


def clone_retained_elsewhere(path, backup, guard, git):
    marker = guard.check(path/".git", path, allow_missing=True)
    if not os.path.lexists(marker):
        return "bare-or-unclassified-git-manual-review"
    if not stat.S_ISDIR(os.lstat(native(marker)).st_mode):
        return "linked-worktree-other-owner"
    registry = guard.check(marker/"worktrees", path, allow_missing=True)
    if os.path.lexists(registry):
        return "owns-linked-worktree-registry"
    if os.path.lexists(marker/"objects/info/alternates") or os.path.lexists(marker/"commondir"):
        return "shared-object-store-manual-review"
    if os.path.lexists(path/".gitmodules"):
        return "submodules-manual-review"
    if index_flags_hide_changes(path, git):
        return "index-flags-manual-review"
    if git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"):
        return "dirty-without-verified-backup"
    if not backup:
        return "external-commit-backup-required"
    backup = absolute(backup)
    if overlaps(path, backup):
        return "external-commit-backup-required"
    guard.check(backup/".git", backup)
    if not (backup/".git").is_dir():
        return "external-commit-backup-required"
    snapshot(backup/".git", backup, guard)
    if os.path.lexists(backup/".git/objects/info/alternates") or os.path.lexists(backup/".git/commondir"):
        return "external-commit-backup-required"
    if git(backup, "rev-parse", "--is-shallow-repository").strip() != b"false":
        return "incomplete-shallow-backup"
    settings = git(backup, "config", "--local", "--list").splitlines()
    partial = False
    for setting in settings:
        key, separator, value = setting.lower().partition(b"=")
        if key == b"extensions.partialclone" or (key.endswith(b".promisor") and (not separator or value not in {b"false", b"no", b"off", b"0"})):
            partial = True
    packs = guard.check(backup/".git/objects/pack", backup, allow_missing=True)
    if partial or any(packs.glob("*.promisor")):
        return "incomplete-partial-backup"
    # --contains only verifies ancestry among available commits. Check complete
    # local object connectivity/integrity; a failure or timeout retains the clone.
    git(backup, "fsck", "--full", "--no-reflogs", "--no-dangling")
    refs = git(path, "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/tags", "refs/stash").decode().split()
    refs.append(git(path, "rev-parse", "HEAD").decode().strip())
    for commit in set(refs):
        if not git(backup, "for-each-ref", "--contains", commit, "--format=%(refname)", "refs/heads", "refs/tags", "refs/stash").strip():
            return "external-commit-backup-required"
    ignored = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if any(not set(Path(os.fsdecode(name)).parts) & REBUILDABLE for name in ignored.split(b"\0") if name):
        return "ignored-payload-without-verified-backup"
    return None


def preserve_capacity(home, needed, cap=PRESERVATION_CAP):
    root = home/"maintenance/preserved-worktrees"
    size = 0
    if os.path.lexists(root):
        size = snapshot(root, home, Guard(home)).bytes
    if size + needed > cap:
        raise ValueError("preservation-cap-reached-source-retained")


def outcome_pins(home, guard, record):
    roots = [home/"portable-harness"/name for name in ("artifacts", "evidence", "experiments", "state")]
    incomplete = False
    base = home/"portable-harness/checkpoints"
    try:
        guard.check(base, base.parent, allow_missing=True)
    except (OSError, ValueError):
        guard.pins.update(roots)
        record("outcome-references", base, "retained", reason="checkpoint-reference-index-unreadable")
        return roots
    for checkpoint in units(base, guard=guard):
        try:
            guard.check(checkpoint, base)
            if not checkpoint.is_file() or checkpoint.stat().st_size > 2 * 1024**2:
                incomplete = True; continue
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            def visit(item):
                nonlocal incomplete
                if isinstance(item, dict):
                    for key, data in item.items():
                        if key in {"accepted_evidence", "evidence_refs"} and isinstance(data, list):
                            for reference in data:
                                if isinstance(reference, str) and Path(reference).is_absolute():
                                    guard.pins.add(absolute(reference))
                                elif reference:
                                    incomplete = True
                        else:
                            visit(data)
                elif isinstance(item, list):
                    for data in item:visit(data)
            visit(value)
        except (OSError, ValueError, RecursionError):
            incomplete = True
    if incomplete:
        guard.pins.update(roots)
        record("outcome-references", base, "retained", reason="checkpoint-reference-index-incomplete")
    return roots


def sweep(home, apply, now, record, guard, config, git):
    """Each candidate has one disposition; shared roots require explicit ownership."""
    home = absolute(home)
    local = config.get("storage", {})
    permanent_pins = {absolute(p) for p in [*config.get("pinned_worktrees", []), *local.get("pinned", [])]}
    guard.pins.update(permanent_pins)
    owned = {absolute(p) for p in local.get("owned_paths", [])}
    backups = {str(absolute(k)): v for k, v in local.get("clone_backups", {}).items()}
    outcome_roots = outcome_pins(home, guard, record)
    repository = home/"portable-harness"
    tracked = set()
    if os.path.lexists(repository/".git"):
        try:
            guard.check(repository/".git", repository)
            if not (repository/".git").is_dir():
                raise ValueError("linked-harness-metadata-manual-review")
            tracked = {repository/os.fsdecode(name) for name in git(repository, "ls-files", "-z", "--", "artifacts", "evidence", "experiments", "state", "checkpoints").split(b"\0") if name}
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            guard.pins.update([*outcome_roots, repository/"checkpoints"])
            record("tracked-outcomes", repository, "blocked", error=str(exc))
    rules = [("tmp", home/"tmp", 7, False, True), ("legacy-tmp", home/".tmp", 7, False, True)]
    for name in ("visualizations", "generated_images", "harness-runs"):
        rules.append((name, home/name, 30, True, True))
    for name in ("artifacts", "evidence", "experiments", "checkpoints", "state"):
        rules.append((name, home/"portable-harness"/name, 30, False, True))
    for name in ("reports", "logs", "install-backups", "schedule-backups", "config-backups", "acl-backups", "preserved-worktrees"):
        rules.append((name, home/"maintenance"/name, 90 if name == "acl-backups" else 30, False, True))
    for name, paths in (("legacy-scratch", local.get("legacy_roots", [])), ("task-clone", local.get("clone_roots", []))):
        for root in paths:
            rules.append((name, absolute(root), 14 if name == "task-clone" else 7, name == "task-clone", False))
    for path in owned:
        rules.append(("registered-task", path, 7, False, False))
    for path in local.get("legacy_paths", []):
        rules.append(("legacy-exact", absolute(path), 7, False, False))
    seen = set()
    for kind, root, days, dated, implicit_owner in rules:
        if guard.forbidden(root):
            record(kind, root, "retained", reason="protected-path")
            continue
        try:
            guard.check(root, root.parent, allow_missing=True)
            if kind in {"registered-task", "legacy-exact"}:
                candidates, root = [root], root.parent
            elif kind == "task-clone":
                candidates = list(repository_units(root, guard))
            else:
                candidates = list(units(root, dated, guard))
            latest_backup = max(candidates, key=lambda p: os.lstat(native(p)).st_mtime) if candidates and kind in {"install-backups", "acl-backups"} else None
        except (OSError, ValueError) as exc:
            record(kind, root, "blocked", error=str(exc)); continue
        for path in candidates:
            path = absolute(path)
            if path in seen:
                continue
            seen.add(path)
            try:
                guard.check(path, root)
                value = receipt(path, guard) if stat.S_ISDIR(os.lstat(native(path)).st_mode) else {}
                if not implicit_owner and path not in owned and value.get("owner") != OWNER and kind != "task-clone":
                    record(kind, path, "retained", reason="ownership-unproven")
                    continue
                if guard.pinned(path) or value.get("pinned") or value.get("accepted") or value.get("state") == "running":
                    record(kind, path, "retained", reason="pinned-accepted-or-running" if value else "pinned-or-active")
                    continue
                if kind == "tmp" and path.name == "test-runs":
                    record(kind,path,"retained",reason="managed-test-run-sweep")
                    continue
                root_age = now - activity(os.lstat(native(path)))
                if root_age <= 3 * DAY:
                    record(kind, path, "retained", reason="recent-within-three-days")
                    continue
                if root_age <= days * DAY:
                    record(kind, path, "retained", reason="within-retention")
                    continue
                if kind == "task-clone":
                    marker=path/".git"
                    if not os.path.lexists(marker):
                        record(kind,path,"retained",reason="non-git-task-ownership-required");continue
                    if not stat.S_ISDIR(os.lstat(native(marker)).st_mode):
                        record(kind,path,"retained",reason="linked-worktree-other-owner");continue
                    if str(path) not in backups:
                        record(kind,path,"retained",reason="external-commit-backup-required");continue
                info = snapshot(path, root, guard)
                details = {"bytes": info.bytes, "files": info.files}
                reason = None
                if info.pinned:
                    reason = "pinned-or-active"
                elif now - info.latest <= 3 * DAY:
                    reason = "recent-within-three-days"
                elif now - info.latest <= days * DAY:
                    reason = "within-retention"
                elif path == latest_backup:
                    reason = "latest-rollback-backup"
                if value.get("pinned") or value.get("accepted") or value.get("state") == "running":
                    reason = "pinned-accepted-or-running"
                if kind == "tmp" and path.name == "test-runs":
                    reason = "managed-test-run-sweep"
                if not implicit_owner and path not in owned and value.get("owner") != OWNER and kind != "task-clone":
                    reason = reason or "ownership-unproven"
                if kind in {"state", "experiments"} and value.get("owner") != OWNER:
                    reason = reason or "governance-state-receipt-required"
                if kind == "checkpoints" and not reason and (not path.is_file() or not completed_checkpoint(path)):
                    reason = "active-unknown-or-referenced-checkpoint"
                if kind in {"artifacts", "evidence", "experiments", "state", "checkpoints"} and not reason:
                    if any(overlaps(path, p) for p in tracked):
                        reason = "git-tracked-outcome"
                if kind == "preserved-worktrees" and not reason:
                    target = value.get("redundant_copy")
                    if not target or not any(absolute(target).is_relative_to(pin) for pin in permanent_pins) or not duplicate_verified(path, target, guard):
                        reason = "sole-preserved-copy"
                if info.repositories and not reason:
                    if info.repositories != [str(path)]:
                        reason = "nested-repositories-manual-review"
                    elif now - info.latest <= 14 * DAY:
                        reason = "clone-within-fourteen-days"
                    else:
                        reason = clone_retained_elsewhere(path, backups.get(str(path)), guard, git)
                if kind == "task-clone" and not info.repositories:
                    reason = reason or "non-git-task-ownership-required"
                if reason:
                    record(kind, path, "retained", reason=reason, **details)
                else:
                    record(kind, path, "delete-expired", fingerprint=info.fingerprint, **details)
                    if apply:
                        remove(path, root, guard, info.fingerprint)
                        record(kind, path, "removed", **details)
            except FileNotFoundError:
                record(kind, path, "retained", reason="missing-or-concurrently-removed")
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                record(kind, path, "blocked", error=str(exc))
    for name in ("thread_history_1.sqlite", "state_5.sqlite"):
        path = home/name
        if os.path.lexists(path):
            record("app-database", path, "retained", reason="offline-backup-and-app-shutdown-required", bytes=os.lstat(native(path)).st_size)


PROJECT = re.compile(r'(?m)^\[projects\.("(?:[^"\\\r\n]|\\.)*"|\x27[^\x27\r\n]*\x27)\][ \t]*(?:\r?\n|$)')


def validate_toml(text):
    if tomllib is None:
        raise ValueError("TOML-editing-requires-python-3.11-tomllib")
    return tomllib.loads(text)


def toml_table_boundaries(text):
    validate_toml(text)
    # A complete TOML prefix can end before a real table header, never in an
    # unfinished multiline string or array. Use the standard parser as the lexer
    # so examples inside string values cannot become removable table boundaries.
    headers = []
    for match in re.finditer(r"(?m)^[ \t]*\[", text):
        try:
            validate_toml(text[:match.start()])
        except ValueError:
            continue
        headers.append(match.start())
    return headers


def project_sections(text):
    headers = toml_table_boundaries(text)
    boundaries = set(headers)
    result = []
    for match in PROJECT.finditer(text):
        if match.start() not in boundaries:
            continue
        token = match.group(1)
        try:
            path = json.loads(token) if token.startswith('"') else token[1:-1]
        except ValueError:
            continue
        end = next((start for start in headers if start > match.start()), len(text))
        result.append((path, match.start(), end))
    return result


def prune_projects(home, apply, now, record, guard):
    path = home/"config.toml"
    if not os.path.lexists(path):
        return
    guard.check(path, home)
    if guard.pinned(path):
        record("config", path, "retained", reason="pinned-or-active"); return
    if tomllib is None:
        record("config", path, "retained", reason="project-pruning-requires-python-3.11-tomllib"); return
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    sections = project_sections(text)
    ledger_path = guard.check(home/"maintenance/missing-projects.json", home, allow_missing=True)
    if os.path.lexists(ledger_path):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    else:
        ledger = {}
    retained, deletions, observations = {}, [], []
    for target, start, end in sections:
        reason = None
        target_path = absolute(target)
        entry_hash = hashlib.sha256(text[start:end].encode()).hexdigest()
        if guard.forbidden(target_path) or guard.pinned(target_path):
            reason = "protected-or-pinned-project"
        else:
            try:
                guard.check(target_path, target_path.anchor)
                reason = "project-path-exists"
            except FileNotFoundError:
                item = ledger.get(target, {})
                first = item.get("first_missing", now) if item.get("section_sha256") == entry_hash else now
                retained[target] = {"first_missing": first, "section_sha256": entry_hash}
                if now - first <= 3 * DAY:
                    reason = "missing-project-observation-three-days"
                else:
                    deletions.append((start, end))
                    observations.append(target)
            except (OSError, ValueError):
                reason = "project-unreadable-or-reparse"
        record("config-project", target_path, "retained" if reason else "prune-candidate", reason=reason)
    # The observation ledger is evidence, not removal authorization.
    atomic_write_json(ledger_path, retained)
    if not deletions:
        return
    changed = text
    for start, end in reversed(deletions):
        changed = changed[:start]+changed[end:]
    validate_toml(changed)
    fingerprint = hashlib.sha256(raw).hexdigest()
    record("config", path, "prune-projects", fingerprint=fingerprint, entries=observations, bytes=0)
    if apply:
        if path.read_bytes() != raw:
            raise ValueError("config-changed-concurrently")
        backup = guard.check(home/"maintenance/config-backups"/(fingerprint+".toml"), home, allow_missing=True)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists() and backup.read_bytes() != raw:
            raise ValueError("config-backup-conflict")
        if not backup.exists():
            backup.write_bytes(raw)
        temporary = path.with_name(path.name+".storage-new")
        if os.path.lexists(temporary):
            raise ValueError("config-temporary-collision")
        with temporary.open("xb") as stream:
            stream.write(changed.encode("utf-8")); stream.flush(); os.fsync(stream.fileno())
        if path.read_bytes() != raw:
            temporary.unlink(); raise ValueError("config-changed-concurrently")
        os.replace(temporary, path)
        record("config", path, "pruned", entries=observations)
