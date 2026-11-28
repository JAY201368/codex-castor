#!/usr/bin/env python3
"""Display Codex wakeup telemetry and capture first replies in Antigravity."""

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_DIR = HERE if (HERE / "telemetry.py").is_file() else HERE.parent / "mcp"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

CODEX_TOOLS = ("codex_dispatch", "codex_resume")
LAUNCH = re.compile(r"Tool is running as a background task with task id:\s*(\S+)")
WAKEUP = re.compile(r'Task id "([^"]+)"\s*$')
SENDER = re.compile(r"(?:^|\s)sender=(\S+)")
RESULT_FIELDS = {"task_id", "round", "status"}
RESULT_KEY = re.compile(r'"(task_id|round|status|artifacts|workdir|duration_seconds)"\s*:')
RESULT_TASK = re.compile(r'"task_id"\s*:\s*"([^"]+)"')
RESULT_ROUND = re.compile(r'"round"\s*:\s*(\d+)(?=\s*(?:[,}]|$))')
DECODER = json.JSONDecoder()


def safe_conversation_id(conversation_id):
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", conversation_id).strip("._")[:120] or "default"


def user_prompt(content):
    _, opening, request = content.partition("<USER_REQUEST>")
    prompt, closing, _ = request.partition("</USER_REQUEST>")
    if opening and closing:
        prompt = re.sub(r"^\r?\n", "", prompt, count=1)
        return re.sub(r"\r?\n\Z", "", prompt, count=1)
    return content.strip()


def is_codex_call(call):
    if not isinstance(call, dict):
        return False
    name = call.get("name")
    args = call.get("args")
    return isinstance(name, str) and (
        name.endswith(CODEX_TOOLS)
        or (name == "call_mcp_tool" and isinstance(args, dict) and args.get("ToolName") in CODEX_TOOLS)
    )


def result_round(content, signature=()):
    start = content.find("{")
    if start == -1:
        return None
    body = content[start:]
    try:
        result, _ = DECODER.raw_decode(body)
        keys = result.keys()
        task_id, number = result.get("task_id"), result.get("round")
    except json.JSONDecodeError:
        # Antigravity may truncate the serialized result after its leading fields.
        keys = set(RESULT_KEY.findall(body)) if signature else ()
        task = RESULT_TASK.search(body)
        number_match = RESULT_ROUND.search(body)
        if not task or not number_match:
            return None
        task_id, number = task[1], int(number_match[1])
    if signature and (not RESULT_FIELDS.issubset(keys) or not any(key in keys for key in signature)):
        return None
    if (
        isinstance(task_id, str) and task_id and "\0" not in task_id
        and Path(task_id).name == task_id and task_id not in (".", "..")
        and type(number) is int and number >= 1
    ):
        return task_id, number
    return None


def completed_round(kind, content, state):
    if kind == "GENERIC":
        launch = LAUNCH.search(content)
        if launch:
            description = content.partition("Task Description:")[2]
            if any(tool in description for tool in CODEX_TOOLS):
                task = launch[1]
                state["bg_tasks"] = [item for item in state["bg_tasks"] if item != task][-49:] + [task]
        elif state["expect_sync_result"]:
            return result_round(content, ("artifacts",))
    elif kind == "SYSTEM_MESSAGE":
        header, marker, body = content.partition("finished with result:")
        if marker:
            match = WAKEUP.search(header) or SENDER.search(header)
            if match:
                task = match[1]
                if task in state["bg_tasks"]:
                    state["bg_tasks"].remove(task)
                    return result_round(body)
                return result_round(body, ("artifacts", "workdir", "duration_seconds"))
    return None


