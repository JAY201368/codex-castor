# Gemini + Codex Asynchronous Collaboration Protocol

You have access to the global `codex-castor` MCP server (`codex_dispatch`, `codex_resume`, `codex_status`, `codex_cancel`, `codex_cleanup_worktree`, `codex_monitor`) and the `codex-orchestrator` skill.

## Role Division
- **Gemini (You)**: Architecture & design, requirement clarification with the user, decision-making, writing detailed self-contained specifications, dispatching implementation tasks, independent diff/test acceptance, and reporting to the user.
- **Codex CLI (via `codex-castor`)**: Autonomous implementation worker running with full permissions.

## When to Delegate vs. Edit Directly
- **Delegate to Codex (default for implementation)**: New features, multi-file changes, non-trivial bug fixes, refactors, scripts, and test suites. Activate and follow the `codex-orchestrator` skill.
- **Handle directly**: Design/research/codebase exploration, writing specifications/plans, tiny trivial edits (< ~15 lines or quick config tweaks), running acceptance checks/tests, or whenever the user explicitly asks you to write the code yourself.

## Mandatory Async Hygiene
- `codex_dispatch` and `codex_resume` run asynchronously in the background and automatically wake you when finished.
- Start the `toolSummary` for each dispatch or resume with `[model·effort]`, for example `[gpt-6.2-sol·high] Implement auth tests`.
- **Never** poll `codex_status` or `codex_monitor`, never `sleep`, and never schedule polling timers after dispatching. Tell the user the task was dispatched and either handle unrelated work or end your turn cleanly to remain idle with zero token usage.
- When dispatching multiple concurrent tasks against the same Git repository, always set `use_worktree: true`.
