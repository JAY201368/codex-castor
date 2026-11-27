"""Task telemetry, shared by the MCP server and the analytics CLI."""

from __future__ import annotations

import csv
import datetime as dt
import fcntl
import json
import os
import re
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
VERDICTS = ("accepted", "rework", "partial", "rejected", "abandoned")
FEEDBACK_FIELDS = ("rating", "verdict", "tags", "notes")
TOTAL_FIELDS = ("task_summary", "rework_count", "total_duration_seconds", "total_usage")
CSV_FIELDS = (
    "task_id", "project", "task_summary", "created_at", "finished_at", "status",
    "model", "reasoning_effort", "rounds", "rework_count", "total_duration_seconds",
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
    "total_tokens", "rating", "verdict", "tags", "notes", "first_user_prompt",
)
LOCK = threading.Lock()


def default_state_root() -> Path:
    return Path(os.environ.get("CODEX_CASTOR_STATE_DIR", Path.home() / ".gemini" / "codex-castor"))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def transaction(state_root: Path):
    """Serialize read-modify-write across MCP threads and local CLI processes."""
    with LOCK:
        state_root.mkdir(parents=True, exist_ok=True)
        with (state_root / ".telemetry.lock").open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            yield


@contextmanager
def atomic_output(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    )
    temp = Path(stream.name)
    try:
        with stream:
            yield stream
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_meta(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_meta(path: Path, meta: dict) -> None:
    with atomic_output(path) as stream:
        json.dump(meta, stream, ensure_ascii=False, indent=2)


def normalize_usage(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    normalized = {key: usage[key] if type(usage.get(key)) is int else 0 for key in TOKEN_FIELDS}
    if type(usage.get("total_tokens")) is not int:
        normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    return normalized


def sum_usage(rounds) -> dict | None:
    usages = [normalize_usage(item.get("usage")) for item in rounds]
    known = [usage for usage in usages if usage is not None]
    return {key: sum(usage[key] for usage in known) for key in TOKEN_FIELDS} if known else None


def round_usage(meta: dict) -> dict | None:
    usage = meta.get("usage")
    baseline = meta.get("_rollout_usage_baseline")
    if isinstance(usage, dict) and baseline and not meta.get("_turn_usage_reported"):
        # The fallback rollout counter is cumulative within a Codex session.
        # A counter reset starts a new baseline.
        current_total = normalize_usage(usage)["total_tokens"]
        previous_total = normalize_usage(baseline)["total_tokens"]
        if current_total >= previous_total:
            usage = {key: max(0, value - baseline.get(key, 0)) for key, value in usage.items()}
    return normalize_usage(usage)


def extract_summary(text: str, max_len: int = 180) -> str:
    lines = text.splitlines()
    heading = re.compile(r"^\s*#{1,6}\s+(?:\d+\.\s*)?objective\s*#*\s*$", re.I)
    markup = re.compile(r"^[\s#*\-]+")
    first = ""
    in_objective = False
    fence = ""
    comment = False
    for line in lines:
        emphasis = re.fullmatch(r"(\*{1,3})(\S.*?)(?<!\s)\1", line.strip())
        content = emphasis[2] if emphasis else markup.sub("", line)
        clean = " ".join(content.split())
        first = first or clean
        stripped = line.strip()
        if comment or stripped.startswith("<!--"):
            comment = "-->" not in stripped
            continue
        if fence:
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        if heading.fullmatch(line):
            in_objective = True
        elif in_objective:
            if re.match(r"^\s*#{1,6}\s", line):
                break
            if clean and not re.fullmatch(r"(?:_\s*){3,}", clean):
                first = clean
                break
    if len(first) <= max_len:
        return first
    if max_len <= 0:
        return ""
    head = first[:max_len - 1]
    if " " in head and not head.endswith(" ") and first[max_len - 1] != " ":
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def prompt_summary(path: Path) -> str:
    try:
        return extract_summary(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return ""


_HEADING = re.compile(r"^#{1,6}\s+(.+?)(?:\s+#+)?$")
_GENERIC = re.compile(
    r"^(?:\d+[.)]\s*)?(objective|goal|summary|overview|context|background|specification|"
    r"requirements|task|prompt|description|feedback|instructions|scope)\s*[:：]?$", re.I,
)
_TITLE_PREFIX = re.compile(
    r"^(?:specification|spec|task(?:\s+[\w-]+)?|from-scratch redesign|redesign|goal|plan):\s*", re.I,
)


def _prompt_lines(text):
    fence = ""
    comment = False
    for raw in text.splitlines():
        line = raw.strip()
        if fence:
            if line.startswith(fence) and not line.strip(fence[0]):
                fence = ""
            continue
        if comment:
            _, end, line = line.partition("-->")
            comment = not end
        while "<!--" in line:
            before, _, rest = line.partition("<!--")
            _, end, after = rest.partition("-->")
            comment = not end
            line = before + after
        line = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", line)
        if marker:
            fence = marker[1]
        elif line and not re.fullmatch(r"[-*_\s]{3,}", line):
            yield line


def _plain_text(line):
    line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    return " ".join(line.replace("*", "").replace("`", "").split())


def _shorten(text, limit):
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if " " in head and text[limit - 1] != " " and not head.endswith(" "):
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def extract_title(text: str, *, task_id: str = "", max_len: int = 68) -> str:
    first = objective = ""
    in_objective = False
    for line in _prompt_lines(text):
        heading = _HEADING.match(line)
        if heading:
            title = _TITLE_PREFIX.sub("", _plain_text(heading[1]))
            generic = _GENERIC.fullmatch(title)
            if not first and re.match(r"^#{1,2}\s", line) and title and not generic:
                return _shorten(title, max_len)
            in_objective = bool(generic and generic[1].lower() == "objective")
        else:
            body = _plain_text(line)
            first = first or body
            if in_objective and body:
                objective = body
                break
    title = objective or first
    if title:
        simple = " ".join(re.sub(r"\([^()]*\)|（[^（）]*）", "", title).split())
        if len(simple) >= 18:
            title = simple
        title = re.sub(r"^perform an independent, thorough, read-only (root-cause analysis\b\s*)?",
                       lambda m: "Root-cause analysis " if m[1] else "Root-cause analysis: ", title, flags=re.I)
        title = re.sub(r"^investigate and diagnose why ", "Diagnose why ", title, flags=re.I)
        title = re.sub(r"^implement the approved ", "Implement ", title, flags=re.I)
        for boundary in re.finditer(r"[:：。]|\.(?=\s|$)", title):
            if 20 <= boundary.start() <= max_len:
                title = title[:boundary.start()].rstrip()
                break
    else:
        title = re.sub(r"^\d{8}-\d{6}-(?=\w)", "", task_id)
        title = re.sub(r"[-_]+", " ", title).strip()
        title = re.sub(r"^(feat|diag|impl) ", lambda m: {
            "feat": "Add ", "diag": "Diagnose ", "impl": "Implement ",
        }[m[1].lower()], title, flags=re.I)
    return _shorten(re.sub(r"^[a-z]", lambda m: m[0].upper(), title), max_len)


def _read_prompt(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def prompt_title(path: Path, *, task_id: str = "", max_len: int = 68) -> str:
    return extract_title(_read_prompt(path), task_id=task_id, max_len=max_len)


def extract_prompt_preview(text: str, *, max_len: int = 220) -> str:
    lines = []
    for index, line in enumerate(_prompt_lines(text)):
        heading = _HEADING.match(line)
        if heading:
            line = heading[1]
            if (index == 0 and re.match(r"^#{1,2}\s", heading[0])) or _GENERIC.fullmatch(_plain_text(line)):
                continue
        clean = _plain_text(line)
        if clean:
            lines.append(clean)
        if len(lines) == 3:
            break
    return _shorten(" ".join(lines), max_len)


def prompt_preview(path: Path, *, max_len: int = 220) -> str:
    return extract_prompt_preview(_read_prompt(path), max_len=max_len)


def jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except FileNotFoundError:
        return


def merge_feedback(*sources) -> dict:
    """Merge fields independently and keep the latest recorded prompt for each round."""
    result = {}
    prompts = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        result.update({key: source[key] for key in FEEDBACK_FIELDS if key in source})
        if source.get("updated_at"):
            result["updated_at"] = max(result.get("updated_at", ""), source["updated_at"])
        for item in source.get("user_first_prompts") or ():
            if not isinstance(item, dict) or type(item.get("round")) is not int or not item.get("prompt"):
                continue
            prior = prompts.get(item["round"], {})
            if (item.get("recorded_at") or "") >= (prior.get("recorded_at") or ""):
                prompts[item["round"]] = item
    if prompts:
        result["user_first_prompts"] = [prompts[number] for number in sorted(prompts)]
    return result


def merge_stored_feedback(ledger: dict | None, metadata: dict | None) -> dict:
    stored = sorted((ledger or {}, metadata or {}), key=lambda item: item.get("updated_at") or "")
    return merge_feedback(*stored)


def round_prompts(rounds: list) -> dict:
    return {"user_first_prompts": [
        {"round": item["round"], "prompt": item["user_first_prompt"], "recorded_at": item.get("user_first_prompt_at")}
        for item in rounds if item.get("user_first_prompt")
    ]}


def apply_round_prompts(rounds: list, feedback: dict) -> list:
    prompts = {item["round"]: item for item in feedback.get("user_first_prompts", [])}
    return [
        {**item, "user_first_prompt": prompts[item["round"]]["prompt"],
         "user_first_prompt_at": prompts[item["round"]].get("recorded_at")}
        if item["round"] in prompts else item
        for item in rounds
    ]


def round_record(number, kind, prompt_file: Path, meta: dict, **overrides) -> dict:
    return {
        "round": number,
        "kind": kind,
        "started_at": meta.get("round_started_at") or (meta.get("created_at") if number == 1 else None),
        "finished_at": meta.get("finished_at"),
        "status": meta.get("status"),
        "exit_code": meta.get("exit_code"),
        "duration_seconds": meta.get("duration_seconds"),
        "duration_estimated": False,
        "model": meta.get("effective_model") or meta.get("model"),
        "reasoning_effort": meta.get("effective_reasoning_effort") or meta.get("reasoning_effort"),
        "model_source": meta.get("model_source"),
        "reasoning_effort_source": meta.get("reasoning_effort_source"),
        "prompt_file": str(prompt_file),
        "prompt_summary": prompt_summary(prompt_file),
        "usage": round_usage(meta),
        "user_first_prompt": None,
        "user_first_prompt_at": None,
        **overrides,
    }


def modification_time(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def iso_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def historical_round(task_dir: Path, meta: dict, number: int, latest: int) -> dict:
    prompt = task_dir / ("spec.md" if number == 1 else f"feedback-r{number}.md")
    kind = "dispatch" if number == 1 else "resume"
    if number == 1 and not prompt.is_file():
        resume_prompt = task_dir / "feedback-r1.md"
        if resume_prompt.is_file():
            prompt, kind = resume_prompt, "resume"

    events = task_dir / f"events-r{number}.jsonl"
    usage = None
    event_status = None
    for event in jsonl(events):
        if event.get("type") == "turn.completed":
            event_status = "completed"
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        elif event.get("type") == "turn.failed":
            event_status = "failed"

    if number == latest:
        duration = meta.get("duration_seconds") if meta.get("status") not in ("starting", "running") else None
        return round_record(
            number, kind, prompt, meta, duration_seconds=duration,
            usage=normalize_usage(usage) if usage is not None else round_usage(meta),
        )

    started = modification_time(prompt)
    finished = modification_time(events)
    duration = round(max(0.0, finished - started), 2) if started is not None and finished is not None else None
    return round_record(
        number, kind, prompt, meta, started_at=iso_timestamp(started),
        finished_at=iso_timestamp(finished), status=event_status,
        exit_code=None, duration_seconds=duration, duration_estimated=duration is not None,
        usage=normalize_usage(usage),
    )


def update_totals(meta: dict, task_dir: Path) -> None:
    rounds = meta.get("rounds") or []
    durations = [item["duration_seconds"] for item in rounds if item.get("duration_seconds") is not None]
    summary = meta.get("task_summary") or prompt_summary(task_dir / "spec.md")
    if not summary and rounds:
        summary = rounds[0].get("prompt_summary") or ""
    meta.update(
        task_summary=summary,
        rework_count=max(0, len(rounds) - 1),
        total_duration_seconds=round(float(sum(durations)), 2) if durations else None,
        total_usage=sum_usage(rounds),
    )


def reconstruct_task_record(task_dir: Path, meta: dict) -> dict:
    latest = max(1, int(meta.get("round", 1)))
    existing = {item["round"]: item for item in meta.get("rounds") or []}
    rounds = [
        existing[number] if number in existing else historical_round(task_dir, meta, number, latest)
        for number in range(1, latest + 1)
    ]
    feedback = merge_feedback(round_prompts(rounds), meta.get("feedback"))
    rounds = apply_round_prompts(rounds, feedback)
    data = {**meta, "rounds": rounds}
    update_totals(data, task_dir)
    workdir = meta.get("requested_workdir") or meta.get("effective_workdir")
    return {
        "task_id": meta.get("task_id") or task_dir.name,
        "project": Path(workdir).name if workdir else None,
        "workdir": workdir,
        **{key: meta.get(key) for key in ("created_at", "updated_at", "finished_at", "status")},
        **{key: rounds[-1].get(key) for key in ("model", "reasoning_effort", "model_source", "reasoning_effort_source")},
        **{key: data[key] for key in TOTAL_FIELDS},
        "round_count": len(rounds),
        "duration_estimated": any(item.get("duration_estimated") for item in rounds),
        "rounds": rounds,
        "feedback": feedback,
        "source": "backfill",
    }


def ordered_records(records) -> list:
    return sorted(records, key=lambda item: (item.get("created_at") or "", item["task_id"]), reverse=True)


def load_records(state_root: Path) -> list:
    return ordered_records(item for item in jsonl(state_root / "telemetry.jsonl") if item.get("task_id"))


def record_with_feedback(record: dict, prior: dict | None) -> dict:
    if prior:
        record["feedback"] = merge_stored_feedback(prior.get("feedback"), record["feedback"])
        record["rounds"] = apply_round_prompts(record["rounds"], record["feedback"])
    return record


def scan_records(state_root: Path, existing=()) -> list:
    records = {item["task_id"]: item for item in existing}
    for path in (state_root / "tasks").glob("*/meta.json"):
        try:
            meta = read_meta(path)
            if not meta:
                continue
            task_id = meta.get("task_id") or path.parent.name
            prior = records.get(task_id)
            if prior and meta.get("status") in ("starting", "running"):
                continue
            record = reconstruct_task_record(path.parent, meta)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            print(f"codex-castor: skipping telemetry for {path.parent.name}: {exc}", file=sys.stderr)
            continue
        if prior:
            record["source"] = prior.get("source", "backfill")
        records[record["task_id"]] = record_with_feedback(record, prior)
    return ordered_records(records.values())


def load_or_build_records(state_root: Path) -> list:
    if (state_root / "telemetry.jsonl").is_file():
        return load_records(state_root)
    return scan_records(state_root)


def filter_records(records, *, model=None, effort=None, project=None, since=None) -> list:
    return [
        item for item in records
        if (model is None or model == (item.get("model") or "unknown"))
        and (effort is None or effort == (item.get("reasoning_effort") or "unknown"))
        and (project is None or project == item.get("project"))
        and (since is None or (item.get("created_at") or "")[:10] >= since)
    ]


def ratio(numerator, denominator) -> float:
    return numerator / denominator if denominator else 0.0


def average(values) -> float | None:
    return sum(values) / len(values) if values else None


def metrics(records: list) -> dict:
    count = len(records)
    completed = [item for item in records if item.get("status") == "completed"]
    reworks = [item.get("rework_count", 0) for item in records]
    durations = [item["total_duration_seconds"] for item in records if item.get("total_duration_seconds") is not None]
    usage = sum_usage({"usage": item.get("total_usage")} for item in records) or dict.fromkeys(TOKEN_FIELDS, 0)
    feedback = [item.get("feedback") or {} for item in records]
    ratings = [item["rating"] for item in feedback if type(item.get("rating")) is int and 1 <= item["rating"] <= 5]
    reworked = sum(value > 0 for value in reworks)
    return {
        "task_count": count,
        "completed_count": len(completed),
        "failed_count": sum(item.get("status") == "failed" for item in records),
        "cancelled_count": sum(item.get("status") == "cancelled" for item in records),
        "completion_rate": ratio(len(completed), count),
        "total_reworks": sum(reworks),
        "rework_task_count": reworked,
        "rework_rate": ratio(reworked, count),
        "first_pass_rate": ratio(sum(item.get("rework_count", 0) == 0 for item in completed), len(completed)),
        "avg_reworks": ratio(sum(reworks), count),
        "total_duration_seconds": round(sum(durations), 2),
        "avg_duration_seconds": average(durations),
        "total_tokens": usage["total_tokens"],
        **{f"total_{field}": usage[field] for field in TOKEN_FIELDS if field != "total_tokens"},
        "avg_total_tokens": average([
            normalize_usage(item["total_usage"])["total_tokens"]
            for item in records if isinstance(item.get("total_usage"), dict)
        ]),
        "rated_count": len(ratings),
        "avg_rating": average(ratings),
        "verdict_counts": {
            verdict: sum(item.get("verdict") == verdict for item in feedback) for verdict in VERDICTS
        },
    }


def is_counted_in_analytics(record: dict) -> bool:
    return record.get("status") != "failed"


def compute_analytics(records, *, model=None, effort=None, project=None, since=None) -> dict:
    records = filter_records(records, model=model, effort=effort, project=project, since=since)
    records = [item for item in records if is_counted_in_analytics(item)]
    groups = {}
    for item in records:
        key = (item.get("model") or "unknown", item.get("reasoning_effort") or "unknown")
        groups.setdefault(key, []).append(item)
    fields = (
        "task_count", "completed_count", "rework_rate", "first_pass_rate", "avg_reworks",
        "avg_duration_seconds", "avg_total_tokens", "total_tokens", "rated_count", "avg_rating",
    )
    by_model_effort = []
    for (name, level), items in sorted(groups.items(), key=lambda pair: (-len(pair[1]), *pair[0])):
        group = metrics(items)
        by_model_effort.append({
            "model": name, "reasoning_effort": level, **{key: group[key] for key in fields},
        })
    overall = metrics(records)
    overall.pop("avg_reworks")
    overall.pop("total_cache_write_input_tokens")
    return {"overall": overall, "by_model_effort": by_model_effort}


def latest_prompt(feedback: dict) -> str:
    prompts = feedback.get("user_first_prompts") or []
    latest = max(prompts, key=lambda item: (item.get("recorded_at") or "", item["round"]), default={})
    return latest.get("prompt", "")


def export_csv(records, csv_path: Path) -> None:
    with atomic_output(csv_path) as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in ordered_records(records):
            feedback = item.get("feedback") or {}
            writer.writerow({
                **item, **(item.get("total_usage") or {}), **feedback,
                "rounds": item["round_count"], "tags": ", ".join(feedback.get("tags") or []),
                "first_user_prompt": latest_prompt(feedback),
            })


def format_number(value, precision=0) -> str:
    return "—" if value is None else f"{value:,.{precision}f}"


def format_duration(value) -> str:
    return "—" if value is None else f"{value:,.2f}s"


def format_percent(value) -> str:
    return f"{value:.1%}"


def kpi_rows(overall: dict) -> list:
    verdicts = ", ".join(f"{key} {value:,}" for key, value in overall["verdict_counts"].items() if value) or "—"
    return [
        ("Tasks", format_number(overall["task_count"])),
        ("Completed", format_number(overall["completed_count"])),
        ("Failed", format_number(overall["failed_count"])),
        ("Cancelled", format_number(overall["cancelled_count"])),
        ("Completion rate", format_percent(overall["completion_rate"])),
        ("First-pass rate", format_percent(overall["first_pass_rate"])),
        ("Reworks", format_number(overall["total_reworks"])),
        ("Tasks reworked", format_number(overall["rework_task_count"])),
        ("Rework rate", format_percent(overall["rework_rate"])),
        ("Total duration", format_duration(overall["total_duration_seconds"])),
        ("Avg duration", format_duration(overall["avg_duration_seconds"])),
        ("Total tokens", format_number(overall["total_tokens"])),
        ("Input / cached input", f'{overall["total_input_tokens"]:,} / {overall["total_cached_input_tokens"]:,}'),
        ("Output / reasoning output", f'{overall["total_output_tokens"]:,} / {overall["total_reasoning_output_tokens"]:,}'),
        ("Avg tokens", format_number(overall["avg_total_tokens"])),
        ("Rated tasks", format_number(overall["rated_count"])),
        ("Avg rating", format_number(overall["avg_rating"], 2)),
        ("Verdicts", verdicts),
    ]


MATRIX_COLUMNS = (
    "Model", "Effort", "Tasks", "Completed", "First pass", "Rework rate",
    "Avg reworks", "Avg time", "Avg tokens", "Total tokens", "Rated", "Avg rating",
)
RECENT_COLUMNS = (
    "Task", "Project", "Summary", "Model · effort", "Rounds / reworks",
    "Duration", "Total tokens", "Rating / verdict", "Notes / first reply",
)


def matrix_rows(groups) -> list:
    return [
        (
            item["model"], item["reasoning_effort"], format_number(item["task_count"]),
            format_number(item["completed_count"]), format_percent(item["first_pass_rate"]),
            format_percent(item["rework_rate"]), format_number(item["avg_reworks"], 2),
            format_duration(item["avg_duration_seconds"]), format_number(item["avg_total_tokens"]),
            format_number(item["total_tokens"]), format_number(item["rated_count"]),
            format_number(item["avg_rating"], 2),
        )
        for item in groups
    ]


def recent_rows(records) -> list:
    rows = []
    for item in records:
        feedback = item.get("feedback") or {}
        prompt = latest_prompt(feedback)
        duration = format_duration(item.get("total_duration_seconds"))
        if item.get("duration_estimated"):
            duration = "≈" + duration
        rows.append((
            item["task_id"], item.get("project") or "—", item.get("task_summary") or "—",
            f'{item.get("model") or "unknown"} · {item.get("reasoning_effort") or "unknown"}',
            f'{item["round_count"]} / {item["rework_count"]}', duration,
            format_number((item.get("total_usage") or {}).get("total_tokens")),
            f'{feedback.get("rating") or "—"} / {feedback.get("verdict") or "—"}',
            " · ".join(filter(None, [feedback.get("notes"), f"First reply: {prompt}" if prompt else None])) or "—",
        ))
    return rows


def markdown_cell(value) -> str:
    text = " ".join(str(value).split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\|`*_[\]])", r"\\\1", text)


def markdown_table(columns, rows) -> str:
    return "\n".join(
        "| " + " | ".join(markdown_cell(value) for value in row) + " |"
        for row in (columns, ["---"] * len(columns), *rows)
    )


def export_markdown(records, md_path: Path) -> None:
    records = ordered_records(records)
    analytics = compute_analytics(records)
    with atomic_output(md_path) as stream:
        stream.write("\n\n".join((
            "# Codex Castor Evaluation Report",
            f"Generated {utc_now()}",
            "## Overall KPIs",
            markdown_table(("Metric", "Value"), kpi_rows(analytics["overall"])),
            "## Model × Reasoning Effort Performance Matrix",
            markdown_table(MATRIX_COLUMNS, matrix_rows(analytics["by_model_effort"])),
            "## Recent Tasks Log",
            markdown_table(RECENT_COLUMNS, recent_rows(records[:50])),
        )) + "\n")


def _sync_exports(state_root: Path, records) -> None:
    records = ordered_records(records)
    with atomic_output(state_root / "telemetry.jsonl") as stream:
        for item in records:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    for exporter, name in ((export_csv, "telemetry.csv"), (export_markdown, "report.md")):
        try:
            exporter(records, state_root / name)
        except OSError as exc:
            print(f"codex-castor: could not write {name}: {exc}", file=sys.stderr)


def sync_exports(state_root: Path, records) -> None:
    with transaction(state_root):
        _sync_exports(state_root, records)


def backfill(state_root: Path) -> list:
    with transaction(state_root):
        records = scan_records(state_root, load_records(state_root))
        _sync_exports(state_root, records)
    return records


def _commit_record(state_root: Path, record: dict, records: dict) -> dict:
    record["source"] = "live"
    records[record["task_id"]] = record
    _sync_exports(state_root, records.values())
    return record


def upsert_task_record(state_root: Path, task_dir: Path, meta: dict) -> dict:
    with transaction(state_root):
        latest = read_meta(task_dir / "meta.json")
        # A feedback update may have committed after the caller saved its round.
        if latest and latest.get("round") == meta.get("round") and latest.get("status") == meta.get("status"):
            meta = latest
        records = {item["task_id"]: item for item in load_or_build_records(state_root)}
        record = reconstruct_task_record(task_dir, meta)
        return _commit_record(state_root, record_with_feedback(record, records.get(record["task_id"])), records)


def save_round_meta(state_root: Path, task_dir: Path, meta: dict) -> None:
    """Preserve feedback written while a round's in-memory snapshot was in use."""
    path = task_dir / "meta.json"
    with transaction(state_root):
        current = read_meta(path) or {}
        feedback = merge_feedback(meta.get("feedback"), current.get("feedback"))
        if feedback:
            meta["feedback"] = feedback
        if "rounds" in meta:
            meta["rounds"] = apply_round_prompts(meta["rounds"], feedback)
        write_meta(path, meta)


def record_feedback(state_root: Path, task_id: str, updates: dict) -> dict:
    if not isinstance(task_id, str) or not task_id or Path(task_id).name != task_id or task_id in (".", ".."):
        raise ValueError("A valid task_id is required")
    changes = {key: updates[key] for key in FEEDBACK_FIELDS if key in updates}
    if "rating" in changes and (type(changes["rating"]) is not int or not 1 <= changes["rating"] <= 5):
        raise ValueError("rating must be an integer from 1 to 5")
    if "verdict" in changes and changes["verdict"] not in VERDICTS:
        raise ValueError(f"verdict must be one of: {', '.join(VERDICTS)}")
    if "tags" in changes and (
        not isinstance(changes["tags"], list) or not all(isinstance(tag, str) for tag in changes["tags"])
    ):
        raise ValueError("tags must be an array of strings")
    if "notes" in changes and not isinstance(changes["notes"], str):
        raise ValueError("notes must be a string")
    if "round" in updates and (type(updates["round"]) is not int or updates["round"] < 1):
        raise ValueError("round must be a positive integer")
    prompt = updates.get("user_first_prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("user_first_prompt must be a string")

    task_dir = state_root / "tasks" / task_id
    path = task_dir / "meta.json"
    with transaction(state_root):
        meta = read_meta(path)
        if not meta:
            raise ValueError(f"Task not found: {task_id}")
        current_round = int(meta.get("round", 1))
        target = updates.get("round", current_round)
        if target > current_round:
            raise ValueError(f"Round {target} does not exist for task '{task_id}'")
        running = meta.get("status") in ("starting", "running")
        if prompt.strip() and target == current_round and running:
            raise ValueError(f"Round {target} has not finished for task '{task_id}'")
        records = {item["task_id"]: item for item in load_or_build_records(state_root)}
        prior = records.get(meta.get("task_id") or task_id)
        if running and prior:
            record = {**prior, "feedback": merge_stored_feedback(prior.get("feedback"), meta.get("feedback"))}
        else:
            record = record_with_feedback(reconstruct_task_record(task_dir, meta), prior)
        now = utc_now()
        changes["updated_at"] = now
        if prompt.strip():
            changes["user_first_prompts"] = [{"round": target, "prompt": prompt, "recorded_at": now}]
        feedback = merge_feedback(record["feedback"], changes)
        record.update(feedback=feedback, rounds=apply_round_prompts(record["rounds"], feedback))
        if running:
            history = meta.get("rounds") or [item for item in record["rounds"] if item["round"] < current_round]
            meta["rounds"] = apply_round_prompts(history, feedback)
        else:
            meta["rounds"] = record["rounds"]
        meta["feedback"] = feedback
        update_totals(meta, task_dir)
        write_meta(path, meta)
        _commit_record(state_root, record, records)
    return {
        "task_id": task_id,
        "feedback": record["feedback"],
        **{key: meta[key] for key in TOTAL_FIELDS if key != "task_summary"},
    }
