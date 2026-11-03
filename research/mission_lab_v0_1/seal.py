"""HMAC-SHA256 seals for run manifests and approval records.

The key stays in the controller process. A key id is a fingerprint, not the key.
HMAC shows integrity to holders of that key. It is not a public-key signature
and does not by itself prove authorship to someone who lacks the key.

`algorithm` is a string so a later version can accept another scheme, such as
Ed25519, in the same object. This version only verifies `hmac-sha256`. An
unknown algorithm is rejected. Existing hmac-sha256 records stay valid.

The loader reads a key file as bytes. It does not inspect POSIX permission
bits. A symlink is refused. That is not a claim about who else can read the file.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path

from . import core
from .core import (
    CONTROLLER_KEY_ENV,
    SHA256,
    LabError,
    canonical,
    confined_path,
    file_hash,
    has_link,
    read_json,
)


HASH_NOTE = (
    "File hashes bind the run directory to this manifest. "
    "The signature is HMAC-SHA256 over the canonical manifest for holders of the recorded key. "
    "It is not public non-repudiation and it is not proof of semantic truth."
)
# The executor wrote this note before signatures existed. A run that still carries
# HASH_NOTE, or withheld_controller_key_names, is a post-change run even if the
# signature and the seal claim were both removed.
LEGACY_HASH_NOTE = "Integrity only; not source authenticity or proof of semantic truth"
KEY_ID_LABEL = b"mission-lab.key-id.v1"
MIN_KEY_BYTES = 16
MAX_KEY_BYTES = 4096
_ALGORITHM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HEX_VALUE = re.compile(r"^[a-f0-9]{2,512}$")
_SEAL_ENV = "MISSION_LAB_SEAL_KEY"
_SEAL_FILE_ENV = "MISSION_LAB_SEAL_KEY_FILE"
_APPROVER_ENV = "MISSION_LAB_APPROVER_KEY"
_APPROVER_FILE_ENV = "MISSION_LAB_APPROVER_KEY_FILE"
_APPROVER_DIR_ENV = "MISSION_LAB_APPROVER_KEY_DIR"


def key_id(key):
    """Fingerprint. The same key always yields the same id. The key is not recoverable from it."""
    key = _check_key(key, "Seal key")
    return hmac.new(key, KEY_ID_LABEL, hashlib.sha256).hexdigest()


def claim(key):
    """What the semantic record stores. The MAC itself lives only on the manifest."""
    return {"schema_version": 1, "algorithm": "hmac-sha256", "key_id": key_id(key)}


def sign_body(body, key):
    """HMAC over canonical JSON of `body` with any signature field removed."""
    key = _check_key(key, "Seal key")
    payload = _payload(body)
    return {
        "schema_version": 1,
        "algorithm": "hmac-sha256",
        "key_id": key_id(key),
        "value": hmac.new(key, payload, hashlib.sha256).hexdigest(),
    }


def mac_matches(body, key, value):
    """True when `value` is the HMAC of `body`. Mismatched lengths do not match."""
    if not isinstance(value, str):
        return False
    key = _check_key(key, "Seal key")
    expected = hmac.new(key, _payload(body), hashlib.sha256).hexdigest()
    if len(value) != len(expected):
        return False
    return hmac.compare_digest(expected, value)


def sign_approval(record, key):
    """Return a copy of an approval record with an HMAC over the other fields."""
    if not isinstance(record, dict):
        raise LabError("Approval record must be an object")
    body = {field: record[field] for field in record if field != "signature"}
    signed = dict(body)
    signed["signature"] = sign_body(body, key)
    return signed


def approval_mac_matches(record, key):
    if not isinstance(record, dict):
        return False
    signature = record.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "hmac-sha256":
        return False
    if signature.get("key_id") != key_id(key):
        return False
    body = {field: record[field] for field in record if field != "signature"}
    return mac_matches(body, key, signature.get("value"))


def parse_signature_object(value, label="seal signature"):
    """Closed object. Unknown algorithms are still parsed; verification rejects them."""
    from .core import fields
    fields(value, ("schema_version", "algorithm", "key_id", "value"), label=label)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise LabError("Unsupported " + label + " schema")
    algorithm = value["algorithm"]
    if not isinstance(algorithm, str) or _ALGORITHM.fullmatch(algorithm) is None:
        raise LabError("Malformed " + label + " algorithm")
    if not isinstance(value["key_id"], str) or SHA256.fullmatch(value["key_id"]) is None:
        raise LabError("Malformed " + label + " key id")
    raw = value["value"]
    if not isinstance(raw, str) or _HEX_VALUE.fullmatch(raw) is None or len(raw) % 2:
        raise LabError("Malformed " + label + " value")
    return {"schema_version": 1, "algorithm": algorithm, "key_id": value["key_id"], "value": raw}


def load_seal_key():
    """Return the controller seal key, or None when neither source is set.

    Both sources set is an error. An empty or short value is an error.
    """
    return _load_one(_SEAL_ENV, _SEAL_FILE_ENV, "Seal key")


def require_seal_key():
    key = load_seal_key()
    if key is None:
        raise LabError("Seal key is not configured")
    return key


def load_approver_keys():
    """Map approver key id to key bytes. No approver key configured is an empty map."""
    index = {}
    single = _load_one(_APPROVER_ENV, _APPROVER_FILE_ENV, "Approver key")
    if single is not None:
        _remember(index, single)
    directory = os.environ.get(_APPROVER_DIR_ENV)
    if directory:
        _load_key_dir(directory, index)
    return index


def write_integrity(directory, key=None, *, semantic_sha256=None, hash_note=None, sign=True, signature=None):
    """Write integrity.json. `sign` computes an HMAC. A passed `signature` is stored unchanged."""
    directory = Path(directory)
    if sign and signature is not None:
        raise LabError("Seal signature cannot be both computed and supplied")
    files = {}
    for path in sorted(directory.rglob("*")):
        if has_link(path):
            raise LabError("Evidence contains a link")
        if path.is_file() and path != directory / "integrity.json":
            files[path.relative_to(directory).as_posix()] = file_hash(path)
    body = {"schema_version": 1, "files": files}
    if semantic_sha256 is not None:
        body["semantic_sha256"] = semantic_sha256
    if hash_note is not None:
        body["hash_note"] = hash_note
    if sign:
        if key is None:
            raise LabError("Seal key is not configured")
        body["signature"] = sign_body(body, key)
    elif signature is not None:
        body["signature"] = signature
    target = directory / "integrity.json"
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical(body))
    return body


def reseal(directory):
    """Recompute file hashes and sign again with the controller key."""
    directory = Path(directory)
    previous = read_json(directory / "integrity.json")
    semantic_sha256 = file_hash(directory / "semantic.json") if "semantic_sha256" in previous else None
    hash_note = previous.get("hash_note") if "hash_note" in previous else None
    return write_integrity(directory, require_seal_key(), semantic_sha256=semantic_sha256, hash_note=hash_note, sign=True)


def rewrite_unsigned(directory, *, signature=None):
    """Recompute hashes without a new HMAC. `signature` is copied through unchanged when given."""
    directory = Path(directory)
    previous = read_json(directory / "integrity.json")
    semantic_sha256 = file_hash(directory / "semantic.json") if "semantic_sha256" in previous else None
    hash_note = previous.get("hash_note") if "hash_note" in previous else None
    return write_integrity(
        directory, sign=False, signature=signature, semantic_sha256=semantic_sha256, hash_note=hash_note)


def verify_directory(output, *, allow_legacy_unsigned=False):
    """Check file bytes, then the HMAC. Return the directory and a status record.

    File changes are reported before signature failures. Missing key, wrong key,
    a bad HMAC, a stripped signature, and an unsigned legacy seal are different errors.
    """
    from .core import fields
    # Read ROOT at call time. Tests replace core.ROOT; a copied binding would not follow.
    output = confined_path(output, [core.ROOT / "evidence"])
    manifest = read_json(output / "integrity.json")
    fields(manifest, ("schema_version", "files"), ("semantic_sha256", "hash_note", "signature"), label="integrity")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or not isinstance(manifest["files"], dict):
        raise LabError("Malformed evidence integrity manifest")
    actual = set()
    for path in output.rglob("*"):
        if has_link(path):
            raise LabError("Evidence contains a link")
        if path.is_file() and path != output / "integrity.json":
            actual.add(path.relative_to(output).as_posix())
    if actual != set(manifest["files"]):
        raise LabError("Evidence file inventory changed")
    for relative, expected in manifest["files"].items():
        path = confined_path(output / relative, [output])
        if not isinstance(expected, str) or file_hash(path) != expected:
            raise LabError("Archived evidence changed: " + relative)
    if "semantic_sha256" in manifest and manifest["semantic_sha256"] != file_hash(output / "semantic.json"):
        raise LabError("Semantic evidence hash mismatch")
    status = _verify_signature(output, manifest, allow_legacy_unsigned=allow_legacy_unsigned)
    return output, status


def lineage_record(directory, status):
    """Parent pointer stored on a later plan or run. The hash is the integrity file's bytes."""
    if not isinstance(status, dict) or status.get("status") not in ("verified", "unsigned (legacy)"):
        raise LabError("Parent seal was not checked")
    record = {
        "schema_version": 1,
        "seal_sha256": file_hash(Path(directory) / "integrity.json"),
        "algorithm": "hmac-sha256" if status["status"] == "verified" else "unsigned",
    }
    if status.get("key_id"):
        record["key_id"] = status["key_id"]
    return record


