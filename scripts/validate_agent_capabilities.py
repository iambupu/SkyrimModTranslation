"""Validate agent capability manifests."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_capabilities import EXAMPLE_CAPABILITIES_PATH, config_validation_errors, load_agent_capabilities


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent capability configuration.")
    parser.add_argument("--config-path", default=str(EXAMPLE_CAPABILITIES_PATH))
    parser.add_argument(
        "--example",
        action="store_true",
        help="Validate config/agent_capabilities.example.json.",
    )
    args = parser.parse_args()

    path = EXAMPLE_CAPABILITIES_PATH if args.example else Path(args.config_path)
    config = load_agent_capabilities(path)
    errors = config_validation_errors(config)
    if errors:
        print("Agent capability validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Agent capability config is valid: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
