"""The sole public command-line controller for SMT workflows."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence

import smt_cli


OPEN_TARGETS = ("root", "final-mod", "intermediate", "package-directory")


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smt.py",
        description="Translate one Bethesda Mod through the controlled SMT workflow.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="public result format (default: text)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="create or reuse one Mod workspace")
    run.add_argument("input", help="Mod directory, ZIP, or 7Z path")
    run.add_argument("--game", required=True, help="explicit Game Profile id")
    run.add_argument("--workspace", help="explicit workspace path")
    run.add_argument("--workspace-root", help="root for newly allocated workspaces")
    run.add_argument(
        "--tool-setup",
        choices=("auto", "manual", "skip"),
        default="auto",
        help="tool preparation policy (default: auto)",
    )
    run.add_argument(
        "--timeout-seconds",
        type=_positive_seconds,
        default=1800.0,
        help="whole-command timeout (default: 1800)",
    )

    status = commands.add_parser("status", help="read the latest progress snapshot")
    status.add_argument("--workspace", help="explicit workspace path")

    resume = commands.add_parser("resume", help="continue one existing workflow")
    resume.add_argument("--workspace", help="explicit workspace path")
    resume.add_argument(
        "--timeout-seconds",
        type=_positive_seconds,
        default=1800.0,
        help="whole-command timeout (default: 1800)",
    )

    doctor = commands.add_parser("doctor", help="run read-only diagnostics")
    doctor.add_argument("--workspace", help="explicit workspace path")

    output = commands.add_parser("output", help="show current output paths")
    output.add_argument("--workspace", help="explicit workspace path")
    output.add_argument(
        "--open",
        dest="open_target",
        choices=OPEN_TARGETS,
        help="open one predefined output directory",
    )
    return parser


def _write_text_result(result: smt_cli.CliResult) -> None:
    print(f"Outcome: {result.outcome or '-'}")
    print(f"Message: {result.message or '-'}")

    if result.progress_card is not None:
        if result.progress_card:
            sys.stdout.write(result.progress_card)
            if not result.progress_card.endswith("\n"):
                sys.stdout.write("\n")

    if result.next_action is not None:
        print(
            f"Next action ({result.next_action['kind']}): {result.next_action['summary']}"
        )
        for artifact in result.next_action["artifacts"]:
            print(f"- {artifact}")

    if result.output_paths:
        print("Outputs:")
        for name, artifact in result.output_paths.items():
            exists = "yes" if artifact["exists"] else "no"
            print(f"- {name}: {artifact['path']} (exists: {exists})")

    if result.details:
        print("Details:")
        for detail in result.details:
            print(f"- {detail}")

    if result.diagnostics:
        print("Diagnostics:")
        for diagnostic in result.diagnostics:
            print(f"- {diagnostic}")


def render_result(result: smt_cli.CliResult, output_format: str) -> None:
    if output_format == "json":
        sys.stdout.write(
            json.dumps(result.to_payload(), ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        return
    _write_text_result(result)


def main(
    argv: Sequence[str] | None = None,
) -> int:
    namespace = build_parser().parse_args(argv)
    result = smt_cli.dispatch(namespace)
    render_result(result, namespace.format)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