def can_capture(state_root, state, task, number):
    from telemetry import read_meta

    if number in state["recorded"].get(task, []):
        return False
    try:
        meta = read_meta(state_root / "tasks" / task / "meta.json")
        if not meta or (meta.get("status") in ("starting", "running") and number == int(meta.get("round", 1))):
            return False
    except (OSError, TypeError, ValueError):
        return False
    feedback = meta.get("feedback")
    for items, field in (
        (feedback.get("user_first_prompts") if isinstance(feedback, dict) else None, "prompt"),
        (meta.get("rounds"), "user_first_prompt"),
    ):
        if isinstance(items, list) and any(
            isinstance(item, dict) and item.get("round") == number and item.get(field) for item in items
        ):
            return False
    return True


def record_reply(state_root, state, task, prompt):
    from telemetry import record_feedback

    number = state["pending"].pop(task)["round"]
    try:
        if can_capture(state_root, state, task, number):
            record_feedback(state_root, task, {"round": number, "user_first_prompt": prompt})
    except (ValueError, OSError):
        # A failed write for one task must allow the other pending rounds to be processed.
        pass
    recorded = state["recorded"].setdefault(task, [])
    if number not in recorded:
        recorded.append(number)


def format_wakeup_summary(task, number, meta):
    from telemetry import normalize_usage

    round_entry = next(
        (item for item in meta.get("rounds") or [] if isinstance(item, dict) and item.get("round") == number),
        {},
    )
    status = round_entry.get("status") or meta.get("status") or "unknown"
    model = round_entry.get("model") or meta.get("effective_model") or meta.get("model") or "unknown"
    effort = (
        round_entry.get("reasoning_effort") or meta.get("effective_reasoning_effort")
        or meta.get("reasoning_effort") or "unknown"
    )
    duration = round_entry.get("duration_seconds")
    usage = normalize_usage(round_entry.get("usage"))
    if number == int(meta.get("round", 1)):
        if duration is None:
            duration = meta.get("duration_seconds")
        if usage is None:
            usage = normalize_usage(meta.get("usage"))
    total_duration = meta.get("total_duration_seconds")
    total_usage = normalize_usage(meta.get("total_usage"))

    round_label = f"Round {number}"
    if number > 1:
        round_label += f" ({number - 1} {'rework' if number == 2 else 'reworks'})"

    if type(duration) in (int, float):
        duration_label = f"{duration:.2f}s"
        if number > 1 and type(total_duration) in (int, float):
            duration_label += f" (total {total_duration:.2f}s)"
    elif type(total_duration) in (int, float):
        duration_label = f"total {total_duration:.2f}s"
    else:
        duration_label = "duration —"

    if usage is not None:
        tokens_label = f"{usage['total_tokens']:,} tokens"
        if number > 1 and total_usage is not None:
            tokens_label += f" (total {total_usage['total_tokens']:,})"
    elif total_usage is not None:
        tokens_label = f"total {total_usage['total_tokens']:,} tokens"
    else:
        tokens_label = "tokens —"

    return (
        f"> **[Codex Castor Hook · auto]** `{task}` ({status}) | `{model}` (`{effort}`) | "
        f"{round_label} | {duration_label} | {tokens_label}"
    )


