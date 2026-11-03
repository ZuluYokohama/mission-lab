"""Merit gates: closed predicates, blocked downstream work, replay, and approval files.

Runs use the fixture transport. They do not open a socket and do not read an API key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from research.mission_lab_v0_1 import core, executor, gates, operators, planner, replan, report


CONTENT_SHA = "19624095ed12be774a42f02c3773a553f36e260c071bf5003724ec2c483128c3"
DIGEST = "a" * 64


def _profile(**extra):
    return {"schema_version": 1, "workers": 2, "read_buffer_bytes": 4 * 1024 * 1024, "timeout_seconds": 30,
            "estimate_origin": "injected_fixture", **extra}


def _budget():
    return {"max_requests": 2, "max_spend": 1000, "max_output_tokens": 64, "max_total_tokens": 64,
            "unit": "fixture", "prices": {"grok-test": {"input": 1, "output": 1, "per": "token"}}}


def _mission():
    return core.load_mission(core.ROOT / "fixtures" / "mission_gate.json")


def _replace_predicate(mission, predicate, **node_edits):
    drafted = copy.deepcopy(mission)
    merit = next(node for node in drafted["tasks"] if node["id"] == "merit")
    merit["predicate"] = predicate
    merit.update(node_edits)
    return drafted


def _task(semantic, task_id):
    return next(task for task in semantic["tasks"] if task["task_id"] == task_id)


def _rewrite_events(output, mutate):
    rows = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = mutate(rows)
    for index, row in enumerate(rows):
        row["sequence"] = index
    (output / "events.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8", newline="\n")
    _reseal(output)


def _reseal(output):
    from research.mission_lab_v0_1.seal import reseal
    reseal(output)


@pytest.fixture
def evidence_dir():
    root = core.ROOT / "evidence"
    root.mkdir(exist_ok=True)
    path = root / ("gate-test-" + hashlib.sha256(os.urandom(8)).hexdigest()[:12])
    path.mkdir()
    yield path
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def test_closed_predicates_reject_code_and_unknown_references():
    mission = _mission()
    for predicate, message in (
            ({"kind": "python", "source": "print(1)"}, "Unknown gate predicate"),
            ({"kind": "llm_field", "node": "ask", "field": "content_sha256", "equals": "ok", "pattern": "ok"}, "equals or pattern"),
            ({"kind": "llm_field", "node": "ask", "field": "content_sha256", "pattern": ".*"}, "unsupported character"),
            ({"kind": "llm_field", "node": "ask", "field": "content_sha256", "pattern": "(?P<name>a)"}, "unsupported character"),
            ({"kind": "numeric_bounds", "node": "ask", "field": "total_tokens", "minimum": True}, "integer"),
            ({"kind": "llm_field", "node": "hash:reply", "field": "content_sha256", "equals": "a"}, "llm:grok"),
            ({"kind": "digest_equal", "nodes": ["missing", "hash:reply"]}, "not a dependency"),
            ({"kind": "human_approval", "approver_id": "reviewer-1"}, None),
    ):
        drafted = _replace_predicate(mission, predicate)
        if predicate["kind"] == "human_approval":
            drafted = _replace_predicate(mission, predicate, depends_on=["ask"], state_reads=["output:ask"])
            plan = planner.build_plan(drafted, _profile())
            assert plan["bindings"]["merit"]["candidate"] == "predicate_v1"
            assert plan["bindings"]["merit"]["buffer_bytes"] == 0
            assert "model_reservation" not in plan["bindings"]["merit"]
            continue
        with pytest.raises(core.LabError, match=message):
            planner.build_plan(drafted, _profile())
    drafted = copy.deepcopy(mission)
    next(node for node in drafted["tasks"] if node["id"] == "merit")["depends_on"] = ["follow"]
    next(node for node in drafted["tasks"] if node["id"] == "follow")["depends_on"] = ["merit", "verify:reply"]
    with pytest.raises(core.LabError, match="Cycle or missing dependency"):
        planner.build_plan(drafted, _profile())
    drafted = copy.deepcopy(mission)
    merit = next(node for node in drafted["tasks"] if node["id"] == "merit")
    merit["depends_on"] = ["ask", "hash:reply", "reference:reply", "missing"]
    with pytest.raises(core.LabError, match="Cycle or missing dependency"):
        planner.build_plan(drafted, _profile())
    assert gates._match_pattern(":hex:{64}", CONTENT_SHA) is True
    assert gates._match_pattern(":hex:{64}", CONTENT_SHA[:-1] + "g") is False
    assert gates._match_pattern("stop", "stop") is True


def test_composition_pass_fail_and_indeterminate_are_stable():
    same = {"sha256": DIGEST, "size_bytes": 3}
    other = {"sha256": DIGEST, "size_bytes": 4}
    ask = {"content_sha256": CONTENT_SHA, "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    node = {"id": "merit", "operation": "gate", "predicate": {"kind": "all", "predicates": [
        {"kind": "digest_equal", "nodes": ["hash:reply", "reference:reply"]},
        {"kind": "numeric_bounds", "node": "ask", "field": "total_tokens", "maximum": 1},
    ]}}
    failed = gates.evaluate(node, {"outputs": {"hash:reply": same, "reference:reply": same, "ask": ask}})
    assert failed["outcome"] == "fail"
    assert failed["reason"] == "child predicate failed: total_tokens is outside bounds"
    node["predicate"] = {"kind": "any", "predicates": node["predicate"]["predicates"]}
    passed = gates.evaluate(node, {"outputs": {"hash:reply": same, "reference:reply": same, "ask": ask}})
    assert passed["outcome"] == "pass"
    assert passed["reason"] == "a child predicate passed"
    node["predicate"] = {"kind": "digest_equal", "nodes": ["ask", "hash:reply"]}
    missing = gates.evaluate(node, {"outputs": {"ask": ask, "hash:reply": same}})
    assert missing["outcome"] == "indeterminate"
    assert missing["reason"] == "digest or byte count is missing"
    node["predicate"] = {"kind": "digest_equal", "nodes": ["hash:reply", "reference:reply"]}
    differed = gates.evaluate(node, {"outputs": {"hash:reply": same, "reference:reply": other}})
    assert differed["outcome"] == "fail"
    again = gates.evaluate(node, {"outputs": {"hash:reply": same, "reference:reply": other}})
    assert again == differed
    node["predicate"] = {"kind": "numeric_bounds", "node": "ask", "field": "spend", "maximum": 10}
    no_budget = gates.evaluate(node, {"outputs": {"ask": ask}})
    assert no_budget["outcome"] == "indeterminate"
    settled = gates.evaluate(node, {"outputs": {"ask": ask}, "accounts": {"ask": {"state": "settled", "actual": {
        "requests": 1, "input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "spend": 5}}}})
    assert settled["outcome"] == "pass"
    node["predicate"] = {"kind": "numeric_bounds", "node": "ask", "field": "exit_code", "minimum": 0, "maximum": 0}
    assert gates.evaluate(node, {"exit_codes": {"ask": 0}})["outcome"] == "pass"
    assert gates.evaluate(node, {"exit_codes": {"ask": 2}})["outcome"] == "fail"


def test_fixture_mission_passes_and_replays_without_a_gate_worker(evidence_dir, monkeypatch):
    mission = _mission()
    plan = planner.build_plan(mission, _profile())
    merit = next(node for node in plan["graph"] if node["id"] == "merit")
    schedule = {row["task_id"]: row for row in plan["schedule"]}
    assert schedule["follow"]["start_ns"] >= schedule["merit"]["end_ns"]
    assert plan["bindings"]["merit"] == {"candidate": "predicate_v1", "revision": "1", "buffer_bytes": 0,
                                         "duration_ns": plan["resources"]["cost_model"]["fixed_task_ns"]}
    launches = []
    real_popen = executor.subprocess.Popen

    def spy(command, **kwargs):
        if isinstance(command, (list, tuple)) and "--job" in command:
            job = core.read_json(Path(command[command.index("--job") + 1]))
            launches.append(job["node"]["id"])
        return real_popen(command, **kwargs)

    monkeypatch.setattr(executor.subprocess, "Popen", spy)
    output = executor.run_plan_data(plan, evidence_dir / "pass")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is True
    gate = _task(semantic, "merit")
    assert gate["status"] == "succeeded"
    assert gate["output"]["outcome"] == "pass"
    assert gate["output"]["reason"] == "all child predicates passed"
    assert _task(semantic, "follow")["status"] == "succeeded"
    assert "merit" not in launches
    assert "follow" in launches
    job = {"schema_version": 1, "node": merit, "binding": plan["bindings"]["merit"], "input": None,
           "dependencies": {dep: {"task_id": dep, "status": "succeeded", "output": {"noted": True}, "error": None}
                            for dep in merit["depends_on"]}}
    with pytest.raises(core.LabError, match="evaluated by the controller"):
        operators.execute_operation(job)
    replay = report.replay_run(output)
    assert replay["accepted"] is True
    assert replay["independent_execution"] is False
    assert "gate-fixture-ok" not in (output / "report.md").read_text(encoding="utf-8")
    assert "Merit gates" in replay["report"]


def test_failed_and_indeterminate_gates_block_downstream_and_do_not_charge_it(evidence_dir):
    mission = _mission()
    failed_mission = _replace_predicate(mission, {"kind": "all", "predicates": [
        {"kind": "digest_equal", "nodes": ["hash:reply", "reference:reply"]},
        {"kind": "numeric_bounds", "node": "ask", "field": "total_tokens", "maximum": 1},
    ]})
    plan = planner.build_plan(failed_mission, _profile(model_budget=_budget()))
    assert plan["bindings"]["merit"]["model_reservation"] == {
        "requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "spend": 0, "input_bound": "none"}
    assert plan["bindings"]["follow"]["model_reservation"]["requests"] == 1
    output = executor.run_plan_data(plan, evidence_dir / "fail")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is False
    assert _task(semantic, "merit")["output"]["outcome"] == "fail"
    follow = _task(semantic, "follow")
    assert follow["status"] == "blocked"
    assert follow["blocked_by"] == ["merit"]
    assert "Blocked by gate merit" in follow["error"]["message"]
    assert follow["model_account"]["state"] == "unlaunched"
    assert _task(semantic, "merit")["model_account"]["state"] == "unlaunched"
    assert semantic["model_budget"]["totals"]["accounted"]["requests"] == 1
    assert semantic["model_budget"]["totals"]["accounted"]["spend"] == 5
    observations = core.read_json(output / "observations.json")
    assert "follow" not in {row["task_id"] for row in observations}
    assert "merit" not in {row["task_id"] for row in observations}
    replay = report.replay_run(output)
    assert replay["accepted"] is False
    assert "Blocked by gate merit" in replay["report"]

    unclear = _replace_predicate(mission, {"kind": "digest_equal", "nodes": ["ask", "hash:reply"]},
                                 depends_on=["ask", "hash:reply"],
                                 state_reads=["output:ask", "output:hash:reply"])
    unclear_plan = planner.build_plan(unclear, _profile(model_budget=_budget()))
    unclear_out = executor.run_plan_data(unclear_plan, evidence_dir / "indeterminate")
    unclear_semantic = core.read_json(unclear_out / "semantic.json")
    assert _task(unclear_semantic, "merit")["output"]["outcome"] == "indeterminate"
    assert _task(unclear_semantic, "follow")["status"] == "blocked"
    assert _task(unclear_semantic, "follow")["model_account"]["state"] == "unlaunched"
    assert unclear_semantic["model_budget"]["totals"]["accounted"]["requests"] == 1
    assert report.replay_run(unclear_out)["accepted"] is False


def test_replay_rejects_a_tampered_receipt_and_a_changed_predicate(evidence_dir):
    plan = planner.build_plan(_mission(), _profile())
    output = executor.run_plan_data(plan, evidence_dir / "replay")
    semantic = core.read_json(output / "semantic.json")
    gate = _task(semantic, "merit")
    gate["output"]["outcome"] = "fail"
    gate["output"]["reason"] = "field does not match"
    (output / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8", newline="\n")
    _reseal(output)
    with pytest.raises(core.LabError, match="Gate receipt does not match"):
        report.replay_run(output)

    fresh = executor.run_plan_data(plan, evidence_dir / "predicate")
    tightened = _replace_predicate(_mission(), {"kind": "all", "predicates": [
        {"kind": "digest_equal", "nodes": ["hash:reply", "reference:reply"]},
        {"kind": "llm_field", "node": "ask", "field": "content_sha256", "equals": CONTENT_SHA},
        {"kind": "numeric_bounds", "node": "ask", "field": "total_tokens", "minimum": 1, "maximum": 1},
    ]})
    revised = planner.build_plan(tightened, _profile(), sources=plan["sources"])
    (fresh / "plan.json").write_text(core.canonical(revised), encoding="utf-8", newline="\n")
    archived = core.read_json(fresh / "semantic.json")
    archived["plan_id"] = revised["plan_id"]
    (fresh / "semantic.json").write_text(core.canonical(archived), encoding="utf-8", newline="\n")
    _reseal(fresh)
    with pytest.raises(core.LabError, match="Gate predicate does not match"):
        report.replay_run(fresh)


def test_replay_rejects_a_forged_gate_event_ledger(evidence_dir):
    plan = planner.build_plan(_mission(), _profile())
    assert "model_budget" not in plan["resources"]

    def merit_event(rows):
        return next(row for row in rows if row.get("kind") == "evaluated" and row.get("task_id") == "merit")

    blocked = executor.run_plan_data(plan, evidence_dir / "blocked-event")

    def swap_blocked(rows):
        row = merit_event(rows)
        stamp = row["monotonic_ns"]
        row.clear()
        row.update({"sequence": 0, "kind": "blocked", "task_id": "merit", "monotonic_ns": stamp})
        return rows

    _rewrite_events(blocked, swap_blocked)
    with pytest.raises(core.LabError, match="Gate evaluation event is missing"):
        report.replay_run(blocked)

    forged = executor.run_plan_data(plan, evidence_dir / "worker-event")

    def swap_worker(rows):
        index = next(i for i, row in enumerate(rows) if row.get("kind") == "evaluated" and row.get("task_id") == "merit")
        stamp = rows[index]["monotonic_ns"]
        launched = {"sequence": 0, "kind": "launched", "task_id": "merit", "monotonic_ns": stamp, "pid": 1,
                    "reserved_buffer_bytes": 0, "used_buffer_bytes": 0, "workers_active": 1,
                    "environment_names": ["PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"]}
        completed = {"sequence": 0, "kind": "completed", "task_id": "merit", "monotonic_ns": stamp,
                     "status": "succeeded", "used_buffer_bytes": 0, "workers_active": 0}
        return rows[:index] + [launched, completed] + rows[index + 1:]

    _rewrite_events(forged, swap_worker)
    with pytest.raises(core.LabError, match="not launched"):
        report.replay_run(forged)

    failed = executor.run_plan_data(plan, evidence_dir / "launch-failed-event")

    def swap_launch_failed(rows):
        row = merit_event(rows)
        stamp = row["monotonic_ns"]
        row.clear()
        row.update({"sequence": 0, "kind": "launch_failed", "task_id": "merit", "monotonic_ns": stamp})
        return rows

    _rewrite_events(failed, swap_launch_failed)
    with pytest.raises(core.LabError, match="not launched"):
        report.replay_run(failed)


def test_replay_rejects_a_second_terminal_event(evidence_dir):
    plan = planner.build_plan(_mission(), _profile())
    assert "model_budget" not in plan["resources"]

    def append_terminal(rows, task_id, prior_kind, extra_kind):
        index = next(i for i, row in enumerate(rows) if row.get("kind") == prior_kind and row.get("task_id") == task_id)
        extra = {"sequence": 0, "kind": extra_kind, "task_id": task_id, "monotonic_ns": rows[index]["monotonic_ns"]}
        return rows[:index + 1] + [extra] + rows[index + 1:]

    gate_run = executor.run_plan_data(plan, evidence_dir / "second-gate")
    _rewrite_events(gate_run, lambda rows: append_terminal(rows, "merit", "evaluated", "blocked"))
    with pytest.raises(core.LabError, match="Repeated terminal event"):
        report.replay_run(gate_run)

    worker_run = executor.run_plan_data(plan, evidence_dir / "second-worker")
    _rewrite_events(worker_run, lambda rows: append_terminal(rows, "hash:reply", "completed", "aborted"))
    with pytest.raises(core.LabError, match="Repeated terminal event"):
        report.replay_run(worker_run)


def test_replay_rejects_completion_after_an_earlier_terminal(evidence_dir):
    plan = planner.build_plan(_mission(), _profile())
    assert "model_budget" not in plan["resources"]
    output = executor.run_plan_data(plan, evidence_dir / "blocked-before-launch")

    def block_before_launch(rows):
        index = next(i for i, row in enumerate(rows) if row.get("kind") == "launched" and row.get("task_id") == "hash:reply")
        blocked = {"sequence": 0, "kind": "blocked", "task_id": "hash:reply", "monotonic_ns": rows[index]["monotonic_ns"]}
        return rows[:index] + [blocked] + rows[index:]

    _rewrite_events(output, block_before_launch)
    with pytest.raises(core.LabError, match="Repeated terminal event"):
        report.replay_run(output)


def test_default_gate_evidence_text_is_truncated():
    reason = "x" * 200
    tasks = [{"task_id": f"g{index}", "output": {"outcome": "fail", "reason": reason}} for index in range(8)]
    joined = " ".join(f"g{index} fail: {reason}" for index in range(8))
    assert len(joined) > 1000
    trigger = replan.gate_evidence_trigger({"tasks": tasks})
    assert trigger["text"] == joined[:1000].strip()
    assert len(trigger["text"]) <= 1000
    assert replan.gate_evidence_trigger({"tasks": tasks})["text"] == trigger["text"]
    with pytest.raises(core.LabError, match="exceeds 1000"):
        replan.gate_evidence_trigger({"tasks": tasks[:1]}, text="y" * 1001)
    with pytest.raises(core.LabError, match="Revision trigger text is required"):
        replan.gate_evidence_trigger({"tasks": tasks[:1]}, text="   ")


def test_human_approval_blocks_until_a_resume_copies_an_unauthenticated_record(evidence_dir):
    mission = _replace_predicate(_mission(), {"kind": "human_approval", "approver_id": "reviewer-1"},
                                 id="gate:merit", depends_on=["ask"], state_reads=["output:ask"],
                                 outputs=["output:gate:merit"], state_writes=["output:gate:merit"])
    follow = next(node for node in mission["tasks"] if node["id"] == "follow")
    follow["depends_on"] = ["gate:merit", "verify:reply"]
    follow["state_reads"] = ["output:gate:merit", "output:verify:reply"]
    plan = planner.build_plan(mission, _profile())
    pending_run = executor.run_plan_data(plan, evidence_dir / "pending")
    pending = core.read_json(pending_run / "semantic.json")
    assert pending["accepted"] is False
    assert _task(pending, "gate:merit")["output"]["outcome"] == "pending"
    assert "not authenticated" in _task(pending, "gate:merit")["output"]["reason"]
    assert _task(pending, "follow")["blocked_by"] == ["gate:merit"]
    assert not (pending_run / "approvals.json").exists()
    assert report.replay_run(pending_run)["accepted"] is False

    approval = evidence_dir / "approval.json"
    approval.write_text(core.canonical({
        "schema_version": 1, "gate_id": "gate:merit", "approver_id": "reviewer-1",
        "approved_at": "2026-11-03T02:00:00Z"}), encoding="utf-8", newline="\n")
    resumed = executor.resume_run(pending_run, evidence_dir / "resumed", approval)
    semantic = core.read_json(resumed / "semantic.json")
    assert semantic["accepted"] is True
    assert _task(semantic, "gate:merit")["output"]["outcome"] == "pass"
    assert "not authenticated" in _task(semantic, "gate:merit")["output"]["reason"]
    assert semantic["resume_of"]["plan_id"] == plan["plan_id"]
    assert semantic["resume_of"]["prior_semantic_sha256"] == core.digest_object(pending)
    stored = core.read_json(resumed / "approvals.json")
    assert stored["records"][0]["approver_id"] == "reviewer-1"
    assert "not authenticated" in (resumed / "report.md").read_text(encoding="utf-8")
    assert report.replay_run(resumed)["accepted"] is True

    wrong = evidence_dir / "wrong.json"
    wrong.write_text(core.canonical({
        "schema_version": 1, "gate_id": "gate:merit", "approver_id": "other-reviewer",
        "approved_at": "2026-11-03T02:00:00Z"}), encoding="utf-8", newline="\n")
    rejected = executor.resume_run(pending_run, evidence_dir / "wrong", wrong)
    rejected_semantic = core.read_json(rejected / "semantic.json")
    assert _task(rejected_semantic, "gate:merit")["output"]["outcome"] == "fail"
    assert _task(rejected_semantic, "follow")["status"] == "blocked"
    assert report.replay_run(rejected)["accepted"] is False

    (pending_run / "approvals.json").write_text(core.canonical({
        "schema_version": 1, "records": [{
            "schema_version": 1, "gate_id": "gate:merit", "approver_id": "reviewer-1",
            "approved_at": "2026-11-03T02:00:00Z"}]}), encoding="utf-8", newline="\n")
    _reseal(pending_run)
    with pytest.raises(core.LabError, match="Gate receipt does not match"):
        report.replay_run(pending_run)
    with pytest.raises(core.LabError, match="Approval timestamp"):
        gates.validate_approval_record({"schema_version": 1, "gate_id": "gate:merit", "approver_id": "reviewer-1",
                                        "approved_at": "2026-02-31T00:00:00Z"})


def test_gate_failure_is_revision_evidence_and_can_tighten_or_swap(evidence_dir):
    mission = _mission()
    blob = evidence_dir / "blob.bin"
    blob.write_bytes(b"b" * 70000)
    base = {key: value for key, value in mission.items() if key != "tasks"}
    base["inputs"] = list(mission["inputs"]) + [{
        "id": "blob", "path": str(blob), "size_bytes": 70000,
        "sha256": hashlib.sha256(b"b" * 70000).hexdigest()}]
    ask = copy.deepcopy(next(node for node in mission["tasks"] if node["id"] == "ask"))
    merit = {"id": "merit", "operation": "gate", "depends_on": ["ask"], "outputs": ["output:merit"],
             "state_reads": ["output:ask"], "state_writes": ["output:merit"],
             "predicate": {"kind": "numeric_bounds", "node": "ask", "field": "total_tokens", "maximum": 1}}
    failed = {**base, "tasks": planner.compile_graph(base) + [ask, merit]}
    plan = planner.build_plan(failed, _profile())
    assert plan["bindings"]["hash:blob"]["candidate"] == "whole_v1"
    output = executor.run_plan_data(plan, evidence_dir / "evidence")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is False
    assert _task(semantic, "merit")["output"]["outcome"] == "fail"
    trigger = replan.gate_evidence_trigger(semantic)
    assert trigger["kind"] == "gate_evidence"
    assert trigger["evidence_refs"] == ["merit"]
    tightened = copy.deepcopy(plan["mission"])
    next(node for node in tightened["tasks"] if node["id"] == "merit")["predicate"]["maximum"] = 0
    tight_resources = _profile(read_buffer_bytes=65536)
    revised = replan.replan_data(plan, tight_resources, trigger, mission=tightened)
    assert revised["bindings"]["hash:blob"]["candidate"] == "stream_v1"
    assert revised["revision"]["trigger"]["kind"] == "gate_evidence"
    assert "merit" in revised["revision"]["trigger"]["evidence_refs"]
    assert "hash:blob" in revised["revision"]["changed_tasks"]
    revised_out = executor.run_plan_data(revised, evidence_dir / "revised")
    revised_semantic = core.read_json(revised_out / "semantic.json")
    assert revised_semantic["accepted"] is False
    assert _task(revised_semantic, "merit")["output"]["outcome"] == "fail"
    text = (revised_out / "report.md").read_text(encoding="utf-8")
    assert "gate_evidence" in text
    assert "merit" in text
    assert "hash:blob" in text
    assert report.replay_run(revised_out)["accepted"] is False
    with pytest.raises(core.LabError, match="Gate evidence must name a gate"):
        replan.replan_data(plan, tight_resources, {**trigger, "evidence_refs": ["ask"]})
