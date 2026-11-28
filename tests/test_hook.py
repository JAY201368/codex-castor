"""Unit and subprocess tests for the Antigravity PreInvocation hook."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "hook.py"
SPEC = importlib.util.spec_from_file_location("castor_hook", HOOK)
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def step(kind, content, **fields):
    return {"type": kind, "content": content, **fields}


def user(content, source="USER_EXPLICIT"):
    return step("USER_INPUT", content, source=source, status="DONE")


def call(name, **args):
    return {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": name, "args": args}]}


def launch(background, tool="codex_dispatch"):
    return step("GENERIC", (
        f"Created At: 2026-11-28\nTool is running as a background task with task id: {background}\n"
        f'Task Description: Called tool "mcp_codex-castor_{tool}" with arguments {{}}'
    ), source="MODEL")


def result(task, number=1, **fields):
    return json.dumps({"task_id": task, "round": number, "status": "completed", **fields}, indent=2)


def wakeup(background, body, *, sender_only=False):
    message = "Background tool" if sender_only else f'Task id "{background}"'
    return step(
        "SYSTEM_MESSAGE",
        f"System messages\n[Message] timestamp=2026-11-28 sender={background} content={message} finished with result:\n{body}",
        source="SYSTEM",
    )


def json_line(entry):
    return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class HookUnitTests(unittest.TestCase):
    def test_safe_conversation_id(self):
        self.assertEqual(hook.safe_conversation_id(".__team/ab :cd._"), "team_ab_cd")
        self.assertEqual(hook.safe_conversation_id("../"), "default")
        self.assertEqual(hook.safe_conversation_id("a" * 121), "a" * 120)

    def test_user_prompt_removes_only_wrapping_newlines(self):
        self.assertEqual(hook.user_prompt("  plain input  \n"), "plain input")
        content = "header<USER_REQUEST>\r\n\r\n  保留  \n\r\n</USER_REQUEST>\n<ADDITIONAL_METADATA>"
        self.assertEqual(hook.user_prompt(content), "\r\n  保留  \n")
        self.assertEqual(hook.user_prompt("<USER_REQUEST> literal "), "<USER_REQUEST> literal")

    def test_result_signature_and_invalid_rounds(self):
        signature = ("artifacts", "workdir")
        self.assertEqual(hook.result_round(result("ok", workdir="/tmp") + "\ntrailer", signature), ("ok", 1))
        self.assertEqual(hook.result_round(result("ok", artifacts={})[:-3], signature), ("ok", 1))
        for task, number in (("../escape", 1), (".", 1), ("..", 1), ("", 1), ("ok", 0), ("ok", True), ("ok", "1")):
            with self.subTest(task=task, number=number):
                self.assertIsNone(hook.result_round(result(task, number, artifacts={}), signature))
        self.assertIsNone(hook.result_round(result("ok"), signature))

    def test_background_tracking_is_deduplicated_and_bounded(self):
        state = {"bg_tasks": [], "expect_sync_result": True}
        for background in [*(f"conv/task-{index}" for index in range(51)), "conv/task-1"]:
            hook.completed_round("GENERIC", launch(background)["content"], state)
        hook.completed_round("GENERIC", launch("conv/unrelated", "other_tool")["content"], state)
        self.assertEqual(state["bg_tasks"], [*(f"conv/task-{index}" for index in range(2, 51)), "conv/task-1"])
        truncated = '{ "task_id": "known", "round": 3, "status": "comple'
        self.assertEqual(
            hook.completed_round("SYSTEM_MESSAGE", wakeup("conv/task-1", truncated)["content"], state), ("known", 3),
        )
        self.assertNotIn("conv/task-1", state["bg_tasks"])

    def test_failed_feedback_does_not_prevent_other_captures(self):
        state = {
            "pending": {"missing": {"round": 1, "seen_line": 4}, "present": {"round": 2, "seen_line": 5}},
            "recorded": {},
            "transcript_offset": 0,
            "line_number": 5,
        }
        with tempfile.TemporaryFile() as stream:
            stream.write(json_line(user("reply")))
            with mock.patch.object(hook, "can_capture", return_value=True), mock.patch(
                "telemetry.record_feedback", side_effect=[OSError("write failed"), None],
            ) as write:
                hook.scan(stream, Path("/unused"), state, None)
        self.assertEqual([item.args[1] for item in write.call_args_list], ["missing", "present"])
        self.assertEqual(state["pending"], {})


class HookSubprocessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-castor-hook-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_root = self.root / "castor"
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.touch()
        self.env = {**os.environ, "CODEX_CASTOR_STATE_DIR": str(self.state_root)}

    def invoke(self, live=None, *, conversation="conv", transcript=None, raw=None, script=HOOK, **fields):
        payload = {
            "conversationId": conversation, "transcriptPath": str(transcript or self.transcript),
            "hookEventName": "PreInvocation", "lastUserInput": live, **fields,
        }
        completed = subprocess.run(
            [sys.executable, str(script)], input=json.dumps(payload) if raw is None else raw,
            env=self.env, cwd=self.root, text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "{}\n")
        return completed

    def append(self, *entries, transcript=None):
        with (transcript or self.transcript).open("ab") as stream:
            for entry in entries:
                stream.write(json_line(entry))

    def state_path(self, conversation="conv"):
        return self.state_root / "hook_state" / f"{hook.safe_conversation_id(conversation)}.json"

    def task(self, name, **fields):
        path = self.state_root / "tasks" / name / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "task_id": name, "round": 1, "status": "completed", "duration_seconds": 1.0, **fields,
        }), encoding="utf-8")
        return path

    def prompts(self, path):
        return {
            entry["round"]: entry["prompt"]
            for entry in read(path).get("feedback", {}).get("user_first_prompts", [])
        }

    def test_parallel_background_rounds_capture_identical_wrapped_reply_verbatim(self):
        first = self.task("first", feedback={"rating": 4, "notes": "Monitor note"})
        second = self.task("second")
        prompt = "  保留空格\n和换行  "
        wrapped = f"<USER_REQUEST>\r\n{prompt}\r\n</USER_REQUEST>\n<ADDITIONAL_METADATA>context"
        self.append(user(wrapped), launch("conv/task-656"), launch("conv/task-657"))
        self.invoke(prompt)
        self.assertEqual(read(self.state_path())["bg_tasks"], ["conv/task-656", "conv/task-657"])

        self.append(
            wakeup("conv/task-656", result("first") + "\ntrailer"),
            wakeup("conv/task-657", result("second"), sender_only=True),
            user("system continuation", source="SYSTEM"),
        )
        self.invoke(prompt)
        state = read(self.state_path())
        self.assertEqual(state["bg_tasks"], [])
        self.assertEqual(state["pending"], {
            "first": {"round": 1, "seen_line": 4, "baseline_user_input": prompt},
            "second": {"round": 1, "seen_line": 5, "baseline_user_input": prompt},
        })
        self.assertEqual(self.prompts(first), {})
        self.append(user(wrapped))
        self.invoke(prompt)
        self.assertEqual(self.prompts(first), {1: prompt})
        self.assertEqual(self.prompts(second), {1: prompt})
        self.assertEqual(read(first)["feedback"]["rating"], 4)
        self.assertEqual(read(first)["feedback"]["notes"], "Monitor note")
        self.assertEqual(read(first)["rounds"][0]["user_first_prompt"], prompt)
        state_path = self.state_path()
        state = read(state_path)
        self.assertEqual(state["recorded"], {"first": [1], "second": [1]})
        self.assertEqual(state["pending"], {})
        self.assertEqual(state["transcript_offset"], self.transcript.stat().st_size)
        self.assertEqual(state["line_number"], 7)
        for name in ("telemetry.jsonl", "telemetry.csv", "report.md"):
            self.assertTrue((self.state_root / name).is_file(), name)
        info = state_path.stat()
        self.invoke(prompt)
        self.assertEqual((state_path.stat().st_ino, state_path.stat().st_mtime_ns), (info.st_ino, info.st_mtime_ns))

    def test_last_user_input_fallback_only_when_baseline_is_known_and_changes(self):
        task = self.task("fallback")
        self.append(wakeup("unknown", result("fallback", workdir="/tmp")))
        self.invoke("original")
        self.invoke("original")
        self.assertEqual(self.prompts(task), {})
        self.append(wakeup("unknown", result("fallback", workdir="/tmp")))
        self.invoke("  New\nreply  ")
        self.assertEqual(self.prompts(task), {1: "  New\nreply  "})
        original = task.read_bytes()
        self.append(user("New\nreply"))
        self.invoke("New\nreply")
        self.assertEqual(task.read_bytes(), original)

        unknown = self.task("unknown-baseline")
        self.append(wakeup("unknown", result("unknown-baseline", duration_seconds=1)))
        self.invoke()
        self.invoke("ahead of transcript")
        self.assertEqual(self.prompts(unknown), {})
        self.append(user("ahead of transcript", source=None))
        self.invoke()
        self.assertEqual(self.prompts(unknown), {1: "ahead of transcript"})

    def test_sync_dispatch_and_auto_resume_use_latest_round(self):
        task = self.task("two-rounds", round=2)
        direct = self.task("direct")
        unrelated = self.task("unrelated")
        self.append(
            user("initial"),
            call("mcp_codex-castor_codex_dispatch"),
            step("GENERIC", result("two-rounds", artifacts={})),
            step("GENERIC", result("direct", artifacts={})),
            call("other_tool"),
            step("GENERIC", result("unrelated", artifacts={})),
        )
        self.invoke("initial")
        self.assertEqual(read(self.state_path())["pending"]["two-rounds"]["round"], 1)
        self.append(call("call_mcp_tool", ToolName="codex_resume"))
        self.invoke("initial")
        self.assertTrue(read(self.state_path())["expect_sync_result"])
        self.append(
            step("GENERIC", "Tool result:\n" + result("two-rounds", 2, artifacts={})[:-3]),
            wakeup("unknown", result("two-rounds", artifacts={})),
            user("  rework again  "),
        )
        self.invoke("rework again")
        self.assertEqual(self.prompts(task), {2: "rework again"})
        self.assertEqual(self.prompts(direct), {1: "rework again"})
        self.assertEqual(self.prompts(unrelated), {})
        self.assertFalse(read(self.state_path())["expect_sync_result"])

    def test_unflushed_and_malformed_lines_do_not_lose_the_cursor(self):
        task = self.task("truncated")
        wake = json_line(wakeup("unknown", result("truncated", artifacts={"info": "中"})[:-6]))
        with self.transcript.open("ab") as stream:
            stream.write(b"{malformed\n" + wake[:-1])
        self.invoke("initial")
        state = read(self.state_path())
        self.assertEqual(state["transcript_offset"], len(b"{malformed\n"))
        self.assertEqual(state["line_number"], 1)
        self.assertEqual(state["pending"], {})

        with self.transcript.open("ab") as stream:
            stream.write(wake[-1:] + json_line(user("first reply")))
        self.invoke("first reply")
        self.assertEqual(self.prompts(task), {1: "first reply"})
        self.assertEqual(read(self.state_path())["transcript_offset"], self.transcript.stat().st_size)

    def test_validates_metadata_and_leaves_existing_feedback_in_place(self):
        running = self.task("running", status="running")
        prior = self.task("prior", status="starting", round=2)
        from_feedback = self.task("existing-feedback", feedback={"user_first_prompts": [{"round": 1, "prompt": "old"}]})
        from_rounds = self.task("existing-round", rounds=[{"round": 1, "user_first_prompt": "old"}])
        false_match = self.task("unrelated")
        originals = {path: path.read_bytes() for path in (running, from_feedback, from_rounds, false_match)}
        self.append(
            user("before"),
            wakeup("unknown", result("unrelated")),
            *(wakeup("unknown", result(name, artifacts={})) for name in (
                "running", "prior", "existing-feedback", "existing-round", "missing", "../unsafe",
            )),
            wakeup("unknown", result("prior", 2, artifacts={})),
            user("accepted"),
        )
        self.invoke("accepted")
        self.assertEqual(self.prompts(prior), {1: "accepted"})
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.assertEqual(read(self.state_path())["recorded"], {"prior": [1]})

    def test_compaction_rotation_and_path_changes_clear_pending_but_keep_recorded(self):
        for method in ("shrink", "replace", "switch"):
            with self.subTest(method=method):
                done = self.task(f"done-{method}")
                discarded = self.task(f"discarded-{method}")
                new = self.task(f"new-{method}")
                transcript = self.root / f"{method}.jsonl"
                transcript.touch()
                self.append(
                    user("p" * 1500), wakeup("unknown", result(done.parent.name, artifacts={})), user("original reply"),
                    launch("conv/background"), wakeup("unknown", result(discarded.parent.name, artifacts={})),
                    call("mcp_codex-castor_codex_resume"), transcript=transcript,
                )
                self.invoke("original reply", conversation=method, transcript=transcript)
                before = read(self.state_path(method))
                self.assertIn(discarded.parent.name, before["pending"])
                self.assertEqual(before["bg_tasks"], ["conv/background"])
                self.assertTrue(before["expect_sync_result"])
                # Remove the metadata prompt to verify the hook also remembers the captured round.
                self.task(done.parent.name)
                new_lines = b"".join(map(json_line, (
                    wakeup("unknown", result(done.parent.name, artifacts={})),
                    wakeup("unknown", result(new.parent.name, artifacts={})),
                    user("new reply"),
                )))
                if method == "shrink":
                    transcript.write_bytes(new_lines)
                elif method == "replace":
                    replacement = transcript.with_suffix(".new")
                    replacement.write_bytes(new_lines + json_line(user("p" * 3000)))
                    replacement.replace(transcript)
                else:
                    transcript = transcript.with_suffix(".new")
                    transcript.write_bytes(new_lines)
                self.invoke("new reply", conversation=method, transcript=transcript)
                after = read(self.state_path(method))
                self.assertEqual(after["recorded"], {done.parent.name: [1], new.parent.name: [1]})
                self.assertEqual(after["pending"], {})
                self.assertEqual(after["bg_tasks"], [])
                self.assertFalse(after["expect_sync_result"])
                self.assertEqual(after["transcript_offset"], len(transcript.read_bytes()))
                self.assertEqual(self.prompts(discarded), {})
                self.assertEqual(self.prompts(new), {1: "new reply"})

    def test_conversations_have_isolated_state_even_with_colliding_safe_ids(self):
        pending = self.task("only-a")
        other = self.task("only-b")
        transcript_b = self.root / "other.jsonl"
        self.append(user("baseline"), wakeup("unknown", result("only-a", artifacts={})))
        self.invoke("baseline", conversation="conv/a")
        self.append(
            user("unrelated message"), wakeup("unknown", result("only-b", artifacts={})), transcript=transcript_b,
        )
        self.invoke("unrelated message", conversation="conv?a", transcript=transcript_b)
        self.assertEqual(self.prompts(pending), {})
        self.invoke("reply to A", conversation="conv/a")
        self.assertEqual(self.prompts(pending), {1: "reply to A"})
        self.assertEqual(self.prompts(other), {})
        self.invoke("reply to B", conversation="conv?a", transcript=transcript_b)
        self.assertEqual(self.prompts(other), {1: "reply to B"})

    def test_fail_open_for_invalid_inputs_and_unreadable_dependencies(self):
        for raw in ("", "{bad JSON", "[]", "null", '{"transcriptPath": 7}'):
            with self.subTest(raw=raw):
                self.invoke(raw=raw)
        self.invoke(hookEventName="PostInvocation")
        self.assertFalse(self.state_root.exists())
        self.invoke(transcript=self.root / "no-file")
        self.invoke(transcript=self.root)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.invoke(transcript=fifo)
        self.invoke(transcriptPath="bad\0path")
        self.assertFalse(self.state_path().exists())
        standalone = self.root / "hook.py"
        standalone.write_bytes(HOOK.read_bytes())
        self.invoke(script=standalone)


if __name__ == "__main__":
    unittest.main()
