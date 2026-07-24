"""Controlled CLI for shared managed-tool cache maintenance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from managed_tool_maintenance import apply_plan, create_plan, inspect_store
from managed_tool_store import (
    ManagedStoreRoots,
    ManagedToolStoreError,
    resolve_managed_store_roots,
)
from smt_windows import ManagedProcessEnvironmentError


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def _assert_bootstrap_runtime(
    roots: ManagedStoreRoots,
    executable: Path | None = None,
) -> None:
    current_executable = executable or Path(sys.executable)
    if _path_is_within(current_executable, roots.payload):
        raise ManagedToolStoreError(
            "Managed-tool cache maintenance must run from the independent "
            "bootstrap Python, not from a shared managed Python entry."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly maintain the SMT managed-tool cache."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("inspect")
    clean = subparsers.add_parser("plan-clean-unused")
    clean.add_argument(
        "--release-stale-reference",
        action="append",
        default=[],
        help="Exact stale catalog reference ID explicitly approved for release.",
    )
    subparsers.add_parser("plan-uninstall")
    apply = subparsers.add_parser("apply-plan")
    apply.add_argument("--plan-id", required=True)
    apply.add_argument("--confirmation-token", required=True)
    apply.add_argument("--lock-timeout-seconds", type=float, default=0.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        _assert_bootstrap_runtime(resolve_managed_store_roots())
        if args.operation == "inspect":
            payload = inspect_store().to_payload()
        elif args.operation == "plan-clean-unused":
            plan = create_plan(
                "clean-unused",
                release_stale_reference_ids=args.release_stale_reference,
            )
            payload = {
                "schema_version": 1,
                "operation": args.operation,
                "status": "planned",
                "reference_coverage": "known-registered-only",
                "plan": plan.to_payload(),
                "requires_confirmation": True,
            }
        elif args.operation == "plan-uninstall":
            plan = create_plan("uninstall")
            payload = {
                "schema_version": 1,
                "operation": args.operation,
                "status": "planned",
                "reference_coverage": "known-registered-only",
                "coverage_warning": (
                    "Copied, moved, offline, or never-reregistered workspaces "
                    "may not be represented in the catalog."
                ),
                "plan": plan.to_payload(),
                "requires_confirmation": True,
            }
        else:
            result = apply_plan(
                args.plan_id,
                args.confirmation_token,
                lock_timeout_seconds=args.lock_timeout_seconds,
            )
            post = inspect_store()
            payload = {
                "schema_version": 1,
                "operation": args.operation,
                "status": result.outcome,
                "result": result.to_payload(),
                "post_inspection": post.to_payload(),
            }
    except (
        OSError,
        ValueError,
        ManagedProcessEnvironmentError,
        ManagedToolStoreError,
    ) as exc:
        payload = {
            "schema_version": 1,
            "operation": getattr(args, "operation", None),
            "status": "blocked",
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if payload["status"] in {"blocked", "partial", "interrupted"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
