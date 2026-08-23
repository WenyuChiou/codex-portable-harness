from __future__ import annotations

import argparse
import datetime as dt
import uuid
from pathlib import Path

from common import active_codex_home, atomic_write_json, ensure_within, load_json
from validators import canonical_uuid, validate_memory_candidate


DECISIONS = ("approved", "rejected")
OPERATIONS = ("add", "archive", "supersede", "delete")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def proposal_store(override: str | None, home: Path) -> Path:
    home = home.resolve()
    store = Path(override).expanduser() if override else Path("memory-proposals")
    store = store.resolve() if store.is_absolute() else (home / store).resolve()
    ensure_within(store, home)
    return store


def propose(
    store: Path,
    allowed_home: Path,
    task_id: str,
    summary: str,
    evidence: list[str],
    operation: str = "add",
    target_ref: str | None = None,
) -> Path:
    ensure_within(store, allowed_home)
    if not summary.strip():
        raise ValueError("summary must not be empty")
    clean_evidence = sorted({item.strip() for item in evidence if item.strip()})
    if not clean_evidence:
        raise ValueError("at least one evidence reference is required")
    if operation not in OPERATIONS:
        raise ValueError(f"unsupported memory operation: {operation}")
    clean_target = target_ref.strip() if target_ref else None
    if operation != "add" and not clean_target:
        raise ValueError(f"{operation} proposals require a target reference")
    if operation == "add" and clean_target:
        raise ValueError("add proposals must not specify a target reference")
    identifier = str(uuid.uuid4())
    record = {
        "schema_version": 1,
        "id": identifier,
        "source_task": task_id,
        "operation": operation,
        "target_ref": clean_target,
        "summary": summary.strip(),
        "evidence_refs": clean_evidence,
        "state": "proposed",
        "created_at": now(),
        "decided_at": None,
        "approved_by": None,
    }
    validate_memory_candidate(record)
    path = store / f"{identifier}.json"
    atomic_write_json(path, record)
    return path


def decide(store: Path, allowed_home: Path, identifier: str, decision: str, approved_by: str) -> Path:
    ensure_within(store, allowed_home)
    if decision not in DECISIONS:
        raise ValueError(f"unsupported memory decision: {decision}")
    identifier = canonical_uuid(identifier)
    path = store / f"{identifier}.json"
    if path.is_symlink():
        raise ValueError("proposal files must not be symlinks")
    resolved = path.resolve()
    ensure_within(resolved, store)
    ensure_within(resolved, allowed_home)
    record = load_json(resolved)
    validate_memory_candidate(record)
    if record["id"] != identifier:
        raise ValueError("proposal id does not match its filename")
    if record.get("state") != "proposed":
        raise ValueError(f"proposal is already decided: {record.get('state')}")
    if not approved_by.strip():
        raise ValueError("approved-by must identify the human decision maker")
    record["state"] = decision
    record["decided_at"] = now()
    record["approved_by"] = approved_by.strip()
    validate_memory_candidate(record)
    atomic_write_json(resolved, record)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and decide memory proposals without mutating canonical memory.")
    parser.add_argument("--store")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("propose")
    create.add_argument("--task-id", required=True)
    create.add_argument("--summary", required=True)
    create.add_argument("--evidence", action="append", required=True)
    create.add_argument("--operation", choices=OPERATIONS, default="add")
    create.add_argument("--target-ref")

    decision = subparsers.add_parser("decide")
    decision.add_argument("--id", required=True)
    decision.add_argument("--decision", choices=DECISIONS, required=True)
    decision.add_argument("--approved-by", required=True)

    args = parser.parse_args()
    home = active_codex_home()
    store = proposal_store(args.store, home)
    if args.command == "propose":
        path = propose(store, home, args.task_id, args.summary, args.evidence, args.operation, args.target_ref)
    else:
        path = decide(store, home, args.id, args.decision, args.approved_by)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
