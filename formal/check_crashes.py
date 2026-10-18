#!/usr/bin/env python3
"""Replay watcher crash windows against the product module without network access.

This is an executable crash harness for ``t3_done_notify.py``.  Each model step
calls the named product function directly; temporary directories stand in for
the state file, Claude transcript tree, and daybook.  Delivery is replaced by a
local function in the outbox scenarios.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock


class SimulatedCrash(BaseException):
    """Stop execution at an injected crash boundary, bypassing Exception handlers."""


class InvariantFailure(AssertionError):
    """An actual product replay violated the durable-notification invariant."""


def load_product(path: Path):
    """Import the product copy under test from the requested file path."""
    spec = importlib.util.spec_from_file_location("t3_done_notify_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import product module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_config(product, root: Path, *, debounce_seconds=1):
    """Build an isolated product Config with only local filesystem paths."""
    config_dir = root / "config"
    state_dir = root / "state"
    env = {
        "T3_DONE_CONFIG_DIR": str(config_dir),
        "T3_DONE_STATE_FILE": str(state_dir / "state.json"),
        "T3_DONE_DAYBOOK_DIR": str(state_dir / "daybook"),
        "T3_DONE_DEBOUNCE_SECONDS": str(debounce_seconds),
        "T3_DONE_SOURCES": "claude",
        "CLAUDE_PROJECTS_DIR": str(root / "claude-projects"),
    }
    return product.load_config(environ=env)


def write_claude_completion(path: Path, session_id: str, text: str):
    """Create a real Claude JSONL completion parsed by parse_claude_session."""
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = "2026-10-18T00:00:00Z"
    rows = [
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": f"{session_id}-turn-1",
            "timestamp": timestamp,
            "message": {"content": "Please handle this request."},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": f"{session_id}-answer-1",
            "timestamp": timestamp,
            "message": {
                "content": text,
                "stop_reason": "end_turn",
            },
        },
    ]
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def run_index_loss(product):
    """Verify B remains discoverable when A commits and the process crashes."""
    with tempfile.TemporaryDirectory(prefix="t3-done-index-crash-") as raw_root:
        root = Path(raw_root)
        config = make_config(product, root)
        state_path = config.state_file
        daybook_dir = config.daybook_dir

        # Persist the requested empty local index before either new transcript exists.
        state = {"_files": {}}
        product.save_state(state, state_path)
        state = product.load_state(state_path)

        transcript_a = Path(config.claude_projects_dir) / "project" / "A.jsonl"
        transcript_b = Path(config.claude_projects_dir) / "project" / "B.jsonl"
        write_claude_completion(transcript_a, "session-A", "Could you approve this change?")
        write_claude_completion(transcript_b, "session-B", "Please approve this change.")

        # Preserve real discovery and parsing, but make the first committed session
        # deterministic even on filesystems whose scandir order is unspecified.
        discover = product._discover_observations

        def ordered_discovery(config_arg):
            observations, loose, unstable = discover(config_arg)
            observations.sort(key=lambda item: item["thread"]["id"])
            return observations, loose, unstable

        save = product.save_state
        saves = 0

        def save_then_crash_after_first_pending(candidate, path=None):
            nonlocal saves
            save(candidate, path)
            saves += 1
            if saves == 1:
                raise SimulatedCrash("crash after first _commit_observation state replace")

        state = product.load_state(state_path)
        try:
            with mock.patch.object(product, "_discover_observations", ordered_discovery), \
                    mock.patch.object(product, "save_state", save_then_crash_after_first_pending):
                product.poll_local_sessions(
                    state,
                    now=100.0,
                    config=config,
                    save_path=state_path,
                    daybook_dir=daybook_dir,
                )
        except SimulatedCrash:
            pass
        else:
            raise AssertionError("the injected crash after A's saved pending record did not fire")

        disk_after_crash = product.load_state(state_path)
        path_a = str(transcript_a)
        path_b = str(transcript_b)
        if set(disk_after_crash.get("_files", {})) != {path_a}:
            raise InvariantFailure("index recovery invariant failed: B index advanced before its observation")
        if "session-A" not in disk_after_crash or "session-B" in disk_after_crash:
            raise AssertionError("crash point did not leave only session-A's pending record")
        if disk_after_crash["session-A"].get("pending_since") != 100.0:
            raise AssertionError("session-A was not persisted in its initial debounce-pending state")

        trace = [
            "1. Start with persisted state {'_files': {}}; then create completed attention transcripts A and B.",
            "2. _track_files records both paths as changed; poll_local_sessions saves A's pending debounce record.",
            "3. Crash immediately after save_state replaces the state file. Disk has A's record/index; B remains unindexed and discoverable.",
        ]
        state = product.load_state(state_path)
        for index in range(1, 4):
            product.poll_local_sessions(
                state,
                now=100.0 + index,
                config=config,
                save_path=state_path,
                daybook_dir=daybook_dir,
            )
            trace.append(
                f"{3 + index}. Restart poll {index}: recover both completions; "
                f"outbox IDs are {[item['payload']['thread_id'] for item in state.get('_outbox', [])]}."
            )

        delivered_payloads = [
            item["payload"]
            for item in state.get("_outbox", [])
            if item.get("payload", {}).get("source") == "claude"
        ]
        counts = {
            "session-A": sum(item.get("thread_id") == "session-A" for item in delivered_payloads),
            "session-B": sum(item.get("thread_id") == "session-B" for item in delivered_payloads),
        }
        if counts != {"session-A": 1, "session-B": 1}:
            raise InvariantFailure(f"index recovery invariant failed: counts={counts}")
        print("PASS INDEX RECOVERY: A and B each queued once after restart")
        print("\n".join(trace))


def make_payload(thread_id: str, completion_key: str):
    """Build the completed attention payload consumed by _commit_observation."""
    return {
        "thread_id": thread_id,
        "title": thread_id,
        "status": "completed",
        "reason": "done",
        "updated_at": "2026-10-18T00:00:00Z",
        "last_message": "Could you approve this?",
        "source": "claude",
        "completion_key": completion_key,
    }


def is_done_record(state, thread_id):
    """Recognize the durable completion fields written by _commit_observation."""
    record = state.get(thread_id)
    return isinstance(record, dict) and bool(record.get("completed_turn") or record.get("last_fired"))


def outbox_items(state, thread_id):
    """Return persisted _outbox items for one product payload thread ID."""
    return [
        item
        for item in state.get("_outbox", [])
        if item.get("payload", {}).get("thread_id") == thread_id
    ]


def assert_durable_or_accepted(state, thread_id, accepted_ids, where):
    """Check that a done record has a queued item or a successful delivery."""
    if is_done_record(state, thread_id) and not outbox_items(state, thread_id) \
            and thread_id not in accepted_ids:
        raise InvariantFailure(
            f"outbox invariant failed at {where}:\n"
            f"1. _commit_observation persisted {thread_id!r} as completed and remembered its completion key.\n"
            "2. After restart, _outbox has no matching payload and no webhook accepted it.\n"
            "3. poll_local_sessions treats that completion as already completed, so the notification is lost."
        )


def commit_payload(product, state, state_path, daybook_dir, thread_id, completion_key):
    """Run the actual _commit_observation daybook/state transaction for one turn."""
    record = {
        "active": False,
        "completed_turn": completion_key,
        "source": "claude",
        "transcript_paths": [],
    }
    payload = make_payload(thread_id, completion_key)
    product._commit_observation(
        state,
        record,
        thread_id,
        payload,
        200.0,
        state_path,
        daybook_dir,
        completed_key=completion_key,
    )


def run_durable_outbox(product):
    """Exercise _commit_observation, save_state, flush_outbox, and deliver crash gaps."""
    env = {
        "WEBHOOK_URL": "https://webhook.invalid/test",
        "WEBHOOK_KEY": "local-test-key",
    }
    seed_id = "seeded-turn"
    seed_key = "completion-seeded-turn"

    # First check the key invariant after a completed observation has been committed.
    with tempfile.TemporaryDirectory(prefix="t3-done-outbox-commit-") as raw_root:
        root = Path(raw_root)
        state_path = str(root / "state.json")
        daybook_dir = str(root / "daybook")
        product.save_state({}, state_path)
        state = product.load_state(state_path)
        commit_payload(product, state, state_path, daybook_dir, seed_id, seed_key)
        persisted = product.load_state(state_path)
        assert_durable_or_accepted(persisted, seed_id, set(), "after save_state / process restart")
        if not outbox_items(persisted, seed_id):
            raise AssertionError("_commit_observation did not queue the attention payload")

    # A crash before os.replace leaves no done record; re-discovery and retry commit it.
    with tempfile.TemporaryDirectory(prefix="t3-done-outbox-before-replace-") as raw_root:
        root = Path(raw_root)
        state_path = str(root / "state.json")
        daybook_dir = str(root / "daybook")
        product.save_state({"marker": "old-state"}, state_path)
        state = product.load_state(state_path)
        def crash_before_replace(_source, _destination):
            raise SimulatedCrash("crash immediately before os.replace")

        try:
            with mock.patch.object(product.os, "replace", crash_before_replace):
                commit_payload(product, state, state_path, daybook_dir, "before-replace", "key-before")
        except SimulatedCrash:
            pass
        else:
            raise AssertionError("the injected pre-os.replace crash did not fire")
        restarted = product.load_state(state_path)
        if is_done_record(restarted, "before-replace"):
            raise AssertionError("pre-replace crash unexpectedly committed the done record")
        daybook_files = list(Path(daybook_dir).glob("*.md"))
        if len(daybook_files) != 1:
            raise AssertionError("the daybook append did not happen before the pre-replace crash")
        commit_payload(product, restarted, state_path, daybook_dir, "before-replace", "key-before")
        retried = product.load_state(state_path)
        assert_durable_or_accepted(retried, "before-replace", set(), "after retry following pre-replace crash")
        if len(outbox_items(retried, "before-replace")) != 1:
            raise AssertionError("replayed completion did not leave exactly one queued payload")

    # A crash after os.replace has a durable record and durable queue on restart.
    with tempfile.TemporaryDirectory(prefix="t3-done-outbox-after-replace-") as raw_root:
        root = Path(raw_root)
        state_path = str(root / "state.json")
        daybook_dir = str(root / "daybook")
        product.save_state({"marker": "old-state"}, state_path)
        state = product.load_state(state_path)
        replace = product.os.replace

        def replace_then_crash(source, destination):
            replace(source, destination)
            raise SimulatedCrash("crash immediately after os.replace")

        try:
            with mock.patch.object(product.os, "replace", replace_then_crash):
                commit_payload(product, state, state_path, daybook_dir, "after-replace", "key-after")
        except SimulatedCrash:
            pass
        else:
            raise AssertionError("the injected post-os.replace crash did not fire")
        restarted = product.load_state(state_path)
        assert_durable_or_accepted(restarted, "after-replace", set(), "after post-replace crash / restart")

        failed_attempts = []

        def webhook_failure(payload, _env):
            failed_attempts.append(payload["thread_id"])
            return False

        with mock.patch.object(product, "deliver", webhook_failure):
            product.flush_outbox(restarted, env, save_path=state_path)
        if failed_attempts != ["after-replace"] or len(outbox_items(restarted, "after-replace")) != 1:
            raise AssertionError("webhook failure did not leave the persisted outbox item queued")
        assert_durable_or_accepted(
            product.load_state(state_path), "after-replace", set(), "after failed webhook attempt / restart"
        )

    print("DURABLE-OUTBOX REPLAY: PASS")
    print("1. _commit_observation saves a done record and matching _outbox item; restart retains both.")
    print("2. Crash before os.replace leaves the old state and a daybook append; re-observation retries the commit and queues the payload.")
    print("3. Crash after os.replace leaves the done record and queued payload on disk; a failed webhook attempt keeps it queued across restart.")


def run_delivery_duplicate(product):
    """Replay flush_outbox acknowledgement loss and t3_done_notify.py:deliver retries."""
    env = {
        "WEBHOOK_URL": "https://webhook.invalid/test",
        "WEBHOOK_KEY": "local-test-key",
    }
    # Successful remote acceptance followed by a crash before the ack save is the
    # intentional duplicate window in flush_outbox.
    duplicate_id = "accepted-before-ack-crash"
    duplicate_key = "completion-accepted-before-ack-crash"
    with tempfile.TemporaryDirectory(prefix="t3-done-outbox-ack-crash-") as raw_root:
        root = Path(raw_root)
        state_path = str(root / "state.json")
        daybook_dir = str(root / "daybook")
        product.save_state({}, state_path)
        state = product.load_state(state_path)
        commit_payload(product, state, state_path, daybook_dir, duplicate_id, duplicate_key)
        accepted_ids = []
        event_ids = []

        def accepted(payload, _env):
            accepted_ids.append(payload["thread_id"])
            event_ids.append(payload.get("event_id"))
            return True

        save = product.save_state

        def crash_before_ack_save(_candidate, _path=None):
            raise SimulatedCrash("crash after webhook accepted, before flush_outbox saves its acknowledgement")

        try:
            with mock.patch.object(product, "deliver", accepted), \
                    mock.patch.object(product, "save_state", crash_before_ack_save):
                product.flush_outbox(state, env, save_path=state_path)
        except SimulatedCrash:
            pass
        else:
            raise AssertionError("the injected pre-ack-save crash did not fire")

        restarted = product.load_state(state_path)
        assert_durable_or_accepted(restarted, duplicate_id, set(), "restart after acceptance / lost local ack")
        if len(outbox_items(restarted, duplicate_id)) != 1:
            raise AssertionError("the unacknowledged item was not available for at-least-once retry")
        with mock.patch.object(product, "deliver", accepted), mock.patch.object(product, "save_state", save):
            product.flush_outbox(restarted, env, save_path=state_path)
        final_state = product.load_state(state_path)
        if accepted_ids != [duplicate_id, duplicate_id]:
            raise AssertionError(f"expected one explicit retry duplicate, got deliveries {accepted_ids}")
        if not event_ids[0] or len(set(event_ids)) != 1:
            raise InvariantFailure("event identity invariant failed across acknowledgement crash")
        if outbox_items(final_state, duplicate_id):
            raise AssertionError("successful retry did not save its outbox acknowledgement")
        assert_durable_or_accepted(
            final_state, duplicate_id, set(accepted_ids), "after successful retry and saved acknowledgement"
        )

    print("1. A webhook acceptance followed by a crash before flush_outbox saves its acknowledgement causes one retry duplicate; the retry then removes the queue item.")

    class AcceptedResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _kind, _value, _traceback):
            return False

    class LostResponseOpener:
        def __init__(self):
            self.accepted_bodies = []
            self.keys = []

        def open(self, request, timeout):
            del timeout
            self.accepted_bodies.append(request.data)
            self.keys.append(request.get_header("Idempotency-key"))
            if len(self.accepted_bodies) == 1:
                raise TimeoutError("simulated response lost after server acceptance")
            return AcceptedResponse()

    opener = LostResponseOpener()
    payload = make_payload("lost-http-response", "completion-lost-http-response")
    with mock.patch.object(product, "_webhook_opener", lambda: opener), \
            mock.patch.object(product.time, "sleep", lambda _seconds: None):
        delivered = product.deliver(payload, env)
    if not delivered or len(opener.accepted_bodies) != 2 \
            or opener.accepted_bodies[0] != opener.accepted_bodies[1]:
        raise AssertionError(
            "real deliver retry did not resend the same accepted payload after its first response was lost"
        )
    body = json.loads(opener.accepted_bodies[0])
    if not body.get("event_id") or opener.keys != [body["event_id"]] * 2:
        raise InvariantFailure("event identity invariant failed: body/header must match on every retry")
    print("2. PASS stable event_id and Idempotency-Key across real deliver lost-response retries; receiver deduplication yields one effect.")


def parse_args(argv=None):
    """Parse the standalone replay runner options."""
    default_product = Path(__file__).resolve().parents[1] / "t3_done_notify.py"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--product",
        type=Path,
        default=default_product,
        help="product module copy to replay (default: repository t3_done_notify.py)",
    )
    parser.add_argument(
        "--scenario",
        choices=("index-loss", "durable-outbox", "delivery-duplicate", "all"),
        default="all",
        help="replay one crash behavior or all of them",
    )
    parser.add_argument(
        "--expect-seed",
        action="store_true",
        help="expect the seeded _outbox append mutation; the durable invariant must fail",
    )
    return parser.parse_args(argv)


def main(argv=None):
    """Run actual-product crash/restart scenarios and return a check status."""
    args = parse_args(argv)
    if args.expect_seed and args.scenario not in {"all", "durable-outbox"}:
        print("FAIL: --expect-seed applies only to the durable-outbox scenario", file=sys.stderr)
        return 2
    product = load_product(args.product.resolve())
    scenario = "durable-outbox" if args.expect_seed else args.scenario
    try:
        if scenario in {"all", "index-loss"}:
            run_index_loss(product)
        if scenario in {"all", "durable-outbox"}:
            run_durable_outbox(product)
        if scenario in {"all", "delivery-duplicate"}:
            run_delivery_duplicate(product)
    except InvariantFailure as exc:
        label = "FAIL (seeded mutation detected)" if args.expect_seed else "FAIL"
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    except AssertionError as exc:
        label = "FAIL (seeded mutation was not detected correctly)" if args.expect_seed else "FAIL"
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    if args.expect_seed:
        print(
            "FAIL: --expect-seed was set, but the durable outbox invariant still passed; "
            "the seeded candidate['_outbox'].append(queued) mutation is absent.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
