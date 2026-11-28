#!/usr/bin/env python3
"""Watch codex-castor tasks or follow a task's Codex event stream."""

import argparse
from itertools import islice
import json
import math
import os
import re
import shlex
import sys
import time

from statusline import (
    LIVE_STATUSES, TaskSettings, activity, clean, clock, codex_home, config_differences,
    elapsed, object_value, pid_alive, read_meta, recency, reverse_lines, scan_tasks, state_dir, timestamp, trim,
)
from castor_platform import configure_stdio, enable_terminal_sequences


ICONS = {"completed": "✓", "failed": "✗", "cancelled": "⊘", "interrupted": "⊘"}
SNIPPET_WIDTH = 140
USAGE_FIELDS = (
    ("input_tokens", "input"), ("output_tokens", "output"),
    ("cached_input_tokens", "cached"), ("reasoning_output_tokens", "reasoning"),
)


def paint(text, code, color):
    return f"\x1b[{code}m{text}\x1b[0m" if color else text


def model_line(task):
    model = clean(task.get("effective_model") or "unknown")
    effort = clean(task.get("effective_reasoning_effort") or "unknown")
    sources = list(dict.fromkeys(clean(task[key]) for key in ("model_source", "reasoning_effort_source")))
    defaults = ", ".join(map(clean, config_differences(task)))
    override = f" · ≠ config.toml ({defaults})" if defaults else ""
    return f"{model} · {effort} [{'/'.join(sources)}]{override}"


def usage_line(value):
    usage = object_value(value)
    counts = [f"{usage[key]:,} {label}" for key, label in USAGE_FIELDS if type(usage.get(key)) is int]
    if not counts:
        return ""
    total = sum(usage[key] for key, _ in USAGE_FIELDS[:2] if type(usage.get(key)) is int)
    return f"{total:,} tokens ({', '.join(counts)})"


def task_view(task, settings, now):
    settings.enrich(task)
    task["elapsed_seconds"] = elapsed(task, now)
    task["status"] = task.get("status") or "unknown"
    if task.get("status") in LIVE_STATUSES and not task["alive"]:
        task["status"] = "interrupted"
    task["current_activity"] = activity(task)
    task["workdir"] = task.get("effective_workdir") or task.get("requested_workdir") or task.get("workdir")
    task["worktree_branch"] = object_value(task.get("worktree")).get("branch") or task.get("worktree_branch")
    return task


def legacy_activities(directory, task):
    if "recent_activities" in task:
        return
    task["recent_activities"] = entries = []
    path = os.path.join(directory, f"events-r{round_number(task)}.jsonl")
    try:
        with open(path, "rb") as stream:
            for line in islice(reverse_lines(stream), 32):
                try:
                    event = object_value(json.loads(line))
                except ValueError:
                    continue
                text = summarize_event(event)
                if text:
                    entries.append({"ts": event.get("timestamp") or event.get("ts"),
                                    "kind": event.get("type"), "text": text})
                if len(entries) == 4:
                    break
    except OSError:
        pass
    entries.reverse()


def task_rows(root):
    return sorted(scan_tasks(root), key=lambda row: (row[1]["alive"], recency(row[1])), reverse=True)


def event_clock(value, fallback=None):
    epoch = timestamp(value)
    return time.strftime("%H:%M:%S", time.gmtime(epoch if epoch is not None else fallback))


def render_frame(tasks, active_count, total, color):
    heading = paint("CODEX CASTOR · LIVE WATCH", "1;36", color)
    now = time.strftime("%H:%M:%S", time.gmtime())
    lines = [f"{heading}  |  Active: {active_count} · Total: {total}  |  UTC {now}"]
    if not tasks:
        lines += ["", "No tasks to display."]
    previous = None
    for task in tasks:
        active = task["alive"]
        if active != previous:
            lines += ["", "Running tasks" if active else "Recent finished tasks"]
            previous = active
        status = clean(task.get("status") or "unknown")
        icon = "●" if active else ICONS.get(status, "·")
        label = paint(f"{icon} {status.upper()}", "36" if active else "2", color)
        title = f"{label}  {clean(task['task_id'])}  r{clean(task.get('round') or 1)}"
        duration = clock(task["elapsed_seconds"])
        if active:
            lines += [f"{title}  {duration}", f"  {model_line(task)}"]
            location = task["worktree_branch"] or task["workdir"]
            if location:
                lines.append(f"  {clean(location)}")
            lines.append(f"  Current Activity: {task['current_activity']}")
            for entry in (task.get("recent_activities") or [])[-4:]:
                entry = object_value(entry)
                if entry.get("text"):
                    event_time = timestamp(entry.get("ts"))
                    stamp = f"[{event_clock(None, event_time)}] " if event_time is not None else ""
                    text = trim(clean(entry["text"]), SNIPPET_WIDTH)
                    lines.append(f"    {stamp}{text}")
        else:
            tokens = usage_line(task.get("usage"))
            suffix = f" · {tokens}" if tokens else ""
            lines += [f"{title}  {model_line(task)} · {duration}{suffix}", f"  {task['current_activity']}"]
    return "\n".join(lines) + "\n"


