"""Wire messages of the StonX chart stream: payload builders, the envelope and SSE framing.

This module is the Python half of a contract with the TypeScript client (design spec
section 6), so key names and key order are fixed. A payload is a dict whose first key is
`type`. `Envelope.wrap` turns a payload into the message that goes on the wire:

    {"v": 1, "type": ..., "source": ..., "seq": n, <the payload's other keys, in order>}

The envelope keys `v`, `type`, `source` and `seq` are reserved: a payload supplies `type` and
must not use `v`, `source` or `seq` (`wrap` raises). A payload that carries arrays, such as a
series' points, nests them under one key instead of using a top-level `v`.

All times are integer milliseconds since the epoch. `encode_sse` refuses NaN and infinity
(`allow_nan=False`), so a non-finite value fails loudly here instead of reaching the browser
as invalid JSON. Standard library only.
"""

import json
from itertools import count
from typing import Any

from stonx_chart.feed.records import ASSETS
from stonx_chart.feed.session import Bar

SCHEMA_VERSION = 1
SOURCE_SIMULATED = "SIMULATED_DEMO"
_ENVELOPE_KEYS = frozenset({"v", "source", "seq"})  # `type` is the payload's own

Payload = dict[str, Any]

_NS_PER_MS = 1_000_000


def ns_to_ms(ns: int) -> int:
    """Epoch nanoseconds to epoch milliseconds, rounding down."""
    return ns // _NS_PER_MS


def bar_payload(bar: Bar) -> Payload:
    return {"type": "bar", "t": bar.t_ms, "o": bar.o, "h": bar.h, "l": bar.l, "c": bar.c, "vol": bar.vol}


def snapshot_payload(
    asset: int, session_start_ms: int, bars: list[Bar], overlays: list[dict], points: dict
) -> Payload:
    """The first message of a connection: everything a client needs to rebuild its state.

    `bars` becomes columns (one list per field). `overlays` and `points` are embedded as given.
    """
    return {
        "type": "snapshot",
        "asset": ASSETS[asset],
        "assets": [{"id": i, "symbol": symbol} for i, symbol in enumerate(ASSETS)],
        "session_start_ms": session_start_ms,
        "bars": {
            "t": [b.t_ms for b in bars],
            "o": [b.o for b in bars],
            "h": [b.h for b in bars],
            "l": [b.l for b in bars],
            "c": [b.c for b in bars],
            "vol": [b.vol for b in bars],
        },
        "overlays": overlays,
        "points": points,
    }


def heartbeat_payload(now_ms: int) -> Payload:
    return {"type": "heartbeat", "now_ms": now_ms}


def status_payload(feed_state: str, drop_count: int, message: str | None) -> Payload:
    payload: Payload = {"type": "status", "feed_state": feed_state, "drop_count": drop_count}
    if message is not None:
        payload["message"] = message
    return payload


class Envelope:
    """Numbers and labels the messages of one connection. `seq` starts at 1 and counts up by 1.

    Use one envelope per connection, from the connection's own writer thread.
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._seq = count(1)

    def wrap(self, payload: Payload) -> Payload:
        """A new dict: `v`, `type`, `source`, `seq`, then the payload's other keys. Leaves `payload` as is.

        Raises ValueError, without using a `seq`, if the payload has a key of its own named `v`,
        `source` or `seq`: it would silently replace the envelope's value.
        """
        clash = _ENVELOPE_KEYS.intersection(payload)
        if clash:
            raise ValueError(f"payload {payload['type']!r} uses reserved envelope keys: {sorted(clash)}")
        message: Payload = {
            "v": SCHEMA_VERSION,
            "type": payload["type"],
            "source": self._source,
            "seq": next(self._seq),
        }
        for key, value in payload.items():
            if key != "type":
                message[key] = value
        return message


def encode_sse(message: Payload) -> bytes:
    """One server-sent event: a single `data:` line holding compact JSON, then a blank line."""
    body = json.dumps(message, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
    return b"data: " + body.encode() + b"\n\n"
