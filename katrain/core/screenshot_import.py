"""
Screenshot -> Go board -> SGF, in one shot.

Run modes
---------
  python -m katrain.core.screenshot_import --stdout
        Show a fullscreen frame-select overlay, let the user drag a box around the
        board, recognise it, and print the SGF to stdout. (Used by KaTrain's hotkey.)

  python -m katrain.core.screenshot_import
        Same, but copy the SGF to the clipboard (paste into KaTrain with Ctrl-V).

  python -m katrain.core.screenshot_import --image path/to/shot.png --debug
        Skip the overlay: recognise an existing image file. With --debug, also save
        an annotated <name>.debug.png so you can see exactly what was detected.
        (This is the calibration entry point.)

Options
-------
  --to-move B|W   whose turn it is in the SGF PL property (default B)
  --debug         save annotated detection image next to the source / temp dir
  --stdout        print SGF to stdout instead of copying to clipboard

Windows DPI: we mark the process per-monitor DPI aware so the tkinter overlay's
coordinates match PIL.ImageGrab's physical pixels (otherwise the captured region
is offset/scaled on scaled displays).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tempfile
from typing import Optional, Tuple

from PIL import Image, ImageGrab

from katrain.core import board_ocr as ocr


# --------------------------------------------------------------------------- #
# Windows helpers
# --------------------------------------------------------------------------- #
def _set_dpi_aware() -> None:
    if os.name != "nt":
        return
    try:
        # per-monitor v2 (PROCESS_PER_MONITOR_DPI_AWARE = 2)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _virtual_screen_rect() -> Tuple[int, int, int, int]:
    """(x, y, w, h) of the whole virtual desktop (all monitors). Falls back to primary."""
    if os.name == "nt":
        gsm = ctypes.windll.user32.GetSystemMetrics
        x = gsm(76)   # SM_XVIRTUALSCREEN
        y = gsm(77)   # SM_YVIRTUALSCREEN
        w = gsm(78)   # SM_CXVIRTUALSCREEN
        h = gsm(79)   # SM_CYVIRTUALSCREEN
        if w and h:
            return x, y, w, h
    return 0, 0, 0, 0


# --------------------------------------------------------------------------- #
# Frame-select overlay
# --------------------------------------------------------------------------- #
def select_region() -> Optional[Tuple[int, int, int, int]]:
    """Fullscreen translucent overlay; drag a rectangle. Returns absolute (x1,y1,x2,y2) or None."""
    import tkinter as tk

    vx, vy, vw, vh = _virtual_screen_rect()
    root = tk.Tk()
    root.attributes("-alpha", 0.25)
    root.configure(bg="black")
    root.attributes("-topmost", True)
    if vw and vh:
        root.overrideredirect(True)
        root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    else:
        root.attributes("-fullscreen", True)
    try:
        root.config(cursor="crosshair")
    except Exception:
        pass

    canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        20, 20, anchor="nw", fill="#ffffff",
        text="拖动鼠标框选棋盘  |  Esc 取消", font=("Segoe UI", 16),
    )

    state = {"x0": None, "y0": None, "rect": None, "bbox": None}

    def on_down(e):
        state["x0"], state["y0"] = e.x, e.y
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff88", width=2)

    def on_move(e):
        if state["rect"] is not None:
            canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

    def on_up(e):
        if state["x0"] is None:
            return
        x1, y1 = min(state["x0"], e.x), min(state["y0"], e.y)
        x2, y2 = max(state["x0"], e.x), max(state["y0"], e.y)
        if x2 - x1 >= 10 and y2 - y1 >= 10:
            # canvas coords -> absolute screen coords
            state["bbox"] = (x1 + vx, y1 + vy, x2 + vx, y2 + vy)
        root.destroy()

    def on_cancel(_e=None):
        state["bbox"] = None
        root.destroy()

    canvas.bind("<Button-1>", on_down)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_up)
    root.bind("<Escape>", on_cancel)
    root.focus_force()
    root.mainloop()
    return state["bbox"]


def capture(bbox: Tuple[int, int, int, int]) -> Image.Image:
    return ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")


# --------------------------------------------------------------------------- #
# Manual edit window  (fix recognition: add / remove / recolour stones)
# --------------------------------------------------------------------------- #
# 3x3 region picker for partial crops: which part of the full board this crop is.
# Layout matches the on-screen button grid; each maps to (horizontal, vertical) anchors.
_REGION_LAYOUT = [
    [("左上", ("L", "T")), ("上边", ("C", "T")), ("右上", ("R", "T"))],
    [("左边", ("L", "M")), ("中央", ("C", "M")), ("右边", ("R", "M"))],
    [("左下", ("L", "B")), ("下边", ("C", "B")), ("右下", ("R", "B"))],
]


def build_move_sgf(black, white, order, size, col_off, row_off, to_move="B"):
    """SGF with un-numbered stones as setup (AB/AW) and the ordered stones as a move
    SEQUENCE (;B[..];W[..];...) so KaTrain can step through it move by move.
    `order` is a list of (col,row) in play order; each move's colour is the stone's colour."""
    from katrain.core.board_ocr import _SGF_LETTERS as L

    color_of = {tuple(s): "B" for s in black}
    color_of.update({tuple(s): "W" for s in white})
    order = [tuple(s) for s in order if tuple(s) in color_of]
    order_set = set(order)

    def coord(c, r):
        ac, ar = c + col_off, r + row_off
        return f"{L[ac]}{L[ar]}" if 0 <= ac < size and 0 <= ar < size else None

    setup_b = [coord(*s) for s in black if s not in order_set]
    setup_w = [coord(*s) for s in white if s not in order_set]
    first_color = color_of[order[0]] if order else (to_move.upper() if to_move.upper() in "BW" else "B")

    parts = [f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]"]
    ab = "".join(f"[{c}]" for c in setup_b if c)
    aw = "".join(f"[{c}]" for c in setup_w if c)
    if ab:
        parts.append(f"AB{ab}")
    if aw:
        parts.append(f"AW{aw}")
    parts.append(f"PL[{first_color}]")
    for s in order:
        c = coord(*s)
        if c:
            parts.append(f";{color_of[s]}[{c}]")
    parts.append(")")
    return "".join(parts)


def _region_offset(region, size, ncols, nrows):
    """Map a chosen board region to the (col_off, row_off) of the detected grid's
    top-left intersection. Corners pin to two edges (exact); sides pin one edge and
    centre the free axis; centre centres both."""
    h, v = region
    col = {"L": 0, "C": (size - ncols) // 2, "R": size - ncols}[h]
    row = {"T": 0, "M": (size - nrows) // 2, "B": size - nrows}[v]
    return max(0, col), max(0, row)


def edit_board(img: Image.Image, res, _autoclose_ms=None):
    """Show the captured board with recognised stones overlaid and let the user fix it.

    Left-click an intersection cycles  empty -> black -> white -> empty.
    For a PARTIAL crop (board edges off-screen) extra controls appear: the real board
    size and the coordinate of the highlighted top-left intersection (read off the
    on-screen coordinate labels), so the stones land at their true absolute positions.

    Returns [black, white, size, col_off, row_off] (col/row are LOCAL grid coords), or
    None if cancelled. `_autoclose_ms` auto-confirms after N ms (smoke-testing only).
    """
    import tkinter as tk
    from tkinter import simpledialog
    from PIL import ImageTk

    xs, ys = res.xs, res.ys
    is_full = bool(res.snapped_full_board)
    default_size = res.board_size if is_full else 19
    if not xs or not ys:
        return [list(res.black), list(res.white), default_size, 0, 0, []]

    root = tk.Tk()
    root.title("调整棋形 - 左键点交叉点：空→黑→白→空")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = min(1.0, (sw * 0.82) / img.width, (sh * 0.82 - 120) / img.height)
    dw, dh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    photo = ImageTk.PhotoImage(img.resize((dw, dh)))

    sxs = [x * scale for x in xs]
    sys_ = [y * scale for y in ys]
    sp = (sxs[-1] - sxs[0]) / (len(sxs) - 1) if len(sxs) > 1 else 20.0
    rad = max(6, int(sp * 0.42))

    state = {}
    for cr in res.black:
        state[tuple(cr)] = "B"
    for cr in res.white:
        state[tuple(cr)] = "W"

    # auto-read the move number (手数) printed on each stone -> {stone: number}.
    # Require >=2 read numbers so a lone last-move dot isn't mistaken for a sequence.
    try:
        nums = ocr.recognize_numbers(img, res)
    except Exception:
        nums = {}
    numbers = {tuple(k): int(v) for k, v in nums.items()} if len(nums) >= 2 else {}

    canvas = tk.Canvas(root, width=dw, height=dh, highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)
    canvas.image = photo  # keep a reference

    def redraw():
        canvas.delete("mk")
        for (i, j), col in state.items():
            x, y = sxs[i], sys_[j]
            fill = "#101010" if col == "B" else "#f6f6f6"
            outline = "#f6f6f6" if col == "B" else "#101010"
            canvas.create_oval(x - rad, y - rad, x + rad, y + rad,
                               fill=fill, outline=outline, width=2, tags="mk")
        num_font = ("Segoe UI", max(8, int(rad * 0.85)), "bold")
        for (i, j), col in state.items():              # the recognised/edited move number (手数)
            n = numbers.get((i, j))
            x, y = sxs[i], sys_[j]
            if n is not None:
                canvas.create_text(x, y, text=str(n),
                                   fill=("#ffffff" if col == "B" else "#101010"), font=num_font, tags="mk")
            elif seq_var.get():                        # in 手数模式, flag stones still missing a number
                canvas.create_text(x, y, text="?", fill="#ff5050", font=num_font, tags="mk")

    def on_click(e):
        i = min(range(len(sxs)), key=lambda k: abs(sxs[k] - e.x))
        j = min(range(len(sys_)), key=lambda k: abs(sys_[k] - e.y))
        if abs(sxs[i] - e.x) > sp * 0.6 or abs(sys_[j] - e.y) > sp * 0.6:
            return
        if seq_var.get():                              # 手数模式: set / fix this stone's move number
            if (i, j) not in state:
                return
            cur = numbers.get((i, j))
            s = simpledialog.askstring("手数", "输入该子的手数（清空内容=去掉编号）：",
                                       initialvalue=(str(cur) if cur else ""), parent=root)
            if s is None:                               # cancelled -> no change
                return
            s = s.strip()
            if s == "":
                numbers.pop((i, j), None)
            elif s.isdigit():
                numbers[(i, j)] = int(s)
        else:                                           # edit mode: cycle empty->B->W->empty
            nxt = {None: "B", "B": "W", "W": None}[state.get((i, j))]
            if nxt is None:
                state.pop((i, j), None)
                numbers.pop((i, j), None)
            else:
                state[(i, j)] = nxt
        redraw()

    canvas.bind("<Button-1>", on_click)

    holder = {"ok": False, "size": default_size, "off": (0, 0)}
    sel = {"region": None}
    size_var = tk.StringVar(value=str(default_size))
    seq_var = tk.BooleanVar(value=bool(numbers))   # auto-on if move numbers were read
    err_var = tk.StringVar(value="")

    def confirm(*_):
        try:
            size = int(size_var.get())
        except ValueError:
            size = default_size
        if is_full:
            off = (0, 0)
        elif sel["region"] is None:
            err_var.set("请先选择棋形所在位置（四角 / 四边 / 中）")
            return
        else:
            off = _region_offset(sel["region"], size, len(xs), len(ys))
        # only emit a move sequence if 手数模式 is on; otherwise import as setup stones.
        # order = stones sorted by their (recognised/edited) move number.
        order = [k for k, _ in sorted(numbers.items(), key=lambda kv: kv[1])] if seq_var.get() else []
        holder.update(ok=True, size=size, off=off, order=order)
        root.destroy()

    def cancel(*_):
        holder["ok"] = False
        root.destroy()

    def clear_order():
        numbers.clear()
        redraw()

    def auto_number():
        try:
            nm = ocr.recognize_numbers(img, res)
        except Exception:
            nm = {}
        numbers.clear()
        numbers.update({tuple(k): int(v) for k, v in nm.items()})
        seq_var.set(bool(numbers))
        on_seq_toggle()
        redraw()

    def on_seq_toggle():
        hint_var.set("手数模式：点棋子可填/改它的手数，红色?为没识别出的，需手填" if seq_var.get()
                     else "编辑模式：左键 空→黑→白→空")

    bar = tk.Frame(root)
    bar.pack(fill="x")
    hint_var = tk.StringVar(value="")
    tk.Checkbutton(bar, text="手数模式", variable=seq_var, command=on_seq_toggle).pack(side="left", padx=(8, 2))
    tk.Button(bar, text="识别手数", command=auto_number).pack(side="left", padx=2)
    tk.Button(bar, text="清空手数", command=clear_order).pack(side="left", padx=2)
    tk.Label(bar, textvariable=hint_var).pack(side="left", padx=8)
    on_seq_toggle()
    redraw()
    tk.Button(bar, text="确定导入 (Enter)", command=confirm).pack(side="right", padx=6, pady=6)
    tk.Button(bar, text="取消 (Esc)", command=cancel).pack(side="right", padx=6, pady=6)

    if not is_full:
        opt = tk.Frame(root)
        opt.pack(fill="x", pady=2)
        tk.Label(opt, text="棋盘").pack(side="left", padx=(8, 2))
        tk.OptionMenu(opt, size_var, "9", "13", "19").pack(side="left")
        tk.Label(opt, text="   棋形位置：").pack(side="left")
        grid = tk.Frame(opt)
        grid.pack(side="left")
        btns = {}

        def select(rk):
            sel["region"] = rk
            err_var.set("")
            for k, b in btns.items():
                on = (k == rk)
                b.config(relief=("sunken" if on else "raised"),
                         bg=("#5fb0ff" if on else "SystemButtonFace"))

        for rr, rowdef in enumerate(_REGION_LAYOUT):
            for cc, (lbl, rk) in enumerate(rowdef):
                b = tk.Button(grid, text=lbl, width=4, command=lambda rk=rk: select(rk))
                b.grid(row=rr, column=cc, padx=1, pady=1)
                btns[rk] = b
        tk.Label(root, textvariable=err_var, fg="red").pack(fill="x")
        tk.Label(root, fg="#666",
                 text="局部截图：选择这块棋形在整盘的位置（如右上角），角部需框到棋盘的两条边线").pack(fill="x")

    root.bind("<Return>", confirm)
    root.bind("<Escape>", cancel)
    root.attributes("-topmost", True)
    root.update()
    root.focus_force()
    if _autoclose_ms:
        def _auto():
            if not is_full and sel["region"] is None:
                sel["region"] = ("R", "T")
            confirm()
        root.after(_autoclose_ms, _auto)
    root.mainloop()

    if not holder["ok"]:
        return None
    black = [list(k) for k, v in state.items() if v == "B"]
    white = [list(k) for k, v in state.items() if v == "W"]
    seq = [[i, j] for (i, j) in holder.get("order", []) if (i, j) in state]
    return [black, white, holder["size"], holder["off"][0], holder["off"][1], seq]


# --------------------------------------------------------------------------- #
# Clipboard (standalone mode)
# --------------------------------------------------------------------------- #
def copy_to_clipboard(text: str) -> bool:
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()       # now the clipboard is owned
        r.after(120, r.destroy)
        r.mainloop()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Screenshot a Go board and import it as SGF.")
    ap.add_argument("--image", help="recognise an existing image file instead of screenshotting")
    ap.add_argument("--to-move", default="B", choices=["B", "W", "b", "w"])
    ap.add_argument("--debug", action="store_true", help="save annotated detection image")
    ap.add_argument("--edit", action="store_true", help="show a manual add/remove/recolour editor before emitting SGF")
    ap.add_argument("--stdout", action="store_true", help="print SGF to stdout (else copy to clipboard)")
    ap.add_argument("--save-capture", help="also save the raw captured image to this path")
    args = ap.parse_args(argv)

    _set_dpi_aware()

    if args.image:
        img = Image.open(args.image).convert("RGB")
        debug_base = args.image
    else:
        bbox = select_region()
        if not bbox:
            _log("已取消 / cancelled")
            return 2
        img = capture(bbox)
        if args.save_capture:
            img.save(args.save_capture)
            debug_base = os.path.splitext(args.save_capture)[0]
        else:
            debug_base = os.path.join(tempfile.gettempdir(), "katrain_screenshot")

    res = ocr.recognize(img)
    _log("OCR: " + res.summary())

    if args.debug:
        dbg = ocr.render_debug(img, res)
        dbg_path = debug_base + ".debug.png"
        dbg.save(dbg_path)
        _log("debug image -> " + dbg_path)

    edit_size, col_off, row_off, order = None, 0, 0, []
    if args.edit and not args.image:
        edited = edit_board(img, res)
        if edited is None:
            _log("编辑已取消 / edit cancelled")
            return 2
        res.black, res.white = [tuple(s) for s in edited[0]], [tuple(s) for s in edited[1]]
        edit_size, col_off, row_off = edited[2], edited[3], edited[4]
        order = edited[5] if len(edited) > 5 else []
        _log(f"after edit: black={len(res.black)} white={len(res.white)} size={edit_size} "
             f"off=({col_off},{row_off}) moves={len(order)}")

    if not res.black and not res.white:
        _log("没有识别到棋子 / no stones detected - check the debug image and recalibrate")
        # still emit an (empty) sgf so callers don't choke
    if order:
        sgf = build_move_sgf(list(res.black), list(res.white), order,
                             size=edit_size or res.board_size, col_off=col_off, row_off=row_off, to_move=args.to_move)
    else:
        sgf = res.to_sgf(args.to_move, size=edit_size, col_off=col_off, row_off=row_off)

    if args.stdout:
        print(sgf, flush=True)
    else:
        ok = copy_to_clipboard(sgf)
        _log("SGF -> clipboard (Ctrl-V in KaTrain)" if ok else "clipboard failed; SGF below")
        if not ok:
            print(sgf, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
