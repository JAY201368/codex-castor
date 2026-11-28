"""Native fixture executables for the integration tests."""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path


WINDOWS = os.name == "nt"


@lru_cache(maxsize=1)
def launcher():
    directory = tempfile.TemporaryDirectory(prefix="castor-test-launcher-")
    atexit.register(directory.cleanup)
    output = Path(directory.name) / "launcher.exe"
    framework = Path(os.environ["SystemRoot"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    compiler = framework / "csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:exe", f"/out:{output}",
                    str(Path(__file__).with_name("windows_fixture.cs"))], check=True, timeout=30)
    return output


def script_executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        path = path.with_suffix(".exe")
        shutil.copyfile(launcher(), path)
        path.with_suffix(".py").write_text(body, encoding="utf-8")
    else:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return path


def environment():
    return {**os.environ, "CASTOR_TEST_PYTHON": sys.executable, "PYTHONUTF8": "1"}


def platform_shell(command):
    if WINDOWS:
        cmd = subprocess.list2cmdline([os.environ["COMSPEC"]])
        return f'{cmd} /d /s /c "{command}"'
    return ["/bin/sh", "-c", command]
