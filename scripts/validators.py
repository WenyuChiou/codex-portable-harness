from __future__ import annotations

import uuid
from typing import Any


HOST_KEYS = {
    "schema_version", "host_id", "os", "shell", "path_style", "sandbox",
    "pty", "gui", "roots", "available_tools",
}
CHECKPOINT_KEYS = {
    "schema_version", "task_id", "parent_id", "status", "evidence_refs",
    "metrics", "override", "stop_reason",
}
METRIC_KEYS = {
    "cycles", "tool_calls", "elapsed_seconds", "transcript_bytes",
    "last_checkpoint_bytes", "same_failure_retries", "no_evidence_cycles",
    "children_spawned", "active_children", "task_packet_tokens",
    "parent_summary_tokens", "memory_digest_tokens", "child_result_words",
}
MEMORY_KEYS = {
    "schema_version", "id", "source_task", "operation", "target_ref",
    "summary", "evidence_refs", "state", "created_at", "decided_at",
    "approved_by",
}


def _exact_keys(document: dict[str, Any], allowed: set[str], required: set[str], label: str) -> None:
    missing = required.difference(document)
    if missing:
        raise ValueError(f"missing {label} fields: {', '.join(sorted(missing))}")
    unexpected = set(document).difference(allowed)
    if unexpected:
        raise ValueError(f"unexpected {label} fields: {', '.join(sorted(unexpected))}")


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _unique_strings(value: object, label: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{label} must contain non-empty strings")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} must not contain duplicates")
    return value


def canonical_uuid(value: object) -> str:
    text = _nonempty_string(value, "id")
    try:
        normalized = str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise ValueError("id must be a canonical UUID") from exc
    if normalized != text:
        raise ValueError("id must be a lowercase canonical UUID")
    return normalized


def validate_host_manifest(document: dict[str, Any]) -> None:
    _exact_keys(document, HOST_KEYS, HOST_KEYS, "host")
    if document["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    _nonempty_string(document["host_id"], "host_id")
    _nonempty_string(document["shell"], "shell")
    if document["os"] not in {"windows", "macos", "linux"}:
        raise ValueError(f"invalid os: {document['os']}")
    expected_path_style = "windows" if document["os"] == "windows" else "posix"
    if document["path_style"] != expected_path_style:
        raise ValueError(f"path_style must be {expected_path_style} for {document['os']}")
    if document["sandbox"] not in {"supported", "limited", "unavailable", "unknown"}:
        raise ValueError(f"invalid sandbox capability: {document['sandbox']}")
    if not isinstance(document["pty"], bool) or not isinstance(document["gui"], bool):
        raise ValueError("pty and gui must be booleans")
    roots = document["roots"]
    if not isinstance(roots, dict) or set(roots) != {"codex_home"}:
        raise ValueError("roots must contain only codex_home")
    _nonempty_string(roots["codex_home"], "roots.codex_home")
    _unique_strings(document["available_tools"], "available_tools", allow_empty=True)


def validate_legacy_checkpoint(document: dict[str, Any], terminal_states: list[str]) -> None:
    """Validate the parse-only private checkpoint shape before public conversion."""
    required = {"schema_version", "task_id", "status", "evidence_refs", "metrics"}
    _exact_keys(document, CHECKPOINT_KEYS, required, "checkpoint")
    if document["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    _nonempty_string(document["task_id"], "task_id")
    parent = document.get("parent_id")
    if parent is not None:
        _nonempty_string(parent, "parent_id")
    states = {"running", *terminal_states}
    if document["status"] not in states:
        raise ValueError(f"invalid checkpoint status: {document['status']}")
    _unique_strings(document["evidence_refs"], "evidence_refs", allow_empty=True)
    metrics = document["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")
    required_metrics = METRIC_KEYS.difference({"last_checkpoint_bytes"})
    _exact_keys(metrics, METRIC_KEYS, required_metrics, "metric")
    for key, value in metrics.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"metrics.{key} must be a non-negative integer")
    override = document.get("override")
    if override is not None:
        if not isinstance(override, dict) or set(override) != {"reason", "limits"}:
            raise ValueError("override must contain reason and limits")
        _nonempty_string(override["reason"], "override.reason")
        if not isinstance(override["limits"], dict):
            raise ValueError("override.limits must be an object")
        for key, value in override["limits"].items():
            if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("override limits must be non-negative integers")
    stop_reason = document.get("stop_reason")
    if stop_reason is not None:
        _nonempty_string(stop_reason, "stop_reason")


def validate_memory_candidate(document: dict[str, Any]) -> None:
    _exact_keys(document, MEMORY_KEYS, MEMORY_KEYS, "memory")
    if document["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    canonical_uuid(document["id"])
    _nonempty_string(document["source_task"], "source_task")
    _nonempty_string(document["summary"], "summary")
    _unique_strings(document["evidence_refs"], "evidence_refs", allow_empty=False)
    operation = document["operation"]
    if operation not in {"add", "archive", "supersede", "delete"}:
        raise ValueError(f"invalid memory operation: {operation}")
    target = document["target_ref"]
    if operation == "add" and target is not None:
        raise ValueError("add proposals must not have a target_ref")
    if operation != "add":
        _nonempty_string(target, "target_ref")
    state = document["state"]
    if state not in {"proposed", "approved", "rejected"}:
        raise ValueError(f"invalid memory state: {state}")
    _nonempty_string(document["created_at"], "created_at")
    if state == "proposed":
        if document["decided_at"] is not None or document["approved_by"] is not None:
            raise ValueError("proposed memory must not contain decision metadata")
    else:
        _nonempty_string(document["decided_at"], "decided_at")
        _nonempty_string(document["approved_by"], "approved_by")
