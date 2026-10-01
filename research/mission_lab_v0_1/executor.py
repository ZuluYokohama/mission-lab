"""Bounded event-driven executor for a closed registered-operator graph."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import datetime as dt
import platform

from .core import (ARCHIVE, LabError, REVISIONS, ROOT, WORKSPACE, canonical, confined_path, digest_object, file_hash,
                   load_plan, new_output, read_json, verify_sources, write_json)
from .operators import required_buffer


def _failure(task_id, error, status="failed"):
    return {"task_id": task_id, "status": status, "output": None,
            "error": {"type": type(error).__name__, "message": str(error)}}


def _inputs(plan):
    return {value["id"]: value for value in plan["mission"]["inputs"]}


def _check_inputs(plan):
    verify_sources(plan["sources"])
    for artifact in plan["mission"]["inputs"]:
        path = confined_path(artifact["path"], [WORKSPACE, ARCHIVE])
        if not path.is_file() or path.stat().st_size != artifact["size_bytes"] or file_hash(path) != artifact["sha256"]:
            raise LabError("Input changed since planning: " + artifact["id"])


def _snapshot_sources(plan, directory):
    root = directory / "source_snapshot"
    root.mkdir()
    for relative, expected in sorted(plan["sources"].items()):
        source = ROOT.parent / "__init__.py" if relative == "@research/__init__.py" else ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=8192)
        if file_hash(target) != expected:
            raise LabError("Source changed while archiving: " + relative)


def _snapshot_inputs(plan, directory):
    receipts = []
    fixture_root = (ROOT / "fixtures").resolve()
    for index, artifact in enumerate(plan["mission"]["inputs"]):
        source = Path(artifact["path"])
        receipt = {**artifact, "archived": False, "snapshot": None}
        if source.resolve().is_relative_to(fixture_root):
            target = directory / "input_snapshot" / (str(index) + ".bin")
            target.parent.mkdir(exist_ok=True)
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=8192)
            if file_hash(target) != artifact["sha256"]:
                raise LabError("Input changed while archiving: " + artifact["id"])
            receipt.update(archived=True, snapshot=target.relative_to(directory).as_posix())
        receipts.append(receipt)
    write_json(directory / "input_receipts.json", {"archived_inputs": all(row["archived"] for row in receipts), "inputs": receipts})


def _check_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise LabError("Unsupported execution plan schema")
    if plan.get("plan_id") != digest_object({key: value for key, value in plan.items() if key != "plan_id"}):
        raise LabError("Plan identity mismatch")
    from .planner import validate_plan_record
    validate_plan_record(plan)
    inputs = _inputs(plan)
    nodes = {node["id"]: node for node in plan["graph"]}
    if len(nodes) != len(plan["graph"]) or set(nodes) != set(plan["bindings"]):
        raise LabError("Execution node/binding inventory mismatch")
    for node in nodes.values():
        if not isinstance(node["depends_on"], list) or len(set(node["depends_on"])) != len(node["depends_on"]) or not set(node["depends_on"]) <= set(nodes):
            raise LabError("Invalid execution dependencies")
        if node.get("input_id") is not None and node["input_id"] not in inputs:
            raise LabError("Unknown execution input")
        binding = plan["bindings"][node["id"]]
        if binding.get("revision") != REVISIONS.get(binding.get("candidate")):
            raise LabError("Unknown registered implementation revision")
        needed = required_buffer(node["operation"], binding["candidate"], inputs.get(node.get("input_id")))
        if binding.get("buffer_bytes") != needed or needed > plan["resources"]["read_buffer_bytes"]:
            raise LabError("Invalid or infeasible read-buffer reservation")
    return nodes


def _validate_result(value, job, exit_code):
    task_id = job["node"]["id"]
    if not isinstance(value, dict) or set(value) != {"task_id", "status", "output", "error"}:
        raise LabError("Malformed worker result fields")
    if value["task_id"] != task_id or value["status"] not in {"succeeded", "failed"}:
        raise LabError("Worker result identity/status mismatch")
    if value["status"] == "failed":
        if exit_code == 0 or value["output"] is not None or not isinstance(value["error"], dict) or set(value["error"]) != {"type", "message"} or not all(isinstance(v, str) for v in value["error"].values()):
            raise LabError("Malformed failed worker result")
        return value
    if exit_code != 0 or value["error"] is not None or not isinstance(value["output"], dict):
        raise LabError("Worker success contradicted by exit/result")
    operation = job["node"]["operation"]
    output = value["output"]
    if operation in {"sha256", "reference_sha256", "verify_digest"}:
        fields = {"input_id", "sha256", "size_bytes"}
        if operation == "verify_digest":
            fields.add("passed")
        if set(output) != fields or output.get("input_id") != job["input"]["id"] or type(output.get("size_bytes")) is not int or output["size_bytes"] < 0 or not isinstance(output.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", output["sha256"]) is None:
            raise LabError("Malformed digest worker output")
        expected_digest = {"input_id": job["input"]["id"], "sha256": job["input"]["sha256"], "size_bytes": job["input"]["size_bytes"]}
        if {key: output[key] for key in expected_digest} != expected_digest:
            raise LabError("Worker digest does not match the recorded input")
        if operation == "verify_digest":
            expected = {"input_id": job["input"]["id"], "sha256": job["input"]["sha256"], "size_bytes": job["input"]["size_bytes"], "passed": True}
            if output != expected or type(output["passed"]) is not bool:
                raise LabError("Worker verification does not match recorded input")
            for dependency in job["dependencies"].values():
                if dependency.get("output") != {key: expected[key] for key in ("input_id", "sha256", "size_bytes")}:
                    raise LabError("Worker verification contradicted by dependency results")
    elif operation == "assemble_manifest":
        expected = [{key: result["output"][key] for key in ("input_id", "sha256", "size_bytes")} for result in job["dependencies"].values()]
        expected.sort(key=lambda row: row["input_id"])
        if output != {"artifacts": expected}:
            raise LabError("Worker manifest does not match verified dependencies")
    else:
        raise LabError("Unknown worker result operation")
    return value


def run_plan(plan_path, out):
    return run_plan_data(load_plan(plan_path), out)


def run_plan_data(plan, out):
    nodes = _check_plan(plan)
    _check_inputs(plan)
    directory = new_output(out)
    write_json(directory / "environment.json", {"schema_version": 1, "python_version": sys.version,
               "executable": sys.executable, "platform": platform.platform(),
               "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "resource_contract": "registered worker slots and explicit input buffers; controller integrity checks, process overhead, RSS, cores and energy are outside this reservation"})
    private = directory / "private"
    private.mkdir()
    staged = directory / "staged"
    staged.mkdir()
    for name, value in (("plan", plan), ("mission", plan["mission"]), ("graph", plan["graph"]),
                        ("profiles", {"resources": plan["resources"], "bindings": plan["bindings"]}),
                        ("sources", plan["sources"]), ("schedule", plan["schedule"]),
                        ("obligations", plan["obligations"]), ("operator_contracts", plan["operator_contracts"]),
                        ("inputs", plan["mission"]["inputs"])):
        write_json(directory / (name + ".json"), value)
    _snapshot_sources(plan, directory)
    _snapshot_inputs(plan, directory)
    from .planner import architecture
    (directory / "architecture.mmd").write_text(architecture(plan["graph"]), encoding="utf-8", newline="\n")
    pending = set(nodes)
    results = {}
    running = {}
    observations = []
    events = []
    used_buffer = 0
    completions = queue.Queue()
    queued = []
    executor_error = None
    inputs = _inputs(plan)
    order = {row["task_id"]: i for i, row in enumerate(sorted(plan["schedule"], key=lambda row: (row["start_ns"], row["task_id"])))}
    from .planner import critical_path_lengths
    tails = critical_path_lengths(plan["graph"], plan["bindings"])
    timeout = float(plan["resources"]["timeout_seconds"])

    def event(kind, task_id=None, **fields):
        events.append({"sequence": len(events), "kind": kind, "task_id": task_id,
                       "monotonic_ns": time.perf_counter_ns(), **fields})
        # Preserve scheduler progress even if a later task fails.
        with (directory / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(events[-1], sort_keys=True, allow_nan=False) + "\n")

    def wait_for_worker(task_id, child, started):
        try:
            remaining = max(0.0, timeout - (time.perf_counter_ns() - started) / 1e9)
            code, error = child.wait(timeout=remaining), None
        except subprocess.TimeoutExpired:
            child.kill()
            code, error = child.wait(), TimeoutError("Registered worker exceeded its task timeout")
        except Exception as failure:
            code, error = None, failure
        completions.put((task_id, code, time.perf_counter_ns(), error))

    event("run_started", workers_limit=plan["resources"]["workers"], buffer_limit=plan["resources"]["read_buffer_bytes"], enforced_task_timeout_seconds=timeout)
    try:
        while pending or running:
            progress = False
            for task_id in sorted(pending, key=lambda name: (-tails[name], name)):
                node = nodes[task_id]
                if any(dep in results and results[dep]["status"] != "succeeded" for dep in node["depends_on"]):
                    results[task_id] = _failure(task_id, LabError("A dependency did not succeed"), "blocked")
                    write_json(directory / ("result_" + str(order.get(task_id, len(order))) + ".json"), results[task_id])
                    pending.remove(task_id)
                    event("blocked", task_id)
                    progress = True
                    continue
                if not all(dep in results for dep in node["depends_on"]):
                    continue
                binding = plan["bindings"][task_id]
                if len(running) >= plan["resources"]["workers"] or used_buffer + binding["buffer_bytes"] > plan["resources"]["read_buffer_bytes"]:
                    continue
                index = order.get(task_id, len(order))
                job = {"schema_version": 1, "node": node, "binding": binding,
                       "input": inputs.get(node.get("input_id")),
                       "dependencies": {dep: results[dep] for dep in node["depends_on"]}}
                job_path = private / (str(index) + "_job.json")
                result_path = private / (str(index) + "_worker_result.json")
                stdout_path = private / (str(index) + "_stdout.txt")
                stderr_path = private / (str(index) + "_stderr.txt")
                write_json(job_path, job)
                pending.remove(task_id)
                child = None
                try:
                    _check_inputs(plan)
                    command = [sys.executable, "-B", "-m", "research.mission_lab_v0_1.worker", "--job", str(job_path), "--result", str(result_path)]
                    env = os.environ.copy()
                    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
                    creation_started = time.perf_counter_ns()
                    with stdout_path.open("x", encoding="utf-8") as stdout, stderr_path.open("x", encoding="utf-8") as stderr:
                        child = subprocess.Popen(command, cwd=WORKSPACE, env=env, stdout=stdout, stderr=stderr, shell=False)
                    started = time.perf_counter_ns()
                    running[task_id] = {"child": child, "started": started, "job": job,
                                        "result_path": result_path, "index": index, "command": command,
                                        "creation_started": creation_started}
                    used_buffer += binding["buffer_bytes"]
                    event("launched", task_id, pid=child.pid, reserved_buffer_bytes=binding["buffer_bytes"], used_buffer_bytes=used_buffer, workers_active=len(running))
                    threading.Thread(target=wait_for_worker, args=(task_id, child, started), daemon=True).start()
                except Exception as error:
                    if task_id in running:
                        del running[task_id]
                        used_buffer -= binding["buffer_bytes"]
                    if child is not None:
                        if child.poll() is None:
                            child.kill()
                        child.wait()
                    results[task_id] = _failure(task_id, error)
                    write_json(directory / ("result_" + str(index) + ".json"), results[task_id])
                    event("launch_failed", task_id, error=results[task_id]["error"])
                progress = True
            while True:
                try:
                    queued.append(completions.get_nowait())
                except queue.Empty:
                    break
            completed_ids = {item[0] for item in queued}
            now = time.perf_counter_ns()
            expired = [(task_id, active) for task_id, active in running.items()
                       if task_id not in completed_ids and now - active["started"] >= timeout * 1e9]
            for task_id, active in expired:
                active["child"].kill()
                queued.append((task_id, active["child"].wait(), time.perf_counter_ns(), TimeoutError("Registered worker exceeded its task timeout")))
            while queued:
                task_id, code, finished, completion_error = queued.pop(0)
                if task_id not in running:
                    continue  # A watchdog already retained this worker's timeout.
                active = running[task_id]
                child = active["child"]
                if completion_error is None and finished - active["started"] >= timeout * 1e9:
                    completion_error = TimeoutError("Registered worker completed after its task deadline")
                timed_out = isinstance(completion_error, TimeoutError)
                if timed_out:
                    result = _failure(task_id, completion_error, "timed_out")
                elif completion_error is not None:
                    result = _failure(task_id, completion_error)
                else:
                    try:
                        result = _validate_result(read_json(active["result_path"]), active["job"], code)
                    except Exception as error:
                        result = _failure(task_id, error)
                results[task_id] = result
                if result["status"] == "succeeded":
                    write_json(staged / (str(active["index"]) + ".json"), result["output"])
                write_json(directory / ("result_" + str(active["index"]) + ".json"), result)
                observation = {"task_id": task_id, "started_ns": active["started"], "finished_ns": finished,
                               "duration_ns": finished - active["started"], "pid": child.pid,
                               "process_creation_started_ns": active["creation_started"], "process_creation_ns": active["started"] - active["creation_started"],
                               "exit_code": code, "reserved_buffer_bytes": plan["bindings"][task_id]["buffer_bytes"],
                               "status": result["status"], "command": active["command"]}
                observations.append(observation)
                used_buffer -= plan["bindings"][task_id]["buffer_bytes"]
                del running[task_id]
                event("completed", task_id, status=result["status"], used_buffer_bytes=used_buffer, workers_active=len(running))
                progress = True
            if pending and not running and not progress:
                for task_id in sorted(pending):
                    results[task_id] = _failure(task_id, LabError("Dependency deadlock or infeasible ready node"), "blocked")
                    event("blocked", task_id, reason="deadlock_or_infeasible")
                pending.clear()
            if running and not progress:
                nearest = min(active["started"] + timeout * 1e9 for active in running.values())
                remaining = max(0.0, (nearest - time.perf_counter_ns()) / 1e9)
                try:
                    queued.append(completions.get(timeout=remaining))
                except queue.Empty:
                    pass  # The next iteration handles the expired deadline.
    except Exception as error:
        executor_error = {"type": type(error).__name__, "message": str(error)}
        event("executor_failed", error=executor_error)
        for task_id in sorted(pending | set(running)):
            status = "failed" if task_id in running else "blocked"
            results[task_id] = _failure(task_id, error, status)
            target = directory / ("result_" + str(order[task_id]) + ".json")
            if not target.exists():
                write_json(target, results[task_id])
            elif read_json(target) != results[task_id]:
                write_json(private / (str(order[task_id]) + "_pre_abort_result.json"), read_json(target))
                target.write_text(canonical(results[task_id]), encoding="utf-8", newline="\n")
            event("aborted", task_id, status=status)
    finally:
        for active in running.values():
            if active["child"].poll() is None:
                active["child"].kill()
            active["child"].wait()
    tasks_passed = len(results) == len(nodes) and all(value["status"] == "succeeded" for value in results.values())
    identity_error = None
    try:
        _check_inputs(plan)
    except Exception as error:
        identity_error = {"type": type(error).__name__, "message": str(error)}
        event("final_identity_failed", error=identity_error)
    accepted = tasks_passed and identity_error is None and executor_error is None
    validation = {"passed": accepted, "obligations": plan["obligations"],
                  "all_registered_tasks_succeeded": tasks_passed, "sources_and_inputs_rechecked_before_launch": True,
                  "final_sources_and_inputs_match": identity_error is None, "identity_error": identity_error,
                  "executor_error": executor_error,
                  "resource_telemetry": {"peak_workers_observed": max((row.get("workers_active", 0) for row in events), default=0),
                    "peak_reserved_buffer_bytes": max((row.get("used_buffer_bytes", 0) for row in events), default=0),
                    "enforced_timeout_seconds": timeout, "total_process_memory_bytes": None, "physical_cpu_cores_used": None},
                  "resource_scope": "worker count and declared read buffers; not total process memory or CPU cores"}
    semantic = {"schema_version": 1, "plan_id": plan["plan_id"], "mission_id": plan["mission"].get("mission_id"),
                "accepted": accepted, "profile": {"estimate_origin": plan["resources"]["estimate_origin"],
                  "resources": plan["resources"], "bindings": plan["bindings"], "schedule_algorithm": plan["schedule_algorithm"]},
                "tasks": [results[key] for key in sorted(results)]}
    write_json(directory / "semantic.json", semantic)
    write_json(directory / "observations.json", observations)
    write_json(directory / "validation.json", validation)
    event("run_completed", accepted=accepted)
    if accepted:
        final_nodes = [node for node in nodes.values() if node["operation"] == "assemble_manifest"]
        if len(final_nodes) != 1:
            raise LabError("Execution needs exactly one terminal manifest")
        write_json(directory / "manifest.json", results[final_nodes[0]["id"]]["output"])
    from .report import render_report
    (directory / "report.md").write_text(render_report(semantic, observations), encoding="utf-8", newline="\n")
    files = {path.relative_to(directory).as_posix(): file_hash(path) for path in sorted(directory.rglob("*")) if path.is_file()}
    write_json(directory / "integrity.json", {"schema_version": 1, "files": files, "semantic_sha256": file_hash(directory / "semantic.json"),
                                             "hash_note": "Integrity only; not source authenticity or proof of semantic truth"})
    return directory
