"""Best-effort LexTranslator GUI automation through Windows UI Automation.

This adapter may only open and save project-local files. A blocked state is a
valid outcome: launching or inspecting the GUI must never be reported as a
completed translate/save operation.
"""

import argparse
import datetime as _dt
import hashlib
import os
import re
import sys
import time
from pathlib import Path


BINARY_EXTENSIONS = {".esp", ".esm", ".esl", ".pex", ".bsa", ".ba2", ".dll", ".exe"}


class BlockedError(RuntimeError):
    """Raised when the GUI state cannot be confirmed safely enough to proceed."""

    pass


def now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def resolve_project_path(project_root: Path, value: str, *, allow_missing: bool = False) -> Path:
    # GUI automation receives paths from command-line arguments and Windows file
    # dialogs. Validate both before interacting with the desktop tool.
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"Path is outside project root: {value}") from exc
    if not allow_missing and not resolved.exists():
        raise FileNotFoundError(f"Path does not exist: {resolved}")
    return resolved


def is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def append_report(report_path: Path, lines: list[str]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if not report_path.exists():
        report_path.write_text("# LexTranslator GUI Report\n", encoding="utf-8")
    with report_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n")
        handle.write("\n".join(lines))
        handle.write("\n")


def append_tool_log(log_path: Path, *, mode: str, input_path: Path, status: str, next_action: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text("# Tool Invocation Log\n", encoding="utf-8")
    lines = [
        "",
        f"## {now_text()}",
        "",
        "- Tool: LexTranslator",
        f"- Input: {input_path}",
        f"- Mode: {mode}",
        f"- Status: {status}",
        f"- Next action: {next_action}",
    ]
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def file_fingerprint(path: Path) -> dict[str, object]:
    if not path.exists() or not path.is_file():
        return {"exists": False}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "exists": True,
        "sha256": digest.hexdigest().upper(),
        "size": stat.st_size,
        "mtime": _dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def import_pywinauto():
    try:
        from pywinauto import Application, Desktop, keyboard, mouse, timings
    except ModuleNotFoundError as exc:
        raise BlockedError(
            "Python dependency 'pywinauto' is not installed. Install it in an approved "
            "Python environment, then rerun the LexTranslator GUI wrapper."
        ) from exc
    return Application, Desktop, keyboard, mouse, timings


def select_main_window(app, preferred_title: str | None = None, windows: list[object] | None = None):
    if windows is None:
        windows = app.windows()
    if preferred_title:
        for window in windows:
            if window.window_text() == preferred_title:
                window.set_focus()
                return window
    for window in windows:
        title = window.window_text()
        if title and title != "Lex":
            window.set_focus()
            return window
    for window in windows:
        title = window.window_text()
        if title:
            window.set_focus()
            return window
    return None


def connect_or_start(tool_path: Path, timeout_seconds: int):
    Application, Desktop, _keyboard, _mouse, _timings = import_pywinauto()
    app = Application(backend="uia")
    try:
        app.connect(path=str(tool_path), timeout=3)
    except Exception:
        app.start(str(tool_path))

    deadline = time.time() + timeout_seconds
    last_titles: list[str] = []
    while time.time() < deadline:
        windows = app.windows()
        if not windows:
            try:
                process_id = app.process
                desktop_windows = Desktop(backend="uia").windows()
                windows = [window for window in desktop_windows if window.element_info.process_id == process_id]
            except Exception:
                windows = []
        last_titles = [window.window_text() for window in windows]
        window = select_main_window(app, windows=windows)
        if window is not None:
            return app, window
        time.sleep(0.5)

    raise BlockedError(
        "LexTranslator launched or connected, but the main window did not become ready. "
        f"Observed windows: {last_titles}"
    )


def collect_control_summary(main_window) -> list[str]:
    summary: list[str] = []
    interesting = {
        "LoadFileButton",
        "TransProcess",
        "TranslateOTButtonFont",
        "UIApply",
        "SearchBox",
        "FromStr",
        "ToStr",
    }
    try:
        descendants = main_window.descendants()
    except Exception as exc:
        return [f"Failed to read controls: {exc}"]

    for control in descendants[:300]:
        info = control.element_info
        name = (info.name or "").strip()
        auto_id = (info.automation_id or "").strip()
        control_type = (info.control_type or "").strip()
        if auto_id in interesting or name in {"Load File", "Translate(F1)", "Apply(F2)", "Original", "Translated"}:
            summary.append(f"{control_type} name={name!r} auto_id={auto_id!r}")
    return summary[:80]


def matches_control(control, *, auto_id: str | None = None, title: str | None = None, title_re: str | None = None, control_type: str | None = None) -> bool:
    info = control.element_info
    name = (info.name or "").strip()
    current_auto_id = (info.automation_id or "").strip()
    current_control_type = (info.control_type or "").strip()
    if auto_id is not None and current_auto_id != auto_id:
        return False
    if title is not None and name != title:
        return False
    if title_re is not None and re.fullmatch(title_re, name) is None:
        return False
    if control_type is not None and current_control_type != control_type:
        return False
    return True


def find_descendant(root, **criteria):
    last_seen: list[str] = []
    for control in root.descendants():
        info = control.element_info
        last_seen.append(
            f"{info.control_type or ''} name={(info.name or '').strip()!r} auto_id={(info.automation_id or '').strip()!r}"
        )
        if matches_control(control, **criteria):
            return control
    raise BlockedError(f"Control not found for {criteria}. Seen controls: {last_seen[:80]}")


def get_status_text(main_window) -> str:
    for auto_id in ("TransProcess", "CurrentLog"):
        try:
            control = find_descendant(main_window, auto_id=auto_id)
            text = control.window_text()
            if text:
                return text
        except Exception:
            continue
    return ""


def invoke_load_file(main_window) -> None:
    candidates = [
        {"auto_id": "LoadFileButton"},
        {"title": "Load File"},
        {"title_re": ".*Load File.*"},
    ]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            control = find_descendant(main_window, **candidate)
            try:
                control.click_input()
            except Exception:
                control.invoke()
            return
        except Exception as exc:
            last_error = exc
    raise BlockedError(f"Could not invoke LexTranslator Load File control: {last_error}")


def invoke_save_file(main_window) -> None:
    candidates = [
        {"auto_id": "LoadFileButton"},
        {"title": "Save File"},
        {"title_re": ".*Save File.*"},
    ]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            control = find_descendant(main_window, **candidate)
            control_name = (control.element_info.name or "").strip()
            if control_name and "Save" not in control_name:
                last_error = BlockedError(f"LoadFileButton is not in save state; current name is {control_name!r}")
                continue
            try:
                control.click_input()
            except Exception:
                control.invoke()
            return
        except Exception as exc:
            last_error = exc
    raise BlockedError(f"Could not invoke LexTranslator Save File control: {last_error}")


def find_file_dialog(timeout_seconds: int):
    _Application, Desktop, _keyboard, _mouse, _timings = import_pywinauto()
    desktop = Desktop(backend="win32")
    deadline = time.time() + timeout_seconds
    last_titles: list[str] = []
    while time.time() < deadline:
        for title in ("Please select a file", "Open", "打开", "Select a file", "选择文件"):
            dialog = desktop.window(title=title)
            try:
                if dialog.exists(timeout=0.2):
                    dialog.set_focus()
                    return dialog
            except Exception:
                continue
        try:
            dialogs = desktop.windows()
            last_titles = [dialog.window_text() for dialog in dialogs]
            for dialog in dialogs:
                title = dialog.window_text()
                if any(marker in title for marker in ("Open", "打开", "select", "Select", "选择")):
                    dialog.set_focus()
                    return dialog
        except Exception as exc:
            last_titles = [f"window enumeration failed: {exc}"]
        time.sleep(0.25)
    raise BlockedError(f"File dialog did not appear. Observed windows: {last_titles[:20]}")


def set_file_dialog_path(dialog, input_path: Path) -> None:
    path_text = str(input_path)
    edit_errors: list[str] = []
    _Application, _Desktop, keyboard, _mouse, _timings = import_pywinauto()
    try:
        edit = dialog.child_window(class_name="Edit")
        edit.set_focus()
        edit.set_edit_text(path_text)
    except Exception as exc:
        edit_errors.append(str(exc))
        try:
            keyboard.send_keys("^a")
            keyboard.send_keys(path_text, with_spaces=True)
        except Exception as fallback_exc:
            edit_errors.append(str(fallback_exc))
            raise BlockedError(f"Could not set file dialog path. Errors: {edit_errors}") from fallback_exc

    button_errors: list[str] = []
    try:
        button = dialog.child_window(title_re=r".*Open.*|.*打开.*|.*\(&O\).*", class_name="Button")
        button.click_input()
    except Exception as exc:
        button_errors.append(str(exc))
        try:
            keyboard.send_keys("{ENTER}")
        except Exception as fallback_exc:
            button_errors.append(str(fallback_exc))
            raise BlockedError(f"Could not confirm file dialog. Errors: {button_errors}") from fallback_exc


def open_input_file(app, main_window, input_path: Path, timeout_seconds: int) -> tuple[object, str]:
    invoke_load_file(main_window)
    dialog = find_file_dialog(timeout_seconds)
    set_file_dialog_path(dialog, input_path)
    deadline = time.time() + timeout_seconds
    last_status = ""
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            refreshed = select_main_window(app, input_path.name)
            if refreshed is not None:
                main_window = refreshed
            main_window.set_focus()
            last_status = get_status_text(main_window)
            if last_status and "STRINGS(0/0)" not in last_status:
                return main_window, last_status
        except Exception:
            continue
    return main_window, last_status


def parse_status_counts(status_text: str) -> tuple[int, int] | None:
    match = re.search(r"STRINGS\((\d+)/(\d+)\)", status_text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def invoke_run_translation(main_window) -> str:
    controls = main_window.descendants()
    trans_control = None
    search_control = None
    for control in controls:
        auto_id = (control.element_info.automation_id or "").strip()
        if auto_id == "TransProcess" and trans_control is None:
            trans_control = control
        if auto_id == "SearchBox" and search_control is None:
            search_control = control
    if trans_control is None or search_control is None:
        raise BlockedError("Could not locate STRINGS status or SearchBox controls for anchored run-translation click.")

    trans_rect = trans_control.element_info.rectangle
    search_rect = search_control.element_info.rectangle
    gap = search_rect.left - trans_rect.right
    if gap < 80:
        raise BlockedError(f"Unexpected LexTranslator toolbar geometry; gap between STRINGS and SearchBox is {gap}.")

    # LexTranslator does not expose the top play icon through UIA. It is the blue
    # triangle immediately to the right of STRINGS(...); the gear/settings button is farther right.
    x = trans_rect.right + 24
    if x >= search_rect.left:
        raise BlockedError(
            f"Computed run-translation click is outside the STRINGS/play region: "
            f"x={x}, search_left={search_rect.left}, strings_right={trans_rect.right}."
        )
    y = trans_rect.top + ((trans_rect.bottom - trans_rect.top) // 2)
    _Application, _Desktop, _keyboard, mouse, _timings = import_pywinauto()
    mouse.click(button="left", coords=(x, y))
    time.sleep(1)
    try:
        _Application, Desktop, _keyboard, _mouse, _timings = import_pywinauto()
        for window in Desktop(backend="win32").windows():
            if window.window_text() == "LocalConfig":
                raise BlockedError(
                    "Run-translation anchored click opened LexTranslator settings (LocalConfig) instead of the play button."
                )
    except BlockedError:
        raise
    except Exception:
        pass
    return f"strings_play_click x={x} y={y} search_left={search_rect.left} strings_right={trans_rect.right}"


def wait_for_translation_run(main_window, before_status: str, timeout_seconds: int) -> tuple[str, list[str]]:
    before_counts = parse_status_counts(before_status)
    history: list[str] = []
    deadline = time.time() + timeout_seconds
    stable_done_seen = 0
    while time.time() < deadline:
        time.sleep(1)
        status = get_status_text(main_window)
        if status:
            history.append(status)
        counts = parse_status_counts(status)
        if counts is None:
            continue
        current, total = counts
        if total > 0 and current == total:
            stable_done_seen += 1
            if stable_done_seen >= 2:
                return status, history[-20:]
        if before_counts is not None and counts != before_counts and total > 0:
            if current > 0:
                return status, history[-20:]
    return (history[-1] if history else before_status), history[-20:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project-local LexTranslator GUI automation.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--tool-path", required=True)
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--mode", choices=("inspect", "open", "open-save", "open-translate-save", "save-current"), default="open")
    parser.add_argument("--report-path", default="qa/lextranslator_gui_report.md")
    parser.add_argument("--log-path", default="qa/tool_invocation_log.md")
    parser.add_argument("--translation-pairs-path", default="")
    parser.add_argument("--timeout", type=int, default=45)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).resolve(strict=True)
    tool_path = Path(args.tool_path).resolve(strict=True)
    input_path = resolve_project_path(project_root, args.input_path)
    report_path = resolve_project_path(project_root, args.report_path, allow_missing=True)
    log_path = resolve_project_path(project_root, args.log_path, allow_missing=True)
    translation_pairs = None
    if args.translation_pairs_path:
        translation_pairs = resolve_project_path(project_root, args.translation_pairs_path)

    mod_root = resolve_project_path(project_root, "mod")
    if input_path.suffix.lower() in BINARY_EXTENSIONS and is_under(input_path, mod_root):
        raise BlockedError(
            "Refusing to open a binary file directly from mod/. Copy it to "
            "out/tool_outputs/<ModName>/ first, then rerun automation."
        )

    base_report = [
        f"## Attempt {now_text()}",
        "",
        f"- Mode: `{args.mode}`",
        f"- Tool: `{tool_path}`",
        f"- Input: `{input_path}`",
        f"- Translation pairs: `{translation_pairs}`" if translation_pairs else "- Translation pairs: not supplied",
        "- Backend: pywinauto UIA fallback",
    ]

    try:
        app, main_window = connect_or_start(tool_path, args.timeout)
        control_summary = collect_control_summary(main_window)
        status_text = get_status_text(main_window)

        if args.mode == "inspect":
            append_report(
                report_path,
                base_report
                + [
                    "",
                    "### Result",
                    "",
                    "- LexTranslator launched or connected.",
                    f"- Window title: `{main_window.window_text()}`",
                    f"- Status text: `{status_text}`",
                    "",
                    "### Controls",
                    "",
                    *(f"- `{item}`" for item in control_summary),
                    "",
                    "### Safety",
                    "",
                    "- No file was loaded.",
                    "- No plugin was saved.",
                    "- No real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games path was accessed.",
                ],
            )
            append_tool_log(log_path, mode=args.mode, input_path=input_path, status="inspected", next_action="Use mode open after reviewing controls.")
            print(f"LexTranslator inspect completed. Report: {report_path}")
            return 0

        # Fingerprints are used to report whether save changed the project-local
        # file. They do not prove translation quality; QA still has to re-read
        # the output through the normal gates.
        before_fingerprint = file_fingerprint(input_path)
        # Keep operation milestones separate so reports can say exactly whether
        # the file was opened, translation was triggered, and save completed.
        loaded_status = status_text
        opened_file = False
        if args.mode in {"open", "open-save", "open-translate-save"}:
            main_window, loaded_status = open_input_file(app, main_window, input_path, args.timeout)
            opened_file = True
        elif args.mode == "save-current":
            current_title = main_window.window_text()
            if current_title != input_path.name:
                raise BlockedError(
                    f"save-current requires the loaded LexTranslator window title to equal {input_path.name!r}; "
                    f"current title is {current_title!r}"
                )

        after_open_fingerprint = file_fingerprint(input_path)
        open_changed = before_fingerprint != after_open_fingerprint
        translate_requested = args.mode == "open-translate-save"
        translate_invocation = ""
        translate_status = loaded_status
        translate_history: list[str] = []
        if translate_requested:
            translate_invocation = invoke_run_translation(main_window)
            translate_status, translate_history = wait_for_translation_run(main_window, loaded_status, args.timeout)

        save_requested = args.mode in {"open-save", "open-translate-save", "save-current"}
        after_save_fingerprint = after_open_fingerprint
        save_changed = False
        if save_requested:
            invoke_save_file(main_window)
            time.sleep(5)
            after_save_fingerprint = file_fingerprint(input_path)
            save_changed = after_open_fingerprint != after_save_fingerprint

        if args.mode == "open":
            status_for_log = "opened"
            next_action = "Use open-save or save-current only after confirming the loaded project-local file and QA gate."
        elif args.mode == "open-save":
            status_for_log = "saved_changed" if save_changed else "saved_unchanged"
            next_action = "Run verify-plugin-output and rebuild final_mod if the saved output is expected."
        elif args.mode == "open-translate-save":
            status_for_log = "translated_saved_changed" if save_changed else "translated_saved_unchanged"
            next_action = "Verify translated strings, run QA, and rebuild final_mod if the saved output is expected."
        else:
            status_for_log = "saved_changed" if save_changed else "saved_unchanged"
            next_action = "Run verify-plugin-output and rebuild final_mod if the saved output is expected."

        append_report(
            report_path,
            base_report
            + [
                "",
                "### Result",
                "",
                "- LexTranslator launched or connected.",
                "- The Load File control was invoked through UI Automation." if opened_file else "- Existing loaded file was used.",
                "- The Windows file dialog path was set through UI Automation." if opened_file else "- No file dialog was used.",
                f"- Post-open status text: `{loaded_status}`",
                f"- Save requested: `{save_requested}`",
                f"- Translate requested: `{translate_requested}`",
                f"- Translate invocation: `{translate_invocation}`",
                f"- Post-translate status text: `{translate_status}`",
                f"- Translate status history: `{translate_history}`",
                f"- Input file changed during open: `{open_changed}`",
                f"- Input file changed during save: `{save_changed}`",
                f"- Input before operation: `{before_fingerprint}`",
                f"- Input after open/current check: `{after_open_fingerprint}`",
                f"- Input after save: `{after_save_fingerprint}`",
                "",
                "### Controls",
                "",
                *(f"- `{item}`" for item in control_summary),
                "",
                "### Safety",
                "",
                "- Input path was project-local.",
                "- Save, if requested, targeted the already loaded project-local input path.",
                "- No real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games path was accessed.",
            ],
        )
        append_tool_log(log_path, mode=args.mode, input_path=input_path, status=status_for_log, next_action=next_action)
        print(f"LexTranslator {args.mode} completed. Report: {report_path}")
        return 0

    except BlockedError as exc:
        append_report(
            report_path,
            base_report
            + [
                "",
                "### Blocked",
                "",
                f"- {exc}",
                "",
                "### Safety",
                "",
                "- Automation stopped before any save/export action.",
                "- No plugin was modified by this script.",
            ],
        )
        append_tool_log(log_path, mode=args.mode, input_path=input_path, status="blocked", next_action=str(exc))
        print(f"LexTranslator automation blocked: {exc}")
        return 2
    except Exception as exc:
        append_report(
            report_path,
            base_report
            + [
                "",
                "### Failed",
                "",
                f"- {type(exc).__name__}: {exc}",
                "",
                "### Safety",
                "",
                "- Automation stopped before any save/export action.",
            ],
        )
        append_tool_log(log_path, mode=args.mode, input_path=input_path, status="failed", next_action=f"{type(exc).__name__}: {exc}")
        print(f"LexTranslator automation failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BlockedError as top_level_exc:
        print(f"LexTranslator automation blocked: {top_level_exc}")
        raise SystemExit(2)
