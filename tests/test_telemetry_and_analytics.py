"""Telemetry tests using archived artifacts and the MCP stdio fixture."""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import test_server

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "analytics.py"
sys.path.insert(0, str(ROOT / "mcp"))

import telemetry


def write_history(state, task_id="historic", *, created="2026-11-01T12:00:00Z", **overrides):
    folder = state / "tasks" / task_id
    folder.mkdir(parents=True)
    meta = {
        "task_id": task_id,
        "round": 2,
        "status": "completed",
        "exit_code": 0,
        "session_id": test_server.SESSION,
        "requested_workdir": str(state.parent / "Main Project"),
        "effective_workdir": str(state.parent),
        "created_at": created,
        "round_started_at": "2026-11-01T12:00:30Z",
        "finished_at": "2026-11-01T12:00:37Z",
        "duration_seconds": 7.5,
        "effective_model": "sol",
        "reasoning_effort": "high",
        "model_source": "runtime_rollout",
        "usage": {"total_tokens": 4444},
        **overrides,
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    artifacts = {
        "spec.md": "# Work item\n\n## 1. Objective\n\n**Keep task | history.**\n\n## Tests\nRun them.",
        "feedback-r2.md": "  - Review notes and rerun tests.",
        "events-r1.jsonl": (
            '{bad json}\nnull\n{"type":"turn.completed","usage":{"input_tokens":1}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":10,'
            '"reasoning_output_tokens":3,"cache_write_input_tokens":2}}'
        ),
        "events-r2.jsonl": (
            '{"type":"turn.completed","usage":{"input_tokens":60,"output_tokens":20,"total_tokens":80}}\n'
            '{"type":"turn.completed"}'
        ),
    }
    start = 1793534400
    for index, (name, content) in enumerate(artifacts.items()):
        path = folder / name
        path.write_text(content, encoding="utf-8")
        os.utime(path, (start + index * 10, start + index * 10))
    return folder, meta


class LedgerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="castor-telemetry-tests-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        patch = mock.patch.dict(os.environ, {"CODEX_CASTOR_STATE_DIR": str(self.state)})
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *args, success=True):
        process = subprocess.run(
            [sys.executable, str(CLI), *map(str, args)], cwd=self.root,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(process.returncode == 0, success, process.stderr)
        return process

    def test_usage_and_plain_summary(self):
        self.assertEqual(telemetry.default_state_root(), self.state)
        self.assertIsNone(telemetry.sum_usage([{"usage": None}, {}]))
        self.assertEqual(telemetry.sum_usage([{"usage": {}}])["total_tokens"], 0)
        usage = telemetry.normalize_usage({
            "input_tokens": 15, "cached_input_tokens": 8, "cache_write_input_tokens": 2,
            "output_tokens": 10, "reasoning_output_tokens": 9, "total_tokens": None,
        })
        self.assertEqual(usage["total_tokens"], 25)
        self.assertEqual(usage["reasoning_output_tokens"], 9)
        self.assertEqual(telemetry.normalize_usage({"input_tokens": 15, "total_tokens": 0})["total_tokens"], 0)
        self.assertEqual(telemetry.extract_summary("## Plan\n- Ship"), "Plan")
        self.assertEqual(telemetry.extract_summary("   -  Fix   tabs  \n"), "Fix tabs")
        self.assertEqual(telemetry.extract_summary("Alpha beta gamma", 12), "Alpha beta…")
        self.assertEqual(telemetry.extract_summary("w" * 15, 5), "wwww…")
        for heading in ("# Objective", "## Objective", "## 1. Objective"):
            text = f"# Title\n{heading}\n\n<!-- drafting notes -->\n~~~\ncode block\n~~~\n* Enable telemetry.\n## Next"
            self.assertEqual(telemetry.extract_summary(text), "Enable telemetry.")

    def test_session_title_uses_heading_or_objective(self):
        self.assertEqual(telemetry.extract_title(
            "<!-- # Hidden -->\n~~~md\n# Example\n~~~\n# Task Q2: **Review** [runtime](target) `state`\nBody"
        ), "Review runtime state")
        self.assertEqual(telemetry.extract_title(
            "## 1. Objective\n- Investigate and diagnose why exports fail （on CI） during upgrades: details"
        ), "Diagnose why exports fail during upgrades")
        self.assertEqual(telemetry.extract_title(
            "# Context\nA preliminary note\n## Objective\nImplement the approved session browser (mcp/monitor.html)."
        ), "Implement session browser")
        self.assertEqual(telemetry.extract_title(
            "Perform an independent, thorough, read-only root-cause analysis of lost events"
        ), "Root-cause analysis of lost events")
        self.assertEqual(telemetry.prompt_title(self.root / "missing", task_id="20261127-100300-diag-slow_jobs"),
                         "Diagnose slow jobs")

    def test_card_preview_omits_heading_code_and_comments(self):
        text = "# Build a monitor\n<!-- private notes -->\n## 1. Objective\n```sh\nsecret\n```\n"
        text += "- Show **live** [events](target).\n1. Include `commands`.\n"
        self.assertEqual(telemetry.extract_prompt_preview(text), "Show live events. Include commands.")
        self.assertEqual(telemetry.extract_prompt_preview("# Objective\nRead the full prompt"), "Read the full prompt")
        self.assertEqual(telemetry.prompt_preview(self.root / "missing"), "")

    def test_reconstruction_uses_event_usage_and_exact_final_duration(self):
        folder, meta = write_history(self.state)
        record = telemetry.reconstruct_task_record(folder, meta)
        first, second = record["rounds"]
        self.assertEqual(record["project"], "Main Project")
        self.assertEqual(record["source"], "backfill")
        self.assertEqual((record["round_count"], record["rework_count"]), (2, 1))
        self.assertEqual(record["task_summary"], "Keep task | history.")
        self.assertEqual(second["prompt_summary"], "Review notes and rerun tests.")
        self.assertEqual((first["kind"], second["kind"]), ("dispatch", "resume"))
        self.assertEqual((first["status"], second["status"]), ("completed", "completed"))
        self.assertEqual((first["duration_seconds"], second["duration_seconds"]), (20.0, 7.5))
        self.assertEqual((first["duration_estimated"], second["duration_estimated"]), (True, False))
        self.assertTrue(record["duration_estimated"])
        self.assertEqual(record["total_duration_seconds"], 27.5)
        self.assertEqual(record["total_usage"], {
            "total_tokens": 190, "input_tokens": 160, "cached_input_tokens": 40,
            "cache_write_input_tokens": 2, "output_tokens": 30, "reasoning_output_tokens": 3,
        })
        self.assertEqual(first["model"], "sol")
        self.assertEqual(second["reasoning_effort"], "high")
        (folder / "events-r1.jsonl").unlink()
        self.assertIsNone(telemetry.reconstruct_task_record(folder, meta)["rounds"][0]["duration_seconds"])
        self.assertEqual(telemetry.reconstruct_task_record(folder, {**meta, "rounds": [first]})["total_usage"]["total_tokens"], 190)

    def test_backfill_merges_metadata_and_ledger_feedback_and_exports_text(self):
        folder, meta = write_history(self.state, feedback={"rating": 4, "tags": ["fast"]})
        record = telemetry.reconstruct_task_record(folder, meta)
        record["feedback"] = {
            "notes": "Valid | clean\n<markup>", "verdict": "accepted",
            "user_first_prompts": [{"round": 1, "prompt": "请保留\n前一轮", "recorded_at": "2026-11-02T00:00:00Z"}],
        }
        telemetry.sync_exports(self.state, [record])
        for _ in range(2):
            [record] = telemetry.backfill(self.state)
        self.assertEqual(record["feedback"]["rating"], 4)
        self.assertEqual(record["feedback"]["tags"], ["fast"])
        self.assertEqual(record["feedback"]["notes"], "Valid | clean\n<markup>")
        self.assertEqual(record["rounds"][0]["user_first_prompt"], "请保留\n前一轮")
        self.assertEqual(len((self.state / "telemetry.jsonl").read_text().splitlines()), 1)
        with (self.state / "telemetry.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            [row] = list(reader)
            self.assertEqual(reader.fieldnames, list(telemetry.CSV_FIELDS))
        self.assertEqual(row["total_tokens"], "190")
        self.assertEqual(row["first_user_prompt"], "请保留\n前一轮")
        self.assertEqual(row["notes"], "Valid | clean\n<markup>")
        report = (self.state / "report.md").read_text()
        self.assertIn("Model × Reasoning Effort Performance Matrix", report)
        self.assertIn("Recent Tasks Log", report)
        self.assertIn("Valid \\| clean &lt;markup&gt; · First reply: 请保留 前一轮", report)

    def test_newer_ledger_feedback_wins_backfill_conflicts(self):
        folder, meta = write_history(self.state, feedback={
            "rating": 2, "tags": ["reviewed"], "updated_at": "2026-11-02T00:00:00Z",
        })
        record = telemetry.reconstruct_task_record(folder, meta)
        record["feedback"] = {"rating": 5, "updated_at": "2026-11-03T00:00:00Z"}
        telemetry.sync_exports(self.state, [record])
        [record] = telemetry.backfill(self.state)
        self.assertEqual(record["feedback"]["rating"], 5)
        self.assertEqual(record["feedback"]["tags"], ["reviewed"])

    def test_analytics_filters_group_sorting_and_counts(self):
        records = [
            {"model": "sol", "reasoning_effort": "medium", "project": "repo", "created_at": "2026-11-02T12:00:00Z",
             "status": "completed", "rework_count": 1, "total_duration_seconds": 10, "total_usage": {"total_tokens": 16},
             "feedback": {"rating": 4, "verdict": "accepted"}},
            {"model": "sol", "reasoning_effort": "medium", "project": "repo", "created_at": "2026-11-01T12:00:00Z",
             "status": "completed", "total_duration_seconds": 0, "total_usage": {"total_tokens": 0},
             "feedback": {"rating": 2, "verdict": "partial"}},
            {"model": "astra", "reasoning_effort": "high", "status": "failed", "rework_count": 2},
            {"status": "cancelled"},
        ]
        result = telemetry.compute_analytics(records)
        overall = result["overall"]
        fields = {
            "task_count": 4, "completed_count": 2, "failed_count": 1, "cancelled_count": 1,
            "completion_rate": 0.5, "total_reworks": 3, "rework_task_count": 2, "rework_rate": 0.5,
            "first_pass_rate": 0.5, "total_duration_seconds": 10, "avg_duration_seconds": 5,
            "total_tokens": 16, "avg_total_tokens": 8, "rated_count": 2, "avg_rating": 3,
        }
        for field, value in fields.items():
            self.assertEqual(overall[field], value, field)
        self.assertEqual(overall["verdict_counts"]["accepted"], 1)
        self.assertEqual([row["model"] for row in result["by_model_effort"]], ["sol", "astra", "unknown"])
        self.assertEqual(result["by_model_effort"][0]["avg_reworks"], 0.5)
        match = telemetry.compute_analytics(records, model="sol", effort="medium", project="repo", since="2026-11-02")
        self.assertEqual(match["overall"]["task_count"], 1)
        self.assertEqual(match["overall"]["avg_rating"], 4)
        empty = telemetry.compute_analytics(records, project="missing")
        self.assertEqual(empty["overall"]["first_pass_rate"], 0)
        self.assertIsNone(empty["overall"]["avg_total_tokens"])

    def test_concurrent_updates_and_transient_export_failure(self):
        for number in range(5):
            write_history(self.state, f"concurrent-{number}")
        blocked = self.state / "report.md"
        blocked.mkdir()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
            with ThreadPoolExecutor(max_workers=5) as executor:
                list(executor.map(lambda n: telemetry.record_feedback(
                    self.state, f"concurrent-{n}", {"rating": n + 1},
                ), range(5)))
        self.assertIn("could not write report.md", errors.getvalue())
        self.assertEqual({item["feedback"]["rating"] for item in telemetry.load_records(self.state)}, set(range(1, 6)))
        self.assertTrue((self.state / "telemetry.csv").is_file())
        blocked.rmdir()
        telemetry.record_feedback(self.state, "concurrent-0", {"notes": "Recovered"})
        self.assertIn("Recovered", blocked.read_text())
        self.assertFalse(list(self.state.glob(".report.md.*")))

    def test_cli_subcommands_filters_custom_outputs_and_validation(self):
        folder, _ = write_history(self.state)
        write_history(self.state, "newer", created="2026-11-10T12:00:00Z", effective_model="astra")
        before_backfill = json.loads(self.run_cli("summary", "--json", "--model", "sol", "--limit", 1).stdout)
        self.assertEqual(before_backfill["overall"]["task_count"], 1)
        self.assertEqual(before_backfill["recent_tasks"][0]["task_id"], "historic")
        summary = self.run_cli("summary", "--state-dir", self.state, "--since", "2026-11-10").stdout
        self.assertIn("Overall KPIs", summary)
        self.assertIn("Recent Tasks", summary)
        self.assertIn("newer", summary)
        self.assertNotIn("historic", summary)
        backfill = json.loads(self.run_cli("backfill", "--json").stdout)
        self.assertEqual((backfill["task_count"], backfill["round_count"]), (2, 4))
        self.assertEqual([item["task_id"] for item in telemetry.load_records(self.state)], ["newer", "historic"])
        response = json.loads(self.run_cli(
            "feedback", "historic", "--rating", 5, "--verdict", "accepted", "--tags", "quick, accurate",
            "--user-prompt", "  Great work!\n", "--notes", "Checks passed", "--round", 1,
        ).stdout)
        self.assertEqual(response["feedback"]["tags"], ["quick", "accurate"])
        meta = test_server.read_json(folder / "meta.json")
        self.assertEqual(meta["rounds"][0]["user_first_prompt"], "  Great work!\n")
        self.assertEqual(meta["rounds"][1]["user_first_prompt"], None)
        self.assertEqual(meta["feedback"]["rating"], 5)
        self.assertIn("Checks passed", (self.state / "report.md").read_text())
        md, csv_path = self.root / "custom.md", self.root / "custom.csv"
        self.run_cli("export", "--md", md, "--csv", csv_path)
        self.assertEqual(csv_path.read_bytes(), (self.state / "telemetry.csv").read_bytes())
        self.assertIn("Checks passed", md.read_text())
        (self.state / "report.md").unlink()
        self.run_cli("export")
        self.assertIn("Checks passed", (self.state / "report.md").read_text())
        self.run_cli("export", "--md", self.state / "telemetry.jsonl", success=False)
        self.run_cli("summary", "--since", "11-10-2026", success=False)
        self.run_cli("feedback", "historic", "--rating", 0, success=False)
        self.run_cli("feedback", "historic", "--round", 3, success=False)


class McpTelemetryTests(test_server.ServerHarness):
    def ledger(self):
        return {item["task_id"]: item for item in telemetry.load_records(self.state)}

    def test_dispatch_feedback_resume_and_monitor_sync(self):
        first = self.call("codex_dispatch", task_id="tracked", workdir=str(self.root),
                          spec="# Objective\n\nTrack Codex work.", model="sol", reasoning_effort="high")
        self.assertEqual(first["total_usage"]["total_tokens"], 16)
        self.assertEqual((first["task_summary"], first["rework_count"]), ("Track Codex work.", 0))
        for name in ("telemetry.jsonl", "telemetry.csv", "report.md"):
            self.assertTrue((self.state / name).is_file())
        tools = self.receive(self.send("tools/list"))["result"]["tools"]
        tool = next(item for item in tools if item["name"] == "codex_feedback")
        self.assertEqual(tool["_meta"]["ui"]["visibility"], ["model", "app"])
        prompt = "  Can you also run the full suite?\n"
        result = self.receive(self.send_tool(
            "codex_feedback", task_id="tracked", rating=3, verdict="rework", tags=["reviewed"],
            notes="Needs another check", user_first_prompt=prompt,
        ))["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["feedback"]["user_first_prompts"][0]["prompt"], prompt)
        second = self.call("codex_resume", task_id="tracked", feedback="Run the full suite.",
                           reasoning_effort="xhigh")
        self.assertEqual(second["total_usage"]["total_tokens"], 32)
        self.assertEqual(second["rework_count"], 1)
        self.assertAlmostEqual(second["total_duration_seconds"], first["duration_seconds"] + second["duration_seconds"])
        self.call("codex_feedback", task_id="tracked", round=2, user_first_prompt="Looks good.", rating=5,
                  verdict="accepted")
        record = self.ledger()["tracked"]
        self.assertEqual(record["source"], "live")
        self.assertEqual([item["usage"]["total_tokens"] for item in record["rounds"]], [16, 16])
        self.assertEqual([item["reasoning_effort"] for item in record["rounds"]], ["high", "xhigh"])
        self.assertEqual([item["user_first_prompt"] for item in record["rounds"]], [prompt, "Looks good."])
        self.assertTrue(record["rounds"][0]["user_first_prompt_at"])
        self.assertEqual(record["feedback"]["tags"], ["reviewed"])
        monitor, _ = self.monitor(task_id="tracked")
        self.assertEqual(monitor["analytics"]["overall"]["avg_rating"], 5)
        self.assertEqual(monitor["analytics"]["overall"]["rework_rate"], 1)
        self.assertEqual(monitor["selected_task"]["total_usage"]["total_tokens"], 32)
        self.assertEqual(monitor["selected_task"]["feedback"], record["feedback"])
        self.assertIn("Looks good.", (self.state / "report.md").read_text())
        self.assertIn("Looks good.", (self.state / "telemetry.csv").read_text())

    def test_feedback_validation_leaves_task_and_ledger_intact(self):
        folder, _ = write_history(self.state)
        before = (folder / "meta.json").read_bytes()
        for invalid in ({"rating": True}, {"rating": 1.5}, {"rating": 6}, {"verdict": "unknown"},
                        {"tags": [1]}, {"notes": []}, {"round": 0}, {"round": 3}, {"user_first_prompt": 5}):
            with self.subTest(invalid=invalid):
                self.call("codex_feedback", task_id="historic", error=True, **invalid)
        self.call("codex_feedback", task_id="../historic", error=True, rating=1)
        self.call("codex_feedback", task_id="missing", error=True, rating=1)
        self.assertEqual(before, (folder / "meta.json").read_bytes())
        self.assertFalse((self.state / "telemetry.jsonl").exists())

    def test_backfilled_history_survives_resume_and_cli_feedback_during_run(self):
        folder, _ = write_history(self.state)
        request = self.send_tool("codex_resume", task_id="historic", feedback="observe-rollout-only",
                                 model="updated", reasoning_effort="low")
        self.await_meta("historic", lambda item: item["round"] == 3 and item["last_event"] == "turn.started")
        result = subprocess.run([sys.executable, str(CLI), "feedback", "historic", "--round", "2",
                                 "--user-prompt", "Please recheck.", "--rating", "2"],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.call("codex_feedback", task_id="historic", round=3, user_first_prompt="Too early", error=True)
        (self.sync / "finish").touch()
        completion = self.tool_result(request)
        self.assertEqual(completion["total_usage"]["total_tokens"], 190)
        self.assertEqual(completion["rework_count"], 2)
        record = self.ledger()["historic"]
        self.assertEqual([item["model"] for item in record["rounds"]], ["sol", "sol", "updated"])
        self.assertEqual(record["feedback"]["rating"], 2)
        self.assertEqual(record["rounds"][1]["user_first_prompt"], "Please recheck.")
        self.assertEqual(test_server.read_json(folder / "meta.json")["rounds"], record["rounds"])

    def test_rollout_only_resume_counts_delta_but_returns_raw_usage(self):
        self.append_rollout({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
        }}})
        self.call("codex_dispatch", task_id="delta", workdir=str(self.root), spec="implement")
        request = self.send_tool("codex_resume", task_id="delta", feedback="observe-rollout-only")
        self.await_meta("delta", lambda item: item["round"] == 2 and item["last_event"] == "turn.started")
        self.append_rollout({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 42, "output_tokens": 8, "total_tokens": 50},
        }}})
        snapshot, _ = self.monitor(task_id="delta", include_detail=True)
        runtime = snapshot["task_detail"]["runtime"]
        self.assertTrue(runtime["alive"])
        self.assertEqual([r["usage"]["total_tokens"] for r in runtime["rounds"]], [16, 34])
        self.assertEqual(runtime["usage"]["total_tokens"], 50)
        (self.sync / "finish").touch()
        result = self.tool_result(request)
        self.assertEqual(result["usage"]["total_tokens"], 50)
        self.assertEqual(result["total_usage"]["total_tokens"], 50)
        self.assertEqual(self.ledger()["delta"]["rounds"][1]["usage"]["total_tokens"], 34)

    def test_cancelled_and_failed_launch_rounds_are_recorded(self):
        request = self.send_tool("codex_dispatch", task_id="cancel", workdir=str(self.root), spec="slow")
        self.await_meta("cancel", lambda item: item["pid"] is not None)
        self.assertTrue(self.call("codex_cancel", task_id="cancel")["cancelled"])
        self.assertEqual(self.tool_result(request)["status"], "cancelled")
        Path(self.env["CODEX_BIN"]).write_text("invalid executable", encoding="utf-8")
        failing = self.call("codex_dispatch", error=True, task_id="fail", workdir=str(self.root), spec="failing")
        self.assertEqual(failing["status"], "failed")
        records = self.ledger()
        for key, status in (("cancel", "cancelled"), ("fail", "failed")):
            self.assertEqual(records[key]["rounds"][0]["status"], status)
            self.assertIsNone(records[key]["total_usage"])
            self.assertFalse(records[key]["duration_estimated"])

    def test_repeated_session_only_resume_keeps_first_round(self):
        first = self.call("codex_resume", session_id=test_server.SESSION, workdir=str(self.root), feedback="External work")
        second = self.call("codex_resume", session_id=test_server.SESSION, workdir=str(self.root), feedback="One more fix")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(second["round"], 2)
        self.assertEqual(second["task_summary"], "External work")
        record = self.ledger()[first["task_id"]]
        self.assertEqual([item["kind"] for item in record["rounds"]], ["resume", "resume"])
        self.assertEqual(record["total_usage"]["total_tokens"], 32)


if __name__ == "__main__":
    unittest.main()
