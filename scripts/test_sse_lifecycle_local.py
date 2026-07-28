"""Offline smoke test for the /events SSE lifecycle and replay buffer.

No GCP credentials, no ADK, no network: only the pure helpers around the
broadcast fabric in agents.server are exercised.

Why this exists: one open console tab used to cost ~85,000 billable
instance-seconds per day. The generator had no exit condition, so every
connection ran to the Cloud Run request timeout (3600s) and EventSource
reconnected immediately — a ~98% duty cycle for a tab nobody was looking at.
The fix bounds the connection (max lifetime / idle close) and replays missed
events from a ring buffer so the deliberate closes stay invisible to the user.

The bound is only meaningful if it stays below the Cloud Run request timeout,
so that is asserted here rather than left as a comment.

Usage:
    python scripts/test_sse_lifecycle_local.py
"""
import asyncio
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "packages", "agent", "src")
)

import agents.server as server  # noqa: E402

# The Cloud Run request timeout the old code ran into every single time.
CLOUD_RUN_REQUEST_TIMEOUT_S = 3600

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def reset_bus() -> None:
    server._event_ring.clear()
    server._event_seq = 0
    server._console_subscribers.clear()


def test_parse_last_event_id() -> None:
    cases = [
        (None, 0, "missing header -> 0"),
        ("", 0, "empty -> 0"),
        ("  ", 0, "whitespace -> 0"),
        ("5", 5, "plain integer"),
        (" 7 ", 7, "surrounding whitespace"),
        ("abc", 0, "malformed -> 0 (not a crash)"),
        ("-3", 0, "negative clamped to 0"),
        ("3.5", 0, "float string -> 0"),
    ]
    for raw, expected, label in cases:
        got = server._parse_last_event_id(raw)
        check(f"_parse_last_event_id: {label}", got == expected, f"expected {expected}, got {got}")


def test_broadcast_sequences_and_fans_out() -> None:
    reset_bus()
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    server._console_subscribers.append(q)

    server._broadcast({"type": "run_started"})
    server._broadcast({"type": "done"})

    check("broadcast increments the sequence", server._event_seq == 2, f"seq={server._event_seq}")
    check("broadcast fills the ring", len(server._event_ring) == 2, f"ring={len(server._event_ring)}")

    delivered = [q.get_nowait() for _ in range(q.qsize())]
    shapes_ok = (
        len(delivered) == 2
        and delivered[0][0] == 1
        and delivered[1][0] == 2
        and delivered[0][1]["type"] == "run_started"
    )
    check(
        "subscribers receive (seq, event) pairs in order",
        shapes_ok,
        f"delivered={delivered}",
    )


def test_broadcast_never_raises_on_full_queue() -> None:
    reset_bus()
    full: asyncio.Queue = asyncio.Queue(maxsize=1)
    full.put_nowait((0, {"type": "filler"}))
    healthy: asyncio.Queue = asyncio.Queue(maxsize=10)
    server._console_subscribers.extend([full, healthy])

    raised = None
    try:
        server._broadcast({"type": "run_started"})
    except Exception as e:  # noqa: BLE001
        raised = e

    check("broadcast does not raise on a full queue", raised is None, f"raised {raised!r}")
    check(
        "a full/dead subscriber does not starve a healthy one",
        healthy.qsize() == 1,
        f"healthy queue size={healthy.qsize()}",
    )


def test_replay() -> None:
    reset_bus()
    for i in range(5):
        server._broadcast({"type": "tool_call", "n": i})
    now = server.time.time()

    fresh = server._replay_since(0, now)
    check(
        "fresh connection (no Last-Event-ID) replays nothing",
        fresh == [],
        f"got {len(fresh)} events - a new console would replay a stale run",
    )

    resumed = server._replay_since(3, now)
    check(
        "reconnect replays only what was missed",
        [seq for seq, _ in resumed] == [4, 5],
        f"got {[seq for seq, _ in resumed]}",
    )

    caught_up = server._replay_since(5, now)
    check("caller already at head replays nothing", caught_up == [], f"got {caught_up}")

    ahead = server._replay_since(99, now)
    check("id ahead of the ring replays nothing", ahead == [], f"got {ahead}")


def test_replay_ttl_and_bound() -> None:
    reset_bus()
    now = server.time.time()
    stale = now - server._EVENT_RING_TTL_S - 1
    server._event_ring.append((1, stale, {"type": "old"}))
    server._event_ring.append((2, now, {"type": "new"}))

    # Resume from id 1: id 2 is in window, and the aged-out entry is excluded
    # both by id and by TTL. Resume from id 0 would replay nothing by design,
    # so it cannot distinguish "dropped by TTL" from "fresh connection".
    resumed = [ev["type"] for _, ev in server._replay_since(1, now)]
    check(
        "events older than the TTL are dropped from replay",
        resumed == ["new"],
        f"got {resumed} (the stale event must not be resent)",
    )

    reset_bus()
    for _ in range(server._EVENT_RING_MAX + 50):
        server._broadcast({"type": "noise"})
    check(
        "ring buffer is bounded",
        len(server._event_ring) == server._EVENT_RING_MAX,
        f"len={len(server._event_ring)} max={server._EVENT_RING_MAX}",
    )


def test_connection_is_bounded() -> None:
    check(
        "max lifetime is below the Cloud Run request timeout",
        0 < server._SSE_MAX_LIFETIME_S < CLOUD_RUN_REQUEST_TIMEOUT_S,
        f"_SSE_MAX_LIFETIME_S={server._SSE_MAX_LIFETIME_S} must be < "
        f"{CLOUD_RUN_REQUEST_TIMEOUT_S}; at or above it the connection runs the full "
        "request timeout again, which is the original billing bug",
    )
    check(
        "idle close fires no later than the max lifetime",
        0 < server._SSE_IDLE_CLOSE_S <= server._SSE_MAX_LIFETIME_S,
        f"idle={server._SSE_IDLE_CLOSE_S} max={server._SSE_MAX_LIFETIME_S}",
    )
    check(
        "replay window covers the max lifetime",
        server._EVENT_RING_TTL_S >= server._SSE_MAX_LIFETIME_S,
        f"ttl={server._EVENT_RING_TTL_S} max={server._SSE_MAX_LIFETIME_S} - a shorter "
        "window would drop events the console is entitled to resume from",
    )


def main() -> int:
    test_parse_last_event_id()
    test_broadcast_sequences_and_fans_out()
    test_broadcast_never_raises_on_full_queue()
    test_replay()
    test_replay_ttl_and_bound()
    test_connection_is_bounded()
    reset_bus()

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f" - {detail}"
        print(line)
    print(f"\n{passed}/{len(results)} pass")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
