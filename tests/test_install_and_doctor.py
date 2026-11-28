"""Installer and diagnostic CLI tests with a temporary Gemini home."""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from platform_fixture import WINDOWS, environment, platform_shell, script_executable

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.py"
WRAPPER = ROOT / "scripts" / "install.sh"
DOCTOR = ROOT / "scripts" / "doctor.py"
EXAMPLE = ROOT / "config" / "mcp_config.example.json"
RULES_SOURCE = ROOT / "config" / "GEMINI.md"
START = "<!-- codex-castor:start -->"
END = "<!-- codex-castor:end -->"
OTHER_CONFIG = {
    "preferences": {"unicode": "用户"},
    "mcpServers": {"other-mcp": {"command": "other", "args": ["--hello"]}},
}
OTHER_HOOKS = {
    "impeccable": {"PreInvocation": [{"type": "command", "command": "other-hook", "timeout": 15}]},
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScriptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="codex-castor-install-tests-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.gemini = self.home / ("Gemini 的配置 & logs" if WINDOWS else "Gemini's config")
        self.config = self.gemini / "config" / "mcp_config.json"
        self.backup = self.config.with_suffix(".json.bak")
        self.hooks = self.config.with_name("hooks.json")
        self.hooks_backup = self.hooks.with_suffix(".json.bak")
        self.rules = self.gemini / "GEMINI.md"
        self.cli_settings = self.gemini / "antigravity-cli" / "settings.json"
        self.state = self.gemini / "codex-castor"
        self.install_dir = self.gemini / "config" / "codex-castor"
        self.skill = self.gemini / "config" / "skills" / "codex-orchestrator"
        self.artifacts = {
            self.install_dir / "server.py": ROOT / "mcp" / "server.py",
            self.install_dir / "telemetry.py": ROOT / "mcp" / "telemetry.py",
            self.install_dir / "castor_platform.py": ROOT / "mcp" / "castor_platform.py",
            self.install_dir / "windows_process.py": ROOT / "mcp" / "windows_process.py",
            self.install_dir / "analytics.py": ROOT / "scripts" / "analytics.py",
            self.install_dir / "hook.py": ROOT / "scripts" / "hook.py",
            self.install_dir / "report.schema.json": ROOT / "mcp" / "report.schema.json",
            self.skill / "SKILL.md": ROOT / "skills" / "codex-orchestrator" / "SKILL.md",
        }
        self.codex = self.version_script(self.root / "bin" / "codex", "codex mock 1.2.3")
        self.env = {
            **environment(),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "CODEX_BIN": str(self.codex),
            "CODEX_HOME": str(self.root / "codex-home"),
            "CODEX_CASTOR_STATE_DIR": str(self.root / "unused-runtime-state"),
            "PATH": os.pathsep.join((str(self.codex.parent), os.environ.get("PATH") or os.defpath)),
        }
        self.env.pop("CODEX_CASTOR_MONITOR_HTML", None)

    def version_script(self, path, message, code=0):
        if WINDOWS:
            return script_executable(path, f"import sys\nassert sys.argv[1:] == ['--version']\nprint({message!r})\nsys.exit({code})\n")
        return script_executable(path,
            f'#!/bin/sh\n[ "$#" -eq 1 ] && [ "$1" = "--version" ] || exit 99\n'
            f"printf '%s\\n' '{message}'\nexit {code}\n",
        )

    def run_script(self, script, *args, expected=0, env=None, python=sys.executable):
        command = [str(script)] if script == WRAPPER else [python, str(script)]
        result = subprocess.run(
            [*command, "--gemini-dir", str(self.gemini), *map(str, args)],
            env=self.env if env is None else env,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
        )
        self.assertEqual(result.returncode, expected, f"{result.stdout}\n{result.stderr}")
        return result

    def doctor(self, *args, expected=0, env=None):
        result = self.run_script(DOCTOR, "--json", *args, expected=expected, env=env)
        report = json.loads(result.stdout)
        self.assertIs(report["ok"], expected == 0)
        return {item["name"]: item for item in report["checks"]}

    def seed_user_config(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(OTHER_CONFIG, ensure_ascii=False).encode("utf-8")
        self.config.write_bytes(original)
        return original

    def seed_user_hooks(self):
        self.hooks.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(OTHER_HOOKS).encode("utf-8")
        self.hooks.write_bytes(original)
        return original

    def run_installed_hook(self, python):
        target = self.install_dir / "hook.py"
        hook = shlex.quote(str(target))
        command = (f'if exist "{target}" "{python}" -X utf8 "{target}"' if WINDOWS else
                   f"[ ! -f {hook} ] || {shlex.quote(str(python))} {hook}")
        expected = {"PreInvocation": [{
            "type": "command", "command": command, "timeout": 5,
        }]}
        actual = read_json(self.hooks)["codex-castor"]
        self.assertEqual(actual, expected)
        transcript = self.root / "transcript.jsonl"
        transcript.touch()
        payload = {"conversationId": "install-test", "transcriptPath": str(transcript), "hookEventName": "PreInvocation"}
        result = subprocess.run(
            platform_shell(actual["PreInvocation"][0]["command"]),
            env=self.env, cwd=self.root, input=json.dumps(payload), text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "{}\n")
        self.assertEqual(result.stderr, "")
        state = Path(self.env["CODEX_CASTOR_STATE_DIR"]) / "hook_state" / "install-test.json"
        self.assertEqual(read_json(state)["transcript_path"], str(transcript))

    def installer_with_ui_assets(self):
        repo = self.root / "fixture-sources"
        for source in (INSTALL, RULES_SOURCE, *self.artifacts.values()):
            target = repo / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        (repo / "mcp" / "monitor.html").write_text(
            "<!doctype html><title>Fixture monitor</title><p>Codex Castor Monitor</p>", encoding="utf-8",
        )
        (repo / "scripts" / "statusline.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        return repo / "scripts" / "install.py"

    def snapshot(self):
        entries = {}
        for path in self.gemini.rglob("*"):
            contents = path.readlink() if path.is_symlink() else path.read_bytes() if path.is_file() else None
            info = path.lstat()
            entries[path.relative_to(self.gemini)] = (contents, info.st_mtime_ns, info.st_mode)
        return entries

    def test_fresh_install_matches_example_and_preserves_python_executable(self):
        python = Path(sys.executable)
        if not WINDOWS:
            python = self.root / "venv" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
        result = self.run_script(INSTALL, "--python", python, "--codex-bin", self.codex)
        self.assertIn("scripts/doctor.py", result.stdout)
        for target, source in self.artifacts.items():
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), source.read_bytes())
        analytics = self.install_dir / "analytics.py"
        if not WINDOWS:
            self.assertEqual((self.install_dir / "server.py").stat().st_mode & 0o777, 0o755)
            self.assertEqual((self.install_dir / "telemetry.py").stat().st_mode & 0o777, 0o644)
            self.assertEqual(analytics.stat().st_mode & 0o777, 0o755)
            self.assertEqual((self.install_dir / "hook.py").stat().st_mode & 0o777, 0o755)
        self.run_installed_hook(python)
        command = [str(python), str(analytics)] if WINDOWS else [str(analytics)]
        result = subprocess.run([*command, "--help"], cwd=self.root, env=self.env, capture_output=True,
                                text=True, timeout=5, check=True)
        self.assertIn("{summary,backfill,feedback,export}", result.stdout)
        expected = read_json(EXAMPLE)
        entry = expected["mcpServers"]["codex-castor"]
        entry["command"] = str(python)
        entry["args"] = [str(self.install_dir / "server.py")]
        entry["env"]["CODEX_BIN"] = str(self.codex)
        actual = read_json(self.config)
        self.assertEqual(actual, expected)
        self.assertEqual(self.config.read_text(encoding="utf-8"), json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
        expected_rules = f"{START}\n{RULES_SOURCE.read_text().rstrip()}\n{END}\n"
        self.assertEqual(self.rules.read_text(), expected_rules)
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.hooks_backup.exists())

    def test_default_python_keeps_virtual_environment_and_path_finds_codex(self):
        env = {key: value for key, value in self.env.items() if key != "CODEX_BIN"}
        venv = self.root / "test-venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], env=env, check=True, timeout=25)
        python = venv / ("Scripts/python.exe" if WINDOWS else "bin/python3")
        self.run_script(INSTALL, env=env, python=str(python))
        entry = read_json(self.config)["mcpServers"]["codex-castor"]
        self.assertEqual(entry["command"], str(python))
        self.assertEqual(entry["env"]["CODEX_BIN"], str(self.codex))

    def test_merge_backup_and_second_install_are_idempotent(self):
        previous_config = self.seed_user_config()
        previous_hooks = self.seed_user_hooks()
        user_rules = b"# My rules\r\nKeep existing whitespace.  \r\n"
        self.rules.write_bytes(user_rules)
        self.run_script(INSTALL)
        installed = read_json(self.config)
        self.assertEqual(installed["preferences"], OTHER_CONFIG["preferences"])
        self.assertEqual(installed["mcpServers"]["other-mcp"], OTHER_CONFIG["mcpServers"]["other-mcp"])
        self.assertEqual(self.backup.read_bytes(), previous_config)
        installed_hooks = read_json(self.hooks)
        self.assertIn("codex-castor", installed_hooks)
        self.assertEqual({name: entry for name, entry in installed_hooks.items() if name != "codex-castor"}, OTHER_HOOKS)
        self.assertEqual(self.hooks_backup.read_bytes(), previous_hooks)
        self.assertTrue(self.rules.read_bytes().startswith(user_rules + b"\n\n"))
        snapshot = self.snapshot()
        self.run_script(INSTALL)
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(self.rules.read_text().count(START), 1)
        self.assertEqual(self.rules.read_text().count(END), 1)

    def test_existing_markers_are_updated_without_changing_surrounding_bytes(self):
        self.gemini.mkdir()
        before = b"  My rules  \r\n\r\n"
        after = b"\r\n\r\nAdditional rules  \r\n"
        self.rules.write_bytes(before + f"{START}\r\nold\r\n{END}".encode() + after)
        self.run_script(INSTALL)
        content = self.rules.read_bytes()
        self.assertEqual(content, before + START.encode() + b"\n" + RULES_SOURCE.read_bytes() + END.encode() + after)
        self.run_script(INSTALL, "--uninstall")
        self.assertEqual(self.rules.read_bytes(), before + b"Additional rules  \r\n")

    def test_legacy_rules_are_upgraded_without_duplication(self):
        self.gemini.mkdir()
        self.rules.write_text("\n" + RULES_SOURCE.read_text() + "\n", encoding="utf-8")
        self.run_script(INSTALL)
        self.assertEqual(self.rules.read_text(), f"{START}\n{RULES_SOURCE.read_text()}{END}\n")
        self.run_script(INSTALL, "--uninstall")
        self.assertFalse(self.rules.exists())

    def test_previous_unmarked_rules_are_upgraded_without_stale_reply_instructions(self):
        installer = load_module(INSTALL)
        self.gemini.mkdir()
        self.rules.write_text(RULES_SOURCE.read_text() + installer.LEGACY_REPLY_RULE + "\n", encoding="utf-8")
        self.run_script(INSTALL)
        self.assertEqual(self.rules.read_text(), f"{START}\n{RULES_SOURCE.read_text()}{END}\n")

    def test_wrapper_and_symlink_mode_can_switch_back_to_copies(self):
        if WINDOWS:
            probe = self.root / "symlink-probe"
            try:
                probe.symlink_to(INSTALL)
            except OSError as exc:
                if getattr(exc, "winerror", None) != 1314:
                    raise
                result = self.run_script(INSTALL, "--symlink", expected=1)
                self.assertIn("Windows Developer Mode", result.stderr)
                self.assertFalse(self.config.exists())
                return
        self.run_script(INSTALL if WINDOWS else WRAPPER, "--symlink", "--skip-gemini-md", "--python", sys.executable)
        for target, source in self.artifacts.items():
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), source)
        self.run_installed_hook(sys.executable)
        snapshot = self.snapshot()
        self.run_script(INSTALL, "--symlink", "--skip-gemini-md", "--python", sys.executable)
        self.assertEqual(self.snapshot(), snapshot)
        self.assertFalse(self.rules.exists())
        self.run_script(INSTALL, "--skip-gemini-md")
        for target, source in self.artifacts.items():
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), source.read_bytes())
        self.run_script(INSTALL, "--symlink", "--skip-gemini-md")
        self.assertTrue(all(target.is_symlink() for target in self.artifacts))

    def test_dry_run_leaves_fresh_and_existing_homes_untouched(self):
        fresh = self.run_script(INSTALL, "--dry-run")
        self.assertIn("Dry run", fresh.stdout)
        self.assertIn(str(self.config), fresh.stdout)
        self.assertIn(str(self.hooks), fresh.stdout)
        self.assertFalse(self.gemini.exists())

        self.seed_user_config()
        self.seed_user_hooks()
        self.rules.write_text("Personal rules\n", encoding="utf-8")
        original = self.snapshot()
        plan = self.run_script(INSTALL, "--dry-run", "--symlink")
        self.assertIn(str(self.backup), plan.stdout)
        self.assertIn(str(self.hooks_backup), plan.stdout)
        self.assertEqual(self.snapshot(), original)
        self.run_script(INSTALL)
        self.state.mkdir()
        installed = self.snapshot()
        plan = self.run_script(INSTALL, "--dry-run", "--uninstall", "--purge-state")
        self.assertIn(str(self.state), plan.stdout)
        self.assertEqual(self.snapshot(), installed)

    def test_uninstall_keeps_other_entries_and_state_then_purges_explicitly(self):
        self.seed_user_config()
        self.seed_user_hooks()
        user_rules = "Personal rules  \n"
        self.rules.write_text(user_rules, encoding="utf-8")
        self.install_state()
        self.run_script(INSTALL, *(() if WINDOWS else ("--symlink",)))
        previous_config = self.config.read_bytes()
        previous_hooks = self.hooks.read_bytes()
        result = self.run_script(INSTALL, "--uninstall")
        self.assertIn("Runtime state preserved", result.stdout)
        self.assertFalse(self.install_dir.exists())
        self.assertFalse(self.skill.exists())
        self.assertEqual(read_json(self.config), OTHER_CONFIG)
        self.assertEqual(self.backup.read_bytes(), previous_config)
        self.assertEqual(read_json(self.hooks), OTHER_HOOKS)
        self.assertEqual(self.hooks_backup.read_bytes(), previous_hooks)
        self.assertEqual(self.rules.read_text(), user_rules)
        self.assertTrue((self.state / "tasks" / "task.json").is_file())

        preserved = self.snapshot()
        self.run_script(INSTALL, "--uninstall")
        self.assertEqual(self.snapshot(), preserved)
        self.run_script(INSTALL, "--uninstall", "--purge-state")
        self.assertFalse(self.state.exists())
        self.assertEqual(read_json(self.config), OTHER_CONFIG)
        self.assertEqual(self.rules.read_text(), user_rules)

    def test_install_monitor_and_optional_statusline_with_fresh_settings(self):
        installer = self.installer_with_ui_assets()
        self.run_script(installer, "--install-statusline", "--dry-run")
        self.assertFalse(self.gemini.exists())
        self.run_script(installer, "--install-statusline")
        source_monitor = installer.parent.parent / "mcp" / "monitor.html"
        self.assertEqual((self.install_dir / "monitor.html").read_bytes(), source_monitor.read_bytes())
        statusline = self.install_dir / "statusline.py"
        self.assertEqual(statusline.read_bytes(), installer.with_name("statusline.py").read_bytes())
        if not WINDOWS:
            self.assertEqual(statusline.stat().st_mode & 0o777, 0o755)
        expected = f'if exist "{statusline}" "{sys.executable}" -X utf8 "{statusline}"' if WINDOWS else shlex.quote(str(statusline))
        self.assertEqual(read_json(self.cli_settings)["statusLine"], {
            "type": "command", "command": expected, "enabled": True, "stack_with_default": True,
        })
        self.assertFalse(self.cli_settings.with_suffix(".json.bak").exists())
        self.assertEqual(self.doctor()["cli_statusline"]["status"], "PASS")
        snapshot = self.snapshot()
        self.run_script(installer, "--install-statusline")
        self.assertEqual(self.snapshot(), snapshot)

    def test_statusline_force_backup_uninstall_and_third_party_preservation(self):
        installer = self.installer_with_ui_assets()
        self.cli_settings.parent.mkdir(parents=True)
        third_party = {
            "other": {"theme": "dark"},
            "statusLine": {"type": "command", "command": "/custom/statusline.py", "enabled": False},
        }
        self.cli_settings.write_text(json.dumps(third_party), encoding="utf-8")
        original = self.cli_settings.read_bytes()
        snapshot = self.snapshot()
        refused = self.run_script(installer, "--install-statusline", expected=1)
        self.assertIn("custom statusLine.command", refused.stderr)
        self.assertIn("--force-statusline", refused.stderr)
        self.assertEqual(self.snapshot(), snapshot)

        self.run_script(installer, "--uninstall")
        self.assertEqual(self.cli_settings.read_bytes(), original)
        backup = self.cli_settings.with_suffix(".json.bak")
        self.assertFalse(backup.exists())
        self.run_script(installer, "--install-statusline", "--force-statusline")
        self.assertEqual(backup.read_bytes(), original)
        installed = read_json(self.cli_settings)
        self.assertEqual(installed["other"], third_party["other"])
        self.assertTrue(installed["statusLine"]["stack_with_default"])
        installed["statusLine"].update(enabled=False, additional="keep")
        self.cli_settings.write_text(json.dumps(installed), encoding="utf-8")
        before_removal = self.cli_settings.read_bytes()
        self.run_script(installer, "--uninstall")
        self.assertEqual(read_json(self.cli_settings), {
            "other": third_party["other"],
            "statusLine": {"type": "", "command": "", "enabled": False, "additional": "keep"},
        })
        self.assertEqual(backup.read_bytes(), before_removal)
        self.assertFalse((self.install_dir / "statusline.py").exists())

    @unittest.skipUnless(WINDOWS, "Windows Antigravity command shell")
    def test_windows_hook_and_statusline_keep_literal_shell_paths(self):
        self.gemini = self.home / "Gemini %CASTOR_TEST_MARKER%! '审阅"
        self.env["CASTOR_TEST_MARKER"] = "must not expand"
        self.run_script(INSTALL, "--install-statusline")
        install_dir = self.gemini / "config" / "codex-castor"
        hooks = read_json(install_dir.parent / "hooks.json")
        hook_command = hooks["codex-castor"]["PreInvocation"][0]["command"]
        settings = self.gemini / "antigravity-cli" / "settings.json"
        status_command = read_json(settings)["statusLine"]["command"]
        transcript = self.root / "转录.jsonl"
        transcript.touch()
        state = Path(self.env["CODEX_CASTOR_STATE_DIR"])
        payload = {"conversationId": "windows-shell", "transcriptPath": str(transcript)}
        hook = subprocess.run(platform_shell(hook_command), input=json.dumps(payload), cwd=self.root,
                              env=self.env, capture_output=True, encoding="utf-8", timeout=10, check=True)
        self.assertEqual(hook.stdout, "{}\n")
        saved = read_json(state / "hook_state" / "windows-shell.json")
        self.assertEqual(saved["transcript_path"], str(transcript))
        task = state / "tasks" / "live" / "meta.json"
        task.parent.mkdir(parents=True)
        task.write_text(json.dumps({"status": "running", "pid": os.getpid(), "current_activity": "审阅"}),
                        encoding="utf-8")
        line = subprocess.run(platform_shell(status_command), input='{"terminal_width":120}', cwd=self.root,
                              env=self.env, capture_output=True, encoding="utf-8", timeout=10, check=True)
        self.assertIn("审阅", line.stdout)
        checks = self.doctor()
        self.assertEqual(checks["hooks_config"]["status"], "PASS")
        self.assertEqual(checks["cli_statusline"]["status"], "PASS")
        self.run_script(INSTALL, "--uninstall")
        self.assertEqual(read_json(settings)["statusLine"]["command"], "")

    def install_state(self):
        (self.state / "tasks").mkdir(parents=True)
        (self.state / "worktrees").mkdir()
        (self.state / "tasks" / "task.json").write_text("{}", encoding="utf-8")

    def test_skip_rules_applies_to_install_and_uninstall(self):
        self.gemini.mkdir()
        original = f"# User\n\n{START}\nold rules\n{END}\n"
        self.rules.write_text(original, encoding="utf-8")
        self.run_script(INSTALL, "--skip-gemini-md")
        self.assertEqual(self.rules.read_text(), original)
        self.run_script(INSTALL, "--uninstall", "--skip-gemini-md")
        self.assertEqual(self.rules.read_text(), original)

    def test_purge_state_requires_uninstall(self):
        self.install_state()
        original = self.snapshot()
        result = self.run_script(INSTALL, "--purge-state", expected=2)
        self.assertIn("--purge-state requires --uninstall", result.stderr)
        self.assertEqual(self.snapshot(), original)

    def test_invalid_json_or_mcp_servers_aborts_install_and_uninstall(self):
        self.run_script(INSTALL)
        for invalid in (b"{broken", b'{"mcpServers": null}', b'{"other": NaN}', b"[]"):
            for flags in ((), ("--uninstall", "--purge-state")):
                with self.subTest(invalid=invalid, flags=flags):
                    self.config.write_bytes(invalid)
                    snapshot = self.snapshot()
                    result = self.run_script(INSTALL, *flags, expected=1)
                    self.assertIn(str(self.config), result.stderr)
                    self.assertEqual(self.snapshot(), snapshot)

    def test_invalid_hooks_json_aborts_install_and_uninstall_before_any_changes(self):
        self.run_script(INSTALL)
        for invalid in (b"{broken", b'{"other": NaN}', b"[]"):
            for flags in (("--symlink",), ("--uninstall", "--purge-state")):
                with self.subTest(invalid=invalid, flags=flags):
                    self.hooks.write_bytes(invalid)
                    snapshot = self.snapshot()
                    result = self.run_script(INSTALL, *flags, expected=1)
                    self.assertIn(str(self.hooks), result.stderr)
                    self.assertEqual(self.snapshot(), snapshot)

    def test_missing_codex_warns_and_leaves_environment_key_unset(self):
        installer = load_module(INSTALL)
        with mock.patch.object(installer, "find_codex", return_value=None), \
                mock.patch.object(sys, "argv", [str(INSTALL), "--gemini-dir", str(self.gemini)]), \
                mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr") as stderr:
            self.assertEqual(installer.main(), 0)
            self.assertTrue(stdout.write.called)
            self.assertIn("Codex executable not found", stderr.write.call_args_list[0].args[0])
        env = read_json(self.config)["mcpServers"]["codex-castor"]["env"]
        self.assertEqual(env, {"PYTHONUNBUFFERED": "1"})

    def test_repo_only_doctor_uses_real_mcp_without_installing(self):
        checks = self.doctor("--repo-only")
        self.assertEqual(set(checks), {
            "python_version", "git_cli", "codex_cli", "repo_files", "mcp_stdio_handshake",
        })
        self.assertTrue(all(item["status"] == "PASS" for item in checks.values()), checks)
        self.assertIn("hook", checks["repo_files"]["message"])
        for endpoint in ("codex_status", "resources/list", "resources/read", "codex_monitor"):
            self.assertIn(endpoint, checks["mcp_stdio_handshake"]["message"])
        self.assertFalse(self.gemini.exists())
        self.assertFalse(Path(self.env["CODEX_CASTOR_STATE_DIR"]).exists())
        self.assertFalse((self.home / ".gemini").exists())

    def test_installed_doctor_passes_after_install_and_fails_cleanly_before(self):
        checks = self.doctor("--codex-bin", self.codex, expected=1)
        for name in ("installed_files", "mcp_config", "hooks_config", "mcp_stdio_handshake"):
            self.assertEqual(checks[name]["status"], "FAIL")
        self.assertEqual(checks["gemini_rules"]["status"], "WARN")
        self.assertEqual(checks["codex_cli"]["status"], "PASS")
        self.assertFalse(self.gemini.exists())

        self.run_script(INSTALL)
        checks = self.doctor("--codex-bin", self.codex)
        self.assertEqual(set(checks), {
            "python_version", "git_cli", "codex_cli", "installed_files", "mcp_config",
            "hooks_config", "gemini_rules", "mcp_stdio_handshake", "cli_statusline",
        })
        self.assertEqual(checks.pop("cli_statusline")["status"], "INFO")
        self.assertTrue(all(item["status"] == "PASS" for item in checks.values()), checks)
        self.assertFalse(self.state.exists())
        self.assertFalse(Path(self.env["CODEX_CASTOR_STATE_DIR"]).exists())

    def test_doctor_requires_hook_file_and_matching_preinvocation_handler(self):
        self.run_script(INSTALL)
        target = self.install_dir / "hook.py"
        doctor = load_module(DOCTOR)
        command = read_json(self.hooks)["codex-castor"]["PreInvocation"][0]["command"]
        for invalid in (
            [], OTHER_HOOKS, {"codex-castor": {"PreInvocation": []}},
            {"codex-castor": {"PreInvocation": [{"command": command, "type": "prompt"}]}},
            {"codex-castor": {"PreInvocation": [{"type": "command", "command": command + ".bak"}]}},
        ):
            with self.subTest(invalid=invalid):
                self.hooks.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertEqual(doctor.check_hooks(self.hooks, target)["status"], "FAIL")
        handlers = [None, {"command": "'bad quoting"}, {"command": command}]
        self.hooks.write_text(json.dumps({"codex-castor": {"PreInvocation": handlers}}), encoding="utf-8")
        self.assertEqual(doctor.check_hooks(self.hooks, target)["status"], "PASS")

        target.unlink()
        checks = self.doctor(expected=1)
        self.assertEqual(checks["hooks_config"]["status"], "PASS")
        self.assertEqual(checks["installed_files"]["status"], "FAIL")
        self.assertIn(str(target), checks["installed_files"]["message"])

    def test_doctor_resolves_codex_from_config_and_path_and_reports_bad_versions(self):
        configured = self.version_script(self.root / "configured" / "codex", "configured codex")
        self.run_script(INSTALL, "--codex-bin", configured, "--skip-gemini-md")
        self.assertEqual(self.doctor()["codex_cli"]["message"], "codex mock 1.2.3")
        env = {key: value for key, value in self.env.items() if key != "CODEX_BIN"}
        checks = self.doctor(env=env)
        self.assertEqual(checks["codex_cli"]["message"], "configured codex")
        self.assertEqual(checks["gemini_rules"]["status"], "WARN")

        broken = self.version_script(self.root / "broken" / "codex", "bad codex", code=3)
        checks = self.doctor("--codex-bin", broken, env=env, expected=1)
        self.assertEqual(checks["codex_cli"]["status"], "FAIL")
        self.assertIn("exited 3", checks["codex_cli"]["message"])

        configured.unlink()
        self.assertEqual(self.doctor(env=env)["codex_cli"]["message"], "codex mock 1.2.3")

    def test_doctor_reports_missing_codex_by_mode(self):
        doctor = load_module(DOCTOR)
        with mock.patch.object(doctor, "find_codex", return_value=None), \
                mock.patch.object(doctor, "executable_path", return_value=None):
            for repo_only, expected in ((True, "WARN"), (False, "FAIL")):
                with self.subTest(repo_only=repo_only):
                    item = doctor.check_codex(Namespace(codex_bin=None, repo_only=repo_only), {})
                    self.assertEqual(item["status"], expected)

    def test_doctor_reports_invalid_schema_and_background_settings(self):
        self.run_script(INSTALL)
        (self.install_dir / "report.schema.json").write_text('{"required": ["status"]}', encoding="utf-8")
        config = read_json(self.config)
        config["mcpServers"]["codex-castor"]["tools"]["codex_resume"]["background"] = "off"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        checks = self.doctor(expected=1)
        self.assertEqual(checks["installed_files"]["status"], "FAIL")
        self.assertEqual(checks["mcp_config"]["status"], "FAIL")
        self.assertIn("codex_resume.background", checks["mcp_config"]["message"])
        self.assertEqual(checks["mcp_stdio_handshake"]["status"], "PASS")

        config["mcpServers"]["codex-castor"]["timeoutSeconds"] = 10
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.assertIn("timeoutSeconds", self.doctor(expected=1)["mcp_config"]["message"])

    def test_doctor_times_out_and_stops_unresponsive_server(self):
        doctor = load_module(DOCTOR)
        hanging = self.root / "hanging.py"
        hanging.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        with mock.patch.object(doctor, "TIMEOUT", 0.3), mock.patch.dict(os.environ, self.env):
            check = doctor.check_handshake(hanging)
        self.assertEqual(check["status"], "FAIL")
        self.assertIn("initialize timed out", check["message"])

    def test_doctor_rejects_nonzero_server_exit_after_exchange(self):
        server = self.root / "bad-exit.py"
        server.write_text(
            f"import runpy\ntry:\n    runpy.run_path({str(ROOT / 'mcp' / 'server.py')!r}, run_name='__main__')\n"
            f"except SystemExit:\n    raise SystemExit(7)\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, self.env):
            check = load_module(DOCTOR).check_handshake(server)
        self.assertEqual(check["status"], "FAIL")
        self.assertIn("exited with code 7", check["message"])


if __name__ == "__main__":
    unittest.main()
