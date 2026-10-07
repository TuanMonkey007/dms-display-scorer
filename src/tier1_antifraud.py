#!/usr/bin/env python3
"""Tier1 antifraud: moire FFT + blur Laplacian + EXIF/size check (stdlib+numpy+PIL).

Doc: python src/tier1_antifraud.py --dir <project>/data/displays/<range>/<pid>
Ghi antifraud.json: moire, blur, exif_software, recap_suspect. Khong sua tier1.json.
"""
import argparse
import json
import os
from collections import Counter

import numpy as np
from PIL import Image


def moire_score(gray):
    """FFT: dinh NHON cuc bo = van moire man hinh. Tra 0..1.
    Fix 07/10: ban cu mean(top100)/median bao hoa (p50=0.51) vi ke hang
    von nhieu canh sac. Ban nay do peak/max so voi nen dia phuong."""
    h, w = gray.shape
    f = np.fft.fftshift(np.fft.fft2(gray))
    mag = np.abs(f)
    cy, cx = h // 2, w // 2
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    band = (dist > min(h, w) * 0.30) & (dist < min(h, w) * 0.48)
    vals = mag[band]
    med = np.median(vals) + 1e-6
    peak = float(np.max(vals))
    # Luoi pixel man hinh chu ky ~2-3px -> dinh FFT sat Nyquist.
    # Fix 07/10 lan 2: band cu 0.15-0.45 an don hoa van tuong/gach
    # (anh truc tiep moire 0.99 gia). Band hep 0.30-0.48 chi bat luoi man.
    return round(float(min(max(peak / med / 60.0 - 0.08, 0.0), 1.0)), 3)


def blur_var(gray):
    c = gray[1:-1, 1:-1]
    lap = c * -4 + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
    return round(float(np.var(lap)), 1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--moire-th", type=float, default=0.8)
    ap.add_argument("--blur-th", type=float, default=30.0)
    a = ap.parse_args(argv)
    tier = json.load(open(os.path.join(a.dir, "tier1.json"), encoding="utf-8"))
    sizes = Counter()
    for t in tier:
        fp = os.path.join(a.dir, t["file"])
        try:
            sizes[Image.open(fp).size] += 1
        except OSError:
            pass
    out = []
    for t in tier:
        fp = os.path.join(a.dir, t["file"])
        try:
            im = Image.open(fp).convert("L").resize((512, 512))
        except OSError:
            out.append({"imageId": t["imageId"], "error": "unreadable"})
            continue
        g = np.asarray(im, dtype=float)
        m = moire_score(g)
        try:
            b = blur_var(g)
        except Exception:
            b = -1.0
        try:
            full = Image.open(fp)
            ex = full.getexif()
            soft = str(ex.get(0x0131, "")) if ex else ""
        except Exception:
            soft = ""
        sz = Image.open(fp).size
        dup = sizes[sz] > len(tier) * 0.5 and len(sizes) <= 3
        suspect = bool(m >= a.moire_th or (b >= 0 and b < a.blur_th and m > 0.3))
        out.append({"imageId": t["imageId"], "file": t["file"],
                    "moire": m, "blur_var": b, "exif_software": soft,
                    "size_dup_batch": bool(dup), "recap_suspect": suspect})
    json.dump(out, open(os.path.join(a.dir, "antifraud.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    n = sum(1 for o in out if o.get("recap_suspect"))
    print(f"n={len(out)} suspect={n} moire_th={a.moire_th} blur_th={a.blur_th}")
    for o in sorted(out, key=lambda x: x.get("moire", 0), reverse=True)[:5]:
        print(" top", o["imageId"], o["moire"], o["blur_var"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
