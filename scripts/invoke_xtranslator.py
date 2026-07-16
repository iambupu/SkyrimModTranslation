"""xTranslator wrapper placeholder for future safe project-local automation."""

from invoke_lextranslator import launcher_main


def main() -> int:
    return launcher_main("xTranslator", "XTranslatorPath")


if __name__ == "__main__":
    raise SystemExit(main())
