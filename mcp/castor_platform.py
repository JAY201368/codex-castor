"""Platform operations shared by the service and command-line entry points."""

import base64
import errno
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

WINDOWS = os.name == "nt"
POSIX_BIN_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin")


def configure_stdio():
    if WINDOWS:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def executable_path(value):
    if not value:
        return None
    value = os.path.expanduser(value)
    if WINDOWS:
        # Resolve a real executable. A .cmd shim would reparse Codex arguments in a shell.
        suffix = Path(value).suffix.lower()
        if suffix != ".exe":
            if suffix:
                return None
            value += ".exe"
        if os.path.dirname(value):
            return os.path.abspath(value) if os.path.isfile(value) else None
        for directory in os.get_exec_path():
            path = os.path.join(directory.strip('"'), value)
            if directory and os.path.isfile(path):
                return os.path.abspath(path)
        return None
    found = shutil.which(value)
    return os.path.abspath(found) if found else None


def build_env():
    env = os.environ.copy()
    if not WINDOWS:
        home = Path.home()
        extra_paths = (*POSIX_BIN_DIRS, str(home / ".local" / "bin"), str(home / ".cargo" / "bin"))
        parts = [path for path in env.get("PATH", "").split(os.pathsep) if path]
        for path in extra_paths:
            if os.path.isdir(path) and path not in parts:
                parts.insert(0, path)
        env["PATH"] = os.pathsep.join(parts)
    return env


def find_codex(fallback_path=None):
    if WINDOWS:
        candidates = ["codex.exe"]
        # The standalone installer can update the user PATH after the GUI has started.
        if local := os.environ.get("LOCALAPPDATA"):
            candidates.append(str(Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"))
        for candidate in candidates:
            if resolved := executable_path(candidate):
                return resolved
        return None

    home = Path.home()
    if fallback_path is None:
        fallback_path = build_env().get("PATH")
    candidates = [
        shutil.which("codex", path=fallback_path),
        "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
        str(home / ".local" / "bin" / "codex"), str(home / ".npm-global" / "bin" / "codex"),
    ]
    return next((os.path.abspath(path) for path in candidates if path and os.path.isfile(path)
                 and os.access(path, os.X_OK)), None)


POWERSHELL = r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
POWERSHELL_ENCODED = f'if exist "{POWERSHELL}" "{POWERSHELL}" -NoLogo -NoProfile -NonInteractive -EncodedCommand '
POWERSHELL_UTF8 = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "


def powershell_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def python_shell_command(python, target):
    """Build a command for Antigravity's platform shell, including a missing-file guard."""
    if not WINDOWS:
        path = shlex.quote(str(target))
        return f"[ ! -f {path} ] || {shlex.quote(str(python))} {path}"

    python, target = str(python), str(target)
    if any(char in python or char in target for char in "%!"):
        # cmd expands environment variables even inside quotes. Keep literal paths in the encoded script.
        script = (POWERSHELL_UTF8 + f"if (Test-Path -LiteralPath {powershell_literal(target)}) "
                  f"{{ & {powershell_literal(python)} -X utf8 {powershell_literal(target)}; exit $LASTEXITCODE }}")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return POWERSHELL_ENCODED + encoded
    # A leading shell verb also avoids cmd /c stripping the executable's opening quote.
    return f'if exist "{target}" "{python}" -X utf8 "{target}"'


def command_targets(command, target, *, allow_bare=False):
    if not isinstance(command, str) or not command:
        return False
    normalized = os.path.normcase(str(target))
    if allow_bare and os.path.normcase(command) == normalized:
        return True
    if WINDOWS:
        match = re.fullmatch(r'if exist "([^"]+)" "[^"]+" -X utf8 "([^"]+)"', command)
        if match:
            return all(os.path.normcase(path) == normalized for path in match.groups())
        if not command.startswith(POWERSHELL_ENCODED):
            return False
        try:
            script = base64.b64decode(command[len(POWERSHELL_ENCODED):], validate=True).decode("utf-16-le")
        except ValueError:
            return False
        literal = powershell_literal(target)
        before = re.escape(POWERSHELL_UTF8 + f"if (Test-Path -LiteralPath {literal}) {{ & ")
        after = re.escape(f" -X utf8 {literal}; exit $LASTEXITCODE }}")
        return re.fullmatch(before + r"'(?:[^']|'')+'" + after, script) is not None
    try:
        return shlex.split(command, comments=True)[-1:] == [str(target)]
    except ValueError:
        return False


@contextmanager
def file_lock(path):
    with open(path, "a+b") as stream:
        if not WINDOWS:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX)
            yield
            return

        import msvcrt

        # Windows can lock a byte beyond EOF. Serialize on byte zero without modifying the file.
        stream.seek(0)
        while True:
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(0.05)
        try:
            yield
        finally:
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def replace_file(source, target):
    # Windows can briefly deny replacement while a monitor or virus scanner reads the target.
    deadline = time.monotonic() + 0.75
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not WINDOWS or getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.025)


def pid_alive(pid, job_name=None):
    if type(pid) is not int or pid <= 0:
        return False
    if WINDOWS:
        from windows_process import process_alive

        return process_alive(pid, job_name)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def read_available_stdin(limit):
    if sys.stdin.isatty():
        return b""
    fd = sys.stdin.fileno()
    if WINDOWS:
        import msvcrt
        from windows_process import pipe_bytes_available

        msvcrt.setmode(fd, os.O_BINARY)
        regular_file = stat.S_ISREG(os.fstat(fd).st_mode)

        def available():
            return 4096 if regular_file else pipe_bytes_available(msvcrt.get_osfhandle(fd))
    else:
        import select

        def available():
            return 4096 if select.select([fd], [], [], 0)[0] else 0

    data = bytearray()
    while len(data) < limit and (size := available()):
        chunk = os.read(fd, min(size, limit - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def enable_terminal_sequences():
    if not sys.stdout.isatty():
        return False
    if WINDOWS:
        import msvcrt
        from windows_process import enable_virtual_terminal

        return enable_virtual_terminal(msvcrt.get_osfhandle(sys.stdout.fileno()))
    return True