def unwrap_command(command):
    command = str(command or "")
    # Codex records a shell-quoted command as the last argument to the login shell.
    if re.match(r"^(?:/\S*/)?(?:zsh|bash|sh|fish) -[a-z]*c(?:\s|$)", command):
        try:
            parts = shlex.split(command)
            if len(parts) == 3:
                command = parts[2]
        except ValueError:
            pass
    return trim(clean(command), SNIPPET_WIDTH)


def summarize_event(event):
    kind = event.get("type")
    item = object_value(event.get("item"))
    item_type = item.get("type")
    if kind in ("item.started", "item.completed", "item.updated"):
        if item_type == "command_execution" and kind != "item.updated":
            command = unwrap_command(item.get("command"))
            if kind == "item.started":
                return f"▶ {command}"
            code = item.get("exit_code")
            icon = "✓" if code == 0 else "✗"
            return f"{icon} {command}" + (f" (exit {code})" if type(code) is int else "")
        if kind == "item.completed":
            if item_type == "file_change":
                changes = [object_value(change) for change in item.get("changes") or []]
                paths = ", ".join(clean(change["path"]) for change in changes if change.get("path"))
                return f"✎ changed {paths}" if paths else "✎ file changes"
            if item_type == "agent_message":
                return f"💬 {trim(clean(item.get('text') or ''), SNIPPET_WIDTH)}"
            if item_type == "error":
                return f"✗ {trim(clean(item.get('message') or 'Item failed'), SNIPPET_WIDTH)}"
            if item_type:
                return f"· {clean(item_type)} completed"
    elif kind == "turn.completed":
        tokens = usage_line(event.get("usage"))
        return f"✓ turn.completed · {tokens}" if tokens else "✓ turn.completed"
    elif kind in ("turn.failed", "error"):
        message = object_value(event.get("error")).get("message") or event.get("message") or kind
        return f"✗ {trim(clean(message), SNIPPET_WIDTH)}"
    elif kind in ("turn.started", "thread.started"):
        return clean(kind)
    return ""


def round_number(task):
    value = task.get("round")
    return value if type(value) is int and value > 0 else 1


def print_header(task, color, now):
    label = f"CODEX CASTOR · FOLLOW  {clean(task['task_id'])}  r{round_number(task)}"
    print(paint(label, "1;36", color))
    location = " · ".join(clean(task[key]) for key in ("workdir", "worktree_branch") if task.get(key))
    if location:
        print(f"  {location}")
    print(f"  {model_line(task)}")
    print(f"  Elapsed {clock(elapsed(task, now))} · Event times in UTC", flush=True)


def event_logs(directory, first_round):
    with os.scandir(directory) as entries:
        logs = []
        for entry in entries:
            match = re.fullmatch(r"events-r(\d+)\.jsonl", entry.name)
            if match and entry.is_file() and int(match[1]) >= first_round:
                logs.append((int(match[1]), entry.path))
    return sorted(logs)


def tail_events(path, cursor, final, color):
    try:
        with open(path, "rb") as stream:
            state = os.fstat(stream.fileno())
            identity = (state.st_dev, state.st_ino)
            offset = cursor[1] if cursor and cursor[0] == identity and cursor[1] <= state.st_size else 0
            stream.seek(offset)
            saw_usage = bool(offset and cursor[2])
            for line in iter(stream.readline, b""):
                if not final and not line.endswith(b"\n"):
                    break
                offset += len(line)
                try:
                    event = object_value(json.loads(line))
                except ValueError:
                    continue
                summary = summarize_event(event)
                if summary:
                    stamp = event_clock(event.get("timestamp") or event.get("ts"))
                    print(f"[{stamp}] {paint(summary, '36', color) if summary.startswith('▶') else summary}", flush=True)
                if event.get("type") == "turn.completed" and usage_line(event.get("usage")):
                    saw_usage = True
            return identity, offset, saw_usage
    except OSError:
        return cursor


