"""
Go board recognition from a screenshot region.

Pure PIL + numpy (no opencv). Designed for *clean screenshots* of Go boards
(digital boards / tsumego apps), where the grid is axis-aligned and stones are
solid circles - this is far easier and more reliable than recognising photos.

Pipeline:
    image -> grayscale -> detect grid (spacing + extent via projection) ->
    sample each intersection -> classify empty / black / white -> SGF.

The public entry points are:
    recognize(img)            -> RecognitionResult
    result.to_sgf(to_move)    -> SGF string KaTrain can load
    make_synthetic_board(...) -> PIL.Image  (for self-tests)
    render_debug(img, result) -> PIL.Image  (annotated, to eyeball detection)

Coordinates use (col, row), both 0-based from the TOP-LEFT of the detected grid.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional, Tuple

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# SGF column/row letters: a..s covers up to 19; full a..z,A..Z would reach 52.
_SGF_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Board sizes we will "snap" a detected line count to when it is close.
_COMMON_SIZES = (9, 13, 19)


@dataclasses.dataclass
class RecognitionResult:
    board_size: int                       # SGF board size (max of cols/rows, snapped)
    n_cols: int                           # detected vertical lines
    n_rows: int                           # detected horizontal lines
    black: List[Tuple[int, int]]          # (col, row) of black stones, 0-based top-left
    white: List[Tuple[int, int]]
    xs: List[int]                         # pixel x of each vertical line (for debug)
    ys: List[int]                         # pixel y of each horizontal line (for debug)
    snapped_full_board: bool              # True if line counts matched a common full size
    confidence: float                     # rough 0..1 self-assessment

    def to_sgf(self, to_move: str = "B", size: int = None, col_off: int = 0, row_off: int = 0) -> str:
        """Build an SGF with AB/AW setup stones + PL (whose turn). KaTrain reads this directly.

        For a partial-board crop, pass the real board `size` (e.g. 19) and the offset of the
        detected top-left intersection (col_off, row_off) so the local stones land at their
        true absolute coordinates. Stones falling outside the board are dropped.
        """
        size = int(size or self.board_size)

        def coords(stones):
            out = []
            for c, r in sorted(stones):
                ac, ar = c + col_off, r + row_off
                if 0 <= ac < size and 0 <= ar < size:
                    out.append(f"[{_SGF_LETTERS[ac]}{_SGF_LETTERS[ar]}]")
            return "".join(out)

        ab, aw = coords(self.black), coords(self.white)
        parts = [f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]"]
        if ab:
            parts.append(f"AB{ab}")
        if aw:
            parts.append(f"AW{aw}")
        parts.append(f"PL[{'W' if to_move.upper() == 'W' else 'B'}]")
        parts.append(")")
        return "".join(parts)

    def summary(self) -> str:
        return (
            f"{self.n_cols}x{self.n_rows} grid -> SZ[{self.board_size}] "
            f"(full board: {self.snapped_full_board}); "
            f"black={len(self.black)} white={len(self.white)} conf={self.confidence:.2f}"
        )


# --------------------------------------------------------------------------- #
# Grid detection
# --------------------------------------------------------------------------- #
def _line_positions(darkness: np.ndarray) -> List[int]:
    """
    Given a 1-D "darkness" projection along one axis, return the pixel positions
    of evenly-spaced grid lines.

    Strategy: the grid is periodic, so the autocorrelation of the projection has
    a strong peak at the line spacing. We recover that spacing, lock the phase by
    sliding a comb, then keep comb teeth that actually land on dark support.
    """
    n = len(darkness)
    if n < 20:
        return []
    p = darkness - darkness.mean()

    # --- dominant spacing via autocorrelation ---
    ac = np.correlate(p, p, mode="full")[n - 1:]
    lo = max(4, n // 60)            # smallest plausible cell (board can't have >~50 lines)
    hi = max(lo + 1, n // 2)        # need at least ~2 cells visible
    if hi <= lo:
        return []
    spacing = lo + int(np.argmax(ac[lo:hi]))
    # Guard against locking onto a 2x/3x/4x HARMONIC of the true spacing (happens when stone
    # clusters / labels create a coarser periodicity): if a sub-multiple also has strong
    # autocorrelation, that is the real grid period - prefer the smallest such fundamental.
    for div in (4, 3, 2):
        cand = int(round(spacing / div))
        if cand >= lo and ac[cand] >= 0.45 * ac[spacing]:
            spacing = cand
            break
    if spacing < 4:
        return []

    # --- lock phase: best offset for a comb of this spacing ---
    best_off, best_score = 0, -1e18
    for off in range(spacing):
        idx = np.arange(off, n, spacing)
        score = darkness[idx].sum()
        if score > best_score:
            best_score, best_off = score, off
    teeth = np.arange(best_off, n, spacing)

    # --- keep only teeth with real support (a line, not empty margin) ---
    thr = darkness.mean() + 0.4 * darkness.std()
    win = max(1, spacing // 6)
    kept = []
    for t in teeth:
        a, b = max(0, t - win), min(n, t + win + 1)
        # snap to the darkest pixel in the local window
        local = darkness[a:b]
        if local.size and local.max() >= thr:
            kept.append(a + int(np.argmax(local)))
    # de-duplicate positions that snapped together
    dedup: List[int] = []
    for t in kept:
        if not dedup or t - dedup[-1] >= spacing * 0.5:
            dedup.append(t)
    return dedup


def _snap_size(n_lines: int) -> Tuple[int, bool]:
    """Snap a detected line count to the nearest common full-board size if close."""
    for s in _COMMON_SIZES:
        if abs(n_lines - s) <= 1:
            return s, True
    return n_lines, False


def _regularize(pos: List[int], length: int, gray: np.ndarray, axis: str) -> Tuple[List[int], bool]:
    """
    Turn raw detected line positions into a clean, evenly-spaced grid, dropping
    outliers (coordinate labels A-T/1-19, a watermark, a play marker). Works for a
    FULL board and for a PARTIAL crop (edges off-screen): it does NOT force a 9/13/19
    count, it keeps exactly the real grid lines present.

    Key idea - distinguish a grid line from a coordinate label by *continuity*: a real
    line runs across the whole board (dark pixels at almost every perpendicular
    position, INCLUDING where it passes under stones), whereas a coordinate glyph or a
    watermark is dark only over a short stretch. This holds even in stone-dense areas
    (stones are dark too), unlike a projection-magnitude score which collapses there.

    Returns (grid_positions, is_full_size) - is_full_size means count in {9,13,19}.
    """
    pos = sorted(int(p) for p in pos)
    if len(pos) < 4:
        return pos, False
    s = float(np.median(np.diff(pos)))
    if s < 4:
        return pos, False
    si = max(1, int(round(s)))
    w = max(1, int(s * 0.12))
    H, Wd = gray.shape
    thr_dark = float(np.median(gray)) * 0.72   # below this is a line or a stone, not board

    def continuity(p: int) -> float:
        if axis == "col":
            a, b = max(0, p - 1), min(Wd, p + 2)
            line = gray[:, a:b].min(axis=1)
        else:
            a, b = max(0, p - 1), min(H, p + 2)
            line = gray[a:b, :].min(axis=0)
        return float((line < thr_dark).mean()) if line.size else 0.0

    # lock the comb phase to maximise total continuity over the whole axis
    best_off, best_score = 0, -1.0
    for off in range(si):
        score = sum(continuity(int(round(c))) for c in np.arange(off, length, s))
        if score > best_score:
            best_score, best_off = score, off

    teeth = [int(round(c)) for c in np.arange(best_off, length + 1, s)]
    cont = [continuity(t) for t in teeth]
    if not teeth or max(cont) <= 0:
        return pos, False
    thr = 0.55 * max(cont)              # a real line vs a margin / short label glyph
    flags = [v >= thr for v in cont]

    # longest contiguous run of line-like teeth = the actual board grid (interior teeth
    # are kept even in dense areas, because continuity stays high through stones)
    best_lo = best_len = cur_lo = cur_len = 0
    for i, f in enumerate(flags):
        if f:
            if cur_len == 0:
                cur_lo = i
            cur_len += 1
            if cur_len > best_len:
                best_len, best_lo = cur_len, cur_lo
        else:
            cur_len = 0
    grid = teeth[best_lo:best_lo + best_len]
    # snap each grid tooth to the nearest actually-detected line for sub-pixel accuracy
    snapped = []
    for t in grid:
        j = min(range(len(pos)), key=lambda kk: abs(pos[kk] - t))
        snapped.append(pos[j] if abs(pos[j] - t) <= w + 2 else t)
    if len(snapped) < 3:
        return pos, False
    return snapped, len(snapped) in _COMMON_SIZES


def _darkness_projection(gray: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Return (column_darkness, row_darkness). Darkness = how dark relative to the board."""
    # board background is the most common (modal-ish) brightness; lines/stones are darker.
    inv = 255.0 - gray.astype(np.float64)
    col = inv.mean(axis=0)   # length W
    row = inv.mean(axis=1)   # length H
    return col, row


