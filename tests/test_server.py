"""End-to-end MCP tests using a local, deterministic Codex fixture."""

import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp" / "server.py"
TOOL_NAMES = {
    "codex_dispatch", "codex_resume", "codex_status", "codex_cancel", "codex_cleanup_worktree",
    "codex_monitor", "codex_feedback",
}
MONITOR_URI = "ui://codex-castor/monitor.html"
MONITOR_MIME = "text/html;profile=mcp-app"
SESSION = "01234567-89ab-cdef-0123-456789abcdef"
USAGE = {"input_tokens": 12, "output_tokens": 4}
TIMEOUT = 10
WEEKLY_QUOTA = {"rateLimits": {
    "limitId": "codex", "planType": "prolite",
    "primary": {"usedPercent": 78, "windowDurationMins": 10080, "resetsAt": 1796148683},
    "secondary": None,
}}
DUAL_QUOTA = {"rateLimits": {
    **WEEKLY_QUOTA["rateLimits"], "planType": "pro",
    "secondary": {"usedPercent": 16, "windowDurationMins": 300},
}}
MOCK_CODEX = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
sync = Path(os.environ["MOCK_CODEX_SYNC"])
if args[0] == "app-server":
    assert args == ["app-server", "--listen", "stdio://"], args
    with (sync / "quota-calls.log").open("a", encoding="utf-8") as stream:
        stream.write(str(os.getpid()) + "\\n")
    initialize = json.loads(sys.stdin.readline())
    assert initialize["id"] == 1 and initialize["method"] == "initialize"
    assert initialize["params"]["clientInfo"]["name"] == "codex-castor"
    assert initialize["params"]["clientInfo"]["version"]
    print(json.dumps({"method": "fixture/unrelated"}))
    print(json.dumps({"id": 1, "result": {}}), flush=True)
    assert json.loads(sys.stdin.readline()) == {"method": "initialized"}
    assert json.loads(sys.stdin.readline()) == {"id": 2, "method": "account/rateLimits/read"}
    if os.environ.get("MOCK_CODEX_QUOTA_STALL"):
        sys.stdout.write('{"id":2,')
        sys.stdout.flush()
        (sync / "quota-partial-response").touch()
        time.sleep(60)
    if os.environ.get("MOCK_CODEX_QUOTA_FAIL") or (sync / "quota-fail").exists():
        print(json.dumps({"id": 2, "error": {"code": -32000, "message": "No subscription"}}), flush=True)
    else:
        quota_file = os.environ.get("MOCK_CODEX_QUOTA_FILE")
        payload = (Path(quota_file).read_text(encoding="utf-8") if quota_file and Path(quota_file).is_file()
                   else os.environ.get("MOCK_CODEX_QUOTA_JSON", "{}"))
        print(json.dumps({"id": 2, "result": json.loads(payload)}), flush=True)
    sys.exit(0)

assert args[0] == "exec" and args[-1] == "-" and "--json" in args, args
report_path = Path(args[args.index("-o") + 1])
prompt = sys.stdin.read()
session = "01234567-89ab-cdef-0123-456789abcdef"
workdir = Path.cwd()
record = {"args": args, "cwd": str(workdir), "prompt": prompt, "pid": os.getpid()}
report_path.with_name("mock-" + report_path.name).write_text(json.dumps(record), encoding="utf-8")

def event(event_type, **data):
    print(json.dumps({"type": event_type, **data}), flush=True)

def wait_for(path):
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for " + str(path))
        time.sleep(0.01)

event("thread.started", thread_id=session)
if prompt.startswith("parallel-"):
    (sync / prompt).touch()
    wait_for(sync / "release")
elif prompt == "slow":
    time.sleep(60)
elif prompt == "observe":
    event("turn.started")
    event("item.started", item={"type": "command_execution", "command": "/bin/zsh -lc 'printf  hello'"})
    wait_for(sync / "show-file")
    event("item.started", item={"type": "file_change", "changes": [{"path": "main.py"}]})
    wait_for(sync / "show-message")
    event("item.completed", item={"type": "agent_message", "text": "Finished the live edit."})
    wait_for(sync / "finish")
elif prompt == "observe-rollout-only":
    event("turn.started")
    wait_for(sync / "finish")
elif prompt == "activity-burst":
    for index in range(12):
        event("item.started", item={"type": "reasoning", "id": str(index)})
    event("item.completed", item={"type": "command_execution", "command": "/bin/bash -lc 'git status'",
                                  "exit_code": 1})
    event("item.completed", item={"type": "error", "message": "notice about --dangerously-bypass-hook-trust"})
elif prompt == "fail-turn":
    event("turn.failed", error={"message": "Error: request rejected for thread " + session})
    event("turn.failed")
    event("error", message="Codex is ignoring a hook")
    event("error", message="notice about --dangerously-bypass-hook-trust")
    print("Error: stderr fallback", file=sys.stderr)
    sys.exit(0)
elif prompt == "fail-stderr":
    raise ValueError("invalid session configuration")
elif prompt == "recover from failure":
    event("error", message="notice about --dangerously-bypass-hook-trust")

if args[1] == "resume":
    assert args[-2] == session, args
else:
    assert Path(args[args.index("-C") + 1]) == workdir, args
tracked = workdir / "tracked.txt"
if tracked.exists():
    with tracked.open("a", encoding="utf-8") as stream:
        stream.write(prompt + "\\n")
report = {
    "status": "completed",
    "summary": "Handled " + prompt,
    "files_changed": ["tracked.txt"] if tracked.exists() else [],
    "commands_run": [],
    "tests_summary": "mock",
    "open_issues": [],
}
report_path.write_text(json.dumps(report), encoding="utf-8")
event("item.completed", item={"type": "agent_message", "text": json.dumps(report)})
if prompt == "observe-rollout-only":
    event("turn.completed")
else:
    event("turn.completed", usage={"input_tokens": 12, "output_tokens": 4})