def matching_lineage(child, parent, *, allow_legacy_unsigned=False):
    """Compare a child's recorded parent hash with the parent seal. Replay does not call this.

    The parent directory's plan id must be the plan id the child recorded.
    """
    parent, status = verify_directory(parent, allow_legacy_unsigned=allow_legacy_unsigned)
    parent_hash = file_hash(parent / "integrity.json")
    parent_plan = read_json(parent / "plan.json")
    parent_plan_id = parent_plan.get("plan_id") if isinstance(parent_plan, dict) else None
    semantic = read_json(Path(child) / "semantic.json")
    if not isinstance(semantic, dict):
        raise LabError("Recorded lineage does not match the parent seal")
    found = []
    resume = semantic.get("resume_of")
    if isinstance(resume, dict) and resume.get("prior_seal_sha256") == parent_hash:
        if resume.get("plan_id") != parent_plan_id:
            raise LabError("Prior run does not belong to the prior plan")
        _recorded_key(resume.get("prior_seal_key_id"), status)
        found.append("resume_of")
    revision = semantic.get("revision")
    prior = revision.get("prior_seal") if isinstance(revision, dict) else None
    if isinstance(prior, dict) and prior.get("seal_sha256") == parent_hash:
        if not isinstance(revision, dict) or revision.get("prior_plan_id") != parent_plan_id:
            raise LabError("Prior run does not belong to the prior plan")
        _recorded_key(prior.get("key_id"), status)
        found.append("revision")
    if not found:
        raise LabError("Recorded lineage does not match the parent seal")
    return {"parent_seal_sha256": parent_hash, "records": found, "parent_signature_status": status["status"]}


