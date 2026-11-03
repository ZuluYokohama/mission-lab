"""A real bounded audit with explicitly injected planning/revision conditions."""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import core
from .planner import build_plan, write_plan
from .replan import replan_data
from .report import replay_run, seal_integrity


def fixture_mission():
    folder = core.ROOT / "fixtures"
    folder.mkdir(exist_ok=True)
    contents = {"empty.bin": b"", "abc.bin": b"abc", "pattern.bin": bytes(range(256)) * 512 + b"!"}
    inputs = []
    for name, content in contents.items():
        path = folder / name
        if path.exists():
            if path.read_bytes() != content:
                raise core.LabError("Existing fixture changed: " + name)
        else:
            with path.open("xb") as handle:
                handle.write(content)
        inputs.append({"id": Path(name).stem, "path": str(path.resolve()), "size_bytes": len(content),
                       "sha256": hashlib.sha256(content).hexdigest()})
    mission = {"schema_version": 1, "mission_id": "file-audit-demo",
            "requirements": [{"id": "exact-audit", "text": "Audit each declared file without changing it.",
                              "acceptance_criteria": ["Exact SHA-256 and byte count", "Independent sequential reference agreement", "Explicit resource reservations"],
                              "evidence_refs": []}], "inputs": inputs, "unresolved": []}
    portable = {**mission, "inputs": [{**item, "path": Path(item["path"]).name} for item in inputs]}
    for name, value in (("mission.json", portable), ("resources.json", core.default_resources()),
                        ("resources_tight.json", {**core.default_resources(), "read_buffer_bytes": 65536})):
        path = folder / name
        if path.exists():
            if core.read_json(path) != value:
                raise core.LabError("Existing fixture configuration changed: " + name)
        else:
            core.write_json(path, value)
    return mission


def run_demo(out):
    from .executor import run_plan_data
    mission = fixture_mission()
    baseline = build_plan(mission, core.default_resources())
    output = core.new_output(out)
    write_plan(baseline, core.new_output(output / "baseline_plan"))
    baseline_run = run_plan_data(baseline, output / "baseline_run")
    tight = {**baseline["resources"], "read_buffer_bytes": 65536}
    trigger = {"schema_version": 1, "kind": "resource_change", "text": "Injected available read-buffer reservation reduced to 64 KiB.",
               "evidence_origin": "injected_demo", "evidence_refs": []}
    revised = replan_data(baseline, tight, trigger)
    write_plan(revised, core.new_output(output / "revised_plan"))
    revised_run = run_plan_data(revised, output / "revised_run")
    before, after = replay_run(baseline_run), replay_run(revised_run)
    first = core.read_json(Path(baseline_run) / "semantic.json")
    second = core.read_json(Path(revised_run) / "semantic.json")
    def artifacts(semantic):
        return sorted((task["output"]["input_id"], task["output"]["sha256"], task["output"]["size_bytes"])
                      for task in semantic["tasks"] if task["task_id"].startswith("verify:") and task["status"] == "succeeded")
    same = artifacts(first) == artifacts(second) and len(artifacts(first)) == len(mission["inputs"])
    changed = baseline["bindings"] != revised["bindings"]
    accepted = before["accepted"] and after["accepted"] and same and changed
    comparison = {"schema_version": 1, "accepted": accepted, "same_required_results": same,
                  "changed_bindings": changed, "baseline_plan_id": baseline["plan_id"], "revised_plan_id": revised["plan_id"],
                  "baseline_counts": before["counts"], "revised_counts": after["counts"],
                  "revision": revised["revision"], "budget_change_origin": "injected_demo", "cost_estimate_origin": "injected_fixture",
                  "timing_origin": "observed", "human_review": False, "independent_reproduction": False}
    core.write_json(output / "comparison.json", comparison)
    core.write_json(output / "replay_checks.json", {"baseline": {k: v for k, v in before.items() if k != "report"},
                                                    "revised": {k: v for k, v in after.items() if k != "report"}})
    (output / "report.md").write_text("# Mission Lab demonstration\n\n" +
        f"Acceptance: **{'passed' if accepted else 'failed'}**.\n\n" +
        "Both runs audit the same three inputs against a separate sequential reference.\n\n" +
        f"Identical required results: {same}. Changed bindings: {changed}.\n\n" +
        "The budget change and cost model are injected fixtures. Execution timings are observed; no speedup or energy-saving claim is made.\n\n" +
        "See baseline_run/report.md and revised_run/report.md for complete receipts and retained failures.\n", encoding="utf-8", newline="\n")
    seal_integrity(output)
    if not accepted:
        raise core.LabError("Demonstration did not meet acceptance; evidence retained at " + str(output))
    return output
