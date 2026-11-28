"""Isolated tests for the CLI and MCP App observability surfaces."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MONITOR = ROOT / "mcp" / "monitor.html"


class TaskFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.state = self.home / "castor"
        self.codex = self.home / "codex"
        self.codex.mkdir()
        self.env = {
            **os.environ, "HOME": str(self.home), "CODEX_CASTOR_STATE_DIR": str(self.state),
            "CODEX_HOME": str(self.codex), "PYTHONUTF8": "1",
        }
        self.env.pop("NO_COLOR", None)
        self.now = datetime.now(timezone.utc)
        self.stamp = self.iso(0)

    def iso(self, seconds_ago):
        return (self.now - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")

    def task(self, name, legacy=False, **overrides):
        path = self.state / "tasks" / name
        path.mkdir(parents=True, exist_ok=True)
        meta = {
            "task_id": name, "round": 1, "status": "running", "pid": os.getpid(),
            "created_at": self.iso(3600), "updated_at": self.stamp, "finished_at": None,
            "session_id": None, "model": None, "reasoning_effort": None,
            "requested_workdir": "/checkout", "effective_workdir": "/worktree",
            "worktree": {"branch": f"codex/{name}"}, "last_event": "turn.started",
        }
        if not legacy:
            meta.update(
                effective_model="gpt-6.2-sol", effective_reasoning_effort="high",
                model_source="runtime_rollout", reasoning_effort_source="runtime_rollout",
                config_model="gpt-6.2-sol", config_reasoning_effort="high",
                round_started_at=self.iso(102), current_activity="▶ python3 -m unittest",
                recent_activities=[{"ts": self.stamp, "kind": "cmd_start", "text": "▶ prior command"}],
            )
        temporary = path / "meta.tmp"
        temporary.write_text(json.dumps({**meta, **overrides}), encoding="utf-8")
        temporary.replace(path / "meta.json")
        return path

    def config(self):
        (self.codex / "config.toml").write_text(
            'model = "configured-model" # default\nmodel_reasoning_effort = \'xhigh\'\n'
            '[profiles.test]\nmodel = "profile-only"\n', encoding="utf-8",
        )

    def rollout(self, session, payload):
        folder = self.codex / "sessions" / "2025" / "04" / "01"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2025-04-01T00-00-00-{session}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for context in ({"model": "old-turn", "effort": "low"}, payload):
                stream.write(json.dumps({"type": "turn_context", "payload": context}) + "\n")
            # The newest context can be several chunks behind a large later event.
            stream.write(json.dumps({"type": "response_item", "payload": "a" * 140000}) + "\n")
            stream.write('{"type":"turn_context","payload":')
        return path

    def command(self, script, *args):
        return [sys.executable, str(SCRIPTS / f"{script}.py"), *args]

    def run_cli(self, script, *args, stdin=""):
        result = subprocess.run(
            self.command(script, *args), input=stdin, text=True,
            capture_output=True, env=self.env, timeout=5, check=True,
        )
        self.assertEqual(result.stderr, "")
        return result.stdout

    def write_events(self, path, round_num, events):
        log = path / f"events-r{round_num}.jsonl"
        log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return log


class StatuslineTests(TaskFixture):
    def test_idle_and_corrupt_state_stay_silent(self):
        self.assertEqual(self.run_cli("statusline"), "")
        path = self.task("broken")
        (path / "meta.json").write_text("{broken", encoding="utf-8")
        self.task("done", status="completed")
        self.assertEqual(self.run_cli("statusline"), "")

    def test_statusline_renders_and_clips_to_stdin_width(self):
        self.task("live", current_activity="▶ python3 -m unittest " + "界" * 50)
        output = self.run_cli("statusline", "--no-color", stdin='{"terminal_width": 69}').strip()
        self.assertRegex(output, r"^⚙ codex \[live\] gpt-6\.2-sol·high · 01:[45][0-9] · ▶ python3")
        self.assertTrue(output.endswith("…"))
        self.assertLessEqual(sum(2 if char == "界" else 1 for char in output), 69)
        self.assertNotIn("\x1b", output)
        self.assertEqual(self.run_cli("statusline", "--no-color", "--width", "1"), "…\n")

    def test_no_color_environment_and_default_ansi(self):
        self.task("live")
        self.assertIn("\x1b[", self.run_cli("statusline"))
        self.env["NO_COLOR"] = ""
        self.assertNotIn("\x1b[", self.run_cli("statusline"))

    def test_marks_effort_override_and_uses_current_round_start(self):
        self.task("override", config_reasoning_effort="xhigh")
        output = self.run_cli("statusline", "--no-color")
        self.assertIn("gpt-6.2-sol·high* · 01:4", output)

    def test_legacy_rollout_prefers_latest_context_over_request_and_config(self):
        self.config()
        self.task("legacy", legacy=True, session_id="legacy-session", model="requested-model", reasoning_effort="low")
        self.rollout("legacy-session", {
            "model": "new-runtime", "collaboration_mode": {"settings": {"reasoning_effort": "high"}},
        })
        output = self.run_cli("statusline", "--no-color")
        self.assertIn("new-runtime·high* · 01:00:0", output)
        self.assertIn("turn.started", output)

    def test_non_runtime_fields_are_probed_independently(self):
        self.config()
        self.task("live", session_id="mixed", model_source="override")
        self.rollout("mixed", {"model": "true-model", "effort": "low"})
        snapshot = json.loads(self.run_cli("statusline", "--json"))
        self.assertEqual(snapshot["active_count"], 1)
        task = snapshot["tasks"][0]
        self.assertEqual(task["effective_model"], "true-model")
        self.assertEqual(task["effective_reasoning_effort"], "high")

    def test_resume_keeps_overrides_until_current_round_context_arrives(self):
        for with_offset in (True, False):
            with self.subTest(with_offset=with_offset):
                session = f"resume-{with_offset}"
                rollout = self.rollout(session, {})
                prior = {"type": "turn_context", "payload": {"model": "old-model", "effort": "low"},
                         # With an offset, even an old entry within the clock tolerance must be skipped.
                         "timestamp": self.iso(1 if with_offset else 30)}
                rollout.write_text(json.dumps(prior) + "\n", encoding="utf-8")
                stat = rollout.stat()
                boundary = {"_rollout_round_start": [str(rollout), stat.st_ino, stat.st_size]} if with_offset else {}
                self.task("resumed", round=2, round_started_at=self.stamp, session_id=session,
                          model="override-model", reasoning_effort="high", effective_model="override-model",
                          effective_reasoning_effort="high", model_source="override",
                          reasoning_effort_source="override", **boundary)

                def settings():
                    task, = json.loads(self.run_cli("statusline", "--json"))["tasks"]
                    return (task["effective_model"], task["effective_reasoning_effort"],
                            task["model_source"], task["reasoning_effort_source"])

                self.assertEqual(settings(), ("override-model", "high", "override", "override"))
                current = {"type": "turn_context", "payload": {"model": "runtime-model", "effort": "xhigh"}}
                if not with_offset:
                    current["timestamp"] = self.iso(1)
                with rollout.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(current) + "\n")
                self.assertEqual(settings(), ("runtime-model", "xhigh", "runtime_rollout", "runtime_rollout"))

    def test_requested_and_config_fallback_with_explicit_paths(self):
        self.config()
        self.task("legacy", legacy=True, model="requested-model")
        self.env["CODEX_HOME"] = str(self.home / "absent-config")
        self.env["CODEX_CASTOR_STATE_DIR"] = str(self.home / "absent-state")
        output = self.run_cli("statusline", "--state-dir", str(self.state), "--codex-home", str(self.codex),
                              "--no-color")
        self.assertIn("requested-model·xhigh*", output)

    def test_dead_pid_and_finished_task_display(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"], env=self.env)
        child.wait(timeout=5)
        self.task("stale", pid=child.pid)
        self.task("done", status="completed", duration_seconds=30)
        self.assertEqual(self.run_cli("statusline", "--no-color"), "")
        self.assertEqual(json.loads(self.run_cli("statusline", "--json")), {"active_count": 0, "tasks": []})
        output = self.run_cli("statusline", "--all", "--no-color")
        self.assertIn("[done] gpt-6.2-sol·high · 00:30 · completed", output)
        self.assertNotIn("stale", output)

    def test_line_limit_prioritizes_recent_tasks(self):
        self.task("older", updated_at=self.iso(4))
        self.task("newer", status="starting")
        output = self.run_cli("statusline", "--max-lines", "1", "--no-color")
        self.assertIn("[newer]", output)
        self.assertNotIn("[older]", output)
        self.assertTrue(output.endswith("+1 more codex tasks\n"))

    def test_open_stdin_pipe_does_not_block(self):
        process = subprocess.Popen(
            self.command("statusline", "--demo", "--no-color"), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True,
        )
        with process:
            try:
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)
                self.assertIn("castor-observability", process.stdout.read())
                self.assertEqual(process.stderr.read(), "")
            finally:
                if process.poll() is None:
                    process.kill()


class WatchTests(TaskFixture):
    def test_dashboard_shows_live_details_and_finished_summary(self):
        entries = [{"ts": self.stamp, "text": f"prior activity {number}"} for number in range(5)]
        self.task("live", config_reasoning_effort="xhigh", recent_activities=entries)
        self.task("done", status="completed", duration_seconds=15, usage={"input_tokens": 1000, "output_tokens": 5})
        output = self.run_cli("watch", "--once", "--no-color")
        for text in ("CODEX CASTOR · LIVE WATCH", "Active: 1 · Total: 2", "● RUNNING  live  r1  01:4",
                     "gpt-6.2-sol · high [runtime_rollout] · ≠ config.toml (xhigh)", "codex/live",
                     "Current Activity: ▶ python3 -m unittest", "prior activity 1", "prior activity 4",
                     "✓ COMPLETED  done", "00:15", "1,005 tokens"):
            with self.subTest(text=text):
                self.assertIn(text, output)
        self.assertNotIn("prior activity 0", output)
        self.assertNotIn("\x1b", output)
        self.assertLess(output.index("live  r1"), output.index("done  r1"))
        self.assertNotIn("done  r1", self.run_cli("watch", "--once", "--active-only"))

    def test_snapshot_enriches_legacy_settings_and_activities(self):
        self.config()
        path = self.task("legacy", legacy=True, session_id="session")
        self.task("done", legacy=True, status="completed", pid=None)
        self.rollout("session", {"model": "runtime-model", "effort": "medium"})
        self.write_events(path, 1, [{"type": "item.completed", "item": {
            "type": "agent_message", "text": "A legacy activity", }}])
        snapshot = json.loads(self.run_cli("watch", "--once", "--json", "--limit", "1", "--active-only"))
        self.assertEqual((snapshot["active_count"], snapshot["total"]), (1, 2))
        task = snapshot["tasks"][0]
        self.assertEqual(task["effective_model"], "runtime-model")
        self.assertEqual(task["effective_reasoning_effort"], "medium")
        self.assertEqual(task["reasoning_effort_source"], "runtime_rollout")
        self.assertEqual(task["config_model"], "configured-model")
        self.assertEqual(task["config_reasoning_effort"], "xhigh")
        self.assertEqual(task["worktree_branch"], "codex/legacy")
        self.assertEqual(task["recent_activities"][0]["text"], "💬 A legacy activity")

    def test_follow_formats_codex_events_and_reports_terminal_status(self):
        path = self.task("done", status="failed", exit_code=1, config_reasoning_effort="xhigh")
        wrapped = """/bin/zsh -lc 'printf "%s" "a b"'"""
        self.write_events(path, 1, [
            {"type": "item.started", "timestamp": "2025-04-01T12:23:34Z",
             "item": {"type": "command_execution", "command": wrapped}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": wrapped, "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "/bin/zsh -lc false",
                                               "exit_code": 1}},
            {"type": "item.completed", "item": {"type": "file_change", "changes": [
                {"path": "scripts/watch.py", "kind": "update"}, {"path": "notes.txt", "kind": "add"}]}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Reviewed\nthe output."}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 12, "cached_input_tokens": 8}},
        ])
        output = self.run_cli("watch", "--follow", "done", "--no-color")
        for text in ("FOLLOW  done  r1", "/worktree · codex/done", "gpt-6.2-sol · high [runtime_rollout]",
                     "≠ config.toml (xhigh)", '[12:23:34] ▶ printf "%s" "a b"',
                     '✓ printf "%s" "a b" (exit 0)', "✗ false (exit 1)",
                     "✎ changed scripts/watch.py, notes.txt", "💬 Reviewed the output.",
                     "112 tokens (100 input, 12 output, 8 cached)", "✗ failed (exit 1)"):
            with self.subTest(text=text):
                self.assertIn(text, output)
        self.assertNotIn("/bin/zsh", output)

    def test_follow_auto_uses_latest_finished_when_idle(self):
        self.task("older", updated_at=self.iso(10), status="completed")
        self.task("newest", status="cancelled", round=3)
        output = self.run_cli("watch", "-f", "--no-color")
        self.assertIn("FOLLOW  newest  r3", output)
        self.assertIn("⊘ cancelled", output)

    def test_long_commands_are_truncated_in_dashboard_and_follow(self):
        prefix = "python3 - <<'PY'"
        opening, closing = prefix + "\n", "\nPY\n"
        command = opening + "#" * (300 - len(opening) - len(closing)) + closing
        item = {"type": "command_execution", "command": "/bin/zsh -lc " + shlex.quote(command), "exit_code": 0}
        path = self.task("long-command", legacy=True)
        self.write_events(path, 1, [{"type": "item.completed", "item": item}])

        dashboard = self.run_cli("watch", "--once", "--no-color")
        activity = next(line.strip() for line in dashboard.splitlines() if prefix in line)
        self.assertTrue(activity.startswith(f"✓ {prefix} "))
        self.assertTrue(activity.endswith("…"))
        self.assertLessEqual(len(activity), 140)

        self.task("long-command", legacy=True, status="completed")
        follow = self.run_cli("watch", "--follow", "long-command", "--no-color")
        snippet = next(line.split("] ✓ ", 1)[1] for line in follow.splitlines() if prefix in line)
        self.assertTrue(snippet.startswith(prefix + " "))
        self.assertTrue(snippet.endswith("… (exit 0)"))
        self.assertEqual(len(snippet.removesuffix(" (exit 0)")), 140)

    def test_follow_stays_open_and_reads_new_round_without_repeating_events(self):
        path = self.task("task", status="completed")
        self.write_events(path, 1, [{"type": "turn.completed"}])
        log = self.home / "watch.out"
        with log.open("w", encoding="utf-8") as output, subprocess.Popen(
            self.command("watch", "-f", "task", "--keep-open", "--interval", "0.02"),
            stdout=output, stderr=subprocess.PIPE, env=self.env, text=True,
        ) as process:
            try:
                self.wait_for_output(log, "\n✓ completed", process)
                self.task("task", round=2, status="running")
                partial = self.write_events(path, 2, [])
                partial.write_text('{"type":"item.completed","item":{"type":"agent_message",', encoding="utf-8")
                self.wait_for_output(log, "FOLLOW  task  r2", process)
                with partial.open("a", encoding="utf-8") as stream:
                    stream.write('"text":"Second round"}}\n')
                self.wait_for_output(log, "💬 Second round", process)
                self.task("task", round=2, status="completed")
                final = self.wait_for_output(log, "💬 Second round\n✓ completed", process)
                self.assertEqual(final.count("turn.completed"), 1)
                self.assertEqual(final.count("💬 Second round"), 1)
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertEqual(process.stderr.read(), "")
            finally:
                if process.poll() is None:
                    process.kill()

    def wait_for_output(self, path, text, process):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and process.poll() is None:
            output = path.read_text(encoding="utf-8", errors="replace")
            if text in output:
                return output
            time.sleep(0.01)
        self.fail(f"Expected {text!r} in {path.read_text(encoding='utf-8')!r}")


class MonitorDocument(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.doctype = ""
        self.tags = []
        self.scripts = []
        self.in_script = False
        self.feed(text)
        self.close()

    def handle_decl(self, declaration):
        self.doctype = declaration.lower()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self.in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts[-1] += data


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.html = MONITOR.read_text(encoding="utf-8")
        self.document = MonitorDocument(self.html)

    def test_html5_document_has_no_external_dependencies(self):
        self.assertEqual(self.document.doctype, "doctype html")
        self.assertLess(MONITOR.stat().st_size, 10 * 1024 * 1024)
        tags = [tag for tag, _ in self.document.tags]
        for tag in ("html", "head", "title", "style", "body", "main", "script"):
            self.assertEqual(tags.count(tag), 1, tag)
        for tag, attrs in self.document.tags:
            self.assertNotIn(tag, ("base", "link", "iframe", "source"))
            self.assertFalse({"src", "srcset"} & attrs.keys())
            self.assertFalse(any(key.startswith("on") for key in attrs))
        self.assertIsNone(re.search(r"https?://|@import\b|\burl\s*\(", self.html, re.IGNORECASE))

    def test_mcp_bridge_methods_and_listener_order(self):
        script, = self.document.scripts
        for token in ("ui/initialize", "ui/notifications/initialized", "2026-01-26",
                      "ui/notifications/tool-result", "ui/notifications/host-context-changed",
                      "ui/notifications/tool-input", "ping", "tools/call", "codex_monitor", "codex_cancel",
                      "structuredContent", "availableDisplayModes", "postMessage"):
            with self.subTest(token=token):
                self.assertIn(token, script)
        self.assertLess(script.index('window.addEventListener("message"'), script.index('method: "ui/initialize"'))

    def test_monitor_views_controls_and_theme(self):
        buttons = {attrs.get("id") for tag, attrs in self.document.tags if tag == "button"}
        self.assertTrue({"show-tasks", "show-analytics", "all", "active-only", "rated", "unrated"} <= buttons)
        self.assertFalse({"pause", "refresh"} & buttons)
        self.assertNotRegex(self.html, r"(?i)>\s*(?:pause|refresh)\s*</button>")
        for token in ("include_detail", "task_detail", "Configuration", "Runtime Status", "Message Stream",
                      'data-theme="dark"', "--color-background-primary", "--font-sans", "prefers-color-scheme: dark"):
            self.assertIn(token, self.html)
        self.assertNotIn("innerHTML", self.html)
        self.assertLess(MONITOR.stat().st_size, 120 * 1024)

    def test_monitor_rating_filters(self):
        script, = self.document.scripts
        for token in ("function isRated(task)", "task?.feedback?.rating != null && task.feedback.rating > 0",
                      'if (filter === "rated") return isRated(task);',
                      'if (filter === "unrated") return !isRated(task);',
                      "No rated tasks.", "No unrated tasks."):
            with self.subTest(token=token):
                self.assertIn(token, script)

    def test_monitor_quota_chip_is_the_rightmost_masthead_control(self):
        chip, = [attrs for tag, attrs in self.document.tags if tag == "button" and attrs.get("id") == "quota-chip"]
        self.assertEqual(chip["type"], "button")
        self.assertIn("quota-chip", chip["class"].split())
        self.assertIn("hidden", chip)
        masthead = re.search(r'<header class="masthead">([\s\S]*?)</header>', self.html)[1]
        self.assertRegex(masthead, r'<button id="quota-chip"[^>]*></button>\s*$')
        self.assertNotRegex(masthead, r'id="(?:active-chip|active-dot|active|total)"')
        self.assertRegex(self.html, r'\.quota-chip\s*\{[^}]*margin-inline-start:\s*auto;')
        self.assertIn(".quota-window + .quota-window", self.html)
        script, = self.document.scripts
        for token in ('if ("quota" in data) quota = data.quota || null', 'quota: demoQuota(params.get("quota"))',
                      'byId("quota-chip").addEventListener("click", refreshQuota)',
                      "function render() {\n  renderQuota();\n  renderFilterCounts();"):
            self.assertIn(token, script)

    def test_monitor_filter_tabs_have_plain_initial_labels(self):
        filters = re.search(r'<div[^>]*id="filters"[^>]*>([\s\S]*?)</div>', self.html)[1]
        buttons = re.findall(r'<button[^>]*id="([^"]+)"[^>]*>(.*?)</button>', filters)
        self.assertEqual([(name, re.sub(r"<[^>]+>", "", label)) for name, label in buttons],
                         [("all", "All"), ("active-only", "Active"), ("rated", "Rated"), ("unrated", "Unrated")])

    def test_monitor_quota_palette_uses_theme_colors_and_transitions(self):
        for selector, colors in ((":root", ("#1e9e4a", "#2f7cf6", "#d99100", "#dc362e")),
                                 (':root[data-theme="dark"]', ("#4ade80", "#60a5fa", "#facc15", "#f87171"))):
            rules = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.html)[1]
            for color, value in zip(("green", "blue", "yellow", "red"), colors):
                self.assertIn(f"--quota-{color}: {value};", rules)
                self.assertIn(f".quota-window.{color} {{ --quota-color: var(--quota-{color}); }}", self.html)
        self.assertIn("transition: color 240ms ease;", self.html)
        self.assertIn("transition: width 240ms ease, background-color 240ms ease;", self.html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for monitor rendering verification")
    def test_monitor_renders_filter_counts_and_refreshes_quota_windows(self):
        script, = self.document.scripts
        render_code = "\n".join(script[script.index(start):script.index(end)] for start, end in (
            ("function node(", "function sourceLabel("), ("function demoQuota(", "function demoAnalytics("),
            ("function isRated(", "function sameRound("), ("function renderFilterCounts(", "function render()"),
        ))
        harness = """
