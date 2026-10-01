"""Local schemas, runtime validation, identity, and output authority.

No shared research helpers are imported: those are frozen study dependencies.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
ARCHIVE = ROOT / "fixtures"
SCHEMA = 1
OPERATIONS = {"sha256", "reference_sha256", "verify_digest", "assemble_manifest"}
REVISIONS = {"whole_v1": "1", "stream_v1": "1", "reference_v1": "1", "compare_v1": "1", "manifest_v1": "1"}
OPERATOR_CONTRACTS = {
    "sha256": {"schema_version": 1, "input_type": "file_artifact_ref", "output_type": "digest_record",
               "candidates": {"whole_v1": "1", "stream_v1": "1"},
               "preconditions": ["declared file identity", "buffer reservation fits"],
               "postconditions": ["exact declared digest and byte count"], "effects": "read declared file; own one staged result"},
    "reference_sha256": {"schema_version": 1, "input_type": "file_artifact_ref", "output_type": "digest_record",
                         "candidates": {"reference_v1": "1"}, "preconditions": ["declared file identity", "8 KiB maximum read buffer"],
                         "postconditions": ["separate sequential read agrees with declared digest"], "effects": "read declared file; own one staged result"},
    "verify_digest": {"schema_version": 1, "input_type": "candidate_and_reference_digest_records", "output_type": "verified_digest_record",
                      "candidates": {"compare_v1": "1"}, "preconditions": ["both dependencies succeeded for same input"],
                      "postconditions": ["exact equality with declared input; passed is true"], "effects": "read dependencies; own one staged result"},
    "assemble_manifest": {"schema_version": 1, "input_type": "verified_digest_records", "output_type": "artifact_manifest",
                          "candidates": {"manifest_v1": "1"}, "preconditions": ["all input verifications succeeded"],
                          "postconditions": ["exactly one digest/size row per input"], "effects": "read dependencies; own one staged manifest"},
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class LabError(ValueError):
    """An explicit contract or evidence-integrity failure."""


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def digest_object(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    state = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(8192):
            state.update(chunk)
    return state.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LabError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(LabError("Non-finite JSON: " + value)))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LabError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical(value))


def fields(value, required, optional=(), label="record"):
    if not isinstance(value, dict):
        raise LabError(label + " must be an object")
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    if missing or extra:
        raise LabError(f"{label}: missing {sorted(missing)}, unknown {sorted(extra)}")


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise LabError("Invalid " + label)
    return value


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise LabError(f"{label} must be an integer in [{low}, {high}]")
    return value


def strings(value, label, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise LabError(label + " must be a list of nonempty strings")
    if nonempty and not value:
        raise LabError(label + " cannot be empty")
    return list(value)


def _version(value, label):
    if type(value) is not int or value != SCHEMA:
        raise LabError("Unsupported " + label + " schema_version")


def has_link(path):
    path = Path(path)
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def confined_path(path, roots, *, exists=True):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise LabError("Path traversal is forbidden")
    resolved = path.resolve()
    roots = [Path(root).resolve() for root in roots]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise LabError("Path is outside allowed roots: " + str(path))
    for component in (path, *path.parents):
        if has_link(component):
            raise LabError("Linked path component is forbidden: " + str(component))
    if exists and not resolved.exists():
        raise LabError("Missing path: " + str(path))
    return resolved


def new_output(requested, *, evidence_root=None):
    evidence = Path(evidence_root).absolute() if evidence_root else ROOT / "evidence"
    requested = Path(requested)
    if not requested.is_absolute():
        requested = (evidence.parent / requested)
    output = confined_path(requested, [evidence], exists=False)
    if output == evidence.resolve() or output.exists():
        raise LabError("Output must be a fresh directory below the evidence root")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after creating parents; no overwrites and no link traversal.
    confined_path(output, [evidence], exists=False)
    output.mkdir(exist_ok=False)
    return output


def validate_resources(value):
    fields(value, ("schema_version", "workers", "read_buffer_bytes", "timeout_seconds", "estimate_origin"),
           ("cost_model",), "resources")
    _version(value["schema_version"], "resources")
    integer(value["workers"], 1, 8, "workers")
    integer(value["read_buffer_bytes"], 1, 2**40, "read_buffer_bytes")
    timeout = value["timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise LabError("timeout_seconds must be finite and in (0, 3600]")
    if value["estimate_origin"] not in ("injected_fixture", "measured"):
        raise LabError("Unknown estimate_origin")
    costs = value.get("cost_model", {"whole_ns_per_byte": 1, "stream_ns_per_byte": 2,
                                   "reference_ns_per_byte": 3, "fixed_task_ns": 1000})
    fields(costs, ("whole_ns_per_byte", "stream_ns_per_byte", "reference_ns_per_byte", "fixed_task_ns"), label="cost_model")
    for key, number in costs.items():
        integer(number, 1, 10**12, key)
    if value["estimate_origin"] == "measured" and "cost_model" not in value:
        raise LabError("Measured profiles require an explicit cost_model; defaults are injected")
    return {**value, "cost_model": dict(costs)}


def default_resources():
    return validate_resources({"schema_version": 1, "workers": 2, "read_buffer_bytes": 4 * 1024 * 1024,
                               "timeout_seconds": 60, "estimate_origin": "injected_fixture"})


def validate_mission(value, base=None, *, allowed_roots=None, check_files=True):
    fields(value, ("schema_version", "mission_id", "requirements", "inputs", "unresolved"), ("tasks",), "mission")
    _version(value["schema_version"], "mission")
    identifier(value["mission_id"], "mission_id")
    base = Path(base or ROOT)
    roots = allowed_roots if allowed_roots is not None else [WORKSPACE, ARCHIVE]
    requirements = value["requirements"]
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 64:
        raise LabError("Mission requires 1..64 requirements")
    ids = set()
    for requirement in requirements:
        fields(requirement, ("id", "text", "acceptance_criteria", "evidence_refs"), label="requirement")
        rid = identifier(requirement["id"], "requirement id")
        if rid in ids:
            raise LabError("Duplicate requirement id")
        ids.add(rid)
        if not isinstance(requirement["text"], str) or not requirement["text"].strip():
            raise LabError("Requirement text must be nonempty")
        strings(requirement["acceptance_criteria"], "acceptance_criteria", True)
        strings(requirement["evidence_refs"], "evidence_refs")
    if not isinstance(value["unresolved"], list):
        raise LabError("unresolved must be a list")
    issue_ids = set()
    for issue in value["unresolved"]:
        fields(issue, ("id", "question", "affects"), label="unresolved interpretation")
        identifier(issue["id"])
        if issue["id"] in issue_ids:
            raise LabError("Duplicate clarification id")
        issue_ids.add(issue["id"])
        if not isinstance(issue["question"], str) or not issue["question"].strip():
            raise LabError("Clarification question must be nonempty")
        if issue["affects"] not in ("intent", "acceptance", "authority", "tradeoff"):
            raise LabError("Unknown clarification scope")
    if not isinstance(value["inputs"], list) or not 1 <= len(value["inputs"]) <= 8:
        raise LabError("Mission requires 1..8 input files")
    inputs, ids, paths = [], set(), set()
    for item in value["inputs"]:
        fields(item, ("id", "path", "size_bytes", "sha256"), label="input")
        aid = identifier(item["id"], "input id")
        if aid in ids:
            raise LabError("Duplicate input id")
        ids.add(aid)
        integer(item["size_bytes"], 0, 2**40, "input size_bytes")
        if not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"]):
            raise LabError("Malformed input SHA-256")
        if not isinstance(item["path"], str) or not item["path"]:
            raise LabError("Input path must be nonempty")
        candidate = Path(item["path"])
        if not candidate.is_absolute():
            candidate = base / candidate
        path = confined_path(candidate, roots, exists=check_files)
        if str(path).casefold() in paths:
            raise LabError("Duplicate input path")
        paths.add(str(path).casefold())
        if check_files and (not path.is_file() or path.stat().st_size != item["size_bytes"] or file_hash(path) != item["sha256"]):
            raise LabError("Input identity changed: " + aid)
        inputs.append({**item, "path": str(path)})
    result = {**value, "inputs": inputs}
    if "tasks" in result:
        validate_graph(result["tasks"], result["inputs"])
    return result


def load_mission(path):
    return validate_mission(read_json(path), Path(path).resolve().parent)


def validate_graph(graph, inputs):
    if not isinstance(graph, list) or not 1 <= len(graph) <= 64:
        raise LabError("Graph requires 1..64 nodes")
    known_inputs = {item["id"] for item in inputs}
    nodes, writers = {}, {}
    for node in graph:
        fields(node, ("id", "operation", "depends_on", "outputs", "state_reads", "state_writes"), ("input_id",), "node")
        nid = identifier(node["id"], "node id")
        if nid in nodes:
            raise LabError("Duplicate node id")
        nodes[nid] = node
        if not isinstance(node["operation"], str) or node["operation"] not in OPERATIONS:
            raise LabError("Unsupported operation")
        for key in ("depends_on", "outputs", "state_reads", "state_writes"):
            strings(node[key], key)
            if len(set(node[key])) != len(node[key]):
                raise LabError("Duplicate graph references")
        if len(node["outputs"]) != 1 or node["state_writes"] != node["outputs"]:
            raise LabError("Each registered node owns exactly one declared output")
        for output in node["outputs"]:
            identifier(output, "output id")
            if output in writers:
                raise LabError("Conflicting output/state writers")
            writers[output] = nid
        if node["operation"] != "assemble_manifest" and (not isinstance(node.get("input_id"), str) or node.get("input_id") not in known_inputs):
            raise LabError("Unknown graph input")
        if node["operation"] == "assemble_manifest" and "input_id" in node:
            raise LabError("Manifest has no direct input file")
    remaining, ordered = set(nodes), []
    while remaining:
        ready = sorted(nid for nid in remaining if set(nodes[nid]["depends_on"]) <= set(ordered))
        if not ready:
            raise LabError("Cycle or missing dependency")
        ordered.extend(ready)
        remaining.difference_update(ready)
    ancestors = {}
    for nid in ordered:
        node = nodes[nid]
        deps = node["depends_on"]
        ancestors[nid] = set(deps) | set().union(*(ancestors[d] for d in deps)) if deps else set()
        for state in node["state_reads"]:
            if state not in writers or writers[state] not in ancestors[nid]:
                raise LabError("State read lacks producer dependency")
        op = node["operation"]
        if op in ("sha256", "reference_sha256") and (deps or node["state_reads"]):
            raise LabError("Digest operators require only their declared file")
        if op == "verify_digest":
            if len(deps) != 2 or {nodes[d]["operation"] for d in deps} != {"sha256", "reference_sha256"}:
                raise LabError("Verification requires a hash and independent reference")
            if any(nodes[d].get("input_id") != node["input_id"] for d in deps):
                raise LabError("Verification input mismatch")
        if op == "assemble_manifest":
            if not deps or any(nodes[d]["operation"] != "verify_digest" for d in deps):
                raise LabError("Manifest requires verified artifacts")
        expected_reads = sorted(nodes[d]["outputs"][0] for d in deps)
        if sorted(node["state_reads"]) != expected_reads:
            raise LabError("State interfaces mismatch dependency outputs")
    for item in inputs:
        for operation in ("sha256", "reference_sha256", "verify_digest"):
            if sum(node["operation"] == operation and node.get("input_id") == item["id"] for node in graph) != 1:
                raise LabError("Each input needs exactly one hash, reference, and verification")
    manifests = [node for node in graph if node["operation"] == "assemble_manifest"]
    if len(manifests) != 1 or set(manifests[0]["depends_on"]) != {n["id"] for n in graph if n["operation"] == "verify_digest"}:
        raise LabError("Exactly one manifest must cover all input verifications")
    return ordered


def source_manifest():
    result = {}
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if set(relative.parts) & {"evidence", "__pycache__", ".pytest_cache"}:
            continue
        if path.is_file():
            if has_link(path):
                raise LabError("Source snapshot cannot contain links")
            result[relative.as_posix()] = file_hash(path)
    result["@research/__init__.py"] = file_hash(ROOT.parent / "__init__.py")
    return result


def verify_sources(expected):
    validate_source_records(expected)
    if not isinstance(expected, dict) or source_manifest() != expected:
        raise LabError("Source identity changed; create a new plan")


def validate_source_records(expected):
    if not isinstance(expected, dict) or not expected:
        raise LabError("Missing source identity")
    for name, checksum in expected.items():
        if not isinstance(name, str) or not name or (name != "@research/__init__.py" and
                (Path(name).is_absolute() or Path(name).drive or ".." in Path(name).parts or name.startswith("@") or "\\" in name)):
            raise LabError("Unsafe source snapshot reference")
        if not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
            raise LabError("Malformed source SHA-256")
    return expected


def seal_plan(plan):
    result = {k: v for k, v in plan.items() if k != "plan_id"}
    return {**result, "plan_id": digest_object(result)}


def load_plan(path):
    value = read_json(path)
    if not isinstance(value, dict) or value.get("plan_id") != seal_plan(value)["plan_id"]:
        raise LabError("Plan identity mismatch")
    from .planner import validate_plan_record
    validate_plan_record(value)
    return value