# --------------------------------------------------------------------------- #
# Stone classification
# --------------------------------------------------------------------------- #
# A go stone is achromatic (black or white); wood, coordinate text and play markers
# are coloured/warm. These guard the white class against e.g. an orange "your move"
# triangle sitting on the centre point.
_STONE_MAX_SAT = 32.0     # max (maxchannel-minchannel) for a stone-coloured patch
_WHITE_MAX_WARM = 24.0    # max (R-B); white stones are neutral/cool, never warm like wood


def _classify(rgb: np.ndarray, xs: List[int], ys: List[int]) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]], float]:
    """
    Sample a DISC at every intersection and take its MEDIAN colour, then label
    empty / black / white.

    The disc spans most of the stone (inside the outline). Taking the median over the whole
    disc is the key to reading stones that carry a MOVE NUMBER (or a last-move dot): the
    digit - even a 2-digit one - is a minority of the disc's pixels, so the median equals the
    stone's fill colour (a white stone with a big black "10" still reads white). Features:
    brightness = mean(R,G,B); sat = max-min (wood/markers high); warm = R-B (wood positive).
    A stone must depart from the board background AND be achromatic; white must also not be warm.
    """
    h, w, _ = rgb.shape
    sx = int(np.median(np.diff(xs))) if len(xs) > 1 else 10
    sy = int(np.median(np.diff(ys))) if len(ys) > 1 else 10
    cell = max(4, min(sx, sy))
    r_out = max(3, int(cell * 0.37))   # inside the stone, mostly clear of the dark outline
    yy, xx = np.mgrid[-r_out:r_out + 1, -r_out:r_out + 1]
    disc = (xx * xx + yy * yy) <= (r_out * r_out)

    rgbf = rgb.astype(np.float64)
    gray = rgbf.mean(axis=2)
    bg = float(np.median(gray))                       # board (wood / paper) brightness
    light_thr = bg + 0.35 * (255.0 - bg)              # white fill is much brighter than board
    dark_thr = bg * 0.45                              # black fill is much darker than board
    sat_achr = 70.0                                   # stones are grey; wood is saturated

    # Per intersection, VOTE over the disc's pixels: a stone's fill colour is whichever of
    # white/black has more achromatic pixels than the other (the move-number digits are only
    # a minority of strokes, so they never outvote the fill - even 2-digit numbers).
    black, white = [], []
    stones = 0
    total = 0
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            a, b, c, d = y - r_out, y + r_out + 1, x - r_out, x + r_out + 1
            if a >= 0 and c >= 0 and b <= h and d <= w:
                pix = rgbf[a:b, c:d, :][disc]
            else:
                pix = rgbf[max(0, a):b, max(0, c):d, :].reshape(-1, 3)
            n = len(pix)
            if n == 0:
                continue
            total += 1
            br = pix.mean(axis=1)
            sa = pix.max(axis=1) - pix.min(axis=1)
            achr = sa < sat_achr
            lf = float(np.count_nonzero(achr & (br > light_thr))) / n
            df = float(np.count_nonzero(achr & (br < dark_thr))) / n
            if max(lf, df) < 0.30:        # mostly board -> empty
                continue
            if lf >= df:
                white.append((i, j))
            else:
                black.append((i, j))
            stones += 1
    conf = round(min(1.0, stones / max(8.0, 0.12 * total)), 3) if total else 0.0
    return black, white, conf


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def recognize(img: Image.Image) -> RecognitionResult:
    img = img.convert("RGB")
    rgb = np.asarray(img)
    gray = np.asarray(img.convert("L"))

    col_dark, row_dark = _darkness_projection(gray)
    xs, full_x = _regularize(_line_positions(col_dark), gray.shape[1], gray, "col")
    ys, full_y = _regularize(_line_positions(row_dark), gray.shape[0], gray, "row")

    n_cols, n_rows = len(xs), len(ys)
    # A clean full board has both axes the same common size (9/13/19), edges included.
    is_full = full_x and full_y and n_cols == n_rows and n_cols in _COMMON_SIZES
    if is_full:
        board_size = n_cols
    else:
        # Partial crop: the board edges are off-screen so absolute position is unknown.
        # Default to a 19x19 board; the real size + top-left offset are set in the edit
        # window. Stones stay in LOCAL (0-based, top-left of the detected grid) coords.
        board_size = 19

    if not xs or not ys:
        return RecognitionResult(board_size, n_cols, n_rows, [], [], xs, ys, is_full, 0.0)

    black, white, stone_conf = _classify(rgb, xs, ys)

    grid_conf = 1.0 if is_full else 0.6
    confidence = round(0.5 * grid_conf + 0.5 * stone_conf, 3)
    return RecognitionResult(board_size, n_cols, n_rows, black, white, xs, ys, is_full, confidence)


