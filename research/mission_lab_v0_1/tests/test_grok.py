"""llm:grok receipts, fixture replay, and the stdlib HTTP transport.

Socket tests bind 127.0.0.1 only. They do not call api.x.ai. The live smoke
test is skipped unless MISSION_LAB_GROK_LIVE=1.
"""
from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import urllib.request

import pytest

from research.mission_lab_v0_1 import core, executor, grok, planner, report


def _completion(content="fixture-reply-9f3a", model="grok-test"):
    return json.dumps({
        "choices": [{"finish_reason": "stop", "index": 0, "message": {"content": content, "role": "assistant"}}],
        "model": model,
        "usage": {"completion_tokens": 2, "prompt_tokens": 3, "total_tokens": 5},
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _profile(**extra):
    return {"schema_version": 1, "workers": 2, "read_buffer_bytes": 65536, "timeout_seconds": 10,
            "estimate_origin": "injected_fixture", **extra}


def _mission(path, payload, input_id="reply"):
    path.write_bytes(payload)
    return {"schema_version": 1, "mission_id": "grok-fixture", "requirements": [{
        "id": "record-call", "text": "Record one confined Grok call without retaining the API key.",
        "acceptance_criteria": ["Request and response hashes match the archived bytes."], "evidence_refs": []}],
        "inputs": [{"id": input_id, "path": str(path), "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}],
        "unresolved": []}


def _ask(verify, **overrides):
    node = {"id": "ask", "operation": "llm:grok", "model": "grok-test", "max_output_tokens": 16,
            "temperature": 0, "timeout_seconds": 2, "max_response_bytes": 4096, "transport": "fixture",
            "prompt": "ping", "fixture_id": "reply", "depends_on": [verify["id"]],
            "outputs": ["output:ask"], "state_reads": [verify["outputs"][0]], "state_writes": ["output:ask"]}
    node.update(overrides)
    return node


def _with_ask(mission, **overrides):
    graph = planner.compile_graph(mission)
    verify = next(node for node in graph if node["operation"] == "verify_digest" and node["input_id"] == "reply")
    return {**mission, "tasks": graph + [_ask(verify, **overrides)]}


@pytest.fixture
def confined(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "WORKSPACE", tmp_path)
    return tmp_path


@pytest.fixture
def evidence_dir():
    root = core.ROOT / "evidence"
    root.mkdir(exist_ok=True)
    path = root / ("grok-test-" + hashlib.sha256(os.urandom(8)).hexdigest()[:12])
    path.mkdir()
    yield path
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def test_transport_names_and_the_api_key_are_outside_the_platform_allowlist():
    names = {name.casefold() for name in core.platform_env_names()}
    for name in grok.TRANSPORT_ENV_NAMES:
        assert name.casefold() not in names
    assert grok.XAI_API_KEY.casefold() not in names
    assert grok.XAI_CHAT_COMPLETIONS_URL == "https://api.x.ai/v1/chat/completions"


@pytest.mark.parametrize("overrides,message", [
    ({"model": "gpt-4"}, "grok model id"),
    ({"temperature": True}, "temperature"),
    ({"temperature": 2.005}, "temperature"),
    ({"max_output_tokens": 0}, "max_output_tokens"),
    ({"max_response_bytes": grok.MAX_RESPONSE_BYTES + 1}, "max_response_bytes"),
    ({"timeout_seconds": 0}, "timeout_seconds"),
    ({"transport": "file"}, "transport"),
    ({"prompt": "ping", "input_id": "reply"}, "exactly one"),
    ({"prompt": "", "fixture_id": "reply"}, "1..4096"),
])
def test_llm_node_rejects_undeclared_or_unbounded_fields(confined, overrides, message):
    payload = _completion()
    mission = _with_ask(_mission(confined / "reply.json", payload), **overrides)
    with pytest.raises(core.LabError, match=message):
        core.validate_mission(mission)


def test_unknown_endpoint_field_is_rejected(confined):
    mission = _with_ask(_mission(confined / "reply.json", _completion()))
    mission["tasks"][-1]["endpoint"] = "https://example.invalid/v1/chat/completions"
    with pytest.raises(core.LabError, match="unknown"):
        core.validate_mission(mission)


def test_xai_requires_the_key_grant_and_fixture_refuses_it(confined):
    payload = _completion()
    base = _mission(confined / "reply.json", payload)
    xai = _with_ask(base, transport="xai", fixture_id=None, depends_on=[], state_reads=[])
    # fixture_id=None leaves the key; remove it.
    xai["tasks"][-1].pop("fixture_id", None)
    with pytest.raises(core.LabError, match="requires an XAI_API_KEY grant"):
        planner.build_plan(xai, _profile())
    granted = _profile(worker_env={"declared": ["XAI_API_KEY", "SSL_CERT_FILE"], "grants": {"ask": ["XAI_API_KEY", "SSL_CERT_FILE"]}})
    plan = planner.build_plan(xai, granted)
    assert plan["bindings"]["ask"] == {"candidate": "chat_v1", "revision": "1", "buffer_bytes": 0,
                                        "duration_ns": plan["resources"]["cost_model"]["fixed_task_ns"]}
    fixture = _with_ask(base)
    leaked = _profile(worker_env={"declared": ["XAI_API_KEY"], "grants": {"ask": ["XAI_API_KEY"]}})
    with pytest.raises(core.LabError, match="cannot receive XAI_API_KEY"):
        planner.build_plan(fixture, leaked)
    stolen = _profile(worker_env={"declared": ["XAI_API_KEY"], "grants": {"hash:reply": ["XAI_API_KEY"]}})
    with pytest.raises(core.LabError, match="only to an llm:grok task"):
        planner.build_plan(fixture, stolen)
    alias = _profile(worker_env={"declared": ["xai_api_key"], "grants": {"ask": ["xai_api_key"]}})
    with pytest.raises(core.LabError, match="exact spelling"):
        planner.build_plan(xai, alias)


def test_request_timeout_must_leave_room_for_the_executor_timeout(confined):
    mission = _with_ask(_mission(confined / "reply.json", _completion()))
    with pytest.raises(core.LabError, match="one second below"):
        planner.build_plan(mission, _profile(timeout_seconds=2))


def test_prompt_file_is_confined_and_must_be_verified(confined):
    payload = b"ping-from-file"
    mission = _mission(confined / "prompt.txt", payload, "prompt")
    graph = planner.compile_graph(mission)
    verify = next(node for node in graph if node["operation"] == "verify_digest")
    node = _ask(verify, prompt=None, input_id="prompt", fixture_id=None, transport="xai", depends_on=[verify["id"]],
                state_reads=[verify["outputs"][0]])
    node.pop("prompt")
    node.pop("fixture_id")
    drafted = {**mission, "tasks": graph + [node]}
    with pytest.raises(core.LabError, match="requires an XAI_API_KEY grant"):
        planner.build_plan(drafted, _profile())
    plan = planner.build_plan(drafted, _profile(worker_env={"declared": ["XAI_API_KEY"], "grants": {"ask": ["XAI_API_KEY"]}}))
    assert plan["bindings"]["ask"]["buffer_bytes"] == len(payload)
    assert plan["graph"][-1]["input_id"] == "prompt" or any(item["input_id"] == "prompt" for item in plan["graph"] if item["id"] == "ask")


def test_fixture_run_replays_without_network_and_does_not_keep_the_key(evidence_dir, monkeypatch):
    secret = "ml-xai-" + "c0ffee91" + "-KEY"
    phrase = "fixture-reply-9f3a"
    payload = _completion(phrase)
    source = evidence_dir / "reply.json"
    mission = _with_ask(_mission(source, payload))
    plan = planner.build_plan(mission, _profile())
    captured = {}
    real_popen = executor.subprocess.Popen

    def spy(command, **kwargs):
        env = kwargs.get("env") or {}
        captured.setdefault("envs", []).append(env)
        assert secret not in env.values()
        assert grok.XAI_API_KEY not in env
        return real_popen(command, **kwargs)

    monkeypatch.setenv(grok.XAI_API_KEY, secret)
    monkeypatch.setattr(executor.subprocess, "Popen", spy)
    output = executor.run_plan_data(plan, evidence_dir / "fixture-run")
    semantic = core.read_json(output / "semantic.json")
    assert semantic["accepted"] is True
    ask = next(task for task in semantic["tasks"] if task["task_id"] == "ask")
    receipt = ask["output"]
    assert receipt["transport"] == "fixture"
    assert receipt["http_status"] == 200
    assert receipt["response_sha256"] == hashlib.sha256(payload).hexdigest()
    content_path = output / "llm_content" / receipt["content_sha256"]
    response_path = output / "llm_responses" / receipt["response_sha256"]
    assert content_path.read_bytes() == phrase.encode("utf-8")
    assert response_path.read_bytes() == payload
    rendered = json.dumps(semantic)
    assert phrase not in rendered
    assert secret not in rendered
    assert secret.encode("utf-8") not in b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())
    replay = report.replay_run(output)
    assert replay["accepted"] is True
    assert replay["independent_execution"] is False
    flipped = bytearray(payload)
    flipped[0] ^= 0x01
    response_path.write_bytes(bytes(flipped))
    from research.mission_lab_v0_1.seal import reseal
    reseal(output)
    with pytest.raises(core.LabError, match="response hash"):
        report.replay_run(output)


def test_missing_key_fails_before_the_xai_task_starts(evidence_dir, monkeypatch):
    source = evidence_dir / "note.txt"
    payload = b"abc"
    mission = _mission(source, payload, "reply")
    graph = planner.compile_graph(mission)
    verify = next(node for node in graph if node["operation"] == "verify_digest")
    node = _ask(verify, transport="xai", depends_on=[], state_reads=[])
    node.pop("fixture_id")
    drafted = {**mission, "tasks": graph + [node]}
    plan = planner.build_plan(drafted, _profile(worker_env={"declared": ["XAI_API_KEY"], "grants": {"ask": ["XAI_API_KEY"]}}))
    launches = []
    real_popen = executor.subprocess.Popen

    def spy(command, **kwargs):
        job = core.read_json(command[command.index("--job") + 1])
        launches.append(job["node"]["id"])
        return real_popen(command, **kwargs)

    monkeypatch.delenv(grok.XAI_API_KEY, raising=False)
    monkeypatch.setattr(executor.subprocess, "Popen", spy)
    output = executor.run_plan_data(plan, evidence_dir / "missing-key")
    assert "ask" not in launches
    semantic = core.read_json(output / "semantic.json")
    ask = next(task for task in semantic["tasks"] if task["task_id"] == "ask")
    assert ask["status"] == "failed"
    assert "XAI_API_KEY" in ask["error"]["message"]
    assert semantic["accepted"] is False
    from research.mission_lab_v0_1 import report
    assert report.replay_run(output)["accepted"] is False


def test_prompt_file_execution_checks_the_verified_bytes(evidence_dir):
    payload = "ping-from-file".encode("utf-8")
    path = evidence_dir / "prompt.txt"
    path.write_bytes(payload)
    artifact = {"id": "prompt", "path": str(path.resolve()), "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    node = {"id": "ask", "operation": "llm:grok", "model": "grok-test", "max_output_tokens": 8, "temperature": 0,
            "timeout_seconds": 2, "max_response_bytes": 1024, "transport": "fixture", "input_id": "prompt",
            "fixture_id": "reply", "depends_on": ["verify:prompt", "verify:reply"], "outputs": ["output:ask"],
            "state_reads": ["output:verify:prompt", "output:verify:reply"], "state_writes": ["output:ask"]}
    reply = _completion("pong")
    reply_path = evidence_dir / "reply.json"
    reply_path.write_bytes(reply)
    fixture = {"id": "reply", "path": str(reply_path.resolve()), "size_bytes": len(reply), "sha256": hashlib.sha256(reply).hexdigest()}
    captures = {"response_path": str((evidence_dir / "response.bin").resolve()),
                "content_path": str((evidence_dir / "content.bin").resolve())}
    job = {"schema_version": 1, "node": node, "binding": {"candidate": "chat_v1", "revision": "1", "buffer_bytes": max(len(payload), len(reply)), "duration_ns": 1000},
           "input": artifact, "fixture": fixture, "captures": captures, "dependencies": {
               "verify:prompt": {"task_id": "verify:prompt", "status": "succeeded", "error": None,
                                 "output": {"input_id": "prompt", "sha256": artifact["sha256"], "size_bytes": len(payload), "passed": True}},
               "verify:reply": {"task_id": "verify:reply", "status": "succeeded", "error": None,
                                "output": {"input_id": "reply", "sha256": fixture["sha256"], "size_bytes": len(reply), "passed": True}}}}
    from research.mission_lab_v0_1 import operators
    receipt = operators.execute_operation(job)
    assert receipt["request"]["prompt_sha256"] == hashlib.sha256(payload).hexdigest()
    assert (evidence_dir / "content.bin").read_bytes() == b"pong"
    job["dependencies"]["verify:prompt"]["output"]["sha256"] = "0" * 64
    captures["response_path"] = str((evidence_dir / "response-2.bin").resolve())
    captures["content_path"] = str((evidence_dir / "content-2.bin").resolve())
    with pytest.raises(core.LabError, match="verification does not match"):
        operators.execute_operation(job)


class _HTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        mode = self.server.mode
        if mode == "timeout":
            self.server.release.wait(5)
            return
        if mode == "redirect":
            payload = b""
            code = 302
            extra = {"Location": self.server.location}
        elif mode == "status":
            payload = b"{}"
            code = self.server.status_code
            extra = {}
        elif mode == "malformed":
            payload = b"{"
            code = 200
            extra = {}
        elif mode == "oversize":
            payload = b"x" * self.server.oversize
            code = 200
            extra = {}
        else:
            payload = self.server.payload
            code = 200
            extra = {}
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve(mode, **kwargs):
    server = _Server(("127.0.0.1", 0), _HTTPHandler)
    server.mode = mode
    server.payload = kwargs.get("payload", b"")
    server.status_code = kwargs.get("status", 401)
    server.oversize = kwargs.get("oversize", 0)
    server.location = kwargs.get("location", "")
    server.requests = []
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _close(server, thread):
    server.release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _url(server):
    return "http://127.0.0.1:" + str(server.server_address[1]) + "/v1/chat/completions"


def _direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), grok._RefuseRedirect)


def test_urllib_transport_handles_status_malformed_oversize_timeout_and_redirect():
    key = "unit-test-key"
    payload = _completion("local")
    good, good_thread = _serve("ok", payload=payload)
    try:
        status, body = grok.urllib_chat(b"{}", key, 2, 4096, url=_url(good), opener=_direct_opener())
        assert status == 200 and body == payload
        assert good.requests[0]["auth"] == "Bearer " + key
        assert key.encode("utf-8") not in good.requests[0]["body"]
    finally:
        _close(good, good_thread)
    denied, denied_thread = _serve("status", status=401)
    try:
        with pytest.raises(core.LabError, match="HTTP 401"):
            grok.urllib_chat(b"{}", key, 2, 4096, url=_url(denied), opener=_direct_opener())
    finally:
        _close(denied, denied_thread)
    broken, broken_thread = _serve("malformed")
    try:
        status, body = grok.urllib_chat(b"{}", key, 2, 128, url=_url(broken), opener=_direct_opener())
        assert status == 200
        with pytest.raises(core.LabError, match="not valid JSON"):
            grok.parse_completion(body, 128)
    finally:
        _close(broken, broken_thread)
    huge, huge_thread = _serve("oversize", oversize=64)
    try:
        with pytest.raises(core.LabError, match="exceeded max_response_bytes"):
            grok.urllib_chat(b"{}", key, 2, 32, url=_url(huge), opener=_direct_opener())
    finally:
        _close(huge, huge_thread)
    stalled, stalled_thread = _serve("timeout")
    try:
        with pytest.raises(core.LabError, match="timed out"):
            grok.urllib_chat(b"{}", key, 0.4, 128, url=_url(stalled), opener=_direct_opener())
    finally:
        _close(stalled, stalled_thread)
    sink, sink_thread = _serve("ok", payload=b"stolen")
    redirected, redirected_thread = _serve("redirect", location=_url(sink))
    try:
        with pytest.raises(core.LabError, match="redirect is refused"):
            grok.urllib_chat(b"{}", key, 2, 128, url=_url(redirected), opener=_direct_opener())
        assert sink.requests == []
        assert redirected.requests[0]["auth"] == "Bearer " + key
    finally:
        _close(redirected, redirected_thread)
        _close(sink, sink_thread)


def test_transport_url_rejects_anything_except_xai_or_loopback():
    with pytest.raises(core.LabError, match="not permitted"):
        grok.urllib_chat(b"{}", "unit-test-key", 1, 64, url="https://example.invalid/v1/chat/completions")
    with pytest.raises(core.LabError, match="not permitted"):
        grok.urllib_chat(b"{}", "unit-test-key", 1, 64, url="http://127.0.0.1/v1/chat/completions")
    with pytest.raises(core.LabError, match="not permitted"):
        grok.urllib_chat(b"{}", "unit-test-key", 1, 64, url="http://localhost:9/v1/chat/completions")
    with pytest.raises(core.LabError, match="not set"):
        grok.urllib_chat(b"{}", "", 1, 64)
    with pytest.raises(core.LabError, match="malformed"):
        grok.urllib_chat(b"{}", "bad key", 1, 64)


def test_duplicate_and_non_object_responses_fail_closed():
    with pytest.raises(core.LabError, match="Duplicate JSON key"):
        grok.parse_completion(b'{"model":"grok-test","model":"other"}', 256)
    with pytest.raises(core.LabError, match="not a JSON object"):
        grok.parse_completion(b"[]", 256)
    with pytest.raises(core.LabError, match="usage was malformed"):
        grok.parse_completion(b'{"model":"grok-test","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"x"}}],"usage":{"prompt_tokens":true,"completion_tokens":1,"total_tokens":1}}', 512)


def test_xai_execute_posts_to_the_fixed_url_and_omits_the_key(evidence_dir, monkeypatch):
    secret = "ml-xai-" + "a17c" + "-KEY"
    captured = {}
    payload = _completion("wired")

    class Response:
        status = 200

        def __init__(self):
            self.pending = payload

        def readinto(self, buffer):
            if not self.pending:
                return 0
            count = min(len(buffer), len(self.pending))
            buffer[:count] = self.pending[:count]
            self.pending = self.pending[count:]
            return count

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout=None):
            captured["url"] = request.full_url
            captured["data"] = request.data
            captured["auth"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args, **kwargs: Opener())
    monkeypatch.setenv(grok.XAI_API_KEY, secret)
    node = {"id": "ask", "operation": "llm:grok", "model": "grok-test", "max_output_tokens": 8, "temperature": 0,
            "timeout_seconds": 2, "max_response_bytes": 1024, "transport": "xai", "prompt": "ping",
            "depends_on": [], "outputs": ["output:ask"], "state_reads": [], "state_writes": ["output:ask"]}
    job = {"node": node, "dependencies": {}, "captures": {
        "response_path": str((evidence_dir / "response.bin").resolve()),
        "content_path": str((evidence_dir / "content.bin").resolve())}}
    receipt = grok.execute(job)
    assert captured["url"] == grok.XAI_CHAT_COMPLETIONS_URL
    assert captured["timeout"] == 2
    assert captured["auth"] == "Bearer " + secret
    assert secret.encode("utf-8") not in captured["data"]
    assert b"grok-test" in captured["data"]
    assert secret not in json.dumps(receipt)
    assert (evidence_dir / "content.bin").read_bytes() == b"wired"


@pytest.mark.skipif(os.environ.get("MISSION_LAB_GROK_LIVE") != "1", reason="set MISSION_LAB_GROK_LIVE=1 to send one live chat completion")
def test_live_grok_smoke_records_a_receipt_without_scoring_content():
    if not os.environ.get(grok.XAI_API_KEY):
        pytest.fail("MISSION_LAB_GROK_LIVE=1 requires XAI_API_KEY")
    model = os.environ.get("MISSION_LAB_GROK_MODEL", "grok-4-fast")
    payload = grok.request_bytes(model, "Reply with one word.", 16, 0)
    status, body = grok.urllib_chat(payload, os.environ[grok.XAI_API_KEY], 60, 65536)
    parsed = grok.parse_completion(body, 65536)
    assert status == 200
    assert parsed["usage"]["total_tokens"] >= 0
    assert isinstance(parsed["content"], str)
