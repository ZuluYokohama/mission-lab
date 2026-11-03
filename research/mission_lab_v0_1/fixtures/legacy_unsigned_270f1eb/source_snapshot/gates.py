"""Controller-side merit gates.

A gate is a node. The controller evaluates a declared predicate over the
receipts of its dependencies and writes a receipt. There is no worker
process, no read-buffer reservation, and no model-budget reservation.

Predicates are a closed set. Nothing in a predicate is executed as code.
`human_approval` reads one JSON record from the run directory. That record
is not signed and not authenticated.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .core import LabError, SHA256, confined_path, digest_object, fields, identifier, integer, read_json, write_json


OUTCOMES = ("pass", "fail", "indeterminate", "pending")
DIGEST_NODES = (2, 8)
MAX_DEPTH = 8
MAX_PREDICATES = 32
MAX_CHILDREN = 16
MAX_DEPENDENCIES = 32
MAX_APPROVALS = 32
MAX_BOUND = 10**18
MIN_BOUND = -(2**31)
LLM_FIELDS = (
    "response_sha256", "content_sha256", "request_sha256",
    "finish_reason", "response_model", "transport", "model",
)
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
LLM_NUMERIC_FIELDS = TOKEN_FIELDS + ("latency_ns", "http_status", "response_bytes", "content_bytes")
NUMERIC_FIELDS = LLM_NUMERIC_FIELDS + ("spend", "exit_code")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_CLASS_MARK = ((":hex:", "hex"), (":alnum:", "alnum"), (":digit:", "digit"), (":any:", "any"))
_RECEIPT_KEYS = ("schema_version", "gate_id", "outcome", "reason", "predicate_sha256")


def normalize_predicate(value):
    """Return a closed predicate or reject the record."""
    count = [0]
    return _normalize(value, 1, count)


def validate_gate_wiring(node, nodes):
    """The predicate may name only direct dependencies, and only where the field can exist."""
    if "input_id" in node:
        raise LabError("Gate has no direct input file")
    deps = node["depends_on"]
    if not 1 <= len(deps) <= MAX_DEPENDENCIES:
        raise LabError("Gate requires 1..32 dependencies")
    known = set(deps)
    if not known <= set(nodes):
        raise LabError("Gate dependency is missing")
    approvers = []
    _check_refs(node["predicate"], nodes, known, approvers)
    if len(approvers) > 1:
        raise LabError("A gate declares one human_approval predicate")


def declared_approver(predicate):
    """The approver id declared by the single human_approval predicate, if any."""
    found = []
    _collect_approver(predicate, found)
    if not found:
        return None
    if len(found) != 1:
        raise LabError("A gate declares one human_approval predicate")
    return found[0]


def approval_document(records):
    """Canonical approvals file. Records are not authenticated."""
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_APPROVALS:
        raise LabError("Approvals require 1..32 records")
    normalized = []
    seen = set()
    for record in records:
        item = validate_approval_record(record)
        if item["gate_id"] in seen:
            raise LabError("Duplicate approval record")
        seen.add(item["gate_id"])
        normalized.append(item)
    normalized.sort(key=lambda item: item["gate_id"])
    return {"schema_version": 1, "records": normalized}


def validate_approval_record(record, gate_id=None):
    fields(record, ("schema_version", "gate_id", "approver_id", "approved_at"), label="approval record")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise LabError("Unsupported approval schema")
    gid = identifier(record["gate_id"], "approval gate id")
    if gate_id is not None and gid != gate_id:
        raise LabError("Approval record names a different gate")
    approver = identifier(record["approver_id"], "approver id")
    return {"schema_version": 1, "gate_id": gid, "approver_id": approver, "approved_at": _timestamp(record["approved_at"])}


def install_approvals(directory, nodes, approvals):
    """Copy supplied records into approvals.json. Unknown gates are rejected.

    A record whose approver id does not match the predicate is still stored.
    The gate then records fail. This does not authenticate the approver.
    """
    if approvals is None:
        return
    if not isinstance(approvals, dict) or not approvals or len(approvals) > MAX_APPROVALS:
        raise LabError("Approvals must map a gate id to one record")
    records = []
    for gate_id, record in approvals.items():
        identifier(gate_id, "approval gate id")
        node = nodes.get(gate_id) if isinstance(nodes, dict) else None
        if not isinstance(node, dict) or node.get("operation") != "gate":
            raise LabError("Approval names an unknown gate")
        if declared_approver(node["predicate"]) is None:
            raise LabError("Approval supplied for a gate that does not declare human_approval")
        if not isinstance(record, dict):
            raise LabError("Approval record must be an object")
        records.append(validate_approval_record(record, gate_id))
    write_json(Path(directory) / "approvals.json", approval_document(records))


def read_approvals(directory):
    """Return ('absent'|'ok'|'malformed', records by gate id).

    A missing file is absent. A file that does not match the approval schema
    is malformed for every gate. This does not authenticate the approver.
    """
    path = Path(directory) / "approvals.json"
    if not path.exists():
        return "absent", {}
    try:
        document = read_json(confined_path(path, [directory]))
        fields(document, ("schema_version", "records"), label="approvals")
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise LabError("Unsupported approval schema")
        indexed = {record["gate_id"]: record for record in approval_document(document["records"])["records"]}
        return "ok", indexed
    except LabError:
        return "malformed", {}


def evidence_from(tasks, observations, directory):
    """Receipts, settled accounts, and recorded exit codes. No new execution."""
    outputs, accounts = {}, {}
    if isinstance(tasks, list):
        rows = tasks
    elif isinstance(tasks, dict):
        rows = list(tasks.values())
    else:
        rows = []
    for task in rows:
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            continue
        outputs[task["task_id"]] = task.get("output") if task.get("status") == "succeeded" else None
        accounts[task["task_id"]] = task.get("model_account")
    exit_codes = {}
    if isinstance(observations, list):
        for row in observations:
            if isinstance(row, dict) and isinstance(row.get("task_id"), str) and type(row.get("exit_code")) is int:
                exit_codes[row["task_id"]] = row["exit_code"]
    status, approvals = read_approvals(directory)
    return {"outputs": outputs, "accounts": accounts, "exit_codes": exit_codes,
            "approval_status": status, "approvals": approvals}


def evaluate(node, evidence):
    """Evaluate one gate. The result is a receipt, not a process status."""
    if not isinstance(node, dict) or node.get("operation") != "gate":
        raise LabError("Gate evaluation requires a gate node")
    if not isinstance(evidence, dict):
        raise LabError("Gate evidence must be an object")
    evidence = {
        "outputs": evidence.get("outputs") if isinstance(evidence.get("outputs"), dict) else {},
        "accounts": evidence.get("accounts") if isinstance(evidence.get("accounts"), dict) else {},
        "exit_codes": evidence.get("exit_codes") if isinstance(evidence.get("exit_codes"), dict) else {},
        "approvals": evidence.get("approvals") if isinstance(evidence.get("approvals"), dict) else {},
        "approval_status": evidence.get("approval_status", "absent"),
    }
    predicate = node["predicate"]
    outcome, reason = _eval(predicate, evidence, node["id"])
    if outcome not in OUTCOMES:
        raise LabError("Gate outcome is unknown")
    receipt = {
        "schema_version": 1,
        "gate_id": node["id"],
        "outcome": outcome,
        "reason": _reason(reason),
        "predicate_sha256": digest_object(predicate),
    }
    return _check_receipt(receipt)


def reevaluate_archived(plan, tasks, observations, run):
    """Recompute every succeeded gate from archived evidence. Blocked gates have no receipt."""
    nodes = {node["id"]: node for node in plan["graph"]}
    by_task = {task["task_id"]: task for task in tasks}
    evidence = evidence_from(tasks, observations, run)
    for node in plan["graph"]:
        if node.get("operation") != "gate":
            continue
        task = by_task[node["id"]]
        if task["status"] == "blocked":
            if task.get("output") is not None:
                raise LabError("Blocked gate recorded a receipt")
            continue
        if task["status"] != "succeeded":
            raise LabError("Gate status is not an evaluation or a block")
        for dep in node["depends_on"]:
            upstream = by_task[dep]
            if upstream.get("status") != "succeeded":
                raise LabError("Gate evaluated without a successful dependency")
            if nodes[dep]["operation"] == "gate":
                upstream_output = upstream.get("output")
                if not isinstance(upstream_output, dict) or upstream_output.get("outcome") != "pass":
                    raise LabError("Gate evaluated without a passing gate dependency")
        output = task.get("output")
        if not isinstance(output, dict) or output.get("predicate_sha256") != digest_object(node["predicate"]):
            raise LabError("Gate predicate does not match the receipt")
        receipt = evaluate(node, evidence)
        if output != receipt:
            raise LabError("Gate receipt does not match the archived evidence")
    return True


def _check_receipt(receipt):
    fields(receipt, _RECEIPT_KEYS, label="gate receipt")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise LabError("Unsupported gate receipt schema")
    identifier(receipt["gate_id"], "gate id")
    if receipt["outcome"] not in OUTCOMES:
        raise LabError("Gate outcome is unknown")
    if not isinstance(receipt["reason"], str) or not receipt["reason"] or len(receipt["reason"]) > 200:
        raise LabError("Gate reason is malformed")
    if not isinstance(receipt["predicate_sha256"], str) or SHA256.fullmatch(receipt["predicate_sha256"]) is None:
        raise LabError("Gate predicate hash is malformed")
    return {key: receipt[key] for key in _RECEIPT_KEYS}


def _normalize(value, depth, count):
    if depth > MAX_DEPTH:
        raise LabError("Gate predicate is too deep")
    count[0] += 1
    if count[0] > MAX_PREDICATES:
        raise LabError("Gate predicate is too large")
    if not isinstance(value, dict) or "kind" not in value or not isinstance(value["kind"], str):
        raise LabError("Gate predicate must be an object")
    kind = value["kind"]
    if kind == "digest_equal":
        fields(value, ("kind", "nodes"), label="digest_equal predicate")
        nodes = value["nodes"]
        if not isinstance(nodes, list) or not DIGEST_NODES[0] <= len(nodes) <= DIGEST_NODES[1]:
            raise LabError("digest_equal requires 2..8 nodes")
        names = [identifier(node, "digest_equal node") for node in nodes]
        if len(set(names)) != len(names):
            raise LabError("digest_equal names a node more than once")
        return {"kind": "digest_equal", "nodes": sorted(names)}
    if kind == "llm_field":
        fields(value, ("kind", "node", "field"), ("equals", "pattern"), label="llm_field predicate")
        node = identifier(value["node"], "llm_field node")
        if value["field"] not in LLM_FIELDS:
            raise LabError("llm_field names an unknown field")
        equals = "equals" in value
        pattern = "pattern" in value
        if equals == pattern:
            raise LabError("llm_field requires equals or pattern")
        record = {"kind": "llm_field", "node": node, "field": value["field"]}
        if equals:
            record["equals"] = _printable(value["equals"], 1, 256, "llm_field equals")
        else:
            record["pattern"] = _compile_pattern(value["pattern"])[0]
        return record
    if kind == "numeric_bounds":
        fields(value, ("kind", "node", "field"), ("minimum", "maximum"), label="numeric_bounds predicate")
        if value["field"] not in NUMERIC_FIELDS:
            raise LabError("numeric_bounds names an unknown field")
        if "minimum" not in value and "maximum" not in value:
            raise LabError("numeric_bounds requires a minimum or a maximum")
        record = {"kind": "numeric_bounds", "node": identifier(value["node"], "numeric_bounds node"), "field": value["field"]}
        if "minimum" in value:
            record["minimum"] = integer(value["minimum"], MIN_BOUND, MAX_BOUND, "gate minimum")
        if "maximum" in value:
            record["maximum"] = integer(value["maximum"], MIN_BOUND, MAX_BOUND, "gate maximum")
        if "minimum" in record and "maximum" in record and record["minimum"] > record["maximum"]:
            raise LabError("Gate numeric bounds are reversed")
        return record
    if kind in ("all", "any"):
        fields(value, ("kind", "predicates"), label=kind + " predicate")
        children = value["predicates"]
        if not isinstance(children, list) or not 1 <= len(children) <= MAX_CHILDREN:
            raise LabError(kind + " requires 1..16 child predicates")
        return {"kind": kind, "predicates": [_normalize(child, depth + 1, count) for child in children]}
    if kind == "human_approval":
        fields(value, ("kind", "approver_id"), label="human_approval predicate")
        return {"kind": "human_approval", "approver_id": identifier(value["approver_id"], "approver id")}
    raise LabError("Unknown gate predicate")


def _check_refs(predicate, nodes, deps, approvers):
    kind = predicate["kind"]
    if kind == "digest_equal":
        for name in predicate["nodes"]:
            _require_dep(name, nodes, deps, "digest_equal")
            if nodes[name]["operation"] == "gate":
                raise LabError("digest_equal cannot read a gate receipt")
        return
    if kind == "llm_field":
        name = predicate["node"]
        _require_dep(name, nodes, deps, "llm_field")
        if nodes[name]["operation"] != "llm:grok":
            raise LabError("llm_field requires an llm:grok dependency")
        return
    if kind == "numeric_bounds":
        name = predicate["node"]
        _require_dep(name, nodes, deps, "numeric_bounds")
        operation = nodes[name]["operation"]
        field = predicate["field"]
        if field in LLM_NUMERIC_FIELDS or field == "spend":
            if operation != "llm:grok":
                raise LabError("numeric_bounds field requires an llm:grok dependency")
        elif field == "exit_code" and operation == "gate":
            raise LabError("exit_code is a worker observation")
        return
    if kind in ("all", "any"):
        for child in predicate["predicates"]:
            _check_refs(child, nodes, deps, approvers)
        return
    if kind == "human_approval":
        approvers.append(predicate["approver_id"])


def _require_dep(name, nodes, deps, label):
    if name not in deps or name not in nodes:
        raise LabError(label + " names a node that is not a dependency")


def _collect_approver(predicate, found):
    if predicate["kind"] == "human_approval":
        found.append(predicate["approver_id"])
    elif predicate["kind"] in ("all", "any"):
        for child in predicate["predicates"]:
            _collect_approver(child, found)


def _eval(predicate, evidence, gate_id):
    kind = predicate["kind"]
    if kind == "digest_equal":
        return _digest_equal(predicate, evidence)
    if kind == "llm_field":
        return _llm_field(predicate, evidence)
    if kind == "numeric_bounds":
        return _numeric(predicate, evidence)
    if kind == "all":
        return _compose(predicate["predicates"], evidence, gate_id, empty_pass=True)
    if kind == "any":
        return _compose(predicate["predicates"], evidence, gate_id, empty_pass=False)
    if kind == "human_approval":
        return _approval(predicate, evidence, gate_id)
    raise LabError("Unknown gate predicate")


def _digest_equal(predicate, evidence):
    rows = []
    for name in predicate["nodes"]:
        row = _digest_row(evidence["outputs"].get(name))
        if row is None:
            return "indeterminate", "digest or byte count is missing"
        rows.append(row)
    if any(row != rows[0] for row in rows):
        return "fail", "digests or byte counts differ"
    return "pass", "digests and byte counts agree"


def _digest_row(output):
    if not isinstance(output, dict):
        return None
    digest = output.get("sha256")
    size = output.get("size_bytes")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None or type(size) is not int or size < 0:
        return None
    return digest, size


def _llm_field(predicate, evidence):
    output = evidence["outputs"].get(predicate["node"])
    if not isinstance(output, dict) or predicate["field"] not in output:
        return "indeterminate", predicate["field"] + " is missing or not text"
    value = output[predicate["field"]]
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(char) < 32 or ord(char) > 126 for char in value):
        return "indeterminate", predicate["field"] + " is missing or not text"
    if "equals" in predicate:
        matched = value == predicate["equals"]
    else:
        matched = _match_pattern(predicate["pattern"], value)
    if matched:
        return "pass", predicate["field"] + " matches"
    return "fail", predicate["field"] + " does not match"


def _numeric(predicate, evidence):
    value = _numeric_value(predicate["field"], predicate["node"], evidence)
    label = predicate["field"]
    if value is None:
        return "indeterminate", label + " is missing"
    if "minimum" in predicate and value < predicate["minimum"]:
        return "fail", label + " is outside bounds"
    if "maximum" in predicate and value > predicate["maximum"]:
        return "fail", label + " is outside bounds"
    return "pass", label + " is within bounds"


def _numeric_value(field, node_id, evidence):
    if field == "exit_code":
        value = evidence["exit_codes"].get(node_id)
        if type(value) is not int:
            return None
        return value
    if field == "spend":
        account = evidence["accounts"].get(node_id)
        if not isinstance(account, dict) or account.get("state") != "settled":
            return None
        actual = account.get("actual")
        if not isinstance(actual, dict) or type(actual.get("spend")) is not int:
            return None
        return actual["spend"]
    output = evidence["outputs"].get(node_id)
    if not isinstance(output, dict):
        return None
    if field in TOKEN_FIELDS:
        usage = output.get("usage")
        if not isinstance(usage, dict) or type(usage.get(field)) is not int:
            return None
        return usage[field]
    value = output.get(field)
    if type(value) is not int:
        return None
    return value


def _compose(children, evidence, gate_id, *, empty_pass):
    results = [_eval(child, evidence, gate_id) for child in children]
    if empty_pass:
        for outcome, reason in results:
            if outcome == "fail":
                return "fail", _reason("child predicate failed: " + reason)
        for outcome, reason in results:
            if outcome == "indeterminate":
                return "indeterminate", _reason("child predicate is indeterminate: " + reason)
        for outcome, reason in results:
            if outcome == "pending":
                return "pending", _reason("child predicate is pending: " + reason)
        return "pass", "all child predicates passed"
    for outcome, reason in results:
        if outcome == "pass":
            return "pass", "a child predicate passed"
    for outcome, reason in results:
        if outcome == "indeterminate":
            return "indeterminate", _reason("child predicate is indeterminate: " + reason)
    for outcome, reason in results:
        if outcome == "pending":
            return "pending", _reason("child predicate is pending: " + reason)
    return "fail", "no child predicate passed"


def _approval(predicate, evidence, gate_id):
    if evidence.get("approval_status") == "malformed":
        return "indeterminate", "approval record is malformed; the record is not authenticated"
    record = evidence.get("approvals", {}).get(gate_id)
    if not isinstance(record, dict):
        return "pending", "approval record is absent; the record is not authenticated"
    if record.get("gate_id") != gate_id or record.get("approver_id") != predicate["approver_id"]:
        return "fail", "approval record does not match the declared approver; the record is not authenticated"
    return "pass", "approval record matches the declared approver; the record is not authenticated"


def _reason(text):
    if not isinstance(text, str):
        text = "gate predicate was not evaluated"
    text = " ".join(text.split())
    if len(text) > 200:
        text = text[:200]
    return text or "gate predicate was not evaluated"


def _printable(value, low, high, label):
    if not isinstance(value, str) or not low <= len(value) <= high or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise LabError(label + " must be printable ASCII")
    return value


def _compile_pattern(pattern):
    """Bounded pattern, not a regular expression.

    Atoms are literals or :hex:, :alnum:, :digit:, :any:. A quantifier is
    {n} or {n,m} with 0..64. At most 32 atoms. The match is against the
    whole value. `re` is not used.
    """
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 128:
        raise LabError("Gate pattern must be 1..128 characters")
    atoms = []
    index = 0
    while index < len(pattern):
        atom = None
        for prefix, name in _CLASS_MARK:
            if pattern.startswith(prefix, index):
                atom = name
                index += len(prefix)
                break
        if atom is None:
            char = pattern[index]
            if char in "{}[]*+?()|^$\\" or ord(char) < 32 or ord(char) > 126:
                raise LabError("Gate pattern has an unsupported character")
            atom = ("lit", char)
            index += 1
        low = high = 1
        if index < len(pattern) and pattern[index] == "{":
            end = pattern.find("}", index)
            if end < 0:
                raise LabError("Gate pattern quantifier is malformed")
            body = pattern[index + 1:end]
            if "," in body:
                left, right = body.split(",", 1)
                if "," in right:
                    raise LabError("Gate pattern quantifier is malformed")
                low, high = _span(left), _span(right)
            else:
                low = high = _span(body)
            index = end + 1
        if low > high:
            raise LabError("Gate pattern quantifier is out of bounds")
        atoms.append((atom, low, high))
        if len(atoms) > 32:
            raise LabError("Gate pattern is too long")
    if not atoms or index != len(pattern):
        raise LabError("Gate pattern is empty")
    return pattern, atoms


def _span(text):
    if not isinstance(text, str) or not text or any(char not in "0123456789" for char in text):
        raise LabError("Gate pattern quantifier is malformed")
    if len(text) > 1 and text[0] == "0":
        raise LabError("Gate pattern quantifier is malformed")
    value = int(text)
    if not 0 <= value <= 64:
        raise LabError("Gate pattern quantifier is out of bounds")
    return value


def _match_pattern(pattern, value):
    _text, atoms = _compile_pattern(pattern)
    if not isinstance(value, str) or len(value) > 256:
        return False
    reachable = {0}
    for atom, low, high in atoms:
        nxt = set()
        for start in reachable:
            for count in range(low, high + 1):
                end = start + count
                if end > len(value):
                    break
                if _atom_span(atom, value, start, end):
                    nxt.add(end)
        reachable = nxt
        if not reachable:
            return False
    return len(value) in reachable


def _atom_span(atom, value, start, end):
    chunk = value[start:end]
    if atom == "hex":
        return all(char in "0123456789abcdef" for char in chunk)
    if atom == "digit":
        return all(char in "0123456789" for char in chunk)
    if atom == "alnum":
        return all(char in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" for char in chunk)
    if atom == "any":
        return all(32 <= ord(char) <= 126 for char in chunk)
    return chunk == atom[1] * (end - start)


def _timestamp(value):
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise LabError("Approval timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise LabError("Approval timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise LabError("Approval timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ")
    return value
