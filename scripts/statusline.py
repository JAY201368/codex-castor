#!/usr/bin/env python3
"""Render codex-castor tasks in the Antigravity CLI status line."""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_DIR = HERE if (HERE / "castor_platform.py").is_file() else HERE.parent / "mcp"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from castor_platform import configure_stdio, pid_alive, read_available_stdin, read_json_file


LIVE_STATUSES = {"starting", "running"}
SETTINGS = (("model", "model"), ("reasoning_effort", "model_reasoning_effort"))


def state_dir():
    return os.path.expanduser(os.environ.get("CODEX_CASTOR_STATE_DIR", "~/.gemini/codex-castor"))


def codex_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))


def object_value(value):
    return value if isinstance(value, dict) else {}


def read_meta(path):
    try:
        return object_value(read_json_file(path))
    except (OSError, ValueError):
        return {}


def scan_tasks(root):
    try:
        with os.scandir(os.path.join(root, "tasks")) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                task = read_meta(os.path.join(entry.path, "meta.json"))
                if not task:
                    continue
                task["task_id"] = task.get("task_id") or entry.name
                task["alive"] = task.get("status") in LIVE_STATUSES and pid_alive(task.get("pid"), task.get("windows_job"))
                yield entry.path, task
    except OSError:
        return


def recency(task):
    return str(task.get("updated_at") or task.get("created_at") or ""), task["task_id"]


def read_config(home):
    """Read the two root-level Codex settings on Python 3.9 and newer."""
    try:
        with open(os.path.join(home, "config.toml"), encoding="utf-8") as stream:
            try:
                import tomllib
            except ImportError:
                # On 3.9 and 3.10, these settings use single-line TOML strings.
                import re

                assignment = re.compile(
                    r"""^\s*(model|model_reasoning_effort)\s*=\s*("(?:(?:\\.)|[^"\\])*"|'[^']*')\s*(?:#.*)?$"""
                )
                config = {}
                for line in stream:
                    if line.lstrip().startswith("["):
                        break
                    match = assignment.match(line)
                    if match:
                        key, value = match.groups()
                        config[key] = json.loads(value) if value.startswith('"') else value[1:-1]
                return config
            return tomllib.loads(stream.read())
    except (OSError, ValueError):
        return {}


def reverse_lines(stream, start=0):
    """Walk a rollout backwards without loading an entire session into memory."""
    position = stream.seek(0, os.SEEK_END)
    prefix = b""
    while position > start:
        size = min(position - start, 65536)
        position -= size
        stream.seek(position)
        prefix, *lines = (stream.read(size) + prefix).split(b"\n")
        yield from reversed(lines)
    if prefix:
        yield prefix


def latest_turn_context(home, session_id, *, round_start=None, earliest=None):
    if not isinstance(session_id, str) or not session_id:
        return {}
    if not all(char.isascii() and (char.isalnum() or char in "-_") for char in session_id):
        return {}

    from glob import escape, iglob

    pattern = os.path.join(escape(home), "sessions", "*", "*", "*", f"rollout-*{session_id}*.jsonl")
    rollouts = []
    for path in iglob(pattern):
        try:
            rollouts.append((os.stat(path).st_mtime_ns, path))
        except OSError:
            continue
    for _, path in sorted(rollouts, reverse=True):
        try:
            with open(path, "rb") as stream:
                start = None
                if isinstance(round_start, list) and len(round_start) == 3:
                    saved_path, inode, offset = round_start
                    if isinstance(saved_path, str) and os.path.abspath(path) == os.path.abspath(saved_path):
                        stat = os.fstat(stream.fileno())
                        if stat.st_ino == inode and type(offset) is int and 0 <= offset <= stat.st_size:
                            start = offset
                for line in reverse_lines(stream, start=start or 0):
                    if b"turn_context" not in line:
                        continue
                    try:
                        event = object_value(json.loads(line))
                    except ValueError:
                        continue
                    if event.get("type") == "turn_context":
                        if start is None and earliest is not None:
                            event_time = timestamp(event.get("timestamp"))
                            if event_time is None or event_time < earliest:
                                continue
                        return object_value(event.get("payload"))
        except OSError:
            continue
    return {}


class TaskSettings:
    def __init__(self, home):
        self.home = home
        self.config = None

    def enrich(self, task):
        if any(not task.get("config_" + field) for field, _ in SETTINGS):
            if self.config is None:
                self.config = read_config(self.home)
        if any(not task.get("effective_" + field) or task.get(field + "_source") != "runtime_rollout"
               for field, _ in SETTINGS):
            round_number = task.get("round")
            started = None
            if type(round_number) is int and round_number > 1 and any(
                    task.get(field + "_source") == "override" for field, _ in SETTINGS):
                started = timestamp(task.get("round_started_at"))
            context = latest_turn_context(
                self.home, task.get("session_id"), round_start=task.get("_rollout_round_start"),
                earliest=started - 2.0 if started is not None else None,
            )
        else:
            context = {}
        mode = object_value(context.get("collaboration_mode"))
        runtime = {
            "model": context.get("model"),
            "reasoning_effort": context.get("effort") or object_value(mode.get("settings")).get("reasoning_effort"),
        }
        for field, key in SETTINGS:
            config_key = "config_" + field
            effective_key = "effective_" + field
            source_key = field + "_source"
            config = task.get(config_key) or (self.config or {}).get(key)
            task[config_key] = config
            if task.get(effective_key) and task.get(source_key) == "runtime_rollout":
                continue
            if runtime[field]:
                task[effective_key], task[source_key] = runtime[field], "runtime_rollout"
            elif task.get(effective_key):
                task[source_key] = task.get(source_key) or "unknown"
            elif task.get(field):
                task[effective_key], task[source_key] = task[field], "override"
            else:
                task[effective_key], task[source_key] = config, "config" if config else "unknown"


