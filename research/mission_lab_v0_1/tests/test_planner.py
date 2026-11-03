"""Planner v2 against the v1 enumerator, and one large synthetic graph.

The large-graph limit is a bound on that test, not a measured speedup.
"""
from __future__ import annotations

import hashlib
import json
import random
import time

import pytest

from research.mission_lab_v0_1 import core, demo, planner, replan


SOURCES = {"planner.py": "ab" * 32}
# Local measurement of this graph was about one second. The bound leaves room
# for a slower CI host. It is not a comparison with the v1 enumerator.
SCALE_PLAN_SECONDS = 20


def _completion():
    return json.dumps({
        "choices": [{"finish_reason": "stop", "index": 0, "message": {"content": "ok", "role": "assistant"}}],
        "model": "grok-test",
        "usage": {"completion_tokens": 1, "prompt_tokens": 1, "total_tokens": 2},
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _profile(**extra):
    value = {"schema_version": 1, "workers": 2, "read_buffer_bytes": 4_194_304, "timeout_seconds": 30,
             "estimate_origin": "injected_fixture",
             "cost_model": {"whole_ns_per_byte": 1, "stream_ns_per_byte": 2,
                            "reference_ns_per_byte": 3, "fixed_task_ns": 1000}}
    value.update(extra)
    return value


def _mission(directory, sizes, mission_id="scale", fixture_at=None):
    directory.mkdir(parents=True, exist_ok=True)
    inputs = []
    completion = _completion()
    for index, size in enumerate(sizes):
        payload = completion if index == fixture_at else bytes([index % 251]) * size
        path = directory / f"f{index}.bin"
        path.write_bytes(payload)
        inputs.append({"id": f"i{index}", "path": str(path), "size_bytes": len(payload),
                       "sha256": hashlib.sha256(payload).hexdigest()})
    return {"schema_version": 1, "mission_id": mission_id, "requirements": [{
        "id": "exact", "text": "Preserve the declared digest.",
        "acceptance_criteria": ["Exact digest and byte count."], "evidence_refs": []}],
            "inputs": inputs, "unresolved": []}


def _with_llm(mission, count):
    graph = planner.compile_graph(mission)
    verify = next(node for node in graph if node["operation"] == "verify_digest" and node["input_id"] == "i0")
    tasks = list(graph)
    for index in range(count):
        tasks.append({
            "id": f"ask{index:04d}", "operation": "llm:grok", "model": "grok-test", "max_output_tokens": 16,
            "temperature": 0, "timeout_seconds": 2, "max_response_bytes": 65536, "transport": "fixture",
            "prompt": "ping", "fixture_id": "i0", "depends_on": [verify["id"]],
            "outputs": [f"oa{index}"], "state_reads": [verify["outputs"][0]], "state_writes": [f"oa{index}"],
        })
    return {**mission, "tasks": tasks}


def _budget(requests, output_tokens, total_tokens, spend=10**9):
    return {"max_requests": requests, "max_output_tokens": output_tokens, "max_total_tokens": total_tokens,
            "max_spend": spend, "unit": "micro-usd",
            "prices": {"grok-test": {"input": 3, "output": 15, "per": "token"}}}


def test_graph_cap_is_2048_and_still_rejects_an_empty_graph():
    assert core.MAX_GRAPH_NODES == 2048
    with pytest.raises(core.LabError, match="2048 nodes"):
        core.validate_graph([], [])
    with pytest.raises(core.LabError, match="2048 nodes"):
        core.validate_graph([{}] * (core.MAX_GRAPH_NODES + 1), [])


def test_fixture_profiles_keep_the_v1_bindings_and_schedule():
    mission = demo.fixture_mission()
    baseline = _profile(workers=2, read_buffer_bytes=4_194_304, timeout_seconds=60)
    tight = {**baseline, "read_buffer_bytes": 65536}
    for profile in (baseline, tight):
        v1 = planner.build_plan(mission, profile, sources=SOURCES, version=1)
        v2 = planner.build_plan(mission, profile, sources=SOURCES, version=2)
        assert "planner" not in v1
        assert v2["planner"] == {"id": planner.PLANNER_V2_ID, "version": planner.PLANNER_V2_VERSION}
        assert v1["bindings"] == v2["bindings"]
        assert v1["schedule"] == v2["schedule"]
        assert v1["estimated_makespan_ns"] == v2["estimated_makespan_ns"]
        assert v1["plan_id"] != v2["plan_id"]
        assert planner.validate_plan_record(v1) == v1
        assert planner.validate_plan_record(v2) == v2
    wide = planner.build_plan(mission, baseline, sources=SOURCES)
    narrow = planner.build_plan(mission, tight, sources=SOURCES)
    assert wide["bindings"]["hash:pattern"]["candidate"] == "whole_v1"
    assert wide["bindings"]["hash:empty"]["candidate"] == "stream_v1"
    assert narrow["bindings"]["hash:pattern"]["candidate"] == "stream_v1"


def test_v1_plan_replays_and_replanning_it_stamps_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    monkeypatch.setattr(core, "source_manifest", lambda: SOURCES)
    mission = _with_llm(_mission(tmp_path, [len(_completion()), 3], fixture_at=0), 1)
    profile = _profile()
    prior = planner.build_plan(mission, profile, version=1)
    assert prior["schedule_algorithm"] == planner.PLANNER_V1_ALGORITHM
    assert planner.validate_plan_record(prior) == prior
    updated = replan.replan_data(prior, profile, {
        "schema_version": 1, "kind": "resource_change", "text": "Same profile, current planner.",
        "evidence_origin": "injected_demo"})
    assert updated["planner"]["version"] == 2
    assert updated["bindings"] == prior["bindings"]
    assert updated["revision"]["prior_plan_id"] == prior["plan_id"]
    assert updated["revision"]["changed_tasks"] == []
    assert planner.validate_plan_record(updated) == updated
    forged = dict(prior)
    forged["planner"] = {"id": planner.PLANNER_V2_ID, "version": 1}
    forged["plan_id"] = core.seal_plan(forged)["plan_id"]
    with pytest.raises(core.LabError, match="Unsupported planner"):
        planner.validate_plan_record(forged)
    with pytest.raises(core.LabError, match="Unsupported planner"):
        planner.build_plan(mission, profile, sources=SOURCES, version=3)


def test_v1_refuses_a_graph_above_its_historical_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    mission = _with_llm(_mission(tmp_path, [len(_completion())], fixture_at=0), 61)
    assert len(planner.compile_graph(mission)) == 65
    profile = _profile()
    with pytest.raises(core.LabError, match="at most 64"):
        planner.build_plan(mission, profile, sources=SOURCES, version=1)
    plan = planner.build_plan(mission, profile, sources=SOURCES, version=2)
    assert len(plan["graph"]) == 65
    assert planner.validate_plan_record(plan)["plan_id"] == plan["plan_id"]


def test_coordinate_search_matches_enumeration_on_seeded_graphs(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    rng = random.Random(20260311)
    sizes_pool = [0, 1, 3, 100, 1024, 32768, 32769, 40000, 65536, 90000, 131073]
    checked = 0
    for trial in range(30):
        width = rng.randint(1, 4)
        sizes = [rng.choice(sizes_pool) for _ in range(width)]
        sizes[0] = len(_completion())
        llm = rng.choice([0, 0, 1, 2])
        mission = _with_llm(_mission(tmp_path / str(trial), sizes, mission_id=f"eq{trial}", fixture_at=0), llm)
        profile = _profile(
            workers=rng.randint(1, 8),
            read_buffer_bytes=rng.choice([8192, 32768, 65536, 131073, 400000, 4_194_304]),
            cost_model={
                "whole_ns_per_byte": rng.randint(1, 12),
                "stream_ns_per_byte": rng.randint(1, 12),
                "reference_ns_per_byte": rng.randint(1, 12),
                "fixed_task_ns": rng.choice([1, 10, 100, 1000, 5000]),
            })
        if llm and trial % 5 == 0:
            profile["model_budget"] = _budget(llm, 16 * llm, 20 * llm)
        try:
            v1 = planner.build_plan(mission, profile, sources=SOURCES, version=1)
        except core.LabError as exc:
            with pytest.raises(core.LabError) as caught:
                planner.build_plan(mission, profile, sources=SOURCES, version=2)
            assert str(caught.value) == str(exc)
            continue
        v2 = planner.build_plan(mission, profile, sources=SOURCES, version=2)
        assert v1["bindings"] == v2["bindings"]
        assert v1["schedule"] == v2["schedule"]
        assert v1["estimated_makespan_ns"] == v2["estimated_makespan_ns"]
        assert v2["enumerated_bindings"] <= v1["enumerated_bindings"]
        checked += 1
    assert checked >= 20
    # Whole-file is shorter on the two large inputs and reserves more buffer.
    # Enumeration streams both. One flip from the shorter bindings does not
    # improve the key; changing the pair does.
    hard = _mission(tmp_path / "hard", [100000, 131073, 1], mission_id="hard")
    hard_profile = _profile(workers=3, read_buffer_bytes=131073, cost_model={
        "whole_ns_per_byte": 6, "stream_ns_per_byte": 10, "reference_ns_per_byte": 4, "fixed_task_ns": 10000})
    v1 = planner.build_plan(hard, hard_profile, sources=SOURCES, version=1)
    v2 = planner.build_plan(hard, hard_profile, sources=SOURCES, version=2)
    assert v1["bindings"] == v2["bindings"]
    assert v1["schedule"] == v2["schedule"]
    assert [v2["bindings"][f"hash:i{index}"]["candidate"] for index in range(3)] == ["stream_v1", "stream_v1", "whole_v1"]


def test_budget_sum_and_exact_boundary_stay_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    mission = _with_llm(_mission(tmp_path, [len(_completion())], fixture_at=0), 2)
    # prompt "ping" is 4 bytes. Each call reserves 4 + 16 tokens and spend 4*3 + 16*15 = 252.
    fitting = _profile(model_budget=_budget(2, 32, 40, spend=504))
    plan = planner.build_plan(mission, fitting, sources=SOURCES)
    assert plan["bindings"]["ask0000"]["model_reservation"]["spend"] == 252
    assert plan["bindings"]["ask0001"]["model_reservation"]["spend"] == 252
    for key, value, message in (
            ("max_spend", 503, "declared model budget"),
            ("max_requests", 1, "declared model budget"),
            ("max_output_tokens", 31, "declared model budget"),
            ("max_total_tokens", 39, "declared model budget")):
        tight = _budget(2, 32, 40, spend=504)
        tight[key] = value
        with pytest.raises(core.LabError, match=message):
            planner.build_plan(mission, _profile(model_budget=tight), sources=SOURCES)
    one = _with_llm(_mission(tmp_path / "one", [len(_completion())], fixture_at=0), 1)
    edge = _profile(model_budget=_budget(1, 16, 20, spend=252))
    admitted = planner.build_plan(one, edge, sources=SOURCES)
    assert admitted["bindings"]["ask0000"]["model_reservation"]["output_tokens"] == 16
    edge["model_budget"]["max_output_tokens"] = 15
    with pytest.raises(core.LabError, match="cannot reserve task ask0000"):
        planner.build_plan(one, edge, sources=SOURCES)


def _schedule_respects_resources(plan):
    nodes = {node["id"]: node for node in plan["graph"]}
    by_task = {row["task_id"]: row for row in plan["schedule"]}
    assert set(by_task) == set(nodes)
    for node in plan["graph"]:
        for dep in node["depends_on"]:
            assert by_task[dep]["end_ns"] <= by_task[node["id"]]["start_ns"]
    events = []
    for row in plan["schedule"]:
        events.append((row["start_ns"], 1, row["task_id"]))
        events.append((row["end_ns"], 0, row["task_id"]))
    events.sort(key=lambda item: (item[0], item[1]))
    active = set()
    workers = plan["resources"]["workers"]
    buffer = plan["resources"]["read_buffer_bytes"]
    for _time, kind, task_id in events:
        if kind == 0:
            active.remove(task_id)
            continue
        active.add(task_id)
        assert len(active) <= workers
        reserved = sum(plan["bindings"][task]["buffer_bytes"] for task in active)
        assert reserved <= buffer


def test_large_graph_plans_within_the_time_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    sizes = [len(_completion()), 0, 3, 100, 32768, 32769, 65536, 100000]
    mission = _mission(tmp_path, sizes, mission_id="agent-scale", fixture_at=0)
    structural = len(planner.compile_graph(mission))
    mission = _with_llm(mission, core.MAX_GRAPH_NODES - structural)
    profile = _profile(workers=8, read_buffer_bytes=4_194_304)
    started = time.perf_counter()
    plan = planner.build_plan(mission, profile, sources=SOURCES)
    elapsed = time.perf_counter() - started
    assert len(plan["graph"]) == core.MAX_GRAPH_NODES
    assert plan["planner"] == {"id": "coordinate-list-v2", "version": 2}
    assert plan["enumerated_bindings"] <= 160
    assert elapsed < SCALE_PLAN_SECONDS
    _schedule_respects_resources(plan)
    again = planner.build_plan(mission, profile, sources=SOURCES)
    assert core.canonical(again) == core.canonical(plan)
