"""Generated controller seal key for pytest.

Package tests run under pytest. This fixture writes 32 random bytes to a
temporary file and points MISSION_LAB_SEAL_KEY_FILE at it. The key is not a
credential and is not committed. unittest discovery does not load this file.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def controller_seal_key(tmp_path_factory, monkeypatch):
    directory = tmp_path_factory.mktemp("seal-key")
    path = directory / "seal.key"
    path.write_bytes(os.urandom(32))
    monkeypatch.delenv("MISSION_LAB_SEAL_KEY", raising=False)
    monkeypatch.setenv("MISSION_LAB_SEAL_KEY_FILE", str(path))
    return path
