"""One registered xAI chat-completions call, or a local fixture with the same receipt.

The mission names the model and the bounds. Transport `xai` posts to the fixed
Chat Completions URL with urllib. The bearer token is read from `XAI_API_KEY`
in the worker environment and is never written to a receipt. Transport
`fixture` reads a declared file and does not open a socket. Replay checks
archived bytes; it does not call this module's HTTP transport.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .core import LabError, SHA256, _pairs, confined_path, file_hash, fields, has_link, identifier, integer


XAI_API_KEY = "XAI_API_KEY"
XAI_CHAT_COMPLETIONS_URL = "https://api.x.ai/v1/chat/completions"
# Not copied unless a task grants them. urllib uses *_proxy / no_proxy;
# the default SSL context uses SSL_CERT_FILE and SSL_CERT_DIR.
TRANSPORT_ENV_NAMES = ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_DIR", "SSL_CERT_FILE")
MAX_PROMPT_BYTES = 4096
MAX_OUTPUT_TOKENS = 4096
MAX_RESPONSE_BYTES = 65536
MAX_REQUEST_TIMEOUT = 120
MIN_REQUEST_TIMEOUT = 0.1
TIMEOUT_MARGIN_SECONDS = 1
MAX_USAGE_TOKENS = 10**9
MODEL_ID = re.compile(r"^grok-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RECEIPT_FIELDS = (
    "model", "transport", "request", "request_sha256", "response_sha256", "response_bytes",
    "http_status", "latency_ns", "usage", "response_model", "finish_reason", "content_sha256", "content_bytes",
)
REQUEST_FIELDS = (
    "model", "max_output_tokens", "temperature", "timeout_seconds", "max_response_bytes",
    "prompt_sha256", "prompt_bytes",
)
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def normalize_llm_fields(node):
    """Fail closed on the scalar contract. Canonicalize temperature in place."""
    model = node["model"]
    if not isinstance(model, str) or MODEL_ID.fullmatch(model) is None:
        raise LabError("llm:grok model must be a grok model id")
    integer(node["max_output_tokens"], 1, MAX_OUTPUT_TOKENS, "max_output_tokens")
    if "max_input_tokens" in node:
        integer(node["max_input_tokens"], 1, MAX_USAGE_TOKENS, "max_input_tokens")
    node["temperature"] = _temperature(node["temperature"])
    timeout = node["timeout_seconds"]
    if isinstance(timeout, bool) or type(timeout) not in (int, float) or not math.isfinite(timeout):
        raise LabError("llm:grok timeout_seconds must be finite")
    if not MIN_REQUEST_TIMEOUT <= timeout <= MAX_REQUEST_TIMEOUT:
        raise LabError("llm:grok timeout_seconds must be in [0.1, 120]")
    integer(node["max_response_bytes"], 1, MAX_RESPONSE_BYTES, "max_response_bytes")
    if node["transport"] not in ("xai", "fixture"):
        raise LabError("llm:grok transport must be xai or fixture")
    has_prompt = "prompt" in node
    has_file = "input_id" in node
    if has_prompt == has_file:
        raise LabError("llm:grok requires exactly one of prompt text or a prompt file")
    if has_prompt:
        _prompt_text(node["prompt"])
    else:
        identifier(node["input_id"], "llm prompt input id")
    if node["transport"] == "fixture":
        if "fixture_id" not in node:
            raise LabError("llm:grok transport fixture requires fixture_id")
        identifier(node["fixture_id"], "llm fixture id")
        if node.get("input_id") == node["fixture_id"]:
            raise LabError("llm:grok fixture_id must differ from the prompt file")
    elif "fixture_id" in node:
        raise LabError("llm:grok transport xai does not accept fixture_id")
    return node


def validate_llm_wiring(node, nodes, inputs):
    """Require verification of every file this call will read."""
    known = {item["id"]: item for item in inputs}
    needed = []
    if "input_id" in node:
        artifact = known.get(node["input_id"])
        if artifact is None:
            raise LabError("Unknown grok prompt input")
        if not 1 <= artifact["size_bytes"] <= MAX_PROMPT_BYTES:
            raise LabError("Grok prompt file must be 1..4096 bytes")
        needed.append(_verify_id(nodes, node["input_id"], "prompt"))
    if node["transport"] == "fixture":
        artifact = known.get(node["fixture_id"])
        if artifact is None:
            raise LabError("Unknown grok fixture input")
        if not 1 <= artifact["size_bytes"] <= node["max_response_bytes"]:
            raise LabError("Grok fixture exceeds max_response_bytes")
        needed.append(_verify_id(nodes, node["fixture_id"], "fixture"))
    if sorted(node["depends_on"]) != sorted(needed):
        raise LabError("llm:grok dependencies must be the verifications of its prompt and fixture files")


def check_llm_plan(resources, graph, inputs, *, read_files):
    """Grants, timeout margin, and optional fixture parsing. Values are not stored."""
    _reject_key_alias(resources)
    _check_grants(resources, graph)
    limit = resources["timeout_seconds"]
    for node in graph:
        if node.get("operation") != "llm:grok":
            continue
        if node["timeout_seconds"] + TIMEOUT_MARGIN_SECONDS > limit:
            raise LabError("llm:grok timeout_seconds must be at least one second below the resource profile timeout")
        if not read_files:
            continue
        if "input_id" in node:
            _prompt_text(_decode_prompt(read_declared_file(inputs[node["input_id"]], MAX_PROMPT_BYTES)))
        if node["transport"] == "fixture":
            parse_completion(read_declared_file(inputs[node["fixture_id"]], node["max_response_bytes"]), node["max_response_bytes"])


def request_bytes(model, prompt, max_output_tokens, temperature):
    """Exact POST body. Replay rebuilds this byte string and hashes it again."""
    document = {
        "max_tokens": max_output_tokens,
        "messages": [{"content": prompt, "role": "user"}],
        "model": model,
        "stream": False,
        "temperature": temperature,
    }
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def execute(job):
    """Run one call. Capture files receive the response body and assistant text."""
    node = job["node"]
    if node["transport"] == "fixture" and "fixture" not in job:
        raise LabError("Grok fixture file is missing")
    if node["transport"] != "fixture" and "fixture" in job:
        raise LabError("llm:grok transport xai does not accept a fixture input")
    _require_verified_file(job, job.get("input"))
    if node["transport"] == "fixture":
        _require_verified_file(job, job.get("fixture"))
    prompt = _job_prompt(job)
    if node["transport"] == "fixture":
        body, latency_ns = _fixture_exchange(job, node["max_response_bytes"])
        status = 200
    else:
        api_key = _api_key()
        started = time.perf_counter_ns()
        status, body = urllib_chat(
            request_bytes(node["model"], prompt, node["max_output_tokens"], node["temperature"]),
            api_key, node["timeout_seconds"], node["max_response_bytes"])
        latency_ns = time.perf_counter_ns() - started
        if api_key.encode("utf-8") in body:
            raise LabError("Grok response contains the API key")
    parsed = parse_completion(body, node["max_response_bytes"])
    content = parsed["content"].encode("utf-8")
    response_path, content_path = _capture_paths(job)
    _write_new(response_path, body)
    _write_new(content_path, content)
    prompt_bytes = prompt.encode("utf-8")
    receipt = {
        "model": node["model"],
        "transport": node["transport"],
        "request": {
            "model": node["model"],
            "max_output_tokens": node["max_output_tokens"],
            "temperature": node["temperature"],
            "timeout_seconds": node["timeout_seconds"],
            "max_response_bytes": node["max_response_bytes"],
            "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
            "prompt_bytes": len(prompt_bytes),
        },
        "request_sha256": hashlib.sha256(request_bytes(
            node["model"], prompt, node["max_output_tokens"], node["temperature"])).hexdigest(),
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "response_bytes": len(body),
        "http_status": status,
        "latency_ns": latency_ns,
        "usage": parsed["usage"],
        "response_model": parsed["response_model"],
        "finish_reason": parsed["finish_reason"],
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "content_bytes": len(content),
    }
    check_receipt(receipt, node=node, prompt=prompt, response=body, content=content)
    return receipt


def urllib_chat(payload, api_key, timeout, max_bytes, url=XAI_CHAT_COMPLETIONS_URL, opener=None):
    """POST one JSON body. Redirects are refused. The key is not returned."""
    _check_url(url)
    _check_api_key(api_key)
    if isinstance(timeout, bool) or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise LabError("Grok request timeout is invalid")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "mission-lab-grok-operator",
        })
    if opener is None:
        opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            body = read_bounded(response, max_bytes)
    except LabError:
        raise
    except TimeoutError:
        raise LabError("Grok request timed out") from None
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            read_bounded(error, max_bytes)
        except LabError as bounded:
            raise LabError(str(bounded)) from None
        raise LabError("Grok request failed with HTTP " + str(status)) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise LabError("Grok request timed out") from None
        raise LabError("Grok request failed before a response") from None
    except OSError:
        raise LabError("Grok request failed before a response") from None
    if type(status) is not int or not 200 <= status <= 299:
        raise LabError("Grok request failed with HTTP " + str(status))
    return status, body


def read_bounded(stream, limit):
    """Read at most `limit` bytes. One extra byte detects overflow and is discarded."""
    integer(limit, 1, MAX_RESPONSE_BYTES, "max_response_bytes")
    if hasattr(stream, "readinto"):
        buffer = bytearray(limit + 1)
        view = memoryview(buffer)
        size = 0
        while size <= limit:
            amount = stream.readinto(view[size:limit + 1])
            if not amount:
                break
            size += amount
        if size > limit:
            raise LabError("Grok response exceeded max_response_bytes")
        return bytes(view[:size])
    # urllib HTTPError exposes read() and not readinto. The cap still applies.
    buffer = bytearray()
    while len(buffer) <= limit:
        chunk = stream.read(min(8192, limit - len(buffer) + 1))
        if not chunk:
            break
        buffer.extend(chunk)
    if len(buffer) > limit:
        raise LabError("Grok response exceeded max_response_bytes")
    return bytes(buffer)


def parse_completion(body, max_bytes):
    """Accept one non-streaming chat completion. Extra usage fields are not recorded."""
    if len(body) > max_bytes:
        raise LabError("Grok response exceeded max_response_bytes")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda item: (_ for _ in ()).throw(LabError("Non-finite JSON: " + item)))
    except LabError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise LabError("Grok response was not valid JSON") from None
    if not isinstance(value, dict):
        raise LabError("Grok response was not a JSON object")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise LabError("Grok response did not contain one choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant" or not isinstance(message.get("content"), str):
        raise LabError("Grok response message was malformed")
    finish = choice.get("finish_reason")
    if not isinstance(finish, str) or not 1 <= len(finish) <= 64 or any(ord(char) < 32 or ord(char) > 126 for char in finish):
        raise LabError("Grok response finish_reason was malformed")
    response_model = value.get("model")
    if not isinstance(response_model, str) or not 1 <= len(response_model) <= 128 or any(ord(char) < 32 or ord(char) > 126 for char in response_model):
        raise LabError("Grok response model was malformed")
    usage = value.get("usage")
    if not isinstance(usage, dict):
        raise LabError("Grok response usage was malformed")
    recorded = {}
    for key in USAGE_FIELDS:
        number = usage.get(key)
        if type(number) is not int or not 0 <= number <= MAX_USAGE_TOKENS:
            raise LabError("Grok response usage was malformed")
        recorded[key] = number
    content = message["content"]
    if len(content.encode("utf-8")) > max_bytes:
        raise LabError("Grok response content exceeded max_response_bytes")
    return {"content": content, "finish_reason": finish, "response_model": response_model, "usage": recorded}


def check_receipt(receipt, *, node, prompt, response, content):
    """Recompute hashes and compare them to the recorded conclusion."""
    fields(receipt, RECEIPT_FIELDS, label="grok receipt")
    if receipt["model"] != node["model"] or receipt["transport"] != node["transport"]:
        raise LabError("Grok receipt does not match the declared call")
    if receipt["http_status"] != 200 or type(receipt["http_status"]) is not int:
        raise LabError("Grok receipt HTTP status is not a completed response")
    if type(receipt["latency_ns"]) is not int or receipt["latency_ns"] < 0:
        raise LabError("Grok receipt latency is malformed")
    request = receipt["request"]
    fields(request, REQUEST_FIELDS, label="grok request receipt")
    if request["model"] != node["model"] or request["max_output_tokens"] != node["max_output_tokens"]:
        raise LabError("Grok request receipt does not match declared parameters")
    if request["temperature"] != node["temperature"] or request["timeout_seconds"] != node["timeout_seconds"]:
        raise LabError("Grok request receipt does not match declared parameters")
    if request["max_response_bytes"] != node["max_response_bytes"]:
        raise LabError("Grok request receipt does not match declared parameters")
    prompt_bytes = prompt.encode("utf-8")
    if request["prompt_bytes"] != len(prompt_bytes) or request["prompt_sha256"] != hashlib.sha256(prompt_bytes).hexdigest():
        raise LabError("Grok prompt hash does not match the prompt bytes")
    sent = request_bytes(node["model"], prompt, node["max_output_tokens"], node["temperature"])
    if receipt["request_sha256"] != hashlib.sha256(sent).hexdigest():
        raise LabError("Grok request hash does not match the rebuilt body")
    if not isinstance(response, (bytes, bytearray)) or receipt["response_bytes"] != len(response):
        raise LabError("Grok response size does not match the body")
    if receipt["response_sha256"] != hashlib.sha256(response).hexdigest() or SHA256.fullmatch(receipt["response_sha256"]) is None:
        raise LabError("Grok response hash does not match the body")
    if not isinstance(content, (bytes, bytearray)) or receipt["content_bytes"] != len(content):
        raise LabError("Grok content size does not match the artifact")
    if receipt["content_sha256"] != hashlib.sha256(content).hexdigest() or SHA256.fullmatch(receipt["content_sha256"]) is None:
        raise LabError("Grok content hash does not match the artifact")
    parsed = parse_completion(bytes(response), node["max_response_bytes"])
    if parsed["content"].encode("utf-8") != bytes(content):
        raise LabError("Grok content artifact does not match the response message")
    if receipt["usage"] != parsed["usage"] or receipt["finish_reason"] != parsed["finish_reason"]:
        raise LabError("Grok receipt does not match the response body")
    if receipt["response_model"] != parsed["response_model"]:
        raise LabError("Grok receipt does not match the response body")
    return receipt


def accept_worker_output(output, job):
    """Check a worker receipt against the capture files it claims to have written."""
    response_path, content_path = _capture_paths(job, exists=True)
    check_receipt(output, node=job["node"], prompt=_job_prompt(job), response=response_path.read_bytes(), content=content_path.read_bytes())
    return output


def promote_artifacts(directory, receipt, job):
    """Move capture files to hash-named evidence. Refuse a body that contains the parent key."""
    response_path, content_path = _capture_paths(job, exists=True)
    response = response_path.read_bytes()
    content = content_path.read_bytes()
    secret = os.environ.get(XAI_API_KEY)
    if isinstance(secret, str) and secret:
        blob = json.dumps(receipt, sort_keys=True).encode("utf-8")
        if secret.encode("utf-8") in blob or secret.encode("utf-8") in response or secret.encode("utf-8") in content:
            raise LabError("Grok evidence contains the API key")
    check_receipt(receipt, node=job["node"], prompt=_job_prompt(job), response=response, content=content)
    _write_hash_artifact(directory, "llm_responses", receipt["response_sha256"], response)
    _write_hash_artifact(directory, "llm_content", receipt["content_sha256"], content)
    response_path.unlink()
    content_path.unlink()


def verify_archived(run, node, receipt, inputs, input_receipts):
    """Replay one succeeded call from archived bytes. No socket is opened."""
    prompt = _archived_prompt(run, node, input_receipts)
    response = _archived_artifact(run, "llm_responses", receipt.get("response_sha256"))
    content = _archived_artifact(run, "llm_content", receipt.get("content_sha256"))
    check_receipt(receipt, node=node, prompt=prompt, response=response, content=content)
    if node["transport"] == "fixture":
        fixture = _archived_input(run, input_receipts, node["fixture_id"])
        declared = inputs[node["fixture_id"]]
        if file_hash(fixture) != declared["sha256"] or fixture.read_bytes() != response:
            raise LabError("Fixture response artifact does not match the archived fixture")
    return receipt


def read_declared_file(artifact, limit):
    """Read one immutable declared file into a single reserved buffer."""
    if not isinstance(artifact, dict):
        raise LabError("Grok file input is missing")
    fields(artifact, ("id", "path", "size_bytes", "sha256"), label="grok file")
    integer(artifact["size_bytes"], 0, limit, "grok file size")
    if not isinstance(artifact["sha256"], str) or SHA256.fullmatch(artifact["sha256"]) is None:
        raise LabError("Malformed grok file SHA256")
    path = Path(artifact["path"])
    if not isinstance(artifact["path"], str) or not path.is_absolute():
        raise LabError("Grok file path must be absolute")
    before = path.stat()
    if before.st_size != artifact["size_bytes"]:
        raise LabError("Input size changed after its recorded snapshot")
    buffer = bytearray(max(1, artifact["size_bytes"]))
    view = memoryview(buffer)
    size = 0
    with path.open("rb", buffering=0) as handle:
        while size < artifact["size_bytes"]:
            amount = handle.readinto(view[size:artifact["size_bytes"]])
            if not amount:
                raise LabError("Input shrank after its recorded snapshot")
            size += amount
        if artifact["size_bytes"] and handle.readinto(view[:1]):
            raise LabError("Input grew after its recorded snapshot")
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise LabError("Input changed while the operator read it")
    data = bytes(view[:size])
    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise LabError("Input digest changed after its recorded snapshot")
    return data


def llm_input_ids(graph):
    found = set()
    for node in graph:
        if not isinstance(node, dict) or node.get("operation") != "llm:grok":
            continue
        if isinstance(node.get("input_id"), str):
            found.add(node["input_id"])
        if isinstance(node.get("fixture_id"), str):
            found.add(node["fixture_id"])
    return found


def read_reservation(node, inputs):
    if not isinstance(node, dict) or not isinstance(inputs, dict):
        raise LabError("Grok buffer reservation is missing its inputs")
    size = 0
    if "input_id" in node:
        artifact = inputs.get(node["input_id"])
        if not isinstance(artifact, dict) or type(artifact.get("size_bytes")) is not int:
            raise LabError("Grok prompt file is missing its size")
        size = max(size, artifact["size_bytes"])
    if node.get("transport") == "fixture":
        artifact = inputs.get(node.get("fixture_id"))
        if not isinstance(artifact, dict) or type(artifact.get("size_bytes")) is not int:
            raise LabError("Grok fixture file is missing its size")
        size = max(size, artifact["size_bytes"])
    return size


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LabError("Grok endpoint redirect is refused")


def _temperature(value):
    if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(value):
        raise LabError("temperature must be a multiple of 0.01 in [0, 2]")
    if not 0 <= value <= 2:
        raise LabError("temperature must be a multiple of 0.01 in [0, 2]")
    scaled = value * 100
    if abs(scaled - round(scaled)) > 1e-6:
        raise LabError("temperature must be a multiple of 0.01 in [0, 2]")
    scaled_i = int(round(scaled))
    if scaled_i % 100 == 0:
        return scaled_i // 100
    return scaled_i / 100


def _prompt_text(value):
    if not isinstance(value, str) or "\x00" in value:
        raise LabError("Grok prompt must be text without NUL")
    size = len(value.encode("utf-8"))
    if not 1 <= size <= MAX_PROMPT_BYTES:
        raise LabError("Grok prompt must be 1..4096 UTF-8 bytes")
    return value


def _decode_prompt(data):
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise LabError("Grok prompt file is not UTF-8") from None


def _verify_id(nodes, input_id, role):
    matches = [node["id"] for node in nodes.values()
               if node.get("operation") == "verify_digest" and node.get("input_id") == input_id]
    if len(matches) != 1:
        raise LabError("Grok " + role + " input lacks exactly one verification")
    return matches[0]


def _worker_env(resources):
    worker_env = resources.get("worker_env") if isinstance(resources, dict) else None
    if not isinstance(worker_env, dict):
        return {"declared": [], "grants": {}}
    declared = worker_env.get("declared")
    grants = worker_env.get("grants")
    if not isinstance(declared, list) or not isinstance(grants, dict):
        return {"declared": [], "grants": {}}
    return worker_env


def _reject_key_alias(resources):
    for name in _worker_env(resources)["declared"]:
        if isinstance(name, str) and name.casefold() == XAI_API_KEY.casefold() and name != XAI_API_KEY:
            raise LabError("XAI_API_KEY must use that exact spelling")


def _check_grants(resources, graph):
    grants = _worker_env(resources)["grants"]
    nodes = {node["id"]: node for node in graph if isinstance(node, dict) and isinstance(node.get("id"), str)}
    for node in nodes.values():
        if node.get("operation") != "llm:grok":
            continue
        names = grants.get(node["id"], ())
        granted = set(names) if isinstance(names, list) else set()
        if node["transport"] == "xai" and XAI_API_KEY not in granted:
            raise LabError("llm:grok transport xai requires an XAI_API_KEY grant: " + node["id"])
        if node["transport"] != "xai" and XAI_API_KEY in granted:
            raise LabError("llm:grok fixture transport cannot receive XAI_API_KEY: " + node["id"])
    for task_id, names in grants.items():
        if not isinstance(names, list) or XAI_API_KEY not in names:
            continue
        node = nodes.get(task_id)
        if node is None or node.get("operation") != "llm:grok" or node.get("transport") != "xai":
            raise LabError("XAI_API_KEY can be granted only to an llm:grok task that uses transport xai: " + task_id)


def _api_key():
    value = os.environ.get(XAI_API_KEY)
    _check_api_key(value)
    return value


def _check_api_key(value):
    if not isinstance(value, str) or not value:
        raise LabError("XAI_API_KEY is not set")
    if any(char.isspace() for char in value) or len(value) > 4096:
        raise LabError("XAI_API_KEY is malformed")


def _check_url(url):
    if url == XAI_CHAT_COMPLETIONS_URL:
        return
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise LabError("Grok transport URL is not permitted")
    if parsed.username or parsed.password or parsed.fragment:
        raise LabError("Grok transport URL is not permitted")


def _require_verified_file(job, artifact):
    if artifact is None:
        return
    expected = {"input_id": artifact["id"], "sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"]}
    matched = False
    for value in job.get("dependencies", {}).values():
        output = value.get("output") if isinstance(value, dict) else None
        if not isinstance(output, dict) or output.get("input_id") != artifact["id"]:
            continue
        if {key: output.get(key) for key in expected} != expected or output.get("passed") is not True:
            raise LabError("Grok dependency verification does not match the declared file")
        matched = True
    if not matched:
        raise LabError("Grok dependency verification does not match the declared file")


def _job_prompt(job):
    node = job["node"]
    if "prompt" in node:
        return _prompt_text(node["prompt"])
    artifact = job.get("input")
    if not isinstance(artifact, dict) or artifact.get("id") != node.get("input_id"):
        raise LabError("Grok prompt file is missing")
    return _prompt_text(_decode_prompt(read_declared_file(artifact, MAX_PROMPT_BYTES)))


def _fixture_exchange(job, limit):
    node = job["node"]
    artifact = job.get("fixture")
    if not isinstance(artifact, dict) or artifact.get("id") != node.get("fixture_id"):
        raise LabError("Grok fixture file is missing")
    started = time.perf_counter_ns()
    body = read_declared_file(artifact, limit)
    return body, time.perf_counter_ns() - started


def _capture_paths(job, *, exists=False):
    from .core import ROOT
    captures = job.get("captures")
    fields(captures, ("response_path", "content_path"), label="grok captures")
    paths = []
    for key in ("response_path", "content_path"):
        raw = captures[key]
        if not isinstance(raw, str):
            raise LabError("Grok capture path must be a string")
        path = confined_path(raw, [ROOT / "evidence"], exists=exists)
        if exists:
            if not path.is_file() or has_link(path):
                raise LabError("Grok capture file is missing")
        elif path.exists() or has_link(path):
            raise LabError("Grok capture path must be a fresh file under evidence")
        paths.append(path)
    if paths[0] == paths[1]:
        raise LabError("Grok capture paths must differ")
    return paths[0], paths[1]


def _write_new(path, payload):
    with Path(path).open("xb") as handle:
        handle.write(payload)


def _write_hash_artifact(directory, kind, digest, payload):
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise LabError("Malformed grok artifact name")
    folder = Path(directory) / kind
    folder.mkdir(exist_ok=True)
    target = folder / digest
    if target.exists():
        if target.read_bytes() != payload:
            raise LabError("Grok artifact hash collision")
        return
    _write_new(target, payload)


def _archived_artifact(run, kind, digest):
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise LabError("Malformed grok artifact name")
    path = confined_path(Path(run) / kind / digest, [Path(run) / kind])
    return path.read_bytes()


def _archived_input(run, receipts, input_id):
    receipt = receipts.get(input_id)
    if not isinstance(receipt, dict) or receipt.get("archived") is not True or not isinstance(receipt.get("snapshot"), str):
        raise LabError("Grok input was not archived for replay")
    return confined_path(Path(run) / receipt["snapshot"], [Path(run) / "input_snapshot"])


def _archived_prompt(run, node, receipts):
    if "prompt" in node:
        return _prompt_text(node["prompt"])
    path = _archived_input(run, receipts, node["input_id"])
    return _prompt_text(_decode_prompt(path.read_bytes()))
