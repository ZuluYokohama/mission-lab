"""Command-line interface for the isolated Mission Lab controller."""
import argparse
import json
import sys

from . import core


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--mission", required=True)
    for name in ("plan", "run", "replan", "inspect", "demo"):
        command = commands.add_parser(name)
        command.add_argument("--out", required=True)
        if name == "plan":
            command.add_argument("--mission", required=True)
            command.add_argument("--resources", required=True)
        if name in ("run", "replan"):
            command.add_argument("--plan", required=True)
        if name == "replan":
            command.add_argument("--resources", required=True)
            command.add_argument("--trigger", required=True)
        if name == "inspect":
            command.add_argument("--root", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            mission = core.load_mission(args.mission)
            result = {"schema_version": 1, "valid": True, "human_review": False,
                      "status": "needs_clarification" if mission["unresolved"] else "structurally_valid",
                      "questions": mission["unresolved"], "mission_identity": core.digest_object(mission)}
        elif args.command == "plan":
            from .planner import plan_to_output
            result = {"output": str(plan_to_output(args.mission, args.resources, args.out))}
        elif args.command == "run":
            from .executor import run_plan
            output = run_plan(args.plan, args.out)
            semantic = core.read_json(output / "semantic.json")
            result = {"output": str(output), "accepted": semantic["accepted"]}
        elif args.command == "replan":
            from .replan import replan_to_output
            result = {"output": str(replan_to_output(args.plan, args.resources, args.trigger, args.out))}
        elif args.command == "replay":
            from .report import replay_run
            result = replay_run(args.run)
        elif args.command == "inspect":
            from .inventory import inspect_archive
            result = {"output": str(inspect_archive(args.root, args.out))}
        else:
            from .demo import run_demo
            result = {"output": str(run_demo(args.out)), "accepted": True}
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        if args.command == "run" and not result["accepted"]:
            return 1
        return 0
    except (core.LabError, OSError, UnicodeError) as exc:
        print(json.dumps({"error": str(exc), "accepted": False}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
