"""Executor resource, failure, and evidence contracts using stdlib fixtures."""
from __future__ import annotations

import hashlib
import itertools
import json
import queue
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from research.mission_lab_v0_1 import core, executor, operators, planner, worker


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


if __name__ == "__main__":
    unittest.main()
