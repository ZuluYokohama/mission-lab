"""Write a one-job HMAC seal key for the hosted test workflow.

The key is random bytes under the runner temp directory. It is not a
repository secret, and this script does not print it. Local demos generate
their own key; they do not run this script.
"""
from __future__ import annotations

import os
from pathlib import Path


def main():
    root = os.environ.get("RUNNER_TEMP")
    github_env = os.environ.get("GITHUB_ENV")
    if not root or not github_env:
        raise SystemExit("RUNNER_TEMP and GITHUB_ENV are required")
    path = Path(root) / "mission-lab-seal.key"
    path.write_bytes(os.urandom(32))
    with Path(github_env).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("MISSION_LAB_SEAL_KEY_FILE=" + str(path) + "\n")


if __name__ == "__main__":
    main()
