"""One registered operation per isolated Python process; no child processes."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .core import CONTROLLER_KEY_ENV, read_json, write_json
from .operators import execute_operation


def drop_controller_keys(env=None):
    """Remove controller key names from a mapping. This does not erase the process image.

    On Linux the initial environment block can still hold a value the parent passed.
    The control that matters is that the controller does not pass these names.
    """
    target = os.environ if env is None else env
    folded = {name.casefold() for name in CONTROLLER_KEY_ENV}
    for name in [key for key in target if isinstance(key, str) and key.casefold() in folded]:
        target.pop(name, None)
    return target


def main(argv=None):
    drop_controller_keys()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    task_id = "unknown"
    try:
        job = read_json(args.job)
        task_id = job["node"]["id"]
        output = execute_operation(job)
        result = {"task_id": task_id, "status": "succeeded", "output": output, "error": None}
        code = 0
    except Exception as error:
        result = {"task_id": task_id, "status": "failed", "output": None,
                  "error": {"type": type(error).__name__, "message": str(error)}}
        code = 2
    try:
        write_json(args.result, result)
    except Exception as error:
        print("Worker could not preserve its result: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps({"task_id": task_id, "status": result["status"]}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
