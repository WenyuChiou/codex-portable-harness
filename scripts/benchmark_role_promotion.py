"""Per-host paired Codex model admission; raw evidence never enters Git."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import random
import re
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from benchmark_autonomy import (_source_fingerprint, _status,
                                _materialize, build_command, normalize_usage,
                                output_lock)
from common import REPO_ROOT, active_codex_home, load_json
import model_promotion

CASES = REPO_ROOT / "fixtures" / "role_promotion" / "cases.json"
POLICY = REPO_ROOT / "policies" / "model-routing.json"


def load_cases(task_class: str) -> list[dict[str, Any]]:
    document = load_json(CASES)
    if document.get("schema_version") != 1 or not isinstance(document.get("cases"), list):
        raise ValueError("invalid role-promotion fixture schema")
    selected = [case for case in document["cases"] if case.get("class") == task_class]
    required = 4 if task_class == "implementation" else 2
    if len(selected) != required or len({case["id"] for case in selected}) != required:
        raise ValueError("role-promotion fixtures do not cover the role")
    for case in selected:
        if (set(case) != {"id", "class", "sandbox", "prompt", "files", "verify"}
            or case["sandbox"] != ("read-only" if task_class == "exploration" else "workspace-write")
            or not isinstance(case["prompt"], str) or not case["prompt"].strip()
            or not isinstance(case["files"], dict) or not case["files"]
            or not isinstance(case["verify"], dict)):
            raise ValueError("invalid role-promotion case")
        for name, content in case["files"].items():
            from benchmark_autonomy import _validate_fixture_entry
            _validate_fixture_entry(name, content)
    return selected


def schedule(cases: list[dict[str, Any]], seed: int) -> list[tuple[str, dict[str, Any], tuple[str, str]]]:
    rounds = 2 if len(cases) == 4 else 4
    pairs = [(f"{case['id']}-{index + 1}", case) for case in cases for index in range(rounds)]
    rng = random.Random(seed)
    rng.shuffle(pairs)
    return [(key, case, ("control", "candidate") if rng.randrange(2) == 0 else ("candidate", "control"))
            for key, case in pairs]


def run_codex(*, command: list[str], prompt: str, cwd: Path, timeout: int) -> dict[str, Any]:
    start = time.monotonic()
    environment = os.environ.copy()
    # Keep both arms identical and avoid Windows sandbox-owned __pycache__
    # directories that the verifier cannot inspect or remove.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        process = subprocess.run(command, input=prompt, cwd=cwd, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 shell=False, timeout=timeout, check=False, env=environment)
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": None, "timeout": True, "wall_seconds": time.monotonic() - start,
                "stdout": exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or "",
                "stderr": exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""}
    return {"exit_code": process.returncode, "timeout": False,
            "wall_seconds": time.monotonic() - start, "stdout": process.stdout,
            "stderr": process.stderr}


def parse_codex(raw: dict[str, Any]) -> dict[str, Any]:
    events = []
    for line in raw.get("stdout", "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    turns = [event for event in events if event.get("type") == "turn.completed"]
    usage = normalize_usage(turns[-1].get("usage")) if turns else None
    messages = [event.get("item", {}).get("text", "") for event in events
                if event.get("type") == "item.completed" and isinstance(event.get("item"), dict)
                and event["item"].get("type") == "agent_message"]
    errors = [event for event in events if event.get("type") in {"turn.failed", "error"}]
    retries = sum(event.get("type") in {"turn.restarted", "turn.retry"} for event in events)
    return {"ok": raw.get("exit_code") == 0 and not raw.get("timeout") and len(turns) == 1
            and not errors and usage is not None,
            "usage": usage, "answer": "\n".join(messages), "retries": retries,
            "errors": len(errors)}


def safe_fixture_tree(root: Path) -> tuple[str, set[str]]:
    """Reject links/reparse points before reading model-controlled output."""
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink():
        raise ValueError("unsafe fixture root")
    digest = hashlib.sha256()
    names: set[str] = set()
    def deny_unreadable(exc: OSError) -> None:
        raise ValueError("fixture tree cannot be fully inspected") from exc

    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False,
                                          onerror=deny_unreadable):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise ValueError("fixture contains a link or reparse point")
            if name == ".git" and Path(directory) == root:
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("unsafe fixture Git metadata")
                continue
            if stat.S_ISDIR(info.st_mode):
                relative = path.relative_to(root).as_posix() + "/"
                names.add(relative)
                digest.update(b"D")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ValueError("fixture contains a non-regular or linked file")
            relative = path.relative_to(root).as_posix()
            names.add(relative)
            digest.update(b"F")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        if Path(directory) == root:
            dirs[:] = [name for name in dirs if name != ".git"]
    return digest.hexdigest(), names


def _function(path: Path, name: str) -> tuple[ast.Module, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"missing or duplicate function: {name}")
    return tree, matches[0]


def _module_shape(tree: ast.Module, *, functions: dict[str, tuple[str, ...]],
                  imports: set[tuple[str, str, str]] = frozenset(),
                  classes: set[str] = frozenset()) -> bool:
    found_functions: dict[str, tuple[str, ...]] = {}
    found_imports: set[tuple[str, str, str]] = set()
    found_classes: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if (node.name in found_functions or node.decorator_list or node.returns is not None
                or node.args.defaults or any(default is not None for default in node.args.kw_defaults)
                or node.args.vararg or node.args.kwarg or node.args.kwonlyargs or node.args.posonlyargs
                or any(argument.annotation is not None for argument in node.args.args)):
                return False
            found_functions[node.name] = tuple(argument.arg for argument in node.args.args)
        elif isinstance(node, ast.Import) and len(node.names) == 1 and node.names[0].asname is None:
            item = ("import", node.names[0].name, "")
            if item in found_imports:
                return False
            found_imports.add(item)
        elif (isinstance(node, ast.ImportFrom) and node.level == 0 and len(node.names) == 1
              and node.names[0].asname is None):
            item = ("from", node.module or "", node.names[0].name)
            if item in found_imports:
                return False
            found_imports.add(item)
        elif isinstance(node, ast.ClassDef) and not node.decorator_list and not node.keywords:
            if node.name in found_classes or len(node.body) != 1 or not isinstance(node.body[0], ast.Pass):
                return False
            found_classes.add(node.name)
        else:
            return False
    return found_functions == functions and found_imports == imports and found_classes == classes


def _return_expression(function: ast.FunctionDef) -> ast.expr:
    if len(function.body) != 1 or not isinstance(function.body[0], ast.Return) or function.body[0].value is None:
        raise ValueError("function must use one return expression")
    return function.body[0].value


def _safe_expr(node: ast.expr, value: Any) -> Any:
    """Interpret a tiny expression grammar; never execute fixture source."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "value":
            return value
        if node.id == "str":
            return str
        raise ValueError("unexpected name in fixture expression")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_safe_expr(part, value)) for part in node.values)
        if isinstance(node.op, ast.Or):
            return any(bool(_safe_expr(part, value)) for part in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _safe_expr(node.operand, value)
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        left, right = _safe_expr(node.left, value), _safe_expr(node.comparators[0], value)
        operation = node.ops[0]
        if isinstance(operation, (ast.IsNot, ast.NotEq)):
            return left is not right if isinstance(operation, ast.IsNot) else left != right
        if isinstance(operation, (ast.Is, ast.Eq)):
            return left is right if isinstance(operation, ast.Is) else left == right
    if isinstance(node, ast.Call) and not node.keywords:
        arguments = [_safe_expr(argument, value) for argument in node.args]
        if isinstance(node.func, ast.Name):
            if node.func.id == "isinstance" and len(arguments) == 2 and arguments[1] is str:
                return isinstance(arguments[0], str)
            if node.func.id == "bool" and len(arguments) == 1:
                return bool(arguments[0])
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "fullmatch"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
            and len(arguments) == 2 and arguments[0] == "[a-z0-9-]+"
            and isinstance(arguments[1], str)):
            return re.fullmatch(arguments[0], arguments[1])
    raise ValueError("unsupported fixture expression")


