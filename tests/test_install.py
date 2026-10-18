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
    for key in list(environment):
        if key.startswith("XDG_"):
            environment.pop(key)
    xdg_paths = {
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "XDG_RUNTIME_DIR": os.path.join(home, ".runtime"),
    }
    for path in xdg_paths.values():
        os.makedirs(path, exist_ok=True)
    os.chmod(xdg_paths["XDG_RUNTIME_DIR"], 0o700)
    environment.update(xdg_paths)

    stub_dir = os.path.join(home, "bin")
    os.makedirs(stub_dir, exist_ok=True)
    for command in ("systemctl", "launchctl"):
        stub = os.path.join(stub_dir, command)
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)
    environment["PATH"] = stub_dir + os.pathsep + environment.get("PATH", "")
    environment.pop("CLAUDE_PROJECTS_DIR", None)
    environment.pop("CODEX_SESSIONS_DIR", None)
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
                T3_URL="http://localhost:3773",
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
            self.assertIn("T3_URL=http://localhost:3773", text)
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

    def test_custom_session_dirs_persist_into_env_and_service(self):
        with tempfile.TemporaryDirectory() as home:
            claude_dir = os.path.join(home, "claude projects")
            codex_dir = os.path.join(home, "codex-sessions")
            base = install_env(
                home,
                WEBHOOK_URL="https://hooks.example.invalid/notify",
                WEBHOOK_KEY="test-webhook-key",
                T3_DONE_SOURCES="claude,codex",
                CLAUDE_PROJECTS_DIR="~/claude projects",
                CODEX_SESSIONS_DIR="codex-sessions",
            )
            first = subprocess.run(
                [INSTALL, "--non-interactive", "--no-service"],
                cwd=home,
                env=base,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            env_file = os.path.join(home, ".config", "t3-done", "env")
            dropin = os.path.join(
                home, ".config", "systemd", "user",
                "t3-done-notify.service.d", "config.conf",
            )
            with open(env_file, encoding="utf-8") as handle:
                env_text = handle.read()
            with open(dropin, encoding="utf-8") as handle:
                dropin_text = handle.read()
            self.assertIn("CLAUDE_PROJECTS_DIR=" + claude_dir, env_text)
            self.assertIn("CODEX_SESSIONS_DIR=" + codex_dir, env_text)
            self.assertIn('Environment="CLAUDE_PROJECTS_DIR=' + claude_dir + '"', dropin_text)
            self.assertIn('Environment="CODEX_SESSIONS_DIR=' + codex_dir + '"', dropin_text)
            self.assertEqual(env_text.count("CLAUDE_PROJECTS_DIR="), 1)
            self.assertEqual(env_text.count("CODEX_SESSIONS_DIR="), 1)

            bin_dir = os.path.join(home, "bin")
            os.makedirs(bin_dir, exist_ok=True)
            uname_path = os.path.join(bin_dir, "uname")
            with open(uname_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = \"-s\" ]; then\n"
                    "  printf '%s\\n' Darwin\n"
                    "else\n"
                    "  exec /usr/bin/uname \"$@\"\n"
                    "fi\n"
                )
            os.chmod(uname_path, 0o755)
            second_env = install_env(
                home,
                WEBHOOK_URL="https://hooks.example.invalid/notify",
                WEBHOOK_KEY="test-webhook-key",
                T3_DONE_SOURCES="claude,codex",
            )
            second_env["PATH"] = bin_dir + os.pathsep + second_env.get("PATH", "")
            second = subprocess.run(
                [INSTALL, "--non-interactive", "--no-service"],
                cwd=home,
                env=second_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            with open(env_file, encoding="utf-8") as handle:
                env_text = handle.read()
            with open(dropin, encoding="utf-8") as handle:
                dropin_text = handle.read()
            self.assertIn("CLAUDE_PROJECTS_DIR=" + claude_dir, env_text)
            self.assertIn("CODEX_SESSIONS_DIR=" + codex_dir, env_text)
            self.assertEqual(env_text.count("CLAUDE_PROJECTS_DIR="), 1)
            self.assertEqual(env_text.count("CODEX_SESSIONS_DIR="), 1)
            self.assertIn('Environment="CLAUDE_PROJECTS_DIR=' + claude_dir + '"', dropin_text)
            self.assertIn('Environment="CODEX_SESSIONS_DIR=' + codex_dir + '"', dropin_text)
            plist = os.path.join(home, "Library", "LaunchAgents", "com.t3-done-notify.plist")
            with open(plist, encoding="utf-8") as handle:
                plist_text = handle.read()
            self.assertIn("<key>CLAUDE_PROJECTS_DIR</key>", plist_text)
            self.assertIn("<string>" + claude_dir + "</string>", plist_text)
            self.assertIn("<key>CODEX_SESSIONS_DIR</key>", plist_text)
            self.assertIn("<string>" + codex_dir + "</string>", plist_text)
            self.assertIn("<key>T3_DONE_CONFIG_DIR</key>", plist_text)

    def test_systemd_paths_escape_percent(self):
        with tempfile.TemporaryDirectory() as home:
            config_home = os.path.join(home, "xdg config %h")
            config_dir = os.path.join(config_home, "t3-done")
            claude_dir = os.path.join(home, "claude projects %h")
            codex_dir = os.path.join(home, "codex sessions %h")
            environment = install_env(
                home,
                WEBHOOK_URL="https://hooks.example.invalid/notify",
                WEBHOOK_KEY="test-webhook-key",
                T3_DONE_SOURCES="claude,codex",
                XDG_CONFIG_HOME=config_home,
                CLAUDE_PROJECTS_DIR="~/claude projects %h",
                CODEX_SESSIONS_DIR="codex sessions %h",
            )
            installed = subprocess.run(
                [INSTALL, "--non-interactive", "--no-service"],
                cwd=home,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            env_file = os.path.join(config_dir, "env")
            dropin = os.path.join(
                config_home, "systemd", "user",
                "t3-done-notify.service.d", "config.conf",
            )
            unit = os.path.join(config_home, "systemd", "user", "t3-done-notify.service")
            with open(env_file, encoding="utf-8") as handle:
                env_text = handle.read()
            with open(dropin, encoding="utf-8") as handle:
                dropin_text = handle.read()
            with open(unit, encoding="utf-8") as handle:
                unit_text = handle.read()

            self.assertIn("CLAUDE_PROJECTS_DIR=" + claude_dir, env_text)
            self.assertIn("CODEX_SESSIONS_DIR=" + codex_dir, env_text)
            self.assertIn(
                'Environment="T3_DONE_CONFIG_DIR=' + config_dir.replace("%", "%%") + '"',
                dropin_text,
            )
            self.assertIn(
                'Environment="CLAUDE_PROJECTS_DIR=' + claude_dir.replace("%", "%%") + '"',
                dropin_text,
            )
            self.assertIn(
                'Environment="CODEX_SESSIONS_DIR=' + codex_dir.replace("%", "%%") + '"',
                dropin_text,
            )
            self.assertIn(
                "ExecStart=%h/.local/share/t3-done-notify/.venv/bin/python "
                "%h/.local/share/t3-done-notify/t3_done_notify.py",
                unit_text,
            )

            bin_dir = os.path.join(home, "darwin-bin")
            os.makedirs(bin_dir)
            uname_path = os.path.join(bin_dir, "uname")
            with open(uname_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = \"-s\" ]; then\n"
                    "  printf '%s\\n' Darwin\n"
                    "else\n"
                    "  exec /usr/bin/uname \"$@\"\n"
                    "fi\n"
                )
            os.chmod(uname_path, 0o755)
            darwin_environment = install_env(
                home,
                WEBHOOK_URL="https://hooks.example.invalid/notify",
                WEBHOOK_KEY="test-webhook-key",
                T3_DONE_SOURCES="claude,codex",
                XDG_CONFIG_HOME=config_home,
            )
            darwin_environment["PATH"] = (
                bin_dir + os.pathsep + darwin_environment.get("PATH", "")
            )
            darwin = subprocess.run(
                [INSTALL, "--non-interactive", "--no-service"],
                cwd=home,
                env=darwin_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(darwin.returncode, 0, darwin.stderr)
            plist = os.path.join(home, "Library", "LaunchAgents", "com.t3-done-notify.plist")
            with open(plist, encoding="utf-8") as handle:
                plist_text = handle.read()
            self.assertIn("<string>" + config_dir + "</string>", plist_text)
            self.assertIn("<string>" + claude_dir + "</string>", plist_text)
            self.assertIn("<string>" + codex_dir + "</string>", plist_text)


if __name__ == "__main__":
    unittest.main()
