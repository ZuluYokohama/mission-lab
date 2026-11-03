"""Deterministic resource-aware scheduling.

Planner v1 enumerates every admitted candidate combination. Planner v2, the
default, keeps that list scheduler and those admission checks, and searches
candidate bindings with a bounded coordinate walk. v2 is polynomial in the
node count and in the number of multi-candidate nodes. It can select a
different valid binding than v1; see CONTRACT.md. A plan records which
planner produced it. Replay of a v1 plan still rebuilds that plan with v1.
"""
from __future__ import annotations

import itertools
import copy
from collections import deque

from . import core
from .budget import add_cost, exceeds, reservation_for, reservation_numbers, zero_cost
from .operators import required_buffer


PLANNER_V1_ALGORITHM = (
    "bounded enumeration; critical-path-first feasible list scheduling; "
    "makespan then total-work then candidate-ID tie break")
PLANNER_V1_OBJECTIVE = "lowest estimated completion time among constructed feasible schedules"
PLANNER_V2_ID = "coordinate-list-v2"
PLANNER_V2_VERSION = 2
PLANNER_V2_ALGORITHM = (
    "coordinate-list-v2; critical-path-first feasible list scheduling; "
    "multi-seed single- and pairwise-candidate coordinate search; "
    "makespan then total-work then candidate-ID tie break")
PLANNER_V2_OBJECTIVE = (
    "lowest estimated completion time among the feasible schedules this coordinate search constructs; "
    "not an enumeration of every binding combination")
# v1 shipped with this graph cap. Replay refuses to enumerate above it.
V1_MAX_GRAPH_NODES = 64
# Improving passes per seed. A longer chain of improvements is not explored.
_MAX_PASSES = 8
_BUDGET_SCHEDULE = "No feasible schedule under the declared model budget"
_OPTIONS = {
    "sha256": ("whole_v1", "stream_v1"),
    "reference_sha256": ("reference_v1",),
    "verify_digest": ("compare_v1",),
    "assemble_manifest": ("manifest_v1",),
    "llm:grok": ("chat_v1",),
    "gate": ("predicate_v1",),
}
_PLAN_FIELDS = (
    "schema_version", "mission", "mission_identity", "graph", "resources", "bindings", "schedule",
    "estimated_makespan_ns", "enumerated_bindings", "schedule_algorithm", "objective", "sources",
    "obligations", "operator_contracts", "machine_validation", "human_review", "plan_id",
)


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


def binding(node, candidate, inputs, costs, resources):
    size = inputs[node["input_id"]]["size_bytes"] if "input_id" in node else 0
    if candidate == "whole_v1":
        duration = costs["fixed_task_ns"] + size * costs["whole_ns_per_byte"]
    elif candidate == "stream_v1":
        duration = costs["fixed_task_ns"] + size * costs["stream_ns_per_byte"]
    elif candidate == "reference_v1":
        duration = costs["fixed_task_ns"] + size * costs["reference_ns_per_byte"]
    elif candidate in ("compare_v1", "manifest_v1", "chat_v1", "predicate_v1"):
        duration = costs["fixed_task_ns"]
    else:
        raise core.LabError("Unknown registered candidate")
    buffer = required_buffer(node["operation"], candidate, inputs.get(node.get("input_id")), node=node, inputs=inputs)
    core.integer(duration, 1, 2**63 - 1, "estimated task duration_ns")
    record = {"candidate": candidate, "revision": core.REVISIONS[candidate], "buffer_bytes": buffer, "duration_ns": duration}
    reservation = reservation_for(node, inputs, resources)
    if reservation is not None:
        record["model_reservation"] = reservation
    return record


def _dependency_index(graph):
    nodes = {node["id"]: node for node in graph}
    children = {nid: [] for nid in nodes}
    indegree = {nid: 0 for nid in nodes}
    for node in graph:
        deps = node["depends_on"]
        indegree[node["id"]] = len(deps)
        for dep in deps:
            children[dep].append(node["id"])
    return nodes, children, indegree


