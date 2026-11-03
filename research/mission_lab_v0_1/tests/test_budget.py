"""Model-budget reservation, settlement, and offline ledger checks.

Prices in these tests are injected fixtures. They are not a provider rate card.
No test opens a socket or reads an API key.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from research.mission_lab_v0_1 import budget, core, executor, planner, report
from research.mission_lab_v0_1 import replan


def _completion(content="fixture-reply-9f3a", prompt_tokens=3, completion_tokens=2, total_tokens=5):
    return json.dumps({
        "choices": [{"finish_reason": "stop", "index": 0, "message": {"content": content, "role": "assistant"}}],
        "model": "grok-test",
        "usage": {"completion_tokens": completion_tokens, "prompt_tokens": prompt_tokens, "total_tokens": total_tokens},
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _prices(per="token", input_price=3, output_price=15):
    return {"grok-test": {"input": input_price, "output": output_price, "per": per}}


def _budget(**overrides):
    value = {
        "max_requests": 2,
        "max_output_tokens": 64,
        "max_total_tokens": 128,
        "max_spend": 1_000_000,
        "unit": "micro-usd",
        "prices": _prices(),
    }
    value.update(overrides)
    return value


def _profile(**extra):
    return {"schema_version": 1, "workers": 2, "read_buffer_bytes": 65536, "timeout_seconds": 10,
            "estimate_origin": "injected_fixture", **extra}


def _mission(path, payload, input_id="reply"):
    path.write_bytes(payload)
    return {"schema_version": 1, "mission_id": "grok-budget", "requirements": [{
        "id": "record-call", "text": "Record confined Grok calls and settle the declared model budget.",
        "acceptance_criteria": ["The ledger matches archived usage and does not exceed the declared caps."],
        "evidence_refs": []}],
        "inputs": [{"id": input_id, "path": str(path), "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest()}],
        "unresolved": []}


def _ask(verify, task_id="ask", **overrides):
    node = {"id": task_id, "operation": "llm:grok", "model": "grok-test", "max_output_tokens": 16,
            "temperature": 0, "timeout_seconds": 2, "max_response_bytes": 4096, "transport": "fixture",
            "prompt": "ping", "fixture_id": "reply", "depends_on": [verify["id"]],
            "outputs": ["output:" + task_id], "state_reads": [verify["outputs"][0]],
            "state_writes": ["output:" + task_id]}
    node.update(overrides)
    return node


def _with_asks(mission, task_ids=("ask",)):
    graph = planner.compile_graph(mission)
    verify = next(node for node in graph if node["operation"] == "verify_digest" and node["input_id"] == "reply")
    return {**mission, "tasks": graph + [_ask(verify, task_id) for task_id in task_ids]}


def _hash_mission(path):
    payload = b"abc"
    path.write_bytes(payload)
    return {"schema_version": 1, "mission_id": "hash-budget", "requirements": [{
        "id": "exact", "text": "Preserve the digest.", "acceptance_criteria": ["Exact digest."], "evidence_refs": []}],
        "inputs": [{"id": "sample", "path": str(path), "size_bytes": 3, "sha256": hashlib.sha256(payload).hexdigest()}],
        "unresolved": []}


@pytest.fixture
def confined(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    monkeypatch.setattr(executor, "WORKSPACE", tmp_path)
    return tmp_path


@pytest.fixture
def evidence_dir():
    root = core.ROOT / "evidence"
    root.mkdir(exist_ok=True)
    path = root / ("budget-test-" + hashlib.sha256(os.urandom(8)).hexdigest()[:12])
    path.mkdir()
    yield path
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def _reseal(run):
    manifest = core.read_json(run / "integrity.json")
    manifest["files"] = {path.relative_to(run).as_posix(): core.file_hash(path)
                         for path in run.rglob("*") if path.is_file() and path.name != "integrity.json"}
    if "semantic_sha256" in manifest:
        manifest["semantic_sha256"] = core.file_hash(run / "semantic.json")
    (run / "integrity.json").write_text(core.canonical(manifest), encoding="utf-8", newline="\n")


def test_spend_ceiling_is_per_side_and_has_no_float():
    token = {"input": 3, "output": 15, "per": "token"}
    assert budget.spend_for(4, 16, token) == 252
    assert budget.spend_for(3, 2, token) == 39
    million = {"input": 1, "output": 1, "per": "million"}
    assert budget.spend_for(0, 0, million) == 0
    assert budget.spend_for(1, 0, million) == 1
    assert budget.spend_for(1_000_000, 0, million) == 1
    assert budget.spend_for(1_000_001, 0, million) == 2
    assert budget.spend_for(1, 1, million) == 2


@pytest.mark.parametrize("mutant,message", [
    ({"max_spend": 1.5}, "integer"),
    ({"max_requests": True}, "integer"),
    ({"max_spend": -1}, "integer"),
    ({"unit": "USD"}, "fixed-point label"),
    ({"prices": {}}, "1..32"),
    ({"prices": {"gpt-4": {"input": 1, "output": 1, "per": "token"}}}, "model id"),
    ({"prices": {"grok-test": {"input": 1, "output": 1, "per": "token", "currency": "usd"}}}, "unknown"),
    ({"prices": {"grok-test": {"input": 1, "output": 1, "per": "thousand"}}}, "token or million"),
    ({"note": "invoice"}, "unknown"),
])
def test_model_budget_rejects_undeclared_or_non_integer_prices(mutant, message):
    value = _budget()
    value.update(mutant)
    with pytest.raises(core.LabError, match=message):
        core.validate_resources(_profile(model_budget=value))


def test_profile_without_a_budget_does_not_grow_a_reservation(confined):
    payload = _completion()
    mission = _with_asks(_mission(confined / "reply.json", payload))
    plan = planner.build_plan(mission, _profile())
    assert "model_budget" not in plan["resources"]
    assert "model_reservation" not in plan["bindings"]["ask"]
    assert "model_reservation" not in plan["bindings"]["manifest"]
    profile = _profile()
    profile["model_budget"] = _budget()
    profile["model_budget"]["prices"] = {"grok-other": {"input": 1, "output": 1, "per": "token"}}
    with pytest.raises(core.LabError, match="no price for grok-test"):
        planner.build_plan(mission, profile)


def test_exact_boundaries_admit_equality_and_reject_one_past_it(confined):
    mission = _with_asks(_mission(confined / "reply.json", _completion()))
    fitting = _budget(max_requests=1, max_output_tokens=16, max_total_tokens=20, max_spend=252)
    plan = planner.build_plan(mission, _profile(model_budget=fitting))
    reservation = plan["bindings"]["ask"]["model_reservation"]
    assert reservation == {
        "requests": 1, "input_tokens": 4, "output_tokens": 16, "total_tokens": 20,
        "spend": 252, "input_bound": "prompt_bytes",
    }
    for key, value in (("max_spend", 251), ("max_requests", 0), ("max_output_tokens", 15), ("max_total_tokens", 19)):
        tight = dict(fitting)
        tight[key] = value
        with pytest.raises(core.LabError, match="cannot reserve task ask"):
            planner.build_plan(mission, _profile(model_budget=tight))
    million = _budget(max_spend=0, prices=_prices(per="million", input_price=1, output_price=0))
    with pytest.raises(core.LabError, match="cannot reserve task ask"):
        planner.build_plan(mission, _profile(model_budget=million))
    million["max_spend"] = 1
    admitted = planner.build_plan(mission, _profile(model_budget=million))
    assert admitted["bindings"]["ask"]["model_reservation"]["spend"] == 1


def test_two_calls_fail_closed_when_only_their_sum_exceeds_the_budget(confined):
    mission = _with_asks(_mission(confined / "reply.json", _completion()), ("ask-a", "ask-b"))
    room = _budget(max_requests=2, max_output_tokens=32, max_total_tokens=40, max_spend=504)
    plan = planner.build_plan(mission, _profile(model_budget=room))
    assert plan["bindings"]["ask-a"]["model_reservation"]["spend"] == 252
    assert plan["bindings"]["ask-b"]["model_reservation"]["spend"] == 252
    for key, value in (("max_spend", 503), ("max_requests", 1), ("max_output_tokens", 31), ("max_total_tokens", 39)):
        tight = dict(room)
        tight[key] = value
        with pytest.raises(core.LabError, match="declared model budget"):
            planner.build_plan(mission, _profile(model_budget=tight))


def test_declared_input_bound_cannot_undercut_the_prompt_byte_length(confined):
    payload = _completion()
    base = _mission(confined / "reply.json", payload)
    graph = planner.compile_graph(base)
    verify = next(node for node in graph if node["operation"] == "verify_digest")
    low = {**base, "tasks": graph + [_ask(verify, max_input_tokens=3)]}
    with pytest.raises(core.LabError, match="below the prompt byte length"):
        planner.build_plan(low, _profile(model_budget=_budget()))
    raised = {**base, "tasks": graph + [_ask(verify, max_input_tokens=6)]}
    plan = planner.build_plan(raised, _profile(model_budget=_budget()))
    reservation = plan["bindings"]["ask"]["model_reservation"]
    assert reservation["input_bound"] == "declared"
    assert reservation["input_tokens"] == 6
    assert reservation["total_tokens"] == 22
    assert reservation["spend"] == budget.spend_for(6, 16, _prices()["grok-test"])


def test_fixture_settlement_records_reserved_and_actual_and_replays(evidence_dir):
    payload = _completion()
    source = evidence_dir / "reply.json"
    mission = _with_asks(_mission(source, payload))
    profile = core.read_json(core.ROOT / "fixtures" / "resources_model.json")
    plan = planner.build_plan(mission, profile)
    output = executor.run_plan_data(plan, evidence_dir / "settled")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is True
    ask = next(task for task in semantic["tasks"] if task["task_id"] == "ask")
    assert ask["model_account"]["state"] == "settled"
    assert ask["model_account"]["reserved"]["spend"] == 252
    assert ask["model_account"]["actual"] == {
        "requests": 1, "input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "spend": 39,
    }
    ledger = semantic["model_budget"]
    assert ledger["unit"] == "micro-usd"
    assert ledger["totals"]["reserved"]["spend"] == 252
    assert ledger["totals"]["actual"]["spend"] == 39
    assert ledger["totals"]["accounted"]["spend"] == 39
    assert ledger["totals"]["remaining"]["spend"] == 1_000_000 - 39
    assert ledger["totals"]["accounted"]["requests"] == 1
    text = (output / "report.md").read_text(encoding="utf-8")
    assert "## Model budget" in text
    assert "micro-usd" in text
    assert "| ask | settled | 1 | 1 | 4 | 3 | 16 | 2 | 20 | 5 | 252 | 39 |" in text
    replay = report.replay_run(output)
    assert replay["accepted"] is True
    assert replay["independent_execution"] is False
    observations = core.read_json(output / "observations.json")
    observed = next(row for row in observations if row["task_id"] == "ask")
    assert observed["model_account"] == ask["model_account"]


def test_profile_without_a_budget_keeps_the_previous_receipt_shape(evidence_dir):
    payload = _completion()
    mission = _with_asks(_mission(evidence_dir / "reply.json", payload))
    plan = planner.build_plan(mission, _profile())
    output = executor.run_plan_data(plan, evidence_dir / "unbudgeted")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is True
    assert "model_budget" not in semantic
    assert all("model_account" not in task for task in semantic["tasks"])
    observations = core.read_json(output / "observations.json")
    assert all("model_account" not in row for row in observations)
    assert "## Model budget" not in (output / "report.md").read_text(encoding="utf-8")
    events = (output / "events.jsonl").read_text(encoding="utf-8")
    assert "model_" not in events
    replay = report.replay_run(output)
    assert replay["accepted"] is True


def test_hash_only_budget_accounts_zeros(evidence_dir):
    mission = _hash_mission(evidence_dir / "sample.bin")
    plan = planner.build_plan(mission, _profile(model_budget=_budget()))
    assert all(binding["model_reservation"]["requests"] == 0 for binding in plan["bindings"].values())
    output = executor.run_plan_data(plan, evidence_dir / "hash-budget")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is True
    assert semantic["model_budget"]["totals"]["accounted"] == budget.zero_cost()
    assert semantic["model_budget"]["totals"]["remaining"]["requests"] == 2
    assert report.replay_run(output)["accepted"] is True


def test_usage_above_the_reservation_keeps_the_hold_and_does_not_pass_the_cap(evidence_dir):
    payload = _completion(prompt_tokens=100, completion_tokens=2, total_tokens=102)
    mission = _with_asks(_mission(evidence_dir / "reply.json", payload))
    fitting = _budget(max_requests=1, max_output_tokens=16, max_total_tokens=20, max_spend=252)
    plan = planner.build_plan(mission, _profile(model_budget=fitting))
    output = executor.run_plan_data(plan, evidence_dir / "over-usage")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is False
    ask = next(task for task in semantic["tasks"] if task["task_id"] == "ask")
    assert ask["status"] == "failed"
    assert ask["output"] is None
    assert "exceeded the model reservation" in ask["error"]["message"]
    assert ask["model_account"]["state"] == "charged"
    assert ask["model_account"]["actual"] is None
    assert ask["model_account"]["reserved"]["spend"] == 252
    ledger = semantic["model_budget"]
    assert ledger["totals"]["accounted"]["spend"] == 252
    assert ledger["totals"]["actual"]["spend"] == 0
    assert ledger["totals"]["remaining"]["spend"] == 0
    assert ledger["totals"]["accounted"]["requests"] == 1
    replay = report.replay_run(output)
    assert replay["accepted"] is False


def test_replay_rejects_a_ledger_that_disagrees_with_the_receipt(evidence_dir):
    payload = _completion()
    mission = _with_asks(_mission(evidence_dir / "reply.json", payload))
    plan = planner.build_plan(mission, _profile(model_budget=_budget()))
    output = executor.run_plan_data(plan, evidence_dir / "tamper")
    assert report.replay_run(output)["accepted"] is True

    def contradict(message):
        _reseal(output)
        with pytest.raises(core.LabError, match=message):
            report.replay_run(output)

    semantic = core.read_json(output / "semantic.json")
    semantic["model_budget"]["totals"]["accounted"]["spend"] += 1
    (output / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8", newline="\n")
    (output / "report.md").write_text(report.render_report(semantic, core.read_json(output / "observations.json")), encoding="utf-8", newline="\n")
    contradict("summary contradicts")

    fresh = executor.run_plan_data(plan, evidence_dir / "tamper-observation")
    observations = core.read_json(fresh / "observations.json")
    row = next(item for item in observations if item["task_id"] == "ask")
    row["model_account"]["actual"]["spend"] = 40
    (fresh / "observations.json").write_text(core.canonical(observations), encoding="utf-8", newline="\n")
    semantic = core.read_json(fresh / "semantic.json")
    (fresh / "report.md").write_text(report.render_report(semantic, observations), encoding="utf-8", newline="\n")
    output = fresh
    contradict("Observation model account")

    fresh = executor.run_plan_data(plan, evidence_dir / "tamper-event")
    rows = [json.loads(line) for line in (fresh / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    completed = next(row for row in rows if row["kind"] == "completed" and row["task_id"] == "ask")
    completed["model_accounted"]["spend"] += 1
    (fresh / "events.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8", newline="\n")
    output = fresh
    contradict("running ledger")


def test_replan_tightens_the_budget_and_a_further_unit_has_no_plan(evidence_dir):
    payload = _completion()
    mission = _with_asks(_mission(evidence_dir / "reply.json", payload))
    baseline_profile = core.read_json(core.ROOT / "fixtures" / "resources_model.json")
    tight_profile = core.read_json(core.ROOT / "fixtures" / "resources_model_tight.json")
    baseline = planner.build_plan(mission, baseline_profile)
    trigger = {"schema_version": 1, "kind": "resource_change",
               "text": "Injected model budget reduced to the worst-case reservation of one 4-byte prompt.",
               "evidence_origin": "injected_demo", "evidence_refs": []}
    revised = replan.replan_data(baseline, tight_profile, trigger)
    assert revised["plan_id"] != baseline["plan_id"]
    assert revised["revision"]["prior_plan_id"] == baseline["plan_id"]
    assert revised["resources"]["model_budget"]["max_spend"] == 252
    assert revised["bindings"]["ask"]["model_reservation"]["spend"] == 252
    first = executor.run_plan_data(baseline, evidence_dir / "baseline")
    second = executor.run_plan_data(revised, evidence_dir / "revised")
    before = core.read_json(first / "semantic.json")["model_budget"]
    after = core.read_json(second / "semantic.json")["model_budget"]
    assert before["limits"]["max_spend"] == 1_000_000
    assert after["limits"]["max_spend"] == 252
    assert after["limits"]["max_requests"] == 1
    assert after["limits"]["max_output_tokens"] == 16
    assert after["limits"]["max_total_tokens"] == 20
    assert before["totals"]["remaining"]["spend"] == 1_000_000 - 39
    assert after["totals"]["remaining"]["spend"] == 252 - 39
    assert "of 1000000" in (first / "report.md").read_text(encoding="utf-8")
    assert "of 252" in (second / "report.md").read_text(encoding="utf-8")
    assert report.replay_run(first)["accepted"] is True
    assert report.replay_run(second)["accepted"] is True
    smaller = core.read_json(core.ROOT / "fixtures" / "resources_model_tight.json")
    smaller["model_budget"]["max_spend"] = 251
    with pytest.raises(core.LabError, match="cannot reserve task ask"):
        replan.replan_data(baseline, smaller, trigger)


def test_scheduler_refuses_a_launch_that_would_exceed_the_remaining_budget(evidence_dir, monkeypatch):
    payload = _completion()
    mission = _with_asks(_mission(evidence_dir / "reply.json", payload), ("ask-a", "ask-b"))
    plan = planner.build_plan(mission, _profile(model_budget=_budget(max_requests=2, max_spend=1_000_000)))
    plan["resources"]["model_budget"]["max_requests"] = 1
    plan["plan_id"] = core.seal_plan(plan)["plan_id"]
    # The planner would refuse this profile. The patch keeps that refusal out of
    # the executor's plan check so the launch gate has to enforce the cap itself.
    monkeypatch.setattr(planner, "validate_plan_record", lambda value: value)
    launched = []
    real_popen = executor.subprocess.Popen

    def spy(command, **kwargs):
        job = core.read_json(command[command.index("--job") + 1])
        launched.append(job["node"]["id"])
        return real_popen(command, **kwargs)

    monkeypatch.setattr(executor.subprocess, "Popen", spy)
    output = executor.run_plan_data(plan, evidence_dir / "refused")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is False
    tasks = {task["task_id"]: task for task in semantic["tasks"]}
    assert tasks["ask-a"]["status"] == "succeeded"
    assert tasks["ask-a"]["model_account"]["state"] == "settled"
    assert tasks["ask-b"]["status"] == "blocked"
    assert tasks["ask-b"]["model_account"]["state"] == "refused"
    assert "cannot reserve task ask-b" in tasks["ask-b"]["error"]["message"]
    assert "ask-b" not in launched
    assert "ask-a" in launched
    ledger = semantic["model_budget"]
    assert ledger["totals"]["accounted"]["requests"] == 1
    assert ledger["totals"]["accounted"]["spend"] == 39
    assert ledger["limits"]["max_requests"] == 1
    assert ledger["totals"]["remaining"]["requests"] == 0
    monkeypatch.undo()
    with pytest.raises(core.LabError):
        report.replay_run(output)