# --------------------------------------------------------------------------- #
# Move-number (手数) recognition: read the digit printed on each stone
# --------------------------------------------------------------------------- #
_DIGIT_TEMPLATES = None   # list of (digit:int, norm_mask:np.ndarray[bool])
_TPL_H, _TPL_W = 28, 20


def _norm_glyph(mask: np.ndarray):
    """Crop a binary glyph to its bounding box and resize to a fixed (W,H) box."""
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    sub = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray((sub * 255).astype("uint8")).resize((_TPL_W, _TPL_H))
    return np.asarray(im) > 127


def _digit_templates():
    """Build (cached) normalised templates for digits 0-9 from several fonts."""
    global _DIGIT_TEMPLATES
    if _DIGIT_TEMPLATES is not None:
        return _DIGIT_TEMPLATES
    pkg_fonts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
    candidates = [
        os.path.join(pkg_fonts, "NotoSansCJKsc-Regular.otf"),
        r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\seguisb.ttf",
        r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\verdana.ttf",
        r"C:\Windows\Fonts\micross.ttf",
    ]
    fonts = []
    for fp in candidates:
        try:
            if os.path.exists(fp):
                fonts.append(ImageFont.truetype(fp, 64))
        except Exception:
            pass
    if not fonts:
        try:
            fonts.append(ImageFont.load_default())
        except Exception:
            pass
    templates = []
    for f in fonts:
        for d in range(10):
            im = Image.new("L", (90, 100), 0)
            dd = ImageDraw.Draw(im)
            t = str(d)
            bb = dd.textbbox((0, 0), t, font=f)
            dd.text((45 - (bb[2] - bb[0]) / 2 - bb[0], 50 - (bb[3] - bb[1]) / 2 - bb[1]), t, fill=255, font=f)
            ng = _norm_glyph(np.asarray(im) > 127)
            if ng is not None:
                templates.append((d, ng))
    _DIGIT_TEMPLATES = templates
    return templates


