#!/usr/bin/env python3
"""Install, update, or remove the Codex Castor Gemini integration."""

import argparse
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "mcp"))

from castor_platform import (
    WINDOWS, command_targets, configure_stdio, executable_path, find_codex as discover_codex,
    python_shell_command, replace_file,
)

SERVER_NAME = "codex-castor"
START_MARKER = "<!-- codex-castor:start -->"
END_MARKER = "<!-- codex-castor:end -->"
LEGACY_REPLY_RULE = (
    "- After you present the result of a `codex_dispatch` or `codex_resume` round, record the user's first reply "
    "verbatim with `codex_feedback` using the completed `task_id` and `round`. Include any rating, verdict, tags, "
    "or notes the user expresses. Record the reply even when the user asks a follow-up question or starts a new task."
)
TOOL_BACKGROUNDS = {
    "codex_dispatch": "always",
    "codex_resume": "always",
    "codex_feedback": "off",
    "codex_status": "off",
    "codex_cancel": "off",
    "codex_cleanup_worktree": "off",
    "codex_monitor": "off",
}


def find_codex():
    return executable_path(os.environ.get("CODEX_BIN")) or discover_codex()


def server_entry(python, server, codex):
    env = {"PYTHONUNBUFFERED": "1"}
    if codex:
        env["CODEX_BIN"] = codex
    return {
        "command": python,
        "args": [str(server)],
        "env": env,
        "timeoutSeconds": -1,
        "tools": {
            name: {"eager": True, "background": background}
            for name, background in TOOL_BACKGROUNDS.items()
        },
    }


def hooks_entry(python, hook_path):
    return {
        "PreInvocation": [
            {"type": "command", "command": python_shell_command(python, hook_path), "timeout": 5},
        ],
    }


def reject_json_constant(value):
    raise ValueError(f"invalid JSON value {value}")


def read_json_object(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    try:
        data = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_json_constant)
    except ValueError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw, data


def read_config(path):
    raw, data = read_json_object(path)
    if not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError(f"{path} must have an object-valued mcpServers")
    return raw, data


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def config_update(data, entry):
    if entry is None:
        servers = data.get("mcpServers", {})
        if SERVER_NAME not in servers:
            return None
        del servers[SERVER_NAME]
    else:
        servers = data.setdefault("mcpServers", {})
        if json.dumps(servers.get(SERVER_NAME), sort_keys=True) == json.dumps(entry, sort_keys=True):
            return None
        servers[SERVER_NAME] = entry
    return json_bytes(data)


def hooks_update(path, entry):
    raw, data = read_json_object(path)
    if entry is None:
        if SERVER_NAME not in data:
            return raw, None
        del data[SERVER_NAME]
    else:
        if json.dumps(data.get(SERVER_NAME), sort_keys=True) == json.dumps(entry, sort_keys=True):
            return raw, None
        data[SERVER_NAME] = entry
    return raw, json_bytes(data)


def statusline_update(path, target, *, uninstall, force, python=None):
    raw, settings = read_json_object(path)
    statusline = settings.get("statusLine", {})
    if not isinstance(statusline, dict):
        if uninstall:
            return raw, None
        raise ValueError(f"{path} must have an object-valued statusLine")
    command = statusline.get("command")
    castor_owned = command_targets(command, target, allow_bare=True)
    if uninstall:
        if not castor_owned:
            return raw, None
        statusline.update(type="", command="")
        statusline.pop("stack_with_default", None)
    else:
        if command and not castor_owned and not force:
            raise ValueError(
                f"{path} already has a custom statusLine.command. "
                "Use --force-statusline to replace it."
            )
        command = python_shell_command(python, target) if WINDOWS else shlex.quote(str(target))
        updated = {"type": "command", "command": command, "enabled": True, "stack_with_default": True}
        if statusline == updated:
            return raw, None
        settings["statusLine"] = updated
    return raw, json_bytes(settings)


