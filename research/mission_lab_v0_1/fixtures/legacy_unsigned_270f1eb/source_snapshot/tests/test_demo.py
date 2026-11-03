"""A bottlenecked reference must not cause gratuitously slower bindings."""
import hashlib
import copy
import pytest

from research.mission_lab_v0_1 import core, planner, replan


def test_completion_tie_prefers_less_work_and_budget_revision_changes_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    content = bytes(range(256)) * 512 + b"!"
    path = tmp_path / "pattern.bin"
    path.write_bytes(content)
    mission = {"schema_version": 1, "mission_id": "tie-regression", "requirements": [
        {"id": "exact", "text": "Preserve exact digest and byte count.", "acceptance_criteria": ["Exact equality"], "evidence_refs": []}],
        "inputs": [{"id": "pattern", "path": str(path), "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}], "unresolved": []}
    baseline = planner.build_plan(mission, core.default_resources())
    whole = baseline["bindings"]["hash:pattern"]
    assert whole["candidate"] == "whole_v1"
    updated = replan.replan_data(baseline, {**baseline["resources"], "read_buffer_bytes": 65536},
        {"schema_version": 1, "kind": "resource_change", "text": "Injected buffer change", "evidence_origin": "injected_demo"})
    assert updated["bindings"]["hash:pattern"]["candidate"] == "stream_v1"
    assert updated["mission_identity"] == baseline["mission_identity"]
    assert updated["revision"]["affected_tasks"] == ["hash:pattern", "manifest", "verify:pattern"]


def test_plan_contract_metadata_cannot_mutate_the_registered_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    mission = {"schema_version": 1, "mission_id": "contract-alias", "requirements": [
        {"id": "exact", "text": "Exact audit", "acceptance_criteria": ["Exact equality"], "evidence_refs": []}],
        "inputs": [{"id": "empty", "path": str(path), "size_bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}], "unresolved": []}
    trusted = copy.deepcopy(core.OPERATOR_CONTRACTS)
    plan = planner.build_plan(mission, core.default_resources())
    plan["operator_contracts"]["sha256"]["postconditions"] = ["Unverified substitute"]
    assert core.OPERATOR_CONTRACTS == trusted
    with pytest.raises(core.LabError, match="operator_contracts"):
        planner.validate_plan_record(core.seal_plan(plan))
