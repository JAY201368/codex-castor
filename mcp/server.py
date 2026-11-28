#!/usr/bin/env python3
"""Lightweight, zero-dependency MCP stdio server bridging Antigravity (Gemini)
to local Codex CLI with full permissions and concurrent background task support.

Transport: MCP stdio (newline-delimited JSON-RPC 2.0 on stdin/stdout; logs on stderr).
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Tuple

SERVER_NAME = "codex-castor"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", DEFAULT_PROTOCOL_VERSION, "2025-06-18", "2025-11-25")

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import telemetry
from castor_platform import WINDOWS, build_env, configure_stdio, executable_path, find_codex, pid_alive

if WINDOWS:
    from windows_process import CANCEL_EXIT_CODE
else:
    CANCEL_EXIT_CODE = None

SCHEMA_PATH = BASE_DIR / "report.schema.json"
MONITOR_URI = "ui://codex-castor/monitor.html"
MONITOR_MIME = "text/html;profile=mcp-app"
MONITOR_HTML_PATH = Path(os.environ.get("CODEX_CASTOR_MONITOR_HTML", str(BASE_DIR / "monitor.html")))
MCP_APPS_EXTENSION = "io.modelcontextprotocol/ui"

STATE_ROOT = telemetry.default_state_root()
TASKS_ROOT = STATE_ROOT / "tasks"
WORKTREES_ROOT = STATE_ROOT / "worktrees"
QUOTA_PATH = STATE_ROOT / "quota.json"
QUOTA_LOCK = threading.Lock()
QUOTA_REFRESH_DONE = threading.Condition(QUOTA_LOCK)
QUOTA_REFRESH_IN_FLIGHT = False
QUOTA_BOOTSTRAP_ATTEMPTED = False

STDOUT_LOCK = threading.Lock()
LOG_LEVEL_LOCK = threading.Lock()
CLIENT_LOG_LEVEL: Optional[str] = None
LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")
ACTIVE_LOCK = threading.Lock()
ACTIVE_BY_REQ_ID: Dict[Any, Dict[str, Any]] = {}
ACTIVE_BY_TASK_ID: Dict[str, Dict[str, Any]] = {}
CLAIMED_TASKS: set[str] = set()
MODEL_FIELDS = ("model", "reasoning_effort")
MODEL_RESULT_FIELDS = tuple(
    key for field in MODEL_FIELDS for key in (f"effective_{field}", f"{field}_source", f"config_{field}")
)
TOKEN_USAGE_FIELDS = telemetry.TOKEN_FIELDS


def log_err(msg: str) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"[{ts}] [codex-castor] {msg}\n")
    sys.stderr.flush()


def send_json(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with STDOUT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def send_result(req_id: Any, result: Dict[str, Any]) -> None:
    send_json({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    send_json({"jsonrpc": "2.0", "id": req_id, "error": err})


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_codex_bin() -> str:
    env_path = build_env().get("PATH")
    executable = executable_path(os.environ.get("CODEX_BIN")) or find_codex(env_path)
    if executable:
        return executable
    command = "codex.exe" if WINDOWS else "codex"
    raise RuntimeError(f"codex executable not found. Set CODEX_BIN or add {command} to PATH.")


def sanitize_task_id(raw: Optional[str]) -> str:
    if raw:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip()).strip("-.")
        if not cleaned:
            raise ValueError("task_id contains no valid characters")
        cleaned = cleaned[:80].rstrip(".") if WINDOWS else cleaned[:80]
        if WINDOWS and re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", cleaned.split(".", 1)[0]):
            raise ValueError("task_id is a reserved Windows device name")
        return cleaned
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def read_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def run_git(args: List[str], cwd: Path, timeout: int = 30) -> Tuple[int, str, str]:
    try:
        git = executable_path("git.exe") if WINDOWS else "git"
        if not git:
            return 1, "", "git.exe not found on PATH"
        # Git commands must never inherit the open MCP request stream.
        proc = subprocess.run(
            [git, *args],
            cwd=str(cwd),
            env=build_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8" if WINDOWS else None,
            errors="replace" if WINDOWS else None,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def detect_git_root(workdir: Path) -> Optional[Path]:
    rc, out, _ = run_git(["rev-parse", "--show-toplevel"], workdir)
    if rc == 0 and out:
        return Path(out).resolve()
    return None


def codex_home_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def nonempty_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    return None


def quota_cache_path() -> Path:
    return STATE_ROOT / QUOTA_PATH.name


def format_quota_window_label(duration_mins: Optional[int], slot: str) -> str:
    if type(duration_mins) is not int or duration_mins <= 0:
        return slot
    if duration_mins % 1440 == 0:
        return f"{duration_mins // 1440}d"
    if duration_mins % 60 == 0:
        return f"{duration_mins // 60}h"
    return f"{duration_mins}m"


def normalize_quota_payload(payload: Any, *, updated_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None

    def number(value: Any) -> bool:
        return type(value) is int or (type(value) is float and math.isfinite(value))

    def extract_windows(bucket: Any) -> List[Dict[str, Any]]:
        windows = []
        if not isinstance(bucket, dict):
            return windows
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not isinstance(window, dict) or not number(window.get("usedPercent")):
                continue
            used = max(0, min(100, window["usedPercent"]))
            remaining = round(100.0 - used, 1)
            duration_mins = window.get("windowDurationMins")
            if type(duration_mins) is not int or duration_mins <= 0:
                duration_mins = None
            resets_at = window.get("resetsAt")
            windows.append({
                "slot": slot,
                "label": format_quota_window_label(duration_mins, slot),
                "used_percent": int(used) if used == int(used) else used,
                "remaining_percent": int(remaining) if remaining == int(remaining) else remaining,
                "window_duration_mins": duration_mins,
                "resets_at": int(resets_at) if number(resets_at) else None,
            })
        return windows

    candidates = []
    by_limit_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict) and by_limit_id:
        candidates.append(("codex", by_limit_id.get("codex")))
        candidates.extend((limit_id, bucket) for limit_id, bucket in by_limit_id.items() if limit_id != "codex")
    candidates.append(("codex", payload.get("rateLimits")))
    for limit_id, bucket in candidates:
        windows = extract_windows(bucket)
        if windows:
            windows.sort(key=lambda w: (w["window_duration_mins"] is None, w["window_duration_mins"] or 0,
                                        0 if w["slot"] == "primary" else 1))
            return {
                "updated_at": updated_at or utc_now_iso(),
                "limit_id": nonempty_string(bucket.get("limitId")) or limit_id,
                "plan_type": nonempty_string(bucket.get("planType")),
                "windows": windows,
            }
    return None


def parse_codex_config_toml(path: Path) -> Tuple[Optional[str], Optional[str]]:
    if not path.is_file():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    try:
        import tomllib

        config = tomllib.loads(text)
        return nonempty_string(config.get("model")), nonempty_string(config.get("model_reasoning_effort"))
    except (ImportError, ValueError):
        config = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                break
            match = re.match(r"""^(model|model_reasoning_effort)\s*=\s*["']([^"']+)["']""", line)
            if match:
                config[match[1]] = match[2]
        return nonempty_string(config.get("model")), nonempty_string(config.get("model_reasoning_effort"))


def resolve_config_defaults(workdir: Path) -> Dict[str, Optional[str]]:
    user = parse_codex_config_toml(codex_home_dir() / "config.toml")
    project = parse_codex_config_toml((detect_git_root(workdir) or workdir) / ".codex" / "config.toml")
    defaults: Dict[str, Optional[str]] = {}
    for field, project_value, user_value in zip(MODEL_FIELDS, project, user):
        defaults[f"config_{field}"] = project_value or user_value
        defaults[f"config_{field}_source"] = (
            "project_config" if project_value else "user_config" if user_value else None
        )
    return defaults


def set_round_model(
    meta: Dict[str, Any], args: Dict[str, Any], workdir: Path, cmd: List[str],
) -> None:
    meta.update(resolve_config_defaults(workdir))
    for field in MODEL_FIELDS:
        override = nonempty_string(args.get(field))
        effective = f"effective_{field}"
        source = f"{field}_source"
        if override:
            meta[field] = override
            meta[effective] = override
            meta[source] = "override"
            if field == "model":
                cmd.extend(["-m", override])
            else:
                cmd.extend(["-c", f"model_reasoning_effort={json.dumps(override, ensure_ascii=False)}"])
        elif meta.get(effective) or meta.get(field):
            # Older task metadata only records the last requested override.
            meta[effective] = meta.get(effective) or meta[field]
            if meta.get(source) not in ("runtime_rollout", "override"):
                meta[source] = "previous_round"
        else:
            meta.setdefault(field, None)
            meta[effective] = meta[f"config_{field}"]
            meta[source] = meta[f"config_{field}_source"] or "unknown"


def find_rollout_file(session_id: str) -> Optional[Path]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", session_id):
        return None
    sessions = codex_home_dir() / "sessions"
    today = datetime.date.today()
    utc_today = datetime.datetime.now(datetime.timezone.utc).date()
    recent = {day - datetime.timedelta(days=delta) for day in (today, utc_today) for delta in (0, 1)}
    pattern = f"rollout-*{session_id}*.jsonl"
    try:
        matches = [path for day in recent for path in (sessions / day.strftime("%Y/%m/%d")).glob(pattern)
                   if path.is_file()]
        if not matches:
            matches = [path for path in sessions.glob(f"*/*/*/{pattern}") if path.is_file()]
        return max(matches, default=None)
    except OSError:
        return None


def rollout_start_position(session_id: str) -> Optional[Tuple[str, int, int]]:
    path = find_rollout_file(session_id)
    if path is not None:
        try:
            stat = path.stat()
            return str(path), stat.st_ino, stat.st_size
        except OSError:
            pass
    return None


def rollout_lines_from_end(stream: BinaryIO, start: int, end: int) -> Iterator[bytes]:
    pending = b""
    while end > start:
        chunk_start = max(start, end - 65536)
        stream.seek(chunk_start)
        pending, *lines = (stream.read(end - chunk_start) + pending).split(b"\n")
        yield from reversed(lines)
        end = chunk_start
    if pending:
        yield pending


def probe_rollout_runtime(
    session_id: str, *, round_start: Optional[Tuple[str, int, int]] = None,
) -> Optional[Dict[str, Any]]:
    path = find_rollout_file(session_id)
    if path is None:
        return None
    turn = None
    token_count = None
    found = False
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            start = 0
            if round_start and round_start[0] == str(path) and round_start[1] == stat.st_ino:
                if stat.st_size >= round_start[2]:
                    start = round_start[2]
            for line in rollout_lines_from_end(stream, start, stat.st_size):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                found = True
                payload = event.get("payload")
                if turn is None and event.get("type") == "turn_context":
                    turn = payload if isinstance(payload, dict) else {}
                elif token_count is None and event.get("type") == "event_msg" and isinstance(payload, dict):
                    if payload.get("type") == "token_count":
                        token_count = payload
                if turn is not None and token_count is not None:
                    break
    except OSError:
        return None
    if not found:
        return None

    turn = turn or {}
    mode = turn.get("collaboration_mode")
    settings = mode.get("settings") if isinstance(mode, dict) else None
    settings = settings if isinstance(settings, dict) else {}
    info = (token_count or {}).get("info")
    total = info.get("total_token_usage") if isinstance(info, dict) else None
    usage = {}
    if isinstance(total, dict):
        usage = {key: total[key] for key in TOKEN_USAGE_FIELDS if type(total.get(key)) is int}
    return {
        "model": nonempty_string(turn.get("model")) or nonempty_string(settings.get("model")),
        "reasoning_effort": nonempty_string(turn.get("effort")) or nonempty_string(settings.get("reasoning_effort")),
        "usage": usage or None,
    }


def refresh_rollout_metadata(meta: Dict[str, Any]) -> bool:
    session_id = meta.get("session_id")
    if not isinstance(session_id, str):
        return False
    runtime = probe_rollout_runtime(session_id, round_start=meta.get("_rollout_round_start"))
    if not runtime:
        return False
    updates = {}
    for field in MODEL_FIELDS:
        if runtime[field]:
            updates[f"effective_{field}"] = runtime[field]
            updates[f"{field}_source"] = "runtime_rollout"
    if runtime["usage"] is not None and not meta.get("_turn_usage_reported"):
        updates["usage"] = runtime["usage"]
    changed = any(meta.get(key) != value for key, value in updates.items())
    meta.update(updates)
    return changed


def terminate_process_group(proc: Optional[subprocess.Popen], pid: Optional[int] = None, job_name=None) -> bool:
    if WINDOWS:
        from windows_process import terminate_recorded_job

        if proc is not None:
            return proc.castor_job.terminate()
        return terminate_recorded_job(pid, job_name)

    target_pid = proc.pid if proc is not None else pid
    if not target_pid or target_pid <= 0:
        return False
    try:
        pgid = os.getpgid(target_pid)
    except Exception:
        pgid = target_pid

    sent = False
    try:
        os.killpg(pgid, signal.SIGTERM)
        sent = True
    except ProcessLookupError:
        return False
    except Exception:
        try:
            os.kill(target_pid, signal.SIGTERM)
            sent = True
        except Exception:
            return False

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return True
        if proc is None and not pid_alive(target_pid):
            return True
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(target_pid, signal.SIGKILL)
        except Exception:
            pass
    return sent


def launch_codex(args, *, cwd=None, input_path=None, stderr=subprocess.DEVNULL):
    command = [resolve_codex_bin(), *args]
    options = dict(cwd=cwd, stdout=subprocess.PIPE, stderr=stderr, env=build_env(), bufsize=0)
    if WINDOWS:
        from windows_process import launch_in_job

        return launch_in_job(command, input_path=input_path, **options)
    if input_path is None:
        return subprocess.Popen(command, stdin=subprocess.PIPE, start_new_session=True, **options)
    with input_path.open("rb") as prompt:
        return subprocess.Popen(command, stdin=prompt, start_new_session=True, **options)


class PipeReader:
    """Read chunks without blocking the caller's timeout or relying on selectable pipes."""

    def __init__(self, stream):
        self.stream = stream
        self.chunks = queue.Queue(maxsize=16)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()

    def pump(self):
        while not self.stopped.is_set():
            try:
                chunk = self.stream.read(65536)
            except (OSError, ValueError) as exc:
                chunk = exc
            while not self.stopped.is_set():
                try:
                    self.chunks.put(chunk, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if not chunk or isinstance(chunk, Exception):
                return

    def read(self, timeout):
        try:
            chunk = self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(chunk, Exception):
            raise OSError(f"Could not read Codex stdout: {chunk}") from chunk
        return chunk

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=0.25)


