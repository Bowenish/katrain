"""Self-test for board_ocr using synthetic boards (no screenshot required)."""
import sys
from katrain.core import board_ocr as ocr


def check(size, black, white):
    img = ocr.make_synthetic_board(size=size, black=black, white=white)
    res = ocr.recognize(img)
    okb = set(res.black) == set(black)
    okw = set(res.white) == set(white)
    okg = res.n_cols == size and res.n_rows == size
    status = "PASS" if (okb and okw and okg) else "FAIL"
    print(f"[{status}] size={size} {res.summary()}")
    if not (okb and okw and okg):
        print(f"    expected grid {size}x{size}, got {res.n_cols}x{res.n_rows}")
        print(f"    black exp={sorted(black)}")
        print(f"    black got={sorted(res.black)}")
        print(f"    white exp={sorted(white)}")
        print(f"    white got={sorted(res.white)}")
        print(f"    sgf={res.to_sgf()}")
    return okb and okw and okg


cases = [
    (19, [(3, 3), (15, 3), (3, 15), (15, 15), (9, 9)], [(4, 3), (3, 4), (16, 16)]),
    (13, [(3, 3), (9, 3), (6, 6)], [(3, 9), (9, 9)]),
    (9, [(2, 2), (6, 2), (4, 4)], [(2, 6), (6, 6), (4, 6)]),
    # a corner-style tsumego shape on a 19x19 board
    (19, [(0, 1), (1, 0), (2, 1), (1, 2)], [(0, 0), (2, 0), (0, 2)]),
]

def check_numbered():
    """Stones carrying move numbers (incl. 2-digit) must still classify by fill colour."""
    from PIL import ImageDraw, ImageFont

    seq = [(3, 3, 1, True), (5, 3, 2, False), (3, 5, 3, True), (6, 6, 4, False),
           (9, 9, 5, True), (10, 9, 6, False), (15, 15, 9, True), (15, 3, 10, False),
           (3, 15, 11, True), (9, 3, 12, False), (16, 16, 13, True), (16, 2, 14, False)]
    black = [(c, r) for c, r, n, b in seq if b]
    white = [(c, r) for c, r, n, b in seq if not b]
    cell, margin = 34, 40
    img = ocr.make_synthetic_board(19, black=black, white=white, cell=cell, margin=margin, pad_outside=0)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arialbd.ttf", int(cell * 0.42))
    except Exception:
        font = ImageFont.load_default()
    for c, r, n, b in seq:
        x, y, t = margin + c * cell, margin + r * cell, str(n)
        col = (255, 255, 255) if b else (15, 15, 15)
        bb = d.textbbox((0, 0), t, font=font)
        d.text((x - (bb[2] - bb[0]) / 2 - bb[0], y - (bb[3] - bb[1]) / 2 - bb[1]), t, fill=col, font=font)
    res = ocr.recognize(img)
    ok = set(res.black) == set(black) and set(res.white) == set(white)
    print(f"[{'PASS' if ok else 'FAIL'}] numbered stones {res.summary()}")
    if not ok:
        print(f"    black exp={sorted(black)} got={sorted(res.black)}")
        print(f"    white exp={sorted(white)} got={sorted(res.white)}")
    return ok


all_ok = all(check(*c) for c in cases)
all_ok = check_numbered() and all_ok
print("\nRESULT:", "ALL PASS" if all_ok else "SOME FAILED")
sys.exit(0 if all_ok else 1)
