"""Executor resource, failure, and evidence contracts using stdlib fixtures."""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from research.mission_lab_v0_1 import core, executor, operators, planner, seal, worker


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        evidence = core.ROOT / "evidence"
        evidence.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="executor-test-", dir=evidence)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "input.csv"
        self.source.write_bytes(b"label,value\n" + b"alpha,123\n" * 4000)
        self.artifact = {"id": "alpha", "path": str(self.source), "size_bytes": self.source.stat().st_size,
                         "sha256": core.file_hash(self.source)}
        self.mission = {"schema_version": 1, "mission_id": "executor_fixture", "inputs": [self.artifact],
                        "requirements": [{"id": "identity", "text": "Verify the fixture bytes",
                                          "acceptance_criteria": ["Exact digest and byte count agree"], "evidence_refs": []}],
                        "unresolved": []}

    def plan(self, *, workers=2, buffer=32768, timeout=60):
        return planner.build_plan(self.mission, {"schema_version": 1, "workers": workers,
                                  "read_buffer_bytes": buffer, "timeout_seconds": timeout,
                                  "estimate_origin": "injected_fixture"})

    def fake_factory(self, *, malformed=None, hanging=None):
        launches = []
        pids = itertools.count(100)

        class Process:
            def __init__(process, command, **kwargs):
                self.assertEqual(command[:4], [sys.executable, "-B", "-m", "research.mission_lab_v0_1.worker"])
                self.assertIs(kwargs["shell"], False)
                self.assertEqual(kwargs["cwd"], core.WORKSPACE)
                job = core.read_json(command[command.index("--job") + 1])
                result_path = Path(command[command.index("--result") + 1])
                launches.append(job["node"]["id"])
                process.task_id = job["node"]["id"]
                process.pid = next(pids)
                process.killed = False
                process.stopped = threading.Event()
                process.polled = False
                if process.task_id != hanging:
                    if process.task_id == malformed:
                        result_path.write_text("{invalid json", encoding="utf-8")
                        kwargs["stderr"].write("fixture malformed result\n")
                    else:
                        result = {"task_id": process.task_id, "status": "succeeded",
                                  "output": operators.execute_operation(job), "error": None}
                        core.write_json(result_path, result)
                    kwargs["stdout"].write("registered fixture worker\n")

            def poll(process):
                if process.killed:
                    return -9
                if process.task_id == hanging:
                    return None
                if not process.polled:
                    process.polled = True
                    return None
                return 0

            def kill(process):
                process.killed = True
                process.stopped.set()

            def wait(process, timeout=None):
                if process.task_id == hanging:
                    if not process.stopped.wait(timeout):
                        raise subprocess.TimeoutExpired("registered fixture worker", timeout)
                return -9 if process.killed else 0

        return Process, launches

    def assert_integrity(self, directory):
        integrity = core.read_json(directory / "integrity.json")
        actual = {path.relative_to(directory).as_posix(): core.file_hash(path)
                  for path in directory.rglob("*") if path.is_file() and path.name != "integrity.json"}
        self.assertEqual(integrity["files"], actual)
        self.assertEqual(integrity["semantic_sha256"], core.file_hash(directory / "semantic.json"))
        self.assertEqual(integrity["signature"]["algorithm"], "hmac-sha256")
        self.assertEqual(integrity["signature"]["key_id"], seal.key_id(seal.require_seal_key()))
        for name, digest in core.read_json(directory / "sources.json").items():
            self.assertEqual(core.file_hash(directory / "source_snapshot" / name), digest)

    def test_real_registered_worker_and_semantic_repeatability(self):
        plan = self.plan(buffer=65536)
        first = executor.run_plan_data(plan, self.base / "real_first")
        second = executor.run_plan_data(plan, self.base / "real_second")
        self.assertEqual((first / "semantic.json").read_bytes(), (second / "semantic.json").read_bytes())
        semantic = core.read_json(first / "semantic.json")
        self.assertTrue(semantic["accepted"])
        self.assertEqual(core.read_json(first / "manifest.json"), {"artifacts": [
            {key: self.artifact[key] for key in ("id", "sha256", "size_bytes") if key != "id"} | {"input_id": "alpha"}]})
        self.assertTrue(all(row["status"] == "succeeded" for row in semantic["tasks"]))
        self.assertFalse(core.read_json(first / "input_receipts.json")["archived_inputs"])
        self.assert_integrity(first)
        self.assert_integrity(second)

    def test_buffer_and_worker_reservations_and_registered_launch(self):
        plan = self.plan(workers=2, buffer=32768)
        factory, launches = self.fake_factory()
        with patch.object(executor.subprocess, "Popen", factory):
            output = executor.run_plan_data(plan, self.base / "bounded")
        self.assertTrue(core.read_json(output / "semantic.json")["accepted"])
        events = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        launched = [event for event in events if event["kind"] == "launched"]
        self.assertTrue(all(row["used_buffer_bytes"] <= 32768 for row in launched))
        self.assertTrue(all(row["workers_active"] <= 2 for row in launched))
        self.assertEqual(len(launches), len(set(launches)))
        self.assertEqual(set(launches), {node["id"] for node in plan["graph"]})
        self.assert_integrity(output)

    def test_malformed_worker_output_preserved_and_descendants_blocked(self):
        factory, launches = self.fake_factory(malformed="hash:alpha")
        with patch.object(executor.subprocess, "Popen", factory):
            output = executor.run_plan_data(self.plan(), self.base / "malformed")
        results = {row["task_id"]: row for row in core.read_json(output / "semantic.json")["tasks"]}
        self.assertEqual(results["hash:alpha"]["status"], "failed")
        self.assertEqual(results["verify:alpha"]["status"], "blocked")
        self.assertEqual(results["manifest"]["status"], "blocked")
        self.assertNotIn("verify:alpha", launches)
        self.assertFalse((output / "manifest.json").exists())
        self.assertTrue(any(path.read_text(encoding="utf-8") == "{invalid json" for path in (output / "private").glob("*_worker_result.json")))
        self.assert_integrity(output)

    def test_timeout_kills_worker_and_never_retries(self):
        factory, launches = self.fake_factory(hanging="hash:alpha")
        ticks = itertools.count(0, 200_000_000)
        with patch.object(executor.subprocess, "Popen", factory), patch.object(executor.time, "perf_counter_ns", side_effect=lambda: next(ticks)):
            output = executor.run_plan_data(self.plan(timeout=0.1), self.base / "timeout")
        results = {row["task_id"]: row for row in core.read_json(output / "semantic.json")["tasks"]}
        self.assertEqual(results["hash:alpha"]["status"], "timed_out")
        self.assertEqual(launches.count("hash:alpha"), 1)
        self.assertEqual(results["manifest"]["status"], "blocked")
        self.assertFalse(core.read_json(output / "semantic.json")["accepted"])
        self.assert_integrity(output)

    def test_changed_input_rejected_before_output_or_launch(self):
        plan = self.plan()
        self.source.write_bytes(b"changed")
        output = self.base / "stale"
        with patch.object(executor.subprocess, "Popen") as launch, self.assertRaises(core.LabError):
            executor.run_plan_data(plan, output)
        launch.assert_not_called()
        self.assertFalse(output.exists())

    def test_unexpected_scheduler_failure_retains_rejected_evidence(self):
        factory, launches = self.fake_factory()

        class BrokenQueue(queue.Queue):
            def get_nowait(self):
                raise RuntimeError("injected scheduler completion fault")

        with patch.object(executor.subprocess, "Popen", factory), patch.object(executor.queue, "Queue", BrokenQueue):
            output = executor.run_plan_data(self.plan(), self.base / "controller_fault")
        self.assertTrue(launches)
        self.assertFalse(core.read_json(output / "semantic.json")["accepted"])
        validation = core.read_json(output / "validation.json")
        self.assertEqual(validation["executor_error"]["type"], "RuntimeError")
        self.assertFalse((output / "manifest.json").exists())
        self.assertTrue(list((output / "private").glob("*_worker_result.json")))
        self.assert_integrity(output)

    def test_worker_rejects_unregistered_or_extra_job_fields(self):
        plan = self.plan()
        node = next(node for node in plan["graph"] if node["operation"] == "sha256")
        job = {"schema_version": 1, "node": node, "binding": plan["bindings"][node["id"]],
               "input": self.artifact, "dependencies": {}, "command": "ignored arbitrary command"}
        job_path, result_path = self.base / "bad_job.json", self.base / "bad_result.json"
        core.write_json(job_path, job)
        self.assertEqual(worker.main(["--job", str(job_path), "--result", str(result_path)]), 2)
        self.assertEqual(core.read_json(result_path)["status"], "failed")

    def test_digest_buffers_are_reused_and_empty_inputs_are_supported(self):
        original_open = Path.open
        for size in (0, 3, 33000):
            self.source.write_bytes(b"z" * size)
            artifact = {**self.artifact, "size_bytes": size, "sha256": hashlib.sha256(b"z" * size).hexdigest()}
            for candidate, operation in (("whole_v1", "sha256"), ("stream_v1", "sha256"), ("reference_v1", "reference_sha256")):
                with self.subTest(size=size, candidate=candidate):
                    buffers = []
                    capacity = operators.required_buffer(operation, candidate, artifact)

                    class Reader:
                        def __init__(reader, raw):
                            reader.raw = raw
                        def __enter__(reader):
                            return reader
                        def __exit__(reader, *args):
                            reader.raw.close()
                        def readinto(reader, buffer):
                            self.assertLessEqual(len(buffer), capacity)
                            buffers.append(buffer.obj if isinstance(buffer, memoryview) else buffer)
                            return reader.raw.readinto(buffer)
                        def read(reader, *args):
                            self.fail("Digest used allocating read instead of reusable readinto")

                    def instrumented_open(path, *args, **kwargs):
                        raw = original_open(path, *args, **kwargs)
                        if path == self.source:
                            self.assertEqual(kwargs.get("buffering"), 0)
                            return Reader(raw)
                        return raw

                    with patch.object(Path, "open", instrumented_open):
                        result = operators._digest(artifact, candidate)
                    self.assertEqual(result, {"input_id": "alpha", "size_bytes": size, "sha256": artifact["sha256"]})
                    self.assertTrue(buffers)
                    self.assertTrue(all(buffer is buffers[0] for buffer in buffers))
                    self.assertEqual(len(buffers[0]), capacity)


    def test_worker_environment_denies_parent_secrets_and_receipts_list_names_only(self):
        sentinel_name = "MISSION_LAB_SENTINEL_SECRET"
        # Keep the value out of this source file. The run snapshots package
        # sources, and the receipt scan must be able to see a real leak.
        sentinel_value = "ml-sentinel-" + "7f3c9a1e" + "-SECRET"
        granted_name = "MISSION_LAB_GRANTED_TOKEN"
        granted_value = "ml-granted-" + "0c4e8b2d" + "-TOKEN"
        self.source.write_bytes(b"abc")
        artifact = {"id": "alpha", "path": str(self.source), "size_bytes": 3, "sha256": core.file_hash(self.source)}
        mission = {**self.mission, "inputs": [artifact]}
        profile = {"schema_version": 1, "workers": 2, "read_buffer_bytes": 65536, "timeout_seconds": 60,
                   "estimate_origin": "injected_fixture",
                   "worker_env": {"declared": [granted_name, "MISSION_LAB_UNUSED_TOKEN"],
                                  "grants": {"hash:alpha": [granted_name]}}}
        plan = planner.build_plan(mission, profile)
        captured = []
        real_popen = subprocess.Popen

        def spy(command, **kwargs):
            captured.append((command, kwargs.get("env")))
            return real_popen(command, **kwargs)

        parent = os.environ.copy()
        parent[sentinel_name] = sentinel_value
        parent[granted_name] = granted_value
        parent["PYTHONSTARTUP"] = "/tmp/mission-lab-should-not-run.py"
        parent["PYTHONUNBUFFERED"] = "0"
        with patch.dict(os.environ, parent, clear=True), patch.object(executor.subprocess, "Popen", spy):
            output = executor.run_plan_data(plan, self.base / "isolated_env")
        self.assertTrue(core.read_json(output / "semantic.json")["accepted"])
        self.assertEqual(len(captured), len(plan["graph"]))
        observations = {row["task_id"]: row for row in core.read_json(output / "observations.json")}
        granted_env = None
        for command, env in captured:
            job = core.read_json(command[command.index("--job") + 1])
            task_id = job["node"]["id"]
            self.assertIsInstance(env, dict)
            self.assertNotIn(sentinel_name, env)
            self.assertFalse(any(name.casefold() == sentinel_name.casefold() for name in env))
            self.assertNotIn(sentinel_value, tuple(env.values()))
            if os.name == "nt":
                self.assertTrue(all(name == name.upper() for name in env))
            self.assertNotIn("PYTHONSTARTUP", env)
            self.assertNotIn("MISSION_LAB_UNUSED_TOKEN", env)
            self.assertEqual(env.get("PYTHONUNBUFFERED"), "1")
            self.assertEqual(env.get("PYTHONDONTWRITEBYTECODE"), "1")
            self.assertEqual(sorted(env), observations[task_id]["environment_names"])
            self.assertNotIn(sentinel_value, json.dumps(observations[task_id]))
            self.assertNotIn(granted_value, json.dumps(observations[task_id]))
            if task_id == "hash:alpha":
                self.assertEqual(env[granted_name], granted_value)
                granted_env = env
            else:
                self.assertNotIn(granted_name, env)
            probe = subprocess.run(
                [sys.executable, "-B", "-c", "import json,os,sys; sys.stdout.write(json.dumps(sorted(os.environ)))"],
                env=env, capture_output=True, text=True, check=False)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            child_names = json.loads(probe.stdout)
            self.assertEqual(child_names, sorted(env))
            self.assertNotIn(sentinel_name, child_names)
            self.assertFalse(any(name.casefold() == sentinel_name.casefold() for name in child_names))
            self.assertNotIn(sentinel_value, probe.stdout)
        self.assertIsNotNone(granted_env)
        granted_probe = subprocess.run(
            [sys.executable, "-B", "-c",
             "import os,sys; sys.stdout.write(os.environ.get('MISSION_LAB_GRANTED_TOKEN', ''))"],
            env=granted_env, capture_output=True, text=True, check=False)
        self.assertEqual(granted_probe.returncode, 0, granted_probe.stderr)
        self.assertEqual(granted_probe.stdout, granted_value)
        self.assertNotIn(sentinel_value, granted_probe.stdout)
        for path in output.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(sentinel_value.encode(), data)
                self.assertNotIn(granted_value.encode(), data)
        from research.mission_lab_v0_1 import report
        self.assertTrue(report.replay_run(output)["accepted"])
        original_observations = (output / "observations.json").read_text(encoding="utf-8")
        original_events = (output / "events.jsonl").read_text(encoding="utf-8")
        original_integrity = (output / "integrity.json").read_text(encoding="utf-8")
        rows = core.read_json(output / "observations.json")
        events = [json.loads(line) for line in original_events.splitlines()]
        present = [name for name in ("WINDIR", "TZ", "PATHEXT", "TMPDIR", "LC_ALL", "APPDATA", "LANG")
                   if all(name not in row["environment_names"] for row in rows)
                   and all(name not in (event.get("environment_names") or []) for event in events if event.get("kind") == "launched")]
        self.assertTrue(present, "expected an allowed platform name that this run did not pass")
        extra = present[0]

        def reseal():
            seal.reseal(output)

        def restore():
            (output / "observations.json").write_text(original_observations, encoding="utf-8", newline="\n")
            (output / "events.jsonl").write_text(original_events, encoding="utf-8", newline="\n")
            (output / "integrity.json").write_text(original_integrity, encoding="utf-8", newline="\n")

        def contradict():
            reseal()
            with self.assertRaises(core.LabError) as caught:
                report.replay_run(output)
            self.assertIn("contradicts the launch event", str(caught.exception))
            restore()

        for row in rows:
            if row["task_id"] == "hash:alpha":
                row["environment_names"] = sorted(set(row["environment_names"]) | {extra})
        (output / "observations.json").write_text(core.canonical(rows), encoding="utf-8", newline="\n")
        contradict()
        for event in events:
            if event.get("kind") == "launched" and event.get("task_id") == "hash:alpha":
                event["environment_names"] = sorted(set(event["environment_names"]) | {extra})
        (output / "events.jsonl").write_text(
            "".join(json.dumps(event, sort_keys=True, allow_nan=False) + "\n" for event in events),
            encoding="utf-8", newline="\n")
        contradict()
        rows = core.read_json(output / "observations.json")
        events = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        for row in rows:
            if row["task_id"] == "hash:alpha":
                row["environment_names"] = [name for name in row["environment_names"] if name != granted_name]
        for event in events:
            if event.get("kind") == "launched" and event.get("task_id") == "hash:alpha":
                event["environment_names"] = [name for name in event["environment_names"] if name != granted_name]
        (output / "observations.json").write_text(core.canonical(rows), encoding="utf-8", newline="\n")
        (output / "events.jsonl").write_text(
            "".join(json.dumps(event, sort_keys=True, allow_nan=False) + "\n" for event in events),
            encoding="utf-8", newline="\n")
        semantic = core.read_json(output / "semantic.json")
        (output / "report.md").write_text(report.render_report(semantic, rows), encoding="utf-8", newline="\n")
        seal.reseal(output)
        with self.assertRaises(core.LabError) as caught:
            report.replay_run(output)
        self.assertIn("omits a granted variable", str(caught.exception))

    def test_missing_granted_variable_fails_before_process_start(self):
        self.source.write_bytes(b"abc")
        artifact = {"id": "alpha", "path": str(self.source), "size_bytes": 3, "sha256": core.file_hash(self.source)}
        mission = {**self.mission, "inputs": [artifact]}
        profile = {"schema_version": 1, "workers": 2, "read_buffer_bytes": 65536, "timeout_seconds": 60,
                   "estimate_origin": "injected_fixture",
                   "worker_env": {"declared": ["MISSION_LAB_GRANTED_TOKEN"],
                                  "grants": {"hash:alpha": ["MISSION_LAB_GRANTED_TOKEN"]}}}
        plan = planner.build_plan(mission, profile)
        factory, launches = self.fake_factory()
        parent = os.environ.copy()
        parent.pop("MISSION_LAB_GRANTED_TOKEN", None)
        with patch.dict(os.environ, parent, clear=True), patch.object(executor.subprocess, "Popen", factory):
            output = executor.run_plan_data(plan, self.base / "missing_grant")
        self.assertNotIn("hash:alpha", launches)
        results = {row["task_id"]: row for row in core.read_json(output / "semantic.json")["tasks"]}
        self.assertEqual(results["hash:alpha"]["status"], "failed")
        self.assertEqual(results["hash:alpha"]["error"]["message"],
                         "Granted worker environment variable is not set: MISSION_LAB_GRANTED_TOKEN")
        self.assertEqual(results["verify:alpha"]["status"], "blocked")
        self.assertIn("reference:alpha", launches)
        self.assert_integrity(output)
        from research.mission_lab_v0_1 import report
        self.assertFalse(report.replay_run(output)["accepted"])


if __name__ == "__main__":
    unittest.main()
