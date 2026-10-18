"""Crash, retry identity, and persistent rename regressions for F1-F3."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import t3_done_notify as notify
from test_local_sessions import claude_prompt, claude_assistant, isolated_config, write_jsonl


class ProcessCrash(BaseException):
    pass


class FindingRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = isolated_config(str(self.root), {
            "T3_DONE_SOURCES": "claude",
            "CLAUDE_PROJECTS_DIR": str(self.root / "claude"),
            "T3_DONE_DEBOUNCE_SECONDS": "0",
            "WEBHOOK_URL": "http://localhost/hook",
            "WEBHOOK_KEY": "test-key",
        })

    def transcript(self, session_id, turn="first"):
        path = Path(self.config.claude_projects_dir) / "project" / (session_id + ".jsonl")
        prompt = claude_prompt(session_id, "2026-10-18T10:00:00Z", "Review")
        prompt["uuid"] = turn
        write_jsonl(str(path), [prompt, claude_assistant(
            session_id, "2026-10-18T10:00:01Z", "Could you approve?", "end_turn",
        )])
        return path

    def poll(self, state, now):
        notify.poll_local_sessions(state, now=now, config=self.config)

    def test_f1_crash_after_a_commit_leaves_b_discoverable(self):
        # Cover B absent AND a new turn ahead of B's existing committed record.
        for existing_b in (False, True):
            with self.subTest(existing_b=existing_b):
                state = {"_files": {}}
                if existing_b:
                    self.transcript("B", "old")
                    with mock.patch.object(notify, "deliver", return_value=True):
                        self.poll(state, 0)
                        self.poll(state, 1)
                self.transcript("A")
                self.transcript("B", "new")
                notify.save_state(state, self.config.state_file)
                previous_index = copy.deepcopy(state["_files"])
                discover, save = notify._discover_observations, notify.save_state

                def ordered(config):
                    observations, loose, unstable = discover(config)
                    return sorted(observations, key=lambda item: item["thread"]["id"]), loose, unstable

                def save_then_crash(candidate, path=None):
                    save(candidate, path)
                    raise ProcessCrash()

                with mock.patch.object(notify, "_discover_observations", ordered), \
                        mock.patch.object(notify, "save_state", save_then_crash):
                    with self.assertRaises(ProcessCrash):
                        self.poll(state, 10)
                state = notify.load_state(self.config.state_file)
                b_path = str(Path(self.config.claude_projects_dir) / "project" / "B.jsonl")
                self.assertTrue(
                    state["_files"].get(b_path) == previous_index.get(b_path),
                    "B index must stay at its previously committed observation",
                )
                deliveries = []
                with mock.patch.object(notify, "deliver", side_effect=lambda p, e: deliveries.append(p) or True):
                    for now in (11, 12, 13):
                        self.poll(state, now)
                self.assertEqual(sorted(p["thread_id"] for p in deliveries), ["A", "B"])
                for path in Path(self.config.claude_projects_dir).rglob("*.jsonl"):
                    path.unlink()

    def test_f2_lost_response_and_ack_crash_preserve_event_identity(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        for source in ("t3", "claude", "codex"):
            with self.subTest(source=source):
                thread = {
                    "id": "session", "source": source, "status": "completed",
                    "updatedAt": "2026-10-18T10:00:00Z", "turn_key": "turn-1",
                }
                payload = notify.event(thread, "done", "Could you approve?")
                self.assertTrue(payload.get("event_id"), "every generated payload needs an event_id")
                self.assertEqual(payload, notify.event(thread, "done", "Could you approve?"))
                newer = dict(thread, turn_key="turn-2", updatedAt="2026-10-18T10:01:00Z")
                self.assertNotEqual(payload["event_id"], notify.event(newer, "done", "Could you approve?")["event_id"])
                self.assertNotEqual(payload["event_id"], notify.event(thread, "stalled", "Could you approve?")["event_id"])
                notify.save_state({"_outbox": [{"payload": payload, "queued_at": 0}]}, self.config.state_file)
                state = notify.load_state(self.config.state_file)
                requests, effects = [], set()

                def accept(request, timeout):
                    body = json.loads(request.data)
                    header = request.get_header("Idempotency-key")
                    self.assertEqual(header, body["event_id"])
                    requests.append(body)
                    effects.add(header)  # Receiver's durable dedupe key.
                    if len(requests) == 1:
                        raise TimeoutError("accepted, response lost")
                    return Response()

                with mock.patch.object(notify, "_webhook_opener") as opener, \
                        mock.patch.object(notify.time, "sleep"), \
                        mock.patch.object(notify, "save_state", side_effect=ProcessCrash):
                    opener.return_value.open.side_effect = accept
                    with self.assertRaises(ProcessCrash):
                        notify.flush_outbox(state, self.config.environment, save_path=self.config.state_file)
                state = notify.load_state(self.config.state_file)
                with mock.patch.object(notify, "_webhook_opener") as opener:
                    opener.return_value.open.side_effect = accept
                    notify.flush_outbox(state, self.config.environment, save_path=self.config.state_file)
                self.assertEqual(len(requests), 3)
                self.assertEqual(requests, [payload] * 3)
                self.assertEqual(effects, {payload["event_id"]})
                self.assertEqual(notify.load_state(self.config.state_file)["_outbox"], [])

    def test_f3_rename_after_2048_other_completions_does_not_repeat(self):
        state = {"_files": {}}
        path = self.transcript("A")
        with mock.patch.object(notify, "deliver", return_value=True) as deliver:
            self.poll(state, 0)
            self.poll(state, 1)
            self.assertEqual(deliver.call_count, 1)
            original = copy.deepcopy(state["A"])
            # Valid delivered records for vanished transcripts, beyond the old cap.
            for number in range(2048):
                session_id = f"vanished-{number:04d}"
                key = notify._content_key(["claude", session_id, "first"])
                state[session_id] = dict(original, completed_turn=key, transcript_paths=[
                    str(self.root / "vanished" / (session_id + ".jsonl")),
                ])
                notify._remember_local_completion(state, key)
            notify.save_state(state, self.config.state_file)
            path.rename(path.with_name("renamed.jsonl"))
            state = notify.load_state(self.config.state_file)
            for now in (2, 3, 4):
                self.poll(state, now)
            self.assertEqual(deliver.call_count, 1, "renaming unchanged content must not notify again")
            self.assertIn(original["completed_turn"], state["_local_completed"])
            self.assertEqual(len(state["_local_completed"]), 2049)


if __name__ == "__main__":
    unittest.main()
