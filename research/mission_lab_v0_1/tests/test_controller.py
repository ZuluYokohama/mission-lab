"""Independent contracts for the local mission controller.

Expected digests and graph fixtures are fixed independently of the planner.
No generated assessment is substituted for a human review.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from research.mission_lab_v0_1 import core


ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture
def mission(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    path = tmp_path / "input.bin"
    path.write_bytes(b"abc")
    return {
        "schema_version": 1,
        "mission_id": "fixed-digest-contract",
        "requirements": [{
            "id": "R1",
            "text": "Preserve the declared input identity in a verified manifest.",
            "acceptance_criteria": ["The recorded digest and size equal the fixed input fixture."],
            "evidence_refs": [],
        }],
        "inputs": [{"id": "sample", "path": str(path), "size_bytes": 3, "sha256": ABC_SHA256}],
        "unresolved": [],
    }


def graph():
    return [
        {"id": "hash:sample", "operation": "sha256", "input_id": "sample",
         "depends_on": [], "outputs": ["digest:sample"], "state_reads": [], "state_writes": ["digest:sample"]},
        {"id": "reference:sample", "operation": "reference_sha256", "input_id": "sample",
         "depends_on": [], "outputs": ["reference-digest:sample"], "state_reads": [], "state_writes": ["reference-digest:sample"]},
        {"id": "verify:sample", "operation": "verify_digest", "input_id": "sample",
         "depends_on": ["hash:sample", "reference:sample"], "outputs": ["verified:sample"],
         "state_reads": ["digest:sample", "reference-digest:sample"], "state_writes": ["verified:sample"]},
        {"id": "manifest", "operation": "assemble_manifest", "depends_on": ["verify:sample"],
         "outputs": ["manifest"], "state_reads": ["verified:sample"], "state_writes": ["manifest"]},
    ]


def resources():
    return {"schema_version": 1, "workers": 2, "read_buffer_bytes": 65536,
            "timeout_seconds": 5, "estimate_origin": "injected_fixture"}


def test_valid_mission_keeps_recorded_identity(mission):
    actual = core.validate_mission(mission)
    assert actual["inputs"][0]["sha256"] == ABC_SHA256
    assert actual["inputs"][0]["size_bytes"] == 3
    assert Path(actual["inputs"][0]["path"]).read_bytes() == b"abc"


@pytest.mark.parametrize("payload,expected", [
    (b"", EMPTY_SHA256),
    (b"abc", ABC_SHA256),
    (b"a" * 8191, "d691798dcd3bab9099e84b4cbc484caef67262d1ef5dca83330d07f5ce4740dd"),
    (b"a" * 8192, "dd4e6730520932767ec0a9e33fe19c4ce24399d6eba4ff62f13013c9ed30ef87"),
    (b"a" * 8193, "9c10c48d1f1d6618db88fde2c25409181c9201ed34ec6815d62bcf57c10d177b"),
    (b"a" * 16385, "608b801060d579a7b659a01755259a13dec8853b97d3d8062763c97ffac6ddae"),
    (bytes(range(256)) * 33 + b"\x00end", "0396fed26570d7f14b2cf41074d84da3880afeaf3eb2a39ba2fa706d81627638"),
])
def test_file_hash_fixed_vectors_and_chunk_boundaries(tmp_path, payload, expected):
    path = tmp_path / "bytes.bin"
    path.write_bytes(payload)
    assert core.file_hash(path) == expected


@pytest.mark.parametrize("text", [
    '{"id":"a","id":"b"}',
    '{"outer":{"x":1,"x":2}}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
    '{"unterminated":',
])
def test_json_rejects_ambiguous_or_nonfinite_records(tmp_path, text):
    path = tmp_path / "record.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(core.LabError):
        core.read_json(path)


def test_json_rejects_invalid_utf8(tmp_path):
    path = tmp_path / "record.json"
    path.write_bytes(b"\xff")
    with pytest.raises(core.LabError):
        core.read_json(path)


def test_canonical_identity_is_order_independent_and_preserves_unicode():
    assert core.canonical({"b": 2, "a": "\u03b1"}) == '{\n  "a": "\u03b1",\n  "b": 2\n}\n'
    assert core.digest_object({"b": 2, "a": 1}) == core.digest_object({"a": 1, "b": 2})
    assert core.digest_object({"a": 1}) != core.digest_object({"a": True})


def test_write_json_never_overwrites_prior_evidence(tmp_path):
    path = tmp_path / "record.json"
    core.write_json(path, {"value": "first"})
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        core.write_json(path, {"value": "second"})
    assert path.read_bytes() == before


@pytest.mark.parametrize("location", ["mission", "requirement", "input", "unresolved"])
def test_unknown_fields_are_rejected_at_each_mission_boundary(mission, location):
    draft = copy.deepcopy(mission)
    if location == "mission":
        target = draft
    elif location == "requirement":
        target = draft["requirements"][0]
    elif location == "input":
        target = draft["inputs"][0]
    else:
        target = {"id": "gap", "question": "Which criterion applies?", "affects": "acceptance"}
        draft["unresolved"].append(target)
    target["command"] = "untrusted code is data"
    with pytest.raises(core.LabError, match="unknown"):
        core.validate_mission(draft)


@pytest.mark.parametrize("field", ["schema_version", "mission_id", "requirements", "inputs", "unresolved"])
def test_missing_mission_fields_are_rejected(mission, field):
    draft = copy.deepcopy(mission)
    del draft[field]
    with pytest.raises(core.LabError):
        core.validate_mission(draft)


@pytest.mark.parametrize("bad_version", [True, "1", 1.0, 0, 2, None])
def test_mission_schema_version_is_exact(mission, bad_version):
    mission["schema_version"] = bad_version
    with pytest.raises(core.LabError):
        core.validate_mission(mission)


@pytest.mark.parametrize("bad_size", [True, False, -1, 3.0, "3", 2**40 + 1])
def test_input_size_uses_integer_domain(mission, bad_size):
    mission["inputs"][0]["size_bytes"] = bad_size
    with pytest.raises(core.LabError):
        core.validate_mission(mission)


@pytest.mark.parametrize("bad_digest", ["", "A" * 64, "a" * 63, "g" * 64, None, []])
def test_input_hash_is_exact_lowercase_sha256(mission, bad_digest):
    mission["inputs"][0]["sha256"] = bad_digest
    with pytest.raises(core.LabError):
        core.validate_mission(mission)


def test_changed_same_length_input_cannot_keep_prior_identity(mission):
    Path(mission["inputs"][0]["path"]).write_bytes(b"abd")
    with pytest.raises(core.LabError, match="identity changed"):
        core.validate_mission(mission)


def test_missing_input_cannot_be_assessed_as_present(mission):
    Path(mission["inputs"][0]["path"]).unlink()
    with pytest.raises(core.LabError, match="Missing"):
        core.validate_mission(mission)


@pytest.mark.parametrize("duplicate", ["requirement_id", "input_id", "input_path"])
def test_duplicate_identity_is_rejected(mission, duplicate):
    if duplicate == "requirement_id":
        mission["requirements"].append(copy.deepcopy(mission["requirements"][0]))
    else:
        item = copy.deepcopy(mission["inputs"][0])
        if duplicate == "input_path":
            item["id"] = "other"
        mission["inputs"].append(item)
    with pytest.raises(core.LabError, match="Duplicate"):
        core.validate_mission(mission)


def test_valid_graph_has_deterministic_topological_order(mission):
    expected = ["hash:sample", "reference:sample", "verify:sample", "manifest"]
    assert core.validate_graph(graph(), mission["inputs"]) == expected
    assert core.validate_graph(list(reversed(graph())), mission["inputs"]) == expected


@pytest.mark.parametrize("operation", ["python", "shell", "eval", "import", "__import__", [], {}])
def test_graph_accepts_only_typed_registered_operations(mission, operation):
    nodes = graph()
    nodes[0]["operation"] = operation
    with pytest.raises(core.LabError):
        core.validate_graph(nodes, mission["inputs"])


@pytest.mark.parametrize("field", ["command", "code", "module", "callable", "python", "shell"])
def test_graph_cannot_smuggle_executable_fields(mission, field):
    nodes = graph()
    nodes[0][field] = "untrusted executable proposal"
    with pytest.raises(core.LabError, match="unknown"):
        core.validate_graph(nodes, mission["inputs"])


@pytest.mark.parametrize("case", ["self_cycle", "two_node_cycle", "missing_dependency", "duplicate_dependency"])
def test_graph_dependency_failures_are_explicit(mission, case):
    nodes = graph()
    if case == "self_cycle":
        nodes[2]["depends_on"].append("verify:sample")
    elif case == "two_node_cycle":
        nodes[0]["depends_on"] = ["manifest"]
    elif case == "missing_dependency":
        nodes[2]["depends_on"][0] = "missing"
    else:
        nodes[2]["depends_on"].append("hash:sample")
    with pytest.raises(core.LabError):
        core.validate_graph(nodes, mission["inputs"])


def test_single_writer_and_state_dependency_contracts(mission):
    nodes = graph()
    nodes[1]["outputs"] = ["digest:sample"]
    nodes[1]["state_writes"] = ["digest:sample"]
    with pytest.raises(core.LabError, match="writers"):
        core.validate_graph(nodes, mission["inputs"])
    nodes = graph()
    nodes[2]["state_reads"] = ["digest:sample", "unproduced"]
    with pytest.raises(core.LabError, match="producer"):
        core.validate_graph(nodes, mission["inputs"])


def test_verifier_cannot_compare_different_inputs(mission, tmp_path):
    other = tmp_path / "other.bin"
    other.write_bytes(b"abc")
    mission["inputs"].append({"id": "other", "path": str(other), "size_bytes": 3, "sha256": ABC_SHA256})
    nodes = graph()
    nodes[1]["input_id"] = "other"
    with pytest.raises(core.LabError, match="input mismatch"):
        core.validate_graph(nodes, mission["inputs"])


@pytest.mark.parametrize("field,bad", [
    ("workers", True), ("workers", 0), ("workers", 9),
    ("read_buffer_bytes", False), ("read_buffer_bytes", 0), ("read_buffer_bytes", 1.0),
    ("timeout_seconds", True), ("timeout_seconds", 0), ("timeout_seconds", -1),
    ("timeout_seconds", float("nan")), ("timeout_seconds", float("inf")),
    ("timeout_seconds", 3601), ("schema_version", True),
])
def test_resources_reject_wrong_domains_and_nonfinite_limits(field, bad):
    profile = resources()
    profile[field] = bad
    with pytest.raises(core.LabError):
        core.validate_resources(profile)


def test_resource_profile_cannot_claim_measurement_from_injected_defaults():
    profile = resources()
    profile["estimate_origin"] = "measured"
    with pytest.raises(core.LabError, match="explicit cost_model"):
        core.validate_resources(profile)


def test_resource_and_cost_records_are_strict():
    profile = resources()
    profile["gpu_memory_bytes"] = 1
    with pytest.raises(core.LabError, match="unknown"):
        core.validate_resources(profile)
    profile = resources()
    profile["cost_model"] = {"whole_ns_per_byte": 1, "stream_ns_per_byte": 2,
                             "reference_ns_per_byte": 3, "fixed_task_ns": 1000, "score": 0.95}
    with pytest.raises(core.LabError, match="unknown"):
        core.validate_resources(profile)


def test_fresh_output_can_be_created_only_below_evidence_root(tmp_path):
    evidence = tmp_path / "evidence"
    out = evidence / "run"
    assert core.new_output(out, evidence_root=evidence) == out.resolve()
    assert out.is_dir()
    with pytest.raises(core.LabError, match="fresh directory"):
        core.new_output(out, evidence_root=evidence)


@pytest.mark.parametrize("case", ["outside", "traversal", "root", "existing_directory", "existing_file"])
def test_output_boundary_never_overwrites_or_escapes(tmp_path, case):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    sentinel = evidence / "keep.txt"
    sentinel.write_bytes(b"preserved")
    candidates = {"outside": tmp_path / "outside", "traversal": evidence / "child" / ".." / "run",
                  "root": evidence, "existing_directory": evidence / "existing", "existing_file": sentinel}
    (evidence / "existing").mkdir()
    with pytest.raises(core.LabError):
        core.new_output(candidates[case], evidence_root=evidence)
    assert sentinel.read_bytes() == b"preserved"
    assert not (tmp_path / "outside").exists()
    assert not (evidence / "run").exists()


def test_output_cannot_traverse_linked_directory(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = evidence / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("The environment cannot create the symlink fixture: " + str(exc))
    with pytest.raises(core.LabError):
        core.new_output(alias / "run", evidence_root=evidence)
    assert not (outside / "run").exists()


def test_plan_seal_tracks_requirements_inputs_and_sources(mission):
    original = core.seal_plan({"mission": mission, "sources": {"controller.py": "a" * 64}})
    for changed in ("requirement", "input", "source"):
        draft = copy.deepcopy(original)
        if changed == "requirement":
            draft["mission"]["requirements"][0]["acceptance_criteria"] = ["A different acceptance boundary."]
        elif changed == "input":
            draft["mission"]["inputs"][0]["sha256"] = "b" * 64
        else:
            draft["sources"]["controller.py"] = "c" * 64
        assert core.seal_plan(draft)["plan_id"] != original["plan_id"]
    assert core.seal_plan(original) == original


def test_source_snapshot_detects_added_missing_and_altered_files(tmp_path, monkeypatch):
    parent = tmp_path / "research"
    package = parent / "mission_lab_v0_1"
    package.mkdir(parents=True)
    (parent / "__init__.py").write_bytes(b"abc")
    controlled = package / "controller.py"
    controlled.write_bytes(b"abc")
    (package / "evidence").mkdir()
    (package / "evidence" / "mutable.json").write_bytes(b"not source")
    monkeypatch.setattr(core, "ROOT", package)
    expected = {"controller.py": ABC_SHA256, "@research/__init__.py": ABC_SHA256}
    assert core.source_manifest() == expected
    core.verify_sources(expected)
    controlled.write_bytes(b"abd")
    with pytest.raises(core.LabError, match="Source identity"):
        core.verify_sources(expected)
    controlled.write_bytes(b"abc")
    added = package / "unexpected.py"
    added.write_bytes(b"abc")
    with pytest.raises(core.LabError, match="Source identity"):
        core.verify_sources(expected)
    added.unlink()
    controlled.unlink()
    with pytest.raises(core.LabError, match="Source identity"):
        core.verify_sources(expected)


@pytest.fixture
def planning(mission, monkeypatch):
    from research.mission_lab_v0_1 import planner
    monkeypatch.setattr(core, "source_manifest", lambda: {"controller.py": ABC_SHA256})
    profile = resources()
    profile["cost_model"] = {"whole_ns_per_byte": 1, "stream_ns_per_byte": 10,
                             "reference_ns_per_byte": 1, "fixed_task_ns": 100}
    return planner, mission, profile


def test_plan_compiles_dependencies_and_gives_honest_origin(planning):
    planner, mission, profile = planning
    plan = planner.build_plan(mission, profile)
    assert plan["bindings"]["hash:sample"]["candidate"] == "whole_v1"
    assert plan["estimated_makespan_ns"] == 303
    assert plan["resources"]["estimate_origin"] == "injected_fixture"
    assert plan["machine_validation"] is True
    assert plan["human_review"] is False
    assert {node["id"] for node in plan["graph"]} == {
        "hash:sample", "reference:sample", "verify:sample", "manifest",
    }
    schedule = {row["task_id"]: row for row in plan["schedule"]}
    assert schedule["hash:sample"]["start_ns"] == 0
    assert schedule["reference:sample"]["start_ns"] == 0
    assert schedule["verify:sample"]["start_ns"] == 103
    assert schedule["manifest"]["start_ns"] == 203
    assert planner.validate_plan_record(plan) == plan


def test_dependency_and_resource_boundaries_hold_in_projected_schedule(planning):
    planner, mission, profile = planning
    plan = planner.build_plan(mission, profile)
    scheduled = {row["task_id"]: row for row in plan["schedule"]}
    for node in plan["graph"]:
        for dependency in node["depends_on"]:
            assert scheduled[dependency]["end_ns"] <= scheduled[node["id"]]["start_ns"]
    boundaries = {row["start_ns"] for row in plan["schedule"]}
    for boundary in boundaries:
        active = [row["task_id"] for row in plan["schedule"]
                  if row["start_ns"] <= boundary < row["end_ns"]]
        assert len(active) <= 2
        assert sum(plan["bindings"][task]["buffer_bytes"] for task in active) <= 65536


def test_infeasible_resource_profile_has_no_execution_plan(planning):
    planner, mission, profile = planning
    profile["read_buffer_bytes"] = 2
    with pytest.raises(core.LabError, match="No supported candidate"):
        planner.build_plan(mission, profile)


def test_clarification_stops_compilation_without_fabricating_human_review(planning):
    planner, mission, profile = planning
    mission["unresolved"] = [{"id": "acceptance-gap", "question": "Which output must be preserved?", "affects": "acceptance"}]
    record = planner.build_plan(mission, profile)
    assert record["status"] == "needs_clarification"
    assert record["questions"] == mission["unresolved"]
    assert record["human_review"] is False
    assert "plan_id" not in record
    assert "schedule" not in record


@pytest.mark.parametrize("tamper", ["human_review", "machine_validation", "binding", "schedule", "code"])
def test_resealing_does_not_make_malformed_plan_authoritative(planning, tamper):
    planner, mission, profile = planning
    draft = planner.build_plan(mission, profile)
    if tamper == "human_review":
        draft["human_review"] = True
    elif tamper == "machine_validation":
        draft["machine_validation"] = False
    elif tamper == "binding":
        draft["bindings"]["hash:sample"]["buffer_bytes"] = 0
    elif tamper == "schedule":
        next(row for row in draft["schedule"] if row["task_id"] == "verify:sample")["start_ns"] = 0
    else:
        draft["command"] = "unregistered execution proposal"
    with pytest.raises(core.LabError):
        planner.validate_plan_record(core.seal_plan(draft))


@pytest.mark.parametrize("bad_sources", [{"controller.py": "invalid"}, {"../outside.py": ABC_SHA256}, {"controller.py": True}])
def test_plan_source_manifest_has_strict_hash_and_path_contract(planning, bad_sources):
    planner, mission, profile = planning
    plan = planner.build_plan(mission, profile, sources=bad_sources)
    with pytest.raises(core.LabError):
        planner.validate_plan_record(plan)


def trigger(kind="resource_change"):
    return {"schema_version": 1, "kind": kind, "text": "Declared boundary changed.",
            "evidence_origin": "injected_demo"}


def test_resource_replan_preserves_functional_eligibility_but_not_timing(planning):
    from research.mission_lab_v0_1 import replan
    planner, mission, profile = planning
    prior = planner.build_plan(mission, profile)
    updated_profile = copy.deepcopy(profile)
    updated_profile["workers"] = 1
    updated = replan.replan_data(prior, updated_profile, trigger())
    assert updated["plan_id"] != prior["plan_id"]
    revision = updated["revision"]
    assert revision["prior_plan_id"] == prior["plan_id"]
    assert revision["changed_tasks"] == []
    assert revision["affected_tasks"] == []
    assert revision["reusable_functional_results"] == ["hash:sample", "manifest", "reference:sample", "verify:sample"]
    assert revision["invalidated_measurement_scope"] == "all schedule and performance observations"
    assert prior["resources"]["workers"] == 2
    assert updated["human_review"] is False
    assert planner.validate_plan_record(updated) == updated


def test_replan_invalidates_changed_binding_and_all_descendants(planning):
    from research.mission_lab_v0_1 import replan
    planner, mission, profile = planning
    path = Path(mission["inputs"][0]["path"])
    path.write_bytes(b"a" * 65537)
    mission["inputs"][0].update(size_bytes=65537, sha256="008ffc88d3c96a9f307524eb361e47c5222a887fc45fa0c1fb8d429c5c23b430")
    profile["read_buffer_bytes"] = 100000
    prior = planner.build_plan(mission, profile)
    assert prior["bindings"]["hash:sample"]["candidate"] == "whole_v1"
    updated_profile = copy.deepcopy(profile)
    updated_profile["read_buffer_bytes"] = 32768
    updated = replan.replan_data(prior, updated_profile, trigger())
    assert updated["bindings"]["hash:sample"]["candidate"] == "stream_v1"
    assert updated["revision"]["changed_tasks"] == ["hash:sample"]
    assert updated["revision"]["affected_tasks"] == ["hash:sample", "manifest", "verify:sample"]
    assert updated["revision"]["reusable_functional_results"] == ["reference:sample"]


def test_requirement_revision_changes_identity_and_invalidates_old_results(planning):
    from research.mission_lab_v0_1 import replan
    planner, mission, profile = planning
    prior = planner.build_plan(mission, profile)
    revised_mission = copy.deepcopy(mission)
    revised_mission["requirements"][0]["acceptance_criteria"] = ["Use a newly declared interpretation of acceptance."]
    updated = replan.replan_data(prior, profile, trigger("requirement_change"), mission=revised_mission)
    assert updated["mission_identity"] != prior["mission_identity"]
    assert updated["revision"]["affected_tasks"] == ["hash:sample", "manifest", "reference:sample", "verify:sample"]
    assert updated["revision"]["reusable_functional_results"] == []
    with pytest.raises(core.LabError, match="cannot change requirements"):
        replan.replan_data(prior, profile, trigger(), mission=revised_mission)


def test_revision_record_cannot_forge_reuse_by_resealing(planning):
    from research.mission_lab_v0_1 import replan
    planner, mission, profile = planning
    prior = planner.build_plan(mission, profile)
    revised = copy.deepcopy(mission)
    revised["requirements"][0]["text"] = "A revised requirement."
    updated = replan.replan_data(prior, profile, trigger("requirement_change"), mission=revised)
    updated["revision"]["reusable_functional_results"] = ["hash:sample"]
    with pytest.raises(core.LabError):
        planner.validate_plan_record(core.seal_plan(updated))


def test_revision_record_rejects_untrusted_code_fields(planning):
    from research.mission_lab_v0_1 import replan
    planner, mission, profile = planning
    prior = planner.build_plan(mission, profile)
    updated = replan.replan_data(prior, profile, trigger())
    updated["revision"]["command"] = "untrusted code proposal"
    with pytest.raises(core.LabError):
        planner.validate_plan_record(core.seal_plan(updated))


@pytest.mark.parametrize("change", ["kind", "schema", "origin", "extra"])
def test_revision_trigger_requires_explicit_typed_origin(planning, change):
    from research.mission_lab_v0_1 import replan
    planner, mission, profile = planning
    prior = planner.build_plan(mission, profile)
    event = trigger()
    if change == "kind":
        event["kind"] = "automatic_approval"
    elif change == "schema":
        event["schema_version"] = True
    elif change == "origin":
        event["evidence_origin"] = "human_reviewed"
    else:
        event["command"] = "untrusted code proposal"
    with pytest.raises(core.LabError):
        replan.replan_data(prior, profile, event)


def test_controller_imports_do_not_depend_on_frozen_research():
    script = r'''
import sys
class RefuseFrozenImports:
    def find_spec(self, fullname, path=None, target=None):
        prohibited = ("research.language_coherence", "research.v0_3", "research.v0_4", "research.v0_5", "research.v0_6", "research.common")
        if any(fullname == name or fullname.startswith(name + ".") for name in prohibited):
            raise RuntimeError("Frozen research imported: " + fullname)
        return None
sys.meta_path.insert(0, RefuseFrozenImports())
from research.mission_lab_v0_1 import core, planner, replan, report, executor, operators
print("isolated-controller-imports")
'''
    repository = Path(__file__).resolve().parents[3]
    completed = subprocess.run([sys.executable, "-B", "-c", script], cwd=repository,
                               capture_output=True, text=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "isolated-controller-imports"


@pytest.fixture
def executable(planning, tmp_path, monkeypatch):
    """Inject policy roots; real registered workers still execute in subprocesses.

    Marker bytes isolate source-receipt validation from concurrent source edits.
    This is a test fixture, not a claim about installed-source authenticity.
    """
    from research.mission_lab_v0_1 import executor
    planner, mission, profile = planning
    package = tmp_path / "isolated" / "research" / "mission_lab_v0_1"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_bytes(b"abc")
    (package / "controller_contract.txt").write_bytes(b"abc")
    (package / "fixtures").mkdir()
    input_path = package / "fixtures" / "input.bin"
    input_path.write_bytes(b"abc")
    mission["inputs"][0]["path"] = str(input_path)
    monkeypatch.setattr(core, "ROOT", package)
    monkeypatch.setattr(executor, "ROOT", package)
    monkeypatch.setattr(executor, "WORKSPACE", tmp_path)
    # The fixed worker module lives in the repository, while policy inputs
    # and execution evidence are confined to this test's temporary root.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    # The planning fixture uses a stable fixture source provider; these exact
    # source bytes are archived and checked by the real executor and replay.
    monkeypatch.setattr(core, "source_manifest", lambda: {
        "controller_contract.txt": core.file_hash(package / "controller_contract.txt"),
        "@research/__init__.py": core.file_hash(package.parent / "__init__.py"),
    })
    return planner.build_plan(mission, profile), package / "evidence", executor


def test_actual_execution_and_replay_are_semantically_deterministic(executable):
    from research.mission_lab_v0_1 import report
    plan, evidence, executor = executable
    first = executor.run_plan_data(plan, evidence / "first")
    second = executor.run_plan_data(plan, evidence / "second")
    expected_manifest = {"artifacts": [{"input_id": "sample", "sha256": ABC_SHA256, "size_bytes": 3}]}
    assert core.read_json(first / "manifest.json") == expected_manifest
    assert core.read_json(second / "manifest.json") == expected_manifest
    assert (first / "semantic.json").read_bytes() == (second / "semantic.json").read_bytes()
    regenerated_a = report.replay_run(first)
    regenerated_b = report.replay_run(second)
    assert regenerated_a["counts"] == {"succeeded": 4, "failed": 0, "blocked": 0, "timed_out": 0}
    assert regenerated_b["counts"] == regenerated_a["counts"]
    assert regenerated_a["semantic_sha256"] == regenerated_b["semantic_sha256"]
    assert regenerated_a["accepted"] is True
    assert regenerated_a["integrity_verified"] is True
    assert regenerated_a["independent_execution"] is False
    assert regenerated_a["report"] == (first / "report.md").read_text(encoding="utf-8")
    assert report.replay_run(first) == regenerated_a
    receipts = core.read_json(first / "input_receipts.json")
    assert receipts["archived_inputs"] is True
    assert (first / receipts["inputs"][0]["snapshot"]).read_bytes() == b"abc"


def test_actual_events_respect_dependencies_and_resource_reservations(executable):
    plan, evidence, executor = executable
    run = executor.run_plan_data(plan, evidence / "events")
    events = [json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["sequence"] for event in events] == list(range(len(events)))
    completed, active, used = set(), set(), 0
    nodes = {node["id"]: node for node in plan["graph"]}
    for event in events:
        if event["kind"] == "launched":
            task_id = event["task_id"]
            assert set(nodes[task_id]["depends_on"]) <= completed
            assert task_id not in active
            active.add(task_id)
            used += plan["bindings"][task_id]["buffer_bytes"]
            assert event["workers_active"] == len(active) <= 2
            assert event["used_buffer_bytes"] == used <= 65536
        elif event["kind"] == "completed":
            task_id = event["task_id"]
            assert task_id in active
            active.remove(task_id)
            used -= plan["bindings"][task_id]["buffer_bytes"]
            assert event["workers_active"] == len(active)
            assert event["used_buffer_bytes"] == used
            assert event["status"] == "succeeded"
            completed.add(task_id)
    assert completed == set(nodes)
    assert not active
    assert used == 0
    observations = core.read_json(run / "observations.json")
    assert {row["task_id"] for row in observations} == set(nodes)
    assert all(row["duration_ns"] > 0 and row["exit_code"] == 0 for row in observations)


def test_changed_input_or_source_is_rejected_before_output_creation(executable):
    plan, evidence, executor = executable
    artifact = Path(plan["mission"]["inputs"][0]["path"])
    artifact.write_bytes(b"abd")
    with pytest.raises(core.LabError):
        executor.run_plan_data(plan, evidence / "changed-input")
    assert not (evidence / "changed-input").exists()
    artifact.write_bytes(b"abc")
    marker = artifact.parents[1] / "controller_contract.txt"
    marker.write_bytes(b"abd")
    with pytest.raises(core.LabError, match="Source identity"):
        executor.run_plan_data(plan, evidence / "changed-source")
    assert not (evidence / "changed-source").exists()


def reseal_evidence_for_adversarial_fixture(run):
    """Resealing tests semantic validation; hashes alone are not authenticity."""
    manifest = core.read_json(run / "integrity.json")
    manifest["files"] = {path.relative_to(run).as_posix(): core.file_hash(path)
                         for path in run.rglob("*") if path.is_file() and path.name != "integrity.json"}
    if "semantic_sha256" in manifest:
        manifest["semantic_sha256"] = core.file_hash(run / "semantic.json")
    (run / "integrity.json").write_text(core.canonical(manifest), encoding="utf-8")


@pytest.mark.parametrize("tamper", ["promoted_manifest", "semantic_manifest", "missing_observations", "negative_duration", "human_review", "input_receipt", "resource_event"])
def test_replay_rejects_resealed_semantic_and_receipt_contradictions(executable, tamper):
    from research.mission_lab_v0_1 import report
    plan, evidence, executor = executable
    run = executor.run_plan_data(plan, evidence / "tampered")
    semantic = core.read_json(run / "semantic.json")
    observations = core.read_json(run / "observations.json")
    if tamper == "promoted_manifest":
        (run / "manifest.json").write_text(core.canonical({"artifacts": []}), encoding="utf-8")
    elif tamper == "semantic_manifest":
        final = next(task for task in semantic["tasks"] if task["task_id"] == "manifest")
        final["output"] = {"artifacts": []}
        (run / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8")
    elif tamper == "missing_observations":
        observations = []
        (run / "observations.json").write_text(core.canonical(observations), encoding="utf-8")
    elif tamper == "negative_duration":
        observations[0]["duration_ns"] = -1
        (run / "observations.json").write_text(core.canonical(observations), encoding="utf-8")
    elif tamper == "human_review":
        semantic["human_review"] = True
        (run / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8")
    elif tamper == "input_receipt":
        receipts = core.read_json(run / "input_receipts.json")
        receipts["inputs"][0]["sha256"] = "f" * 64
        (run / "input_receipts.json").write_text(core.canonical(receipts), encoding="utf-8")
    else:
        rows = [json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        next(row for row in rows if row["kind"] == "launched")["workers_active"] = 9
        (run / "events.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    # Regenerate the report too, so rejection has to come from a meaningful
    # contract contradiction rather than a stale Markdown representation.
    (run / "report.md").write_text(report.render_report(semantic, observations), encoding="utf-8")
    reseal_evidence_for_adversarial_fixture(run)
    with pytest.raises(core.LabError):
        report.replay_run(run)