def follow(args, color):
    rows = task_rows(args.state_dir)
    match = next(((directory, task) for directory, task in rows
                  if args.follow == "auto" or task["task_id"] == args.follow), None)
    if match is None:
        print("No Codex tasks found." if args.follow == "auto" else
              f"Codex task not found: {clean(args.follow)}", file=sys.stderr)
        return 1
    directory, task = match
    meta_path = os.path.join(directory, "meta.json")
    task_id = task["task_id"]
    first_round = round_number(task)
    banner = None
    reported_end = None
    cursors = {}
    while True:
        latest = read_meta(meta_path)
        if not latest:
            if not os.path.isdir(directory):
                print("Codex task was removed.", flush=True)
                return 0
            time.sleep(args.interval)
            continue
        latest["task_id"] = task_id
        now = time.time()
        status = latest.get("status")
        latest["alive"] = status in LIVE_STATUSES and pid_alive(latest.get("pid"), latest.get("windows_job"))
        updated = timestamp(latest.get("updated_at") or latest.get("created_at"))
        # The process may exit before the server finishes writing its final metadata.
        stale = not latest["alive"] and updated is not None and now - updated > 30
        terminal = status not in LIVE_STATUSES or stale
        view = task_view(latest, TaskSettings(args.codex_home), now)
        current_round = round_number(view)
        signature = (current_round, model_line(view))
        if signature != banner:
            print_header(view, color, now)
            banner = signature
        try:
            logs = event_logs(directory, first_round)
        except OSError:
            logs = []
        for number, path in logs:
            cursor = tail_events(path, cursors.get(path), terminal or number < current_round, color)
            if cursor is not None:
                cursors[path] = cursor
        if terminal and reported_end != (current_round, view["status"]):
            reported_end = (current_round, view["status"])
            code = view.get("exit_code")
            suffix = f" (exit {code})" if type(code) is int else ""
            print(f"{ICONS.get(view['status'], '·')} {clean(view['status'])}{suffix}", flush=True)
            saw_usage = any(cursors.get(path, (None, 0, False))[2] for number, path in logs
                            if number == current_round)
            tokens = usage_line(view.get("usage"))
            if tokens and not saw_usage:
                print(tokens, flush=True)
        if terminal and not args.keep_open:
            return 0
        time.sleep(args.interval)


def dashboard(args, color, tty):
    while True:
        rows = task_rows(args.state_dir)
        active = sum(task["alive"] for _, task in rows)
        total = len(rows)
        now = time.time()
        settings = TaskSettings(args.codex_home)
        visible = rows if args.json else [(directory, task) for directory, task in rows
                                          if not args.active_only or task["alive"]][:args.limit]
        tasks = []
        for directory, task in visible:
            legacy_activities(directory, task)
            tasks.append(task_view(task, settings, now))
        if args.json:
            print(json.dumps({"active_count": active, "total": total, "tasks": tasks}, ensure_ascii=False))
            return 0
        frame = render_frame(tasks, active, total, color)
        sys.stdout.write(("\x1b[H\x1b[J" if tty and not args.once else "") + frame)
        sys.stdout.flush()
        if args.once:
            return 0
        time.sleep(args.interval)


def main():
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=state_dir())
    parser.add_argument("--codex-home", default=codex_home())
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("-f", "--follow", nargs="?", const="auto", metavar="TASK_ID")
    parser.add_argument("--keep-open", action="store_true", help="Keep following after the task finishes")
    colors = parser.add_mutually_exclusive_group()
    colors.add_argument("--no-color", action="store_true")
    colors.add_argument("--color", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit all tasks as JSON with --once")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0 or args.limit < 1:
        parser.error("--interval and --limit must be positive")
    if args.json and (not args.once or args.follow is not None):
        parser.error("--json requires dashboard mode with --once")
    args.state_dir = os.path.expanduser(args.state_dir)
    args.codex_home = os.path.expanduser(args.codex_home)
    tty = enable_terminal_sequences()
    color = args.color or (tty and not args.no_color and "NO_COLOR" not in os.environ)
    try:
        return follow(args, color) if args.follow is not None else dashboard(args, color, tty)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
