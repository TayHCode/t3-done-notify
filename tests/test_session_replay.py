"""Regression coverage for local transcript replay and state pruning."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import t3_done_notify as notify
from test_local_sessions import (
    append_jsonl,
    claude_assistant,
    claude_prompt,
    codex_line,
    codex_meta,
    codex_user,
    isolated_config,
    write_jsonl,
)


SESSION_IDS = {
    "claude": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01",
    "codex": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
}
T3_THREAD_ID = "thread:project:example:keep-this-thread"
START = "2026-10-15T12:00:00Z"
START_EPOCH = notify.parse_time(START)


def _stamp(seconds):
    return notify.dt.datetime.fromtimestamp(
        START_EPOCH + seconds, tz=notify.dt.timezone.utc
    ).isoformat().replace("+00:00", "Z")


def _rows(source, session_id, phase, turn=1):
    if source == "claude":
        prompt_time = _stamp(1 + (turn - 1) * 60)
        assistant_time = _stamp(2 + (turn - 1) * 60)
        if phase == "active":
            return [
                claude_prompt(session_id, prompt_time, f"Turn {turn}"),
                claude_assistant(
                    session_id, assistant_time, f"Working on turn {turn}",
                    "tool_use", tool_use=True,
                ),
            ]
        return [
            claude_prompt(session_id, prompt_time, f"Turn {turn}"),
            claude_assistant(
                session_id, assistant_time, f"Could you approve turn {turn}?", "end_turn"
            ),
        ]

    turn_time = 1 + (turn - 1) * 60
    rows = [
        codex_meta(session_id, _stamp(0)),
        codex_line(_stamp(turn_time), "event_msg", {
            "type": "task_started", "turn_id": f"turn-{turn}",
        }),
        codex_user(_stamp(turn_time + 1), f"Turn {turn}"),
        codex_line(_stamp(turn_time + 2), "response_item", {
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": f"Could you approve turn {turn}?",
            }],
        }),
    ]
    if phase == "complete":
        rows.append(codex_line(_stamp(turn_time + 3), "event_msg", {
            "type": "task_complete",
            "turn_id": f"turn-{turn}",
            "last_agent_message": f"Could you approve turn {turn}?",
        }))
    return rows


def _source_path(root, source, session_id):
    if source == "claude":
        return root / "claude" / "-workspace" / f"{session_id}.jsonl"
    return (
        root / "codex" / "2026" / "10" / "15"
        / f"rollout-2026-10-15T12-00-00-{session_id}.jsonl"
    )


class _SessionReplayTestMixin:
    """Shared replay checks, run once for each transcript source."""

    source = None

    def setUp(self):
        super().setUp()
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.tmp_path = Path(self._temporary_directory.name)

    def _patch(self, target, attribute, replacement):
        patcher = mock.patch.object(target, attribute, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _harness(self, source=None):
        if source is None:
            source = self.source
        session_id = SESSION_IDS[source]
        config = isolated_config(str(self.tmp_path), {
            "T3_DONE_SOURCES": source,
            "CLAUDE_PROJECTS_DIR": str(self.tmp_path / "claude"),
            "CODEX_SESSIONS_DIR": str(self.tmp_path / "codex"),
            "T3_DONE_DEBOUNCE_SECONDS": "30",
            "T3_DONE_STALL_SECONDS": "999999",
            "WEBHOOK_URL": "http://localhost/hook",
            "WEBHOOK_KEY": "local-test-key",
        })
        posts = []

        def deliver(payload, _environment):
            posts.append(dict(payload))
            return True

        self._patch(notify, "deliver", deliver)
        state = {}
        state_path = str(self.tmp_path / "state.json")
        daybook_dir = str(self.tmp_path / "daybook")
        path = _source_path(self.tmp_path, source, session_id)

        def poll(seconds):
            notify.poll_local_sessions(
                state,
                now=START_EPOCH + seconds,
                save_path=state_path,
                daybook_dir=daybook_dir,
                config=config,
            )

        def write(phase, turn=1):
            write_jsonl(str(path), _rows(source, session_id, phase, turn=turn))

        return {
            "config": config,
            "path": path,
            "poll": poll,
            "posts": posts,
            "session_id": session_id,
            "state": state,
            "state_path": state_path,
            "write": write,
        }

    def _notify_first_turn(self, harness):
        harness["write"]("active")
        harness["poll"](3)
        harness["write"]("complete")
        harness["poll"](4)
        harness["poll"](34)
        self.assertEqual(
            [post["thread_id"] for post in harness["posts"]],
            [harness["session_id"]],
        )

    def test_touch_does_not_replay_a_notified_turn(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            self._notify_first_turn(harness)
            path = harness["path"]
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 20_000_000_000))

            harness["poll"](35)
            harness["poll"](65)

            self.assertEqual(
                [post["thread_id"] for post in harness["posts"]],
                [harness["session_id"]],
            )

    def _check_same_content_file_rotation_does_not_replay(self, rotation):
        with self.subTest(source=self.source, rotation=rotation):
            harness = self._harness()
            self._notify_first_turn(harness)
            path = harness["path"]
            content = path.read_bytes()
            before = path.stat()

            if rotation == "replacement":
                replacement = path.with_name(path.name + ".replacement")
                replacement.write_bytes(content)
                os.utime(
                    replacement,
                    ns=(before.st_atime_ns, before.st_mtime_ns + 20_000_000_000),
                )
                os.replace(replacement, path)
                self.assertNotEqual(path.stat().st_ino, before.st_ino)
            elif rotation == "rename":
                path.rename(path.with_name(path.stem + "-moved.jsonl"))
            else:
                with path.open("wb") as handle:
                    handle.write(content)
                current = path.stat()
                os.utime(
                    path,
                    ns=(current.st_atime_ns, before.st_mtime_ns + 20_000_000_000),
                )

            harness["poll"](35)
            harness["poll"](65)

            self.assertEqual(
                [post["thread_id"] for post in harness["posts"]],
                [harness["session_id"]],
            )

    def test_same_content_file_rotation_replacement_does_not_replay(self):
        self._check_same_content_file_rotation_does_not_replay("replacement")

    def test_same_content_file_rotation_truncate_rewrite_does_not_replay(self):
        self._check_same_content_file_rotation_does_not_replay("truncate_rewrite")

    def test_same_content_file_rotation_rename_does_not_replay(self):
        self._check_same_content_file_rotation_does_not_replay("rename")

    def test_observed_truncation_then_restore_does_not_replay(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            self._notify_first_turn(harness)
            path = harness["path"]
            content = path.read_bytes()

            path.write_bytes(b"")
            harness["poll"](35)
            path.write_bytes(b'{"partial":')
            harness["poll"](36)
            harness["write"]("active")
            # Replaying the already completed prompt must not emit a stalled event either.
            harness["poll"](1_000_100)
            path.write_bytes(content)
            harness["poll"](1_000_160)
            harness["poll"](1_000_220)

            self.assertEqual(
                [post["thread_id"] for post in harness["posts"]],
                [harness["session_id"]],
            )

    def test_appended_second_turn_posts_once(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            self._notify_first_turn(harness)

            append_jsonl(
                str(harness["path"]),
                _rows(self.source, harness["session_id"], "complete", turn=2),
            )
            harness["poll"](35)
            harness["poll"](65)
            harness["poll"](66)

            self.assertEqual(
                [post["thread_id"] for post in harness["posts"]],
                [harness["session_id"], harness["session_id"]],
            )
            self.assertEqual(
                harness["posts"][-1]["last_message"], "Could you approve turn 2?"
            )

    def test_same_size_new_turn_content_posts_once(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            self._notify_first_turn(harness)
            size = harness["path"].stat().st_size
            harness["write"]("complete", turn=2)
            self.assertEqual(harness["path"].stat().st_size, size)
            harness["poll"](60)
            harness["poll"](120)
            harness["poll"](180)
            self.assertEqual(len(harness["posts"]), 2)
            self.assertEqual(
                harness["posts"][-1]["last_message"], "Could you approve turn 2?"
            )

    def _check_appended_non_turn_content_does_not_replay(self, addition):
        with self.subTest(source=self.source, addition=addition):
            harness = self._harness()
            self._notify_first_turn(harness)
            path = harness["path"]

            if addition == "metadata":
                if self.source == "claude":
                    append_jsonl(str(path), [
                        {
                            "type": "custom-title",
                            "sessionId": harness["session_id"],
                            "timestamp": _stamp(10),
                            "customTitle": "Updated title",
                        },
                    ])
                else:
                    append_jsonl(str(path), [
                        codex_line(_stamp(10), "other_event", {"type": "metadata"}),
                    ])
            else:
                with path.open("ab") as handle:
                    handle.write(b"not a transcript record\n")

            harness["poll"](35)
            harness["poll"](65)

            self.assertEqual(
                [post["thread_id"] for post in harness["posts"]],
                [harness["session_id"]],
            )

    def test_appended_metadata_does_not_replay(self):
        self._check_appended_non_turn_content_does_not_replay("metadata")

    def test_appended_invalid_line_does_not_replay(self):
        self._check_appended_non_turn_content_does_not_replay("invalid_line")

    def test_short_session_between_polls_posts_once(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            harness["poll"](0)
            harness["write"]("complete")

            # A 60-second poll can first observe the transcript after it has completed.
            harness["poll"](60)
            harness["poll"](90)
            harness["poll"](120)

            self.assertEqual(
                [post["thread_id"] for post in harness["posts"]],
                [harness["session_id"]],
            )

    def test_missing_transcript_state_is_pruned_on_poll_and_t3_state_survives(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            harness["write"]("active")
            harness["poll"](3)
            self.assertIn(harness["session_id"], harness["state"])
            harness["state"][T3_THREAD_ID] = {"active": True}

            harness["path"].unlink()
            harness["poll"](4)

            self.assertNotIn(harness["session_id"], harness["state"])
            self.assertEqual(harness["state"][T3_THREAD_ID], {"active": True})

    def test_load_prunes_missing_transcript_record_and_keeps_t3_state(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            harness["write"]("active")
            harness["poll"](3)
            harness["state"][T3_THREAD_ID] = {"active": True}
            notify.save_state(harness["state"], harness["state_path"])
            harness["path"].unlink()
            self._patch(notify, "load_config", lambda: harness["config"])

            loaded = notify.load_state(harness["state_path"])

            self.assertNotIn(harness["session_id"], loaded)
            self.assertEqual(loaded[T3_THREAD_ID], {"active": True})

    def test_pruned_session_reappearing_same_content_does_not_replay_and_new_turn_does(self):
        with self.subTest(source=self.source):
            harness = self._harness()
            self._notify_first_turn(harness)
            path = harness["path"]
            content = path.read_bytes()

            path.unlink()
            harness["poll"](35)
            self.assertNotIn(harness["session_id"], harness["state"])

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            harness["poll"](36)
            harness["poll"](66)
            self.assertEqual(len(harness["posts"]), 1)

            append_jsonl(
                str(path),
                _rows(self.source, harness["session_id"], "complete", turn=2),
            )
            harness["poll"](67)
            harness["poll"](97)
            harness["poll"](98)

            self.assertEqual(
                [post["thread_id"] for post in harness["posts"]],
                [harness["session_id"], harness["session_id"]],
            )
            self.assertEqual(
                harness["posts"][-1]["last_message"], "Could you approve turn 2?"
            )


class ClaudeSessionReplayTests(_SessionReplayTestMixin, unittest.TestCase):
    source = "claude"


class CodexSessionReplayTests(_SessionReplayTestMixin, unittest.TestCase):
    source = "codex"

    def _check_prompt_before_turn_start_is_not_a_completion(self, kind):
        with self.subTest(kind=kind):
            harness = self._harness()
            self._notify_first_turn(harness)
            if kind == "response_item":
                prompt = codex_user(_stamp(60), "Review again")
            else:
                prompt = codex_line(_stamp(60), "event_msg", {
                    "type": "user_message", "message": "Review again",
                })
            append_jsonl(str(harness["path"]), [prompt])
            harness["poll"](60)
            harness["poll"](120)
            self.assertEqual(len(harness["posts"]), 1)

            append_jsonl(str(harness["path"]), [
                codex_line(_stamp(121), "event_msg", {"type": "task_started", "turn_id": "turn-2"}),
                codex_line(_stamp(122), "event_msg", {
                    "type": "task_complete", "turn_id": "turn-2",
                    "last_agent_message": "Could you approve turn 2?",
                }),
            ])
            harness["poll"](180)
            harness["poll"](240)
            harness["poll"](300)
            self.assertEqual(len(harness["posts"]), 2)
            self.assertEqual(
                harness["posts"][-1]["last_message"], "Could you approve turn 2?"
            )

    def test_codex_prompt_before_turn_start_is_not_a_completion_response_item(self):
        self._check_prompt_before_turn_start_is_not_a_completion("response_item")

    def test_codex_prompt_before_turn_start_is_not_a_completion_event_msg(self):
        self._check_prompt_before_turn_start_is_not_a_completion("event_msg")