const assert = require("node:assert/strict");
function element(tag) {
  return {tag, children: [], style: {}, attributes: {},
    get firstElementChild() { return this.children[0]; },
    get lastElementChild() { return this.children.at(-1); },
    get childElementCount() { return this.children.length; },
    append(...children) { this.children.push(...children); for (const child of children) child.parent = this; },
    prepend(child) { this.children.unshift(child); child.parent = this; },
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); },
    setAttribute(name, value) { this.attributes[name] = value; }};
}
const controls = Object.fromEntries(["quota-chip", "all", "active-only", "rated", "unrated"]
  .map(id => [id, element("button")]));
const byId = id => controls[id];
const chip = byId("quota-chip");
const active = byId("active-only");
active.append(element("span"));
const document = {createElement: element};
let quota = null, refreshingQuota = false, disposed = false, preview = true;
let tasks = [], count = 0, total = 0;
let reply;
function call(name, args) {
  assert.equal(name, "codex_monitor");
  assert.deepEqual(args, {limit: 0, refresh_quota: true});
  return new Promise(resolve => { reply = resolve; });
}
const toolPayload = result => result.structuredContent;
function showNotice(message) { throw new Error(message); }
"""
        assertions = """
function verifyFilterCounts() {
  renderFilterCounts();
  assert.equal(byId("all").textContent, "All (0)");
  assert.equal(active.lastElementChild.textContent, "Active (0)");
  assert.equal(active.childElementCount, 1);
  assert.equal(byId("rated").textContent, "Rated (0)");
  assert.equal(byId("unrated").textContent, "Unrated (0)");

  tasks = [{feedback: {rating: 4}}, {feedback: {rating: 0}}, {}, {feedback: {rating: 5}}];
  count = 1;
  total = 8;
  renderFilterCounts();
  assert.equal(byId("all").textContent, "All (8)");
  assert.equal(active.lastElementChild.textContent, "Active (1)");
  assert.equal(active.childElementCount, 2);
  const dot = active.firstElementChild;
  assert.equal(dot.tag, "i");
  assert.equal(dot.className, "dot running");
  assert.equal(byId("rated").textContent, "Rated (2)");
  assert.equal(byId("unrated").textContent, "Unrated (2)");
  renderFilterCounts();
  assert.equal(active.firstElementChild, dot);
  count = 0;
  renderFilterCounts();
  assert.equal(active.lastElementChild.textContent, "Active (0)");
  assert.equal(active.childElementCount, 1);
}

