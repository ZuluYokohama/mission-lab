"""Bounded candidate enumeration and deterministic resource-aware scheduling."""
from __future__ import annotations

import itertools
import copy
from pathlib import Path

from . import core


def compile_graph(mission):
    if "tasks" in mission:
        graph = mission["tasks"]
    else:
        graph = []
        for item in sorted(mission["inputs"], key=lambda value: value["id"]):
            aid = item["id"]
            for prefix, operation, deps in (
                ("hash", "sha256", []), ("reference", "reference_sha256", []),
                ("verify", "verify_digest", ["hash:" + aid, "reference:" + aid]),
            ):
                graph.append({"id": prefix + ":" + aid, "operation": operation, "input_id": aid,
                              "depends_on": deps, "outputs": ["output:" + prefix + ":" + aid],
                              "state_reads": ["output:" + dep for dep in deps],
                              "state_writes": ["output:" + prefix + ":" + aid]})
        deps = sorted(node["id"] for node in graph if node["operation"] == "verify_digest")
        graph.append({"id": "manifest", "operation": "assemble_manifest", "depends_on": deps,
                      "outputs": ["output:manifest"], "state_reads": ["output:" + dep for dep in deps],
                      "state_writes": ["output:manifest"]})
    core.validate_graph(graph, mission["inputs"])
    return sorted(graph, key=lambda node: node["id"])


def binding(node, candidate, inputs, costs):
    size = inputs[node["input_id"]]["size_bytes"] if "input_id" in node else 0
    if candidate == "whole_v1":
        buffer = max(1, size)
        duration = costs["fixed_task_ns"] + size * costs["whole_ns_per_byte"]
    elif candidate == "stream_v1":
        buffer = max(1, min(size, 32768))
        duration = costs["fixed_task_ns"] + size * costs["stream_ns_per_byte"]
    elif candidate == "reference_v1":
        buffer = max(1, min(size, 8192))
        duration = costs["fixed_task_ns"] + size * costs["reference_ns_per_byte"]
    elif candidate in ("compare_v1", "manifest_v1"):
        buffer, duration = 0, costs["fixed_task_ns"]
    else:
        raise core.LabError("Unknown registered candidate")
    core.integer(duration, 1, 2**63 - 1, "estimated task duration_ns")
    return {"candidate": candidate, "revision": core.REVISIONS[candidate], "buffer_bytes": buffer, "duration_ns": duration}


def critical_path_lengths(graph, bindings):
    nodes = {node["id"]: node for node in graph}
    pending, order = set(nodes), []
    while pending:
        ready = sorted(nid for nid in pending if set(nodes[nid]["depends_on"]) <= set(order))
        if not ready:
            raise core.LabError("Invalid scheduling dependencies")
        order.extend(ready)
        pending.difference_update(ready)
    tails = {}
    for nid in reversed(order):
        children = [child for child in nodes if nid in nodes[child]["depends_on"]]
        tails[nid] = bindings[nid]["duration_ns"] + max((tails[child] for child in children), default=0)
    return tails


def construct_schedule(graph, bindings, resources):
    tails = critical_path_lengths(graph, bindings)
    nodes = {node["id"]: node for node in graph}
    pending, completed, active, schedule = set(nodes), set(), [], []
    now = 0
    while pending or active:
        while len(active) < resources["workers"]:
            reserved = sum(bindings[nid]["buffer_bytes"] for _, nid in active)
            ready = sorted((nid for nid in pending if set(nodes[nid]["depends_on"]) <= completed),
                           key=lambda nid: (-tails[nid], nid))
            feasible = [nid for nid in ready if reserved + bindings[nid]["buffer_bytes"] <= resources["read_buffer_bytes"]]
            if not feasible:
                break
            nid = feasible[0]
            end = now + bindings[nid]["duration_ns"]
            schedule.append({"task_id": nid, "start_ns": now, "end_ns": end})
            active.append((end, nid))
            pending.remove(nid)
        if not active:
            raise core.LabError("No feasible schedule under the declared resource profile")
        now = min(end for end, _ in active)
        finished = {nid for end, nid in active if end == now}
        completed.update(finished)
        active = [(end, nid) for end, nid in active if end != now]
    return sorted(schedule, key=lambda row: (row["start_ns"], row["task_id"])), now


