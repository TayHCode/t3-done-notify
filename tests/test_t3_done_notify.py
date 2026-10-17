import contextlib
import datetime
import http.server
import io
import json
import os
import stat
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import t3_done_notify as notify


def thread(status="running", updated="2026-10-15T12:00:00Z", active="run-1", thread_id="thread-1"):
    return {
        "id": thread_id,
        "title": "Example",
        "status": status,
        "updatedAt": updated,
        "activeRunId": active,
    }


def isolated_config(directory, extra=None):
    environment = {
        "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
        "XDG_STATE_HOME": os.path.join(directory, "state"),
    }
    environment.update(extra or {})
    return notify.load_config(environment)


def poll_paths(directory, **extra):
    config = isolated_config(directory)
    result = {
        "config": config,
        "watch_path": os.path.join(directory, "watch.txt"),
        "save_path": os.path.join(directory, "state.json"),
        "daybook_dir": os.path.join(directory, "daybook"),
        "env": {},
    }
    result.update(extra)
    return result


class NotifyTests(unittest.TestCase):
    def test_debounce_waits_two_minutes_and_fires_once(self):
        state = {}
        self.assertIsNone(notify.transition(state, thread(), 0))
        stopped = thread("completed", "2026-10-15T12:01:00Z", None)
        self.assertIsNone(notify.transition(state, stopped, 10))
        self.assertIsNone(notify.transition(state, stopped, 129))
        result = notify.transition(state, stopped, 130)
        self.assertEqual(result["reason"], "done")
        self.assertIsNone(notify.transition(state, stopped, 300))

    def test_resuming_during_debounce_cancels_pending_transition(self):
        state = {}
        notify.transition(state, thread(), 0)
        notify.transition(state, thread("idle", "2026-10-15T12:01:00Z", None), 1)
        self.assertIsNone(notify.transition(state, thread(), 60))
        self.assertIsNone(notify.transition(state, thread("idle", "2026-10-15T12:02:00Z", None), 61))
        self.assertIsNone(notify.transition(state, thread("idle", "2026-10-15T12:02:00Z", None), 180))

    def test_failed_is_error_and_stall_fires_once_per_updated_timestamp(self):
        state = {}
        failed = thread("failed", "2026-10-15T12:01:00Z", None)
        notify.transition(state, thread(), 0)
        notify.transition(state, failed, 1)
        self.assertEqual(notify.transition(state, failed, 121)["reason"], "error")
        old = thread(updated="2026-10-15T10:00:00Z")
        stale_now = notify.parse_time(old["updatedAt"]) + notify.STALL_SECONDS + 1
        self.assertEqual(notify.transition(state, old, stale_now)["reason"], "stalled")
        self.assertIsNone(notify.transition(state, old, stale_now + 100))

    def test_configuration_paths_precedence_and_token_file_default(self):
        with tempfile.TemporaryDirectory() as directory:
            xdg_config = os.path.join(directory, "xdg-config")
            config_dir = os.path.join(xdg_config, "t3-done")
            os.makedirs(config_dir)
            with open(os.path.join(config_dir, "env"), "w", encoding="utf-8") as handle:
                handle.write(
                    "WEBHOOK_URL=http://localhost/hook\n"
                    "WEBHOOK_KEY=file-key\n"
                    "T3_DONE_POLL_SECONDS=11\n"
                    "T3_DONE_CONFIG_DIR=/ignored-from-env-file\n"
                )
            config = notify.load_config({
                "XDG_CONFIG_HOME": xdg_config,
                "XDG_STATE_HOME": os.path.join(directory, "xdg-state"),
                "WEBHOOK_KEY": "process-key",
                "T3_DONE_POLL_SECONDS": "17",
                "T3_DONE_WATCH_FILE": os.path.join(directory, "process-watch.txt"),
            })
            self.assertEqual(config.config_dir, config_dir)
            self.assertEqual(config.environment["WEBHOOK_KEY"], "process-key")
            self.assertEqual(config.poll_seconds, 17)
            self.assertEqual(
                config.state_file,
                os.path.join(directory, "xdg-state", "t3-done", "state.json"),
            )
            self.assertEqual(
                config.watch_file, os.path.join(directory, "process-watch.txt")
            )
            self.assertEqual(
                config.daybook_dir,
                os.path.join(directory, "xdg-state", "t3-done", "daybook"),
            )
            self.assertEqual(
                config.environment["T3_TOKEN_FILE"],
                os.path.join(config_dir, "t3-token"),
            )
            self.assertEqual(
                notify.validate_webhook(config.environment),
                ("http://localhost/hook", "process-key"),
            )

    def test_config_dir_environment_variable_selects_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = os.path.join(directory, "custom-config")
            os.makedirs(config_dir)
            with open(os.path.join(config_dir, "env"), "w", encoding="utf-8") as handle:
                handle.write("WEBHOOK_URL=https://hooks.example.invalid/notify\n")
                handle.write("WEBHOOK_KEY=test-key\n")
            config = notify.load_config({
                "T3_DONE_CONFIG_DIR": config_dir,
                "XDG_STATE_HOME": os.path.join(directory, "xdg-state"),
            })
            self.assertEqual(config.config_dir, config_dir)
            self.assertEqual(config.env_file, os.path.join(config_dir, "env"))
            self.assertEqual(config.environment["WEBHOOK_KEY"], "test-key")

    def test_invalid_numeric_settings_and_partial_credentials_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in ("0", "-1", "nan", "inf", "not-a-number"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    notify.load_config({
                        "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
                        "T3_DONE_POLL_SECONDS": value,
                    })
            with self.assertRaises(ValueError):
                notify.load_config({
                    "T3_DONE_CONFIG_DIR": os.path.join(directory, "config"),
                    "WEBHOOK_URL": "https://hooks.example.invalid/x",
                })
        with self.assertRaises(ValueError):
            notify.deliver({"thread_id": "example"}, {"WEBHOOK_KEY": "partial"})
        self.assertIsNone(notify.validate_webhook({}))

    def test_subprocess_uses_merged_env_suppresses_stderr_and_has_no_lane_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = os.path.join(directory, "fake-t3ctl")
            with open(adapter, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/bin/sh\n"
                    "printf '%s' \"$T3_URL\" >&2\n"
                    "if [ \"$1\" = lanes ]; then\n"
                    "  [ \"$2\" = --json ] && [ \"$3\" = --include-archived ] || exit 9\n"
                    "  [ -z \"$4\" ] || exit 8\n"
                    "  printf '[{\"id\":\"thread-1\"}]'\n"
                    "else\n"
                    "  printf '{\"messages\":[{\"text\":\"finished\"}]}'\n"
                    "fi\n"
                )
            os.chmod(adapter, 0o700)
            config = isolated_config(directory, {
                "T3_DONE_T3CTL": adapter,
                "T3_URL": "https://api.example.invalid",
                "T3_TOKEN_FILE": os.path.join(directory, "token"),
            })
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                lanes = notify.snapshot(config.environment, config)
                message = notify.last_message("thread-1", config.environment, config)
            self.assertEqual(list(lanes), ["thread-1"])
            self.assertEqual(message, "finished")
            self.assertEqual(output.getvalue(), "")

    def test_subprocess_timeout_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = os.path.join(directory, "slow-t3ctl")
            with open(adapter, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/usr/bin/env python3\n"
                    "import time\n"
                    "time.sleep(2)\n"
                    "print('[]')\n"
                )
            os.chmod(adapter, 0o700)
            config = isolated_config(directory, {
                "T3_DONE_T3CTL": adapter,
                "T3_DONE_T3CTL_TIMEOUT_SECONDS": "0.05",
            })
            with self.assertRaises(subprocess.TimeoutExpired):
                notify.snapshot(config.environment, config)

    def test_watch_ids_are_deduplicated_and_exact_match_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = os.path.join(directory, "watch.txt")
            exact = thread(thread_id="thread-1")
            prefixed = thread(thread_id="thread:project:example:thread-1-extra")
            with open(watch, "w", encoding="utf-8") as handle:
                handle.write("thread-1\nthread-1\n")
            self.assertEqual(notify.watch_ids(watch), ["thread-1"])
            state = {}
            notify.poll_once(
                state,
                now=1,
                env={},
                snap={"thread-1": exact, prefixed["id"]: prefixed},
                message_reader=lambda _: "",
                save_path=os.path.join(directory, "state.json"),
                watch_path=watch,
                daybook_dir=os.path.join(directory, "daybook"),
                config=isolated_config(directory),
            )
            self.assertIn("thread-1", state)
            self.assertNotIn(prefixed["id"], state)

    def test_prefix_watch_matches_synthetic_thread_suffix_and_migrates_state(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = os.path.join(directory, "watch.txt")
            with open(watch, "w", encoding="utf-8") as handle:
                handle.write("example-prefix\n")
            actual_id = "thread:project:example:example-prefix-00000000-0000"
            stopped = thread(
                "completed",
                "2026-10-15T12:01:00Z",
                None,
                thread_id=actual_id,
            )
            state = {"example-prefix": {"active": True}}
            notify.poll_once(
                state,
                now=10,
                env={},
                snap={actual_id: stopped},
                message_reader=lambda _: "",
                save_path=os.path.join(directory, "state.json"),
                watch_path=watch,
                daybook_dir=os.path.join(directory, "daybook"),
                config=isolated_config(directory),
            )
            self.assertNotIn("example-prefix", state)
            self.assertEqual(state[actual_id]["pending_since"], 10)

    def test_reader_failure_does_not_commit_transition_or_lose_event(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = os.path.join(directory, "watch.txt")
            with open(watch, "w", encoding="utf-8") as handle:
                handle.write("thread-1\n")
            config = isolated_config(directory)
            common = {
                "watch_path": watch,
                "save_path": os.path.join(directory, "state.json"),
                "daybook_dir": os.path.join(directory, "daybook"),
                "env": {},
                "config": config,
            }
            active = thread()
            done = thread("completed", "2026-10-15T12:01:00Z", None)
            state = {}
            notify.poll_once(state, now=1, snap={"thread-1": active},
                             message_reader=lambda _: "", **common)
            notify.poll_once(state, now=10, snap={"thread-1": done},
                             message_reader=lambda _: "", **common)
            before = json.loads(json.dumps(state))

            def broken_reader(_):
                raise OSError("reader failed")

            with self.assertRaises(OSError):
                notify.poll_once(state, now=130, snap={"thread-1": done},
                                 message_reader=broken_reader, **common)
            self.assertEqual(state, before)
            notify.poll_once(state, now=130, snap={"thread-1": done},
                             message_reader=lambda _: "Could you review this?", **common)
            self.assertEqual(len(state["_outbox"]), 1)
            self.assertEqual(state["_outbox"][0]["payload"]["last_message"],
                             "Could you review this?")

    def test_daybook_failure_does_not_commit_transition_or_lose_event(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = os.path.join(directory, "watch.txt")
            with open(watch, "w", encoding="utf-8") as handle:
                handle.write("thread-1\n")
            blocked_daybook = os.path.join(directory, "not-a-directory")
            with open(blocked_daybook, "w", encoding="utf-8") as handle:
                handle.write("occupied")
            config = isolated_config(directory)
            common = {
                "watch_path": watch,
                "save_path": os.path.join(directory, "state.json"),
                "daybook_dir": blocked_daybook,
                "env": {},
                "config": config,
                "message_reader": lambda _: "Please approve?",
            }
            active = thread()
            done = thread("completed", "2026-10-15T12:01:00Z", None)
            state = {}
            notify.poll_once(state, now=1, snap={"thread-1": active}, **common)
            notify.poll_once(state, now=10, snap={"thread-1": done}, **common)
            before = json.loads(json.dumps(state))
            with self.assertRaises(OSError):
                notify.poll_once(state, now=130, snap={"thread-1": done}, **common)
            self.assertEqual(state, before)
            os.unlink(blocked_daybook)
            common["daybook_dir"] = os.path.join(directory, "daybook")
            notify.poll_once(state, now=130, snap={"thread-1": done}, **common)
            self.assertEqual(len(state["_outbox"]), 1)

    def test_no_credentials_keeps_attention_event_queued_for_later(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = os.path.join(directory, "watch.txt")
            with open(watch, "w", encoding="utf-8") as handle:
                handle.write("thread-1\n")
            common = poll_paths(directory)
            common["message_reader"] = lambda _: "I need someone to approve this."
            state = {}
            done = thread("completed", "2026-10-15T12:01:00Z", None)
            notify.poll_once(state, now=1, snap={"thread-1": thread()}, **common)
            notify.poll_once(state, now=10, snap={"thread-1": done}, **common)
            notify.poll_once(state, now=130, snap={"thread-1": done}, **common)
            self.assertEqual(len(state["_outbox"]), 1)
            self.assertNotIn("last_fired", state["thread-1"])
            saved = notify.load_state(common["save_path"])
            self.assertEqual(len(saved["_outbox"]), 1)

    def test_completion_without_attention_only_writes_private_daybook(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = os.path.join(directory, "watch.txt")
            with open(watch, "w", encoding="utf-8") as handle:
                handle.write("thread-1\n")
            common = poll_paths(directory, message_reader=lambda _: "All done.")
            done = thread("completed", "2026-10-15T12:01:00Z", None)
            state = {}
            notify.poll_once(state, now=1, snap={"thread-1": thread()}, **common)
            notify.poll_once(state, now=10, snap={"thread-1": done}, **common)
            notify.poll_once(state, now=130, snap={"thread-1": done}, **common)
            self.assertEqual(state.get("_outbox", []), [])
            files = os.listdir(common["daybook_dir"])
            self.assertEqual(len(files), 1)
            daybook = os.path.join(common["daybook_dir"], files[0])
            with open(daybook, encoding="utf-8") as handle:
                logged = handle.read()
            self.assertIn("Example", logged)
            self.assertIn("thread-1", logged)
            self.assertIn("completed", logged)
            self.assertIn("All done.", logged)
            self.assertEqual(stat.S_IMODE(os.stat(daybook).st_mode), 0o600)

    def test_local_webhook_success_full_message_and_restart_deduplication(self):
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
                watch = os.path.join(directory, "watch.txt")
                statefile = os.path.join(directory, "state.json")
                with open(watch, "w", encoding="utf-8") as handle:
                    handle.write("thread-1\n")
                config = isolated_config(directory)
                state = {}
                done = thread("completed", "2026-10-15T12:01:00Z", None)
                message = "Could you approve this? " + "x" * 2000
                common = {
                    "watch_path": watch,
                    "save_path": statefile,
                    "daybook_dir": os.path.join(directory, "daybook"),
                    "env": {
                        "WEBHOOK_URL": f"http://localhost:{server.server_port}/hook",
                        "WEBHOOK_KEY": "local-test-key",
                    },
                    "message_reader": lambda _: message,
                    "config": config,
                }
                notify.poll_once(state, now=1, snap={"thread-1": thread()}, **common)
                notify.poll_once(state, now=10, snap={"thread-1": done}, **common)
                notify.poll_once(state, now=130, snap={"thread-1": done}, **common)
                restored = notify.load_state(statefile)
                notify.poll_once(restored, now=500, snap={"thread-1": done}, **common)
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0]["reason"], "done")
                self.assertEqual(len(received[0]["last_message"]), len(message))
                daybook_name = datetime.datetime.now().strftime("%Y-%m-%d") + ".md"
                with open(os.path.join(directory, "daybook", daybook_name),
                          encoding="utf-8") as handle:
                    self.assertIn("x" * 2000, handle.read())
                self.assertEqual(server.auth, "Bearer local-test-key")
                self.assertEqual(stat.S_IMODE(os.stat(statefile).st_mode), 0o600)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_non_2xx_delivery_stays_queued_and_retries_on_later_poll(self):
        codes = [500] * 5 + [200]
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                code = codes.pop(0)
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"response body must not be logged")

            def log_message(self, *_):
                pass

        server = http.server.HTTPServer(("localhost", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        old_delays = notify.RETRY_DELAYS
        notify.RETRY_DELAYS = (0, 0, 0, 0)
        output = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as directory:
                watch = os.path.join(directory, "watch.txt")
                with open(watch, "w", encoding="utf-8") as handle:
                    handle.write("thread-1\n")
                common = poll_paths(directory,
                    env={
                        "WEBHOOK_URL": f"http://localhost:{server.server_port}/hook",
                        "WEBHOOK_KEY": "local-test-key",
                    },
                    message_reader=lambda _: "Could you approve this?",
                )
                state = {}
                done = thread("completed", "2026-10-15T12:01:00Z", None)
                with contextlib.redirect_stdout(output):
                    notify.poll_once(state, now=1, snap={"thread-1": thread()}, **common)
                    notify.poll_once(state, now=10, snap={"thread-1": done}, **common)
                    notify.poll_once(state, now=130, snap={"thread-1": done}, **common)
                self.assertEqual(len(received), 5)
                self.assertEqual(len(state["_outbox"]), 1)
                self.assertNotIn("last_fired", state["thread-1"])
                self.assertNotIn("response body", output.getvalue())
                self.assertNotIn("local-test-key", output.getvalue())
                restored = notify.load_state(common["save_path"])
                notify.poll_once(restored, now=200, snap={"thread-1": done}, **common)
                self.assertEqual(len(received), 6)
                self.assertEqual(restored["_outbox"], [])
                self.assertTrue(restored["thread-1"]["last_fired"].startswith("done:"))
                notify.poll_once(restored, now=300, snap={"thread-1": done}, **common)
                self.assertEqual(len(received), 6)
        finally:
            notify.RETRY_DELAYS = old_delays
            server.shutdown()
            worker.join()
            server.server_close()

    def test_redirect_is_not_followed_or_sent_auth_to_second_host(self):
        second_host_requests = []

        class Receiver(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                second_host_requests.append(self.headers.get("Authorization"))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        second = http.server.HTTPServer(("localhost", 0), Receiver)
        second_worker = threading.Thread(target=second.serve_forever, daemon=True)
        second_worker.start()

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header(
                    "Location", f"http://localhost:{second.server_port}/redirect-target"
                )
                self.end_headers()

            def log_message(self, *_):
                pass

        first = http.server.HTTPServer(("localhost", 0), Redirector)
        first_worker = threading.Thread(target=first.serve_forever, daemon=True)
        first_worker.start()
        old_delays = notify.RETRY_DELAYS
        notify.RETRY_DELAYS = ()
        try:
            result = notify.deliver(
                {"thread_id": "thread-1", "last_message": "example"},
                {
                    "WEBHOOK_URL": f"http://localhost:{first.server_port}/hook",
                    "WEBHOOK_KEY": "local-test-key",
                },
            )
            self.assertFalse(result)
            self.assertEqual(second_host_requests, [])
        finally:
            notify.RETRY_DELAYS = old_delays
            first.shutdown()
            first_worker.join()
            first.server_close()
            second.shutdown()
            second_worker.join()
            second.server_close()

    def test_existing_outbox_is_flushed_even_when_snapshot_fails(self):
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
                watch = os.path.join(directory, "watch.txt")
                with open(watch, "w", encoding="utf-8") as handle:
                    handle.write("thread-1\n")
                state_path = os.path.join(directory, "state.json")
                payload = {
                    "thread_id": "thread-1",
                    "title": "Example",
                    "status": "failed",
                    "reason": "error",
                    "updated_at": "2026-10-15T12:01:00Z",
                    "last_message": "example failure",
                }
                state = {
                    "thread-1": {"active": False},
                    "_outbox": [{"payload": payload, "queued_at": 1}],
                }
                common = {
                    "env": {
                        "WEBHOOK_URL": f"http://localhost:{server.server_port}/hook",
                        "WEBHOOK_KEY": "local-test-key",
                    },
                    "save_path": state_path,
                    "watch_path": watch,
                    "daybook_dir": os.path.join(directory, "daybook"),
                    "config": isolated_config(directory),
                }
                with mock.patch.object(
                    notify, "snapshot", side_effect=RuntimeError("snapshot unavailable")
                ):
                    with self.assertRaises(RuntimeError):
                        notify.poll_once(state, now=10, **common)
                self.assertEqual(len(received), 1)
                self.assertEqual(state["_outbox"], [])
                self.assertTrue(state["thread-1"]["last_fired"].startswith("error:"))
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_explicit_dry_run_does_not_acknowledge_queued_events(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "thread_id": "thread-1",
                "reason": "done",
                "updated_at": "2026-10-15T12:01:00Z",
                "last_message": "example",
            }
            state = {"_outbox": [{"payload": payload, "queued_at": 1}]}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                notify.flush_outbox(
                    state,
                    {
                        "WEBHOOK_URL": "https://hooks.example.invalid/notify",
                        "WEBHOOK_KEY": "local-test-key",
                    },
                    save_path=os.path.join(directory, "state.json"),
                    dry_run=True,
                )
            self.assertEqual(len(state["_outbox"]), 1)
            self.assertNotIn("last_fired", state)
            self.assertIn("dry-run: pending notification", output.getvalue())

    def test_once_returns_failure_when_poll_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            config = isolated_config(directory)
            with mock.patch.object(notify, "load_config", return_value=config), \
                 mock.patch.object(notify, "poll_once", side_effect=OSError("poll failed")):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(notify.main(["--once"]), 1)

    def test_save_state_supports_bare_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            try:
                os.chdir(directory)
                notify.save_state({"thread-1": {"active": True}}, "state.json")
                self.assertEqual(
                    notify.load_state("state.json"),
                    {"thread-1": {"active": True}},
                )
                self.assertEqual(stat.S_IMODE(os.stat("state.json").st_mode), 0o600)
            finally:
                os.chdir(previous_directory)


if __name__ == "__main__":
    unittest.main()