async function verifyQuota() {
  renderQuota();
  assert.equal(chip.hidden, true);
  quota = demoQuota("weekly");
  renderQuota();
  assert.equal(chip.hidden, false);
  assert.deepEqual(chip.children.map(item => item.children.map(child => child.textContent).slice(0, 2)),
    [["7d", "22%"]]);
  assert.match(chip.attributes["aria-label"], /Codex quota \\(prolite\\).*7d: 22% left \\(78% used\\).*resets /);
  assert.match(chip.title, /Updated .*\\nClick to refresh$/);
  quota.updated_at = "2020-01-01T00:00:00Z";
  await refreshQuota();
  assert.notEqual(quota.updated_at, "2020-01-01T00:00:00Z");

  preview = false;
  const refresh = refreshQuota();
  assert.equal(chip.disabled, true);
  reply({structuredContent: {quota: demoQuota()}});
  await refresh;
  assert.equal(chip.disabled, false);
  assert.deepEqual(chip.children.map(item => [item.children[0].textContent, item.children[1].textContent,
    item.children[2].children[0].style.width, item.className]),
    [["5h", "84%", "84%", "quota-window green"], ["7d", "22%", "22%", "quota-window yellow"]]);
  const item = chip.lastElementChild;
  const fill = item.lastElementChild.firstElementChild;
  for (const [remaining, color, width] of [[110, "green", "100%"], [75, "green", "75%"], [74.9, "blue", "74.9%"],
      [45, "blue", "45%"], [44.9, "yellow", "44.9%"], [20, "yellow", "20%"], [19.9, "red", "19.9%"], [-1, "red", "0%"]]) {
    quota.windows[1].remaining_percent = remaining;
    renderQuota();
    assert.equal(chip.lastElementChild, item);
    assert.equal(item.lastElementChild.firstElementChild, fill);
    assert.equal(item.className, `quota-window ${color}`);
    assert.equal(fill.style.width, width);
  }
  quota = demoQuota("weekly");
  renderQuota();
  assert.equal(chip.childElementCount, 1);
  quota = {windows: []};
  renderQuota();
  assert.equal(chip.hidden, true);
}
verifyFilterCounts();
verifyQuota().catch(error => { process.stderr.write(String(error.stack)); process.exitCode = 1; });
"""
        result = subprocess.run(["node"], input=harness + render_code + assertions,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_monitor_project_groups_sheets_and_polling(self):
        elements = {attrs["id"]: (tag, attrs) for tag, attrs in self.document.tags if "id" in attrs}
        self.assertEqual(elements["home-layer"][0], "main")
        for level, name in [(2, "detail-layer"), (3, "sub-layer")]:
            tag, attrs = elements[name]
            self.assertEqual(tag, "div")
            self.assertEqual(attrs["role"], "dialog")
            self.assertEqual(attrs["aria-modal"], "true")
            self.assertIn("hidden", attrs)
            self.assertIn(f"level-{level}", attrs["class"].split())
        script, = self.document.scripts
        for token in ("expandedProjects = new Set()", 'setAttribute("aria-expanded"', "projectName(task)",
                      "event.target === detailLayer", "event.target === subLayer", "homeLayer.inert",
                      'fullViewButton("prompt")', 'fullViewButton("stream")', 'back.id = "back-sub"',
                      "count > 0 || live(selectedTask()) ? 1000 : 8000", "setInterval(tick, 1000)",
                      "{limit: 0, task_id: selected, include_detail: true}", "{limit: 200}",
                      "task.finished_at || task.updated_at || task.created_at",
                      'new Option("Undecided", "")',
                      "cardModel(task)"):
            with self.subTest(token=token):
                self.assertIn(token, script)
        self.assertNotIn('new Option("Verdict", "")', script)
        self.assertNotIn("Show full prompt", self.html)
        self.assertNotIn("Override · config ", self.html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax verification")
    def test_inline_javascript_syntax(self):
        script, = self.document.scripts
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", "--check", str(path)], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
