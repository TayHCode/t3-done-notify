import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import t3_done_notify as notify


def write_jsonl(path, rows, torn=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        if torn:
            handle.write('{"type":"assistant","message":\n')


def append_jsonl(path, rows):
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def claude_row(kind, session_id, timestamp, **extra):
    row = {"type": kind, "sessionId": session_id, "timestamp": timestamp}
    row.update(extra)
    return row


def claude_prompt(session_id, timestamp, text):
    return claude_row(
        "user",
        session_id,
        timestamp,
        message={"role": "user", "content": text},
        uuid="user-" + timestamp,
    )


def claude_assistant(session_id, timestamp, text, stop_reason, tool_use=False, **extra):
    content = [{"type": "text", "text": text}]
    if tool_use:
        content.append({
            "type": "tool_use",
            "id": "toolu_1",
            "name": "Bash",
            "input": {"command": "true"},
        })
    row = claude_row(
        "assistant",
        session_id,
        timestamp,
        uuid="assistant-" + timestamp,
        message={
            "role": "assistant",
            "id": "msg-" + timestamp,
            "model": "claude-test",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
        },
    )
    row.update(extra)
    return row


def codex_line(timestamp, kind, payload):
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def codex_meta(session_id, timestamp, parent=None):
    payload = {
        "id": session_id,
        "session_id": parent or session_id,
        "cwd": "/tmp/example",
        "cli_version": "0.99.0",
        "originator": "codex-cli",
        "source": "cli",
    }
    if parent:
        payload["parent_thread_id"] = parent
    return codex_line(timestamp, "session_meta", payload)


def codex_user(timestamp, text):
    return codex_line(timestamp, "response_item", {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    })


def codex_assistant(timestamp, text):
    return codex_line(timestamp, "response_item", {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    })


def isolated_config(directory, extra=None):
    environment = {
        "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
        "XDG_STATE_HOME": os.path.join(directory, "state"),
    }
    environment.update(extra or {})
    return notify.load_config(environment)


class SourceConfigTests(unittest.TestCase):
    def test_sources_and_session_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            config = notify.load_config({
                "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
                "T3_DONE_SOURCES": "codex, claude, codex",
                "CLAUDE_CONFIG_DIR": "/tmp/claude-home",
                "CLAUDE_PROJECTS_DIR": "/tmp/claude-projects",
                "CODEX_HOME": "/tmp/codex-home",
            })
            self.assertEqual(config.sources, ("codex", "claude"))
            self.assertEqual(config.claude_projects_dir, "/tmp/claude-projects")
            self.assertEqual(config.codex_sessions_dir, "/tmp/codex-home/sessions")
            default = notify.load_config({
                "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
                "CLAUDE_CONFIG_DIR": "/tmp/claude-home",
                "CODEX_HOME": "/tmp/codex-home",
            })
            self.assertEqual(default.sources, ("t3",))
            self.assertEqual(default.claude_projects_dir, "/tmp/claude-home/projects")
            self.assertEqual(default.codex_sessions_dir, "/tmp/codex-home/sessions")
            home_default = notify.load_config({
                "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
            })
            self.assertTrue(home_default.claude_projects_dir.endswith(
                os.path.join(".claude", "projects")
            ))
            self.assertTrue(home_default.codex_sessions_dir.endswith(
                os.path.join(".codex", "sessions")
            ))
            with self.assertRaises(ValueError):
                notify.load_config({
                    "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
                    "T3_DONE_SOURCES": "claude,gpt",
                })


class TranscriptTests(unittest.TestCase):
    def test_claude_turn_and_error_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "session.jsonl")
            session_id = "11111111-1111-1111-1111-111111111111"
            write_jsonl(path, [
                claude_row("custom-title", session_id, "2026-10-15T12:00:00Z",
                           customTitle="User title"),
                claude_row("ai-title", session_id, "2026-10-15T12:00:01Z",
                           aiTitle="Generated title"),
                claude_prompt(session_id, "2026-10-15T12:00:02Z", "Please review"),
                claude_assistant(session_id, "2026-10-15T12:00:03Z", "Running",
                                 "tool_use", tool_use=True),
                claude_row("user", session_id, "2026-10-15T12:00:04Z", message={
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "ok"}],
                }),
            ])
            running = notify.parse_claude_session(path)
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["title"], "User title")
            self.assertEqual(running["activeRunId"], "open")

            write_jsonl(path, [
                claude_prompt(session_id, "2026-10-15T12:00:02Z", "Please review"),
                claude_assistant(session_id, "2026-10-15T12:00:05Z", "All done.", "end_turn"),
                claude_row("system", session_id, "2026-10-15T12:00:06Z",
                           subtype="turn_duration", durationMs=1000),
            ], torn=True)
            done = notify.parse_claude_session(path)
            self.assertEqual(done["status"], "completed")
            self.assertIsNone(done["activeRunId"])
            self.assertEqual(done["last_message"], "All done.")

            write_jsonl(path, [
                claude_prompt(session_id, "2026-10-15T12:00:02Z", "Please review"),
                claude_row("system", session_id, "2026-10-15T12:00:07Z",
                           subtype="api_error", level="error",
                           error={"message": "Request timed out."}),
                claude_assistant(session_id, "2026-10-15T12:00:08Z", "Recovered", "end_turn"),
            ])
            self.assertEqual(notify.parse_claude_session(path)["status"], "completed")

            write_jsonl(path, [
                claude_prompt(session_id, "2026-10-15T12:00:02Z", "Please review"),
                claude_row("system", session_id, "2026-10-15T12:00:07Z",
                           subtype="api_error", level="error",
                           error={"message": "Request timed out."}),
            ])
            failed = notify.parse_claude_session(path)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["last_message"], "Request timed out.")

            write_jsonl(path, [
                claude_prompt(session_id, "2026-10-15T12:00:02Z", "Please review"),
                claude_assistant(
                    session_id, "2026-10-15T12:00:09Z", "You've hit your session limit",
                    "stop_sequence", isApiErrorMessage=True,
                ),
            ])
            synthetic = notify.parse_claude_session(path)
            self.assertEqual(synthetic["status"], "failed")
            self.assertEqual(synthetic["source"], "claude")

    def test_codex_lifecycle_and_child_thread_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rollout-2026-10-15T12-00-00-child.jsonl")
            child = "22222222-2222-2222-2222-222222222222"
            parent = "33333333-3333-3333-3333-333333333333"
            write_jsonl(path, [
                codex_meta(child, "2026-10-15T12:00:00Z", parent=parent),
                codex_line("2026-10-15T12:00:01Z", "event_msg", {
                    "type": "task_started", "turn_id": "turn-1",
                }),
                codex_user("2026-10-15T12:00:02Z", "Run the tests"),
                codex_assistant("2026-10-15T12:00:03Z", "Working"),
            ])
            running = notify.parse_codex_session(path)
            self.assertEqual(running["id"], child)
            self.assertNotEqual(running["id"], parent)
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["title"], "Run the tests")

            write_jsonl(path, [
                codex_meta(child, "2026-10-15T12:00:00Z"),
                codex_line("2026-10-15T12:00:04Z", "event_msg", {
                    "type": "turn_aborted", "turn_id": "turn-1", "reason": "interrupted",
                }),
            ])
            self.assertEqual(notify.parse_codex_session(path)["status"], "interrupted")

            write_jsonl(path, [
                codex_meta(child, "2026-10-15T12:00:00Z"),
                codex_line("2026-10-15T12:00:05Z", "event_msg", {
                    "type": "turn_aborted", "turn_id": "turn-1", "reason": "budget_limited",
                }),
            ])
            limited = notify.parse_codex_session(path)
            self.assertEqual(limited["status"], "failed")
            self.assertEqual(limited["source"], "codex")

            write_jsonl(path, [
                codex_meta(child, "2026-10-15T12:00:00Z"),
                codex_assistant("2026-10-15T12:00:06Z", "partial"),
                codex_line("2026-10-15T12:01:00Z", "event_msg", {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "Could you approve the result?",
                    "error": {
                        "message": "usage limit exceeded",
                        "codex_error_info": "usage_limit_exceeded",
                    },
                }),
            ])
            failed = notify.parse_codex_session(path)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["last_message"], "usage limit exceeded")

    def test_discovery_skips_subagents_and_keeps_newest_codex_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            claude_root = os.path.join(directory, "claude")
            project = os.path.join(claude_root, "-tmp-example")
            session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            write_jsonl(
                os.path.join(project, session_id + ".jsonl"),
                [claude_prompt(session_id, "2026-10-15T12:00:00Z", "Main session")],
            )
            write_jsonl(
                os.path.join(project, session_id, "subagents", "agent-child.jsonl"),
                [claude_prompt("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                               "2026-10-15T12:00:00Z", "Subagent")],
            )
            write_jsonl(
                os.path.join(project, "agent-side.jsonl"),
                [claude_prompt("cccccccc-cccc-cccc-cccc-cccccccccccc",
                               "2026-10-15T12:00:00Z", "Side file")],
            )
            found = {row["id"]: row for row in notify.claude_session_threads(claude_root)}
            self.assertEqual(list(found), [session_id])

            codex_root = os.path.join(directory, "codex")
            session = "dddddddd-dddd-dddd-dddd-dddddddddddd"
            older = os.path.join(
                codex_root, "2026", "10", "15",
                "rollout-2026-10-15T12-00-00-" + session + ".jsonl",
            )
            newer = os.path.join(
                codex_root, "2026", "10", "15",
                "rollout-2026-10-15T12-05-00-" + session + ".jsonl",
            )
            write_jsonl(older, [
                codex_meta(session, "2026-10-15T12:00:00Z"),
                codex_line("2026-10-15T12:00:01Z", "event_msg", {
                    "type": "task_started", "turn_id": "turn-1",
                }),
            ])
            write_jsonl(newer, [
                codex_meta(session, "2026-10-15T12:05:00Z"),
                codex_line("2026-10-15T12:05:01Z", "event_msg", {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "Finished",
                }),
            ])
            write_jsonl(
                os.path.join(codex_root, "2026", "10", "14", "notes.jsonl"),
                [codex_meta("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "2026-10-14T12:00:00Z")],
            )
            rows = notify.codex_session_threads(codex_root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], session)
            self.assertEqual(rows[0]["status"], "completed")
            self.assertEqual(rows[0]["last_message"], "Finished")


class WatcherPostTests(unittest.TestCase):
    def test_each_watcher_posts_once_for_sample_sessions(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.server.auth = self.headers.get("Authorization")
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.HTTPServer(("localhost", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                claude_root = os.path.join(directory, "claude")
                codex_root = os.path.join(directory, "codex")
                project = os.path.join(claude_root, "-tmp-example")
                env = {
                    "WEBHOOK_URL": f"http://localhost:{server.server_port}/hook",
                    "WEBHOOK_KEY": "local-test-key",
                }
                config = isolated_config(directory, {
                    "T3_DONE_SOURCES": "t3,claude,codex",
                    "CLAUDE_PROJECTS_DIR": claude_root,
                    "CODEX_SESSIONS_DIR": codex_root,
                    **env,
                })
                state_path = os.path.join(directory, "state.json")
                daybook = os.path.join(directory, "daybook")
                watch = os.path.join(directory, "watch.txt")
                t3_id = "thread:project:example:example-thread"
                with open(watch, "w", encoding="utf-8") as handle:
                    handle.write(t3_id + "\n")

                done_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
                stall_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"
                error_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"
                ancient_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4"
                codex_done = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
                codex_stall = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
                codex_error = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3"
                codex_ancient = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb4"
                stall_at = "2026-10-15T10:00:00Z"
                recent = "2026-10-15T12:00:00Z"
                t0 = notify.parse_time(stall_at) + notify.STALL_SECONDS + 5

                def claude_running(session_id, text):
                    return [
                        claude_row("custom-title", session_id, recent, customTitle="Review the patch"),
                        claude_prompt(session_id, recent, "Please review"),
                        claude_assistant(session_id, recent, text, "tool_use", tool_use=True),
                    ]

                write_jsonl(os.path.join(project, done_id + ".jsonl"), claude_running(done_id, "Working"))
                write_jsonl(os.path.join(project, stall_id + ".jsonl"), [
                    claude_row("ai-title", stall_id, stall_at, aiTitle="Stalled patch"),
                    claude_prompt(stall_id, stall_at, "Check the stall"),
                    claude_assistant(
                        stall_id, stall_at, "Could you check this stall?", "tool_use", tool_use=True
                    ),
                ])
                write_jsonl(os.path.join(project, error_id + ".jsonl"), claude_running(error_id, "Working"))
                write_jsonl(os.path.join(project, ancient_id + ".jsonl"), [
                    claude_prompt(ancient_id, recent, "Old session"),
                    claude_assistant(
                        ancient_id, recent, "FIRST_SEEN_SHOULD_NOT_POST", "end_turn"
                    ),
                ])
                write_jsonl(
                    os.path.join(project, done_id, "subagents", "agent-child.jsonl"),
                    [claude_assistant(
                        "cccccccc-cccc-cccc-cccc-cccccccccccc", recent,
                        "SUBAGENT_SHOULD_NOT_POST", "end_turn",
                    )],
                )

                def codex_running(session_id, timestamp, text):
                    return [
                        codex_meta(session_id, timestamp),
                        codex_user(timestamp, "Run the tests"),
                        codex_line(timestamp, "event_msg", {
                            "type": "task_started", "turn_id": "turn-1",
                        }),
                        codex_assistant(timestamp, text),
                    ]

                codex_done_path = os.path.join(
                    codex_root, "2026", "10", "15",
                    "rollout-2026-10-15T12-00-00-" + codex_done + ".jsonl",
                )
                write_jsonl(codex_done_path, codex_running(codex_done, recent, "Working"))
                write_jsonl(os.path.join(
                    codex_root, "2026", "10", "15",
                    "rollout-2026-10-15T10-00-00-" + codex_stall + ".jsonl",
                ), codex_running(codex_stall, stall_at, "Could you check this stall?"))
                codex_error_path = os.path.join(
                    codex_root, "2026", "10", "15",
                    "rollout-2026-10-15T12-00-00-" + codex_error + ".jsonl",
                )
                write_jsonl(codex_error_path, codex_running(codex_error, recent, "Working"))
                write_jsonl(os.path.join(
                    codex_root, "2026", "10", "14",
                    "rollout-2026-10-14T12-00-00-" + codex_ancient + ".jsonl",
                ), [
                    codex_meta(codex_ancient, recent),
                    codex_line(recent, "event_msg", {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": "FIRST_SEEN_SHOULD_NOT_POST",
                    }),
                ])

                state = {}

                def poll_local(now):
                    notify.poll_local_sessions(
                        state,
                        now=now,
                        env=env,
                        save_path=state_path,
                        daybook_dir=daybook,
                        config=config,
                    )

                def poll_t3(now, status, active):
                    thread = {
                        "id": t3_id,
                        "title": "Example task",
                        "status": status,
                        "updatedAt": recent,
                        "activeRunId": active,
                    }
                    notify.poll_once(
                        state,
                        now=now,
                        env=env,
                        snap={t3_id: thread},
                        message_reader=lambda _: "Could you approve this?",
                        save_path=state_path,
                        watch_path=watch,
                        daybook_dir=daybook,
                        config=config,
                    )

                poll_local(t0)
                poll_t3(t0, "running", "run-1")
                self.assertEqual(
                    sorted((item["source"], item["reason"]) for item in received),
                    [("claude", "stalled"), ("codex", "stalled")],
                )

                write_jsonl(os.path.join(project, done_id + ".jsonl"), [
                    claude_row("custom-title", done_id, recent, customTitle="Review the patch"),
                    claude_prompt(done_id, recent, "Please review"),
                    claude_assistant(
                        done_id, "2026-10-15T12:01:00Z",
                        "Could you approve the result?", "end_turn",
                    ),
                    claude_row("system", done_id, "2026-10-15T12:01:01Z",
                               subtype="turn_duration", durationMs=1000),
                ], torn=True)
                write_jsonl(os.path.join(project, error_id + ".jsonl"), [
                    claude_row("custom-title", error_id, recent, customTitle="Review the patch"),
                    claude_prompt(error_id, recent, "Please review"),
                    claude_assistant(
                        error_id, "2026-10-15T12:02:00Z",
                        "You've hit your session limit", "stop_sequence",
                        isApiErrorMessage=True,
                    ),
                ])
                write_jsonl(os.path.join(
                    codex_root, "2026", "10", "15",
                    "rollout-2026-10-15T12-05-00-" + codex_done + ".jsonl",
                ), [
                    codex_meta(codex_done, "2026-10-15T12:05:00Z"),
                    codex_user("2026-10-15T12:05:00Z", "Run the tests"),
                    codex_line("2026-10-15T12:05:01Z", "event_msg", {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": "Could you approve the result?",
                    }),
                ])
                write_jsonl(codex_error_path, [
                    codex_meta(codex_error, recent),
                    codex_user(recent, "Run the tests"),
                    codex_line("2026-10-15T12:02:00Z", "event_msg", {
                        "type": "task_started", "turn_id": "turn-1",
                    }),
                    codex_line("2026-10-15T12:03:00Z", "event_msg", {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": None,
                        "error": {"message": "usage limit exceeded",
                                  "codex_error_info": "usage_limit_exceeded"},
                    }),
                ])

                poll_local(t0 + 10)
                poll_t3(t0 + 10, "completed", None)
                self.assertEqual(len(received), 2)
                poll_local(t0 + 130)
                poll_t3(t0 + 130, "completed", None)
                poll_local(t0 + 400)
                poll_t3(t0 + 400, "completed", None)

                keys = []
                for item in received:
                    self.assertEqual(set(item), {
                        "thread_id", "title", "status", "reason",
                        "updated_at", "last_message", "source", "event_id",
                    })
                    keys.append((item["source"], item["reason"], item["thread_id"]))
                self.assertEqual(sorted(keys), sorted([
                    ("claude", "stalled", stall_id),
                    ("claude", "done", done_id),
                    ("claude", "error", error_id),
                    ("codex", "stalled", codex_stall),
                    ("codex", "done", codex_done),
                    ("codex", "error", codex_error),
                    ("t3", "done", t3_id),
                ]))
                self.assertEqual(len(keys), len(set(keys)))
                by_id = {item["thread_id"]: item for item in received}
                self.assertEqual(by_id[done_id]["title"], "Review the patch")
                self.assertEqual(by_id[done_id]["last_message"], "Could you approve the result?")
                self.assertEqual(by_id[done_id]["status"], "completed")
                self.assertEqual(by_id[error_id]["last_message"], "You've hit your session limit")
                self.assertEqual(by_id[error_id]["status"], "failed")
                self.assertEqual(by_id[codex_done]["title"], "Run the tests")
                self.assertEqual(by_id[codex_done]["last_message"], "Could you approve the result?")
                self.assertEqual(by_id[codex_error]["last_message"], "usage limit exceeded")
                self.assertEqual(by_id[codex_stall]["status"], "running")
                self.assertEqual(by_id[t3_id]["source"], "t3")
                self.assertEqual(by_id[t3_id]["last_message"], "Could you approve this?")
                self.assertNotIn(ancient_id, state)
                self.assertNotIn(codex_ancient, state)
                self.assertNotIn("cccccccc-cccc-cccc-cccc-cccccccccccc", state)
                self.assertEqual(server.auth, "Bearer local-test-key")
                self.assertNotIn("FIRST_SEEN_SHOULD_NOT_POST", json.dumps(received))
                self.assertNotIn("SUBAGENT_SHOULD_NOT_POST", json.dumps(received))
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_session_finished_between_polls_posts_once(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.HTTPServer(("localhost", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                claude_root = os.path.join(directory, "claude")
                codex_root = os.path.join(directory, "codex")
                project = os.path.join(claude_root, "-tmp-example")
                env = {
                    "WEBHOOK_URL": f"http://localhost:{server.server_port}/hook",
                    "WEBHOOK_KEY": "local-test-key",
                }
                config = isolated_config(directory, {
                    "T3_DONE_SOURCES": "claude,codex",
                    "CLAUDE_PROJECTS_DIR": claude_root,
                    "CODEX_SESSIONS_DIR": codex_root,
                    "T3_DONE_DEBOUNCE_SECONDS": "0",
                    **env,
                })
                state_path = os.path.join(directory, "state.json")
                daybook = os.path.join(directory, "daybook")
                state = {}
                now = 1_800_000_000
                recent = "2026-10-15T12:00:00Z"
                ancient_claude = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4"
                ancient_codex = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb4"
                short_claude = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa5"
                short_codex = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb5"
                grown = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa6"
                ancient_claude_path = os.path.join(project, ancient_claude + ".jsonl")
                grown_path = os.path.join(project, grown + ".jsonl")
                ancient_codex_path = os.path.join(
                    codex_root, "2026", "10", "14",
                    "rollout-2026-10-14T12-00-00-" + ancient_codex + ".jsonl",
                )

                def poll(at):
                    notify.poll_local_sessions(
                        state,
                        now=at,
                        env=env,
                        save_path=state_path,
                        daybook_dir=daybook,
                        config=config,
                    )

                def posted_ids():
                    return [item["thread_id"] for item in received]

                write_jsonl(ancient_claude_path, [
                    claude_prompt(ancient_claude, recent, "Old session"),
                    claude_assistant(
                        ancient_claude, recent, "Could you review the old session?", "end_turn"
                    ),
                ])
                write_jsonl(ancient_codex_path, [
                    codex_meta(ancient_codex, recent),
                    codex_line(recent, "event_msg", {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": "Could you review the old session?",
                    }),
                ])
                write_jsonl(grown_path, [
                    claude_prompt(grown, recent, "Baseline"),
                    claude_assistant(grown, recent, "Already finished.", "end_turn"),
                ])

                poll(now)
                self.assertEqual(received, [])
                self.assertNotIn(ancient_claude, state)
                self.assertNotIn(ancient_codex, state)
                self.assertNotIn(grown, state)
                for path in (ancient_claude_path, ancient_codex_path, grown_path):
                    recorded = state["_files"][path]
                    self.assertEqual(recorded["offset"], os.path.getsize(path))
                    self.assertEqual(recorded["mtime_ns"], os.stat(path).st_mtime_ns)
                poll(now + 1)
                self.assertEqual(received, [])
                self.assertNotIn(grown, state)

                write_jsonl(os.path.join(project, short_claude + ".jsonl"), [
                    claude_row(
                        "custom-title", short_claude, recent, customTitle="Short review"
                    ),
                    claude_prompt(short_claude, recent, "Please review"),
                    claude_assistant(
                        short_claude, "2026-10-15T12:01:00Z",
                        "Could you approve the result?", "end_turn",
                    ),
                ])
                short_codex_path = os.path.join(
                    codex_root, "2026", "10", "15",
                    "rollout-2026-10-15T12-01-00-" + short_codex + ".jsonl",
                )
                write_jsonl(short_codex_path, [
                    codex_meta(short_codex, "2026-10-15T12:01:00Z"),
                    codex_user("2026-10-15T12:01:00Z", "Run the tests"),
                    codex_line("2026-10-15T12:01:01Z", "event_msg", {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": None,
                        "error": {
                            "message": "usage limit exceeded",
                            "codex_error_info": "usage_limit_exceeded",
                        },
                    }),
                ])
                append_jsonl(grown_path, [
                    claude_prompt(grown, "2026-10-15T12:02:00Z", "One more pass"),
                    claude_assistant(
                        grown, "2026-10-15T12:02:01Z",
                        "Could you approve the grown result?", "end_turn",
                    ),
                ])

                poll(now + 2)
                self.assertEqual(received, [])
                self.assertTrue(state[short_claude]["active"])
                self.assertIsNotNone(state[short_claude]["pending_since"])
                self.assertTrue(state[short_codex]["active"])
                self.assertTrue(state[grown]["active"])
                poll(now + 3)
                keys = sorted(
                    (item["source"], item["reason"], item["thread_id"]) for item in received
                )
                self.assertEqual(keys, sorted([
                    ("claude", "done", short_claude),
                    ("codex", "error", short_codex),
                    ("claude", "done", grown),
                ]))
                by_id = {item["thread_id"]: item for item in received}
                self.assertEqual(by_id[short_claude]["last_message"], "Could you approve the result?")
                self.assertEqual(by_id[short_claude]["title"], "Short review")
                self.assertEqual(by_id[short_codex]["status"], "failed")
                self.assertEqual(by_id[short_codex]["last_message"], "usage limit exceeded")
                self.assertEqual(
                    by_id[grown]["last_message"], "Could you approve the grown result?"
                )
                grown_record = state["_files"][grown_path]
                self.assertEqual(grown_record["offset"], os.path.getsize(grown_path))
                self.assertEqual(grown_record["mtime_ns"], os.stat(grown_path).st_mtime_ns)
                poll(now + 4)
                self.assertEqual(posted_ids().count(short_claude), 1)
                self.assertEqual(posted_ids().count(short_codex), 1)
                self.assertEqual(posted_ids().count(grown), 1)
                self.assertNotIn(ancient_claude, posted_ids())
                self.assertNotIn(ancient_codex, posted_ids())
                self.assertNotIn("Could you review the old session?", json.dumps(received))
                self.assertNotIn("Already finished.", json.dumps(received))
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_claude_only_does_not_poll_t3(self):
        with tempfile.TemporaryDirectory() as directory:
            config = isolated_config(directory, {
                "T3_DONE_SOURCES": "claude",
                "CLAUDE_PROJECTS_DIR": os.path.join(directory, "missing"),
            })
            with mock.patch.object(notify, "load_config", return_value=config), \
                    mock.patch.object(notify, "snapshot", side_effect=AssertionError("t3 called")):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(notify.main(["--once"]), 0)


if __name__ == "__main__":
    unittest.main()
