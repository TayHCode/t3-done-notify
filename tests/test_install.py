import os
import stat
import subprocess
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")


def install_env(home, **extra):
    environment = os.environ.copy()
    environment["HOME"] = home
    environment.pop("XDG_CONFIG_HOME", None)
    environment.pop("XDG_STATE_HOME", None)
    environment.update(extra)
    return environment


class InstallTests(unittest.TestCase):
    def test_script_syntax(self):
        subprocess.check_call(["sh", "-n", INSTALL])

    def test_interactive_claude_and_codex(self):
        with tempfile.TemporaryDirectory() as home:
            completed = subprocess.run(
                [INSTALL, "--no-service"],
                input="https://hooks.example.invalid/notify\ninteractive-key\nClaude Code, Codex\n",
                text=True,
                cwd=ROOT,
                env=install_env(home),
                check=False,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            env_file = os.path.join(home, ".config", "t3-done", "env")
            with open(env_file, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("WEBHOOK_URL=https://hooks.example.invalid/notify", text)
            self.assertIn("WEBHOOK_KEY=interactive-key", text)
            self.assertIn("T3_DONE_SOURCES=claude,codex", text)
            self.assertNotIn("T3_URL=", text)
            self.assertEqual(stat.S_IMODE(os.stat(env_file).st_mode), 0o600)
            self.assertIn("Service start skipped.", completed.stdout)
            unit = os.path.join(
                home, ".config", "systemd", "user", "t3-done-notify.service"
            )
            self.assertTrue(os.path.isfile(unit))

    def test_noninteractive_rerun_writes_token_and_one_env(self):
        with tempfile.TemporaryDirectory() as home:
            base = install_env(
                home,
                WEBHOOK_URL="https://hooks.example.invalid/notify",
                WEBHOOK_KEY="test-webhook-key",
                T3_DONE_SOURCES="t3,claude",
                T3_URL="http://127.0.0.1:3773",
                T3_TOKEN="read-only-token-value",
            )
            first = subprocess.run(
                [INSTALL, "--non-interactive", "--no-service"],
                cwd=ROOT,
                env=base,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            config = os.path.join(home, ".config", "t3-done")
            env_file = os.path.join(config, "env")
            token_file = os.path.join(config, "t3-token")
            with open(token_file, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "read-only-token-value")
            self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)
            with open(env_file, encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("read-only-token-value", text)
            self.assertIn("T3_URL=http://127.0.0.1:3773", text)
            self.assertIn("T3_DONE_SOURCES=t3,claude", text)
            self.assertNotIn("T3_TOKEN_FILE=", text)
            self.assertTrue(os.path.isfile(os.path.join(config, "watch.txt")))
            dropin = os.path.join(
                home, ".config", "systemd", "user",
                "t3-done-notify.service.d", "config.conf",
            )
            with open(dropin, encoding="utf-8") as handle:
                self.assertIn("T3_DONE_CONFIG_DIR=" + config, handle.read())
            python = os.path.join(home, ".local", "share", "t3-done-notify", ".venv", "bin", "python")
            self.assertTrue(os.path.isfile(python))

            second_env = dict(base)
            second_env["WEBHOOK_URL"] = "https://hooks.example.invalid/other"
            second_env["T3_DONE_SOURCES"] = "codex"
            second_env.pop("T3_TOKEN")
            second = subprocess.run(
                [INSTALL, "--non-interactive", "--no-service"],
                cwd=ROOT,
                env=second_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            with open(env_file, encoding="utf-8") as handle:
                text = handle.read()
            self.assertEqual(text.count("WEBHOOK_URL="), 1)
            self.assertIn("WEBHOOK_URL=https://hooks.example.invalid/other", text)
            self.assertIn("T3_DONE_SOURCES=codex", text)
            self.assertIn("WEBHOOK_KEY=test-webhook-key", text)
            with open(token_file, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "read-only-token-value")


if __name__ == "__main__":
    unittest.main()
