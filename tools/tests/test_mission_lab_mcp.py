"""HTTP checks for the remote Mission Lab MCP adapter.

The adapter is outside the controller package. These tests speak Streamable
HTTP to a loopback server and call the registered controller operations.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


spec = importlib.util.spec_from_file_location(
    "mission_lab_mcp", Path(__file__).resolve().parents[1] / "mission_lab_mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
ACCEPT = "application/json, text/event-stream"


def mission_abc():
    return {
        "schema_version": 1,
        "mission_id": "remote-abc",
        "requirements": [{
            "id": "audit",
            "text": "Audit the uploaded file.",
            "acceptance_criteria": ["Exact SHA-256 and byte count"],
            "evidence_refs": [],
        }],
        "inputs": [{
            "id": "abc",
            "path": "abc.bin",
            "size_bytes": 3,
            "sha256": ABC_SHA256,
        }],
        "unresolved": [],
    }


def resources(workers=1):
    return {
        "schema_version": 1,
        "workers": workers,
        "read_buffer_bytes": 65536,
        "timeout_seconds": 30,
        "estimate_origin": "injected_fixture",
    }


class RemoteServer(unittest.TestCase):
    def setUp(self):
        self._prior_key = os.environ.get("MISSION_LAB_SEAL_KEY")
        self._prior_file = os.environ.get("MISSION_LAB_SEAL_KEY_FILE")
        os.environ.pop("MISSION_LAB_SEAL_KEY", None)
        # The key file lives outside the package. The controller reads it by env.
        key_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "mission-lab-mcp-tests"
        key_dir.mkdir(exist_ok=True)
        self.key_path = key_dir / (hashlib.sha256(self.id().encode()).hexdigest() + ".key")
        self.key_path.write_bytes(os.urandom(32))
        os.environ["MISSION_LAB_SEAL_KEY_FILE"] = str(self.key_path)
        self.server = mcp.serve("127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]
        self.session = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self._prior_key is None:
            os.environ.pop("MISSION_LAB_SEAL_KEY", None)
        else:
            os.environ["MISSION_LAB_SEAL_KEY"] = self._prior_key
        if self._prior_file is None:
            os.environ.pop("MISSION_LAB_SEAL_KEY_FILE", None)
        else:
            os.environ["MISSION_LAB_SEAL_KEY_FILE"] = self._prior_file
        self.key_path.unlink(missing_ok=True)

    def post(self, payload, *, session=None, token=None, accept=ACCEPT):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": accept}
        if session:
            headers["Mcp-Session-Id"] = session
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(self.base, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else None
                return response.status, response.headers.get("Mcp-Session-Id"), parsed
        except HTTPError as exc:
            raw = exc.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else None
            return exc.code, exc.headers.get("Mcp-Session-Id"), parsed

    def initialize(self):
        status, session, body = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}},
        })
        self.assertEqual(status, 200)
        self.assertTrue(session)
        self.assertEqual(body["result"]["serverInfo"]["name"], "mission-lab")
        self.assertEqual(body["result"]["protocolVersion"], "2025-03-26")
        accepted, _, empty = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session)
        self.assertEqual(accepted, 202)
        self.assertIsNone(empty)
        self.session = session

    def call(self, name, arguments, request_id=2):
        status, _, body = self.post({
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, session=self.session)
        self.assertEqual(status, 200, body)
        self.assertNotIn("error", body)
        result = body["result"]
        text = result["content"][0]["text"]
        return result.get("isError", False), json.loads(text)


class ProtocolTests(RemoteServer):
    def test_lists_the_controller_commands(self):
        self.initialize()
        status, _, body = self.post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session=self.session)
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in body["result"]["tools"]}
        self.assertEqual(names, {
            "validate", "plan", "run", "replay", "inspect", "resume", "replan", "demo"})

    def test_request_without_session_is_rejected(self):
        self.initialize()
        status, _, body = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32001)

    def test_get_does_not_open_an_sse_stream(self):
        request = Request(self.base, method="GET", headers={"Accept": ACCEPT})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 405)

    def test_public_bind_requires_a_token(self):
        with self.assertRaises(mcp.AdapterError) as caught:
            mcp.check_bind("0.0.0.0", None)
        self.assertIn("MISSION_LAB_MCP_TOKEN", str(caught.exception))
        mcp.check_bind("127.0.0.1", None)
        mcp.check_bind("0.0.0.0", "secret")

    def test_bearer_token_gates_the_endpoint(self):
        self.server.shutdown()
        self.server.server_close()
        self.server = mcp.serve("127.0.0.1", 0, token="desk-token")
        self.base = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]
        status, _, _ = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}},
        })
        self.assertEqual(status, 401)
        status, session, body = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}},
        }, token="desk-token")
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], "mission-lab")
        self.assertTrue(session)


class ControllerToolTests(RemoteServer):
    def test_validate_fixture_and_refuse_an_escaping_path(self):
        self.initialize()
        failed, payload = self.call("validate", {
            "mission_path": "research/mission_lab_v0_1/fixtures/mission.json"})
        self.assertFalse(failed)
        self.assertTrue(payload["valid"])
        self.assertFalse(payload["human_review"])
        failed, payload = self.call("validate", {"mission_path": "../outside.json"}, request_id=3)
        self.assertTrue(failed)
        self.assertNotIn("plan_id", payload)

    def test_uploaded_bytes_plan_run_replay_and_replan(self):
        self.initialize()
        uploaded = {
            "mission": mission_abc(),
            "inputs": [{"id": "abc", "content_base64": base64.b64encode(b"abc").decode("ascii")}],
            "resources": resources(),
        }
        failed, planned = self.call("plan", uploaded)
        self.assertFalse(failed, planned)
        self.assertEqual(len(planned["plan_id"]), 64)
        self.assertFalse(planned["human_review"])
        self.assertNotIn("/", planned["plan_id"])
        failed, again = self.call("plan", uploaded, request_id=3)
        self.assertFalse(failed, again)
        self.assertEqual(again["plan_id"], planned["plan_id"])
        self.assertTrue(again["already_recorded"])
        failed, ran = self.call("run", {"plan_id": planned["plan_id"]}, request_id=4)
        self.assertFalse(failed, ran)
        self.assertTrue(ran["accepted"])
        self.assertEqual(ran["plan_id"], planned["plan_id"])
        self.assertFalse(ran["human_review"])
        verify = next(task for task in ran["tasks"] if task["task_id"] == "verify:abc")
        self.assertEqual(verify["output"]["sha256"], ABC_SHA256)
        self.assertNotIn("path", json.dumps(ran))
        failed, replayed = self.call("replay", {"run_id": ran["run_id"]}, request_id=5)
        self.assertFalse(failed, replayed)
        self.assertTrue(replayed["accepted"])
        self.assertTrue(replayed["integrity_verified"])
        self.assertEqual(replayed["signature_status"], "verified")
        self.assertFalse(replayed["independent_execution"])
        failed, revised = self.call("replan", {
            "plan_id": planned["plan_id"],
            "resources": resources(workers=2),
            "trigger": {
                "schema_version": 1,
                "kind": "resource_change",
                "text": "Raise the declared worker slots from 1 to 2.",
                "evidence_origin": "human_supplied",
                "evidence_refs": [],
            },
        }, request_id=6)
        self.assertFalse(failed, revised)
        self.assertNotEqual(revised["plan_id"], planned["plan_id"])
        self.assertEqual(revised["revision"]["trigger"]["kind"], "resource_change")

    def test_wrong_bytes_do_not_plan(self):
        self.initialize()
        failed, payload = self.call("plan", {
            "mission": mission_abc(),
            "inputs": [{"id": "abc", "content_base64": base64.b64encode(b"abd").decode("ascii")}],
            "resources": resources(),
        })
        self.assertTrue(failed)
        self.assertNotIn("plan_id", payload)

    def test_inspect_returns_fixture_metadata_without_a_host_path(self):
        self.initialize()
        failed, payload = self.call("inspect", {})
        self.assertFalse(failed, payload)
        self.assertTrue(payload["complete"])
        self.assertGreater(payload["counts"]["files"], 0)
        self.assertNotIn("root", payload)
        self.assertTrue(all("/" not in entry["path"][:1] for entry in payload["entries"]))

    def test_resume_of_a_gateless_run_keeps_the_controller_error(self):
        self.initialize()
        _, planned = self.call("plan", {
            "mission": mission_abc(),
            "inputs": [{"id": "abc", "content_base64": base64.b64encode(b"abc").decode("ascii")}],
            "resources": resources(),
        })
        _, ran = self.call("run", {"plan_id": planned["plan_id"]}, request_id=3)
        failed, payload = self.call("resume", {
            "run_id": ran["run_id"],
            "approval": {
                "schema_version": 1,
                "gate_id": "merit",
                "approver_id": "ada",
                "approved_at": "2026-11-04T15:00:00Z",
            },
        }, request_id=4)
        self.assertTrue(failed)
        self.assertIn("gate", payload["error"].casefold())