def verify_ast_rule(rule: str, workspace: Path) -> bool:
    if rule == "sum-and-report":
        total_tree, total = _function(workspace / "math_ops.py", "total")
        report_tree, report = _function(workspace / "report.py", "total_prices")
        return (_module_shape(total_tree, functions={"total": ("items",)})
                and _module_shape(report_tree, functions={"total_prices": ("items",)},
                                  imports={("from", "math_ops", "total")})
                and ast.dump(_return_expression(total)) == ast.dump(ast.parse("sum(items)", mode="eval").body)
                and ast.dump(_return_expression(report)) == ast.dump(ast.parse("total(items)", mode="eval").body))
    if rule == "slug-and-service":
        validator_tree, validator = _function(workspace / "validator.py", "valid_slug")
        service_tree, service = _function(workspace / "service.py", "is_accepted")
        if (not _module_shape(validator_tree, functions={"valid_slug": ("value",)},
                              imports={("import", "re", "")})
            or not _module_shape(service_tree, functions={"is_accepted": ("value",)},
                                 imports={("from", "validator", "valid_slug")})
            or ast.dump(_return_expression(service)) != ast.dump(ast.parse("valid_slug(value)", mode="eval").body)):
            return False
        expression = _return_expression(validator)
        samples = {"a": True, "a-1": True, "0": True, "--": True, "": False,
                   "A": False, "a b": False, "_": False, "é": False,
                   None: False, 42: False}
        return all(bool(_safe_expr(expression, sample)) == expected for sample, expected in samples.items())
    if rule == "nonempty-id":
        validator_tree, validator = _function(workspace / "validator.py", "valid_id")
        if not _module_shape(validator_tree, functions={"valid_id": ("value",)}):
            return False
        expression = _return_expression(validator)
        samples = {"": False, "a b": True, " x ": True, None: False,
                   42: False, True: False}
        return all(bool(_safe_expr(expression, sample)) == expected for sample, expected in samples.items())
    if rule == "save-exception-chain":
        tree, save = _function(workspace / "storage.py", "save")
        errors = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StorageError"]
        if (not _module_shape(tree, functions={"save": ("writer", "value")}, classes={"StorageError"})
            or len(errors) != 1 or len(errors[0].bases) != 1
            or not isinstance(errors[0].bases[0], ast.Name) or errors[0].bases[0].id != "Exception"
            or len(save.body) != 1 or not isinstance(save.body[0], ast.Try)):
            return False
        statement = save.body[0]
        if len(statement.body) != 1 or len(statement.handlers) != 1 or statement.orelse or statement.finalbody:
            return False
        call = statement.body[0].value if isinstance(statement.body[0], (ast.Return, ast.Expr)) else None
        handler = statement.handlers[0]
        raised = handler.body[0] if len(handler.body) == 1 else None
        return (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "writer"
                and len(call.args) == 1 and not call.keywords
                and isinstance(call.args[0], ast.Name) and call.args[0].id == "value"
                and isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
                and isinstance(handler.name, str) and isinstance(raised, ast.Raise)
                and isinstance(raised.exc, ast.Call) and isinstance(raised.exc.func, ast.Name)
                and raised.exc.func.id == "StorageError" and not raised.exc.keywords
                and len(raised.exc.args) <= 1
                and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        for arg in raised.exc.args)
                and isinstance(raised.cause, ast.Name)
                and raised.cause.id == handler.name)
    return False


