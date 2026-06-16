"""xTranslator wrapper placeholder for future safe project-local automation."""

import argparse

from invoke_lextranslator import launch_tool


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch xTranslator with project-local input logging.")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--optional-mode", default="manual-open")
    args = parser.parse_args()
    return launch_tool("xTranslator", "XTranslatorPath", args.input_path, args.optional_mode)


if __name__ == "__main__":
    raise SystemExit(main())