def build_plan(mission, resources, *, sources=None, check_files=True):
    mission = core.validate_mission(mission, check_files=check_files)
    resources = core.validate_resources(resources)
    if mission["unresolved"]:
        return {"schema_version": 1, "status": "needs_clarification", "mission_id": mission["mission_id"],
                "questions": mission["unresolved"], "machine_validation": True, "human_review": False}
    graph = compile_graph(mission)
    inputs = {item["id"]: item for item in mission["inputs"]}
    choices = []
    options = {"sha256": ("whole_v1", "stream_v1"), "reference_sha256": ("reference_v1",),
               "verify_digest": ("compare_v1",), "assemble_manifest": ("manifest_v1",)}
    for node in graph:
        candidates = [binding(node, candidate, inputs, resources["cost_model"]) for candidate in options[node["operation"]]]
        candidates = [candidate for candidate in candidates if candidate["buffer_bytes"] <= resources["read_buffer_bytes"]]
        if not candidates:
            raise core.LabError("No supported candidate fits task " + node["id"])
        choices.append(candidates)
    best = None
    enumerated = 0
    for selection in itertools.product(*choices):
        enumerated += 1
        bindings = {node["id"]: candidate for node, candidate in zip(graph, selection)}
        schedule, makespan = construct_schedule(graph, bindings, resources)
        identity = tuple(bindings[node["id"]]["candidate"] for node in graph)
        total_work = sum(candidate["duration_ns"] for candidate in bindings.values())
        if best is None or (makespan, total_work, identity) < best[0]:
            best = ((makespan, total_work, identity), bindings, schedule)
    obligations = [{"task_id": node["id"], "requirement_ids": [r["id"] for r in mission["requirements"]],
                    "checks": ["registered operation and revision", "declared resource reservations", "dependency and ownership"],
                    "requirement_semantics": "prose retained for human alignment; not automatically assessed",
                    "functional_check": "exact hash and byte count against declared input and sequential reference"}
                   for node in graph]
    plan = {"schema_version": 1, "mission": mission, "mission_identity": core.digest_object(mission),
            "graph": graph, "resources": resources, "bindings": best[1], "schedule": best[2],
            "estimated_makespan_ns": best[0][0], "enumerated_bindings": enumerated,
            "schedule_algorithm": "bounded enumeration; critical-path-first feasible list scheduling; makespan then total-work then candidate-ID tie break",
            "objective": "lowest estimated completion time among constructed feasible schedules",
            "sources": sources if sources is not None else core.source_manifest(), "obligations": obligations,
            "operator_contracts": copy.deepcopy(core.OPERATOR_CONTRACTS),
            "machine_validation": True, "human_review": False}
    return core.seal_plan(plan)


def validate_plan_record(plan):
    core.fields(plan, ("schema_version", "mission", "mission_identity", "graph", "resources", "bindings", "schedule",
                       "estimated_makespan_ns", "enumerated_bindings", "schedule_algorithm", "objective", "sources",
                       "obligations", "operator_contracts", "machine_validation", "human_review", "plan_id"), ("revision",), "plan")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise core.LabError("Unsupported plan schema")
    if plan["human_review"] is not False or plan["machine_validation"] is not True:
        raise core.LabError("Machine validation cannot impersonate human review")
    core.validate_source_records(plan["sources"])
    expected = build_plan(plan["mission"], plan["resources"], sources=plan["sources"], check_files=False)
    if "plan_id" not in expected:
        raise core.LabError("Unresolved mission cannot have an executable plan")
    for key, value in expected.items():
        if key != "plan_id" and plan.get(key) != value:
            raise core.LabError("Plan contract mismatch: " + key)
    if plan["plan_id"] != core.seal_plan(plan)["plan_id"]:
        raise core.LabError("Plan identity mismatch")
    if "revision" in plan:
        from .replan import validate_revision
        validate_revision(plan["revision"], plan["graph"])
    return plan


def architecture(graph):
    lines = ["flowchart TD"]
    labels = {node["id"]: "N" + str(index) for index, node in enumerate(graph)}
    for node in graph:
        lines.append(f'    {labels[node["id"]]}["{node["id"]}: {node["operation"]}"]')
    for node in graph:
        for dep in node["depends_on"]:
            lines.append(f'    {labels[dep]} --> {labels[node["id"]]}')
    return "\n".join(lines) + "\n"


def write_plan(plan, output):
    core.write_json(output / "plan.json", plan)
    if "plan_id" in plan:
        for name, value in (("mission", plan["mission"]), ("graph", plan["graph"]), ("resources", plan["resources"]),
                            ("obligations", plan["obligations"]), ("operator_contracts", plan["operator_contracts"]), ("sources", plan["sources"])):
            core.write_json(output / (name + ".json"), value)
        (output / "architecture.mmd").write_text(architecture(plan["graph"]), encoding="utf-8", newline="\n")


def plan_to_output(mission_path, resources_path, out):
    plan = build_plan(core.load_mission(mission_path), core.read_json(resources_path))
    output = core.new_output(out)
    write_plan(plan, output)
    return output