def _classify_digit(ng: np.ndarray):
    """Return (digit, score) for a normalised glyph by best template match (fallback)."""
    best_d, best_s = -1, -1.0
    for d, tm in _digit_templates():
        s = float(np.mean(ng == tm))
        if s > best_s:
            best_s, best_d = s, d
    return best_d, best_s


# --- tiny digit CNN, trained offline (see _train_digit_cnn.py), run in pure numpy ----------
_DIGIT_CNN = None   # dict of weights, or False if unavailable


def _digit_model():
    global _DIGIT_CNN
    if _DIGIT_CNN is None:
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digit_cnn.npz")
            data = np.load(path)
            _DIGIT_CNN = {k: data[k].astype(np.float32) for k in data.files}
        except Exception:
            _DIGIT_CNN = False
    return _DIGIT_CNN or None


def _conv2d(x, w, b):                      # x:(Cin,H,W) w:(Cout,Cin,3,3) pad=1 stride=1
    cin, h, wd = x.shape
    xp = np.pad(x, ((0, 0), (1, 1), (1, 1)))
    out = np.zeros((w.shape[0], h, wd), np.float32)
    for ky in range(3):
        for kx in range(3):
            out += np.einsum("oc,chw->ohw", w[:, :, ky, kx], xp[:, ky:ky + h, kx:kx + wd])
    return out + b[:, None, None]


