"""Windows Job Objects, process probing, and terminal handles."""

import ctypes as ct
import os
import re
import subprocess
import sys
import threading
import uuid
from ctypes import wintypes as wt
from pathlib import Path


def kernel_function(name, result, *parameters):
    function = getattr(kernel, name)
    function.restype = result
    function.argtypes = parameters
    return function


kernel = ct.WinDLL("kernel32", use_last_error=True)
create_job = kernel_function("CreateJobObjectW", wt.HANDLE, wt.LPVOID, wt.LPCWSTR)
open_job = kernel_function("OpenJobObjectW", wt.HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
set_job_info = kernel_function("SetInformationJobObject", wt.BOOL, wt.HANDLE, ct.c_int, wt.LPVOID, wt.DWORD)
assign_job = kernel_function("AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)
terminate_job = kernel_function("TerminateJobObject", wt.BOOL, wt.HANDLE, wt.UINT)
open_process = kernel_function("OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
is_process_in_job = kernel_function("IsProcessInJob", wt.BOOL, wt.HANDLE, wt.HANDLE, ct.POINTER(wt.BOOL))
wait_for_handle = kernel_function("WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
close_handle = kernel_function("CloseHandle", wt.BOOL, wt.HANDLE)
peek_pipe = kernel_function("PeekNamedPipe", wt.BOOL, wt.HANDLE, wt.LPVOID, wt.DWORD,
                            wt.LPVOID, ct.POINTER(wt.DWORD), wt.LPVOID)
get_console_mode = kernel_function("GetConsoleMode", wt.BOOL, wt.HANDLE, ct.POINTER(wt.DWORD))
set_console_mode = kernel_function("SetConsoleMode", wt.BOOL, wt.HANDLE, wt.DWORD)

JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 258
ERROR_ACCESS_DENIED = 5
JOB_PREFIX = "Local\\CodexCastor-"
CANCEL_EXIT_CODE = 0xC0D30001


class BasicLimitInformation(ct.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ct.c_longlong), ("PerJobUserTimeLimit", ct.c_longlong),
        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ct.c_size_t), ("MaximumWorkingSetSize", ct.c_size_t),
        ("ActiveProcessLimit", wt.DWORD), ("Affinity", ct.c_size_t), ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class ExtendedLimitInformation(ct.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimitInformation), ("IoCounters", ct.c_ulonglong * 6),
        ("ProcessMemoryLimit", ct.c_size_t), ("JobMemoryLimit", ct.c_size_t),
        ("PeakProcessMemoryUsed", ct.c_size_t), ("PeakJobMemoryUsed", ct.c_size_t),
    ]


class Job:
    def __init__(self):
        self.name = JOB_PREFIX + uuid.uuid4().hex
        self.lock = threading.Lock()
        self.handle = create_job(None, self.name)
        if not self.handle:
            raise ct.WinError(ct.get_last_error())
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not set_job_info(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ct.byref(info), ct.sizeof(info)):
            error = ct.get_last_error()
            self.close()
            raise ct.WinError(error)

    def assign(self, pid):
        process = open_process(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise ct.WinError(ct.get_last_error())
        try:
            if not assign_job(self.handle, process):
                raise ct.WinError(ct.get_last_error())
        finally:
            close_handle(process)

    def terminate(self):
        with self.lock:
            return bool(self.handle and terminate_job(self.handle, CANCEL_EXIT_CODE))

    def close(self):
        with self.lock:
            if self.handle:
                close_handle(self.handle)
                self.handle = None


def launch_in_job(command, *, input_path=None, **options):
    """Hold the launcher at a one-byte gate until the worker and its descendants are tracked."""
    job = Job()
    process = None
    try:
        launcher = [sys.executable, str(Path(__file__).absolute()), str(input_path or ""), *command]
        process = subprocess.Popen(launcher, stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW, **options)
        job.assign(process.pid)
        process.castor_job = job
        process.stdin.write(b"1")
        process.stdin.flush()
        if input_path is not None:
            process.stdin.close()
    except BaseException:
        job.close()
        if process is not None:
            process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise

    def release_job():
        process.wait()
        job.close()

    threading.Thread(target=release_job, daemon=True).start()
    return process


def valid_job_name(name):
    return isinstance(name, str) and re.fullmatch(re.escape(JOB_PREFIX) + r"[0-9a-f]{32}", name)


def member(process, job):
    in_job = wt.BOOL()
    return bool(is_process_in_job(process, job, ct.byref(in_job)) and in_job.value)


def process_alive(pid, job_name=None):
    if pid > 0xFFFFFFFF:
        return False
    job = None
    if job_name is not None:
        if not valid_job_name(job_name):
            return False
        job = open_job(JOB_OBJECT_QUERY, False, job_name)
        if not job:
            return False
    process = open_process(SYNCHRONIZE | (PROCESS_QUERY_LIMITED_INFORMATION if job else 0), False, pid)
    try:
        if not process:
            return not job and ct.get_last_error() == ERROR_ACCESS_DENIED
        return wait_for_handle(process, 0) == WAIT_TIMEOUT and (not job or member(process, job))
    finally:
        if process:
            close_handle(process)
        if job:
            close_handle(job)


def terminate_recorded_job(pid, job_name):
    # Never act on a bare or recycled PID. Verify membership with handles held open.
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or not valid_job_name(job_name):
        return False
    job = open_job(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, False, job_name)
    if not job:
        return False
    process = open_process(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        return bool(process and wait_for_handle(process, 0) == WAIT_TIMEOUT and member(process, job)
                    and terminate_job(job, CANCEL_EXIT_CODE))
    finally:
        if process:
            close_handle(process)
        close_handle(job)


def pipe_bytes_available(handle):
    available = wt.DWORD()
    if not peek_pipe(handle, None, 0, None, ct.byref(available), None):
        error = ct.get_last_error()
        if error not in (109, 232, 233):  # A closed or disconnected pipe has no more input.
            raise ct.WinError(error)
    return available.value


def enable_virtual_terminal(handle):
    mode = wt.DWORD()
    return bool(get_console_mode(handle, ct.byref(mode)) and set_console_mode(handle, mode.value | 0x0004))


def launch_worker():
    import msvcrt

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    if os.read(sys.stdin.fileno(), 1) != b"1":
        return 1
    input_path, *command = sys.argv[1:]
    options = {"stdout": sys.stdout, "stderr": sys.stderr, "creationflags": subprocess.CREATE_NO_WINDOW}
    if input_path:
        with open(input_path, "rb") as prompt:
            return subprocess.call(command, stdin=prompt, **options)
    return subprocess.call(command, stdin=sys.stdin, **options)


if __name__ == "__main__":
    sys.exit(launch_worker())
