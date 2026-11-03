"""Explicit revisions, scoped invalidation, and retained applicability."""
from . import core
from .planner import build_plan, validate_plan_record, write_plan


def validate_trigger(trigger):
    core.fields(trigger, ("schema_version", "kind", "text", "evidence_origin"), ("evidence_refs",), "revision trigger")
    if type(trigger["schema_version"]) is not int or trigger["schema_version"] != 1:
        raise core.LabError("Unsupported trigger schema")
    if trigger["kind"] not in ("resource_change", "requirement_change", "gate_evidence"):
        raise core.LabError("Unsupported revision trigger")
    if trigger["kind"] == "gate_evidence" and not trigger.get("evidence_refs"):
        raise core.LabError("Gate evidence requires evidence_refs")
    if trigger["evidence_origin"] not in ("injected_demo", "observed", "human_supplied"):
        raise core.LabError("Trigger origin is required")
    if not isinstance(trigger["text"], str) or not trigger["text"].strip():
        raise core.LabError("Revision trigger text is required")
    core.strings(trigger.get("evidence_refs", []), "trigger evidence_refs")


def validate_revision(revision, graph):
    core.fields(revision, ("schema_version", "prior_plan_id", "trigger", "changed_tasks", "affected_tasks",
                           "reusable_functional_results", "reuse_note", "invalidated_measurement_scope", "phase"), label="revision")
    if type(revision["schema_version"]) is not int or revision["schema_version"] != 1:
        raise core.LabError("Unsupported revision schema")
    if not isinstance(revision["prior_plan_id"], str) or not core.SHA256.fullmatch(revision["prior_plan_id"]):
        raise core.LabError("Malformed prior plan identity")
    validate_trigger(revision["trigger"])
    known = {node["id"] for node in graph}
    for key in ("changed_tasks", "affected_tasks", "reusable_functional_results"):
        core.strings(revision[key], key)
        if len(set(revision[key])) != len(revision[key]) or not set(revision[key]) <= known:
            raise core.LabError("Invalid revision task references")
    affected = set(revision["changed_tasks"])
    while True:
        expanded = affected | {node["id"] for node in graph if set(node["depends_on"]) & affected}
        if expanded == affected:
            break
        affected = expanded
    if set(revision["affected_tasks"]) != affected or set(revision["reusable_functional_results"]) != known - affected:
        raise core.LabError("Revision invalidation/reuse is inconsistent with the dependency closure")
    if revision["phase"] != "between execution batches" or revision["invalidated_measurement_scope"] != "all schedule and performance observations":
        raise core.LabError("Unsupported revision phase or measurement reuse")
    if not isinstance(revision["reuse_note"], str) or not revision["reuse_note"]:
        raise core.LabError("Revision reuse scope is required")
    return revision


def gate_evidence_trigger(semantic, text=None):
    """Build a revision trigger from gate receipts that did not pass.

    The text is evidence for a later plan. It does not change the prior run.
    """
    if not isinstance(semantic, dict) or not isinstance(semantic.get("tasks"), list):
        raise core.LabError("Gate evidence requires task receipts")
    refs, details = [], []
    for task in semantic["tasks"]:
        output = task.get("output") if isinstance(task, dict) else None
        if not isinstance(output, dict) or output.get("outcome") not in ("fail", "indeterminate", "pending"):
            continue
        refs.append(task["task_id"])
        reason = output.get("reason") if isinstance(output.get("reason"), str) else ""
        details.append(task["task_id"] + " " + output["outcome"] + ": " + reason)
    if not refs:
        raise core.LabError("Gate evidence requires a gate that did not pass")
    if text is None:
        text = " ".join(details)[:1000]
    if not isinstance(text, str) or not text.strip():
        raise core.LabError("Revision trigger text is required")
    if len(text) > 1000:
        raise core.LabError("Revision trigger text exceeds 1000 characters")
    return {"schema_version": 1, "kind": "gate_evidence", "text": text.strip(),
            "evidence_origin": "observed", "evidence_refs": sorted(refs)}


def replan_data(prior, resources, trigger, *, mission=None):
    validate_plan_record(prior)
    validate_trigger(trigger)
    if trigger["kind"] == "resource_change" and mission is not None and mission != prior["mission"]:
        raise core.LabError("A resource revision cannot change requirements")
    if trigger["kind"] == "requirement_change" and mission is None:
        raise core.LabError("Requirement revisions require a new mission")
    if trigger["kind"] == "gate_evidence":
        prior_gates = {node["id"] for node in prior["graph"] if isinstance(node, dict) and node.get("operation") == "gate"}
        refs = trigger.get("evidence_refs") or []
        if not refs or any(ref not in prior_gates for ref in refs):
            raise core.LabError("Gate evidence must name a gate in the prior plan")
    updated = build_plan(mission or prior["mission"], resources)
    if "plan_id" not in updated:
        return {**updated, "prior_plan_id": prior["plan_id"], "trigger": trigger}
    changed = {nid for nid in set(updated["bindings"])
               if prior["bindings"].get(nid) != updated["bindings"].get(nid)}
    if updated["mission_identity"] != prior["mission_identity"] or updated["sources"] != prior["sources"]:
        changed.update(node["id"] for node in updated["graph"])
    affected = set(changed)
    while True:
        expanded = affected | {node["id"] for node in updated["graph"] if set(node["depends_on"]) & affected}
        if expanded == affected:
            break
        affected = expanded
    reusable = sorted(node["id"] for node in updated["graph"] if node["id"] not in affected)
    updated["revision"] = {"schema_version": 1, "prior_plan_id": prior["plan_id"], "trigger": trigger,
                           "changed_tasks": sorted(changed), "affected_tasks": sorted(affected),
                           "reusable_functional_results": reusable,
                           "reuse_note": "Eligibility only; recorded result identities must match. Timing/resource evidence is plan-specific.",
                           "invalidated_measurement_scope": "all schedule and performance observations",
                           "phase": "between execution batches"}
    return core.seal_plan(updated)


def replan_to_output(plan_path, resources_path, trigger_path, out):
    trigger = core.read_json(trigger_path)
    # Requirement change includes its new mission in a separate declared reference.
    mission = None
    if isinstance(trigger, dict) and "mission_path" in trigger:
        trigger = dict(trigger)
        mission = core.load_mission(trigger.pop("mission_path"))
    updated = replan_data(core.load_plan(plan_path), core.read_json(resources_path), trigger, mission=mission)
    output = core.new_output(out)
    write_plan(updated, output)
    return output
