"""
Offline trainer for the move-number digit recogniser (route A: small local CNN).

NOT needed at runtime — this is run once (needs torch) to produce `digit_cnn.npz`,
which board_ocr.py loads and runs with pure numpy (no torch dependency at runtime).

Pipeline:
  synthetic digit glyphs (many fonts + heavy augmentation, rendered as BINARY masks
  normalised to 28x28 — exactly the representation board_ocr extracts from a stone) ->
  tiny CNN (28x28 -> 10 classes) -> export weights to npz.

Usage:
  python -m katrain.core._train_digit_cnn --gen-preview   # just dump a sample sheet (no torch)
  python -m katrain.core._train_digit_cnn --train         # train + export digit_cnn.npz (needs torch)
"""

from __future__ import annotations

import glob
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SIZE = 28  # normalised glyph box (HxW)


# --------------------------------------------------------------------------- #
# Fonts: gather as many as possible for generalisation to unseen app fonts
# --------------------------------------------------------------------------- #
def _gather_fonts():
    paths = []
    win = r"C:\Windows\Fonts"
    if os.path.isdir(win):
        for fp in glob.glob(os.path.join(win, "*.ttf")) + glob.glob(os.path.join(win, "*.otf")):
            paths.append(fp)
    pkg_fonts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
    for fp in glob.glob(os.path.join(pkg_fonts, "*.otf")) + glob.glob(os.path.join(pkg_fonts, "*.ttf")):
        paths.append(fp)
    fonts = []
    for fp in paths:
        try:
            # skip symbol/emoji fonts that don't render digits well
            f = ImageFont.truetype(fp, 64)
            bb = f.getmask("7").getbbox()
            if bb is not None:
                fonts.append(fp)
        except Exception:
            pass
    return fonts


_FONTS = None


def _fonts():
    global _FONTS
    if _FONTS is None:
        _FONTS = _gather_fonts()
    return _FONTS


# --------------------------------------------------------------------------- #
# Render one digit as a normalised BINARY glyph, with augmentation
# --------------------------------------------------------------------------- #
def _render_digit(d, font_path, rng):
    px = rng.randint(48, 96)
    try:
        font = ImageFont.truetype(font_path, px)
    except Exception:
        font = ImageFont.load_default()
    canvas = px * 2
    im = Image.new("L", (canvas, canvas), 0)
    dr = ImageDraw.Draw(im)
    t = str(d)
    bb = dr.textbbox((0, 0), t, font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    dr.text((canvas / 2 - w / 2 - bb[0], canvas / 2 - h / 2 - bb[1]), t, fill=255, font=font)

    # random rotation (slant) + scale jitter
    if rng.random() < 0.7:
        im = im.rotate(rng.uniform(-12, 12), resample=Image.BILINEAR, expand=True)
    arr = np.asarray(im) > 96
    ys, xs = np.where(arr)
    if len(xs) == 0:
        return None
    sub = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8) * 255
    g = Image.fromarray(sub)

    # thickness jitter via resize-then-threshold blur (emulates erode/dilate + AA)
    scale = rng.uniform(0.8, 1.2)
    g = g.resize((max(1, int(SIZE * 0.7 * scale)), max(1, int(SIZE * 0.95 * scale))), Image.BILINEAR)
    glyph = Image.new("L", (SIZE, SIZE), 0)
    ox = (SIZE - g.width) // 2 + rng.randint(-2, 2)
    oy = (SIZE - g.height) // 2 + rng.randint(-2, 2)
    glyph.paste(g, (ox, oy))
    a = np.asarray(glyph).astype(np.float32) / 255.0

    # augment: partial crop (neighbour/disc clipping), speckle noise, blur-ish threshold
    if rng.random() < 0.35:  # clip an edge (digit partly outside the sampled disc)
        side = rng.randint(0, 3)
        cut = rng.randint(2, 7)
        if side == 0:
            a[:cut, :] = 0
        elif side == 1:
            a[-cut:, :] = 0
        elif side == 2:
            a[:, :cut] = 0
        else:
            a[:, -cut:] = 0
    if rng.random() < 0.4:  # speckle
        noise = (np.random.rand(SIZE, SIZE) < 0.03).astype(np.float32)
        a = np.clip(a + noise, 0, 1)
    a = (a > 0.45).astype(np.float32)  # binarise like board_ocr's mask
    return a


