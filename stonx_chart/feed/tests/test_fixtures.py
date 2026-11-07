import json
from pathlib import Path

from stonx_chart.feed.fixtures import FIXTURE_SEED, FIXTURE_START_MS, WIRE_DIR, build_fixtures, write_fixtures

FILES = ("nvda_snapshot_300s.json", "nvda_stream_300_600s.json", "nvda_snapshot_600s.json")


def load(name: str):
    return json.loads((WIRE_DIR / name).read_text(encoding="utf-8"))


def test_constants():
    assert FIXTURE_SEED == 7
    assert FIXTURE_START_MS == 1_793_975_400_000


def test_build_fixtures_has_expected_files():
    assert tuple(build_fixtures()) == FILES


def test_committed_fixtures_match_regenerated(tmp_path: Path):
    write_fixtures(tmp_path)
    for name in FILES:
        assert (tmp_path / name).read_bytes() == (WIRE_DIR / name).read_bytes()


def test_fixture_sizes():
    assert all((WIRE_DIR / n).stat().st_size <= 256 * 1024 for n in FILES)


def test_stream_seq_contiguous():
    messages = load("nvda_stream_300_600s.json")
    seqs = [m["seq"] for m in messages]
    assert seqs and seqs == list(range(2, 2 + len(seqs)))
    assert {m["v"] for m in messages} == {1}


def test_snapshots_start_at_seq_1():
    for name in ("nvda_snapshot_300s.json", "nvda_snapshot_600s.json"):
        message = load(name)
        assert message["type"] == "snapshot" and message["seq"] == 1 and message["asset"] == "NVDA"


def test_snapshot_600_covers_every_overlay_kind():
    ids = [o["id"] for o in load("nvda_snapshot_600s.json")["overlays"]]
    for prefix in ("band.fragmented.", "band.high_curl.", "band.holonomy.", "band.halt.", "marker.reset.", "marker.curl."):
        assert any(i.startswith(prefix) for i in ids), prefix
