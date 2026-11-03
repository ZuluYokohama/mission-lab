"""Readable evidence and offline regeneration; replay is not re-execution."""
from __future__ import annotations

from pathlib import Path
import json

from . import core, grok, seal
from .budget import verify_ledger, verify_telemetry


def counts_for(semantic):
    tasks = semantic.get("tasks", [])
    return {status: sum(task.get("status") == status for task in tasks)
            for status in ("succeeded", "failed", "blocked", "timed_out")}


def render_report(semantic, observations):
    counts = counts_for(semantic)
    profile = semantic.get("profile", {})
    resources = profile.get("resources", {})
    # A pre-change run has no seal claim. Its report bytes omit the controller-key
    # sentence and keep the earlier gate and resume wording. New runs always store a claim.
    seal_claim = semantic.get("seal") if isinstance(semantic, dict) else None
    sealed = isinstance(seal_claim, dict)
    lines = ["# Mission execution evidence", "", f"Mission: `{semantic.get('mission_id')}`",
             f"Plan: `{semantic.get('plan_id')}`", "",
             "Result: " + ("accepted within the registered operator contract" if semantic.get("accepted") else "not accepted; retained failures require review"),
             "", "Machine verification is not human review or independent scientific reproduction.", "",
             "Requirement prose is retained for alignment; its semantic adequacy remains unreviewed.", "",
             "## Resource and measurement boundary", "",
             f"Worker cap: {resources.get('workers')}. Aggregate read-buffer reservation: {resources.get('read_buffer_bytes')} bytes.",
             f"Cost-estimate origin: `{resources.get('estimate_origin')}`.",
             "Reservations constrain registered worker count and explicit input buffers. Total process memory, physical core use, and energy are unknown.",
             "Worker processes receive a deny-by-default environment: platform start names when set, two controller-forced interpreter flags, and per-task resource grants. Observations list those names and do not retain values.",
             "Predicted schedules use a bounded heuristic. Observed durations include worker startup after launch; costs may differ from predictions.", ""]
    if sealed:
        lines += ["", "## Seal", "",
                  "Controller key variables cannot be granted and are not copied: " + ", ".join(f"`{name}`" for name in sorted(core.CONTROLLER_KEY_ENV)) + ".",
                  f"Signature status: `signed`. Algorithm: `{seal_claim.get('algorithm')}`. Key id: `{seal_claim.get('key_id')}`.",
                  "The signature is HMAC-SHA256 over the canonical integrity manifest. That manifest hashes the plan, events, receipts, gate outcomes, budget ledger, and the other files in the run. A verifier needs the same key. A match shows those bytes are unchanged to holders of the key. It is not a public signature, and it does not by itself prove who produced the run to someone who lacks the key.",
                  "Replay reports `verified` only after that HMAC matches. This line says `signed` because it was written when the seal was produced.",
                  ""]
    lines += ["## Outcomes", "", "| Status | Count |", "|---|---:|"]
    for status, count in counts.items():
        lines.append(f"| {status} | {count} |")
    lines += ["", "| Task | Status | SHA-256 / verification | Bytes |", "|---|---|---|---:|"]
    for task in sorted(semantic.get("tasks", []), key=lambda item: item["task_id"]):
        output = task.get("output") or {}
        if output.get("sha256"):
            detail = output["sha256"]
        elif output.get("passed") is True:
            detail = "passed"
        elif output.get("response_sha256"):
            detail = output["response_sha256"]
        elif output.get("outcome") in ("pass", "fail", "indeterminate", "pending"):
            detail = output["outcome"]
        else:
            detail = "—"
        if "size_bytes" in output:
            shown_bytes = output["size_bytes"]
        elif "response_bytes" in output:
            shown_bytes = output["response_bytes"]
        else:
            shown_bytes = "—"
        lines.append(f"| {task['task_id']} | {task['status']} | {detail} | {shown_bytes} |")
        if task.get("error"):
            error = task["error"]
            if isinstance(error, dict):
                rendered_error = str({key: error[key] for key in sorted(error)})
            else:
                rendered_error = str(error)
            lines += ["", f"Retained error for `{task['task_id']}`: `{rendered_error.replace('`', '')}`", ""]
    gate_rows = [task for task in semantic.get("tasks", [])
                 if isinstance(task.get("output"), dict) and task["output"].get("outcome") in ("pass", "fail", "indeterminate", "pending")]
    if gate_rows:
        if sealed:
            pending_text = "Pending means the predicate is not yet a pass. An approval record names an approver id and a UTC timestamp. Without declared approver key ids, that record is not signed and not authenticated. When the predicate lists approver key ids, a missing or invalid HMAC stays pending and does not pass."
        else:
            pending_text = "Pending means `approvals.json` has no matching record. That file records an approver id and a timestamp. It is not signed and not authenticated."
        lines += ["", "## Merit gates", "",
                  "A gate receipt is pass, fail, indeterminate, or pending. The task status stays succeeded when the controller finishes the evaluation. Fail, indeterminate, and pending do not launch downstream tasks and do not charge their model reservations.",
                  pending_text,
                  "", "| Gate | Outcome | Reason |", "|---|---|---|"]
        for task in sorted(gate_rows, key=lambda item: item["task_id"]):
            reason = str(task["output"].get("reason", "")).replace("|", "/").replace("`", "")
            lines.append(f"| {task['task_id']} | {task['output']['outcome']} | {reason} |")
    revision = semantic.get("revision") if isinstance(semantic, dict) else None
    if isinstance(revision, dict):
        trigger = revision.get("trigger") if isinstance(revision.get("trigger"), dict) else {}
        refs = trigger.get("evidence_refs") if isinstance(trigger.get("evidence_refs"), list) else []
        changed = revision.get("changed_tasks") if isinstance(revision.get("changed_tasks"), list) else []
        affected = revision.get("affected_tasks") if isinstance(revision.get("affected_tasks"), list) else []
        text = trigger.get("text") if isinstance(trigger.get("text"), str) else ""
        lines += ["", "## Revision", "",
                  f"Trigger: `{trigger.get('kind')}`. Origin: `{trigger.get('evidence_origin')}`.",
                  text.replace("`", ""),
                  "Evidence refs: " + (", ".join(str(item) for item in refs) if refs else "none") + ".",
                  "Changed tasks: " + (", ".join(str(item) for item in changed) if changed else "none") + ".",
                  "Affected tasks: " + (", ".join(str(item) for item in affected) if affected else "none") + "."]
        prior_seal = revision.get("prior_seal") if isinstance(revision.get("prior_seal"), dict) else None
        if prior_seal:
            lines.append(
                f"Prior seal sha256: `{prior_seal.get('seal_sha256')}`. Algorithm: `{prior_seal.get('algorithm')}`. Key id: `{prior_seal.get('key_id', 'none')}`.")
        lines.append("")
    resume = semantic.get("resume_of") if isinstance(semantic, dict) else None
    if isinstance(resume, dict):
        if sealed:
            lines += ["", "## Resume", "",
                      f"This run started again from plan `{resume.get('plan_id')}` after an approval record was copied into the run directory.",
                      f"Prior semantic sha256: `{resume.get('prior_semantic_sha256')}`.",
                      f"Prior seal sha256: `{resume.get('prior_seal_sha256', 'none')}`. Prior seal key id: `{resume.get('prior_seal_key_id', 'none')}`.",
                      "This line does not reopen the prior run.",
                      ""]
        else:
            lines += ["", "## Resume", "",
                      f"This run started again from plan `{resume.get('plan_id')}` after an approval record was copied into the run directory.",
                      f"Prior semantic sha256: `{resume.get('prior_semantic_sha256')}`. The approval record is not authenticated, and this line does not reopen the prior run.",
                      ""]
    lines += ["", "## Observations", "",
              "| Task | Estimated duration (ns) | Observed duration (ns) | Reserved buffer bytes | Environment names |",
              "|---|---:|---:|---:|---|"]
    if isinstance(observations, dict):
        observations = observations.get("tasks", [])
    for observation in observations:
        if "task_id" in observation and "duration_ns" in observation:
            predicted = profile.get("bindings", {}).get(observation["task_id"], {}).get("duration_ns", "—")
            names = observation.get("environment_names")
            rendered = ", ".join(names) if isinstance(names, list) else "—"
            lines.append(f"| {observation['task_id']} | {predicted} | {observation['duration_ns']} | {observation.get('reserved_buffer_bytes', '—')} | {rendered} |")
    ledger = semantic.get("model_budget") if isinstance(semantic, dict) else None
    if isinstance(ledger, dict):
        lines += ["", "## Model budget", "",
                  f"Fixed-point unit: `{ledger.get('unit')}`. The unit is a profile label. It is not a market rate, an invoice, or an energy measurement.",
                  "Prices are the integers supplied in the resource profile. Per-million prices use ceiling division by 1,000,000 on each of input and output.",
                  "A reservation is the worst case from the input-token bound and max_output_tokens. Settlement replaces that hold with provider-reported usage when the usage fits. A launched task that does not settle keeps the hold.",
                  "",
                  "| Task | State | Requests reserved | Requests actual | Input reserved | Input actual | Output reserved | Output actual | Total reserved | Total actual | Spend reserved | Spend actual |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for row in ledger.get("tasks", []):
            reserved = row.get("reserved") if isinstance(row.get("reserved"), dict) else {}
            actual = row.get("actual") if isinstance(row.get("actual"), dict) else {}
            def shown(source, key):
                return source[key] if key in source else "—"
            lines.append(
                f"| {row.get('task_id')} | {row.get('state')} | {shown(reserved, 'requests')} | {shown(actual, 'requests')} | "
                f"{shown(reserved, 'input_tokens')} | {shown(actual, 'input_tokens')} | {shown(reserved, 'output_tokens')} | {shown(actual, 'output_tokens')} | "
                f"{shown(reserved, 'total_tokens')} | {shown(actual, 'total_tokens')} | {shown(reserved, 'spend')} | {shown(actual, 'spend')} |")
        totals = ledger.get("totals") or {}
        accounted = totals.get("accounted") or {}
        remaining = totals.get("remaining") or {}
        limits = ledger.get("limits") or {}
        lines += ["",
                  f"Accounted requests: {accounted.get('requests')} of {limits.get('max_requests')}. Remaining requests: {remaining.get('requests')}.",
                  f"Accounted spend: {accounted.get('spend')} of {limits.get('max_spend')} `{ledger.get('unit')}`. Remaining spend: {remaining.get('spend')}.",
                  "Accounted output tokens: " + (str(accounted.get("output_tokens")) + " of " + str(limits.get("max_output_tokens")) + "."
                                                 if "max_output_tokens" in limits else "not capped."),
                  "Accounted total tokens: " + (str(accounted.get("total_tokens")) + " of " + str(limits.get("max_total_tokens")) + "."
                                                if "max_total_tokens" in limits else "not capped."),
                  "Reserved totals count launched holds before settlement. Actual totals count settled receipts only. Accounted spend is settled usage plus unsettled holds.",
                  ""]
    lines += ["", "## Limits and alternative explanations", "",
              "Different cache state, process startup, storage contention, and estimate error can explain timing differences.",
              "Matching digests supports the file-audit contract. It establishes no numerical-model, energy-saving, or general software-generation claim.",
              "An llm:grok receipt records the request, response hashes, provider-reported token counts, latency, and HTTP status. It does not score model quality or establish that the response is true.",
              ("A gate predicate compares declared receipt fields. Passing a gate is not a quality score or a human review of the mission. An approval HMAC, when required, is not that review."
               if sealed else
               "A gate predicate compares declared receipt fields. Passing a gate is not a quality score, a signature, or a human review of the mission."),
              "Offline replay regenerates recorded outcomes and re-evaluates gate predicates from archived receipts. It performs no new independent execution and does not call a model.", ""]
    return "\n".join(lines)


def seal_integrity(output):
    seal.write_integrity(Path(output), seal.require_seal_key())


def verify_integrity(output, *, allow_legacy_unsigned=False):
    return seal.verify_directory(output, allow_legacy_unsigned=allow_legacy_unsigned)


def replay_run(run, *, allow_legacy_unsigned=False):
    output, seal_status = verify_integrity(run, allow_legacy_unsigned=allow_legacy_unsigned)
    plan = core.load_plan(output / "plan.json")
    semantic = core.read_json(output / "semantic.json")
    observations = core.read_json(output / "observations.json")
    core.fields(semantic, ("schema_version", "plan_id", "mission_id", "accepted", "profile", "tasks"),
                ("model_budget", "revision", "resume_of", "seal"), label="semantic record")
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
        core.fields(task, ("task_id", "status", "output", "error"), ("model_account", "blocked_by"), label="archived task")
        core.identifier(task["task_id"])
        if not isinstance(task["status"], str):
            raise core.LabError("Malformed task status")
        if task["status"] == "succeeded" and (not isinstance(task["output"], dict) or task["error"] is not None):
            raise core.LabError("Malformed successful task receipt")
    if {task.get("task_id") for task in tasks} != {node["id"] for node in plan["graph"]} or len(tasks) != len(plan["graph"]):
        raise core.LabError("Missing or duplicate task evidence")
    validation = core.read_json(output / "validation.json")
    tasks_passed = all(task.get("status") == "succeeded" for task in tasks)
    gate_nodes = {node["id"]: node for node in plan["graph"] if node.get("operation") == "gate"}
    gates_passed = True
    for task in tasks:
        if task["task_id"] not in gate_nodes:
            continue
        receipt = task.get("output")
        if task.get("status") != "succeeded" or not isinstance(receipt, dict) or receipt.get("outcome") != "pass":
            gates_passed = False
    accepted = tasks_passed and gates_passed and validation.get("final_sources_and_inputs_match") is True and validation.get("executor_error") is None
    if ("revision" in semantic) != ("revision" in plan) or semantic.get("revision") != plan.get("revision"):
        raise core.LabError("Revision record contradicts the plan")
    if "resume_of" in semantic:
        core.fields(semantic["resume_of"], ("plan_id", "prior_semantic_sha256"),
                    ("prior_seal_sha256", "prior_seal_key_id"), label="resume record")
        prior_hash = semantic["resume_of"]["prior_semantic_sha256"]
        if semantic["resume_of"]["plan_id"] != plan["plan_id"] or not isinstance(prior_hash, str) or core.SHA256.fullmatch(prior_hash) is None:
            raise core.LabError("Resume record does not match the plan")
        if "prior_seal_sha256" in semantic["resume_of"] and core.SHA256.fullmatch(semantic["resume_of"]["prior_seal_sha256"]) is None:
            raise core.LabError("Resume record has a malformed prior seal hash")
        if "prior_seal_key_id" in semantic["resume_of"] and (
                "prior_seal_sha256" not in semantic["resume_of"] or core.SHA256.fullmatch(semantic["resume_of"]["prior_seal_key_id"]) is None):
            raise core.LabError("Resume record has a malformed prior seal key id")
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
    archived_inputs = {receipt["id"]: receipt for receipt in receipts["inputs"]}
    for task in tasks:
        if task["status"] not in ("succeeded", "failed", "blocked", "timed_out"):
            raise core.LabError("Unknown archived task status")
        node = next(node for node in plan["graph"] if node["id"] == task["task_id"])
        if task["status"] == "succeeded" and node["operation"] == "llm:grok":
            grok.verify_archived(output, node, task["output"], inputs, archived_inputs)
        if task["status"] == "succeeded" and node["operation"] in ("sha256", "reference_sha256", "verify_digest"):
            expected = inputs[node["input_id"]]
            result = task.get("output", {})
            if (type(result.get("size_bytes")) is not int or result.get("input_id") != expected["id"] or result.get("sha256") != expected["sha256"] or
                    result.get("size_bytes") != expected["size_bytes"]):
                raise core.LabError("Archived digest result violates the input contract")
            if node["operation"] == "verify_digest" and result.get("passed") is not True:
                raise core.LabError("Missing functional verification receipt")
    _check_blocked_by(plan, tasks)
    from .gates import reevaluate_archived
    reevaluate_archived(plan, tasks, observations, output)
    if validation.get("passed") is not accepted or validation.get("all_registered_tasks_succeeded") is not tasks_passed:
        raise core.LabError("Verification receipt contradicts task evidence")
    if type(semantic.get("accepted")) is not bool or semantic["accepted"] != accepted:
        raise core.LabError("Acceptance inconsistent with task evidence")
    manifests = [task for task in tasks if next(node for node in plan["graph"] if node["id"] == task["task_id"])["operation"] == "assemble_manifest"]
    expected_artifacts = sorted([{"input_id": item["id"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
                                 for item in inputs.values()], key=lambda item: item["input_id"])
    if accepted:
        if manifests[0].get("output") != {"artifacts": expected_artifacts} or core.read_json(output / "manifest.json") != manifests[0]["output"]:
            raise core.LabError("Promoted manifest contradicts verified artifacts")
    elif (output / "manifest.json").exists():
        raise core.LabError("Rejected execution cannot promote a manifest")
    validate_observations(plan, semantic, observations, output)
    verify_telemetry(plan, semantic, validation)
    regenerated = render_report(semantic, observations)
    if (output / "report.md").read_text(encoding="utf-8") != regenerated:
        raise core.LabError("Report does not match deterministic regeneration")
    verified = seal_status["status"] == "verified"
    return {"schema_version": 1, "integrity_verified": verified, "signature_status": seal_status["status"],
            "seal_key_id": seal_status.get("key_id"), "independent_execution": False,
            "semantic_sha256": core.digest_object(semantic), "counts": counts_for(semantic),
            "accepted": accepted, "report": regenerated}


def _check_blocked_by(plan, tasks):
    nodes = {node["id"]: node for node in plan["graph"]}
    by_task = {task["task_id"]: task for task in tasks}
    for task in tasks:
        if "blocked_by" not in task:
            continue
        node = nodes[task["task_id"]]
        refs = task["blocked_by"]
        if task["status"] != "blocked" or not isinstance(refs, list) or not refs or refs != sorted(refs) or len(set(refs)) != len(refs):
            raise core.LabError("blocked_by does not name the blocking gates")
        for ref in refs:
            core.identifier(ref, "blocking gate")
            if ref not in node["depends_on"] or nodes[ref]["operation"] != "gate":
                raise core.LabError("blocked_by names a node that is not a gate dependency")
            upstream = by_task[ref]
            output = upstream.get("output") if upstream.get("status") == "succeeded" else None
            if isinstance(output, dict) and output.get("outcome") == "pass":
                raise core.LabError("Task blocked by a gate that passed")


def _upstream_done(nodes, tasks, done, dep):
    if done.get(dep) != "succeeded":
        return False
    if nodes[dep]["operation"] == "gate":
        output = tasks[dep].get("output")
        return isinstance(output, dict) and output.get("outcome") == "pass"
    return True


def validate_observations(plan, semantic, observations, output):
    if not isinstance(observations, list):
        raise core.LabError("Observations must be a list")
    nodes = {node["id"]: node for node in plan["graph"]}
    tasks = {task["task_id"]: task for task in semantic["tasks"]}
    observed = set()
    for row in observations:
        core.fields(row, ("task_id", "started_ns", "finished_ns", "duration_ns", "pid", "exit_code", "reserved_buffer_bytes", "status", "command",
                          "process_creation_started_ns", "process_creation_ns", "environment_names"), ("model_account",), label="observation")
        if not isinstance(row["task_id"], str) or row["task_id"] not in nodes or row["task_id"] in observed:
            raise core.LabError("Invalid observation task identity")
        if nodes[row["task_id"]]["operation"] == "gate":
            raise core.LabError("Gate observation is not a worker process")
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
        core.validate_environment_receipt(row["environment_names"], plan["resources"], row["task_id"])
    worker_nodes = {nid for nid, node in nodes.items() if node["operation"] != "gate"}
    if semantic["accepted"] and observed != worker_nodes:
        raise core.LabError("Accepted execution lacks task observations")
    try:
        events = [json.loads(line, object_pairs_hook=core._pairs) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError) as exc:
        raise core.LabError("Malformed execution events") from exc
    if not events or events[0].get("kind") != "run_started" or events[-1].get("kind") != "run_completed":
        raise core.LabError("Incomplete event ledger")
    active, done, launched, launched_environments, evaluated = {}, {}, set(), {}, set()
    last_time = -1
    allowed = {"run_started", "run_completed", "launched", "completed", "blocked", "launch_failed", "executor_failed", "aborted", "final_identity_failed", "refused", "evaluated"}
    worker_kinds = {"launched", "completed", "launch_failed", "refused"}
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or type(event.get("sequence")) is not int or event.get("sequence") != index or
                not isinstance(event.get("kind"), str) or event.get("kind") not in allowed):
            raise core.LabError("Invalid event sequence or kind")
        stamp = event.get("monotonic_ns")
        if type(stamp) is not int or stamp < last_time:
            raise core.LabError("Invalid event time ordering")
        last_time = stamp
        kind, task_id = event["kind"], event.get("task_id")
        if kind in {"launched", "completed", "blocked", "launch_failed", "aborted", "refused", "evaluated"} and (not isinstance(task_id, str) or task_id not in nodes):
            raise core.LabError("Unknown event task")
        if kind in worker_kinds and nodes[task_id]["operation"] == "gate":
            raise core.LabError("A gate is evaluated by the controller, not launched")
        if kind == "launched":
            if task_id in launched or any(not _upstream_done(nodes, tasks, done, dep) for dep in nodes[task_id]["depends_on"]):
                raise core.LabError("Illegal launch transition/dependency")
            needed = plan["bindings"][task_id]["buffer_bytes"]
            if event.get("reserved_buffer_bytes") != needed:
                raise core.LabError("Event reservation contradicts binding")
            core.validate_environment_receipt(event.get("environment_names"), plan["resources"], task_id)
            launched_environments[task_id] = event["environment_names"]
            active[task_id] = needed
            launched.add(task_id)
        elif kind == "completed":
            if task_id in done:
                raise core.LabError("Repeated terminal event")
            if task_id not in active or event.get("status") != tasks[task_id]["status"]:
                raise core.LabError("Illegal completion transition")
            active.pop(task_id)
            done[task_id] = event["status"]
        elif kind == "evaluated":
            output_row = tasks[task_id].get("output")
            if (nodes[task_id]["operation"] != "gate" or task_id in done or
                    any(not _upstream_done(nodes, tasks, done, dep) for dep in nodes[task_id]["depends_on"]) or
                    event.get("status") != "succeeded" or not isinstance(output_row, dict) or
                    event.get("outcome") != output_row.get("outcome")):
                raise core.LabError("Illegal gate evaluation")
            done[task_id] = "succeeded"
            evaluated.add(task_id)
        elif kind in {"blocked", "launch_failed", "aborted", "refused"}:
            if task_id in done:
                raise core.LabError("Repeated terminal event")
            if kind == "blocked" and ("gate_ids" in event or "blocked_by" in tasks[task_id]):
                if event.get("gate_ids") != tasks[task_id].get("blocked_by"):
                    raise core.LabError("Blocked event does not name the gate")
            active.pop(task_id, None)
            done[task_id] = tasks[task_id]["status"]
        if kind in {"launched", "completed"}:
            if (type(event.get("workers_active")) is not int or type(event.get("used_buffer_bytes")) is not int or
                    event.get("workers_active") != len(active) or event.get("used_buffer_bytes") != sum(active.values()) or
                    len(active) > plan["resources"]["workers"] or sum(active.values()) > plan["resources"]["read_buffer_bytes"]):
                raise core.LabError("Execution event violates resource accounting")
    for task_id, names in ((row["task_id"], row["environment_names"]) for row in observations):
        if launched_environments.get(task_id) != names:
            raise core.LabError("Observation environment receipt contradicts the launch event")
    if active or events[-1].get("accepted") is not semantic["accepted"]:
        raise core.LabError("Terminal event contradicts execution status")
    if semantic["accepted"] and set(done) != set(nodes):
        raise core.LabError("Accepted execution lacks completion events")
    for task_id, node in nodes.items():
        if node["operation"] == "gate" and tasks[task_id]["status"] == "succeeded" and task_id not in evaluated:
            raise core.LabError("Gate evaluation event is missing")
    verify_ledger(plan, semantic, observations, events)
