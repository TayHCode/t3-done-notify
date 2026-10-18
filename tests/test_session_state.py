import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import t3_done_notify as notify
from test_local_sessions import (
    append_jsonl, claude_assistant, claude_prompt, isolated_config, write_jsonl,
)


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp_path = Path(directory.name)

    def test_legacy_missing_sessions_pruned_on_load_without_pruning_t3(self):
        missing = self.tmp_path / "missing.jsonl"
        existing = self.tmp_path / "existing.jsonl"
        existing.write_text("", encoding="utf-8")
        state_path = self.tmp_path / "state.json"
        t3_record = {"active": True, "last_fired": "done:old"}
        local_record = {"active": False, "last_fired": "done:2026-10-15T12:00:20Z"}
        state = {
            "thread:project:example:t3": t3_record,
            "local-missing": local_record,
            "local-retained": local_record,
            "_files": {
                str(missing): {"id": "local-missing", "offset": 1, "mtime_ns": 1},
                str(existing): {"id": "local-retained", "offset": 0, "mtime_ns": 1},
            },
        }
        notify.save_state(state, str(state_path))
        restored = notify.load_state(str(state_path))
        self.assertNotIn("local-missing", restored)
        self.assertNotIn(str(missing), restored["_files"])
        self.assertEqual(restored["local-retained"], local_record)
        self.assertEqual(restored["thread:project:example:t3"], t3_record)

    def test_pruned_legacy_completion_is_not_replayed_when_file_returns(self):
        root = self.tmp_path / "claude"
        path = root / "project" / "example.jsonl"
        config = isolated_config(str(self.tmp_path), {
            "T3_DONE_SOURCES": "claude", "CLAUDE_PROJECTS_DIR": str(root),
            "T3_DONE_DEBOUNCE_SECONDS": "0",
            "WEBHOOK_URL": "http://localhost/hook", "WEBHOOK_KEY": "test-key",
        })
        notify.save_state({
            "example": {"active": False, "last_fired": "done:2026-10-15T12:00:20Z"},
            "_files": {str(path): {"id": "example", "offset": 100, "mtime_ns": 1}},
        }, config.state_file)
        state = notify.load_state(config.state_file)
        self.assertNotIn("example", state)
        write_jsonl(str(path), [
            claude_prompt("example", "2026-10-15T12:00:10Z", "Review"),
            claude_assistant("example", "2026-10-15T12:00:20Z", "Could you approve?", "end_turn"),
        ])
        with mock.patch.object(notify, "deliver", return_value=True) as deliver:
            for now in (60, 120, 180):
                notify.poll_local_sessions(state, now=now, config=config)
            deliver.assert_not_called()

    def test_pruned_completion_history_is_bounded(self):
        root = self.tmp_path / "claude"
        path = root / "project" / "example.jsonl"
        config = isolated_config(str(self.tmp_path), {
            "T3_DONE_SOURCES": "claude", "CLAUDE_PROJECTS_DIR": str(root),
            "T3_DONE_DEBOUNCE_SECONDS": "0",
            "WEBHOOK_URL": "http://localhost/hook", "WEBHOOK_KEY": "test-key",
        })
        state = {}
        with mock.patch.object(notify, "RECENT_LOCAL_COMPLETIONS", 3), \
                mock.patch.object(notify, "deliver", return_value=True) as deliver:
            notify.poll_local_sessions(state, now=0, config=config)
            for turn in range(6):
                session_id = f"session-{turn}"
                write_jsonl(str(path), [
                    claude_prompt(session_id, "2026-10-15T12:00:10Z", "Review"),
                    claude_assistant(session_id, "2026-10-15T12:00:20Z", "Could you approve?", "end_turn"),
                ])
                notify.poll_local_sessions(state, now=1 + turn * 3, config=config)
                notify.poll_local_sessions(state, now=2 + turn * 3, config=config)
                path.unlink()
                notify.poll_local_sessions(state, now=3 + turn * 3, config=config)
                self.assertNotIn(session_id, state)
            self.assertEqual(deliver.call_count, 6)
        self.assertEqual(state["_files"], {})
        self.assertEqual(len(state["_local_completed"]), 3)
        self.assertTrue(all(key.startswith("_") for key in state))

    def test_pending_delivery_survives_pruning_without_duplicate_queue(self):
        root = self.tmp_path / "claude"
        path = root / "project" / "example.jsonl"
        config = isolated_config(str(self.tmp_path), {
            "T3_DONE_SOURCES": "claude", "CLAUDE_PROJECTS_DIR": str(root),
            "T3_DONE_DEBOUNCE_SECONDS": "30",
            "WEBHOOK_URL": "http://localhost/hook", "WEBHOOK_KEY": "test-key",
        })
        state = {}

        def poll(at):
            notify.poll_local_sessions(state, now=at, config=config)

        with mock.patch.object(notify, "deliver", return_value=False):
            poll(0)
            rows = [
                claude_prompt("example", "2026-10-15T12:00:10Z", "Review"),
                claude_assistant("example", "2026-10-15T12:00:20Z", "Could you approve?", "end_turn"),
            ]
            write_jsonl(str(path), rows)
            poll(60)
            poll(120)
            self.assertEqual(len(state["_outbox"]), 1)
            path.unlink()
            poll(180)
            self.assertNotIn("example", state)
            state = notify.load_state(config.state_file)
            write_jsonl(str(path), rows)
            poll(240)
            poll(300)
            self.assertEqual(len(state["_outbox"]), 1)
        with mock.patch.object(notify, "deliver", return_value=True) as deliver:
            poll(360)
            poll(420)
            self.assertEqual(deliver.call_count, 1)
        self.assertEqual(state["_outbox"], [])

    def test_new_turn_with_same_timestamps_is_not_suppressed(self):
        root = self.tmp_path / "claude"
        path = root / "project" / "example.jsonl"
        config = isolated_config(str(self.tmp_path), {
            "T3_DONE_SOURCES": "claude", "CLAUDE_PROJECTS_DIR": str(root),
            "T3_DONE_DEBOUNCE_SECONDS": "0",
            "WEBHOOK_URL": "http://localhost/hook", "WEBHOOK_KEY": "test-key",
        })
        state = {}
        with mock.patch.object(notify, "deliver", return_value=True) as deliver:
            notify.poll_local_sessions(state, now=0, config=config)
            for turn in range(2):
                prompt = claude_prompt("example", "2026-10-15T12:00:10Z", "Review")
                prompt["uuid"] = f"prompt-{turn}"
                rows = [prompt, claude_assistant(
                    "example", "2026-10-15T12:00:20Z", "Could you approve?", "end_turn",
                )]
                if turn:
                    append_jsonl(str(path), rows)
                else:
                    write_jsonl(str(path), rows)
                notify.poll_local_sessions(state, now=60 + turn * 120, config=config)
                notify.poll_local_sessions(state, now=120 + turn * 120, config=config)
                self.assertEqual(deliver.call_count, turn + 1)

    def test_default_t3_only_does_not_scan_local_sessions(self):
        config = isolated_config(str(self.tmp_path))
        self.assertEqual(config.sources, ("t3",))
        with mock.patch.object(notify, "load_config", return_value=config), \
                mock.patch.object(notify, "poll_once", return_value={}) as poll_t3, \
                mock.patch.object(notify, "poll_local_sessions", side_effect=AssertionError("local poll")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notify.main(["--once"]), 0)
        poll_t3.assert_called_once()
        self.assertNotIn("_files", poll_t3.call_args.args[0])
