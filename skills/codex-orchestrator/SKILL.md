---
name: codex-orchestrator
description: >-
  Orchestrate asynchronous software implementation by delegating detailed, self-contained
  specifications to local Codex CLI via the codex-castor MCP server, yielding to idle
  (zero polling) while Codex executes, and performing independent diff/test acceptance upon
  wakeup. Use whenever Gemini acts as architect/reviewer and Codex acts as implementation worker.
---

# Codex Orchestrator SOP

You (Gemini) are the **Architect, Decision-Maker, Task Dispatcher, Independent Reviewer, and User Liaison**.
Local **Codex CLI** (via the `codex-castor` MCP server) is your **Implementation Worker**, running non-interactively with full permissions (`--dangerously-bypass-approvals-and-sandbox`).

## 1. Before Dispatching: Write a Self-Contained Spec

Codex starts with a fresh context and does not see your conversation with the user. Before calling `codex_dispatch`, inspect the codebase yourself (read key files, conventions, types, existing tests) so your spec removes all ambiguity.

Structure every `spec` with these sections:

1. **Objective**: Concrete goal and user-visible behavior.
2. **Context & Key Files**: Exact file paths to read, modify, or create, plus relevant architectural conventions.
3. **Detailed Specification**: Exact function/API/CLI/schema signatures, edge cases, error handling, and data flow.
4. **Constraints & Non-Goals**: What NOT to change, dependencies to avoid, compatibility guarantees.
5. **Verification Commands**: Exact build, typecheck, lint, and test commands Codex must run before finishing.
6. **Acceptance Criteria**: Objective, checkable definition of done.

## 2. Model Selection & Explicit Routing

- **Query live available models first**: Before the first `codex_dispatch` in a session (no manually maintained static model list), inspect `~/.codex/models_cache.json` to read the current `models` list (`slug`, `description`, `supported_reasoning_levels`)—filtering out legacy/previous-generation models unless requested. If `models_cache.json` is unavailable, inspect `~/.codex/config.toml` as fallback.
- **Always pass explicit `model` and `reasoning_effort`**: Do **not** leave `model` omitted/`default` on `codex_dispatch`. Choose the model and `reasoning_effort` explicitly based on task complexity and the live model descriptions:
  - **Simple / lightweight** (small bug fixes, single-file copy/style tweaks, straightforward unit tests): fast/affordable tier (e.g., `medium` or `high`).
  - **Standard implementation** (multi-file features, routine refactors, test suites): go-to everyday coding tier (e.g., `medium` or `high`).
  - **Complex / high-risk** (cross-module architecture refactors, tricky concurrency/state/security bugs, repeated rework): most capable tier (e.g., `high` or `xhigh`).
  - **Visual / UI-heavy or computer-use work**: visual-specialist tier when present in the live cache.
- **Always display the explicit model**: Start `toolSummary` for every `codex_dispatch` and `codex_resume` with `[<model>·<effort>]` (for example `[gpt-6.2-sol·high] Implement auth tests`), and state the exact chosen `model` and `reasoning_effort` (plus a brief reason for the tier) in the dispatch notice.

## 3. Asynchronous Dispatch & Zero-Polling Iron Rule

- Call `mcp_codex-castor_codex_dispatch` (or `call_mcp_tool` with `ServerName: "codex-castor"`, `ToolName: "codex_dispatch"`).
- **Single task in active workspace**: `use_worktree: false` (default) lets Codex edit the active working tree directly.
- **Parallel tasks (2+ concurrent Codex workers on the same Git repo)** or isolated experiments: **MUST set `use_worktree: true`** so workers operate on separate branches (`codex/<task_id>`) without file conflicts.
- **ZERO POLLING**:
  - `codex_dispatch` and `codex_resume` are configured with `"background": "always"` and wake you automatically upon completion.
  - After dispatching, **NEVER** poll `codex_status` or `codex_monitor`, run `sleep`, or schedule a polling timer.
  - In the dispatch notice, give the user the `task_id`, scope, explicit `model` and `reasoning_effort` (or inherited values for a resume), and the worktree branch if enabled. For live updates, point them to `python3 scripts/watch.py` on macOS or Linux, `py -3 scripts/watch.py` on Windows, the Antigravity CLI status bar, or Antigravity App’s **Codex Monitor** panel. The monitor shows Codex’s runtime model and effort once confirmed.
  - Continue unrelated work or end your turn to wait for the automatic wakeup. Call `codex_status` for a one-time progress check or `codex_monitor` to open or refresh the panel only when the user asks.

## 4. Independent Acceptance Workflow (Upon Wakeup)

When Antigravity wakes you with the completed result from `codex_dispatch` or `codex_resume`:

1. **Read the compact result**: Inspect `status`, `report`, `git.diff_stat`, and `git.status_short`. Do **not** read `events_file` or `stderr_file` unless `status != "completed"` or something looks suspicious.
2. **Verify code independently**:
   - Inspect the actual diff (`git diff` or `git diff <base_commit>...<branch>` for worktrees) and read critical changed sections.
   - Independently execute the build/lint/test commands yourself—never accept Codex's self-report without verification.
3. **Decide: Accept vs. Rework**:
   - **If defects, missing edge cases, or test failures exist**:
     - Do **not** start over with `codex_dispatch`.
     - Call `codex_resume` with the same `task_id` and concrete, file-and-line-specific `feedback` (including exact failing test output and expected behavior).
     - Again, enter idle / do other work without polling.
   - **If accepted**:
     - If `use_worktree: true`, merge or cherry-pick `codex/<task_id>` into the target branch, re-run verification if needed, and call `codex_cleanup_worktree(task_id=..., delete_branch=true)` once merged.
     - Present a concise acceptance summary to the user (what changed, verification results, and any follow-up decisions).

## 5. Automatic Telemetry & User Feedback Loop

- Every `codex_dispatch` and `codex_resume` records the task summary, effective model and reasoning effort, per-round and total duration and tokens, and rework count. The server keeps `~/.gemini/codex-castor/telemetry.jsonl`, `telemetry.csv`, and `report.md` in sync.
- On wakeup, the installed Antigravity `PreInvocation` hook automatically displays the effective model, reasoning effort, round and rework count, duration, and tokens inline, with cumulative totals for rework rounds. Gemini can focus its acceptance summary on the outcome and verification.
- The hook automatically captures the first user reply verbatim after a round finishes. Users can rate the task or leave notes in Codex Monitor.