def scan(stream, state_root, state, live_input):
    from telemetry import read_meta

    baseline = live_input
    newly_completed = {}
    last_user_line = state["line_number"]
    stream.seek(state["transcript_offset"])
    for line in stream:
        if not line.endswith(b"\n"):
            break
        state["transcript_offset"] += len(line)
        state["line_number"] += 1
        try:
            step = json.loads(line)
        except ValueError:
            continue
        if not isinstance(step, dict):
            continue
        kind, content = step.get("type"), step.get("content")
        content = content if isinstance(content, str) else ""
        if kind == "PLANNER_RESPONSE":
            calls = step.get("tool_calls")
            state["expect_sync_result"] = isinstance(calls, list) and any(is_codex_call(call) for call in calls)
        elif kind == "USER_INPUT" and step.get("source") != "SYSTEM":
            state["expect_sync_result"] = False
            last_user_line = state["line_number"]
            prompt = user_prompt(content)
            if prompt:
                baseline = prompt
                for task, entry in list(state["pending"].items()):
                    if entry["seen_line"] < state["line_number"]:
                        record_reply(state_root, state, task, prompt)
        else:
            completed = completed_round(kind, content, state)
            if not completed:
                continue
            task, number = completed
            if kind == "SYSTEM_MESSAGE":
                newly_completed.setdefault(completed, state["line_number"])
            previous = state["pending"].get(task)
            if previous and previous["round"] >= number:
                continue
            if can_capture(state_root, state, task, number):
                state["pending"][task] = {"round": number, "seen_line": state["line_number"], "baseline_user_input": baseline}

    if live_input:
        for task, entry in list(state["pending"].items()):
            prior_input = entry.get("baseline_user_input")
            if prior_input is not None and live_input != prior_input:
                record_reply(state_root, state, task, live_input)

    summaries = []
    for (task, number), seen_line in newly_completed.items():
        if seen_line < last_user_line or number in state["announced"].get(task, []):
            continue
        try:
            meta = read_meta(state_root / "tasks" / task / "meta.json")
            if not meta or (meta.get("status") in ("starting", "running") and number == int(meta.get("round", 1))):
                continue
            summary = format_wakeup_summary(task, number, meta)
        except (OSError, TypeError, ValueError):
            continue
        summaries.append(summary)
        state["announced"].setdefault(task, []).append(number)
    return summaries


def update_state(state_path, conversation_id, transcript_path, state_root, live_input):
    from telemetry import atomic_output, read_meta, utc_now

    state = read_meta(state_path) or {}
    if state and state.get("conversation_id") != conversation_id:
        # The sanitized names can collide. Share the lock but keep a separate cursor.
        digest = hashlib.sha256(conversation_id.encode("utf-8", "surrogatepass")).hexdigest()
        state_path = state_path.with_name(f"{state_path.stem[:55]}~{digest}.json")
        state = read_meta(state_path) or {}
    state.setdefault("announced", {})

    transcript = Path(transcript_path).expanduser()
    info = transcript.stat()
    if not stat.S_ISREG(info.st_mode):
        return
    if (
        transcript_path == state.get("transcript_path")
        and info.st_ino == state.get("transcript_ino")
        and info.st_size == state.get("transcript_offset")
        and not state["pending"]
    ):
        return

    with transcript.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            transcript_path != state.get("transcript_path")
            or info.st_ino != state.get("transcript_ino")
            or info.st_size < state["transcript_offset"]
        ):
            state = {
                "conversation_id": conversation_id, "transcript_path": transcript_path, "transcript_ino": info.st_ino,
                "transcript_offset": 0, "line_number": 0, "bg_tasks": [], "pending": {},
                "recorded": state.get("recorded", {}), "announced": state["announced"], "expect_sync_result": False,
            }
        summaries = scan(stream, state_root, state, live_input)

    state["updated_at"] = utc_now()
    with atomic_output(state_path) as output:
        json.dump(state, output)
    return summaries


def main():
    from castor_platform import configure_stdio, file_lock
    from telemetry import default_state_root

    configure_stdio()
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or payload.get("hookEventName") not in (None, "PreInvocation"):
        return
    transcript_path = payload.get("transcriptPath")
    if not isinstance(transcript_path, str) or not transcript_path:
        return
    live_input = payload.get("lastUserInput")
    if not isinstance(live_input, str) or not live_input:
        live_input = None
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str):
        conversation_id = ""
    state_root = default_state_root()
    directory = state_root / "hook_state"
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / (safe_conversation_id(conversation_id) + ".json")
    # Keep overlapping invocations from advancing the same cursor independently.
    with file_lock(state_path.with_suffix(".lock")):
        summaries = update_state(state_path, conversation_id, transcript_path, state_root, live_input)
    if summaries:
        return {"injectSteps": [{"systemMessage": {
            "systemMessage": "", "metadata": {"user_markdown": "\n\n".join(summaries)},
        }}]}


if __name__ == "__main__":
    try:
        print(json.dumps(main() or {}))
    except Exception as exc:
        print(f"codex-castor hook: {type(exc).__name__}", file=sys.stderr)
        print("{}")
