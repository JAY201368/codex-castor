# Windows 11 x64 verification

[中文](#中文)

## Automated checks

Run from the checkout in PowerShell with native x64 Python and Git for Windows. The Windows tests use the in-box .NET Framework C# compiler to build a small fixture executable that exercises the same process and pipe handling as `codex.exe`. Node.js enables the existing Monitor JavaScript tests.

```powershell
py -3 -X utf8 -m unittest discover -s tests -v
py -3 scripts/doctor.py --repo-only
```

GitHub Actions runs these checks on Windows Server 2025 x64 with Python 3.9 and 3.14, and on macOS and Linux. The Windows CI job exercises the native APIs. Run the following checks on Windows 11 x64 with the actual Antigravity clients before declaring that baseline verified.

## Windows 11 x64 end-to-end checks

1. Sign in to the native Codex CLI and to Antigravity App and CLI. Use a standard Windows account and a disposable Git repository. Include spaces and non-ASCII characters in the repository path. Verify `Get-Command git.exe, codex.exe` and `agy --version` in PowerShell.
2. Run `py -3 scripts/install.py --install-statusline`. Restart Antigravity, then run `py -3 scripts/doctor.py`. Check the Git and Codex probes, the installed MCP and hook entries, the status line, and the MCP handshake. Rerun the installer and check that it preserves existing unrelated settings.
3. In the App, request a small edit using `codex_dispatch` with `use_worktree: true`. Confirm that dispatch runs in the background and Antigravity wakes automatically on completion. Ask for a follow-up change, verify the first reply appears verbatim in the previous round, and run `codex_resume` using the original task ID. Inspect the report and the worktree diff.
4. Open Codex Monitor while a task is active. Check live activity, session details, and the quota indicator when the Codex account provides quota data. Add feedback and check it in both Monitor and `py -3 scripts/analytics.py summary`. Run `py -3 scripts/watch.py --follow TASK_ID` in PowerShell. In Antigravity CLI, verify the status line shows a live task below the built-in status and clears once the task ends.
5. Dispatch a slow task in the disposable repository and have it start a child process. Use **Cancel** in Monitor, then repeat on a separate task using MCP request cancellation. Check that each task changes to `cancelled` and its recorded workers and descendants exit. Repeat while closing the Antigravity client and confirm its active task leaves no worker behind. Use Task Manager or `Get-CimInstance Win32_Process` to check the recorded PIDs.
6. Review or discard the worktree edits, then use `codex_cleanup_worktree` and check `git worktree list`. Close Antigravity and run `py -3 scripts/install.py --uninstall`. Verify the managed settings are removed, other entries remain, and the runtime state is preserved. In the disposable test installation, run the command again with `--purge-state` and check that the saved state is removed.

## 中文

### 自动化检查

在 Windows 11 x64 的 PowerShell 中，从仓库目录用原生 Python 和 Git for Windows 执行。测试用系统自带的 .NET Framework C# 编译器生成测试程序，走与 `codex.exe` 相同的进程和管道路径。安装 Node.js 后会额外执行现有 Monitor JavaScript 测试。

```powershell
py -3 -X utf8 -m unittest discover -s tests -v
py -3 scripts/doctor.py --repo-only
```

GitHub Actions 在 Windows Server 2025 x64 上用 Python 3.9、3.14 检查原生 API，并回归 macOS 和 Linux。Windows 11 x64 验收还需用真实的 Antigravity App 和 CLI 执行以下流程。

### Windows 11 x64 实机检查

1. 用普通 Windows 账户登录原生 Codex CLI、Antigravity App 和 CLI。准备一个可丢弃的 Git 仓库，路径包含空格和中文。在 PowerShell 中确认 `Get-Command git.exe, codex.exe` 和 `agy --version` 可用。
2. 运行 `py -3 scripts/install.py --install-statusline`。重启 Antigravity 后运行 `py -3 scripts/doctor.py`，检查 Git、Codex、MCP 配置和握手、钩子及状态栏。再次运行安装器，确认其他已有配置保留。
3. 在 App 中用 `codex_dispatch` 派发一个小改动，并设置 `use_worktree: true`。确认任务在后台运行，结束后自动唤醒 Antigravity。提出一项追加修改，确认上一轮原样保存首条回复，再用原任务 ID 调用 `codex_resume`。检查交付报告和 worktree 改动。
4. 任务运行中打开 Codex Monitor，检查实时活动、会话详情，以及账户提供额度数据时的额度显示。提交反馈，确认 Monitor 和 `py -3 scripts/analytics.py summary` 都能看到结果。用 `py -3 scripts/watch.py --follow TASK_ID` 跟踪任务。在 Antigravity CLI 中确认任务显示在内置状态栏下方，结束后清空。
5. 在测试仓库派发一个会启动子进程的慢任务，从 Monitor 点击 **Cancel**，再对另一个任务测试 MCP 请求取消。确认任务进入 `cancelled`，记录的进程和后代都退出。另起任务后关闭 Antigravity，检查也能清理进程。可用任务管理器或 `Get-CimInstance Win32_Process` 核对 PID。
6. 审查或丢弃 worktree 的改动后，调用 `codex_cleanup_worktree`，并用 `git worktree list` 验证。关闭 Antigravity，运行 `py -3 scripts/install.py --uninstall`，确认安装器写入的配置已移除、其他配置与运行状态保留。在这套测试安装上再带 `--purge-state` 执行一次，确认历史状态已删除。