def verify(case: dict[str, Any], workspace: Path, answer: str, original_hash: str) -> bool:
    rule = case["verify"]
    try:
        if "answer_contains" in rule:
            return rule["answer_contains"] in answer and safe_fixture_tree(workspace)[0] == original_hash
        if "json_file" in rule:
            return json.loads((workspace / rule["json_file"]).read_text(encoding="utf-8")) == rule["equals"]
        if "ast_rule" in rule:
            return verify_ast_rule(rule["ast_rule"], workspace)
    except (OSError, ValueError, SyntaxError, TypeError, subprocess.SubprocessError):
        return False
    return False


def cost_proxy(usage: dict[str, int | None] | None, prices: dict[str, float]) -> float | None:
    if usage is None or any(type(usage.get(key)) is not int for key in
                            ("uncached_input_tokens", "cached_input_tokens", "output_tokens")):
        return None
    return sum(usage[key] * prices[price] for key, price in
               (("uncached_input_tokens", "input"), ("cached_input_tokens", "cached_input"),
                ("output_tokens", "output"))) / 1_000_000


def evaluate(result: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    runs = result["runs"]
    pairs = {}
    for run in runs:
        pairs.setdefault(run["pair"], {})[run["arm"]] = run
    if len(pairs) != 8 or any(set(pair) != {"control", "candidate"} for pair in pairs.values()) or len(runs) != 16:
        return False, "eight complete pairs are required", {}
    if result["source_unchanged"] is not True or any(
        pair["control"]["fixture_sha256"] != pair["candidate"]["fixture_sha256"]
        for pair in pairs.values()
    ):
        return False, "source or paired fixture changed", {}
    if any(not run["ok"] or run["timeout"] for run in runs):
        return False, "all paired calls need complete comparable usage", {}
    if any(not run["verified"] for run in runs if run["arm"] == "candidate"):
        return False, "all candidate quality checks must pass", {}
    if any(type(run.get("retries")) is not int or run["retries"] < 0
           or type(run.get("sandbox_violation")) is not int or run["sandbox_violation"] < 0
           or type(run.get("wall_seconds")) not in (int, float)
           or not math.isfinite(run["wall_seconds"]) or run["wall_seconds"] <= 0
           or type(run.get("cost_proxy_usd")) not in (int, float)
           or not math.isfinite(run["cost_proxy_usd"]) or run["cost_proxy_usd"] < 0
           for run in runs):
        return False, "invalid paired measurements", {}
    wall, cost = [], []
    for pair in pairs.values():
        control, candidate = pair["control"], pair["candidate"]
        if control["wall_seconds"] <= 0 or control["cost_proxy_usd"] is None or control["cost_proxy_usd"] <= 0 or candidate["cost_proxy_usd"] is None:
            return False, "comparable wall-clock and cost proxy unavailable", {}
        wall.append((candidate["wall_seconds"] / control["wall_seconds"] - 1) * 100)
        cost.append((candidate["cost_proxy_usd"] / control["cost_proxy_usd"] - 1) * 100)
    metrics = {"median_wall_regression_percent": statistics.median(wall),
               "median_cost_proxy_regression_percent": statistics.median(cost),
               "control_failures": sum(not run["verified"] for run in runs if run["arm"] == "control"),
               "candidate_failures": sum(not run["verified"] for run in runs if run["arm"] == "candidate"),
               "control_timeouts": sum(run["timeout"] for run in runs if run["arm"] == "control"),
               "candidate_timeouts": sum(run["timeout"] for run in runs if run["arm"] == "candidate"),
               "control_retries": sum(run["retries"] for run in runs if run["arm"] == "control"),
               "candidate_retries": sum(run["retries"] for run in runs if run["arm"] == "candidate"),
               "sandbox_violations": sum(run["sandbox_violation"] for run in runs)}
    gate = policy["role_promotion"]
    if (metrics["median_wall_regression_percent"] > gate["maximum_wall_regression_percent"]
        or metrics["median_cost_proxy_regression_percent"] > gate["maximum_cost_proxy_regression_percent"]
        or metrics["candidate_retries"] > metrics["control_retries"]
        or metrics["sandbox_violations"]):
        return False, "performance, retry, or sandbox gate failed", metrics
    return True, "eight paired quality and regression gates passed", metrics


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_host_id(home: Path) -> str:
    existing = model_promotion.read_host_salt(home)
    if existing:
        return existing
    path = model_promotion.host_id_path(home)
    if path.exists() or path.is_symlink() or path.parent.is_symlink():
        raise ValueError("invalid existing host identity")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = hashlib.sha256(os.urandom(32)).hexdigest()
    with path.open("x", encoding="ascii") as handle:
        handle.write(value + "\n")
    return value


def run_benchmark(home: Path, task_class: str, *, seed: int, executable: str = "codex",
                  runner: Callable[..., dict[str, Any]] = run_codex,
                  timeout: int = 180) -> dict[str, Any]:
    policy = load_json(POLICY)
    cases = load_cases(task_class)
    _ensure_host_id(home)
    environment = model_promotion.current_environment(home, executable=executable)
    if environment is None:
        raise RuntimeError("Codex version, service tier, or host identity unavailable")
    baseline, candidate = policy["routes"][task_class], policy["role_promotion"]["candidates"][task_class]
    prices = policy["role_promotion"]["api_usd_per_million_tokens"]
    source_before, status_before = _source_fingerprint(REPO_ROOT), _status(REPO_ROOT)
    output = home / "experiments" / "model-role-promotion" / f"{task_class}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    fixture_root = output.parent / "temporary-fixtures"
    fixture_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"schema_version": 1, "task_class": task_class, "seed": seed,
                              "warmups": 2, "environment": environment, "suite_sha256": model_promotion.suite_sha256(),
                              "policy_sha256": model_promotion.canonical_policy_sha256(policy),
                              "price_source": policy["role_promotion"]["price_source"],
                              "price_checked_at": policy["role_promotion"]["price_checked_at"],
                              "cost_kind": "official_API_price_proxy_not_ChatGPT_billing", "runs": [],
                              "warmup_results": [], "complete": False, "admitted": False,
                              "source_fingerprint_before": source_before,
                              "source_status_before": status_before}

    def one(arm: str, case: dict[str, Any], pair: str | None = None) -> dict[str, Any]:
        task_start = time.monotonic()
        route = baseline if arm == "control" else candidate
        with tempfile.TemporaryDirectory(dir=fixture_root, ignore_cleanup_errors=True) as temporary:
            workspace = Path(temporary) / "fixture"
            _materialize(case, workspace)
            initialized = subprocess.run(["git", "init", "-q"], cwd=workspace,
                                         capture_output=True, shell=False, timeout=10)
            if initialized.returncode:
                raise RuntimeError("cannot initialize isolated fixture repository")
            initial_hash, _ = safe_fixture_tree(workspace)
            raw = runner(command=build_command(executable, model=route["model"], effort=route["effort"],
                                               sandbox=case["sandbox"]), prompt=case["prompt"], cwd=workspace,
                         timeout=timeout)
            parsed = parse_codex(raw)
            try:
                observed_hash, observed_files = safe_fixture_tree(workspace)
                expected_entries = set(case["files"])
                expected_entries.update(
                    "/".join(name.split("/")[:index]) + "/"
                    for name in case["files"] for index in range(1, len(name.split("/")))
                )
                unexpected_change = (observed_files != expected_entries
                                     or any((workspace / name).read_text(encoding="utf-8") != content
                                            for name, content in case["files"].items() if name == "REVIEW.txt"))
                checked = parsed["ok"] and verify(case, workspace, parsed["answer"], initial_hash)
            except (OSError, ValueError, UnicodeError):
                observed_hash, observed_files = None, set()
                unexpected_change, checked = True, False
            return {"pair": pair, "case_id": case["id"], "arm": arm,
                    "model": route["model"], "effort": route["effort"],
                    "fixture_sha256": initial_hash, "post_fixture_sha256": observed_hash,
                    "sandbox_violation": int(unexpected_change or (case["sandbox"] == "read-only" and observed_hash != initial_hash)),
                    "ok": parsed["ok"], "verified": checked, "timeout": raw["timeout"],
                    "exit_code": raw["exit_code"], "wall_seconds": time.monotonic() - task_start,
                    "model_process_seconds": raw["wall_seconds"],
                    "retries": parsed["retries"], "usage": parsed["usage"],
                    "cost_proxy_usd": cost_proxy(parsed["usage"], prices[route["model"]]),
                    "raw_stdout": raw["stdout"], "raw_stderr": raw["stderr"]}

    with output_lock(output):
        try:
            # Candidate preflight first: an unsupported model fails after one call,
            # never after eight repeated refusals. Warmups are excluded from stats.
            for arm in ("candidate", "control"):
                warmup = one(arm, cases[0])
                result["warmup_results"].append(warmup)
                _atomic_json(output, result)
                if (not warmup["ok"] or warmup["timeout"] or warmup["sandbox_violation"]
                    or (arm == "candidate" and not warmup["verified"])):
                    raise RuntimeError(f"{arm} warmup failed; inspect local raw evidence")
            for pair, case, order in schedule(cases, seed):
                for arm in order:
                    completed = one(arm, case, pair)
                    result["runs"].append(completed)
                    _atomic_json(output, result)
                    if (not completed["ok"] or completed["timeout"] or completed["sandbox_violation"]
                        or (arm == "candidate" and not completed["verified"])):
                        raise RuntimeError("paired run failed quality or sandbox gate; inspect local raw evidence")
            result["complete"] = True
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            result["source_fingerprint_after"] = _source_fingerprint(REPO_ROOT)
            result["source_status_after"] = _status(REPO_ROOT)
            result["source_unchanged"] = (result["source_fingerprint_after"] == source_before
                                          and result["source_status_after"] == status_before)
            if result["complete"]:
                result["admitted"], result["reason"], metrics = evaluate(result, policy)
                result["metrics"] = metrics
            else:
                result["reason"] = result.get("error", "incomplete benchmark")
            _atomic_json(output, result)
    result["local_evidence_file"] = str(output)
    if result["admitted"]:
        fields = {**environment, "schema_version": 1, "task_class": task_class,
                  "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "policy_sha256": result["policy_sha256"], "suite_sha256": result["suite_sha256"],
                  "baseline_model": baseline["model"], "baseline_effort": baseline["effort"],
                  "candidate_model": candidate["model"], "candidate_effort": candidate["effort"],
                  "evidence_name": output.name,
                  "evidence_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                  "paired_runs": 8, "quality_checks": [{"id": pair, "passed": True} for pair, _, _ in schedule(cases, seed)],
                  "source_unchanged": True, **result["metrics"]}
        valid, reason = model_promotion.valid_attestation(fields, policy, task_class, environment)
        if valid:
            _atomic_json(model_promotion.attestation_path(home, task_class), fields)
        else:
            result["admitted"] = False
            result["reason"] = f"attestation validation failed: {reason}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run eight per-host paired Codex model checks for one candidate role.")
    parser.add_argument("--task-class", required=True, choices=("mechanical", "exploration", "implementation"))
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    result = run_benchmark(active_codex_home(), args.task_class, seed=args.seed, executable=args.codex)
    print(json.dumps({"admitted": result["admitted"], "reason": result["reason"],
                      "local_evidence_file": result["local_evidence_file"],
                      "metrics": result.get("metrics")}, ensure_ascii=False))
    return 0 if result["admitted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