def _maxpool2(x):
    c, h, wd = x.shape
    x = x[:, :h // 2 * 2, :wd // 2 * 2]
    return x.reshape(c, h // 2, 2, wd // 2, 2).max(axis=(2, 4))


def _normalize_glyph28(mask: np.ndarray):
    """Crop a binary glyph to bbox and place it in a 28x28 box (matches training)."""
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    sub = (mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8)) * 255
    g = np.asarray(Image.fromarray(sub).resize((int(28 * 0.7), int(28 * 0.95)), Image.BILINEAR))
    out = np.zeros((28, 28), np.float32)
    h, w = g.shape
    oy, ox = (28 - h) // 2, (28 - w) // 2
    out[oy:oy + h, ox:ox + w] = g.astype(np.float32) / 255.0
    return (out > 0.45).astype(np.float32)


def _read_digit(binmask: np.ndarray):
    """Return (digit, confidence). Uses the CNN if available, else template matching."""
    W = _digit_model()
    if W is not None:
        g = _normalize_glyph28(binmask)
        if g is None:
            return None, 0.0
        x = g[None, :, :]
        x = _maxpool2(np.maximum(_conv2d(x, W["c1.weight"], W["c1.bias"]), 0))
        x = _maxpool2(np.maximum(_conv2d(x, W["c2.weight"], W["c2.bias"]), 0))
        x = x.reshape(-1)
        x = np.maximum(W["fc1.weight"] @ x + W["fc1.bias"], 0)
        logits = W["fc2.weight"] @ x + W["fc2.bias"]
        e = np.exp(logits - logits.max())
        p = e / e.sum()
        d = int(p.argmax())
        return d, float(p[d])
    ng = _norm_glyph(binmask)
    if ng is None:
        return None, 0.0
    return _classify_digit(ng)


def recognize_numbers(img: Image.Image, result: RecognitionResult, min_score: float = 0.58) -> dict:
    """Read the move number printed on each detected stone.

    Returns {(col,row): number} for stones whose digit(s) were read confidently. The digit
    is the opposite colour of the stone (white on black, black on white); we crop the stone,
    threshold the digit pixels, split into individual digits by column gaps, and template-match
    each against 0-9. Robust to 1- and 2-digit numbers.
    """
    rgb = np.asarray(img.convert("RGB")).astype(np.float64)
    gray = rgb.mean(axis=2)
    H, W = gray.shape
    xs, ys = result.xs, result.ys
    if len(xs) < 2 or len(ys) < 2:
        return {}
    cell = int(min(np.median(np.diff(xs)), np.median(np.diff(ys))))
    R = max(4, int(cell * 0.36))
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
    disc = (xx * xx + yy * yy) <= (R * R)
    color_of = {(c, r): "B" for c, r in result.black}
    color_of.update({(c, r): "W" for c, r in result.white})

    out = {}
    for (c, r), col in color_of.items():
        x, y = xs[c], ys[r]
        a, b, cc, dd = y - R, y + R + 1, x - R, x + R + 1
        if a < 0 or cc < 0 or b > H or dd > W:
            continue
        patch = gray[a:b, cc:dd]
        rp = rgb[a:b, cc:dd, :]
        sat = rp.max(axis=2) - rp.min(axis=2)               # coloured (e.g. red "67") digits are saturated
        bright_dark = (patch > 150) if col == "B" else (patch < 110)
        digit_px = (bright_dark | (sat > 80)) & disc
        if digit_px.sum() < max(6, cell * 0.4):
            continue
        cols = digit_px.any(axis=0)
        runs, s = [], None
        for i, v in enumerate(cols):
            if v and s is None:
                s = i
            elif not v and s is not None:
                runs.append((s, i))
                s = None
        if s is not None:
            runs.append((s, len(cols)))
        runs = [(p, q) for p, q in runs if q - p >= 2]
        if not runs or len(runs) > 3:
            continue
        digits, ok, sc_min = "", True, 1.0
        for p, q in runs:
            d, sc = _read_digit(digit_px[:, p:q])
            if d is None or sc < min_score:
                ok = False
                break
            digits += str(d)
            sc_min = min(sc_min, sc)
        if ok and digits:
            out[(c, r)] = (int(digits), sc_min)

    # Each move number is used once. When two stones read the same value (a misread), keep the
    # higher-confidence stone and drop the other (it stays unread -> shown as '?', user fixes it).
    best = {}  # number -> (stone, score)
    for stone, (num, sc) in out.items():
        if num not in best or sc > best[num][1]:
            best[num] = (stone, sc)
    return {stone: num for num, (stone, sc) in best.items()}


def render_debug(img: Image.Image, result: RecognitionResult) -> Image.Image:
    """Return a copy of img with detected grid + stone labels drawn on it."""
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    for x in result.xs:
        d.line([(x, 0), (x, out.height)], fill=(255, 0, 0), width=1)
    for y in result.ys:
        d.line([(0, y), (out.width, y)], fill=(255, 0, 0), width=1)
    sx = int(np.median(np.diff(result.xs))) if len(result.xs) > 1 else 10
    sy = int(np.median(np.diff(result.ys))) if len(result.ys) > 1 else 10
    rr = max(3, int(min(sx, sy) * 0.32))
    for (c, r) in result.black:
        x, y = result.xs[c], result.ys[r]
        d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=(0, 200, 255), width=2)
    for (c, r) in result.white:
        x, y = result.xs[c], result.ys[r]
        d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=(0, 255, 0), width=2)
    return out


