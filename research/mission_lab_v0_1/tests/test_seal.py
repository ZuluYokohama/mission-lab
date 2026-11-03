"""HMAC seals, approval signatures, lineage, and controller-key isolation.

Keys are generated in temporary files or built in the test process. No network
and no committed secret.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research.mission_lab_v0_1 import core, executor, gates, planner, replan, report, seal, worker
from research.mission_lab_v0_1.__main__ import main


# Split so the contiguous secret is not a substring of this source file.
# A run snapshots package sources, and the leak scan must be able to see a copy.
SEAL_SECRET = "VISIBLE-SEAL-KEY-" + "0123456789abcde"
APPROVER_SECRET = "VISIBLE-APPROVER-" + "KEY-0123456789ab"


def _profile(**extra):
    return {"schema_version": 1, "workers": 1, "read_buffer_bytes": 65536, "timeout_seconds": 60,
            "estimate_origin": "injected_fixture", **extra}


def _evidence(tmp_name):
    root = core.ROOT / "evidence"
    root.mkdir(exist_ok=True)
    path = root / tmp_name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    return path


@pytest.fixture
def evidence_dir():
    path = _evidence("seal-test-" + hashlib.sha256(os.urandom(8)).hexdigest()[:12])
    yield path
    shutil.rmtree(path, ignore_errors=True)


_LEGACY_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_unsigned_270f1eb"


def _install_legacy_run(dest):
    """Copy the run generated at commit 270f1eb and retarget its input path.

    The checked-in report.md is the pre-change renderer. This checkout's
    absolute fixture path differs from the path recorded at generation, so the
    copy substitutes that path and the plan id the substitution produces.
    The rest of the report prose is unchanged. Replay does not reopen command
    paths stored in observations.
    """
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(_LEGACY_FIXTURE, dest)
    plan = core.read_json(dest / "plan.json")
    local = (core.ROOT / "fixtures" / "abc.bin").resolve()
    recorded = plan["mission"]["inputs"][0]
    if recorded["sha256"] != core.file_hash(local) or recorded["size_bytes"] != local.stat().st_size:
        raise AssertionError("legacy fixture input does not match fixtures/abc.bin")
    old_plan_id = plan["plan_id"]
    plan["mission"]["inputs"][0] = {**recorded, "path": str(local)}
    expected = planner.build_plan(plan["mission"], plan["resources"], sources=plan["sources"], check_files=True)
    if expected["bindings"] != plan["bindings"] or expected["graph"] != plan["graph"]:
        raise AssertionError("retarget changed the pre-change plan body")
    (dest / "plan.json").write_text(core.canonical(expected), encoding="utf-8", newline="\n")
    for name in ("semantic.json", "report.md"):
        path = dest / name
        text = path.read_text(encoding="utf-8")
        if text.count(old_plan_id) != 1:
            raise AssertionError(name + " does not contain the recorded plan id once")
        path.write_text(text.replace(old_plan_id, expected["plan_id"]), encoding="utf-8", newline="\n")
    receipts = core.read_json(dest / "input_receipts.json")
    for row in receipts["inputs"]:
        if row.get("id") == recorded["id"]:
            row["path"] = expected["mission"]["inputs"][0]["path"]
    (dest / "input_receipts.json").write_text(core.canonical(receipts), encoding="utf-8", newline="\n")
    seal.rewrite_unsigned(dest)
    return dest


def _simple_plan(directory):
    source = directory / "abc.bin"
    source.write_bytes(b"abc")
    artifact = {"id": "alpha", "path": str(source), "size_bytes": 3, "sha256": core.file_hash(source)}
    mission = {"schema_version": 1, "mission_id": "seal_fixture", "inputs": [artifact],
               "requirements": [{"id": "identity", "text": "Verify the fixture bytes",
                                 "acceptance_criteria": ["Exact digest and byte count agree"], "evidence_refs": []}],
               "unresolved": []}
    return planner.build_plan(mission, _profile())


def _trigger():
    return {"schema_version": 1, "kind": "resource_change", "text": "Record the parent seal.",
            "evidence_origin": "injected_demo", "evidence_refs": []}


def _approval_mission(key_ids):
    mission = core.load_mission(core.ROOT / "fixtures" / "mission_gate.json")
    merit = next(node for node in mission["tasks"] if node["id"] == "merit")
    merit["id"] = "gate:merit"
    merit["depends_on"] = ["ask"]
    merit["state_reads"] = ["output:ask"]
    merit["outputs"] = ["output:gate:merit"]
    merit["state_writes"] = ["output:gate:merit"]
    merit["predicate"] = {"kind": "human_approval", "approver_id": "reviewer-1", "approver_key_ids": list(key_ids)}
    follow = next(node for node in mission["tasks"] if node["id"] == "follow")
    follow["depends_on"] = ["gate:merit", "verify:reply"]
    follow["state_reads"] = ["output:gate:merit", "output:verify:reply"]
    return mission


def _task(semantic, task_id):
    return next(task for task in semantic["tasks"] if task["task_id"] == task_id)


def _gate_node(key_ids):
    return {"id": "gate:merit", "operation": "gate", "predicate": {
        "kind": "human_approval", "approver_id": "reviewer-1", "approver_key_ids": list(key_ids)}}


def _unsigned_record():
    return {"schema_version": 1, "gate_id": "gate:merit", "approver_id": "reviewer-1",
            "approved_at": "2026-11-03T02:00:00Z"}


def test_key_id_is_a_stable_fingerprint_and_the_mac_covers_the_body():
    material = b"seal-key-16bytes"
    assert seal.key_id(material) == seal.key_id(material)
    assert seal.key_id(material) != seal.key_id(b"other-key-16byte")
    assert material.decode() not in seal.key_id(material)
    body = {"schema_version": 1, "files": {"a": "ab" * 32}}
    signed = seal.sign_body(body, material)
    assert signed["algorithm"] == "hmac-sha256"
    assert signed["key_id"] == seal.key_id(material)
    assert seal.mac_matches(body, material, signed["value"])
    assert not seal.mac_matches({**body, "files": {}}, material, signed["value"])
    with pytest.raises(core.LabError, match=r"16\.\.4096"):
        seal.key_id(b"short")
    text = Path(seal.__file__).read_text(encoding="utf-8")
    assert "chmod" not in text
    assert "st_mode" not in text


def test_key_sources_reject_ambiguity_links_and_short_values(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY_FILE", raising=False)
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY", "short")
    with pytest.raises(core.LabError, match=r"16\.\.4096"):
        seal.require_seal_key()
    path = tmp_path / "seal.key"
    path.write_bytes(os.urandom(32))
    if os.name != "nt":
        path.chmod(0o644)
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY", raising=False)
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY_FILE", str(path))
    assert seal.require_seal_key() == path.read_bytes()
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY", "a" * 16)
    with pytest.raises(core.LabError, match="ambiguous"):
        seal.require_seal_key()
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY", raising=False)
    link = tmp_path / "seal.link"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("symlink not available")
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY_FILE", str(link))
    with pytest.raises(core.LabError, match="must not be a link"):
        seal.require_seal_key()


def test_approver_keys_load_from_a_directory_and_reject_a_collision(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY", raising=False)
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY_FILE", raising=False)
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY_DIR", raising=False)
    assert seal.load_approver_keys() == {}
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY", "short")
    with pytest.raises(core.LabError, match=r"16\.\.4096"):
        seal.load_approver_keys()
    directory = tmp_path / "approvers"
    directory.mkdir()
    (directory / "nested").mkdir()
    (directory / "nested" / "ignored.key").write_bytes(b"n" * 16)
    material = os.urandom(32)
    (directory / "one.key").write_bytes(material)
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY", raising=False)
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY_DIR", str(directory))
    loaded = seal.load_approver_keys()
    assert loaded == {seal.key_id(material): material}
    def same_id(key):
        return "ab" * 32

    monkeypatch.setattr(seal, "key_id", same_id)
    index = {}
    seal._remember(index, b"a" * 16)
    with pytest.raises(core.LabError, match="collision"):
        seal._remember(index, b"b" * 16)


def test_controller_key_names_cannot_be_granted_or_passed(monkeypatch):
    secret = "grant-" + "secret-value-0123456789abcd"
    parent = os.environ.copy()
    parent["MISSION_LAB_SEAL_KEY"] = secret
    native = core.worker_environment({}, "hash:alpha", parent)
    assert secret not in native.values()
    assert all(name.casefold() not in {name.casefold() for name in core.CONTROLLER_KEY_ENV} for name in native)
    probe = subprocess.run(
        [sys.executable, "-B", "-c",
         "import os,sys; sys.stdout.write(os.environ.get('MISSION_LAB_SEAL_KEY', ''))"],
        env=native, capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == ""
    for windows in (False, True):
        env = core._worker_environment({}, "hash:alpha", parent, windows=windows)
        assert secret not in env.values()
        assert all(name.casefold() not in {name.casefold() for name in core.CONTROLLER_KEY_ENV} for name in env)
    for name in core.CONTROLLER_KEY_ENV:
        for variant in (name, name.casefold()):
            with pytest.raises(core.LabError, match="Controller key variable cannot be granted: " + variant):
                core.validate_worker_env({"declared": [variant], "grants": {"hash:alpha": [variant]}})
            resources = {"worker_env": {"declared": [variant], "grants": {"hash:alpha": [variant]}}}
            for windows in (False, True):
                with pytest.raises(core.LabError, match="cannot be granted"):
                    core._worker_environment(resources, "hash:alpha", {variant: secret, "PATH": "/usr/bin"}, windows=windows)
    mission_dir = _evidence("seal-grant-" + hashlib.sha256(os.urandom(4)).hexdigest()[:8])
    try:
        source = mission_dir / "abc.bin"
        source.write_bytes(b"abc")
        artifact = {"id": "alpha", "path": str(source), "size_bytes": 3, "sha256": core.file_hash(source)}
        mission = {"schema_version": 1, "mission_id": "seal_grant", "inputs": [artifact],
                   "requirements": [{"id": "identity", "text": "Verify the fixture bytes",
                                     "acceptance_criteria": ["Exact digest"], "evidence_refs": []}],
                   "unresolved": []}
        profile = _profile(worker_env={"declared": ["mission_lab_seal_key"], "grants": {"hash:alpha": ["mission_lab_seal_key"]}})
        with pytest.raises(core.LabError, match="cannot be granted") as caught:
            planner.build_plan(mission, profile)
        assert secret not in str(caught.value)
    finally:
        shutil.rmtree(mission_dir, ignore_errors=True)
    mapping = {"Mission_Lab_Seal_Key": secret, "PATH": "/usr/bin"}
    worker.drop_controller_keys(mapping)
    assert mapping == {"PATH": "/usr/bin"}
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY_DIR", secret)
    worker.drop_controller_keys()
    assert "MISSION_LAB_APPROVER_KEY_DIR" not in os.environ


def test_signed_run_verifies_and_the_key_stays_out_of_the_worker(evidence_dir, monkeypatch):
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY_FILE", raising=False)
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY", SEAL_SECRET)
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY", APPROVER_SECRET)
    plan = _simple_plan(evidence_dir)
    captured = []
    real_popen = subprocess.Popen

    def spy(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "research.mission_lab_v0_1.worker" in command:
            captured.append(kwargs.get("env"))
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(executor.subprocess, "Popen", spy)
    output = executor.run_plan_data(plan, evidence_dir / "run")
    key = SEAL_SECRET.encode()
    manifest_path = output / "integrity.json"
    manifest = core.read_json(manifest_path)
    assert manifest_path.read_bytes() == core.canonical(manifest).encode()
    assert manifest["signature"]["algorithm"] == "hmac-sha256"
    assert manifest["signature"]["key_id"] == seal.key_id(key)
    assert seal.mac_matches(manifest, key, manifest["signature"]["value"])
    assert manifest["hash_note"] == seal.HASH_NOTE
    semantic = core.read_json(output / "semantic.json")
    assert semantic["seal"] == seal.claim(key)
    replayed = report.replay_run(output)
    assert replayed["signature_status"] == "verified"
    assert replayed["integrity_verified"] is True
    assert replayed["seal_key_id"] == seal.key_id(key)
    text = (output / "report.md").read_text(encoding="utf-8")
    assert "Signature status: `signed`" in text
    assert "Controller key variables cannot be granted and are not copied:" in text
    assert seal.key_id(key) in text
    withheld = core.read_json(output / "environment.json")["worker_environment"]["withheld_controller_key_names"]
    assert withheld == sorted(core.CONTROLLER_KEY_ENV)
    assert len(captured) == len(plan["graph"])
    for env in captured:
        assert isinstance(env, dict)
        assert SEAL_SECRET not in env.values()
        assert APPROVER_SECRET not in env.values()
        folded = {name.casefold() for name in env}
        assert folded.isdisjoint({name.casefold() for name in core.CONTROLLER_KEY_ENV})
        probe = subprocess.run(
            [sys.executable, "-B", "-c",
             "import os,sys; sys.stdout.write(os.environ.get('MISSION_LAB_SEAL_KEY', '') + os.environ.get('MISSION_LAB_APPROVER_KEY', ''))"],
            env=env, capture_output=True, text=True, check=False)
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout == ""
    observations = core.read_json(output / "observations.json")
    events = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in list(observations) + [event for event in events if event.get("kind") == "launched"]:
        folded = {name.casefold() for name in row["environment_names"]}
        assert folded.isdisjoint({name.casefold() for name in core.CONTROLLER_KEY_ENV})
    for path in output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert SEAL_SECRET.encode() not in data
            assert APPROVER_SECRET.encode() not in data


def test_tamper_wrong_key_missing_key_and_legacy_are_distinct(evidence_dir, monkeypatch, capsys):
    original_key = os.environ["MISSION_LAB_SEAL_KEY_FILE"]
    plan = _simple_plan(evidence_dir)
    output = executor.run_plan_data(plan, evidence_dir / "run")
    key_id = core.read_json(output / "integrity.json")["signature"]["key_id"]

    changed = evidence_dir / "changed"
    shutil.copytree(output, changed)
    report_path = changed / "report.md"
    report_path.write_bytes(report_path.read_bytes() + b"\n")
    with pytest.raises(core.LabError, match="Archived evidence changed"):
        report.replay_run(changed)

    resealed = evidence_dir / "resealed"
    shutil.copytree(output, resealed)
    old = core.read_json(resealed / "integrity.json")["signature"]
    (resealed / "report.md").write_bytes((resealed / "report.md").read_bytes() + b"\n")
    seal.rewrite_unsigned(resealed, signature=old)
    with pytest.raises(core.LabError, match="Seal signature is not valid"):
        report.replay_run(resealed)

    stripped = evidence_dir / "stripped"
    shutil.copytree(output, stripped)
    seal.rewrite_unsigned(stripped)
    with pytest.raises(core.LabError, match="Resealed run has no valid signature"):
        report.replay_run(stripped, allow_legacy_unsigned=True)

    other = evidence_dir / "other.key"
    other.write_bytes(os.urandom(32))
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY_FILE", str(other))
    with pytest.raises(core.LabError, match="Seal key does not match the recorded key id"):
        report.replay_run(output)
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY", raising=False)
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY_FILE", raising=False)
    with pytest.raises(core.LabError, match="Seal key is not available"):
        report.replay_run(output)
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY_FILE", original_key)

    stripped_claim = evidence_dir / "stripped-claim"
    shutil.copytree(output, stripped_claim)
    semantic = core.read_json(stripped_claim / "semantic.json")
    semantic.pop("seal")
    (stripped_claim / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8", newline="\n")
    seal.rewrite_unsigned(stripped_claim)
    with pytest.raises(core.LabError, match="Resealed run has no valid signature"):
        report.replay_run(stripped_claim, allow_legacy_unsigned=True)

    legacy = _install_legacy_run(evidence_dir / "legacy")
    assert core.read_json(legacy / "integrity.json")["hash_note"] == seal.LEGACY_HASH_NOTE
    assert "withheld_controller_key_names" not in core.read_json(legacy / "environment.json")["worker_environment"]
    with pytest.raises(core.LabError, match="Unsigned seal"):
        report.replay_run(legacy)
    allowed = report.replay_run(legacy, allow_legacy_unsigned=True)
    assert allowed["signature_status"] == "unsigned (legacy)"
    assert allowed["integrity_verified"] is False
    assert allowed["seal_key_id"] is None
    assert main(["replay", "--run", str(legacy), "--allow-unsigned-legacy"]) == 0
    assert json.loads(capsys.readouterr().out)["signature_status"] == "unsigned (legacy)"
    assert main(["replay", "--run", str(legacy)]) == 2
    assert "Unsigned seal" in capsys.readouterr().err

    unknown = evidence_dir / "unknown"
    shutil.copytree(output, unknown)
    manifest = core.read_json(unknown / "integrity.json")
    manifest["signature"]["algorithm"] = "ed25519"
    manifest["signature"]["value"] = "ab" * 64
    (unknown / "integrity.json").write_text(core.canonical(manifest), encoding="utf-8", newline="\n")
    with pytest.raises(core.LabError, match="Unsupported seal signature algorithm"):
        report.replay_run(unknown)

    mismatch = evidence_dir / "mismatch"
    shutil.copytree(output, mismatch)
    semantic = core.read_json(mismatch / "semantic.json")
    semantic["seal"]["key_id"] = "ab" * 32
    (mismatch / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8", newline="\n")
    seal.reseal(mismatch)
    with pytest.raises(core.LabError, match="Seal claim does not match the signature"):
        report.replay_run(mismatch)

    fresh = evidence_dir / "missing-key"
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY", raising=False)
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY_FILE", raising=False)
    with pytest.raises(core.LabError, match="Seal key is not configured"):
        executor.run_plan_data(plan, fresh)
    assert not fresh.exists()
    assert key_id == seal.key_id(Path(original_key).read_bytes())


def test_signed_and_unsigned_approvals_and_resume_lineage(evidence_dir, monkeypatch):
    material = b"approver-key-" + b"bytes!!"
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY", material.decode())
    key_id = seal.key_id(material)
    other = b"second-approver-" + b"key!!"
    node = _gate_node([key_id])
    absent = gates.evaluate(node, {"approval_status": "absent", "approvals": {}})
    assert absent["outcome"] == "pending"
    assert absent["reason"] == "approval record is absent"
    unsigned = gates.evaluate(node, {"approval_status": "ok", "approvals": {"gate:merit": _unsigned_record()}})
    assert unsigned["outcome"] == "pending"
    assert unsigned["reason"] == "approval signature is absent"
    signed = seal.sign_approval(_unsigned_record(), material)
    assert gates.evaluate(node, {"approval_status": "ok", "approvals": {"gate:merit": signed}})["outcome"] == "pass"
    bad = copy.deepcopy(signed)
    bad["signature"]["value"] = ("0" if bad["signature"]["value"][-1] != "0" else "1") + bad["signature"]["value"][1:]
    invalid = gates.evaluate(node, {"approval_status": "ok", "approvals": {"gate:merit": bad}})
    assert invalid["outcome"] == "pending"
    assert invalid["reason"] == "approval signature is not valid"
    foreign = seal.sign_approval(_unsigned_record(), other)
    wrong_key = gates.evaluate(node, {"approval_status": "ok", "approvals": {"gate:merit": foreign}})
    assert wrong_key["outcome"] == "pending"
    assert wrong_key["reason"] == "approval signature key is not a declared approver key"
    future = copy.deepcopy(signed)
    future["signature"]["algorithm"] = "ed25519"
    future["signature"]["value"] = "cd" * 64
    algorithm = gates.evaluate(node, {"approval_status": "ok", "approvals": {"gate:merit": future}})
    assert algorithm["outcome"] == "pending"
    assert algorithm["reason"] == "approval signature algorithm is not hmac-sha256"
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY", raising=False)
    missing = gates.evaluate(node, {"approval_status": "ok", "approvals": {"gate:merit": signed}})
    assert missing["outcome"] == "pending"
    assert missing["reason"] == "approver key is not available: " + key_id
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY", material.decode())
    malformed = evidence_dir / "malformed"
    malformed.mkdir()
    (malformed / "approvals.json").write_text(core.canonical({
        "schema_version": 1, "records": [{**_unsigned_record(), "signature": {
            "schema_version": 1, "algorithm": "hmac-sha256", "key_id": key_id, "value": "zz"}}]}),
        encoding="utf-8", newline="\n")
    assert gates.read_approvals(malformed)[0] == "malformed"

    mission = _approval_mission([key_id])
    plan = planner.build_plan(mission, _profile(workers=2, read_buffer_bytes=4 * 1024 * 1024, timeout_seconds=30))
    pending_run = executor.run_plan_data(plan, evidence_dir / "pending")
    pending = core.read_json(pending_run / "semantic.json")
    assert pending["accepted"] is False
    assert _task(pending, "gate:merit")["output"]["outcome"] == "pending"
    assert _task(pending, "gate:merit")["output"]["reason"] == "approval record is absent"
    assert _task(pending, "follow")["status"] == "blocked"
    pending_replay = report.replay_run(pending_run)
    assert pending_replay["signature_status"] == "verified"
    assert pending_replay["integrity_verified"] is True
    assert material not in (pending_run / "plan.json").read_bytes()

    unsigned_run = executor.resume_run(pending_run, evidence_dir / "unsigned", _unsigned_record())
    unsigned_semantic = core.read_json(unsigned_run / "semantic.json")
    assert unsigned_semantic["accepted"] is False
    assert _task(unsigned_semantic, "gate:merit")["output"]["outcome"] == "pending"
    assert _task(unsigned_semantic, "gate:merit")["output"]["reason"] == "approval signature is absent"
    assert report.replay_run(unsigned_run)["signature_status"] == "verified"
    assert report.replay_run(unsigned_run)["integrity_verified"] is True

    bad_run = executor.resume_run(pending_run, evidence_dir / "bad-mac", bad)
    bad_semantic = core.read_json(bad_run / "semantic.json")
    assert bad_semantic["accepted"] is False
    assert _task(bad_semantic, "gate:merit")["output"]["outcome"] == "pending"
    assert _task(bad_semantic, "gate:merit")["output"]["reason"] == "approval signature is not valid"

    resumed = executor.resume_run(pending_run, evidence_dir / "signed", signed)
    semantic = core.read_json(resumed / "semantic.json")
    assert semantic["accepted"] is True
    assert _task(semantic, "gate:merit")["output"]["outcome"] == "pass"
    assert key_id in _task(semantic, "gate:merit")["output"]["reason"]
    text = (resumed / "report.md").read_text(encoding="utf-8")
    assert key_id in text
    assert "Signature status: `signed`" in text
    assert semantic["resume_of"]["prior_seal_sha256"] == core.file_hash(pending_run / "integrity.json")
    assert semantic["resume_of"]["prior_seal_key_id"] == seal.key_id(seal.require_seal_key())
    lineage = seal.matching_lineage(resumed, pending_run)
    assert lineage["records"] == ["resume_of"]
    mismatched = evidence_dir / "resume-plan"
    shutil.copytree(resumed, mismatched)
    mismatched_semantic = core.read_json(mismatched / "semantic.json")
    mismatched_semantic["resume_of"]["plan_id"] = "ab" * 32
    (mismatched / "semantic.json").write_text(core.canonical(mismatched_semantic), encoding="utf-8", newline="\n")
    with pytest.raises(core.LabError, match="Prior run does not belong to the prior plan"):
        seal.matching_lineage(mismatched, pending_run)
    assert report.replay_run(resumed)["signature_status"] == "verified"
    assert report.replay_run(resumed)["integrity_verified"] is True
    for path in resumed.rglob("*"):
        if path.is_file():
            assert material not in path.read_bytes()


def test_replan_records_the_parent_seal_and_replay_does_not_open_it(evidence_dir):
    plan = _simple_plan(evidence_dir)
    parent = executor.run_plan_data(plan, evidence_dir / "parent")
    parent_hash = core.file_hash(parent / "integrity.json")
    parent_key = seal.key_id(seal.require_seal_key())
    revised = replan.replan_data(plan, plan["resources"], _trigger(), prior_run=parent)
    assert revised["revision"]["prior_seal"]["seal_sha256"] == parent_hash
    assert revised["revision"]["prior_seal"]["algorithm"] == "hmac-sha256"
    assert revised["revision"]["prior_seal"]["key_id"] == parent_key
    planner.validate_plan_record(revised)
    child = executor.run_plan_data(revised, evidence_dir / "child")
    assert parent_key in (child / "report.md").read_text(encoding="utf-8")
    assert seal.matching_lineage(child, parent)["records"] == ["revision"]
    assert report.replay_run(child)["signature_status"] == "verified"

    wrong_hash = evidence_dir / "wrong-hash"
    shutil.copytree(child, wrong_hash)
    semantic = core.read_json(wrong_hash / "semantic.json")
    semantic["revision"]["prior_seal"]["seal_sha256"] = "ab" * 32
    (wrong_hash / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8", newline="\n")
    with pytest.raises(core.LabError, match="Recorded lineage does not match the parent seal"):
        seal.matching_lineage(wrong_hash, parent)

    wrong_key = evidence_dir / "wrong-key"
    shutil.copytree(child, wrong_key)
    semantic = core.read_json(wrong_key / "semantic.json")
    semantic["revision"]["prior_seal"]["key_id"] = "cd" * 32
    (wrong_key / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8", newline="\n")
    with pytest.raises(core.LabError, match="Recorded lineage key id does not match the parent seal"):
        seal.matching_lineage(wrong_key, parent)

    other_source = evidence_dir / "other.bin"
    other_source.write_bytes(b"xyz")
    other_plan = planner.build_plan(
        {"schema_version": 1, "mission_id": "other_fixture", "inputs": [
            {"id": "alpha", "path": str(other_source), "size_bytes": 3, "sha256": core.file_hash(other_source)}],
         "requirements": [{"id": "identity", "text": "Verify other bytes",
                           "acceptance_criteria": ["Exact digest and byte count agree"], "evidence_refs": []}],
         "unresolved": []},
        _profile())
    other_run = executor.run_plan_data(other_plan, evidence_dir / "other-run")
    with pytest.raises(core.LabError, match="Prior run does not belong to the prior plan"):
        replan.replan_data(plan, plan["resources"], _trigger(), prior_run=other_run)
    borrowed = evidence_dir / "borrowed"
    shutil.copytree(child, borrowed)
    semantic = core.read_json(borrowed / "semantic.json")
    semantic["revision"]["prior_seal"]["seal_sha256"] = core.file_hash(other_run / "integrity.json")
    semantic["revision"]["prior_seal"]["key_id"] = parent_key
    (borrowed / "semantic.json").write_text(core.canonical(semantic), encoding="utf-8", newline="\n")
    with pytest.raises(core.LabError, match="Prior run does not belong to the prior plan"):
        seal.matching_lineage(borrowed, other_run)

    moved = evidence_dir / "parent-moved"
    parent.rename(moved)
    replayed = report.replay_run(child)
    assert replayed["signature_status"] == "verified"
    assert replayed["integrity_verified"] is True
    with pytest.raises(core.LabError, match="Missing path"):
        seal.matching_lineage(child, parent)

    legacy = _install_legacy_run(evidence_dir / "legacy-parent")
    legacy_plan = core.load_plan(legacy / "plan.json")
    with pytest.raises(core.LabError, match="Unsigned seal"):
        replan.replan_data(legacy_plan, legacy_plan["resources"], _trigger(), prior_run=legacy)
    unsigned = replan.replan_data(legacy_plan, legacy_plan["resources"], _trigger(), prior_run=legacy,
                                  allow_legacy_unsigned=True)
    assert unsigned["revision"]["prior_seal"]["algorithm"] == "unsigned"
    assert unsigned["revision"]["prior_seal"]["seal_sha256"] == core.file_hash(legacy / "integrity.json")
    assert "key_id" not in unsigned["revision"]["prior_seal"]
    planner.validate_plan_record(unsigned)
    claimed = evidence_dir / "legacy-child"
    claimed.mkdir()
    core.write_json(claimed / "semantic.json", {"revision": {
        "prior_plan_id": legacy_plan["plan_id"],
        "prior_seal": {"schema_version": 1, "seal_sha256": core.file_hash(legacy / "integrity.json"),
                       "algorithm": "unsigned", "key_id": "ab" * 32},
    }})
    with pytest.raises(core.LabError, match="Recorded lineage key id does not match the parent seal"):
        seal.matching_lineage(claimed, legacy, allow_legacy_unsigned=True)


def test_approver_configuration_raises_before_evaluation_and_replay_names_the_key(evidence_dir, monkeypatch):
    material = b"approver-key-" + b"bytes!!"
    fingerprint = seal.key_id(material)
    directory = evidence_dir / "approver-keys"
    directory.mkdir()
    (directory / "empty.key").write_bytes(b"")
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY_DIR", str(directory))
    executor.run_plan_data(_simple_plan(evidence_dir), evidence_dir / "hash-ok")
    plan = planner.build_plan(_approval_mission([fingerprint]),
                              _profile(workers=2, read_buffer_bytes=4 * 1024 * 1024, timeout_seconds=30))
    blocked = evidence_dir / "not-started"
    with pytest.raises(core.LabError, match=r"16\.\.4096"):
        executor.run_plan_data(plan, blocked)
    assert not blocked.exists()
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY_DIR")
    key_file = directory / "one.key"
    key_file.write_bytes(material)
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY", material.decode())
    monkeypatch.setenv("MISSION_LAB_APPROVER_KEY_FILE", str(key_file))
    with pytest.raises(core.LabError, match="ambiguous"):
        executor.run_plan_data(plan, blocked)
    assert not blocked.exists()
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY_FILE")
    pending = executor.run_plan_data(plan, evidence_dir / "pending-key")
    signed = seal.sign_approval(_unsigned_record(), material)
    passed = executor.resume_run(pending, evidence_dir / "passed-key", signed)
    assert _task(core.read_json(passed / "semantic.json"), "gate:merit")["output"]["outcome"] == "pass"
    monkeypatch.delenv("MISSION_LAB_APPROVER_KEY")
    with pytest.raises(core.LabError, match="approver key is not available: " + fingerprint):
        report.replay_run(passed)
