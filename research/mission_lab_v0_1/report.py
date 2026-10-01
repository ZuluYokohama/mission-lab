"""Readable evidence and offline regeneration; replay is not re-execution."""
from __future__ import annotations

from pathlib import Path
import json

from . import core


def counts_for(semantic):
    tasks = semantic.get("tasks", [])
    return {status: sum(task.get("status") == status for task in tasks)
            for status in ("succeeded", "failed", "blocked", "timed_out")}


def render_report(semantic, observations):
    counts = counts_for(semantic)
    profile = semantic.get("profile", {})
    resources = profile.get("resources", {})
    lines = ["# Mission execution evidence", "", f"Mission: `{semantic.get('mission_id')}`",
             f"Plan: `{semantic.get('plan_id')}`", "",
             "Result: " + ("accepted within the registered file-audit contract" if semantic.get("accepted") else "not accepted; retained failures require review"),
             "", "Machine verification is not human review or independent scientific reproduction.", "",
             "Requirement prose is retained for alignment; its semantic adequacy remains unreviewed.", "",
             "## Resource and measurement boundary", "",
             f"Worker cap: {resources.get('workers')}. Aggregate read-buffer reservation: {resources.get('read_buffer_bytes')} bytes.",
             f"Cost-estimate origin: `{resources.get('estimate_origin')}`.",
             "Reservations constrain registered worker count and explicit input buffers. Total process memory, physical core use, and energy are unknown.",
             "Predicted schedules use a bounded heuristic. Observed durations include worker startup after launch; costs may differ from predictions.", "",
             "## Outcomes", "", "| Status | Count |", "|---|---:|"]
    for status, count in counts.items():
        lines.append(f"| {status} | {count} |")
    lines += ["", "| Task | Status | SHA-256 / verification | Bytes |", "|---|---|---|---:|"]
    for task in sorted(semantic.get("tasks", []), key=lambda item: item["task_id"]):
        output = task.get("output") or {}
        detail = output.get("sha256", "passed" if output.get("passed") is True else "—")
        lines.append(f"| {task['task_id']} | {task['status']} | {detail} | {output.get('size_bytes', '—')} |")
        if task.get("error"):
            lines += ["", f"Retained error for `{task['task_id']}`: `{str(task['error']).replace('`', '')}`", ""]
    lines += ["", "## Observations", "", "| Task | Estimated duration (ns) | Observed duration (ns) | Reserved buffer bytes |", "|---|---:|---:|---:|"]
    if isinstance(observations, dict):
        observations = observations.get("tasks", [])
    for observation in observations:
        if "task_id" in observation and "duration_ns" in observation:
            predicted = profile.get("bindings", {}).get(observation["task_id"], {}).get("duration_ns", "—")
            lines.append(f"| {observation['task_id']} | {predicted} | {observation['duration_ns']} | {observation.get('reserved_buffer_bytes', '—')} |")
    lines += ["", "## Limits and alternative explanations", "",
              "Different cache state, process startup, storage contention, and estimate error can explain timing differences.",
              "Matching digests supports this file-audit contract. It establishes no numerical-model, energy-saving, or general software-generation claim.",
              "Offline replay regenerates recorded outcomes and tables; it performs no new independent execution.", ""]
    return "\n".join(lines)


def seal_integrity(output):
    output = Path(output)
    files = {path.relative_to(output).as_posix(): core.file_hash(path)
             for path in sorted(output.rglob("*")) if path.is_file() and path != output / "integrity.json"}
    core.write_json(output / "integrity.json", {"schema_version": 1, "files": files})


def verify_integrity(output):
    output = core.confined_path(output, [core.ROOT / "evidence"])
    manifest = core.read_json(output / "integrity.json")
    core.fields(manifest, ("schema_version", "files"), ("semantic_sha256", "hash_note"), label="integrity")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or not isinstance(manifest["files"], dict):
        raise core.LabError("Malformed evidence integrity manifest")
    actual = set()
    for path in output.rglob("*"):
        if core.has_link(path):
            raise core.LabError("Evidence contains a link")
        if path.is_file() and path != output / "integrity.json":
            actual.add(path.relative_to(output).as_posix())
    if actual != set(manifest["files"]):
        raise core.LabError("Evidence file inventory changed")
    for relative, expected in manifest["files"].items():
        path = core.confined_path(output / relative, [output])
        if not isinstance(expected, str) or core.file_hash(path) != expected:
            raise core.LabError("Archived evidence changed: " + relative)
    if "semantic_sha256" in manifest and manifest["semantic_sha256"] != core.file_hash(output / "semantic.json"):
        raise core.LabError("Semantic evidence hash mismatch")
    return output