"""


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_server():
    spec = importlib.util.spec_from_file_location("castor_server", SERVER)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


class ServerHarness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="codex-castor-tests-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state = self.root / "state"
        self.sync = self.root / "sync"
        self.sync.mkdir()
        self.mock_quota = self.sync / "quota-payload.json"
        self.quota_calls = self.sync / "quota-calls.log"
        self.quota_cache = self.state / "quota.json"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.monitor_html = self.root / "fixture-monitor.html"
        home = self.root / "home"
        home.mkdir()
        fake = self.root / "mock-codex"
        fake.write_text(MOCK_CODEX, encoding="utf-8")
        fake.chmod(0o755)
        self.env = {name: value for name, value in os.environ.items()
                    if not name.startswith(("GIT_", "MOCK_CODEX_"))}
        self.env.update(
            HOME=str(home),
            USERPROFILE=str(home),
            GIT_CEILING_DIRECTORIES=str(self.root.parent),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            CODEX_BIN=str(fake),
            CODEX_HOME=str(self.codex_home),
            CODEX_CASTOR_STATE_DIR=str(self.state),
            CODEX_CASTOR_MONITOR_HTML=str(self.monitor_html),
            MOCK_CODEX_SYNC=str(self.sync),
            MOCK_CODEX_QUOTA_FILE=str(self.mock_quota),
        )
        self.stderr = tempfile.TemporaryFile()
        self.addCleanup(self.stderr.close)
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            env=self.env,
        )
        self.responses = queue.Queue()
        self.deferred = {}
        self.notifications = []
        self.request_id = 0
        self.reader = threading.Thread(target=self.read_stdout, daemon=True)
        self.addCleanup(self.close_server)
        self.reader.start()

    def read_stdout(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def server_log(self):
        self.stderr.seek(0)
        return self.stderr.read().decode("utf-8", errors="replace")

    def close_server(self):
        try:
            self.process.stdin.close()
            try:
                code = self.process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                self.fail("MCP server did not exit after stdin closed")
            self.assertEqual(code, 0, self.server_log())
        finally:
            self.reader.join(timeout=TIMEOUT)
            self.process.stdout.close()
        self.assertFalse(self.deferred, f"Unexpected buffered responses: {self.deferred}")
        self.assertIsNone(self.responses.get_nowait(), "Unexpected stdout after the final response")

    def send(self, method, params=None, *, notification=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            self.request_id += 1
            message["id"] = self.request_id
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        return message.get("id")

    def receive(self, request_id):
        deadline = time.monotonic() + TIMEOUT
        while request_id not in self.deferred:
            try:
                line = self.responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self.fail(f"No response for request {request_id}\n{self.server_log()}")
            self.assertIsNotNone(line, self.server_log())
            reply = json.loads(line)
            self.assertEqual(reply["jsonrpc"], "2.0")
            if "id" not in reply:
                self.notifications.append(reply)
                continue
            self.assertNotIn(reply["id"], self.deferred)
            self.deferred[reply["id"]] = reply
        return self.deferred.pop(request_id)

    def send_tool(self, name, **arguments):
        return self.send("tools/call", {"name": name, "arguments": arguments})

    def tool_result(self, request_id, *, error=False):
        result = self.receive(request_id)["result"]
        self.assertIs(result["isError"], error, result)
        content, = result["content"]
        self.assertEqual(content["type"], "text")
        return json.loads(content["text"])

    def call(self, name, *, error=False, **arguments):
        return self.tool_result(self.send_tool(name, **arguments), error=error)

    def git(self, workdir, *args):
        result = subprocess.run(
            ["git", *args], cwd=workdir, env=self.env, capture_output=True, text=True, timeout=TIMEOUT
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def make_repo(self):
        workdir = self.root / "repo with spaces"
        workdir.mkdir()
        self.git(workdir, "init", "-q")
        (workdir / "tracked.txt").write_text("original\n", encoding="utf-8")
        self.git(workdir, "add", "tracked.txt")
        self.git(workdir, "-c", "user.name=Test User", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgSign=false", "commit", "-qm", "Initial commit")
        return workdir

    def await_files(self, *paths):
        deadline = time.monotonic() + TIMEOUT
        while not all(path.is_file() for path in paths):
            if time.monotonic() >= deadline:
                self.fail(f"Mock did not create {paths}\n{self.server_log()}")
            time.sleep(0.01)

    def await_meta(self, task_id, predicate):
        path = self.state / "tasks" / task_id / "meta.json"
        self.await_files(path)
        deadline = time.monotonic() + TIMEOUT
        while True:
            meta = read_json(path)
            if predicate(meta):
                return meta
            self.assertLess(time.monotonic(), deadline, f"Expected activity missing from {meta}")
            time.sleep(0.01)

    def append_rollout(self, *entries):
        rollout_dir = self.codex_home / "sessions" / "2024" / "01" / "09"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        with (rollout_dir / f"rollout-2024-01-09T11-00-{SESSION}.jsonl").open("a", encoding="utf-8") as stream:
            for entry in entries:
                stream.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")

    def monitor(self, **arguments):
        result = self.receive(self.send_tool("codex_monitor", **arguments))["result"]
        self.assertIs(result["isError"], False)
        self.assertEqual(result["content"][0]["type"], "text")
        return result["structuredContent"], result["content"][0]["text"]


class ServerTests(ServerHarness):
    def record_failed_task(self, task_id, round_num=1, **metadata):
        task = self.state / "tasks" / task_id
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({
            "task_id": task_id, "round": round_num, "status": "failed", "duration_seconds": 8.75,
            "current_activity": "failed (8.75s)", "recent_activities": [], **metadata,
        }))
        (task / f"events-r{round_num}.jsonl").touch()
        return task

    def test_historical_failure_prefers_user_facing_stderr_error(self):
        task = self.record_failed_task("historical-failure", round_num=3)
        (task / "stderr-r3.log").write_text(
            "2026-11-24T14:14:54.709056Z ERROR codex_core::session::session: failed to initialize thread "
            "persistence: thread-store conflict: thread 01a20dd4-131d-7ae2-9d6d-3ed7bb1e5268 already has an active writer\n"
            "2026-11-24T14:14:54.711884Z ERROR codex_core::session: Failed to create session: thread-store conflict: "
            "thread 01a20dd4-131d-7ae2-9d6d-3ed7bb1e5268 already has an active writer\n"
            "2026-11-24T14:14:54.857020Z ERROR codex_models_manager::manager: failed to refresh available models: "
            "request timed out\n"
            "Error: thread/resume: thread/resume failed: thread 01a20dd4-131d-7ae2-9d6d-3ed7bb1e5268 "
            "already has an active writer (code -32600)\n"
        )
        expected = {"kind": "error", "text": "thread/resume failed: thread 01a20dd4 already has an active writer (code -32600)"}
        self.assertEqual(self.monitor()[0]["tasks"][0]["latest_activity"], expected)
        self.assertEqual(self.call("codex_status", task_id=task.name)["latest_activity"], expected)
        self.assertNotIn("error_summary", read_json(task / "meta.json"))

    def test_failed_task_prefers_stderr_exception_over_command(self):
        task = self.record_failed_task(
            "traceback-failure", recent_activities=[{"kind": "cmd_done", "text": "✓ git status"}],
        )
        (task / "stderr-r1.log").write_text(
            'Traceback (most recent call last):\n  File "runner.py", line 3, in <module>\n'
            '    raise ValueError("invalid settings")\n    ^~~~~\nValueError: invalid settings\n'
            '    at launcher (index.js:5:1)\nError: Codex is ignoring a hook\n'
        )
        activity = self.monitor()[0]["tasks"][0]["latest_activity"]
        self.assertEqual(activity, {"kind": "error", "text": "ValueError: invalid settings"})

    def test_failed_task_uses_only_valid_recorded_stderr_path(self):
        task_id = "recorded-stderr"
        recorded_path = self.state / "tasks" / task_id / "legacy-stderr.log"
        self.record_failed_task(task_id, current_stderr_file=str(recorded_path))
        recorded_path.write_text(
            "\x1b[31m2026-11-24T14:14:54.709056Z ERROR codex_core::session:\x1b[0m unable to open session\n"
            "2026-11-24T14:14:55Z WARN codex_core::session: shutdown in progress\n"
        )
        self.assertEqual(self.monitor()[0]["tasks"][0]["latest_activity"],
                         {"kind": "error", "text": "unable to open session"})

        recorded_path.unlink()
        outside = self.root / "unrelated.log"
        outside.write_text("Error: unrelated failure\n")
        recorded_path.symlink_to(outside)
        self.assertEqual(self.monitor()[0]["tasks"][0]["latest_activity"],
                         {"kind": "error", "text": "failed (8.75s)"})

    def test_failed_task_without_error_reason_keeps_status_fallback(self):
        task = self.record_failed_task(
            "missing-reason",
            recent_activities=[{"kind": "error", "text": "turn.failed"},
                               {"text": "⚠ thread.started"},
                               {"kind": "error", "text": "⚠ notice about --dangerously-bypass-hook-trust"}],
        )
        for stderr_exists in (False, True):
            with self.subTest(stderr_exists=stderr_exists):
                if stderr_exists:
                    (task / "stderr-r1.log").touch()
                activity = self.call("codex_status")["tasks"][0]["latest_activity"]
                self.assertEqual(activity, {"kind": "error", "text": "failed (8.75s)"})

    def test_failed_dispatch_persists_turn_reason_and_successful_resume_clears_it(self):
        task_id = "turn-failure"
        task = self.state / "tasks" / task_id
        result = self.call("codex_dispatch", error=True, task_id=task_id, workdir=str(self.root), spec="fail-turn")
        expected = "request rejected for thread 01234567"
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(read_json(task / "meta.json")["error_summary"], expected)
        status = self.call("codex_status", task_id=task_id)
        self.assertEqual(status["error_summary"], expected)
        self.assertEqual(status["latest_activity"], {"kind": "error", "text": expected})
        self.assertIn(f"⚠ {expected}", status["recent_events"])

        result = self.call("codex_resume", task_id=task_id, feedback="recover from failure")
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(read_json(task / "meta.json")["error_summary"])
        status = self.call("codex_status", task_id=task_id)
        self.assertIsNone(status["error_summary"])
        self.assertEqual(status["latest_activity"]["kind"], "report")

    def test_failed_dispatch_persists_stderr_reason(self):
        task_id = "stderr-failure"
        result = self.call("codex_dispatch", error=True, task_id=task_id, workdir=str(self.root), spec="fail-stderr")
        self.assertEqual(result["status"], "failed")
        meta = read_json(self.state / "tasks" / task_id / "meta.json")
        self.assertEqual(meta["error_summary"], "ValueError: invalid session configuration")

    def test_launch_oserror_is_persisted_and_surfaced(self):
        Path(self.env["CODEX_BIN"]).write_text("invalid executable format\n")
        task_id = "launch-failure"
        result = self.call("codex_dispatch", error=True, task_id=task_id, workdir=str(self.root), spec="launch")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["exit_code"])
        meta = read_json(self.state / "tasks" / task_id / "meta.json")
        self.assertTrue(meta["error_summary"].startswith("[Errno 8]"), meta["error_summary"])
        self.assertEqual(self.monitor()[0]["tasks"][0]["latest_activity"],
                         {"kind": "error", "text": meta["error_summary"]})

    def test_json_rpc_protocol(self):
        initialized = self.receive(self.send("initialize", {"protocolVersion": "2025-03-26"}))
        self.assertEqual(initialized["result"]["serverInfo"], {"name": "codex-castor", "version": "1.1.0"})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(initialized["result"]["capabilities"], {
            "tools": {"listChanged": False}, "resources": {"listChanged": False}, "logging": {},
        })
        self.send("notifications/initialized", notification=True)
        self.assertEqual(self.receive(self.send("ping"))["result"], {})
        tools = self.receive(self.send("tools/list"))["result"]["tools"]
        self.assertEqual(len(tools), len(TOOL_NAMES))
        self.assertEqual({tool["name"] for tool in tools}, TOOL_NAMES)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertEqual(self.receive(self.send("no/such/method"))["error"]["code"], -32601)
        self.process.stdin.write(b"{bad json\n")
        self.process.stdin.flush()
        self.assertEqual(self.receive(None)["error"]["code"], -32700)
        unknown = self.receive(self.send_tool("unknown_tool"))
        self.assertEqual(unknown["error"]["code"], -32602)
        invalid = self.receive(self.send("tools/call", {
            "name": "codex_monitor", "_meta": {"progressToken": True},
        }))
        self.assertEqual(invalid["error"]["code"], -32602)

    def test_mcp_apps_resource_and_tool_discovery(self):
        extension = "io.modelcontextprotocol/ui"
        initialized = self.receive(self.send("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"extensions": {extension: {"mimeTypes": [MONITOR_MIME]}}},
        }))
        self.assertEqual(initialized["result"]["capabilities"]["extensions"], {extension: {}})
        tools = {tool["name"]: tool for tool in self.receive(self.send("tools/list"))["result"]["tools"]}
        monitor_meta = tools["codex_monitor"]["_meta"]
        self.assertEqual(tools["codex_monitor"]["inputSchema"]["properties"]["refresh_quota"],
                         {"type": "boolean", "default": False})
        self.assertEqual(monitor_meta["ui"], {"resourceUri": MONITOR_URI, "visibility": ["model", "app"]})
        self.assertEqual(monitor_meta["com.google.antigravity/ui"]["entrypoints"], [
            {"type": "aux_pane", "title": "Codex Monitor"},
            {"type": "global", "title": "Codex Monitor"},
        ])
        for name in ("codex_status", "codex_cancel"):
            self.assertEqual(tools[name]["_meta"]["ui"]["visibility"], ["model", "app"])
        resource, = self.receive(self.send("resources/list"))["result"]["resources"]
        self.assertEqual(resource["uri"], MONITOR_URI)
        self.assertEqual(resource["mimeType"], MONITOR_MIME)
        failure = self.receive(self.send("resources/read", {"uri": "ui://codex-castor/missing"}))
        self.assertEqual(failure["error"]["code"], -32002)

        def read_monitor():
            response = self.receive(self.send("resources/read", {"uri": MONITOR_URI}))
            return response["result"]["contents"][0]

        self.assertTrue(read_monitor()["text"].lower().startswith("<!doctype html>"))
        for message in ("Monitor fixture one", "Monitor fixture two"):
            html = f"<!doctype html><title>Monitor</title><p>{message}</p>"
            self.monitor_html.write_text(html, encoding="utf-8")
            self.assertEqual(read_monitor(), {
                "uri": MONITOR_URI, "mimeType": MONITOR_MIME, "text": html,
                "_meta": {"ui": {"prefersBorder": False}},
            })
        payload, contents = self.monitor()
        self.assertEqual(payload["active_count"], 0)
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["tasks"], [])
        self.assertIsNone(payload["selected_task"])
        self.assertIn("0 active", contents)

    def test_dispatch_refreshes_quota_and_monitor_reads_the_cache(self):
        self.mock_quota.write_text(json.dumps(WEEKLY_QUOTA), encoding="utf-8")
        task = self.call("codex_dispatch", workdir=str(self.root), spec="check quota")
        self.assertEqual(task["status"], "completed")
        self.await_files(self.quota_cache)
        cached = read_json(self.quota_cache)
        self.assertEqual(cached["windows"][0]["remaining_percent"], 22)
        invocations = self.quota_calls.read_text().splitlines()
        self.assertEqual(len(invocations), 1)

        for _ in range(3):
            snapshot, summary = self.monitor()
            self.assertEqual(snapshot["quota"], cached)
            self.assertEqual(summary.splitlines()[0], "Codex Monitor: 0 active, 1 total")
        self.assertEqual(self.quota_calls.read_text().splitlines(), invocations)

    def test_monitor_refreshes_quota_synchronously_and_preserves_cache_on_error(self):
        self.mock_quota.write_text(json.dumps(WEEKLY_QUOTA), encoding="utf-8")
        first, _ = self.monitor(limit=0, refresh_quota=True)
        self.assertEqual(first["quota"], read_json(self.quota_cache))
        self.assertEqual([window["label"] for window in first["quota"]["windows"]], ["7d"])

        self.mock_quota.write_text(json.dumps(DUAL_QUOTA), encoding="utf-8")
        second, _ = self.monitor(limit=0, refresh_quota=True)
        self.assertEqual(second["quota"], read_json(self.quota_cache))
        self.assertEqual([window["label"] for window in second["quota"]["windows"]], ["5h", "7d"])

        (self.sync / "quota-fail").touch()
        fallback, _ = self.monitor(limit=0, refresh_quota=True)
        self.assertEqual(fallback["quota"], second["quota"])
        self.assertEqual(read_json(self.quota_cache), second["quota"])
        self.assertEqual(len(self.quota_calls.read_text().splitlines()), 3)

    def test_missing_quota_is_bootstrapped_once(self):
        (self.sync / "quota-fail").touch()
        self.assertIsNone(self.monitor()[0]["quota"])
        self.await_files(self.quota_calls)
        deadline = time.monotonic() + TIMEOUT
        while "Could not refresh Codex quota:" not in self.server_log():
            self.assertLess(time.monotonic(), deadline, "Background quota query did not finish")
            time.sleep(0.01)
        for _ in range(3):
            self.assertIsNone(self.monitor()[0]["quota"])
        self.assertFalse(self.quota_cache.exists())
        self.assertEqual(len(self.quota_calls.read_text().splitlines()), 1)

    def test_quota_query_times_out_on_partial_reply_and_reaps_process(self):
        server = load_server()
        start = time.monotonic()
        with mock.patch.dict(os.environ, {**self.env, "MOCK_CODEX_QUOTA_STALL": "1"}):
            with self.assertRaisesRegex(TimeoutError, "quota query timed out"):
                server.query_codex_quota(timeout=1)
        self.assertLess(time.monotonic() - start, 4)
        self.assertTrue((self.sync / "quota-partial-response").is_file())
        self.assertFalse(server.is_pid_alive(int(self.quota_calls.read_text())))

    def test_monitor_on_demand_multi_round_detail(self):
        task = self.state / "tasks" / "review"
        task.mkdir(parents=True)
        prompt = "# Review the ledger\n## Objective\nKeep every round visible."
        (task / "spec.md").write_text(prompt)
        (task / "feedback-r2.md").write_text("Include rollback.")
        (task / "meta.json").write_text(json.dumps({
            "task_id": "review", "session_id": SESSION, "status": "completed", "round": 2,
            "exit_code": 0, "duration_seconds": 2, "effective_workdir": str(self.root), "model": "model-a",
        }))
        report = {"status": "completed", "summary": "Both rounds verified.", "files_changed": ["schema.py"],
                  "commands_run": ["pwd"], "tests_summary": "Passed", "open_issues": []}
        (task / "report-r2.json").write_text(json.dumps(report))
        cmd = {"type": "command_execution", "id": "item1", "command": "/bin/zsh -lc 'pwd'"}
        first = [{"type": "thread.started"}, {"type": "turn.started"},
                 {"type": "item.started", "timestamp": "2025-01-01T00:00:00Z", "item": cmd},
                 {"type": "item.completed", "item": {**cmd, "exit_code": 0, "aggregated_output": "project"}},
                 {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}]
        second = [{"type": "error", "message": "Noisy --dangerously-bypass-hook-trust warning"},
                  {"type": "item.completed", "item": {"type": "file_change", "id": "item1",
                   "changes": [{"path": str(self.root / "schema.py"), "kind": "add"}]}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}},
                  {"type": "turn.completed", "usage": {"input_tokens": 60, "output_tokens": 3}}]
        for n, events in enumerate([first, second], 1):
            (task / f"events-r{n}.jsonl").write_text("\n".join(map(json.dumps, events)))
        default, _ = self.monitor()
        self.assertNotIn("task_detail", default)
        snapshot, _ = self.monitor(task_id="review", include_detail=False)
        summary = snapshot["selected_task"]
        self.assertEqual(summary, default["tasks"][0])
        self.assertEqual(summary["task_title"], "Review the ledger")
        self.assertEqual(summary["prompt_preview"], "Keep every round visible.")
        self.assertEqual(summary["latest_activity"], {"kind": "report", "text": report["summary"]})
        self.assertNotIn("task_detail", snapshot)
        snapshot, _ = self.monitor(task_id="review", include_detail=True, limit=0)
        self.assertEqual(snapshot["selected_task"], summary)
        detail = snapshot["task_detail"]
        self.assertEqual(detail["config"]["session_id"], SESSION)
        self.assertEqual(detail["config"]["effective_model"], "model-a")
        self.assertEqual(detail["config"]["rework_count"], 1)
        self.assertEqual(detail["runtime"]["usage"]["total_tokens"], 168)
        self.assertEqual(detail["runtime"]["event_count"], len(first) + len(second))
        self.assertEqual([p["text"] for p in detail["prompts"]], [prompt, "Include rollback."])
        self.assertEqual([r["prompt_file"] for r in detail["runtime"]["rounds"]], ["spec.md", "feedback-r2.md"])
        messages = detail["messages"]
        self.assertEqual([m["kind"] for m in messages],
                         ["round_start", "command", "turn_status", "round_start", "file_change", "report", "turn_status"])
        self.assertEqual(messages[1]["meta"]["command"], "pwd")
        self.assertEqual(messages[1]["body"], "project")
        self.assertEqual(messages[1]["ts"], first[2]["timestamp"])
        self.assertEqual(messages[4]["meta"]["files"], [{"path": "schema.py", "kind": "add"}])
        self.assertEqual(messages[5]["meta"], report)
        self.assertFalse(detail["messages_truncated"])

    def test_monitor_project_fallback_and_selected_list_limits(self):
        for index in range(22):
            task = self.state / "tasks" / f"session-{index:02}"
            task.mkdir(parents=True)
            meta = {"task_id": task.name, "status": "completed", "effective_workdir": "/work/Atlas",
                    "updated_at": f"2026-01-{index + 1:02}T12:00:00Z"}
            if index == 0:
                meta.update(effective_workdir="/worktrees/session-00", worktree={"repo_root": "/work/HikariDaily"})
            (task / "meta.json").write_text(json.dumps(meta))
        default, _ = self.monitor()
        self.assertEqual(len(default["tasks"]), 20)
        self.assertEqual(default["tasks"][0]["project"], "Atlas")
        home, _ = self.monitor(limit=200)
        self.assertEqual(len(home["tasks"]), 22)
        selected, _ = self.monitor(task_id="session-00", limit=1)
        self.assertEqual(selected["tasks"], [selected["selected_task"]])
        self.assertEqual(selected["selected_task"]["project"], "HikariDaily")
        self.assertIsNone(selected["selected_task"]["requested_workdir"])
        detail, _ = self.monitor(task_id="session-00", limit=0, include_detail=True)
        self.assertEqual(detail["tasks"], [])
        self.assertEqual(detail["task_detail"]["config"]["project"], "HikariDaily")

    def test_monitor_sorts_settled_tasks_by_finished_at_across_feedback(self):
        older = self.state / "tasks" / "older"
        newer = self.state / "tasks" / "newer"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        older_meta = {
            "task_id": "older", "status": "completed", "effective_workdir": "/work/Atlas",
            "created_at": "2026-01-01T10:00:00Z", "updated_at": "2026-01-03T12:00:00Z",
            "finished_at": "2026-01-01T12:00:00Z",
        }
        newer_meta = {
            "task_id": "newer", "status": "completed", "effective_workdir": "/work/Atlas",
            "created_at": "2026-01-02T10:00:00Z", "updated_at": "2026-01-02T12:00:00Z",
            "finished_at": "2026-01-02T12:00:00Z",
        }
        (older / "meta.json").write_text(json.dumps(older_meta))
        (newer / "meta.json").write_text(json.dumps(newer_meta))
        feedback = self.receive(self.send_tool("codex_feedback", task_id="older", rating=5, verdict="accepted"))["result"]
        self.assertIs(feedback["isError"], False)
        updated_older = json.loads((older / "meta.json").read_text())
        self.assertEqual(updated_older["updated_at"], "2026-01-03T12:00:00Z")
        snapshot, _ = self.monitor(limit=10)
        self.assertEqual([task["task_id"] for task in snapshot["tasks"]], ["newer", "older"])

    def test_monitor_detail_limits_stream_and_prompt_bytes(self):
        task = self.state / "tasks" / "large"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"task_id": "large", "status": "failed"}))
        (task / "spec.md").write_text("界" * 120001)
        (task / "stderr-r1.log").write_text("\n".join(str(i) for i in range(60)))
        with (task / "events-r1.jsonl").open("w") as f:
            for i in range(260):
                f.write(json.dumps({"type": "item.completed", "item": {"id": str(i), "type": "agent_message",
                                       "text": f"Message {i}: " + "界" * 4000}}) + "\n")
        snapshot, content = self.monitor(task_id="large", include_detail=True)
        detail = snapshot["task_detail"]
        self.assertTrue(detail["messages_truncated"])
        self.assertTrue(detail["prompts"][0]["truncated"])
        self.assertEqual(detail["total_stream_items"], 260)
        self.assertEqual(len(detail["messages"]), 250)
        self.assertTrue(detail["messages"][0]["body"].startswith("Message 10:"))
        self.assertTrue(detail["messages"][-1]["body"].startswith("Message 259:"))
        self.assertEqual(detail["stderr_tail"].splitlines()[0], "20")
        self.assertLess(len(json.dumps(snapshot, ensure_ascii=False).encode()) + len(content.encode()), 1024**2)

    def test_dispatch_in_direct_workdir_and_resume_by_task_id(self):
        workdir = self.make_repo()
        task = self.state / "tasks" / "direct"
        first = self.call("codex_dispatch", task_id="direct", workdir=str(workdir), spec="implement",
                          model="mock-model", reasoning_effort="medium", extra_writable_dirs=[str(self.root)],
                          title="  Verify direct dispatch  ")
        self.assertEqual(read_json(task / "meta.json")["title"], "Verify direct dispatch")
        self.assertEqual(self.monitor(task_id="direct")[0]["selected_task"]["task_title"], "Verify direct dispatch")
        self.assertEqual(first["workdir"], str(workdir))
        self.assertFalse(first["worktree"]["enabled"])
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["round"], 1)
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual(first["session_id"], SESSION)
        self.assertEqual(first["usage"], USAGE)
        self.assertTrue(first["git"]["is_git_repo"])
        self.assertIn("tracked.txt", first["git"]["diff_stat"])
        self.assertEqual(first["git"]["status_short"], "M tracked.txt")
        self.assertEqual(Path(first["artifacts"]["prompt_file"]), task / "spec.md")
        self.assertEqual((task / "spec.md").read_text(encoding="utf-8"), "implement")
        self.assertEqual(Path(first["artifacts"]["report_file"]), task / "report-r1.json")
        self.assertEqual(first["report"], read_json(task / "report-r1.json"))
        events = [json.loads(line) for line in (task / "events-r1.jsonl").read_text().splitlines()]
        self.assertEqual([event["type"] for event in events], [
            "thread.started", "item.completed", "turn.completed",
        ])
        self.assertEqual(events[0]["thread_id"], SESSION)
        self.assertEqual(events[-1]["usage"], USAGE)
        invocation = read_json(task / "mock-report-r1.json")
        self.assertEqual(invocation["prompt"], "implement")
        self.assertEqual(invocation["args"], [
            "exec", "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
            "--skip-git-repo-check", "-C", str(workdir), "--output-schema",
            str(SERVER.with_name("report.schema.json")), "-o", str(task / "report-r1.json"),
            "--json", "-m", "mock-model", "-c", 'model_reasoning_effort="medium"',
            "--add-dir", str(self.root), "-",
        ])

        second = self.call("codex_resume", task_id="direct", feedback="review feedback")
        self.assertEqual(second["round"], 2)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["session_id"], SESSION)
        self.assertEqual(second["workdir"], str(workdir))
        self.assertEqual(second["report"], read_json(task / "report-r2.json"))
        self.assertEqual(second["report"]["summary"], "Handled review feedback")
        self.assertEqual((task / "feedback-r2.md").read_text(encoding="utf-8"), "review feedback")
        invocation = read_json(task / "mock-report-r2.json")
        self.assertEqual(invocation["cwd"], str(workdir))
        self.assertEqual(invocation["prompt"], "review feedback")
        self.assertEqual(invocation["args"][:2], ["exec", "resume"])
        self.assertEqual(invocation["args"][-2:], [SESSION, "-"])

        status = self.call("codex_status", task_id="direct")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["round"], 2)
        self.assertFalse(status["alive"])
        self.assertEqual(status["recent_events"][-1], "turn.completed")
        listed = self.call("codex_status")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["tasks"][0]["task_id"], "direct")
        self.assertEqual(listed["tasks"][0]["status"], "completed")

    def test_model_resolution_user_project_override_and_resume_inheritance(self):
        config = self.codex_home / "config.toml"
        config.write_text('model = "user-model"\nmodel_reasoning_effort = "medium"\n', encoding="utf-8")
        workdir = self.make_repo()
        subdir = workdir / "nested"
        subdir.mkdir()

        def dispatch(task_id, **overrides):
            return self.call("codex_dispatch", task_id=task_id, workdir=str(subdir), spec="implement", **overrides)

        first = dispatch("user-defaults")
        self.assertEqual((first["effective_model"], first["model_source"]), ("user-model", "user_config"))
        self.assertEqual((first["effective_reasoning_effort"], first["reasoning_effort_source"]), ("medium", "user_config"))

        project = workdir / ".codex" / "config.toml"
        project.parent.mkdir()
        project.write_text('model = "project-model"\n[profiles.other]\nmodel_reasoning_effort = "low"\n',
                           encoding="utf-8")
        result = dispatch("project-defaults")
        self.assertEqual((result["effective_model"], result["model_source"]), ("project-model", "project_config"))
        self.assertEqual((result["effective_reasoning_effort"], result["reasoning_effort_source"]), ("medium", "user_config"))

        project.write_text('model = "changed"\nmodel_reasoning_effort = "low"\n[invalid\n', encoding="utf-8")
        inherited = self.call("codex_resume", task_id="project-defaults", feedback="review")
        self.assertEqual((inherited["effective_model"], inherited["model_source"]), ("project-model", "previous_round"))
        self.assertEqual((inherited["effective_reasoning_effort"], inherited["reasoning_effort_source"]),
                         ("medium", "previous_round"))
        self.assertEqual((inherited["config_model"], inherited["config_reasoning_effort"]), ("changed", "low"))

        result = dispatch("overridden", model="round-one", reasoning_effort="high")
        self.assertEqual((result["effective_model"], result["model_source"]), ("round-one", "override"))
        self.assertEqual((result["effective_reasoning_effort"], result["reasoning_effort_source"]), ("high", "override"))
        self.assertEqual((result["config_model"], result["config_reasoning_effort"]), ("changed", "low"))
        self.append_rollout({"type": "turn_context", "payload": {"model": "earlier-runtime", "effort": "medium"}})
        self.append_rollout({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 999},
        }}})
        second = self.call("codex_resume", task_id="overridden", feedback="review", model="round-two",
                           reasoning_effort="xhigh")
        self.assertEqual((second["effective_model"], second["model_source"]), ("round-two", "override"))
        self.assertEqual((second["effective_reasoning_effort"], second["reasoning_effort_source"]), ("xhigh", "override"))
        task_dir = self.state / "tasks" / "overridden"
        meta = read_json(task_dir / "meta.json")
        self.assertEqual((meta["model"], meta["reasoning_effort"]), ("round-two", "xhigh"))
        self.assertEqual(read_json(task_dir / "mock-report-r2.json")["args"][-6:],
                         ["-m", "round-two", "-c", 'model_reasoning_effort="xhigh"', SESSION, "-"])
        third = self.call("codex_resume", task_id="overridden", feedback="follow up")
        self.assertEqual((third["effective_model"], third["model_source"]), ("round-two", "override"))
        self.assertEqual((third["effective_reasoning_effort"], third["reasoning_effort_source"]), ("xhigh", "override"))

    def test_live_rollout_and_activity_are_persisted_and_shared_with_monitor(self):
        (self.codex_home / "config.toml").write_text(
            'model = "configured"\nmodel_reasoning_effort = "low"\n', encoding="utf-8"
        )
        self.append_rollout(
            {"type": "turn_context", "payload": {"model": "old", "effort": "low"}},
            {"type": "turn_context", "payload": {"model": "runtime", "collaboration_mode": {
                "settings": {"model": "secondary", "reasoning_effort": "high"},
            }}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 18, "total_tokens": 27, "not_standard": 4, "output_tokens": True},
            }}},
            {"type": "unrelated", "payload": " " * 70000},  # Exercise a rollout record spanning read chunks.
            '{"type": "turn_context"',  # A partial record does not replace the latest complete entry.
        )
        task_id = "live"
        pending = self.send_tool("codex_dispatch", task_id=task_id, workdir=str(self.root),
                                 spec="observe", model="requested", reasoning_effort="medium")
        meta = self.await_meta(task_id, lambda item: item["current_activity"] == "▶ printf hello")
        self.assertTrue(meta["round_started_at"])
        self.assertEqual((meta["effective_model"], meta["model_source"]), ("runtime", "runtime_rollout"))
        self.assertEqual((meta["effective_reasoning_effort"], meta["reasoning_effort_source"]), ("high", "runtime_rollout"))
        self.assertEqual((meta["config_model"], meta["config_reasoning_effort"]), ("configured", "low"))
        self.assertEqual(meta["usage"], {"input_tokens": 18, "total_tokens": 27})
        self.assertEqual(meta["recent_activities"][-1]["kind"], "cmd_start")

        self.append_rollout({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"cached_input_tokens": 7, "total_tokens": 41},
        }}})
        single = self.call("codex_status", task_id=task_id)
        listed, = self.call("codex_status")["tasks"]
        snapshot, text = self.monitor(task_id=task_id, limit=0)
        self.assertEqual(snapshot["active_count"], 1)
        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(snapshot["tasks"], [])
        self.assertIn("[running] live (runtime·high)", text)
        for view in (single, listed, snapshot["selected_task"]):
            self.assertTrue(view["alive"])
            self.assertGreaterEqual(view["elapsed_seconds"], 0)
            self.assertEqual(view["current_activity"], "▶ printf hello")
            self.assertEqual(view["recent_activities"], meta["recent_activities"])
            self.assertEqual(view["usage"], {"cached_input_tokens": 7, "total_tokens": 41})
        # The worker also updates the on-disk snapshot when Codex stdout is idle.
        self.await_meta(task_id, lambda item: item["usage"].get("total_tokens") == 41)

        (self.sync / "show-file").touch()
        meta = self.await_meta(task_id, lambda item: item["current_activity"] == "✎ editing main.py")
        self.assertEqual(meta["recent_activities"][-1]["kind"], "file")
        (self.sync / "show-message").touch()
        meta = self.await_meta(task_id, lambda item: item["current_activity"] == "💬 Finished the live edit.")
        self.assertEqual(meta["recent_activities"][-1]["kind"], "message")
        (self.sync / "finish").touch()
        result = self.tool_result(pending)
        self.assertEqual(result["effective_model"], "runtime")
        self.assertEqual(result["effective_reasoning_effort"], "high")
        self.assertEqual(result["usage"], USAGE)
        finished, _ = self.monitor(task_id=task_id)
        self.assertEqual(finished["active_count"], 0)
        self.assertEqual(finished["selected_task"], finished["tasks"][0])
        self.assertTrue(finished["selected_task"]["current_activity"].startswith("completed ("))

    def test_resume_keeps_runtime_values_until_new_rollout_and_returns_rollout_usage(self):
        self.append_rollout({"type": "turn_context", "payload": {"model": "initial-runtime", "effort": "high"}})
        first = self.call("codex_dispatch", task_id="resumed", workdir=str(self.root), spec="implement")
        self.assertEqual(first["model_source"], "runtime_rollout")
        pending = self.send_tool("codex_resume", task_id="resumed", feedback="observe-rollout-only",
                                 reasoning_effort="xhigh")
        meta = self.await_meta("resumed", lambda item: item["round"] == 2 and item["last_event"] == "turn.started")
        self.assertIsNone(meta["finished_at"])
        self.assertIsNone(meta["usage"])
        self.assertEqual((meta["effective_model"], meta["model_source"]), ("initial-runtime", "runtime_rollout"))
        self.assertEqual((meta["effective_reasoning_effort"], meta["reasoning_effort_source"]), ("xhigh", "override"))
        self.append_rollout(
            {"type": "turn_context", "payload": {"model": "new-runtime", "effort": "low"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 24, "reasoning_output_tokens": 8, "total_tokens": 44},
            }}},
        )
        (self.sync / "finish").touch()
        second = self.tool_result(pending)
        self.assertEqual((second["effective_model"], second["model_source"]), ("new-runtime", "runtime_rollout"))
        self.assertEqual((second["effective_reasoning_effort"], second["reasoning_effort_source"]), ("low", "runtime_rollout"))
        self.assertEqual(second["usage"], {"input_tokens": 24, "reasoning_output_tokens": 8, "total_tokens": 44})

    def test_progress_and_logging_are_opt_in_and_progress_is_increasing(self):
        initial = self.call("codex_dispatch", task_id="quiet", workdir=str(self.root), spec="implement")
        self.assertEqual(initial["model_source"], "unknown")
        self.assertEqual(self.notifications, [])
        arguments = {
            "name": "codex_dispatch", "arguments": {
                "task_id": "progress", "workdir": str(self.root), "spec": "implement",
                "model": "tag-model", "reasoning_effort": "high",
            },
            "_meta": {"progressToken": 0},
        }
        self.tool_result(self.send("tools/call", arguments))
        progress = self.notifications[:]
        self.assertTrue(progress)
        self.assertTrue(all(note["method"] == "notifications/progress" for note in progress))
        params = [note["params"] for note in progress]
        self.assertTrue(all(p["progressToken"] == 0 for p in params))
        self.assertTrue(all(p["message"].startswith("[progress · tag-model·high]") for p in params))
        self.assertEqual([p["progress"] for p in params], sorted({p["progress"] for p in params}))
        self.assertTrue(params[0]["message"].endswith("launching"))
        self.assertTrue(params[-1]["message"].startswith("[progress · tag-model·high] completed ("))
        self.notifications.clear()

        invalid = self.receive(self.send("logging/setLevel", {"level": "verbose"}))
        self.assertEqual(invalid["error"]["code"], -32602)
        self.assertEqual(self.receive(self.send("logging/setLevel", {"level": "warning"}))["result"], {})
        self.call("codex_resume", task_id="progress", feedback="review")
        self.assertEqual(self.notifications, [])

        self.assertEqual(self.receive(self.send("logging/setLevel", {"level": "debug"}))["result"], {})
        self.call("codex_resume", task_id="progress", feedback="review")
        self.assertTrue(self.notifications)
        for note in self.notifications:
            self.assertEqual(note["method"], "notifications/message")
            self.assertEqual(note["params"]["level"], "info")
            self.assertEqual(note["params"]["logger"], "codex-castor")
            self.assertTrue(note["params"]["data"].startswith("[progress · tag-model·high]"))

        self.notifications.clear()
        arguments = {
            "name": "codex_resume",
            "arguments": {"task_id": "progress", "feedback": "activity-burst"},
            "_meta": {"progressToken": "resume-progress"},
        }
        self.tool_result(self.send("tools/call", arguments))
        resumed = [note["params"] for note in self.notifications if note["method"] == "notifications/progress"]
        self.assertTrue(all(note["progressToken"] == "resume-progress" for note in resumed))
        self.assertEqual(len(resumed), 18)  # Launch, 16 meaningful events, and finish.
        activity = self.call("codex_status", task_id="progress")["recent_activities"]
        self.assertEqual(len(activity), 12)
        self.assertEqual((activity[-3]["kind"], activity[-3]["text"]), ("cmd_done", "✗(rc=1) git status"))
        self.assertNotIn("hook-trust", str(activity))

    def test_managed_worktree_isolation_and_cleanup(self):
        workdir = self.make_repo()
        result = self.call("codex_dispatch", task_id="isolated", workdir=str(workdir),
                           spec="worktree edit", use_worktree=True)
        worktree = self.state / "worktrees" / "isolated"
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["worktree"]["enabled"])
        self.assertEqual(result["worktree"]["path"], str(worktree))
        summary = self.monitor(task_id="isolated")[0]["selected_task"]
        self.assertEqual(summary["project"], workdir.name)
        self.assertEqual(summary["requested_workdir"], str(workdir))
        self.assertEqual(summary["workdir"], str(worktree))
        self.assertEqual(result["worktree"]["branch"], "codex/isolated")
        self.assertEqual(self.git(worktree, "branch", "--show-current"), "codex/isolated")
        self.assertEqual(result["git"]["branch"], "codex/isolated")
        self.assertIn("tracked.txt", result["git"]["diff_stat"])
        self.assertEqual((worktree / "tracked.txt").read_text(), "original\nworktree edit\n")
        self.assertEqual((workdir / "tracked.txt").read_text(), "original\n")
        self.assertEqual(self.git(workdir, "status", "--porcelain"), "")

        cleanup = self.call("codex_cleanup_worktree", task_id="isolated", delete_branch=True, force=True)
        self.assertTrue(cleanup["cleaned"])
        self.assertTrue(cleanup["branch_deleted"])
        self.assertFalse(worktree.exists())
        self.assertEqual(self.git(workdir, "branch", "--list", "codex/isolated"), "")

    def test_parallel_dispatches_share_stdio_and_keep_worktrees_separate(self):
        workdir = self.make_repo()
        pending = {
            slug: self.send_tool("codex_dispatch", task_id=slug, workdir=str(workdir),
                                 spec=slug, use_worktree=True)
            for slug in ("parallel-one", "parallel-two")
        }
        # Each process reaches the gate before either can finish.
        self.await_files(*(self.sync / slug for slug in pending))
        (self.sync / "release").touch()
        for slug, request_id in pending.items():
            result = self.tool_result(request_id)
            worktree = self.state / "worktrees" / slug
            self.assertEqual(result["task_id"], slug)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["workdir"], str(worktree))
            self.assertEqual(result["git"]["branch"], f"codex/{slug}")
            self.assertEqual((worktree / "tracked.txt").read_text(), f"original\n{slug}\n")
        self.assertEqual(self.git(workdir, "status", "--porcelain"), "")

    def test_stale_pid_is_interrupted_in_single_and_list_status(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.assertEqual(child.wait(timeout=TIMEOUT), 0)
        task_dir = self.state / "tasks" / "stale"
        task_dir.mkdir(parents=True, exist_ok=True)
        meta = {"task_id": "stale", "status": "running", "pid": child.pid}
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        single = self.call("codex_status", task_id="stale")
        tasks = self.call("codex_status", limit=1)["tasks"]
        listed, = tasks
        for status in (single, listed):
            self.assertEqual(status["status"], "interrupted")
            self.assertFalse(status["alive"])
        self.assertEqual(read_json(task_dir / "meta.json")["status"], "running")

    def test_cancel_by_tool_and_rpc_notification(self):
        for method in ("tool", "notification"):
            with self.subTest(method=method):
                request_id = self.send_tool("codex_dispatch", task_id=method, workdir=str(self.root), spec="slow")
                invocation_file = self.state / "tasks" / method / "mock-report-r1.json"
                # The session event is flushed in the fixture and persisted by the server after activation.
                events_file = invocation_file.with_name("events-r1.jsonl")
                self.await_files(invocation_file, events_file)
                deadline = time.monotonic() + TIMEOUT
                while not events_file.stat().st_size:
                    self.assertLess(time.monotonic(), deadline, "mock never emitted its first event")
                    time.sleep(0.01)
                status = self.call("codex_status", task_id=method)
                self.assertEqual(status["status"], "running")
                self.assertTrue(status["alive"])
                started = time.monotonic()
                if method == "tool":
                    self.assertTrue(self.call("codex_cancel", task_id=method)["cancelled"])
                else:
                    self.send("notifications/cancelled", {"requestId": request_id}, notification=True)
                result = self.tool_result(request_id)
                self.assertEqual(result["status"], "cancelled")
                self.assertLess(result["exit_code"], 0)
                self.assertLess(time.monotonic() - started, TIMEOUT)
                with self.assertRaises(ProcessLookupError):
                    os.kill(read_json(invocation_file)["pid"], 0)
                status = self.call("codex_status", task_id=method)
                self.assertEqual(status["status"], "cancelled")
                self.assertFalse(status["alive"])

    def test_dispatch_validation_errors(self):
        cases = [
            ({"workdir": str(self.root / "missing"), "spec": "spec"}, "workdir does not exist"),
            ({"workdir": str(self.root)}, "'spec' (non-empty string) or 'spec_file'"),
            ({"workdir": str(self.root), "spec_file": str(self.root / "missing.md")}, "spec_file does not exist"),
            ({"workdir": str(self.root), "spec": "spec", "use_worktree": True}, "requires a Git repository"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                self.assertIn(message, self.call("codex_dispatch", error=True, **arguments)["error"])

        spec_file = self.root / "user-spec.md"
        spec_file.write_text("from an existing file", encoding="utf-8")
        arguments = {"workdir": str(self.root), "spec_file": str(spec_file), "task_id": "duplicate"}
        first = self.call("codex_dispatch", **arguments)
        self.assertEqual(first["report"]["summary"], "Handled from an existing file")
        self.assertIn("already exists", self.call("codex_dispatch", error=True, **arguments)["error"])


class QuotaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def test_window_labels(self):
        for minutes, label in [(10080, "7d"), (1440, "1d"), (300, "5h"), (45, "45m"),
                               (None, "secondary"), (0, "secondary"), (-1, "secondary"),
                               (True, "secondary"), (300.0, "secondary")]:
            with self.subTest(minutes=minutes):
                self.assertEqual(self.server.format_quota_window_label(minutes, "secondary"), label)

    def test_normalizes_weekly_only_window(self):
        updated_at = "2026-11-28T10:00:00Z"
        self.assertEqual(self.server.normalize_quota_payload(WEEKLY_QUOTA, updated_at=updated_at), {
            "updated_at": updated_at, "limit_id": "codex", "plan_type": "prolite", "windows": [{
                "slot": "primary", "label": "7d", "used_percent": 78, "remaining_percent": 22,
                "window_duration_mins": 10080, "resets_at": 1796148683,
            }],
        })

    def test_sorts_dual_windows_by_duration(self):
        windows = self.server.normalize_quota_payload(DUAL_QUOTA)["windows"]
        self.assertEqual([(w["slot"], w["label"], w["remaining_percent"]) for w in windows],
                         [("secondary", "5h", 84), ("primary", "7d", 22)])

    def test_selects_codex_bucket_then_first_valid_bucket_then_fallback(self):
        by_limit_id = {"other": {"primary": {"usedPercent": 80}}, "codex": WEEKLY_QUOTA["rateLimits"]}
        payload = {**DUAL_QUOTA, "rateLimitsByLimitId": by_limit_id}
        self.assertEqual(self.server.normalize_quota_payload(payload)["plan_type"], "prolite")
        by_limit_id["codex"] = {"primary": {"usedPercent": True}}
        alternative = self.server.normalize_quota_payload(payload)
        self.assertEqual(alternative["limit_id"], "other")
        self.assertEqual(alternative["windows"][0]["remaining_percent"], 20)
        by_limit_id["other"] = None
        self.assertEqual(self.server.normalize_quota_payload(payload)["plan_type"], "pro")

    def test_validates_window_numbers_and_optional_fields(self):
        payload = {"rateLimits": {
            "limitId": " ", "planType": 123,
            "primary": {"usedPercent": -12, "windowDurationMins": True, "resetsAt": False},
            "secondary": {"usedPercent": 25.35, "windowDurationMins": 60, "resetsAt": 123.8},
        }}
        quota = self.server.normalize_quota_payload(payload)
        self.assertEqual(quota["limit_id"], "codex")
        self.assertIsNone(quota["plan_type"])
        self.assertEqual(quota["windows"], [
            {"slot": "secondary", "label": "1h", "used_percent": 25.35, "remaining_percent": 74.7,
             "window_duration_mins": 60, "resets_at": 123},
            {"slot": "primary", "label": "primary", "used_percent": 0, "remaining_percent": 100,
             "window_duration_mins": None, "resets_at": None},
        ])
        payload["rateLimits"]["primary"]["usedPercent"] = 103
        primary = self.server.normalize_quota_payload(payload)["windows"][-1]
        self.assertEqual((primary["used_percent"], primary["remaining_percent"]), (100, 0))

    def test_missing_or_invalid_payload_returns_none(self):
        for payload in (None, [], {}, {"rateLimits": "unavailable"},
                        {"rateLimits": {"primary": [], "secondary": {"usedPercent": "78"}}},
                        {"rateLimits": {"primary": {"usedPercent": True}}},
                        {"rateLimits": {"primary": {"usedPercent": float("nan")}}}):
            with self.subTest(payload=payload):
                self.assertIsNone(self.server.normalize_quota_payload(payload))

    def test_cache_path_uses_current_state_root(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(self.server, "STATE_ROOT", Path(directory)):
            cache_path = Path(directory) / "quota.json"
            self.assertEqual(self.server.quota_cache_path(), cache_path)
            cache_path.write_text('{"windows": []}', encoding="utf-8")
            self.assertIsNone(self.server.load_cached_quota())
            normalized = self.server.normalize_quota_payload(WEEKLY_QUOTA)
            cache_path.write_text(json.dumps(normalized), encoding="utf-8")
            self.assertEqual(self.server.load_cached_quota(), normalized)


class FailureSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def test_clean_error_summary_formats_and_truncates(self):
        message = "\x1b[31mError: error: fatal: Thread/Resume: thread/resume failed:\x1b[0m\n  " + SESSION
        self.assertEqual(self.server.clean_error_summary(message), "thread/resume failed: 01234567")
        self.assertEqual(self.server.clean_error_summary(message, 14), "thread/resume…")

    def test_turn_failed_activity_uses_event_message(self):
        event = {"type": "turn.failed", "error": {"message": "Error: connection lost"}}
        kind, text = self.server.summarize_event(event)
        self.assertEqual((kind, text), ("error", "⚠ connection lost"))
        meta = {
            "recent_activities": [
                {"kind": kind, "text": text},
                {"kind": "error", "text": "turn.failed"},
                {"kind": "cmd_done", "text": "✓ git status"},
            ],
        }
        activity = self.server.latest_activity(meta, SERVER.parent, False, "failed")
        self.assertEqual(activity, {"kind": "error", "text": "connection lost"})
        event = {"type": "turn.failed", "error": {}, "message": "fallback event message"}
        self.assertEqual(self.server.summarize_event(event), ("error", "⚠ fallback event message"))

    def test_warning_events_are_excluded_from_error_activity(self):
        for message in ("Codex is ignoring a hook", "notice about --dangerously-bypass-hook-trust"):
            with self.subTest(message=message):
                error_event = {"type": "error", "message": message}
                self.assertIsNone(self.server.summarize_event(error_event))
                failure_event = {"type": "turn.failed", "error": {"message": message}}
                self.assertEqual(self.server.summarize_event(failure_event), ("error", "turn.failed"))


class ExecutableResolutionTests(unittest.TestCase):
    def test_resolution_order_uses_augmented_path_and_home_fallbacks(self):
        server = load_server()
        paths = ["/custom/codex", "/augmented/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
                 "/test-home/.local/bin/codex", "/test-home/.npm-global/bin/codex"]
        candidates = set(paths)
        with mock.patch.dict(os.environ, {"CODEX_BIN": paths[0]}), \
                mock.patch.object(server, "build_env", return_value={"PATH": "/augmented"}), \
                mock.patch.object(server.Path, "home", return_value=Path("/test-home")), \
                mock.patch.object(server.shutil, "which", return_value=paths[1]) as which, \
                mock.patch.object(server.os.path, "isfile", side_effect=candidates.__contains__), \
                mock.patch.object(server.os, "access", return_value=True):
            for path in paths:
                self.assertEqual(server.resolve_codex_bin(), path)
                candidates.remove(path)
            with self.assertRaisesRegex(RuntimeError, "codex executable not found"):
                server.resolve_codex_bin()
            which.assert_called_with("codex", path="/augmented")


if __name__ == "__main__":
    unittest.main()