def critical_path_lengths(graph, bindings):
    _nodes, children, indegree = _dependency_index(graph)
    queue = deque(sorted(nid for nid, count in indegree.items() if count == 0))
    order = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for child in children[nid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(graph):
        raise core.LabError("Invalid scheduling dependencies")
    tails = {}
    for nid in reversed(order):
        longest = 0
        for child in children[nid]:
            if tails[child] > longest:
                longest = tails[child]
        tails[nid] = bindings[nid]["duration_ns"] + longest
    return tails


def construct_schedule(graph, bindings, resources):
    """Critical-path-first list schedule.

    Ready tasks are ordered by descending remaining critical-path duration, then
    task id. A start needs a free worker slot, room in the aggregate read-buffer
    reservation, and room in the running sum of model reservations. The sum is
    not released when a task finishes. Equality with a cap still fits.
    """
    tails = critical_path_lengths(graph, bindings)
    _nodes, children, indegree = _dependency_index(graph)
    limits = resources.get("model_budget")
    spent = zero_cost()
    buffer_cap = resources["read_buffer_bytes"]
    workers = resources["workers"]

    def budget_ok(task_id):
        if limits is None:
            return True
        projected = add_cost(spent, reservation_numbers(bindings[task_id]["model_reservation"]))
        return not exceeds(projected, limits)

    def priority(nid):
        return (-tails[nid], nid)

    ready = sorted((nid for nid, count in indegree.items() if count == 0), key=priority)
    unstarted = len(graph)
    active = []
    schedule = []
    now = 0
    while unstarted or active:
        while len(active) < workers and ready:
            reserved = 0
            for _end, nid in active:
                reserved += bindings[nid]["buffer_bytes"]
            choice = None
            any_budget = False
            for index, nid in enumerate(ready):
                if budget_ok(nid):
                    any_budget = True
                    if reserved + bindings[nid]["buffer_bytes"] <= buffer_cap:
                        choice = index
                        break
            if choice is None:
                if limits is not None and ready and not any_budget:
                    raise core.LabError(_BUDGET_SCHEDULE)
                break
            nid = ready.pop(choice)
            if limits is not None:
                spent = add_cost(spent, reservation_numbers(bindings[nid]["model_reservation"]))
            end = now + bindings[nid]["duration_ns"]
            schedule.append({"task_id": nid, "start_ns": now, "end_ns": end})
            active.append((end, nid))
            unstarted -= 1
        if not active:
            raise core.LabError("No feasible schedule under the declared resource profile")
        now = min(end for end, _nid in active)
        finished = [nid for end, nid in active if end == now]
        active = [(end, nid) for end, nid in active if end != now]
        newcomers = []
        for nid in finished:
            for child in children[nid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    newcomers.append(child)
        if newcomers:
            ready.extend(newcomers)
            ready.sort(key=priority)
    return sorted(schedule, key=lambda row: (row["start_ns"], row["task_id"])), now


def _clarification(mission):
    return {"schema_version": 1, "status": "needs_clarification", "mission_id": mission["mission_id"],
            "questions": mission["unresolved"], "machine_validation": True, "human_review": False}


def _admitted_choices(graph, inputs, resources):
    choices = []
    for node in graph:
        candidates = [binding(node, candidate, inputs, resources["cost_model"], resources)
                      for candidate in _OPTIONS[node["operation"]]]
        candidates = [candidate for candidate in candidates if candidate["buffer_bytes"] <= resources["read_buffer_bytes"]]
        if not candidates:
            raise core.LabError("No supported candidate fits task " + node["id"])
        if "model_budget" in resources:
            affordable = [candidate for candidate in candidates
                          if not exceeds(reservation_numbers(candidate["model_reservation"]), resources["model_budget"])]
            if not affordable:
                raise core.LabError("Model budget cannot reserve task " + node["id"])
            candidates = affordable
        choices.append(candidates)
    return choices


def _identity(selection):
    return tuple(candidate["candidate"] for candidate in selection)


def _bindings_for(graph, selection):
    return {node["id"]: candidate for node, candidate in zip(graph, selection)}


def _score(graph, selection, resources):
    bindings = _bindings_for(graph, selection)
    schedule, makespan = construct_schedule(graph, bindings, resources)
    total_work = 0
    for candidate in selection:
        total_work += candidate["duration_ns"]
    return (makespan, total_work, _identity(selection)), bindings, schedule


def _enumerate_bindings(graph, choices, resources):
    best = None
    enumerated = 0
    for selection in itertools.product(*choices):
        enumerated += 1
        found = _score(graph, selection, resources)
        if best is None or found[0] < best[0]:
            best = found
    return best, enumerated


def _seed_selections(choices):
    orders = (
        lambda candidate: (candidate["duration_ns"], candidate["buffer_bytes"], candidate["candidate"]),
        lambda candidate: (candidate["buffer_bytes"], candidate["duration_ns"], candidate["candidate"]),
        lambda candidate: (candidate["candidate"],),
    )
    seeds = []
    seen = set()
    for key in orders:
        selection = [min(candidates, key=key) for candidates in choices]
        identity = _identity(selection)
        if identity not in seen:
            seen.add(identity)
            seeds.append(selection)
    return seeds


def _neighborhood(indexes):
    groups = [(index,) for index in indexes]
    for left in range(len(indexes)):
        for right in range(left + 1, len(indexes)):
            groups.append((indexes[left], indexes[right]))
    return groups


def _coordinate_bindings(graph, choices, resources):
    """Polynomial search over admitted candidates.

    Three seeds, then single-candidate and pairwise changes. A seed keeps a
    change only when it is strictly better than that seed's current key. The
    recorded plan is the best key among those walks, so a later seed may step
    through a key that is worse than an earlier seed. At most eight improving
    passes run per seed. Schedule results are reused by candidate-id tuple, so
    the number of list schedules built is at most the number of distinct tuples
    visited, which this walk bounds by the seeds, the pass cap, and the
    quadratic neighborhood.
    """
    indexes = [index for index, candidates in enumerate(choices) if len(candidates) > 1]
    groups = _neighborhood(indexes)
    cache = {}
    evaluated = 0
    best = None
    first_error = None

    def consider(selection):
        nonlocal evaluated
        identity = _identity(selection)
        if identity in cache:
            return cache[identity]
        evaluated += 1
        try:
            found = _score(graph, selection, resources)
        except core.LabError as exc:
            if str(exc) == _BUDGET_SCHEDULE:
                raise
            cache[identity] = exc
            return exc
        cache[identity] = found
        return found

    for seed in _seed_selections(choices):
        current = consider(seed)
        if isinstance(current, core.LabError):
            if first_error is None:
                first_error = current
            continue
        if best is None or current[0] < best[0]:
            best = current
        selection = list(seed)
        current_key = current[0]
        for _pass in range(_MAX_PASSES):
            improved = False
            for group in groups:
                options = [choices[index] for index in group]
                for combo in itertools.product(*options):
                    if all(combo[offset] == selection[group[offset]] for offset in range(len(group))):
                        continue
                    trial = list(selection)
                    for offset, candidate in enumerate(combo):
                        trial[group[offset]] = candidate
                    found = consider(trial)
                    if isinstance(found, core.LabError):
                        if first_error is None:
                            first_error = found
                        continue
                    if found[0] < current_key:
                        current_key = found[0]
                        selection = trial
                        improved = True
                        if best is None or found[0] < best[0]:
                            best = found
                        break
                if improved:
                    break
            if not improved:
                break
    if best is None:
        if first_error is not None:
            raise first_error
        raise core.LabError("No feasible schedule under the declared resource profile")
    return best, evaluated


def _functional_check(operation):
    if operation == "llm:grok":
        return ("recorded request and response hashes, HTTP status, reported token usage, and content artifact; "
                "a declared model budget settles its reservation against that usage; no model-quality claim")
    if operation == "gate":
        return ("declared predicate over dependency receipts; outcome is pass, fail, indeterminate, or pending; "
                "no arbitrary code and no model-quality claim")
    return "exact hash and byte count against declared input and sequential reference"


def _obligations(mission, graph):
    return [{"task_id": node["id"], "requirement_ids": [requirement["id"] for requirement in mission["requirements"]],
             "checks": ["registered operation and revision", "declared resource reservations", "dependency and ownership"],
             "requirement_semantics": "prose retained for human alignment; not automatically assessed",
             "functional_check": _functional_check(node["operation"])}
            for node in graph]


def build_plan(mission, resources, *, sources=None, check_files=True, version=2):
    if version not in (1, 2):
        raise core.LabError("Unsupported planner")
    mission = core.validate_mission(mission, check_files=check_files)
    resources = core.validate_resources(resources)
    # Unresolved missions are not executable. Compile only when grants must be
    # checked against a graph; validate_mission already checked explicit tasks.
    if mission["unresolved"] and "worker_env" not in resources:
        return _clarification(mission)
    graph = compile_graph(mission)
    core.check_worker_env_tasks(resources, [node["id"] for node in graph])
    from .grok import check_llm_plan
    inputs = {item["id"]: item for item in mission["inputs"]}
    check_llm_plan(resources, graph, inputs, read_files=check_files)
    if mission["unresolved"]:
        return _clarification(mission)
    if version == 1 and len(graph) > V1_MAX_GRAPH_NODES:
        raise core.LabError("Planner v1 supports graphs of at most 64 nodes")
    choices = _admitted_choices(graph, inputs, resources)
    if version == 1:
        best, enumerated = _enumerate_bindings(graph, choices, resources)
        algorithm = PLANNER_V1_ALGORITHM
        objective = PLANNER_V1_OBJECTIVE
    else:
        best, enumerated = _coordinate_bindings(graph, choices, resources)
        algorithm = PLANNER_V2_ALGORITHM
        objective = PLANNER_V2_OBJECTIVE
    plan = {"schema_version": 1, "mission": mission, "mission_identity": core.digest_object(mission),
            "graph": graph, "resources": resources, "bindings": best[1], "schedule": best[2],
            "estimated_makespan_ns": best[0][0], "enumerated_bindings": enumerated,
            "schedule_algorithm": algorithm,
            "objective": objective,
            "sources": sources if sources is not None else core.source_manifest(), "obligations": _obligations(mission, graph),
            "operator_contracts": copy.deepcopy(core.OPERATOR_CONTRACTS),
            "machine_validation": True, "human_review": False}
    if version == 2:
        plan["planner"] = {"id": PLANNER_V2_ID, "version": PLANNER_V2_VERSION}
    return core.seal_plan(plan)


def _planner_version(plan):
    if "planner" not in plan:
        return 1
    stamp = plan["planner"]
    core.fields(stamp, ("id", "version"), label="planner")
    if stamp["id"] != PLANNER_V2_ID or type(stamp["version"]) is not int or stamp["version"] != PLANNER_V2_VERSION:
        raise core.LabError("Unsupported planner")
    return 2


def validate_plan_record(plan):
    if not isinstance(plan, dict):
        raise core.LabError("plan must be an object")
    version = _planner_version(plan)
    required = _PLAN_FIELDS + (("planner",) if version == 2 else ())
    core.fields(plan, required, ("revision",), "plan")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise core.LabError("Unsupported plan schema")
    if plan["human_review"] is not False or plan["machine_validation"] is not True:
        raise core.LabError("Machine validation cannot impersonate human review")
    core.validate_source_records(plan["sources"])
    expected = build_plan(plan["mission"], plan["resources"], sources=plan["sources"], check_files=False, version=version)
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
