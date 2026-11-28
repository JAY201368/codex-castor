#!/usr/bin/env python3
"""Inspect and maintain Codex Castor task telemetry."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_DIR = HERE if (HERE / "telemetry.py").is_file() else HERE.parent / "mcp"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import telemetry
from castor_platform import configure_stdio


def bounded_int(minimum, maximum=None):
    def convert(value):
        number = int(value)
        if number < minimum or (maximum is not None and number > maximum):
            message = f"must be at least {minimum}" if maximum is None else f"must be between {minimum} and {maximum}"
            raise argparse.ArgumentTypeError(message)
        return number
    return convert


def iso_date(value):
    try:
        if dt.date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use YYYY-MM-DD")


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_table(title, columns, rows, limits=None):
    print(title)
    if not rows:
        print("  No matching tasks.\n")
        return

    def cell(value, index):
        value = " ".join(str(value).split())
        limit = limits[index] if limits else len(value)
        return value if len(value) <= limit else value[:limit - 1] + "…"

    rows = [columns, *(tuple(cell(value, index) for index, value in enumerate(row)) for row in rows)]
    widths = [max(map(len, column)) for column in zip(*rows)]
    for index, row in enumerate(rows):
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))
    print()


def summary(args, state_root):
    records = telemetry.filter_records(
        telemetry.load_or_build_records(state_root),
        model=args.model, effort=args.effort, project=args.project, since=args.since,
    )
    result = telemetry.compute_analytics(records)
    recent = records[:args.limit]
    if args.json:
        print_json({**result, "recent_tasks": recent})
        return
    print_table("Overall KPIs", ("Metric", "Value"), telemetry.kpi_rows(result["overall"]))
    print_table(
        "Model × Reasoning Effort Performance Matrix", telemetry.MATRIX_COLUMNS,
        telemetry.matrix_rows(result["by_model_effort"]),
    )
    print_table(
        "Recent Tasks", telemetry.RECENT_COLUMNS, telemetry.recent_rows(recent),
        (24, 18, 40, 27, 16, 12, 12, 18, 35),
    )


def backfill(args, state_root):
    records = telemetry.backfill(state_root)
    result = {
        "task_count": len(records),
        "round_count": sum(item["round_count"] for item in records),
        "state_dir": str(state_root),
    }
    if args.json:
        print_json(result)
    else:
        print(f"Backfill complete. {result['task_count']} tasks and {result['round_count']} rounds in {state_root}.")


def feedback(args, state_root):
    updates = {
        key: getattr(args, key) for key in (*telemetry.FEEDBACK_FIELDS, "round", "user_first_prompt")
        if getattr(args, key) is not None
    }
    if "tags" in updates:
        updates["tags"] = [tag.strip() for tag in updates["tags"].split(",") if tag.strip()]
    print_json(telemetry.record_feedback(state_root, args.task_id, updates))


def export(args, state_root):
    md_path, csv_path = args.md, args.csv
    if md_path is None and csv_path is None:
        md_path, csv_path = state_root / "report.md", state_root / "telemetry.csv"
    destinations = ((telemetry.export_markdown, md_path), (telemetry.export_csv, csv_path))
    paths = [path.expanduser().resolve() for _, path in destinations if path is not None]
    reserved = {(state_root / name).resolve() for name in ("telemetry.jsonl", ".telemetry.lock")}
    if reserved.intersection(paths) or len(paths) != len(set(paths)):
        raise ValueError("Reports need distinct output paths separate from the ledger and its lock")
    with telemetry.transaction(state_root):
        records = telemetry.load_or_build_records(state_root)
        for exporter, path in destinations:
            if path is not None:
                path = path.expanduser()
                exporter(records, path)
                print(f"Wrote {path}")


def main():
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", type=Path, help="Override the Codex Castor state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("summary", parents=[common], help="Show task and model effectiveness")
    command.add_argument("--model")
    command.add_argument("--effort")
    command.add_argument("--project")
    command.add_argument("--since", type=iso_date, metavar="YYYY-MM-DD")
    command.add_argument("--limit", type=bounded_int(0), default=15)
    command.add_argument("--json", action="store_true")
    command.set_defaults(run=summary)

    command = sub.add_parser("backfill", parents=[common], help="Reconstruct the ledger from task artifacts")
    command.add_argument("--json", action="store_true")
    command.set_defaults(run=backfill)

    command = sub.add_parser("feedback", parents=[common], help="Record ratings and first replies")
    command.add_argument("task_id")
    command.add_argument("--rating", type=bounded_int(1, 5))
    command.add_argument("--verdict", choices=telemetry.VERDICTS)
    command.add_argument("--tags", help="Comma-separated tags")
    command.add_argument("--notes")
    command.add_argument("--user-prompt", dest="user_first_prompt")
    command.add_argument("--round", type=bounded_int(1))
    command.set_defaults(run=feedback)

    command = sub.add_parser("export", parents=[common], help="Regenerate Markdown and CSV reports")
    command.add_argument("--md", type=Path, metavar="PATH")
    command.add_argument("--csv", type=Path, metavar="PATH")
    command.set_defaults(run=export)

    args = parser.parse_args()
    state_root = (args.state_dir or telemetry.default_state_root()).expanduser()
    try:
        args.run(args, state_root)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"analytics: {exc}\n")


if __name__ == "__main__":
    main()
