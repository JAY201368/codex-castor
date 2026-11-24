#!/usr/bin/env python3
"""Check Codex Castor configuration and exercise its MCP stdio connection."""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "codex-castor"
TIMEOUT = 5
MONITOR_URI = "ui://codex-castor/monitor.html"
MONITOR_MIME = "text/html;profile=mcp-app"
TOOL_BACKGROUNDS = {
    "codex_dispatch": "always",
    "codex_resume": "always",
    "codex_status": "off",
    "codex_cancel": "off",
    "codex_cleanup_worktree": "off",
    "codex_monitor": "off",
}
REPORT_FIELDS = {"status", "summary", "files_changed", "commands_run", "tests_summary", "open_issues"}


def check(name, status, message):
    return {"name": name, "status": status, "message": message}


def reject_json_constant(value):
    raise ValueError(f"invalid JSON value {value}")


def probe_version(name, executable):
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, errors="replace", timeout=TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check(name, "FAIL", str(exc))
    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode:
        detail = f" ({output})" if output else ""
        return check(name, "FAIL", f"{executable} --version exited {result.returncode}{detail}")
    return check(name, "PASS", output or executable)


def read_config(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
            raise ValueError("expected an object-valued mcpServers")
        entry = data["mcpServers"].get(SERVER_NAME)
        if not isinstance(entry, dict):
            raise ValueError(f"missing or invalid mcpServers.{SERVER_NAME}")
    except (OSError, ValueError) as exc:
        return {}, check("mcp_config", "FAIL", f"{path}: {exc}")
    if type(entry.get("timeoutSeconds")) is not int or entry["timeoutSeconds"] != -1:
        return entry, check("mcp_config", "FAIL", "timeoutSeconds must be -1")
    tools = entry.get("tools")
    if not isinstance(tools, dict):
        return entry, check("mcp_config", "FAIL", "tools must be an object")
    for name, background in TOOL_BACKGROUNDS.items():
        tool = tools.get(name)
        if not isinstance(tool, dict) or tool.get("background") != background:
            return entry, check("mcp_config", "FAIL", f"{name}.background must be {background!r}")
    return entry, check("mcp_config", "PASS", str(path))


def check_codex(args, entry):
    env = entry.get("env")
    configured = env.get("CODEX_BIN") if isinstance(env, dict) else None
    candidates = (args.codex_bin,) if args.codex_bin else (os.environ.get("CODEX_BIN"), configured)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            executable = shutil.which(os.path.expanduser(candidate))
            if executable:
                return probe_version("codex_cli", executable)
    if args.codex_bin:
        return check("codex_cli", "FAIL", f"Codex executable not found: {args.codex_bin}")
    search = os.pathsep.join((
        os.environ.get("PATH") or os.defpath,
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".npm-global" / "bin"),
    ))
    executable = shutil.which("codex", path=search)
    if executable:
        return probe_version("codex_cli", executable)
    return check(
        "codex_cli", "WARN" if args.repo_only else "FAIL",
        "Codex executable not found. Set CODEX_BIN or add codex to PATH.",
    )


def check_files(name, server, skill):
    schema = server.with_name("report.schema.json")
    try:
        for path in (server, schema, skill):
            if not path.is_file():
                raise ValueError(f"Missing file: {path}")
        data = json.loads(schema.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
        required = data.get("required") if isinstance(data, dict) else None
        if (
            not isinstance(required, list)
            or not all(isinstance(field, str) for field in required)
            or not REPORT_FIELDS.issubset(required)
        ):
            raise ValueError(f"{schema} must be a JSON object listing the required report fields")
    except (OSError, ValueError) as exc:
        return check(name, "FAIL", str(exc))
    return check(name, "PASS", f"Server, report schema, and skill found ({server.parent})")


def check_rules(path):
    try:
        if SERVER_NAME in path.read_text(encoding="utf-8"):
            return check("gemini_rules", "PASS", str(path))
    except (OSError, ValueError) as exc:
        return check("gemini_rules", "WARN", f"{path}: {exc}")
    return check("gemini_rules", "WARN", f"{path} does not mention {SERVER_NAME}")


def check_cli_statusline(path):
    try:
        settings = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
        statusline = settings.get("statusLine") if isinstance(settings, dict) else None
        command = statusline.get("command") if isinstance(statusline, dict) else None
    except FileNotFoundError:
        command = None
    except (OSError, ValueError) as exc:
        return check("cli_statusline", "INFO", f"Optional status line could not be checked in {path}: {exc}")
    if isinstance(command, str) and Path(command).parts[-2:] == (SERVER_NAME, "statusline.py"):
        return check("cli_statusline", "PASS", f"Codex Castor status line configured in {path}")
    return check("cli_statusline", "INFO", "Optional Codex Castor status line can be installed with --install-statusline")


def read_stdout(stream, output):
    try:
        with stream:
            for line in stream:
                output.put(line)
    finally:
        output.put(None)


def exchange(process, output):
    def send(method, params, request_id=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        process.stdin.flush()

    def request(request_id, method, params):
        send(method, params, request_id)
        try:
            line = output.get(timeout=TIMEOUT)
        except queue.Empty as exc:
            raise ValueError(f"{method} timed out") from exc
        if line is None:
            raise ValueError(f"Server closed stdout before replying to {method}")
        try:
            reply = json.loads(line, parse_constant=reject_json_constant)
        except ValueError as exc:
            raise ValueError(f"{method} received invalid JSON: {exc}") from exc
        if (
            not isinstance(reply, dict)
            or reply.get("jsonrpc") != "2.0"
            or type(reply.get("id")) is not int
            or reply["id"] != request_id
            or "error" in reply
            or not isinstance(reply.get("result"), dict)
        ):
            raise ValueError(f"Unexpected {method} response: {reply!r}")
        return reply["result"]

    initialized = request(1, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "codex-castor-doctor", "version": "1.1"},
    })
    server_info = initialized.get("serverInfo")
    if not isinstance(server_info, dict) or server_info.get("name") != SERVER_NAME:
        raise ValueError(f"initialize did not identify {SERVER_NAME}")
    send("notifications/initialized", {})
    if request(2, "ping", {}) != {}:
        raise ValueError("ping did not return an empty result")
    tools = request(3, "tools/list", {}).get("tools")
    if (
        not isinstance(tools, list)
        or len(tools) != len(TOOL_BACKGROUNDS)
        or not all(isinstance(tool, dict) and isinstance(tool.get("name"), str) for tool in tools)
        or {tool["name"] for tool in tools} != set(TOOL_BACKGROUNDS)
    ):
        raise ValueError("tools/list did not return the six Codex Castor tools")
    result = request(4, "tools/call", {"name": "codex_status", "arguments": {}})
    if result.get("isError") is not False:
        raise ValueError("codex_status returned an error")
    try:
        item = result["content"][0]
        status = json.loads(item["text"], parse_constant=reject_json_constant)
        if item.get("type") != "text" or status["tasks"] != []:
            raise ValueError("expected an empty task list")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid codex_status result: {exc}") from exc

    def monitor_resource(items):
        if isinstance(items, list):
            return next((
                item for item in items if isinstance(item, dict)
                and item.get("uri") == MONITOR_URI and item.get("mimeType") == MONITOR_MIME
            ), None)
        return None

    resources = request(5, "resources/list", {}).get("resources")
    if monitor_resource(resources) is None:
        raise ValueError("resources/list did not list the MCP Apps monitor")
    contents = request(6, "resources/read", {"uri": MONITOR_URI}).get("contents")
    resource = monitor_resource(contents)
    html = resource.get("text") if resource else None
    if not isinstance(html, str) or not html.strip():
        raise ValueError("resources/read did not return monitor HTML")
    monitor = request(7, "tools/call", {"name": "codex_monitor", "arguments": {}})
    if monitor.get("isError") is not False or not isinstance(monitor.get("structuredContent"), dict):
        raise ValueError("codex_monitor did not return a structured snapshot")