def replay_run(run):
    output = verify_integrity(run)
    plan = core.load_plan(output / "plan.json")
    semantic = core.read_json(output / "semantic.json")
    observations = core.read_json(output / "observations.json")
    core.fields(semantic, ("schema_version", "plan_id", "mission_id", "accepted", "profile", "tasks"), label="semantic record")
    if type(semantic["schema_version"]) is not int or semantic["schema_version"] != 1:
        raise core.LabError("Unsupported semantic schema")
    if semantic["profile"] != {"estimate_origin": plan["resources"]["estimate_origin"], "resources": plan["resources"],
                              "bindings": plan["bindings"], "schedule_algorithm": plan["schedule_algorithm"]}:
        raise core.LabError("Archived profile contradicts plan")
    if semantic.get("plan_id") != plan["plan_id"] or semantic.get("mission_id") != plan["mission"]["mission_id"]:
        raise core.LabError("Semantic record does not match archived plan")
    tasks = semantic.get("tasks")
    if not isinstance(tasks, list):
        raise core.LabError("Malformed task evidence")
    for task in tasks:
        core.fields(task, ("task_id", "status", "output", "error"), label="archived task")
        core.identifier(task["task_id"])
        if not isinstance(task["status"], str):
            raise core.LabError("Malformed task status")
        if task["status"] == "succeeded" and (not isinstance(task["output"], dict) or task["error"] is not None):
            raise core.LabError("Malformed successful task receipt")
    if {task.get("task_id") for task in tasks} != {node["id"] for node in plan["graph"]} or len(tasks) != len(plan["graph"]):
        raise core.LabError("Missing or duplicate task evidence")
    validation = core.read_json(output / "validation.json")
    tasks_passed = all(task.get("status") == "succeeded" for task in tasks)
    accepted = tasks_passed and validation.get("final_sources_and_inputs_match") is True and validation.get("executor_error") is None
    if validation.get("passed") is not accepted or validation.get("all_registered_tasks_succeeded") is not tasks_passed:
        raise core.LabError("Verification receipt contradicts task evidence")
    if type(semantic.get("accepted")) is not bool or semantic["accepted"] != accepted:
        raise core.LabError("Acceptance inconsistent with task evidence")
    inputs = {item["id"]: item for item in plan["mission"]["inputs"]}
    for relative, expected in plan["sources"].items():
        snapshot = core.confined_path(output / "source_snapshot" / relative, [output / "source_snapshot"])
        if core.file_hash(snapshot) != expected:
            raise core.LabError("Archived source snapshot contradicts the plan")
    receipts = core.read_json(output / "input_receipts.json")
    if not isinstance(receipts.get("inputs"), list) or len(receipts["inputs"]) != len(inputs):
        raise core.LabError("Missing input lineage receipts")
    seen = set()
    for receipt in receipts["inputs"]:
        core.fields(receipt, ("id", "path", "size_bytes", "sha256", "archived", "snapshot"), label="input receipt")
        core.identifier(receipt["id"])
        if type(receipt["archived"]) is not bool or type(receipt["size_bytes"]) is not int:
            raise core.LabError("Malformed input receipt flags or size")
        if receipt.get("id") not in inputs or receipt["id"] in seen:
            raise core.LabError("Invalid input receipt identity")
        seen.add(receipt["id"])
        if any(receipt.get(key) != value for key, value in inputs[receipt["id"]].items()):
            raise core.LabError("Input receipt contradicts plan identity")
        if receipt.get("archived") is True:
            snapshot = core.confined_path(output / receipt["snapshot"], [output / "input_snapshot"])
            if snapshot.stat().st_size != receipt["size_bytes"] or core.file_hash(snapshot) != receipt["sha256"]:
                raise core.LabError("Input snapshot contradicts receipt")
    for task in tasks:
        if task["status"] not in ("succeeded", "failed", "blocked", "timed_out"):
            raise core.LabError("Unknown archived task status")
        node = next(node for node in plan["graph"] if node["id"] == task["task_id"])
        if task["status"] == "succeeded" and node["operation"] in ("sha256", "reference_sha256", "verify_digest"):
            expected = inputs[node["input_id"]]
            result = task.get("output", {})
            if (type(result.get("size_bytes")) is not int or result.get("input_id") != expected["id"] or result.get("sha256") != expected["sha256"] or
                    result.get("size_bytes") != expected["size_bytes"]):
                raise core.LabError("Archived digest result violates the input contract")
            if node["operation"] == "verify_digest" and result.get("passed") is not True:
                raise core.LabError("Missing functional verification receipt")
    manifests = [task for task in tasks if next(node for node in plan["graph"] if node["id"] == task["task_id"])["operation"] == "assemble_manifest"]
    expected_artifacts = sorted([{"input_id": item["id"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
                                 for item in inputs.values()], key=lambda item: item["input_id"])
    if accepted:
        if manifests[0].get("output") != {"artifacts": expected_artifacts} or core.read_json(output / "manifest.json") != manifests[0]["output"]:
            raise core.LabError("Promoted manifest contradicts verified artifacts")
    elif (output / "manifest.json").exists():
        raise core.LabError("Rejected execution cannot promote a manifest")
    validate_observations(plan, semantic, observations, output)
    regenerated = render_report(semantic, observations)
    if (output / "report.md").read_text(encoding="utf-8") != regenerated:
        raise core.LabError("Report does not match deterministic regeneration")
    return {"schema_version": 1, "integrity_verified": True, "independent_execution": False,
            "semantic_sha256": core.digest_object(semantic), "counts": counts_for(semantic),
            "accepted": accepted, "report": regenerated}


def validate_observations(plan, semantic, observations, output):
    if not isinstance(observations, list):
        raise core.LabError("Observations must be a list")
    nodes = {node["id"]: node for node in plan["graph"]}
    tasks = {task["task_id"]: task for task in semantic["tasks"]}
    observed = set()
    for row in observations:
        core.fields(row, ("task_id", "started_ns", "finished_ns", "duration_ns", "pid", "exit_code", "reserved_buffer_bytes", "status", "command",
                          "process_creation_started_ns", "process_creation_ns"), label="observation")
        if not isinstance(row["task_id"], str) or row["task_id"] not in nodes or row["task_id"] in observed:
            raise core.LabError("Invalid observation task identity")
        observed.add(row["task_id"])
        for key in ("started_ns", "finished_ns", "duration_ns", "process_creation_started_ns", "process_creation_ns", "reserved_buffer_bytes"):
            if type(row[key]) is not int or row[key] < 0:
                raise core.LabError("Invalid observed time or reservation")
        if (row["finished_ns"] - row["started_ns"] != row["duration_ns"] or
                row["started_ns"] - row["process_creation_started_ns"] != row["process_creation_ns"] or
                row["reserved_buffer_bytes"] != plan["bindings"][row["task_id"]]["buffer_bytes"] or
                row["status"] != tasks[row["task_id"]]["status"]):
            raise core.LabError("Observation contradicts timing/resource/task receipt")
        if type(row["pid"]) is not int or row["pid"] <= 0 or type(row["exit_code"]) is not int:
            raise core.LabError("Malformed process observation")
        if row["status"] == "succeeded" and row["exit_code"] != 0:
            raise core.LabError("Success contradicted by observed process status")
    if semantic["accepted"] and observed != set(nodes):
        raise core.LabError("Accepted execution lacks task observations")
    try:
        events = [json.loads(line, object_pairs_hook=core._pairs) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError) as exc:
        raise core.LabError("Malformed execution events") from exc
    if not events or events[0].get("kind") != "run_started" or events[-1].get("kind") != "run_completed":
        raise core.LabError("Incomplete event ledger")
    active, done, launched = {}, {}, set()
    last_time = -1
    allowed = {"run_started", "run_completed", "launched", "completed", "blocked", "launch_failed", "executor_failed", "aborted", "final_identity_failed"}
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or type(event.get("sequence")) is not int or event.get("sequence") != index or
                not isinstance(event.get("kind"), str) or event.get("kind") not in allowed):
            raise core.LabError("Invalid event sequence or kind")
        stamp = event.get("monotonic_ns")
        if type(stamp) is not int or stamp < last_time:
            raise core.LabError("Invalid event time ordering")
        last_time = stamp
        kind, task_id = event["kind"], event.get("task_id")
        if kind in {"launched", "completed", "blocked", "launch_failed", "aborted"} and (not isinstance(task_id, str) or task_id not in nodes):
            raise core.LabError("Unknown event task")
        if kind == "launched":
            if task_id in launched or any(done.get(dep) != "succeeded" for dep in nodes[task_id]["depends_on"]):
                raise core.LabError("Illegal launch transition/dependency")
            needed = plan["bindings"][task_id]["buffer_bytes"]
            if event.get("reserved_buffer_bytes") != needed:
                raise core.LabError("Event reservation contradicts binding")
            active[task_id] = needed
            launched.add(task_id)
        elif kind == "completed":
            if task_id not in active or event.get("status") != tasks[task_id]["status"]:
                raise core.LabError("Illegal completion transition")
            active.pop(task_id)
            done[task_id] = event["status"]
        elif kind in {"blocked", "launch_failed", "aborted"}:
            active.pop(task_id, None)
            done[task_id] = tasks[task_id]["status"]
        if kind in {"launched", "completed"}:
            if (type(event.get("workers_active")) is not int or type(event.get("used_buffer_bytes")) is not int or
                    event.get("workers_active") != len(active) or event.get("used_buffer_bytes") != sum(active.values()) or
                    len(active) > plan["resources"]["workers"] or sum(active.values()) > plan["resources"]["read_buffer_bytes"]):
                raise core.LabError("Execution event violates resource accounting")
    if active or events[-1].get("accepted") is not semantic["accepted"]:
        raise core.LabError("Terminal event contradicts execution status")
    if semantic["accepted"] and set(done) != set(nodes):
        raise core.LabError("Accepted execution lacks completion events")