def _verify_signature(output, manifest, *, allow_legacy_unsigned):
    claim = _seal_claim(output)
    signature = manifest.get("signature")
    if signature is None:
        if claim is not None or _post_change_run(output, manifest):
            raise LabError("Resealed run has no valid signature")
        if allow_legacy_unsigned:
            return {"status": "unsigned (legacy)", "algorithm": None, "key_id": None}
        raise LabError("Unsigned seal")
    parsed = parse_signature_object(signature)
    if parsed["algorithm"] != "hmac-sha256":
        raise LabError("Unsupported seal signature algorithm")
    if len(parsed["value"]) != 64:
        raise LabError("Malformed seal signature value")
    key = load_seal_key()
    if key is None:
        raise LabError("Seal key is not available")
    if parsed["key_id"] != key_id(key):
        raise LabError("Seal key does not match the recorded key id")
    if not mac_matches(_body(manifest), key, parsed["value"]):
        raise LabError("Seal signature is not valid")
    if claim is not None and (claim["algorithm"] != parsed["algorithm"] or claim["key_id"] != parsed["key_id"]):
        raise LabError("Seal claim does not match the signature")
    return {"status": "verified", "algorithm": parsed["algorithm"], "key_id": parsed["key_id"]}


def _post_change_run(output, manifest):
    """True when the directory still has a marker this version writes and the pre-change executor did not.

    The HMAC hash note and withheld_controller_key_names survive deletion of the
    signature and the seal claim. A directory rewritten to the pre-change shape
    has neither marker and is not distinguishable here.
    """
    if manifest.get("hash_note") == HASH_NOTE:
        return True
    path = Path(output) / "environment.json"
    if not path.is_file():
        return False
    try:
        document = read_json(path)
    except LabError:
        return False
    worker = document.get("worker_environment") if isinstance(document, dict) else None
    return isinstance(worker, dict) and "withheld_controller_key_names" in worker