def make_dataset(n_per_digit=2000, seed=0):
    rng = random.Random(seed)
    np.random.seed(seed)
    fonts = _fonts()
    if not fonts:
        raise RuntimeError("no usable fonts found")
    X, y = [], []
    for d in range(10):
        made = 0
        while made < n_per_digit:
            fp = rng.choice(fonts)
            g = _render_digit(d, fp, rng)
            if g is None:
                continue
            X.append(g)
            y.append(d)
            made += 1
    X = np.stack(X).astype(np.float32)[:, None, :, :]  # N,1,28,28
    y = np.array(y, dtype=np.int64)
    idx = np.random.permutation(len(y))
    return X[idx], y[idx], len(fonts)


def gen_preview(path):
    rng = random.Random(1)
    fonts = _fonts()
    sheet = Image.new("L", (SIZE * 10, SIZE * 10), 30)
    for col, d in enumerate(range(10)):
        for row in range(10):
            g = None
            while g is None:
                g = _render_digit(d, rng.choice(fonts), rng)
            sheet.paste(Image.fromarray((g * 255).astype("uint8")), (col * SIZE, row * SIZE))
    sheet.save(path)
    return len(fonts)


# --------------------------------------------------------------------------- #
# Train (torch) + export to npz for numpy inference
# --------------------------------------------------------------------------- #
def train_and_export(out_npz, epochs=8, n_per_digit=2500):
    import torch
    import torch.nn as nn

    X, y, nf = make_dataset(n_per_digit=n_per_digit)
    print(f"dataset: {X.shape} from {nf} fonts")
    n_val = len(y) // 10
    Xtr, ytr, Xva, yva = X[n_val:], y[n_val:], X[:n_val], y[:n_val]

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.c1 = nn.Conv2d(1, 8, 3, padding=1)
            self.c2 = nn.Conv2d(8, 16, 3, padding=1)
            self.fc1 = nn.Linear(16 * 7 * 7, 32)
            self.fc2 = nn.Linear(32, 10)
            self.pool = nn.MaxPool2d(2)
            self.relu = nn.ReLU()

        def forward(self, x):
            x = self.pool(self.relu(self.c1(x)))
            x = self.pool(self.relu(self.c2(x)))
            x = x.flatten(1)
            x = self.relu(self.fc1(x))
            return self.fc2(x)

    net = Net()
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    lossf = nn.CrossEntropyLoss()
    Xtr_t, ytr_t = torch.tensor(Xtr), torch.tensor(ytr)
    Xva_t, yva_t = torch.tensor(Xva), torch.tensor(yva)
    bs = 256
    for ep in range(epochs):
        net.train()
        perm = torch.randperm(len(ytr_t))
        for i in range(0, len(perm), bs):
            b = perm[i:i + bs]
            opt.zero_grad()
            loss = lossf(net(Xtr_t[b]), ytr_t[b])
            loss.backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            acc = (net(Xva_t).argmax(1) == yva_t).float().mean().item()
        print(f"epoch {ep + 1}/{epochs}  val_acc={acc:.4f}")

    w = {k: v.detach().numpy() for k, v in net.state_dict().items()}
    np.savez(out_npz, **w)
    print("exported", out_npz, "val_acc=%.4f" % acc)
    return acc


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--gen-preview", action="store_true")
    ap.add_argument("--train", action="store_true")
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    if args.gen_preview:
        p = os.path.join(here, "_digit_preview.png")
        print("fonts:", gen_preview(p), "-> preview", p)
    if args.train:
        train_and_export(os.path.join(here, "digit_cnn.npz"))