def rule_span(text, path):
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start == end == -1:
        return None
    if start == -1 or end < start or text.count(START_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise ValueError(f"Expected one complete Codex Castor marker block in {path}")
    return start, end + len(END_MARKER)


def remove_rule_block(before, after):
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    # The first line in after is the remainder of the end marker's line.
    if after_lines and not after_lines[0].strip():
        after_lines.pop(0)
    separated = (before_lines and not before_lines[-1].strip()) or (
        after_lines and not after_lines[0].strip()
    )
    while before_lines and not before_lines[-1].strip():
        before_lines.pop()
    while after_lines and not after_lines[0].strip():
        after_lines.pop(0)
    if separated and before_lines and after_lines and before_lines[-1].endswith(("\n", "\r")):
        separator = "\r\n" if before_lines[-1].endswith("\r\n") else "\n"
        before_lines.append(separator)
    return "".join(before_lines + after_lines)


def rules_update(path, uninstall):
    try:
        original = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        original = None
    span = rule_span(original, path) if original is not None else None
    if uninstall:
        if span is None:
            return original, original
        start, end = span
        remaining = remove_rule_block(original[:start], original[end:])
        return original, remaining if remaining.strip() else None

    source = (REPO_ROOT / "config" / "GEMINI.md").read_bytes().decode("utf-8")
    body = source if source.endswith("\n") else source + "\n"
    block = f"{START_MARKER}\n{body}{END_MARKER}"
    if span is not None:
        start, end = span
        return original, original[:start] + block + original[end:]
    if original is None or original.strip() in (source.strip(), f"{source.strip()}\n{LEGACY_REPLY_RULE}"):
        return original, block + "\n"
    return original, original + "\n\n" + block + "\n"


def atomic_write(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            if mode is not None:
                temporary.chmod(mode)
            replace_file(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write_config_update(path, original, updated, dry_run):
    if updated is None:
        return
    if original is not None:
        backup = path.with_name(path.name + ".bak")
        print(f"Back up {path} -> {backup}")
        if not dry_run:
            atomic_write(backup, original)
    print(f"Update {path}")
    if not dry_run:
        mode = stat.S_IMODE(path.stat().st_mode) if original is not None else None
        atomic_write(path, updated, mode)


def install_artifact(source, target, data, mode, dry_run):
    if data is None:
        if target.is_symlink() and target.resolve() == source.resolve():
            return
        print(f"Link {target} -> {source}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent) as directory:
                temporary = Path(directory) / target.name
                try:
                    temporary.symlink_to(source)
                except OSError as exc:
                    if WINDOWS and getattr(exc, "winerror", None) == 1314:
                        raise ValueError("--symlink requires Windows Developer Mode or permission to create symlinks. "
                                         "Run without --symlink to install copies.") from exc
                    raise
                replace_file(temporary, target)
    elif (
        target.is_symlink()
        or not target.is_file()
        or target.read_bytes() != data
        or (not WINDOWS and stat.S_IMODE(target.stat().st_mode) != mode)
    ):
        print(f"Copy {source} -> {target}")
        if not dry_run:
            atomic_write(target, data, mode)


def remove_directory(path, dry_run):
    if path.is_symlink():
        print(f"Remove link {path}")
        if not dry_run:
            path.unlink()
    elif path.is_dir():
        print(f"Remove directory {path}")
        if not dry_run:
            shutil.rmtree(path)


def run(args):
    gemini = args.gemini_dir.expanduser().absolute()
    config_dir = gemini / "config"
    config_path = config_dir / "mcp_config.json"
    hooks_path = config_dir / "hooks.json"
    install_dir = config_dir / SERVER_NAME
    hook_target = install_dir / "hook.py"
    skill_dir = config_dir / "skills" / "codex-orchestrator"
    rules_path = gemini / "GEMINI.md"
    state_dir = gemini / SERVER_NAME
    cli_settings = gemini / "antigravity-cli" / "settings.json"
    statusline_target = install_dir / "statusline.py"

    # Read and validate configuration, rules, and sources before making changes.
    raw_config, config = read_config(config_path)
    original_rules, updated_rules = (
        (None, None) if args.skip_gemini_md else rules_update(rules_path, args.uninstall)
    )
    entry = None
    hook = None
    artifacts = []
    raw_settings, updated_settings = None, None
    if args.uninstall:
        raw_settings, updated_settings = statusline_update(
            cli_settings, statusline_target, uninstall=True, force=False,
        )
    if not args.uninstall:
        default_python = sys.executable or ("python.exe" if WINDOWS else "python3")
        python = executable_path(args.python or default_python)
        if not python:
            raise ValueError(f"Python executable not found: {args.python or default_python}")
        codex = executable_path(args.codex_bin) if args.codex_bin else find_codex()
        if args.codex_bin and not codex:
            native = " (provide the native codex.exe)" if WINDOWS else ""
            raise ValueError(f"Codex executable not found: {args.codex_bin}{native}")
        if not codex:
            print("Warning: Codex executable not found. The server will search at runtime.", file=sys.stderr)
        entry = server_entry(python, install_dir / "server.py", os.path.abspath(codex) if codex else None)
        hook = hooks_entry(python, hook_target)
        for name, target, mode in (
            ("mcp/server.py", install_dir / "server.py", 0o755),
            ("mcp/telemetry.py", install_dir / "telemetry.py", 0o644),
            ("mcp/castor_platform.py", install_dir / "castor_platform.py", 0o644),
            ("mcp/windows_process.py", install_dir / "windows_process.py", 0o644),
            ("scripts/analytics.py", install_dir / "analytics.py", 0o755),
            ("scripts/hook.py", hook_target, 0o755),
            ("mcp/report.schema.json", install_dir / "report.schema.json", 0o644),
            ("skills/codex-orchestrator/SKILL.md", skill_dir / "SKILL.md", 0o644),
        ):
            source = REPO_ROOT / name
            if not source.is_file():
                raise ValueError(f"Missing installation source: {source}")
            artifacts.append((source, target, None if args.symlink else source.read_bytes(), mode))
        monitor = REPO_ROOT / "mcp" / "monitor.html"
        if monitor.is_file():
            artifacts.append((monitor, install_dir / "monitor.html", None if args.symlink else monitor.read_bytes(), 0o644))
        if args.install_statusline:
            statusline = REPO_ROOT / "scripts" / "statusline.py"
            if statusline.is_file():
                raw_settings, updated_settings = statusline_update(
                    cli_settings, statusline_target, uninstall=False, force=args.force_statusline, python=python,
                )
                artifacts.append((statusline, statusline_target, statusline.read_bytes(), 0o755))
            else:
                print(f"Warning: Status line source is unavailable at {statusline}.", file=sys.stderr)

    updated_config = config_update(config, entry)
    raw_hooks, updated_hooks = hooks_update(hooks_path, hook)
    if args.dry_run:
        print("Dry run. No files will be changed.")
    if not args.uninstall:
        for artifact in artifacts:
            install_artifact(*artifact, args.dry_run)

    write_config_update(config_path, raw_config, updated_config, args.dry_run)
    write_config_update(hooks_path, raw_hooks, updated_hooks, args.dry_run)
    write_config_update(cli_settings, raw_settings, updated_settings, args.dry_run)
    if updated_rules != original_rules:
        if updated_rules is None:
            print(f"Remove {rules_path}")
            if not args.dry_run:
                rules_path.unlink()
        else:
            print(f"Update {rules_path}")
            if not args.dry_run:
                mode = stat.S_IMODE(rules_path.stat().st_mode) if original_rules is not None else 0o644
                atomic_write(rules_path, updated_rules.encode("utf-8"), mode)
    if args.uninstall:
        remove_directory(install_dir, args.dry_run)
        remove_directory(skill_dir, args.dry_run)
        if args.purge_state:
            remove_directory(state_dir, args.dry_run)
        else:
            print(f"Preserve runtime state at {state_dir}" if args.dry_run else f"Runtime state preserved at {state_dir}")
        if not args.dry_run:
            print(f"Codex Castor uninstalled from {gemini}")
    elif not args.dry_run:
        print(f"Codex Castor configured in {config_path}")
        print(f"PreInvocation hook configured in {hooks_path}")
        print(f"Server, schema, and hook in {install_dir}")
        print(f"Skill in {skill_dir}")
        if WINDOWS:
            escaped = str(gemini).replace("'", "''")
            print(f"Run py -3 scripts/doctor.py --gemini-dir '{escaped}'")
        else:
            print(f"Run python3 scripts/doctor.py --gemini-dir {shlex.quote(str(gemini))}")


def main():
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gemini-dir", type=Path, default=Path.home() / ".gemini")
    parser.add_argument("--python", metavar="PATH", help="Python executable for the MCP server and hook")
    parser.add_argument("--codex-bin", metavar="PATH", help="Codex CLI executable for the MCP server")
    parser.add_argument("--symlink", action="store_true", help="Link the integration files to the repo")
    parser.add_argument("--skip-gemini-md", action="store_true", help="Leave GEMINI.md untouched")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without applying them")
    parser.add_argument("--uninstall", action="store_true", help="Remove the installed integration")
    parser.add_argument("--purge-state", action="store_true", help="Also remove runtime state when uninstalling")
    parser.add_argument("--install-statusline", action="store_true", help="Install and configure the Antigravity CLI status line")
    parser.add_argument("--force-statusline", action="store_true", help="Allow replacing a custom status line during installation")
    args = parser.parse_args()
    if args.purge_state and not args.uninstall:
        parser.error("--purge-state requires --uninstall")
    if args.install_statusline and args.uninstall:
        parser.error("--install-statusline cannot be used with --uninstall")
    if args.force_statusline and not args.install_statusline:
        parser.error("--force-statusline requires --install-statusline")
    try:
        run(args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
