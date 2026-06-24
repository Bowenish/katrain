"""Helper sub-tasks that run in a CHILD process of KaTrain.

A few features need their own tkinter event loop - a native file/text dialog (real IME &
OS file browser) or the screenshot frame-select overlay - which would clash with Kivy if run
in-process. They are launched as a child process instead.

From source that child is ``python -m katrain --katrain-subtask <task> ...``; in a PyInstaller
build ``sys.executable`` is the app exe, so ``exe -m module`` cannot work - the frozen exe
re-runs *itself* with the same ``--katrain-subtask`` sentinel, which ``katrain/__main__.py``
routes here BEFORE importing Kivy (so the child stays light and never opens a second GUI).

``helper_cmd()`` builds the right command for either case; ``run_subtask()`` dispatches it.

Heavy deps (numpy / Pillow, needed only by the screenshot recognizer) are imported lazily, so
the stdlib-only dialogs still work in a slim frozen build that excludes numpy - and a build
without the recognizer degrades with a clear message instead of crashing.
"""

from __future__ import annotations

import os
import sys


def helper_cmd(task: str, *args) -> list:
    """argv to run sub-task ``task`` in a child process, source- or frozen-safe."""
    head = [sys.executable, "--katrain-subtask"] if getattr(sys, "frozen", False) \
        else [sys.executable, "-m", "katrain", "--katrain-subtask"]
    return head + [task] + [str(a) for a in args]


def helper_cwd() -> str:
    """Working dir for a helper child process: the dir that CONTAINS the 'katrain' package, so
    ``python -m katrain`` resolves it even from a non-installed source checkout launched elsewhere.
    Pass it as subprocess.run(..., cwd=helper_cwd()). Harmless when frozen (the child re-runs the
    exe and ignores cwd for import resolution). This file is <parent>/katrain/core/subtasks.py."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _set_dpi_aware() -> None:
    if os.name != "nt":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _pick_sgf(out_path: str) -> int:
    """Native 'open file' dialog; write the chosen path (or '') to out_path."""
    import tkinter as tk
    from tkinter import filedialog

    _set_dpi_aware()
    r = tk.Tk()
    r.withdraw()
    r.attributes("-topmost", True)
    f = filedialog.askopenfilename(
        title="选择 SGF 棋谱", filetypes=[("SGF 棋谱", "*.sgf"), ("所有文件", "*.*")]
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f or "")
    return 0


def _ask_text(out_path: str, title: str = "", initial: str = "") -> int:
    """Native single-line text prompt (full IME); write the text, or '\\x00' if cancelled."""
    import tkinter as tk
    from tkinter import simpledialog

    _set_dpi_aware()
    r = tk.Tk()
    r.withdraw()
    r.attributes("-topmost", True)
    v = simpledialog.askstring(title, "请输入名称：", initialvalue=initial, parent=r)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\x00" if v is None else v)
    return 0


def run_subtask(task: str, argv: list) -> int:
    """Dispatch a ``--katrain-subtask`` invocation. Returns a process exit code."""
    if task == "screenshot":
        try:
            from katrain.core.screenshot_import import main as screenshot_main
        except Exception as exc:  # numpy / Pillow unavailable (e.g. a slim frozen build)
            print(f"screenshot recognizer unavailable (needs numpy + Pillow): {exc}",
                  file=sys.stderr, flush=True)
            return 3
        return screenshot_main(argv) or 0
    if task in ("pick_sgf", "ask_text"):
        if not argv:
            print(f"{task}: missing output path argument", file=sys.stderr, flush=True)
            return 4
        if task == "pick_sgf":
            return _pick_sgf(argv[0])
        return _ask_text(argv[0],
                         argv[1] if len(argv) > 1 else "",
                         argv[2] if len(argv) > 2 else "")
    print(f"unknown subtask: {task!r}", file=sys.stderr, flush=True)
    return 4