def check_handshake(server):
    name = "mcp_stdio_handshake"
    try:
        with tempfile.TemporaryDirectory(prefix="codex-castor-doctor-") as state, tempfile.TemporaryFile() as stderr:
            env = {**os.environ, "CODEX_CASTOR_STATE_DIR": state, "PYTHONUNBUFFERED": "1"}
            process = subprocess.Popen(
                [sys.executable, str(server)],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
            with process.stdin:
                output = queue.Queue()
                reader = threading.Thread(target=read_stdout, args=(process.stdout, output), daemon=True)
                reader.start()
                error = None
                try:
                    exchange(process, output)
                    process.stdin.close()
                    try:
                        code = process.wait(timeout=TIMEOUT)
                    except subprocess.TimeoutExpired as exc:
                        raise ValueError("Server did not exit after stdin closed") from exc
                    if code:
                        raise ValueError(f"Server exited with code {code}")
                    try:
                        if output.get(timeout=TIMEOUT) is not None:
                            raise ValueError("Server sent unexpected stdout after the last reply")
                    except queue.Empty as exc:
                        raise ValueError("Server stdout did not close") from exc
                except (OSError, ValueError) as exc:
                    error = str(exc)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=TIMEOUT)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                if error:
                    stderr.seek(max(0, stderr.tell() - 1500))
                    detail = stderr.read().decode("utf-8", errors="replace").strip()
                    return check(name, "FAIL", f"{error}\n{detail}" if detail else error)
    except OSError as exc:
        return check(name, "FAIL", str(exc))
    return check(
        name, "PASS",
        "initialize, ping, tools/list, codex_status, resources/list, resources/read, codex_monitor, and clean exit",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gemini-dir", type=Path, default=Path.home() / ".gemini")
    parser.add_argument("--repo-only", action="store_true", help="Check the repository without using an installation")
    parser.add_argument("--codex-bin", metavar="PATH", help="Codex executable to probe")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args()

    gemini = args.gemini_dir.expanduser().absolute()
    if args.repo_only:
        server = REPO_ROOT / "mcp" / "server.py"
        skill = REPO_ROOT / "skills" / "codex-orchestrator" / "SKILL.md"
        entry = {}
    else:
        config_dir = gemini / "config"
        server = config_dir / SERVER_NAME / "server.py"
        skill = config_dir / "skills" / "codex-orchestrator" / "SKILL.md"
        entry, config_check = read_config(config_dir / "mcp_config.json")

    checks = [
        check("python_version", "PASS" if sys.version_info >= (3, 9) else "FAIL", sys.version.split()[0]),
        probe_version("git_cli", "git"),
        check_codex(args, entry),
        check_files("repo_files" if args.repo_only else "installed_files", server, skill),
    ]
    if not args.repo_only:
        checks.extend([
            config_check, check_rules(gemini / "GEMINI.md"),
            check_cli_statusline(gemini / "antigravity-cli" / "settings.json"),
        ])
    checks.append(check_handshake(server))
    ok = all(item["status"] != "FAIL" for item in checks)
    if args.json:
        print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        print(f"{'Check':<21} {'Status':<6} Details")
        for item in checks:
            print(f"{item['name']:<21} {item['status']:<6} {item['message']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
