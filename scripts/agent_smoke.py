"""Opt-in native dispatch acceptance; fixtures and traces remain host-local."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bootstrap import _reject_symlinks
from common import REPO_ROOT, active_codex_home, atomic_write_json, load_json, sha256
from policy_engine import evaluate
from setup_machine import python_at


def trace_for(home: Path, parent: str) -> Path:
    # Read only the exact session created by this controlled test invocation.
    uuid.UUID(parent)
    matches = list((home / "sessions").rglob("*" + parent + "*.jsonl"))
    if len(matches) != 1:
        raise ValueError("expected exactly one trace for the test parent")
    return matches[0]


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def gate(home: Path, folder: Path, index: int, *, complete: bool = False, passed: bool = True) -> dict:
    _reject_symlinks(folder, home)
    plan = load_json(folder / "plan.json")
    metrics = {name: 0 for name in ("cycles", "tool_calls", "elapsed_seconds", "transcript_bytes",
        "last_checkpoint_bytes", "same_failure_retries", "no_evidence_cycles", "children_spawned",
        "active_children", "task_packet_tokens", "parent_summary_tokens", "memory_digest_tokens", "child_result_words")}
    metrics.update(children_spawned=index, task_packet_tokens=len(json.dumps(plan["cases"][min(index, len(plan["cases"]) - 1)]).encode()))
    if plan.get("prior_evidence"):
        metrics["same_failure_retries"] = 1
    started = next((x["thread_id"] for x in events(folder / "events.jsonl") if x.get("type") == "thread.started"), None)
    if not started:
        raise ValueError("test parent identity unavailable; refusing dispatch")
    trace = trace_for(home, started)
    messages = events(trace)
    metrics["tool_calls"] = sum(x.get("payload", {}).get("type") == "function_call" for x in messages)
    metrics["transcript_bytes"] = trace.stat().st_size
    beginning = datetime.fromisoformat(messages[0]["timestamp"].replace("Z", "+00:00"))
    metrics["elapsed_seconds"] = max(0, int((datetime.now(timezone.utc) - beginning).total_seconds()))
    metrics["cycles"] = index
    final_words = []
    for event in messages:
        if event.get("payload", {}).get("type") == "agent_message":
            for block in event["payload"].get("content", []):
                text = block.get("text", "")
                if "Message Type: FINAL_ANSWER\n" in text and "Payload:\n" in text:
                    final_words.append(len(text.split("Payload:\n", 1)[1].split()))
    metrics["child_result_words"] = max(final_words, default=0)
    checkpoint = {"schema_version": 1, "task_id": "machine-smoke-" + folder.name, "parent_id": started,
        "status": ("completed" if passed else "error") if complete else "running", "metrics": metrics,
        "evidence_refs": [str(folder / "plan.json"), *plan.get("prior_evidence", [])],
        "stop_reason": ("acceptance passed" if passed else "acceptance failed; preserve evidence") if complete else None}
    decision = evaluate(checkpoint, load_json(REPO_ROOT / "policies/agent-budget.yaml"))
    return {"checkpoint": checkpoint, "decision": decision, "token_measurement": "UTF-8 byte upper bound; actual cost unknown"}


def fixtures(folder: Path, home: Path) -> list[list[dict]]:
    files = {
        "cache.py": 'cache = {}\ndef convert(amount, source, target):\n    key = (amount, source)\n    if key not in cache:\n        cache[key] = amount * {"cm": 100, "mm": 1000}[target]\n    return cache[key]\n',
        "loss.py": 'def loss(row):\n    try:\n        return float(row["depth"]) * 100\n    except (KeyError, ValueError):\n        return 0.0\ndef total(rows):\n    return sum(loss(row) for row in rows)\n',
        "settings.py": 'RETRY_LIMIT = 5\n', "consumer.py": 'MAX_RETRIES = 3\n',
        "clamp.py": 'def clamp(values, lower=0):\n    return [min(value, lower) for value in values]\n',
        "records.csv": 'symbol,value\nB,2\nA,3\nA,1\nB,2\n',
        "sentinel.txt": 'preserve this exact file\n',
        "test_clamp.py": 'import unittest\nfrom clamp import clamp\nclass Checks(unittest.TestCase):\n    def test_default(self): self.assertEqual(clamp([-2,3]),[0,3])\n    def test_empty(self): self.assertEqual(clamp([]),[])\n    def test_lower(self): self.assertEqual(clamp([1,3],2),[2,3])\n    def test_immutable(self):\n        values=[-1,2]; clamp(values); self.assertEqual(values,[-1,2])\nif __name__ == "__main__": unittest.main()\n',
    }
    for name, data in files.items():
        (folder / name).write_text(data, encoding="utf-8")
    path = lambda name: str(folder / name)
    supplied = 'Supplied records: R1=(beta,2), R2=(alpha,3), R3=(beta,2), R4=(alpha,1). Return deduplicated pairs ordered by label then number, including original record references. Include "alpha", "beta", "R4", "R2", "R1", "R3".'
    case = lambda role, task, words: {"agent_type": role, "task": task, "required_terms": words}
    readers = [
        case("portable-mechanical-readonly", supplied, ["alpha", "beta", "R4", "R2", "R1", "R3"]),
        case("portable-explorer", f'Read only {path("cache.py")}. Trace convert(1,"m","cm") then convert(1,"m","mm") in one process. Give actual 100, 100 versus intended 1000, cause and source lines. No execution.', ["100", "1000", "target"]),
        case("portable-quality-explorer", f'Read only {path("cache.py")}. Diagnose the missing target in the cache key and the incorrect second conversion (100 instead of 1000) with file/line evidence. No execution.', ["100", "1000", "target"]),
    ]
    auditors = [
        case("code-reviewer", f'Review this tiny fixture: settings.py RETRY_LIMIT changed from 3 to 5, but {path("consumer.py")} still uses MAX_RETRIES=3. Read both files and report parity defect, trigger and lines. No edits.', ["5", "3", "consumer"]),
        case("silent-failure-hunter-py", f'Read only {path("loss.py")}. Find swallowed missing/invalid required depth and its downstream impact through total. Give file/lines and severity. No execution.', ["depth", "total"]),
        case("banned-word-auditor", 'Audit this supplied sentence using only the installed academic corpus and current prose rules: "We delve into the intricate landscape of robust mechanisms." Return located terms and concise alternatives; no rewrite or file writes.', ["delve"]),
    ]
    writers = [
        case("harness-drift-monitor", f'Use this installed response-only Python helper to audit {home / "agents"}, {home / "portable-harness/bootstrap-ownership.json"}, and EVERY manifest-listed file inside {home}, including skills: {python_at(home / "portable-harness-runtime")} -B {REPO_ROOT / "scripts/audit_deployment.py"}. This avoids unsupported PowerShell path-normalization methods in constrained language. Run once and report actual agent_count, managed_file_count, checked_file_count and managed_mismatches; distinguish incomplete checks. Legacy signals and usage are out of scope. No config, auth, sessions, databases or caches.', ["managed_mismatches", "0", "checked_file_count", "9"]),
        case("portable-mechanical", f'Read {path("records.csv")}, remove exact duplicates, sort symbol then numeric value; write only {path("normalized.csv")} retaining header symbol,value. Verify and stop.', ["A", "B"]),
        case("portable-worker", f'Change only {path("clamp.py")} so every output is at least lower, preserving order and input. Read but do not change test_clamp.py. Run the original venv exactly, no system Python fallback: {python_at(home / "research-runtime")} -B {path("test_clamp.py")}. Return actual four-test result and stop.', ["4", "OK"]),
    ]
    return [readers, auditors, writers]


def native_evidence(home: Path, parent: str) -> dict:
    trace = trace_for(home, parent)
    calls, outputs, finals = {}, {}, {}
    for event in events(trace):
        payload = event.get("payload", {})
        if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
            arguments = json.loads(payload["arguments"])
            calls[payload["call_id"]] = {k: arguments.get(k) for k in ("agent_type", "model", "fork_turns")}
        elif payload.get("type") == "function_call_output":
            outputs[payload["call_id"]] = payload.get("output")
        elif payload.get("type") == "agent_message":
            for block in payload.get("content", []):
                text = block.get("text", "")
                if "Message Type: FINAL_ANSWER\n" in text and "Payload:\n" in text:
                    sender = text.split("Sender: ", 1)[1].split("\n", 1)[0]
                    finals[sender] = text.split("Payload:\n", 1)[1].strip()
    dispatch = []
    for call_id, value in calls.items():
        dispatched = json.loads(outputs.get(call_id) or "{}")
        dispatch.append({**value, "agent_id": dispatched.get("task_name")})
    return {"trace": str(trace), "dispatch": dispatch, "finals": finals}


def accepted(cases: list[dict], result: dict, evidence: dict) -> bool:
    actual = result.get("cases", [])
    dispatched = evidence["dispatch"]
    if len(actual) != len(cases) or len(dispatched) != len(cases):
        return False
    for expected, found, call in zip(cases, actual, dispatched):
        if (found["agent_type"] != expected["agent_type"] or call["agent_type"] != expected["agent_type"]
            or call["model"] is not None or call["fork_turns"] != "none"
            or not call["agent_id"] or found["agent_id"] != call["agent_id"] or found["status"] != "completed"
            or found["answer"].strip() != evidence["finals"].get(call["agent_id"])
            or not all(term.lower() in found["answer"].lower() for term in expected["required_terms"])):
            return False
    return True


def smoke(home: Path, *, codex: str, controller_model: str | None, selected: list[str] | None = None, prior_evidence: list[str] | None = None) -> dict:
    folder = home / "machine-setup/agent-smoke" / uuid.uuid4().hex
    _reject_symlinks(folder, home)
    folder.mkdir(parents=True)
    groups = fixtures(folder, home)
    available = {case["agent_type"] for group in groups for case in group}
    if selected and not set(selected) <= available:
        raise ValueError("unknown requested agent type")
    if selected:
        groups = [[case for case in group if case["agent_type"] in selected] for group in groups]
        groups = [group for group in groups if group]
    roles = [case["agent_type"] for group in groups for case in group]
    before = {str(p): sha256(p) for p in folder.iterdir() if p.is_file()
              and (p.name != "clamp.py" or "portable-worker" not in roles)}
    definitions = {str(p): sha256(p) for p in (home / "agents").glob("*.toml")}
    schema = {"type": "object", "properties": {"cases": {"type": "array", "items": {"type": "object", "properties": {
        "agent_type": {"type": "string"}, "agent_id": {"type": "string"}, "status": {"type": "string"}, "answer": {"type": "string"}},
        "required": ["agent_type", "agent_id", "status", "answer"], "additionalProperties": False}}}, "required": ["cases"], "additionalProperties": False}
    atomic_write_json(folder / "schema.json", schema)
    reports = []
    for group_index, cases in enumerate(groups):
        run = folder / ("batch-" + str(group_index))
        run.mkdir()
        atomic_write_json(run / "plan.json", {"cases": cases, "prior_evidence": prior_evidence or []})
        prompt = 'You are a native-agent installation-test controller. For each ordered case, run its exact gate command first; stop if spawn_allowed is false. Spawn the exact installed agent_type, with fork_turns="none" and no model/effort override; one child at a time, no retries or substitutes. Include your actual parent identity, task, expected evidence, only named writable paths and stop condition. All children must not delegate. Wait for completion and release its slot before the next case. Do not perform child tasks or fix their answers. Return each actual child ID, status and verbatim answer. Failure stays failure. Read-only tasks are response-only; writer tasks may change only their named fixture. No credential/auth/session inspection, service calls or installations. Keep results under 180 words per child.\n'
        for index, packet in enumerate(cases):
            gate_command = [str(python_at(home / "portable-harness-runtime")), "-B", str(REPO_ROOT / "scripts/agent_smoke.py"), "--gate", str(run), str(index)]
            prompt += json.dumps({"case": packet, "gate_argv": gate_command}) + "\n"
        writes = any(case["agent_type"] in {"portable-worker", "portable-mechanical"} for case in cases)
        command = [codex, "exec", "--json", "--ignore-user-config", "--sandbox", "workspace-write" if writes else "read-only",
                   "--skip-git-repo-check", "-C", str(folder), "--output-schema", str(folder / "schema.json"), "--output-last-message", str(run / "result.json")]
        if os.name == "nt":
            command += ["-c", 'windows.sandbox="elevated"']
        if controller_model:
            command += ["--model", controller_model]
        command.append(prompt)
        print(f"Native batch {group_index + 1}/{len(groups)}", file=sys.stderr, flush=True)
        try:
            with (run / "events.jsonl").open("w", encoding="utf-8") as output, (run / "stderr.log").open("w", encoding="utf-8") as error:
                process = subprocess.run(command, stdout=output, stderr=error, timeout=420)
            if process.returncode:
                raise RuntimeError(f"native controller failed: exit {process.returncode}")
            parent = next(x["thread_id"] for x in events(run / "events.jsonl") if x.get("type") == "thread.started")
            evidence = native_evidence(home, parent)
            result = load_json(run / "result.json")
            passed = accepted(cases, result, evidence)
            completion = gate(home, run, len(cases), complete=True, passed=passed)
            record = {"passed": passed, "parent": parent, "result": result, "evidence": evidence, "completion": completion}
            atomic_write_json(run / "acceptance.json", record)
            reports.append(record)
            if not passed:
                break
        except (OSError, RuntimeError, ValueError, StopIteration, subprocess.SubprocessError) as exc:
            reports.append({"passed": False, "error_type": type(exc).__name__})
            break
    protected = all(Path(p).exists() and sha256(Path(p)) == digest for p, digest in {**before, **definitions}.items())
    protected = protected and {str(p): sha256(p) for p in (home / "agents").glob("*.toml")} == definitions
    permitted_root = {Path(p).name for p in before} | {"clamp.py", "schema.json"}
    if "portable-mechanical" in roles:
        permitted_root.add("normalized.csv")
    protected = protected and {p.name for p in folder.iterdir() if p.is_file()} <= permitted_root
    protected = protected and {p.name for p in folder.iterdir() if p.is_dir()} <= {"batch-" + str(i) for i in range(len(groups))}
    csv_ok, worker_ok = "portable-mechanical" not in roles, "portable-worker" not in roles
    if "portable-mechanical" in roles and (folder / "normalized.csv").exists():
        with (folder / "normalized.csv").open(newline="", encoding="utf-8") as handle:
            csv_ok = list(csv.reader(handle)) == [["symbol", "value"], ["A", "1"], ["A", "3"], ["B", "2"]]
    if "portable-worker" in roles and len(reports) == len(groups) and all(r["passed"] for r in reports):
        worker = subprocess.run([str(python_at(home / "research-runtime")), "-B", str(folder / "test_clamp.py")], capture_output=True, timeout=30)
        worker_ok = worker.returncode == 0 and b"Ran 4 tests" in worker.stderr
    result = {"status": "PASS" if len(reports) == len(groups) and all(r["passed"] for r in reports) and protected and csv_ok and worker_ok else "FAIL",
              "kind": "bounded native acceptance, not a performance benchmark", "groups": reports,
              "requested_agents": roles,
              "protected_files_unchanged": protected,
              "csv_verified": csv_ok if "portable-mechanical" in roles else None,
              "worker_four_tests_verified": worker_ok if "portable-worker" in roles else None,
              "evidence_directory": str(folder), "original_venv_required": True}
    atomic_write_json(folder / "summary.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize paid native test calls; no login, fallback or permission widening")
    parser.add_argument("--gate", nargs=2, metavar=("RUN_FOLDER", "CASE_INDEX"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--controller-model", help="Controller only; installed child definitions still select/inherit their model")
    parser.add_argument("--agent", action="append", help="Target an installed suite agent; repeat to select multiple (default: all nine)")
    parser.add_argument("--prior-evidence", action="append", help="Preserve a previous failed run when relevant inputs were corrected")
    args = parser.parse_args()
    home = active_codex_home()
    if args.gate:
        result = gate(home, Path(args.gate[0]), int(args.gate[1]))
        print(json.dumps(result))
        return 0 if result["decision"]["spawn_allowed"] else 4
    if not args.run:
        print("NOT_RUN: use --run to execute nine native agent acceptance tasks; no paid calls made.")
        return 0
    result = smoke(home, codex=args.codex, controller_model=args.controller_model, selected=args.agent, prior_evidence=args.prior_evidence)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