def config_differences(task):
    return [task["config_" + field] for field, _ in SETTINGS
            if task.get("config_" + field) and task.get("effective_" + field)
            and task["config_" + field] != task["effective_" + field]]


def timestamp(value):
    if not isinstance(value, str):
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def elapsed(task, now):
    duration = task.get("duration_seconds")
    if not task["alive"] and type(duration) in (int, float) and 0 <= duration < float("inf"):
        return duration
    start = timestamp(task.get("round_started_at"))
    if start is None:
        start = timestamp(task.get("created_at"))
    end = now if task["alive"] else timestamp(task.get("finished_at") or task.get("updated_at"))
    return max(0, end - start) if end is not None and start is not None else 0


def clock(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours:02d}:" if hours else "") + f"{minutes:02d}:{seconds:02d}"


def clean(value):
    # Keep terminal control characters from task output out of the display.
    return "".join(ch if ch.isprintable() or ch == "\u200d" else " " for ch in str(value)).strip()


def activity(task):
    return clean(task.get("current_activity") or task.get("last_event") or "Waiting for activity")


def display_width(text):
    from unicodedata import combining, east_asian_width

    return sum(0 if combining(ch) or ch in "\ufe0e\ufe0f\u200d"
               else 2 if east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def trim(text, width):
    if display_width(text) <= width:
        return text
    used = 0
    for index, char in enumerate(text):
        used += display_width(char)
        if used >= width:
            return text[:index].rstrip() + "…"
    return text


def stdin_width():
    try:
        data = read_available_stdin(65536)
        value = object_value(json.loads(data)).get("terminal_width")
        if type(value) is int and value > 0:
            return value
    except (OSError, ValueError):
        pass
    return 100


def demo_task():
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 102))
    return {
        "task_id": "castor-observability", "status": "running", "alive": True, "round": 1,
        "round_started_at": stamp, "effective_model": "gpt-6.2-sol",
        "effective_reasoning_effort": "high", "config_model": "gpt-6.2-sol",
        "config_reasoning_effort": "xhigh", "model_source": "runtime_rollout",
        "reasoning_effort_source": "runtime_rollout", "current_activity": "▶ python3 -m unittest",
    }


def main():
    configure_stdio()
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=state_dir())
    parser.add_argument("--codex-home", default=codex_home())
    parser.add_argument("--width", type=int)
    parser.add_argument("--max-lines", type=int, default=3)
    parser.add_argument("--all", action="store_true", help="Show the latest finished task when idle")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.max_lines < 1 or (args.width is not None and args.width < 1):
        parser.error("--width and --max-lines must be positive")
    width = args.width if args.width is not None else stdin_width()

    if args.demo:
        active = [demo_task()]
        latest_finished = None
    else:
        active = []
        latest_finished = None
        for _, task in scan_tasks(os.path.expanduser(args.state_dir)):
            if task["alive"]:
                active.append(task)
            elif args.all and task.get("status") not in LIVE_STATUSES:
                if latest_finished is None or recency(task) > recency(latest_finished):
                    latest_finished = task
        active.sort(key=recency, reverse=True)

    if args.json:
        tasks = active
    else:
        tasks = active[:args.max_lines] or ([latest_finished] if latest_finished else [])
    settings = TaskSettings(os.path.expanduser(args.codex_home))
    now = time.time()
    for task in tasks:
        settings.enrich(task)
        task["elapsed_seconds"] = elapsed(task, now)
    if args.json:
        print(json.dumps({"active_count": len(active), "tasks": tasks}, ensure_ascii=False))
        return 0

    color = not args.no_color and "NO_COLOR" not in os.environ
    for task in tasks:
        model = clean(task.get("effective_model") or "unknown")
        effort = clean(task.get("effective_reasoning_effort") or "unknown")
        marker = "*" if config_differences(task) else ""
        details = activity(task)
        if not task["alive"]:
            details = f"{clean(task.get('status') or 'finished')} · {details}"
        line = (f"⚙ codex [{clean(task['task_id'])}] {model}·{effort}{marker}"
                f" · {clock(task['elapsed_seconds'])} · {details}")
        line = trim(line, width)
        print(f"\x1b[36m{line}\x1b[0m" if color else line)
    if len(active) > args.max_lines:
        print(trim(f"+{len(active) - args.max_lines} more codex tasks", width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
