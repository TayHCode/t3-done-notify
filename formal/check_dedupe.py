#!/usr/bin/env python3
"""Exercise local-session completion dedupe in the selected product source.

Examples:
  python3 formal/check_dedupe.py --product t3_done_notify.py --scenario ordinary
  python3 formal/check_dedupe.py --product /tmp/seed/t3_done_notify.py --scenario ordinary
  python3 formal/check_dedupe.py --product t3_done_notify.py --scenario all

The harness uses only temporary files and replaces deliver() with an in-memory
success response. It mirrors poll_local_sessions(), _track_files(),
_prune_local_sessions(), _remember_local_completion(), and
_commit_observation() in t3_done_notify.py. It performs no network calls.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


def load_product(path: Path):
    spec = importlib.util.spec_from_file_location("t3_done_notify_checked", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import product source: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def claude_rows(session_id: str):
    start = "2026-10-18T10:00:00Z"
    end = "2026-10-18T10:00:01Z"
    return [
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": start,
            "uuid": "prompt-" + session_id,
            "message": {"role": "user", "content": "Review this change"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": end,
            "uuid": "assistant-" + session_id,
            "message": {
                "role": "assistant",
                "id": "message-" + session_id,
                "model": "formal-test",
                "content": [{"type": "text", "text": "Could you approve this?"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
            },
        },
    ]


class Harness:
    def __init__(self, notify, directory: Path):
        self.notify = notify
        self.root = directory / "claude"
        self.project = self.root / "-workspace"
        self.project.mkdir(parents=True)
        self.save_path = directory / "state.json"
        self.daybook = directory / "daybook"
        self.config = notify.load_config({
            "T3_DONE_CONFIG_DIR": str(directory / "config"),
            "XDG_STATE_HOME": str(directory / "xdg-state"),
            "T3_DONE_SOURCES": "claude",
            "CLAUDE_PROJECTS_DIR": str(self.root),
            "T3_DONE_DEBOUNCE_SECONDS": "0",
            "T3_DONE_STALL_SECONDS": "999999",
            "WEBHOOK_URL": "http://localhost/hook",
            "WEBHOOK_KEY": "formal-test-key",
        })
        self.state = {}
        self.deliveries = []
        self._original_deliver = notify.deliver
        notify.deliver = self._deliver

    def _deliver(self, payload, _environment):
        self.deliveries.append(payload["thread_id"])
        return True

    def path(self, session_id: str) -> Path:
        return self.project / f"{session_id}.jsonl"

    def write_completed(self, session_id: str, destination: Path | None = None):
        path = destination or self.path(session_id)
        with path.open("w", encoding="utf-8") as stream:
            for row in claude_rows(session_id):
                stream.write(json.dumps(row) + "\n")
        return path

    def poll(self, now: int):
        self.notify.poll_local_sessions(
            self.state,
            now=now,
            save_path=str(self.save_path),
            daybook_dir=str(self.daybook),
            config=self.config,
        )

    def seed_empty_index(self):
        self.poll(0)

    def complete(self, session_id: str, now: int):
        self.write_completed(session_id)
        self.poll(now)
        self.poll(now + 1)

    def close(self):
        self.notify.deliver = self._original_deliver


def duplicate_counts(deliveries):
    counts = {}
    for session_id in deliveries:
        counts[session_id] = counts.get(session_id, 0) + 1
    return {session_id: count - 1 for session_id, count in counts.items() if count > 1}


def fail_duplicate(deliveries, where):
    duplicates = duplicate_counts(deliveries)
    if duplicates:
        details = ", ".join(f"{key} (+{extra})" for key, extra in sorted(duplicates.items()))
        print(f"FAIL duplicate completion enqueue [{where}]: {details}", file=sys.stderr)
        return False
    return True


def ordinary_scenario(notify, directory: Path):
    """Retain A's identity while A stays live; touch/rewrite/rotate unchanged bytes."""
    harness = Harness(notify, directory)
    try:
        harness.seed_empty_index()
        a_path = harness.path("session-A")
        harness.complete("session-A", 1)
        harness.complete("session-B", 3)
        if "session-A" not in harness.state:
            raise AssertionError("live A record unexpectedly pruned before file removal")

        before = a_path.stat()
        os.utime(a_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        harness.poll(5)

        content = a_path.read_bytes()
        with a_path.open("wb") as stream:
            stream.write(content)
        harness.poll(6)

        replacement = a_path.with_suffix(".replacement")
        replacement.write_bytes(content)
        os.replace(replacement, a_path)
        harness.poll(7)

        # Polling both files can re-remember them in filesystem iteration order.
        # A B-only replay is a real call path through poll_local_sessions(sessions=...)
        # and exercises dedupe through the explicit sessions call path.
        b_thread = notify.parse_claude_session(str(harness.path("session-B")))
        notify.poll_local_sessions(
            harness.state,
            now=7.5,
            sessions=[b_thread],
            save_path=str(harness.save_path),
            daybook_dir=str(harness.daybook),
            config=harness.config,
        )
        a_key = harness.state["session-A"].get("completed_turn")

        moved = a_path.with_name("session-A-moved.jsonl")
        a_path.rename(moved)
        harness.poll(8)
        harness.poll(9)

        if not fail_duplicate(harness.deliveries, "ordinary same-content replay"):
            return False
        counts = {sid: harness.deliveries.count(sid) for sid in ("session-A", "session-B")}
        if counts != {"session-A": 1, "session-B": 1}:
            print(f"FAIL unexpected ordinary delivery counts: {counts}", file=sys.stderr)
            return False
        print("PASS ordinary touch/rewrite/replacement/rename with persistent completion identities: A and B each delivered once")
        return True
    finally:
        harness.close()


def capacity_one_rotation_finding(notify, directory: Path):
    """Rotate two live completed files together, retaining both identities."""
    harness = Harness(notify, directory)
    try:
        harness.seed_empty_index()
        harness.complete("session-A", 1)
        harness.complete("session-B", 3)

        old_paths = {sid: harness.path(sid) for sid in ("session-A", "session-B")}
        for sid, old in old_paths.items():
            moved = old.with_name(sid + "-moved.jsonl")
            old.rename(moved)

        harness.poll(5)
        harness.poll(6)
        if not fail_duplicate(harness.deliveries, "simultaneous unchanged rotations"):
            return False
        if sorted(harness.deliveries) != ["session-A", "session-B"]:
            raise AssertionError(f"unexpected deliveries: {harness.deliveries}")
        print("PASS simultaneous same-content rotations: both sessions delivered once")
        return True
    finally:
        harness.close()


def production_capacity_rotation_finding(notify, directory: Path):
    """Retain A among 2048 vanished-session records, beyond the former cap."""
    limit = 2048  # Regression boundary of the removed production cap, not a new bound.
    harness = Harness(notify, directory)
    try:
        harness.seed_empty_index()
        a_path = harness.path("session-A")
        harness.complete("session-A", 1)
        a_record = copy.deepcopy(harness.state["session-A"])
        a_index = copy.deepcopy(harness.state["_files"][str(a_path)])
        a_key = a_record["completed_turn"]
        if a_key != notify._content_key(["claude", "session-A", "prompt-session-A"]):
            raise AssertionError("A completion key does not match _session_thread namespace")

        files = dict(harness.state.get("_files") or {})
        for number in range(limit):
            session_id = f"vanished-{number:04d}"
            path = harness.root / "-vanished" / f"{session_id}.jsonl"
            rows = claude_rows(session_id)
            encoded = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
            key = notify._content_key(["claude", session_id, "prompt-" + session_id])
            record = copy.deepcopy(a_record)
            record["completed_turn"] = key
            record["transcript_paths"] = [str(path)]
            record["last_fired"] = "done:" + rows[-1]["timestamp"]
            harness.state[session_id] = record
            entry = copy.deepcopy(a_index)
            entry.update(
                mtime_ns=a_index["mtime_ns"] + number,
                offset=len(encoded),
                id=session_id,
                fingerprint=hashlib.sha256(
                    encoded[-notify.SESSION_TAIL_BYTES:]
                ).hexdigest(),
                completion_key=key,
            )
            files[str(path)] = entry
            notify._remember_local_completion(harness.state, key)
        harness.state["_files"] = files
        notify.save_state(harness.state, str(harness.save_path))

        old = a_path
        moved = old.with_name("session-A-moved.jsonl")
        old.rename(moved)
        harness.poll(3)
        harness.poll(4)

        if harness.deliveries != ["session-A"]:
            print(f"FAIL duplicate completion enqueue after 2048 pruned records: {harness.deliveries}", file=sys.stderr)
            return False
        print("PASS unchanged rename after 2048 vanished completed records: A delivered once")
        return True
    finally:
        harness.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_product = Path(__file__).resolve().parents[1] / "t3_done_notify.py"
    parser.add_argument(
        "--product", type=Path, default=default_product,
        help=f"product Python source to exercise (default: {default_product})",
    )
    parser.add_argument("--scenario", choices=("ordinary", "rotation", "all"), default="all")
    args = parser.parse_args(argv)

    product = args.product.resolve()
    if not product.is_file():
        parser.error(f"product source does not exist: {product}")
    notify = load_product(product)
    ok = True
    with tempfile.TemporaryDirectory(prefix="t3-done-dedupe-") as temporary:
        root = Path(temporary)
        if args.scenario in {"ordinary", "all"}:
            ok = ordinary_scenario(notify, root / "ordinary") and ok
        if args.scenario in {"rotation", "all"}:
            ok = capacity_one_rotation_finding(notify, root / "rotation") and ok
            if args.scenario == "all":
                ok = production_capacity_rotation_finding(notify, root / "production") and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