def _seal_claim(output):
    path = Path(output) / "semantic.json"
    if not path.is_file():
        return None
    semantic = read_json(path)
    if not isinstance(semantic, dict) or "seal" not in semantic:
        return None
    from .core import fields
    value = semantic["seal"]
    fields(value, ("schema_version", "algorithm", "key_id"), label="seal claim")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise LabError("Unsupported seal claim schema")
    if value["algorithm"] != "hmac-sha256":
        raise LabError("Unsupported seal claim algorithm")
    if not isinstance(value["key_id"], str) or SHA256.fullmatch(value["key_id"]) is None:
        raise LabError("Malformed seal claim key id")
    return {"schema_version": 1, "algorithm": value["algorithm"], "key_id": value["key_id"]}


def _recorded_key(recorded, status):
    if status["status"] == "verified":
        if recorded != status["key_id"]:
            raise LabError("Recorded lineage key id does not match the parent seal")
        return
    if recorded is not None:
        raise LabError("Recorded lineage key id does not match the parent seal")


def _body(manifest):
    return {key: value for key, value in manifest.items() if key != "signature"}


def _payload(body):
    return canonical(_body(body) if isinstance(body, dict) else body).encode("utf-8")


def _check_key(key, label):
    if not isinstance(key, bytes) or not MIN_KEY_BYTES <= len(key) <= MAX_KEY_BYTES:
        raise LabError(label + " must be 16..4096 bytes")
    return key


def _load_one(env_name, file_name, label):
    has_env = env_name in os.environ
    has_file = file_name in os.environ
    if has_env and has_file:
        raise LabError(label + " source is ambiguous")
    if has_env:
        return _check_key(_utf8(os.environ[env_name], label), label)
    if has_file:
        return _read_key_file(os.environ[file_name], label)
    return None


def _utf8(value, label):
    if not isinstance(value, str):
        raise LabError(label + " must be text")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise LabError(label + " must be UTF-8") from None


def _read_key_file(name, label):
    path = Path(name)
    if not path.is_absolute():
        path = Path.cwd() / path
    if has_link(path):
        raise LabError(label + " file must not be a link")
    if not path.is_file():
        raise LabError(label + " file is missing")
    size = path.stat().st_size
    if not MIN_KEY_BYTES <= size <= MAX_KEY_BYTES:
        raise LabError(label + " must be 16..4096 bytes")
    return _check_key(path.read_bytes(), label)


def _load_key_dir(name, index):
    path = Path(name)
    if not path.is_absolute():
        path = Path.cwd() / path
    if has_link(path):
        raise LabError("Approver key directory must not be a link")
    if not path.is_dir():
        raise LabError("Approver key directory is missing")
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if has_link(child):
            raise LabError("Approver key file must not be a link")
        if child.is_dir() or not child.is_file():
            continue
        _remember(index, _read_key_file(str(child), "Approver key"))


def _remember(index, key):
    fingerprint = key_id(key)
    current = index.get(fingerprint)
    if current is not None and current != key:
        raise LabError("Approver key id collision")
    index[fingerprint] = key


def controller_key_names():
    """Sorted names the controller refuses to grant. Values are not included."""
    return tuple(sorted(CONTROLLER_KEY_ENV))
