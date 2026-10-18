#!/usr/bin/env python3
"""Bounded executable checks of t3_done_notify.py:transition debounce and stall rules."""

import argparse
import datetime as dt
import importlib.util
import sys
from pathlib import Path


def load_product(path):
    """Load the product module containing transition(state, thread, now, ...)."""
    spec = importlib.util.spec_from_file_location("t3_done_notify_transition_check", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load product module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check(condition, message):
    """Fail with the violated product behavior and input partition."""
    if not condition:
        raise AssertionError(message)


def main(argv=None):
    """Run finite boundary partitions against the actual t3_done_notify.py:transition."""
    default = Path(__file__).resolve().parents[1] / "t3_done_notify.py"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, default=default)
    product = load_product(parser.parse_args(argv).product.resolve())
    calls = 0

    def transition(state, thread, now, debounce=10.0, stall=2700.0):
        nonlocal calls
        calls += 1
        return product.transition(
            state, thread, now,
            debounce_seconds=debounce,
            stall_seconds=stall,
        )

    now = 100.0
    stopped = {"id": "turn", "status": "completed", "updatedAt": ""}
    # The partitions are absent, one second before, exactly at, and one second
    # after the 10-second debounce threshold, crossed with both record.active values.
    for active in (False, True):
        for pending in (None, now - 9, now - 10, now - 11):
            state = {"active": active, "pending_since": pending, "pending_status": "completed"}
            event = transition(state, stopped, now)
            expected = active and pending is not None and now - pending >= 10
            check(bool(event) == expected, f"debounce active={active}, pending_since={pending}: event={event}")
            if expected:
                check(event["reason"] == "done" and state["active"] is False,
                      f"threshold completion did not commit done: {event}, {state}")

    first = {"active": True, "pending_since": None}
    check(transition(first, stopped, now, debounce=0) is None and first["pending_since"] == now,
          "first stopped observation must only start debounce, including when debounce is zero")

    running = {"id": "turn", "status": "running", "activeRunId": "run", "updatedAt": ""}
    for pending in (None, now - 9, now - 10, now - 11):
        state = {"active": True, "pending_since": pending, "pending_status": "completed"}
        check(transition(state, running, now) is None, f"active resume emitted event: {state}")
        check(state["active"] and state["pending_since"] is None and state["pending_status"] is None,
              f"active resume did not clear debounce state: {state}")

    utc = dt.timezone.utc
    iso = lambda seconds: dt.datetime.fromtimestamp(seconds, utc).isoformat().replace("+00:00", "Z")
    stall_now, stall = product.parse_time("2026-10-18T00:00:00Z"), 2700.0
    stale = iso(stall_now - stall)
    active_thread = {"id": "turn", "status": "running", "activeRunId": "run", "updatedAt": stale}
    for marker, should_emit in ((None, True), (stale, False), ("different-update", True)):
        state = {"active": True, "stalled_updated_at": marker}
        event = transition(state, active_thread, stall_now, stall=stall)
        check(bool(event) == should_emit, f"stall marker={marker!r}: event={event}")
        if event:
            check(event["reason"] == "stalled" and state["stalled_updated_at"] == stale,
                  f"stalled event did not remember updatedAt: {event}, {state}")

    for updated in ("", "not-a-timestamp", iso(stall_now - stall + 1)):
        state = {"active": True, "stalled_updated_at": stale}
        check(transition(state, dict(active_thread, updatedAt=updated), stall_now, stall=stall) is None,
              f"invalid or fresh activity emitted stalled: {updated!r}")
        check(state["stalled_updated_at"] is None, f"fresh activity did not clear stall marker: {state}")

    state = {"active": True, "stalled_updated_at": None}
    results = [transition(state, active_thread, stall_now, stall=stall) for _ in range(3)]
    check([bool(result) for result in results] == [True, False, False],
          f"same stale updatedAt emitted more than one stalled event: {results}")
    newer_stale = dict(active_thread, updatedAt=iso(stall_now - stall + 1))
    event = transition(state, newer_stale, stall_now + 1, stall=stall)
    check(event is not None and event["reason"] == "stalled", "new stale updatedAt did not allow a new stall event")
    check(transition(state, newer_stale, stall_now + 1, stall=stall) is None,
          "repeated new stale updatedAt emitted a second stall event")

    print(f"PASS: {calls} actual transition calls; debounce={10}s partitions absent/9/10/11s crossed with active=false/true; stall={stall}s partitions invalid/fresh/at-threshold and marker none/equal/different; repeated same/new updatedAt.")
    print("Scope: bounded executable checks of transition only; not a TLA proof of the full watcher or filesystem/outbox behavior.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