def query_codex_quota(timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    proc = launch_codex(["app-server", "--listen", "stdio://"])
    assert proc.stdin is not None and proc.stdout is not None
    reader = PipeReader(proc.stdout)
    pending = b""

    def send(message: Dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")

    def read_reply(request_id: int) -> Any:
        nonlocal pending
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server quota query timed out")
            # A readable pipe can contain a partial line. Read chunks so the deadline still applies.
            line, separator, rest = pending.partition(b"\n")
            if separator:
                pending = rest
                try:
                    reply = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(reply, dict) and reply.get("id") == request_id:
                    if "error" in reply:
                        raise RuntimeError(f"Codex app-server request {request_id}: {reply['error']}")
                    return reply.get("result")
                continue

            chunk = reader.read(remaining)
            if chunk is None:
                raise TimeoutError("Codex app-server quota query timed out")
            if not chunk:
                raise RuntimeError("Codex app-server closed stdout before replying")
            pending += chunk

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": SERVER_NAME, "version": SERVER_VERSION,
        }}})
        read_reply(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return normalize_quota_payload(read_reply(2))
    finally:
        proc.stdin.close()
        if proc.poll() is None:
            terminate_process_group(proc)
        try:
            proc.wait(timeout=2)
        finally:
            reader.close()
            proc.stdout.close()


def load_cached_quota() -> Optional[Dict[str, Any]]:
    data = read_json_safe(quota_cache_path())
    if data and isinstance(data.get("windows"), list) and data["windows"]:
        return data
    return None


def refresh_codex_quota(*, wait: bool = False, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    global QUOTA_REFRESH_IN_FLIGHT

    def release() -> None:
        global QUOTA_REFRESH_IN_FLIGHT
        with QUOTA_REFRESH_DONE:
            QUOTA_REFRESH_IN_FLIGHT = False
            QUOTA_REFRESH_DONE.notify_all()

    def worker() -> Optional[Dict[str, Any]]:
        try:
            quota = query_codex_quota(timeout=timeout)
            if quota is not None:
                telemetry.write_meta(quota_cache_path(), quota)
                return quota
        except Exception as exc:
            log_err(f"Could not refresh Codex quota: {exc}")
        finally:
            release()
        return None

    with QUOTA_REFRESH_DONE:
        if QUOTA_REFRESH_IN_FLIGHT:
            if not wait:
                return None
            # Share a refresh that already started, including its process teardown.
            QUOTA_REFRESH_DONE.wait_for(lambda: not QUOTA_REFRESH_IN_FLIGHT, timeout=timeout + 5.0)
            return load_cached_quota()
        QUOTA_REFRESH_IN_FLIGHT = True

    if wait:
        return worker() or load_cached_quota()
    try:
        threading.Thread(target=worker, name="codex-quota", daemon=True).start()
    except RuntimeError as exc:
        release()
        log_err(f"Could not refresh Codex quota: {exc}")
    return None


def collect_git_summary(workdir: Path, base_commit: Optional[str] = None) -> Dict[str, Any]:
    git_root = detect_git_root(workdir)
    if git_root is None:
        return {"is_git_repo": False}

    _, branch, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workdir)
    _, head, _ = run_git(["rev-parse", "--short", "HEAD"], workdir)
    _, status_short, _ = run_git(["status", "--short"], workdir)

    diff_stat = ""
    if base_commit:
        rc, out, _ = run_git(["diff", "--stat", base_commit], workdir)
        if rc == 0:
            diff_stat = out
    else:
        rc, out, _ = run_git(["diff", "--stat", "HEAD"], workdir)
        if rc == 0:
            diff_stat = out

    def truncate_lines(text: str, max_lines: int = 40) -> str:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"

    return {
        "is_git_repo": True,
        "branch": branch,
        "head": head,
        "diff_stat": truncate_lines(diff_stat),
        "status_short": truncate_lines(status_short),
    }


def parse_report_file(report_path: Path, fallback_text: str) -> Any:
    if report_path.is_file():
        raw = report_path.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                return {"raw_message": raw[:4000]}
    if fallback_text:
        try:
            return json.loads(fallback_text)
        except Exception:
            return {"raw_message": fallback_text[:4000]}
    return None


def compact_text(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TRACING_PREFIX = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})? )?"
    r"(?:ERROR|WARN|INFO|FATAL) (?:[\w.-]+(?:::[\w.-]+)*: )?"
)
_SEVERITY_PREFIX = re.compile(r"^(?:(?:error|fatal):\s*)+", re.IGNORECASE)
_REPEATED_ERROR_PREFIX = re.compile(r"^(\S+): (?=\1(?= |:|$))", re.IGNORECASE)
_FULL_UUID = re.compile(r"\b([0-9a-fA-F]{8})-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_STDERR_TOP_LEVEL_ERROR = re.compile(r"^(?:(?i:error|fatal|panic):|[A-Za-z_][\w.]*(?:Error|Exception):\s*\S)")
_STDERR_LOG_ERROR = re.compile(r" ERROR |^ERROR:")
_STDERR_FRAME = re.compile(r'^(?:File "|Traceback \(most recent call last\):|at |[\^~\-=|\s]+$)')


def clean_error_summary(message: str, limit: int = 160) -> str:
    text = " ".join(_ANSI_ESCAPE.sub("", message).split())
    text = _TRACING_PREFIX.sub("", text)
    text = _SEVERITY_PREFIX.sub("", text)
    text = _REPEATED_ERROR_PREFIX.sub("", text)
    return compact_text(_FULL_UUID.sub(r"\1", text), limit)


def _failure_reason(message: str) -> str:
    raw = message.strip().removeprefix("⚠").lstrip("\ufe0f ")
    if noisy_warning(raw):
        return ""
    summary = clean_error_summary(raw)
    return "" if re.fullmatch(r"turn\.failed|thread\.[\w.]*", summary) else summary


def _extract_stderr_error_summary(stderr_path: Path, limit: int = 160) -> str:
    tail_bytes = 16 * 1024
    try:
        if not stderr_path.is_file():
            return ""
        with stderr_path.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - tail_bytes))
            tail = stream.read(tail_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""

    lines = []
    for line in tail.splitlines():
        line = _ANSI_ESCAPE.sub("", line).strip()
        if line and not noisy_warning(line):
            lines.append(line)
    informative = [line for line in reversed(lines) if not _STDERR_FRAME.match(line)]
    for pattern in (_STDERR_TOP_LEVEL_ERROR, _STDERR_LOG_ERROR):
        for line in informative:
            if pattern.search(line) and (summary := clean_error_summary(line, limit)):
                return summary
    for line in informative or reversed(lines):
        if summary := clean_error_summary(line, limit):
            return summary
    return ""


def clean_shell_command(cmd: str, *, full: bool = False) -> str:
    if re.match(r"^\s*(?:/bin/)?(?:zsh|bash|sh)\s+-l?c\s", cmd):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = []
        if len(parts) == 3:
            cmd = parts[2]
    return cmd if full else compact_text(cmd)


def _extract_report_summary(raw_text_or_path):
    try:
        if isinstance(raw_text_or_path, Path):
            report = read_json_safe(raw_text_or_path)
        else:
            report = json.loads(raw_text_or_path)
    except (TypeError, ValueError):
        return ""
    if isinstance(report, dict) and isinstance(report.get("summary"), str):
        return compact_text(report["summary"], 160)
    return ""


def _turn_failure_message(ev: Dict[str, Any]) -> str:
    error = ev.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    return str(message or ev.get("message") or "").strip()


def summarize_event(ev: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    ev_type = ev.get("type", "")
    if ev_type == "thread.started":
        return "turn", f"session {str(ev.get('thread_id') or '')[:8]}"
    if ev_type in ("turn.started", "turn.completed"):
        return "turn", ev_type
    if ev_type == "turn.failed":
        message = _turn_failure_message(ev)
        if not noisy_warning(message) and (summary := clean_error_summary(message, 140)):
            return "error", f"⚠ {summary}"
        return "error", ev_type
    if ev_type == "error" and not noisy_warning(str(ev.get("message", ""))):
        return "error", f"⚠ {compact_text(ev.get('message'), 140)}"
    item = ev.get("item")
    if ev_type not in ("item.started", "item.completed") or not isinstance(item, dict):
        return None
    starting = ev_type == "item.started"
    kind = item.get("type")
    if kind == "command_execution":
        command = clean_shell_command(str(item.get("command") or ""))
        if starting:
            return "cmd_start", f"▶ {command}"
        rc = item.get("exit_code")
        marker = "✓" if rc == 0 else f"✗(rc={rc})"
        return "cmd_done", f"{marker} {command}"
    if kind == "file_change":
        changes = item.get("changes") or []
        paths = [str(change["path"]) for change in changes if isinstance(change, dict) and change.get("path")]
        preview = compact_text(", ".join(paths[:3]) + (", …" if len(paths) > 3 else "")) or "files"
        if starting:
            return "file", f"✎ editing {preview}"
        return "file", f"✎ changed {len(changes)} file(s): {preview}"
    if starting:
        if kind == "mcp_tool_call":
            return "tool", f"⚙ {compact_text(item.get('tool') or item.get('name'))}"
        if kind == "web_search":
            return "search", f"🔎 {compact_text(item.get('query'))}"
        if kind == "reasoning":
            return "thinking", "💭 reasoning..."
    elif kind == "agent_message":
        return "message", f"💬 {compact_text(item.get('text'))}"
    elif kind == "error":
        message = str(item.get("message") or "")
        if not noisy_warning(message):
            return "error", f"⚠ {compact_text(message, 140)}"
    return None


def format_model_tag(meta: Dict[str, Any]) -> str:
    model = meta.get("effective_model")
    effort = meta.get("effective_reasoning_effort")
    return f"{model}·{effort}" if model and effort else model or "codex"


def notify_activity(meta: Dict[str, Any], progress_token: Any, progress: int) -> None:
    message = f"[{meta['task_id']} · {format_model_tag(meta)}] {meta['current_activity']}"
    if progress_token is not None:
        send_json({
            "jsonrpc": "2.0", "method": "notifications/progress",
            "params": {"progressToken": progress_token, "progress": progress, "message": message},
        })
    with LOG_LEVEL_LOCK:
        if CLIENT_LOG_LEVEL in ("debug", "info"):
            send_json({
                "jsonrpc": "2.0", "method": "notifications/message",
                "params": {"level": "info", "logger": SERVER_NAME, "data": message},
            })


@contextmanager
def claim_task(task_id: str) -> Iterator[None]:
    key = os.path.normcase(task_id)
    with ACTIVE_LOCK:
        if key in CLAIMED_TASKS:
            raise ValueError(f"Task '{task_id}' is currently running. Wait for it or cancel it first.")
        CLAIMED_TASKS.add(key)
    try:
        yield
    finally:
        with ACTIVE_LOCK:
            CLAIMED_TASKS.remove(key)


def iter_codex_stdout(stream: BinaryIO) -> Iterator[Optional[bytes]]:
    pending = b""
    reader = PipeReader(stream)
    try:
        while True:
            chunk = reader.read(2.0)
            if chunk is None:
                # Check the rollout while Codex is working without publishing stdout events.
                yield None
                continue
            if not chunk:
                if pending:
                    yield pending
                return
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                yield line + b"\n"
    finally:
        reader.close()


def execute_codex_round(
    *,
    req_id: Any,
    task_id: str,
    round_num: int,
    cmd: List[str],
    prompt_file: Path,
    effective_workdir: Path,
    task_dir: Path,
    meta: Dict[str, Any],
    progress_token: Any = None,
) -> Dict[str, Any]:
    events_path = task_dir / f"events-r{round_num}.jsonl"
    stderr_path = task_dir / f"stderr-r{round_num}.log"
    report_path = task_dir / f"report-r{round_num}.json"
    meta.setdefault("rounds", [])
    meta.setdefault("feedback", {})
    if not meta.get("task_summary") and round_num == 1:
        meta["task_summary"] = telemetry.prompt_summary(prompt_file)
    telemetry.update_totals(meta, task_dir)

    started_mono = time.monotonic()
    started_at = utc_now_iso()
    meta.update(
        {
            "status": "running",
            "round": round_num,
            "updated_at": started_at,
            "round_started_at": started_at,
            "finished_at": None,
            "current_events_file": str(events_path),
            "current_stderr_file": str(stderr_path),
            "current_report_file": str(report_path),
            "event_count": 0,
            "last_event": "launching",
            "current_activity": "launching",
            "recent_activities": [],
            "exit_code": None,
            "error_summary": None,
            "pid": None,
            "usage": None,
            "_turn_usage_reported": False,
        }
    )
    meta.pop("windows_job", None)
    telemetry.save_round_meta(STATE_ROOT, task_dir, meta)

    cancel_flag = threading.Event()
    task_key = os.path.normcase(task_id)
    proc: Optional[subprocess.Popen] = None
    last_agent_message = ""
    fatal_errors: List[str] = []
    event_count = 0
    last_rollout_probe = float("-inf")

    try:
        with stderr_path.open("wb") as stderr_f:
            proc = launch_codex(cmd, cwd=effective_workdir, input_path=prompt_file, stderr=stderr_f)

            entry = {
                "req_id": req_id,
                "task_id": task_id,
                "proc": proc,
                "cancel_flag": cancel_flag,
            }
            with ACTIVE_LOCK:
                ACTIVE_BY_REQ_ID[req_id] = entry
                ACTIVE_BY_TASK_ID[task_key] = entry

            meta["pid"] = proc.pid
            if WINDOWS:
                meta["windows_job"] = proc.castor_job.name
            meta["updated_at"] = utc_now_iso()
            telemetry.save_round_meta(STATE_ROOT, task_dir, meta)
            notify_activity(meta, progress_token, 0)

            assert proc.stdout is not None
            with events_path.open("wb") as ev_f:
                for raw_line in iter_codex_stdout(proc.stdout):
                    if raw_line is None:
                        now = time.monotonic()
                        if meta.get("session_id") and now - last_rollout_probe >= 2.0:
                            last_rollout_probe = now
                            if refresh_rollout_metadata(meta):
                                meta["updated_at"] = utc_now_iso()
                                telemetry.save_round_meta(STATE_ROOT, task_dir, meta)
                        continue
                    ev_f.write(raw_line)
                    ev_f.flush()
                    event_count += 1

                    line_str = raw_line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        ev = json.loads(line_str)
                    except Exception:
                        continue

                    if not isinstance(ev, dict):
                        continue

                    ev_type = ev.get("type")
                    if ev_type == "thread.started" and ev.get("thread_id"):
                        meta["session_id"] = ev["thread_id"]
                    elif ev_type == "turn.completed" and isinstance(ev.get("usage"), dict):
                        meta["usage"] = ev["usage"]
                        meta["_turn_usage_reported"] = True
                    elif ev_type == "turn.failed":
                        fatal_errors.append(_turn_failure_message(ev) or "turn.failed")
                    elif ev_type == "error":
                        msg = str(ev.get("message") or "").strip()
                        if msg and not noisy_warning(msg):
                            fatal_errors.append(msg)
                    elif ev_type == "item.completed":
                        item = ev.get("item")
                        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                            last_agent_message = str(item["text"])

                    now = time.monotonic()
                    if (
                        meta.get("session_id")
                        and ev_type in ("turn.started", "item.started", "item.completed")
                        and now - last_rollout_probe >= 2.0
                    ):
                        refresh_rollout_metadata(meta)
                        last_rollout_probe = now
                    summary = summarize_event(ev)
                    meta["event_count"] = event_count
                    meta["updated_at"] = utc_now_iso()
                    if summary:
                        kind, activity = summary
                        meta["last_event"] = meta["current_activity"] = activity
                        activities = meta["recent_activities"]
                        entry = {"ts": meta["updated_at"], "kind": kind, "text": activity}
                        if kind == "message":
                            report_summary = _extract_report_summary(ev["item"].get("text"))
                            if report_summary:
                                entry["report_summary"] = report_summary
                        activities.append(entry)
                        del activities[:-12]
                    if summary or ev_type in ("item.started", "item.completed"):
                        telemetry.save_round_meta(STATE_ROOT, task_dir, meta)
                    if summary:
                        notify_activity(meta, progress_token, event_count)

            exit_code = proc.wait()
        refresh_rollout_metadata(meta)
    except (OSError, RuntimeError) as exc:
        fatal_errors.append(str(exc))
        if proc is not None and proc.poll() is None:
            terminate_process_group(proc)
        exit_code = proc.wait() if proc is not None else None
    finally:
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        with ACTIVE_LOCK:
            ACTIVE_BY_REQ_ID.pop(req_id, None)
            if ACTIVE_BY_TASK_ID.get(task_key, {}).get("req_id") == req_id:
                ACTIVE_BY_TASK_ID.pop(task_key, None)

    duration = round(time.monotonic() - started_mono, 2)
    if cancel_flag.is_set() or (WINDOWS and exit_code == CANCEL_EXIT_CODE):
        final_status = "cancelled"
    elif exit_code == 0 and not fatal_errors:
        final_status = "completed"
    else:
        final_status = "failed"

    error_summary = ""
    if final_status == "failed":
        for message in reversed(fatal_errors):
            if error_summary := _failure_reason(message):
                break
        if not error_summary:
            error_summary = _extract_stderr_error_summary(stderr_path)

    finished_at = utc_now_iso()
    meta.update(
        status=final_status, exit_code=exit_code, pid=None, event_count=event_count,
        duration_seconds=duration, current_activity=f"{final_status} ({duration}s)",
        updated_at=finished_at, finished_at=finished_at,
        error_summary=error_summary or None,
    )
    meta.pop("_rollout_round_start", None)
    meta.pop("windows_job", None)
    kind = "dispatch" if prompt_file.name == "spec.md" else "resume"
    round_entry = telemetry.round_record(round_num, kind, prompt_file, meta)
    meta.pop("_rollout_usage_baseline", None)
    meta.pop("_turn_usage_reported", None)
    history = {item["round"]: item for item in meta["rounds"]}
    if round_num in history:
        for key in ("user_first_prompt", "user_first_prompt_at"):
            round_entry[key] = history[round_num].get(key)
    history[round_num] = round_entry
    meta["rounds"] = [history[number] for number in sorted(history)]
    telemetry.update_totals(meta, task_dir)
    telemetry.save_round_meta(STATE_ROOT, task_dir, meta)
    try:
        telemetry.upsert_task_record(STATE_ROOT, task_dir, meta)
    except OSError as exc:
        log_err(f"Could not sync telemetry for {task_id}: {exc}")
    notify_activity(meta, progress_token, event_count + 1)

    report = parse_report_file(report_path, last_agent_message)
    base_commit = meta.get("worktree", {}).get("base_commit")
    git_summary = collect_git_summary(effective_workdir, base_commit=base_commit)

    stderr_tail = ""
    if stderr_path.is_file():
        stderr_lines = stderr_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if stderr_lines and final_status != "completed":
            stderr_tail = "\n".join(stderr_lines[-20:])

    result_payload: Dict[str, Any] = {
        "task_id": task_id,
        "round": round_num,
        "status": final_status,
        "exit_code": exit_code,
        "session_id": meta.get("session_id"),
        "workdir": str(effective_workdir),
        "worktree": meta.get("worktree"),
        "duration_seconds": duration,
        "usage": meta.get("usage"),
        **{key: meta[key] for key in telemetry.TOTAL_FIELDS},
        **{key: meta.get(key) for key in MODEL_RESULT_FIELDS},
        "report": report,
        "git": git_summary,
        "artifacts": {
            "task_dir": str(task_dir),
            "prompt_file": str(prompt_file),
            "report_file": str(report_path),
            "events_file": str(events_path),
            "stderr_file": str(stderr_path),
        },
    }
    if fatal_errors:
        result_payload["errors"] = fatal_errors[-5:]
    if stderr_tail:
        result_payload["stderr_tail"] = stderr_tail
    refresh_codex_quota(wait=False)
    return result_payload


def handle_codex_dispatch(
    req_id: Any, args: Dict[str, Any], progress_token: Any = None,
) -> Tuple[Dict[str, Any], bool]:
    workdir_raw = args.get("workdir")
    if not workdir_raw or not isinstance(workdir_raw, str):
        raise ValueError("workdir (absolute path) is required")
    workdir = Path(workdir_raw).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f"workdir does not exist or is not a directory: {workdir}")

    spec_text = args.get("spec")
    spec_file_raw = args.get("spec_file")
    if spec_file_raw:
        src = Path(spec_file_raw).expanduser().resolve()
        if not src.is_file():
            raise ValueError(f"spec_file does not exist: {src}")
        spec_content = src.read_text(encoding="utf-8")
    elif isinstance(spec_text, str) and spec_text.strip():
        spec_content = spec_text
    else:
        raise ValueError("Either 'spec' (non-empty string) or 'spec_file' is required")

    custom_id = args.get("task_id")
    task_id = sanitize_task_id(custom_id)
    task_dir = TASKS_ROOT / task_id
    if task_dir.exists():
        if custom_id:
            raise ValueError(
                f"task_id '{task_id}' already exists. Choose a new task_id or use codex_resume."
            )
        task_id = f"{task_id}-{uuid.uuid4().hex[:4]}"
        task_dir = TASKS_ROOT / task_id

    task_dir.mkdir(parents=True, exist_ok=False)
    spec_path = task_dir / "spec.md"
    spec_path.write_text(spec_content, encoding="utf-8")

    use_worktree = bool(args.get("use_worktree", False))
    base_ref = str(args.get("base_ref") or "HEAD")
    worktree_info: Dict[str, Any] = {
        "enabled": False,
        "repo_root": None,
        "path": None,
        "branch": None,
        "base_commit": None,
    }
    effective_workdir = workdir

    if use_worktree:
        repo_root = detect_git_root(workdir)
        if repo_root is None:
            raise ValueError(f"use_worktree=true requires a Git repository, but {workdir} is not in one")
        rc, base_commit, err = run_git(["rev-parse", "--verify", base_ref], repo_root)
        if rc != 0 or not base_commit:
            raise ValueError(f"Failed to resolve base_ref '{base_ref}': {err}")

        rel_subdir = os.path.relpath(workdir, repo_root)
        wt_path = WORKTREES_ROOT / task_id
        branch_name = f"codex/{task_id}"
        WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
        rc, _, err = run_git(
            ["worktree", "add", "-b", branch_name, str(wt_path), base_commit],
            repo_root,
            timeout=60,
        )
        if rc != 0:
            raise ValueError(f"git worktree add failed: {err}")

        effective_workdir = wt_path if rel_subdir == "." else (wt_path / rel_subdir)
        worktree_info = {
            "enabled": True,
            "repo_root": str(repo_root),
            "path": str(wt_path),
            "branch": branch_name,
            "base_commit": base_commit,
        }

    report_path = task_dir / "report-r1.json"

    cmd: List[str] = [
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--skip-git-repo-check",
        "-C",
        str(effective_workdir),
        "--output-schema",
        str(SCHEMA_PATH),
        "-o",
        str(report_path),
        "--json",
    ]

    meta: Dict[str, Any] = {
        "task_id": task_id,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "finished_at": None,
        "status": "starting",
        "round": 1,
        "session_id": None,
        "requested_workdir": str(workdir),
        "effective_workdir": str(effective_workdir),
        "worktree": worktree_info,
        "pid": None,
    }
    if title := nonempty_string(args.get("title")):
        meta["title"] = title
    set_round_model(meta, args, effective_workdir, cmd)

    extra_dirs = args.get("extra_writable_dirs")
    if isinstance(extra_dirs, list):
        for d in extra_dirs:
            if isinstance(d, str) and d.strip():
                cmd.extend(["--add-dir", str(Path(d).expanduser().resolve())])
    cmd.append("-")

    with claim_task(task_id):
        payload = execute_codex_round(
            req_id=req_id,
            task_id=task_id,
            round_num=1,
            cmd=cmd,
            prompt_file=spec_path,
            effective_workdir=effective_workdir,
            task_dir=task_dir,
            meta=meta,
            progress_token=progress_token,
        )
    return payload, payload["status"] == "failed"


def handle_codex_resume(
    req_id: Any, args: Dict[str, Any], progress_token: Any = None,
) -> Tuple[Dict[str, Any], bool]:
    task_id_raw = args.get("task_id")
    session_id = args.get("session_id")
    workdir_raw = args.get("workdir")
    if not task_id_raw and (not session_id or not workdir_raw):
        raise ValueError("Provide either 'task_id', or both 'session_id' and 'workdir'")
    task_id = sanitize_task_id(str(task_id_raw) if task_id_raw else f"resume-{str(session_id)[:8]}")
    with claim_task(task_id):
        payload = resume_codex_round(req_id, args, task_id, progress_token)
    return payload, payload["status"] == "failed"


def resume_codex_round(
    req_id: Any, args: Dict[str, Any], task_id: str, progress_token: Any,
) -> Dict[str, Any]:
    session_id = args.get("session_id")
    task_dir = TASKS_ROOT / task_id
    meta: Dict[str, Any]
    loaded = read_json_safe(task_dir / "meta.json")
    if args.get("task_id") and not loaded:
        raise ValueError(f"Unknown task_id: {task_id}")
    if loaded:
        if not args.get("task_id") and session_id != loaded.get("session_id"):
            raise ValueError(f"Task ID '{task_id}' is already used by another session. Pass the intended task_id.")
        if pid_alive(loaded.get("pid"), loaded.get("windows_job")):
            raise ValueError(f"Task '{task_id}' is currently running. Wait for it or cancel it first.")
        meta = loaded
        previous = telemetry.reconstruct_task_record(task_dir, meta)
        meta.update(rounds=previous["rounds"], feedback=previous["feedback"])
        session_id = session_id or meta.get("session_id")
        round_num = int(meta.get("round", 1)) + 1
        if args.get("task_id"):
            effective_workdir = Path(meta["effective_workdir"]).resolve()
        else:
            effective_workdir = Path(str(args["workdir"])).expanduser().resolve()
            meta.update(requested_workdir=str(effective_workdir), effective_workdir=str(effective_workdir))
    else:
        task_dir.mkdir(parents=True, exist_ok=True)
        effective_workdir = Path(str(args["workdir"])).expanduser().resolve()
        round_num = 1
        meta = {
            "task_id": task_id,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "status": "starting",
            "round": 1,
            "session_id": str(session_id),
            "requested_workdir": str(effective_workdir),
            "effective_workdir": str(effective_workdir),
            "worktree": {"enabled": False, "repo_root": None, "path": None, "branch": None, "base_commit": None},
        }

    if not session_id:
        raise ValueError(f"No Codex session_id recorded for task '{task_id}'; cannot resume.")
    if not effective_workdir.is_dir():
        raise ValueError(f"Working directory no longer exists: {effective_workdir}")

    feedback_text = args.get("feedback")
    feedback_file_raw = args.get("feedback_file")
    if feedback_file_raw:
        src = Path(feedback_file_raw).expanduser().resolve()
        if not src.is_file():
            raise ValueError(f"feedback_file does not exist: {src}")
        feedback_content = src.read_text(encoding="utf-8")
    elif isinstance(feedback_text, str) and feedback_text.strip():
        feedback_content = feedback_text
    else:
        raise ValueError("Either 'feedback' (non-empty string) or 'feedback_file' is required")

    prompt_path = task_dir / f"feedback-r{round_num}.md"
    prompt_path.write_text(feedback_content, encoding="utf-8")
    report_path = task_dir / f"report-r{round_num}.json"

    cmd: List[str] = [
        "exec",
        "resume",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--all",
        "--skip-git-repo-check",
        "--output-schema",
        str(SCHEMA_PATH),
        "-o",
        str(report_path),
        "--json",
    ]

    set_round_model(meta, args, effective_workdir, cmd)
    cmd.extend([str(session_id), "-"])
    meta["session_id"] = str(session_id)
    # A resumed session appends to its rollout. Only entries written this round confirm its settings.
    meta["_rollout_round_start"] = rollout_start_position(str(session_id))
    meta["_rollout_usage_baseline"] = None
    if meta["_rollout_round_start"]:
        prior_runtime = probe_rollout_runtime(str(session_id))
        meta["_rollout_usage_baseline"] = (prior_runtime or {}).get("usage")

    return execute_codex_round(
        req_id=req_id,
        task_id=task_id,
        round_num=round_num,
        cmd=cmd,
        prompt_file=prompt_path,
        effective_workdir=effective_workdir,
        task_dir=task_dir,
        meta=meta,
        progress_token=progress_token,
    )


def handle_codex_feedback(args: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    return telemetry.record_feedback(STATE_ROOT, args.get("task_id"), args), False


def task_is_alive(meta: Dict[str, Any], task_dir: Path) -> bool:
    with ACTIVE_LOCK:
        claimed = os.path.normcase(task_dir.name) in CLAIMED_TASKS
    return meta.get("status") == "running" and (claimed or pid_alive(meta.get("pid"), meta.get("windows_job")))


def primary_prompt(task_dir):
    spec = task_dir / "spec.md"
    resume = task_dir / "feedback-r1.md"
    return resume if not spec.is_file() and resume.is_file() else spec


def _failed_round_stderr_summary(meta, task_dir: Path) -> str:
    stderr_path = task_dir / f"stderr-r{meta.get('round') or 1}.log"
    try:
        recorded_path = nonempty_string(meta.get("current_stderr_file"))
        if not stderr_path.is_file() and recorded_path:
            stderr_path = Path(recorded_path)
        stderr_path = stderr_path.resolve()
        if stderr_path.is_relative_to(task_dir.resolve()):
            return _extract_stderr_error_summary(stderr_path)
    except (OSError, RuntimeError, ValueError):
        pass
    return ""


def latest_activity(meta, task_dir, alive, status):
    if not alive and status == "completed":
        report = _extract_report_summary(task_dir / f"report-r{meta.get('round') or 1}.json")
        if report:
            return {"kind": "report", "text": report}
    prefixes = {"▶": "command", "✓": "command", "✗": "command", "✎": "file", "💬": "message",
                "🔎": "search", "⚙": "tool", "💭": "thinking", "⚠": "error"}
    recent = list(reversed(meta.get("recent_activities") or []))
    if not alive and status == "failed":
        recorded = meta.get("error_summary")
        if isinstance(recorded, str) and (reason := _failure_reason(recorded)):
            return {"kind": "error", "text": reason}
        for entry in recent:
            raw = str(entry.get("text") or "").strip()
            if (entry.get("kind") == "error" or raw.startswith("⚠")) and (reason := _failure_reason(raw)):
                return {"kind": "error", "text": reason}
        if reason := _failed_round_stderr_summary(meta, task_dir):
            return {"kind": "error", "text": reason}
    current = meta.get("current_activity") or meta.get("last_event")
    fallback_kind = "error" if status == "failed" else "status"
    if current:
        entry = {"text": current, "kind": fallback_kind}
        if not alive:
            recent.append(entry)
        elif not recent or recent[0].get("text") != current:
            recent.insert(0, entry)
    for entry in recent:
        raw = str(entry.get("report_summary") or entry.get("text") or "").strip()
        kind = entry.get("kind")
        if not raw or kind == "turn" or raw.startswith(("turn.", "thread.")):
            continue
        marker = raw[:1]
        if marker in prefixes:
            raw = raw[1:].lstrip("\ufe0f ")
            kind = prefixes[marker]
        if kind in ("cmd_start", "cmd_done"):
            kind = "command"
        if raw.startswith("{"):
            # Older metadata can contain a JSON prefix cut off by compact_text.
            raw = _extract_report_summary(raw) or "Assistant report"
        if kind == "error":
            if noisy_warning(raw) or (not alive and status == "failed" and not _failure_reason(raw)):
                continue
        return {"kind": kind if kind in prefixes.values() else "status", "text": compact_text(raw, 160)}
    return {"kind": fallback_kind, "text": str(status or "Idle")}


def last_round_finished_at(meta: Dict[str, Any], record: Optional[Dict[str, Any]] = None) -> Optional[str]:
    finished_at = meta.get("finished_at")
    if isinstance(finished_at, str) and finished_at:
        return finished_at

    rounds = meta.get("rounds") or (record or {}).get("rounds") or []
    latest = None
    latest_number = None
    for position, entry in enumerate(rounds, start=1):
        if not isinstance(entry, dict):
            continue
        finished_at = entry.get("finished_at")
        if not isinstance(finished_at, str) or not finished_at:
            continue
        number = entry.get("round")
        if type(number) is not int:
            number = position
        if latest_number is None or number >= latest_number:
            latest_number, latest = number, finished_at
    return latest


def build_task_summary(
    meta: Dict[str, Any], alive: bool, task_dir: Path, record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if alive:
        meta = meta.copy()
        refresh_rollout_metadata(meta)
    status = meta.get("status")
    if status == "running" and not alive:
        status = "interrupted"

    elapsed = meta.get("duration_seconds")
    if alive:
        started_at = meta.get("round_started_at") or meta.get("created_at")
        elapsed = None
        if isinstance(started_at, str):
            try:
                start = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                elapsed = round(max(0, (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds()), 2)
            except (ValueError, TypeError):
                pass

    if record is None and "total_usage" not in meta:
        record = telemetry.reconstruct_task_record(task_dir, meta)
    totals = record or meta
    task_id = meta.get("task_id") or task_dir.name
    prompt = primary_prompt(task_dir)
    fallback = meta.get("task_summary") or totals.get("task_summary") or ""
    workspace = meta.get("requested_workdir") or (meta.get("worktree") or {}).get("repo_root") or meta.get("effective_workdir")
    fields = (
        "round", "session_id", "worktree", "model", "reasoning_effort", *MODEL_RESULT_FIELDS,
        "last_event", "event_count", "created_at", "round_started_at", "updated_at", "finished_at",
        "usage", "exit_code", "error_summary",
    )
    return {
        **{field: meta.get(field) for field in fields},
        "last_round_finished_at": last_round_finished_at(meta, record),
        "task_id": meta.get("task_id", task_dir.name),
        "task_title": (nonempty_string(meta.get("title")) or telemetry.prompt_title(prompt)
                       or telemetry.extract_title(fallback, task_id=task_id)),
        "prompt_preview": telemetry.prompt_preview(prompt) or fallback,
        "latest_activity": latest_activity(meta, task_dir, alive, status),
        "status": status,
        "alive": alive,
        "workdir": meta.get("effective_workdir"),
        "requested_workdir": meta.get("requested_workdir"),
        "project": Path(workspace).name if workspace else None,
        "worktree_branch": (meta.get("worktree") or {}).get("branch"),
        "current_activity": meta.get("current_activity") or meta.get("last_event"),
        "recent_activities": meta.get("recent_activities") or [],
        "elapsed_seconds": elapsed,
        "round_usage": telemetry.round_usage(meta) if alive else None,
        **{field: meta.get(field, totals.get(field)) for field in telemetry.TOTAL_FIELDS},
        "feedback": telemetry.merge_stored_feedback(totals.get("feedback"), meta.get("feedback")),
    }


def noisy_warning(message):
    return "is ignoring" in message or "--dangerously-bypass-hook-trust" in message


def stream_item(event, number, workdir, prior):
    event_type = event.get("type")
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return None
    starting = event_type == "item.started"
    status = "running" if starting else "completed"
    meta = (prior or {}).get("meta", {}).copy()
    body = ""
    if event_type == "turn.completed":
        kind, title = "turn_status", "Turn completed"
        meta = {"usage": telemetry.normalize_usage(event.get("usage"))}
    elif event_type in ("turn.failed", "error") or item.get("type") == "error":
        error = event.get("error") or item.get("message") or event.get("message")
        body = str((error.get("message") if isinstance(error, dict) else error) or "Turn failed")
        if noisy_warning(body):
            return None
        kind, status, title = "error", "failed", "Error"
    elif event_type in ("item.started", "item.completed"):
        kind = item.get("type")
        if item.get("status") == "failed":
            status = "failed"
        if kind == "command_execution":
            kind = "command"
            cmd = clean_shell_command(str(item.get("command") or meta.get("command", "")), full=True)
            title = compact_text(cmd, 180) or "Command"
            output = str(item.get("aggregated_output", (prior or {}).get("body", "")) or "")
            body = output[:4000]
            rc = item.get("exit_code")
            meta.update(command=cmd, exit_code=rc, output_truncated=len(output) > 4000 or
                        ("aggregated_output" not in item and bool(meta.get("output_truncated"))))
            if rc not in (None, 0):
                status = "failed"
        elif kind == "file_change":
            if "changes" in item:
                files = []
                for change in item.get("changes") or []:
                    if not isinstance(change, dict) or not change.get("path"):
                        continue
                    path = Path(os.path.normpath(change["path"]))
                    if workdir and path.is_relative_to(workdir):
                        path = path.relative_to(workdir)
                    files.append({"path": str(path), "kind": change.get("kind") or "update"})
                meta["files"] = files
            count = len(meta.setdefault("files", []))
            title = f"{'Editing' if starting else 'Changed'} {count} file{'s' if count != 1 else ''}"
        elif kind == "agent_message":
            body = str(item.get("text") or "")
            try:
                report = json.loads(body)
            except ValueError:
                report = None
            if isinstance(report, dict) and isinstance(report.get("summary"), str) and "status" in report:
                kind, title, body = "report", "Final Report", report["summary"]
                meta = {key: report.get(key) for key in (
                    "status", "summary", "files_changed", "commands_run", "tests_summary", "open_issues")}
            else:
                kind, title = "message", "Assistant"
                meta = {"truncated": len(body) > 12000}
                body = body[:12000]
        elif kind == "web_search":
            action = item.get("action") or {}
            queries = action.get("queries") or meta.get("queries") or []
            title = item.get("query") or action.get("query") or ", ".join(map(str, queries))
            title = title or (prior or {}).get("title")
            if not title:
                return None
            kind, meta = "search", {"queries": queries}
        elif kind == "mcp_tool_call":
            title = item.get("tool") or item.get("name") or meta.get("tool") or "Tool call"
            kind, meta = "tool", {"tool": title, "server": item.get("server") or meta.get("server")}
        elif kind == "reasoning":
            title, body = "Reasoning", str(item.get("text") or (prior or {}).get("body") or "")[:4000]
            if not body.strip():
                return None
        else:
            return None
    else:
        return None
    entry = {"round": number, "kind": kind, "status": status, "title": title, "body": body, "meta": meta}
    ts = event.get("timestamp") or event.get("ts") or (prior or {}).get("ts")
    if ts is not None:
        entry["ts"] = ts
    return entry


def append_round_stream(messages, path, number, workdir):
    """Merge into the retained timeline, ordering items by their first event."""
    positions, emitted = {}, set()
    event_count = item_count = 0
    for event_count, event in enumerate(telemetry.jsonl(path), 1):
        item = event.get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
        position = event_count
        timestamp = event.get("timestamp") or event.get("ts")
        if isinstance(item_id, (int, str)):
            position, timestamp = positions.setdefault(item_id, (event_count, timestamp))
        key = (number, position)
        if position in emitted and key not in messages:
            continue  # Completion of an item outside the retained window.
        entry = stream_item(event, number, workdir, messages.get(key))
        if entry is None:
            continue
        entry["stream_id"] = f"{number}-{position}"
        if timestamp is not None:
            entry.setdefault("ts", timestamp)
        later = []
        if key not in messages:
            item_count += 1
            emitted.add(position)
            # An empty search or reasoning placeholder may only acquire its
            # display text on completion, after other items have appeared.
            if position != event_count:
                later = [k for k in messages if k > key]
        messages[key] = entry
        for k in later:
            messages.move_to_end(k)
        if len(messages) > 250:
            messages.popitem(last=False)
    return item_count, event_count


def build_task_detail(task_dir, meta, alive, summary, record):
    latest = max(1, int(meta.get("round") or 1))
    history = {r["round"]: r for r in (record or {}).get("rounds", [])}
    history.update({r["round"]: r for r in meta.get("rounds") or []})
    worktree = meta.get("worktree") or {}
    config = {key: summary.get(key) for key in (
        "task_id", "session_id", *MODEL_RESULT_FIELDS, "created_at", "round_started_at", "updated_at", "finished_at")}
    for key in MODEL_FIELDS:
        config[f"effective_{key}"] = config[f"effective_{key}"] or meta.get(key)
    config.update(project=summary["project"],
                  requested_workdir=meta.get("requested_workdir"), effective_workdir=meta.get("effective_workdir"),
                  worktree_enabled=bool(worktree.get("enabled")), worktree_branch=worktree.get("branch"),
                  worktree_base_commit=worktree.get("base_commit"), round_count=latest, rework_count=latest - 1)

    messages = OrderedDict()
    rounds, prompts = [], []
    total_items = event_count = 0
    for number in range(1, latest + 1):
        path = primary_prompt(task_dir) if number == 1 else task_dir / f"feedback-r{number}.md"
        kind = "dispatch" if path.name == "spec.md" else "resume"
        if path.is_file():
            with path.open(encoding="utf-8", errors="replace") as stream:
                text = stream.read(120001)
            prompts.append({"round": number, "kind": kind, "filename": path.name,
                            "text": text[:120000], "truncated": len(text) > 120000})

        r = history.get(number) or telemetry.historical_round(task_dir, meta, number, latest)
        row = {key: r.get(key) for key in (
            "status", "model", "reasoning_effort", "duration_seconds", "started_at", "finished_at")}
        row.update(round=number, kind=kind, prompt_file=path.name,
                   usage=telemetry.normalize_usage(r.get("usage")))
        if number == latest:
            row.update(status=summary["status"], model=config["effective_model"] or row["model"],
                       reasoning_effort=config["effective_reasoning_effort"] or row["reasoning_effort"])
            if alive:
                row.update(usage=summary.get("round_usage") or row["usage"],
                           duration_seconds=summary["elapsed_seconds"], finished_at=None)
        rounds.append(row)
        if latest > 1:
            messages[(number, 0)] = {
                "round": number, "kind": "round_start", "title": f"Round {number} · {kind.title()}",
                "status": "info", "body": "", "meta": {"round": number, "prompt_kind": kind},
            }
            total_items += 1
        items, round_events = append_round_stream(
            messages, task_dir / f"events-r{number}.jsonl", number, meta.get("effective_workdir"))
        total_items += items
        event_count += max(round_events, (summary.get("event_count") or 0) if number == latest else 0)
        while len(messages) > 250:
            messages.popitem(last=False)
    usage = summary.get("total_usage") or telemetry.sum_usage(rounds)
    duration = summary.get("total_duration_seconds")
    if alive:
        # Stored totals cover finished rounds. A backfilled record may already
        # include the live round, so prefer its individual rows in that case.
        prior_usage = meta.get("total_usage") or telemetry.sum_usage(rounds[:-1])
        usage = telemetry.sum_usage([{"usage": prior_usage}, rounds[-1]])
        prior_duration = meta.get("total_duration_seconds")
        if prior_duration is None:
            prior_duration = sum(r.get("duration_seconds") or 0 for r in rounds[:-1])
        duration = prior_duration + (summary.get("elapsed_seconds") or 0)
    elif duration is None:
        duration = sum(r.get("duration_seconds") or 0 for r in rounds)
    runtime = {key: summary.get(key) for key in (
        "status", "alive", "exit_code", "elapsed_seconds", "current_activity", "latest_activity")}
    runtime.update(total_duration_seconds=round(duration, 2),
                   event_count=event_count, usage=telemetry.normalize_usage(usage or {}), rounds=rounds)
    detail = {"task_id": summary["task_id"], "task_title": summary["task_title"], "config": config,
              "runtime": runtime, "prompts": prompts, "messages": list(messages.values()),
              "messages_truncated": total_items > len(messages), "total_stream_items": total_items}
    stderr = task_dir / f"stderr-r{latest}.log"
    if summary["status"] != "completed" and stderr.is_file():
        with stderr.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - 16000))
            tail = "\n".join(stream.read().decode("utf-8", "replace").splitlines()[-40:])[-4000:]
        if tail.strip():
            detail["stderr_tail"] = tail
    return detail


def wire_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _excerpt(value, limit):
    """Bound variable-width stream fields while preserving their JSON types."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_excerpt(entry, limit) for entry in value[:limit]]
    if isinstance(value, dict):
        return {key: _excerpt(entry, limit) for key, entry in value.items()}
    return value


def _fit_stream_item(item, budget):
    if wire_size(item) <= budget:
        return item
    # Keep the timeline entry even when its output needs a smaller excerpt.
    def clipped(limit):
        meta = _excerpt(item["meta"], limit)
        meta["truncated"] = True
        if item["kind"] == "command" and len(item["body"]) > limit:
            meta["output_truncated"] = True
        return {**item, "body": item["body"][:limit], "title": item["title"][:max(32, limit)], "meta": meta}

    lo, hi = 0, budget
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if wire_size(clipped(mid)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return clipped(lo)


def fit_detail_budget(snapshot, detail, content):
    # Reserve space for the MCP envelope. Count encoded JSON, including escaped
    # control characters and multibyte text, rather than estimating from characters.
    available = 950_000 - wire_size(snapshot) - wire_size(content)
    if wire_size(detail) <= available:
        return
    messages = detail["messages"]
    prompts = detail["prompts"]
    fixed = wire_size({**detail, "messages": [], "prompts": [{**p, "text": ""} for p in prompts]})
    remaining = max(0, available - fixed - 1024)
    prompt_budget = remaining // (2 if messages else 1)
    for index, prompt in enumerate(prompts):
        text = prompt["text"]
        budget = prompt_budget // (len(prompts) - index)
        if wire_size(text) > budget:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if wire_size(text[:mid]) <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            prompt.update(text=text[:lo], truncated=True)
        size = wire_size(prompt["text"])
        prompt_budget -= size
        remaining -= size
    # Allocate small entries first so unused space goes to longer outputs.
    for n, index in enumerate(sorted(range(len(messages)), key=lambda i: wire_size(messages[i]))):
        messages[index] = _fit_stream_item(messages[index], max(0, remaining // (len(messages) - n) - 1))
        remaining -= wire_size(messages[index]) + 1
    if wire_size(detail) > available:
        raise ValueError("Session metadata exceeds the monitor response limit. The full records are in the task directory.")


def list_task_metadata() -> List[Tuple[Path, Dict[str, Any], bool]]:
    tasks = []
    if TASKS_ROOT.is_dir():
        for task_dir in TASKS_ROOT.iterdir():
            meta = read_json_safe(task_dir / "meta.json")
            if meta:
                tasks.append((task_dir, meta, task_is_alive(meta, task_dir)))
    return tasks


def task_limit(args: Dict[str, Any]) -> int:
    limit = args.get("limit", 20)
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    return limit


def handle_codex_status(args: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    task_id_raw = args.get("task_id")
    if task_id_raw:
        task_id = sanitize_task_id(str(task_id_raw))
        task_dir = TASKS_ROOT / task_id
        meta = read_json_safe(task_dir / "meta.json")
        if not meta:
            return {"error": f"Task not found: {task_id}"}, True

        summary = build_task_summary(meta, task_is_alive(meta, task_dir), task_dir)
        recent_events = [entry["text"] for entry in summary["recent_activities"][-8:]]
        if "recent_activities" not in meta:
            # Pre-v1.1 tasks only stored their activity in the event log.
            events_file = meta.get("current_events_file")
            if events_file and Path(events_file).is_file():
                for line in Path(events_file).read_text(encoding="utf-8", errors="replace").splitlines()[-30:]:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    activity = summarize_event(event) if isinstance(event, dict) else None
                    if activity:
                        recent_events.append(activity[1])

        stderr_tail = ""
        err_file = meta.get("current_stderr_file")
        if err_file and Path(err_file).is_file():
            stderr_tail = "\n".join(Path(err_file).read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        return {**summary, "recent_events": recent_events[-8:], "stderr_tail": stderr_tail}, False

    limit = task_limit(args)
    tasks = list_task_metadata()
    tasks.sort(key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)
    summaries = [build_task_summary(meta, alive, task_dir) for task_dir, meta, alive in tasks[:limit]]
    return {"tasks": summaries, "total": len(tasks)}, False


def handle_codex_monitor(args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    global QUOTA_BOOTSTRAP_ATTEMPTED
    limit = task_limit(args)
    selected_id = sanitize_task_id(str(args["task_id"])) if args.get("task_id") else None
    tasks = list_task_metadata()
    records = telemetry.load_or_build_records(STATE_ROOT)
    by_task_id = {record["task_id"]: record for record in records}

    def sort_key(item):
        task_dir, meta, alive = item
        task_id = meta.get("task_id", task_dir.name)
        started_at = str(meta.get("round_started_at") or meta.get("created_at") or "")
        sort_at = started_at if alive else str(last_round_finished_at(meta, by_task_id.get(task_id)) or "")
        return task_id == selected_id, alive, sort_at, started_at, task_id

    tasks.sort(key=sort_key, reverse=True)
    summaries = []
    selected = None
    detail = None
    for index, (task_dir, meta, alive) in enumerate(tasks):
        task_id = meta.get("task_id", task_dir.name)
        is_selected = task_id == selected_id
        if index < limit or is_selected:
            summary = build_task_summary(meta, alive, task_dir, by_task_id.get(task_id))
            if index < limit:
                summaries.append(summary)
            if is_selected:
                selected = summary
                if args.get("include_detail"):
                    detail = build_task_detail(task_dir, meta, alive, selected, by_task_id.get(task_id))

    if args.get("refresh_quota"):
        quota = refresh_codex_quota(wait=True)
    else:
        quota = load_cached_quota()
        if quota is None:
            with QUOTA_LOCK:
                bootstrap = not QUOTA_BOOTSTRAP_ATTEMPTED
                QUOTA_BOOTSTRAP_ATTEMPTED = True
            if bootstrap:
                refresh_codex_quota(wait=False)

    active_count = sum(alive for _, _, alive in tasks)
    result = {
        "server_time": utc_now_iso(),
        "active_count": active_count,
        "total": len(tasks),
        "tasks": summaries,
        "selected_task": selected,
        "analytics": telemetry.compute_analytics(records),
        "quota": quota,
        "telemetry_dir": os.environ.get("CODEX_CASTOR_STATE_DIR", "~/.gemini/codex-castor"),
    }
    lines = [f"Codex Monitor: {active_count} active, {len(tasks)} total"]
    visible = summaries + ([selected] if selected and selected not in summaries else [])
    for task in visible:
        elapsed = task["elapsed_seconds"]
        duration = f" {elapsed:g}s" if elapsed is not None else ""
        activity = task["current_activity"] or "—"
        lines.append(
            f"[{task['status']}] {task['task_id']} ({format_model_tag(task)}){duration} · {activity}"
        )
    if detail is not None:
        fit_detail_budget(result, detail, "\n".join(lines))
        result["task_detail"] = detail
    return result, "\n".join(lines)


def handle_codex_cancel(args: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    task_id_raw = args.get("task_id")
    if not task_id_raw:
        raise ValueError("task_id is required")
    task_id = sanitize_task_id(str(task_id_raw))
    task_dir = TASKS_ROOT / task_id
    meta_path = task_dir / "meta.json"
    meta = read_json_safe(meta_path)

    with ACTIVE_LOCK:
        entry = ACTIVE_BY_TASK_ID.get(os.path.normcase(task_id))

    killed = False
    if entry is not None:
        entry["cancel_flag"].set()
        killed = terminate_process_group(entry["proc"])
    elif meta and meta.get("status") == "running" and pid_alive(meta.get("pid"), meta.get("windows_job")):
        pid = meta["pid"]
        killed = terminate_process_group(None, pid=pid, job_name=meta.get("windows_job"))
        if killed:
            with telemetry.transaction(STATE_ROOT):
                latest = read_json_safe(meta_path)
                if latest and latest.get("status") == "running" and latest.get("pid") == pid:
                    now = utc_now_iso()
                    elapsed = build_task_summary(latest, True, task_dir)["elapsed_seconds"]
                    latest.update(status="cancelled", pid=None, current_activity="cancelled",
                                  updated_at=now, finished_at=now, duration_seconds=elapsed)
                    latest.pop("windows_job", None)
                    latest["rounds"] = telemetry.reconstruct_task_record(task_dir, latest)["rounds"]
                    latest["rounds"][-1]["duration_estimated"] = elapsed is not None
                    telemetry.update_totals(latest, task_dir)
                    telemetry.write_meta(meta_path, latest)
                else:
                    latest = None
            if latest:
                try:
                    telemetry.upsert_task_record(STATE_ROOT, task_dir, latest)
                except OSError as exc:
                    log_err(f"Could not sync telemetry for {task_id}: {exc}")

    return {
        "task_id": task_id,
        "cancelled": killed,
        "message": "Terminated running Codex process." if killed else "No active Codex process found for this task_id.",
    }, False


def handle_codex_cleanup_worktree(args: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    task_id_raw = args.get("task_id")
    if not task_id_raw:
        raise ValueError("task_id is required")
    task_id = sanitize_task_id(str(task_id_raw))
    task_dir = TASKS_ROOT / task_id
    meta_path = task_dir / "meta.json"
    meta = read_json_safe(meta_path)
    if not meta:
        return {"error": f"Task not found: {task_id}"}, True

    wt = meta.get("worktree") or {}
    if not wt.get("enabled"):
        return {"task_id": task_id, "cleaned": False, "message": "Task did not use a managed worktree."}, False

    repo_root = Path(wt["repo_root"])
    wt_path = Path(wt["path"])
    branch = wt.get("branch")
    force = bool(args.get("force", False))
    delete_branch = bool(args.get("delete_branch", False))

    messages: List[str] = []
    if wt_path.exists():
        git_args = ["worktree", "remove"]
        if force:
            git_args.append("--force")
        git_args.append(str(wt_path))
        rc, out, err = run_git(git_args, repo_root, timeout=60)
        if rc != 0:
            return {
                "task_id": task_id,
                "cleaned": False,
                "error": f"git worktree remove failed: {err or out}",
            }, True
        messages.append(f"Removed worktree {wt_path}")
    else:
        run_git(["worktree", "prune"], repo_root)
        messages.append("Worktree directory already absent; pruned stale git worktree metadata.")

    branch_deleted = False
    if delete_branch and branch:
        flag = "-D" if force else "-d"
        rc, out, err = run_git(["branch", flag, branch], repo_root)
        if rc == 0:
            branch_deleted = True
            messages.append(f"Deleted branch {branch}")
        else:
            messages.append(f"Branch {branch} not deleted: {err or out}")

    wt["cleaned_at"] = utc_now_iso()
    with telemetry.transaction(STATE_ROOT):
        latest = read_json_safe(meta_path) or meta
        latest.update(worktree=wt, updated_at=utc_now_iso())
        telemetry.write_meta(meta_path, latest)

    return {
        "task_id": task_id,
        "cleaned": True,
        "branch_deleted": branch_deleted,
        "messages": messages,
    }, False


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "codex_dispatch",
        "description": (
            "Dispatch a detailed implementation specification to local Codex CLI running "
            "non-interactively with full permissions (--dangerously-bypass-approvals-and-sandbox). "
            "Configured as an async background tool: after calling it, do NOT poll codex_status; "
            "either work on something else or stop your turn and wait for automatic wakeup."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "Absolute path to the target project/working directory.",
                },
                "spec": {
                    "type": "string",
                    "description": "Complete, self-contained Markdown task specification for Codex. Provide either spec or spec_file.",
                },
                "spec_file": {
                    "type": "string",
                    "description": "Absolute path to an existing Markdown specification file. Provide either spec or spec_file.",
                },
                "title": {"type": "string", "description": "Optional concise human-readable title for this task."},
                "task_id": {
                    "type": "string",
                    "description": "Optional short unique slug (e.g. 'feat-auth-jwt'). Auto-generated if omitted.",
                },
                "use_worktree": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, run Codex in an isolated managed Git worktree (branch codex/<task_id>). REQUIRED when running multiple Codex tasks in parallel on the same repository.",
                },
                "base_ref": {
                    "type": "string",
                    "default": "HEAD",
                    "description": "Git ref to branch from when use_worktree=true (default: HEAD).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional Codex model override (omit to use default in ~/.codex/config.toml).",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Optional Codex reasoning effort override (e.g. 'medium', 'high', 'xhigh').",
                },
                "extra_writable_dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional extra directories passed via --add-dir.",
                },
            },
            "required": ["workdir"],
        },
    },
    {
        "name": "codex_resume",
        "description": (
            "Resume an existing Codex task/session with concrete review feedback or rework instructions "
            "while preserving Codex's prior session memory and full permissions. Runs asynchronously as a "
            "background tool; do NOT poll after calling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID returned by an earlier codex_dispatch call (automatically resolves session_id and workdir).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Explicit Codex session UUID (only needed if task_id is omitted).",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory (only needed if task_id is omitted).",
                },
                "feedback": {
                    "type": "string",
                    "description": "Concrete review feedback, failing test output, and exact fixes required. Provide either feedback or feedback_file.",
                },
                "feedback_file": {
                    "type": "string",
                    "description": "Path to a Markdown feedback file. Provide either feedback or feedback_file.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional Codex model override.",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Optional Codex reasoning effort override.",
                },
            },
        },
    },
    {
        "name": "codex_feedback",
        "description": (
            "Record or update user/reviewer effectiveness feedback (rating 1–5, verdict, tags, notes) "
            "and/or the user's first prompt after a Codex round finishes. Automatically updates "
            "meta.json, telemetry.jsonl, telemetry.csv, and report.md."
        ),
        "_meta": {"ui": {"visibility": ["model", "app"]}},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "verdict": {"type": "string", "enum": list(telemetry.VERDICTS)},
                "tags": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
                "user_first_prompt": {"type": "string"},
                "round": {"type": "integer", "minimum": 1},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "codex_status",
        "description": (
            "Non-blocking inspection of one Codex task (or list of recent tasks). "
            "Use ONLY when the user explicitly asks for progress or for post-mortem debugging—never poll in a loop."
        ),
        "_meta": {"ui": {"visibility": ["model", "app"]}},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Specific task_id to inspect. Omit to list recent tasks.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of recent tasks to return when task_id is omitted.",
                },
            },
        },
    },
    {
        "name": "codex_cancel",
        "description": "Terminate a currently running Codex task immediately.",
        "_meta": {"ui": {"visibility": ["model", "app"]}},
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The running task_id to terminate.",
                }
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "codex_cleanup_worktree",
        "description": (
            "Remove a managed Git worktree created with use_worktree=true after Gemini has verified "
            "and merged (or discarded) its branch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task_id whose managed worktree should be removed.",
                },
                "delete_branch": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also delete the codex/<task_id> branch.",
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force removal even with uncommitted changes or unmerged branch.",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "codex_monitor",
        "title": "Codex Monitor",
        "description": (
            "Open the live Codex Castor monitor panel (side pane or full page in Antigravity App) "
            "or return a structured live snapshot of running and recent Codex tasks including "
            "effective model, reasoning effort, and current activity."
        ),
        "_meta": {
            "ui": {"resourceUri": MONITOR_URI, "visibility": ["model", "app"]},
            "com.google.antigravity/ui": {
                "entrypoints": [
                    {"type": "aux_pane", "title": "Codex Monitor"},
                    {"type": "global", "title": "Codex Monitor"},
                ],
            },
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 0, "default": 20},
                "include_detail": {"type": "boolean", "default": False},
                "refresh_quota": {"type": "boolean", "default": False},
            },
        },
    },
]


def dispatch_tool_call(req_id: Any, params: Dict[str, Any], progress_token: Any = None) -> None:
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        send_error(req_id, -32602, "Tool arguments must be a JSON object")
        return

    try:
        structured = None
        content_text = None
        if tool_name == "codex_dispatch":
            payload, is_err = handle_codex_dispatch(req_id, arguments, progress_token)
        elif tool_name == "codex_resume":
            payload, is_err = handle_codex_resume(req_id, arguments, progress_token)
        elif tool_name == "codex_feedback":
            payload, is_err = handle_codex_feedback(arguments)
            if not is_err:
                structured = payload
        elif tool_name == "codex_status":
            payload, is_err = handle_codex_status(arguments)
            if not is_err:
                structured = payload
        elif tool_name == "codex_cancel":
            payload, is_err = handle_codex_cancel(arguments)
        elif tool_name == "codex_cleanup_worktree":
            payload, is_err = handle_codex_cleanup_worktree(arguments)
        elif tool_name == "codex_monitor":
            payload, content_text = handle_codex_monitor(arguments)
            structured, is_err = payload, False
        else:
            send_error(req_id, -32602, f"Unknown tool: {tool_name}")
            return

        if content_text is None:
            content_text = json.dumps(payload, ensure_ascii=False, indent=2)
        result = {"content": [{"type": "text", "text": content_text}], "isError": is_err}
        if structured is not None:
            result["structuredContent"] = structured
        send_result(req_id, result)
    except Exception as exc:
        log_err(f"Tool '{tool_name}' failed: {exc}")
        send_result(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2),
                    }
                ],
                "isError": True,
            },
        )


def read_monitor_html() -> str:
    try:
        html = MONITOR_HTML_PATH.read_text(encoding="utf-8")
        if html.strip():
            return html
    except (OSError, UnicodeError):
        pass
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Codex Monitor</title>'
        '</head><body><p>Install the Codex Castor monitor HTML to open the live panel.</p></body></html>'
    )


def handle_message(msg: Dict[str, Any]) -> None:
    global CLIENT_LOG_LEVEL
    method = msg.get("method")
    req_id = msg.get("id")
    is_notification = "id" not in msg or req_id is None
    params = msg.get("params", {})
    if not isinstance(params, dict):
        if not is_notification:
            send_error(req_id, -32602, "Request params must be a JSON object")
        return

    if method == "initialize":
        if is_notification:
            return
        requested_protocol = params.get("protocolVersion")
        proto = requested_protocol if requested_protocol in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        capabilities = {"tools": {"listChanged": False}, "resources": {"listChanged": False}, "logging": {}}
        client_capabilities = params.get("capabilities")
        extensions = client_capabilities.get("extensions") if isinstance(client_capabilities, dict) else None
        app_capability = extensions.get(MCP_APPS_EXTENSION) if isinstance(extensions, dict) else None
        mime_types = app_capability.get("mimeTypes") if isinstance(app_capability, dict) else None
        if isinstance(mime_types, list) and MONITOR_MIME in mime_types:
            capabilities["extensions"] = {MCP_APPS_EXTENSION: {}}
        send_result(
            req_id,
            {
                "protocolVersion": proto,
                "capabilities": capabilities,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Async MCP server for local Codex CLI (full permissions). "
                    "codex_dispatch and codex_resume run as Antigravity background tasks: "
                    "yield or handle other work until reactive wakeup. Call codex_status or "
                    "codex_monitor on user request."
                ),
            },
        )
        return

    if method == "notifications/initialized":
        return

    if method == "notifications/cancelled":
        cancel_id = params.get("requestId")
        with ACTIVE_LOCK:
            entry = ACTIVE_BY_REQ_ID.get(cancel_id)
        if entry is not None:
            log_err(f"Cancellation received for requestId={cancel_id} (task={entry['task_id']})")
            entry["cancel_flag"].set()
            terminate_process_group(entry["proc"])
        return

    if method == "ping":
        if not is_notification:
            send_result(req_id, {})
        return

    if method == "tools/list":
        if not is_notification:
            send_result(req_id, {"tools": TOOL_DEFINITIONS})
        return

    if method == "logging/setLevel":
        if not is_notification:
            level = params.get("level")
            if level not in LOG_LEVELS:
                send_error(req_id, -32602, "Invalid logging level")
            else:
                with LOG_LEVEL_LOCK:
                    CLIENT_LOG_LEVEL = level
                send_result(req_id, {})
        return

    if method == "resources/list":
        if not is_notification:
            send_result(req_id, {"resources": [{
                "uri": MONITOR_URI,
                "name": "codex_castor_monitor",
                "title": "Codex Castor Monitor",
                "description": "Live monitor showing Codex model, reasoning effort, elapsed time, and real-time activity.",
                "mimeType": MONITOR_MIME,
            }]})
        return

    if method == "resources/read":
        if not is_notification:
            if params.get("uri") != MONITOR_URI:
                send_error(req_id, -32002, "Resource not found")
            else:
                send_result(req_id, {"contents": [{
                    "uri": MONITOR_URI,
                    "mimeType": MONITOR_MIME,
                    "text": read_monitor_html(),
                    "_meta": {"ui": {"prefersBorder": False}},
                }]})
        return

    if method == "tools/call":
        if is_notification:
            return
        metadata = params.get("_meta")
        progress_token = metadata.get("progressToken") if isinstance(metadata, dict) else None
        if progress_token is not None and type(progress_token) not in (str, int):
            send_error(req_id, -32602, "progressToken must be a string or integer")
            return
        t = threading.Thread(
            target=dispatch_tool_call,
            args=(req_id, params, progress_token),
            daemon=True,
        )
        t.start()
        return

    if not is_notification:
        send_error(req_id, -32601, f"Method not found: {method}")


def cleanup_all_active() -> None:
    with ACTIVE_LOCK:
        entries = list(ACTIVE_BY_REQ_ID.values())
    for entry in entries:
        entry["cancel_flag"].set()
        terminate_process_group(entry["proc"])


def main() -> int:
    configure_stdio()
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)

    def _on_signal(signum: int, _frame: Any) -> None:
        log_err(f"Received signal {signum}, shutting down active Codex workers.")
        cleanup_all_active()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                send_error(None, -32700, f"Parse error: {exc}")
                continue
            if isinstance(msg, dict):
                handle_message(msg)
    finally:
        cleanup_all_active()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