# --------------------------------------------------------------------------- #
# Synthetic board generator (for self-tests; no screenshot needed)
# --------------------------------------------------------------------------- #
def make_synthetic_board(
    size: int = 19,
    black: Optional[List[Tuple[int, int]]] = None,
    white: Optional[List[Tuple[int, int]]] = None,
    cell: int = 26,
    margin: int = 30,
    bg=(219, 179, 107),     # wood
    line=(40, 40, 40),
    pad_outside: int = 12,  # extra board background beyond the outer lines
) -> Image.Image:
    black = black or []
    white = white or []
    span = cell * (size - 1)
    dim = span + 2 * margin
    img = Image.new("RGB", (dim, dim), bg)
    d = ImageDraw.Draw(img)
    xs = [margin + i * cell for i in range(size)]
    ys = [margin + j * cell for j in range(size)]
    for x in xs:
        d.line([(x, ys[0]), (x, ys[-1])], fill=line, width=1)
    for y in ys:
        d.line([(xs[0], y), (xs[-1], y)], fill=line, width=1)
    r = int(cell * 0.45)
    for (c, rr) in black:
        x, y = xs[c], ys[rr]
        d.ellipse([x - r, y - r, x + r, y + r], fill=(20, 20, 20))
    for (c, rr) in white:
        x, y = xs[c], ys[rr]
        d.ellipse([x - r, y - r, x + r, y + r], fill=(245, 245, 245), outline=(30, 30, 30))
    # crop with a little outside padding to mimic a loose frame-select
    if pad_outside:
        a = margin - pad_outside
        b = dim - margin + pad_outside
        img = img.crop((max(0, a), max(0, a), min(dim, b), min(dim, b)))
    return img
