"""Closed registry of bounded local artifact operators; no arbitrary code jobs."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .core import LabError, REVISIONS, SHA256, fields, identifier, integer, strings


REGISTRY = {
    "sha256": {"whole_v1", "stream_v1"},
    "reference_sha256": {"reference_v1"},
    "verify_digest": {"compare_v1"},
    "assemble_manifest": {"manifest_v1"},
}


def required_buffer(operation, candidate, artifact=None):
    if not isinstance(operation, str) or not isinstance(candidate, str) or operation not in REGISTRY or candidate not in REGISTRY[operation]:
        raise LabError("Unregistered operation/candidate combination")
    if operation in {"verify_digest", "assemble_manifest"}:
        return 0
    if not isinstance(artifact, dict) or type(artifact.get("size_bytes")) is not int:
        raise LabError("Digest operator requires a sized input artifact")
    size = artifact["size_bytes"]
    if size < 0:
        raise LabError("Negative artifact size")
    return max(1, size if candidate == "whole_v1" else min(size, 32768 if candidate == "stream_v1" else 8192))


def _digest(artifact, candidate):
    if candidate == "reference_v1":
        return _reference_digest(artifact)
    path = Path(artifact["path"])
    digest = hashlib.sha256()
    size = 0
    capacity = required_buffer("sha256", candidate, artifact)
    buffer = bytearray(capacity)
    view = memoryview(buffer)
    before = path.stat()
    if before.st_size != artifact["size_bytes"]:
        raise LabError("Input size changed after its recorded snapshot")
    # FileIO.readinto fills the one reserved reusable bytearray. There is no
    # Python buffered reader or second chunk allocation in the digest loop.
    with path.open("rb", buffering=0) as handle:
        if candidate == "whole_v1":
            while size < artifact["size_bytes"]:
                amount = handle.readinto(view[size:artifact["size_bytes"]])
                if not amount:
                    raise LabError("Input shrank after its recorded snapshot")
                size += amount
            digest.update(view[:size])
            if handle.readinto(view[:1]):
                raise LabError("Input grew after its recorded snapshot")
        else:
            while amount := handle.readinto(buffer):
                size += amount
                if size > artifact["size_bytes"]:
                    raise LabError("Input grew after its recorded snapshot")
                digest.update(view[:amount])
    return _finish_digest(artifact, path, before, size, digest)


def _reference_digest(artifact):
    """Separate sequential 8KiB reader; the SHA256 provider remains shared."""
    path = Path(artifact["path"])
    before = path.stat()
    if before.st_size != artifact["size_bytes"]:
        raise LabError("Reference input size changed before reading")
    buffer = bytearray(required_buffer("reference_sha256", "reference_v1", artifact))
    view = memoryview(buffer)
    state = hashlib.sha256()
    total = 0
    with path.open("rb", buffering=0) as stream:
        while True:
            count = stream.readinto(view)
            if count == 0:
                break
            total += count
            if total > artifact["size_bytes"]:
                raise LabError("Reference input grew during reading")
            state.update(view[:count])
    return _finish_digest(artifact, path, before, total, state)


def _finish_digest(artifact, path, before, size, digest):
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise LabError("Input changed while the operator read it")
    if size != artifact["size_bytes"]:
        raise LabError("Input size changed after its recorded snapshot")
    if digest.hexdigest() != artifact["sha256"]:
        raise LabError("Input digest changed after its recorded snapshot")
    return {"input_id": artifact["id"], "sha256": digest.hexdigest(), "size_bytes": size}


def execute_operation(job):
    fields(job, ("schema_version", "node", "binding", "input", "dependencies"), label="worker job")
    if type(job["schema_version"]) is not int or job["schema_version"] != 1:
        raise LabError("Unsupported worker job schema")
    node = job["node"]
    fields(node, ("id", "operation", "depends_on", "outputs", "state_reads", "state_writes"), ("input_id",), "worker node")
    identifier(node["id"], "worker task id")
    for key in ("depends_on", "outputs", "state_reads", "state_writes"):
        strings(node[key], "worker " + key)
        if len(set(node[key])) != len(node[key]):
            raise LabError("Duplicate worker graph references")
    fields(job["binding"], ("candidate", "revision", "buffer_bytes", "duration_ns"), label="worker binding")
    candidate = job["binding"]["candidate"]
    if not isinstance(candidate, str) or job["binding"]["revision"] != REVISIONS.get(candidate):
        raise LabError("Unknown registered implementation revision")
    integer(job["binding"]["duration_ns"], 1, 2**63 - 1, "estimated duration_ns")
    artifact = job.get("input")
    if artifact is not None:
        fields(artifact, ("id", "path", "size_bytes", "sha256"), label="worker input")
        identifier(artifact["id"], "worker input id")
        integer(artifact["size_bytes"], 0, 2**40, "worker input size")
        if not isinstance(artifact["path"], str) or not Path(artifact["path"]).is_absolute() or node.get("input_id") != artifact["id"]:
            raise LabError("Worker input identity/path mismatch")
        if not isinstance(artifact["sha256"], str) or not SHA256.fullmatch(artifact["sha256"]):
            raise LabError("Malformed worker input SHA256")
    needed = required_buffer(node["operation"], candidate, artifact)
    if type(job["binding"]["buffer_bytes"]) is not int or job["binding"]["buffer_bytes"] != needed:
        raise LabError("Binding read-buffer reservation does not match registered operator")
    operation = node["operation"]
    dependencies = job.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != set(node["depends_on"]):
        raise LabError("Dependency result inventory mismatch")
    for task_id, value in dependencies.items():
        fields(value, ("task_id", "status", "output", "error"), label="worker dependency result")
        if value["task_id"] != task_id or value["status"] != "succeeded" or value["error"] is not None or not isinstance(value["output"], dict):
            raise LabError("Operator requires successful dependencies")
    if operation in {"sha256", "reference_sha256"}:
        if dependencies:
            raise LabError("Digest operators do not consume dependency state")
        return _digest(artifact, candidate)
    if operation == "verify_digest":
        if artifact is None or len(dependencies) != 2:
            raise LabError("Digest verification needs an input and two independent paths")
        # The controller validates the graph contains a candidate and a
        # reference operator; task names are identifiers, not operation types.
        expected = {"input_id": artifact["id"], "sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"]}
        if any(value.get("output") != expected for value in dependencies.values()):
            raise LabError("Candidate/reference/recorded input digest or size mismatch")
        return {**expected, "passed": True}
    artifacts = []
    seen = set()
    for task_id in sorted(dependencies):
        result = dependencies[task_id]["output"]
        fields(result, ("input_id", "sha256", "size_bytes", "passed"), label="verified artifact")
        if result.get("passed") is not True:
            raise LabError("Manifest requires verified artifact results")
        if result["input_id"] in seen:
            raise LabError("Duplicate artifact in verified manifest")
        seen.add(result["input_id"])
        artifacts.append({key: result[key] for key in ("input_id", "sha256", "size_bytes")})
    return {"artifacts": sorted(artifacts, key=lambda value: value["input_id"])}
