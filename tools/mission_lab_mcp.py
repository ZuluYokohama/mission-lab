"""Streamable HTTP MCP adapter for the Mission Lab controller.

This process is not part of the package source identity. It does not add a
runtime dependency and it does not register a new operator. Cursor, or any
other MCP client, calls the same validate, plan, run, replay, inspect,
resume, replan, and demo operations. Plans and sealed runs stay on this
server. A tool result does not include the seal key, an API key, or a host
path.

Bind to a public address only with MISSION_LAB_MCP_TOKEN set. Loopback can
run without that token. A new run still needs the controller seal key in
the server environment. Passing a key as a tool argument is refused.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.mission_lab_v0_1 import core, executor  # noqa: E402
from research.mission_lab_v0_1.inventory import inspect_archive  # noqa: E402
from research.mission_lab_v0_1.planner import build_plan, write_plan  # noqa: E402
from research.mission_lab_v0_1.replan import replan_data  # noqa: E402
from research.mission_lab_v0_1.report import replay_run  # noqa: E402

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
PREFERRED_PROTOCOL = "2025-03-26"
MAX_BODY_BYTES = 14 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_PLAN_ID = core.SHA256
_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_DEMO_RUN = re.compile(r"^([a-f0-9]{32})/(baseline_run|revised_run)$")
_HIDDEN = {"path", "snapshot", "executable", "root"}
_STORE = threading.Lock()


class AdapterError(Exception):
    """A remote-adapter refusal. Controller failures stay core.LabError."""


def check_bind(host, token):
    """Public interfaces require a bearer token. Loopback may omit it."""
    if not isinstance(host, str) or not host:
        raise AdapterError("Host must be a nonempty string")
    if host not in LOOPBACK and not token:
        raise AdapterError("MISSION_LAB_MCP_TOKEN is required when host is not loopback")


def _token_matches(presented, expected):
    if not expected:
        return True
    if not isinstance(presented, str):
        return False
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _load(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except UnicodeError as exc:
        raise AdapterError("Request body must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise AdapterError("Request body is not JSON") from exc
    return value


def _public_error(exc):
    text = str(exc)
    return re.sub(r"(?:[A-Za-z]:\\[^\s'\"]+|/(?:[^\s'\"]+))", "[path]", text)


def _publish(value):
    if isinstance(value, dict):
        return {key: _publish(item) for key, item in value.items() if key not in _HIDDEN}
    if isinstance(value, list):
        return [_publish(item) for item in value]
    return value


def _evidence(*parts):
    return core.ROOT / "evidence" / "remote" / Path(*parts)


def _workspace_file(relative, label):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or "\\" in relative:
        raise core.LabError(label + " must be a workspace-relative file")
    if ".." in Path(relative).parts:
        raise core.LabError(label + " must be a workspace-relative file")
    path = core.confined_path(core.WORKSPACE / relative, [core.WORKSPACE])
    if not path.is_file():
        raise core.LabError(label + " is not a file")
    return path


def _object(arguments, key):
    value = arguments.get(key)
    if value is not None and not isinstance(value, dict):
        raise core.LabError(key + " must be an object")
    return value


def _mission_from(arguments):
    path = arguments.get("mission_path")
    inline = _object(arguments, "mission")
    if (path is None) == (inline is None):
        raise core.LabError("Provide mission_path or mission")
    if path is not None:
        if arguments.get("inputs"):
            raise core.LabError("inputs are only for an inline mission")
        return core.load_mission(_workspace_file(path, "mission_path"))
    mission = json.loads(json.dumps(inline), object_pairs_hook=_pairs)
    inputs = arguments.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise core.LabError("An inline mission requires its input bytes")
    if len(inputs) > 8:
        raise core.LabError("Mission requires 1..8 input files")
    declared = mission.get("inputs")
    if not isinstance(declared, list):
        raise core.LabError("Mission inputs must be a list")
    uploaded = {}
    for item in inputs:
        if not isinstance(item, dict):
            raise core.LabError("Input upload must be an object")
        core.fields(item, ("id", "content_base64"), label="input upload")
        core.identifier(item["id"], "input id")
        if item["id"] in uploaded:
            raise core.LabError("Duplicate uploaded input id")
        try:
            raw = base64.b64decode(item["content_base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise core.LabError("Input content is not base64") from exc
        if len(raw) > MAX_INPUT_BYTES:
            raise core.LabError("Uploaded input exceeds 1 MiB")
        uploaded[item["id"]] = raw
    if {item.get("id") for item in declared} != set(uploaded):
        raise core.LabError("Uploaded input ids must match the mission")
    for item in declared:
        name = item.get("path")
        if not isinstance(name, str) or not name or name != Path(name).name or name in {".", ".."}:
            raise core.LabError("Remote input path must be a single file name")
        raw = uploaded[item["id"]]
        digest = hashlib.sha256(raw).hexdigest()
        folder = _evidence("inputs", digest)
        folder.mkdir(parents=True, exist_ok=True)
        target = core.confined_path(folder / name, [core.ROOT / "evidence"], exists=False)
        try:
            with target.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise core.LabError("Stored input identity changed") from None
        item["path"] = str(target)
    return core.validate_mission(mission)


def _resources_from(arguments):
    path = arguments.get("resources_path")
    inline = _object(arguments, "resources")
    if (path is None) == (inline is None):
        raise core.LabError("Provide resources_path or resources")
    if path is not None:
        return core.validate_resources(core.read_json(_workspace_file(path, "resources_path")))
    return core.validate_resources(inline)


def _store_plan(plan):
    plan_id = plan["plan_id"]
    if _PLAN_ID.fullmatch(plan_id) is None:
        raise core.LabError("Malformed plan_id")
    directory = _evidence("plans", plan_id)
    with _STORE:
        if directory.exists():
            recorded = core.load_plan(directory / "plan.json")
            if recorded["plan_id"] != plan_id:
                raise core.LabError("Stored plan identity changed")
            return recorded, True
        write_plan(plan, core.new_output(directory))
    return plan, False


def _plan_summary(plan, already):
    return {
        "plan_id": plan["plan_id"],
        "mission_id": plan["mission"]["mission_id"],
        "human_review": False,
        "already_recorded": already,
        "planner": plan.get("planner"),
        "enumerated_bindings": plan.get("enumerated_bindings"),
        "node_count": len(plan["graph"]),
        "revision": plan.get("revision"),
    }


def _plan_directory(plan_id):
    if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
        raise core.LabError("Malformed plan_id")
    directory = core.confined_path(_evidence("plans", plan_id), [core.ROOT / "evidence"])
    if not (directory / "plan.json").is_file():
        raise core.LabError("Unknown plan_id")
    return directory


def _run_directory(run_id):
    if not isinstance(run_id, str):
        raise core.LabError("Malformed run_id")
    demo = _DEMO_RUN.fullmatch(run_id)
    if _RUN_ID.fullmatch(run_id):
        directory = _evidence("runs", run_id)
    elif demo:
        directory = _evidence("demos", demo.group(1), demo.group(2))
    else:
        raise core.LabError("Malformed run_id")
    directory = core.confined_path(directory, [core.ROOT / "evidence"])
    if not (directory / "semantic.json").is_file():
        raise core.LabError("Unknown run_id")
    return directory


def _run_summary(directory):
    semantic = core.read_json(directory / "semantic.json")
    tasks = []
    for task in semantic["tasks"]:
        tasks.append(_publish({
            "task_id": task["task_id"],
            "status": task["status"],
            "error": task.get("error"),
            "output": task.get("output"),
            "blocked_by": task.get("blocked_by"),
        }))
    seal_claim = semantic.get("seal") if isinstance(semantic.get("seal"), dict) else {}
    return {
        "run_id": directory.name if directory.parent.name == "runs" else directory.parent.name + "/" + directory.name,
        "plan_id": semantic["plan_id"],
        "accepted": semantic["accepted"],
        "human_review": False,
        "seal": {"algorithm": seal_claim.get("algorithm"), "key_id": seal_claim.get("key_id")},
        "tasks": tasks,
    }


def _reject_secret_arguments(arguments):
    if not isinstance(arguments, dict):
        raise core.LabError("Tool arguments must be an object")
    for key in arguments:
        folded = key.casefold().replace("-", "_")
        if folded in {"seal_key", "xai_api_key", "api_key", "authorization", "token"}:
            raise core.LabError("Keys are read from the server environment and are not tool arguments")


def tool_validate(arguments):
    mission = _mission_from(arguments)
    return {
        "schema_version": 1,
        "valid": True,
        "human_review": False,
        "status": "needs_clarification" if mission["unresolved"] else "structurally_valid",
        "questions": mission["unresolved"],
        "mission_identity": core.digest_object(mission),
    }


def tool_plan(arguments):
    plan = build_plan(_mission_from(arguments), _resources_from(arguments))
    if "plan_id" not in plan:
        return {"human_review": False, "status": plan.get("status", "needs_clarification"),
                "questions": plan.get("unresolved", plan.get("questions", []))}
    stored, already = _store_plan(plan)
    return _plan_summary(stored, already)


def tool_run(arguments):
    directory = _plan_directory(arguments.get("plan_id"))
    run_id = secrets.token_hex(16)
    output = executor.run_plan(directory / "plan.json", _evidence("runs", run_id))
    return _run_summary(output)


def tool_replay(arguments):
    result = replay_run(_run_directory(arguments.get("run_id")))
    result.pop("report", None)
    return _publish(result)


def tool_inspect(arguments):
    max_entries = arguments.get("max_entries", 1000)
    max_depth = arguments.get("max_depth", 16)
    run_id = secrets.token_hex(16)
    destination = inspect_archive(core.ARCHIVE, _evidence("inspections", run_id), max_entries, max_depth)
    document = core.read_json(destination / "inventory.json")
    return {
        "run_id": run_id,
        "complete": document["complete"],
        "incomplete_reasons": document["incomplete_reasons"],
        "counts": document["counts"],
        "entries": [{"path": entry["path"], "kind": entry["kind"], "bytes": entry["bytes"],
                     "depth": entry["depth"]} for entry in document["entries"]],
        "file_contents_read": False,
        "human_review": False,
    }


def tool_resume(arguments):
    approval = _object(arguments, "approval")
    if approval is None:
        raise core.LabError("resume requires an approval record")
    prior = _run_directory(arguments.get("run_id"))
    run_id = secrets.token_hex(16)
    output = executor.resume_run(prior, _evidence("runs", run_id), approval)
    summary = _run_summary(output)
    summary["resumed_from"] = arguments.get("run_id")
    return summary


def tool_replan(arguments):
    prior = core.load_plan(_plan_directory(arguments.get("plan_id")) / "plan.json")
    trigger = _object(arguments, "trigger")
    if trigger is None:
        raise core.LabError("replan requires a trigger")
    updated = replan_data(prior, _resources_from(arguments), trigger)
    if "plan_id" not in updated:
        return {"human_review": False, "status": "needs_clarification", "prior_plan_id": prior["plan_id"]}
    stored, already = _store_plan(updated)
    return _plan_summary(stored, already)


def tool_demo(_arguments):
    from research.mission_lab_v0_1.demo import run_demo
    demo_id = secrets.token_hex(16)
    output = run_demo(_evidence("demos", demo_id))
    comparison = core.read_json(output / "comparison.json")
    return {
        "demo_id": demo_id,
        "accepted": comparison["accepted"],
        "human_review": False,
        "independent_reproduction": False,
        "baseline_run_id": demo_id + "/baseline_run",
        "revised_run_id": demo_id + "/revised_run",
        "baseline_plan_id": comparison["baseline_plan_id"],
        "revised_plan_id": comparison["revised_plan_id"],
        "same_required_results": comparison["same_required_results"],
        "changed_bindings": comparison["changed_bindings"],
    }


TOOLS = (
    ("validate", "Check a mission. human_review stays false. Structural validity is not acceptance.",
     {"mission_path": {"type": "string"}, "mission": {"type": "object"},
      "inputs": {"type": "array", "items": {"type": "object"}}}, tool_validate),
    ("plan", "Freeze a plan for a mission and resource profile. The plan_id is the plan digest.",
     {"mission_path": {"type": "string"}, "mission": {"type": "object"}, "inputs": {"type": "array"},
      "resources_path": {"type": "string"}, "resources": {"type": "object"}}, tool_plan),
    ("run", "Execute a stored plan in a fresh evidence directory. accepted is the registered contract, not a quality score.",
     {"plan_id": {"type": "string"}}, tool_run),
    ("replay", "Recompute the sealed run from archived receipts. independent_execution is false.",
     {"run_id": {"type": "string"}}, tool_replay),
    ("inspect", "Inventory package-fixture metadata. File contents are not read.",
     {"max_entries": {"type": "integer"}, "max_depth": {"type": "integer"}}, tool_inspect),
    ("resume", "Start a new run of a sealed plan after one approval record. This does not set human_review.",
     {"run_id": {"type": "string"}, "approval": {"type": "object"}}, tool_resume),
    ("replan", "Write a new plan from a stored plan, a resource profile, and a revision trigger.",
     {"plan_id": {"type": "string"}, "resources_path": {"type": "string"}, "resources": {"type": "object"},
      "trigger": {"type": "object"}}, tool_replan),
    ("demo", "Run the checked-in file-audit demonstration and return the two sealed run ids.",
     {}, tool_demo),
)


def _tool_specs():
    specs = []
    for name, description, properties, _function in TOOLS:
        required = {"run": ["plan_id"], "replay": ["run_id"], "resume": ["run_id", "approval"],
                    "replan": ["plan_id", "trigger"]}.get(name, [])
        specs.append({
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                             "additionalProperties": False, "required": required},
        })
    return specs


def _call_tool(name, arguments):
    _reject_secret_arguments(arguments)
    for tool_name, _description, _properties, function in TOOLS:
        if tool_name == name:
            try:
                payload = function(arguments)
                return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True, allow_nan=False)}],
                        "isError": False}
            except (core.LabError, AdapterError, OSError, UnicodeError, ValueError) as exc:
                payload = {"error": _public_error(exc)}
                return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps({"error": "Unknown tool"})}], "isError": True}


def dispatch(message):
    """Return ('json', response), ('empty', None), or raise AdapterError for a bad request."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        raise AdapterError("Invalid JSON-RPC request")
    method = message["method"]
    has_id = "id" in message
    if not has_id:
        return "empty", None
    request_id = message["id"]
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return "json", _error(request_id, -32602, "Invalid params")
    if method == "initialize":
        version = params.get("protocolVersion")
        chosen = version if version in PROTOCOL_VERSIONS else PREFERRED_PROTOCOL
        return "json", {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": chosen,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mission-lab", "version": "0.1.0"},
            "instructions": (
                "Mission Lab records a closed file-audit plan and replays its seal. "
                "accepted and signature_status are contract checks. They are not a code-quality score. "
                "human_review stays false. Replay does not execute the mission again. "
                "Do not send a seal key or an API key as a tool argument."
            ),
        }}
    if method == "ping":
        return "json", {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return "json", {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _tool_specs()}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return "json", _error(request_id, -32602, "Invalid params")
        return "json", {"jsonrpc": "2.0", "id": request_id, "result": _call_tool(name, arguments)}
    return "json", _error(request_id, -32601, "Method not found")


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mission-lab-mcp/0.1"

    def do_GET(self):
        self._reject(405, {"error": "SSE is not offered. POST JSON-RPC to /mcp."})

    def do_DELETE(self):
        if not self._authorized() or not self._known_session(required=True):
            return
        session = self.headers.get("Mcp-Session-Id")
        self.server.sessions.pop(session, None)
        self._reject(204, None)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/mcp":
            self._reject(404, {"error": "Not found"})
            return
        if not self._authorized():
            return
        origin = self.headers.get("Origin")
        host = self.server.server_address[0]
        if origin and host in LOOPBACK and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            self._reject(403, {"error": "Origin is not allowed on loopback"})
            return
        accept = self.headers.get("Accept", "")
        if "application/json" not in accept:
            self._reject(406, {"error": "Accept must include application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reject(411, {"error": "Content-Length is required"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._reject(413, {"error": "Request body exceeds 14 MiB"})
            return
        raw = self.rfile.read(length)
        try:
            message = _load(raw)
        except AdapterError as exc:
            self._send(400, _error(None, -32700, str(exc)))
            return
        if isinstance(message, list) or not isinstance(message, dict):
            self._send(400, _error(None, -32600, "Batch requests are not accepted"))
            return
        method = message.get("method")
        if method != "initialize" and not self._known_session(required=True):
            return
        try:
            kind, payload = dispatch(message)
        except AdapterError as exc:
            request_id = message.get("id")
            self._send(400, _error(request_id, -32600, str(exc)))
            return
        session = self.headers.get("Mcp-Session-Id")
        if method == "initialize" and kind == "json":
            session = secrets.token_hex(24)
            with self.server.lock:
                if len(self.server.sessions) >= 128:
                    self.server.sessions.pop(next(iter(self.server.sessions)))
                self.server.sessions[session] = True
        if kind == "empty":
            self._send(202, None, session=session if method != "initialize" else None)
            return
        self._send(200, payload, session=session)

    def _authorized(self):
        expected = self.server.token
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        presented = header[7:] if header.startswith("Bearer ") else None
        if _token_matches(presented, expected):
            return True
        self._reject(401, {"error": "unauthorized"})
        return False

    def _known_session(self, required):
        if not required:
            return True
        session = self.headers.get("Mcp-Session-Id")
        if not isinstance(session, str) or len(session) > 64 or session not in self.server.sessions:
            request_id = None
            self._send(400, _error(request_id, -32001, "Mcp-Session-Id is required"))
            return False
        return True

    def _reject(self, status, payload):
        self._send(status, payload)

    def _send(self, status, payload, session=None):
        body = b"" if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("mission-lab-mcp " + (fmt % args) + "\n")


def serve(host, port, token=None):
    """Start the endpoint on a daemon thread and return the server."""
    check_bind(host, token)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.token = token
    httpd.sessions = {}
    httpd.lock = threading.Lock()
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="mission-lab-mcp", daemon=True)
    thread.start()
    httpd.thread = thread
    return httpd


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("serve")
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    if type(args.port) is not int or not 0 <= args.port <= 65535:
        raise AdapterError("Port must be an integer from 0 to 65535")
    token = os.environ.get("MISSION_LAB_MCP_TOKEN") or None
    if isinstance(token, str) and not token.strip():
        token = None
    httpd = serve(args.host, args.port, token=token)
    host, port = httpd.server_address[:2]
    print(json.dumps({"endpoint": "http://%s:%s/mcp" % (host, port), "token_required": bool(token)}, sort_keys=True),
          flush=True)
    try:
        httpd.thread.join()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from exc
