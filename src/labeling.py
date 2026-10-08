#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gán nhãn crop sản phẩm + huấn luyện bộ phân loại (softmax regression trên CLIP embedding).

Dataset dùng chung giữa các chương trình, phân theo sku_code:
    data/labels/crops/<sku_code>/<imageId>_<n>.jpg      (sku_code = "OTHER" cho sản phẩm khác)
    data/labels/state.json                              (trạng thái hiển thị trong tab gán nhãn)
Model: models/clf.pt  -> tier2_cv.py tự dùng nếu có.
"""
import os
import json
import glob
import random

import numpy as np
import torch
from PIL import Image, ImageDraw

import tier2_cv as T

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL_DIR = os.path.join(BASE_DIR, "data", "labels")
CROPS_DIR = os.path.join(LABEL_DIR, "crops")                  # crop chữ nhật quanh polygon (giống lúc suy luận)
MASKED_DIR = os.path.join(LABEL_DIR, "crops_masked")          # cùng crop nhưng nền ngoài polygon tô xám
STATE_FILE = os.path.join(LABEL_DIR, "state.json")
CLF_FILE = os.path.join(BASE_DIR, "models", "clf.pt")
OTHER = "OTHER"
MIN_CROPS_PER_CLASS = 3
MIN_VAL_SAMPLES = 10   # ít hơn thế thì số % kiểm tra không có ý nghĩa

_proposal_cache = {}
LABEL_PROPOSAL_THRESHOLD = 0.25  # cao hơn mặc định để ít box thừa khi gán nhãn (box thiếu thì kéo chuột vẽ thêm)


def _state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st):
    os.makedirs(LABEL_DIR, exist_ok=True)
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _key(rel_dir, image_id):
    return f"{rel_dir}|{image_id}"


def list_images(display_root, rel_dir):
    """Danh sách ảnh của 1 chương trình + số box đã gán nhãn mỗi ảnh."""
    prog_dir = os.path.join(display_root, rel_dir)
    t1 = json.load(open(os.path.join(prog_dir, "tier1.json"), encoding="utf-8"))
    st = _state()
    out = []
    for t in t1:
        iid = str(t.get("imageId"))
        out.append({"imageId": iid, "file": t.get("file"), "customer": t.get("customerName", ""),
                    "labeled": len(st.get(_key(rel_dir, iid), []))})
    return out


def propose(display_root, rel_dir, image_id, force=False):
    """Box đề xuất (chuẩn hoá theo file ảnh hiện tại) + dự đoán của classifier nếu đã có model."""
    prog_dir = os.path.join(display_root, rel_dir)
    t1 = json.load(open(os.path.join(prog_dir, "tier1.json"), encoding="utf-8"))
    item = next((t for t in t1 if str(t.get("imageId")) == str(image_id)), None)
    if not item:
        raise FileNotFoundError("Không thấy ảnh")
    path = os.path.join(prog_dir, item["file"])
    mtime = os.path.getmtime(path)
    ck = (path, mtime)
    if not force and ck in _proposal_cache:
        boxes = _proposal_cache[ck]
    else:
        pil = Image.open(path).convert("RGB")
        px, _ = T._propose(pil, LABEL_PROPOSAL_THRESHOLD)
        W, H = pil.size
        boxes = [[round(float(b[0]) / W, 4), round(float(b[1]) / H, 4),
                  round(float(b[2]) / W, 4), round(float(b[3]) / H, 4)] for b in px]
        _proposal_cache[ck] = boxes
    preds = predict_boxes(path, boxes)
    saved = []
    for it in _state().get(_key(rel_dir, image_id), []):   # nhãn cũ (chỉ có box) -> polygon 4 đỉnh
        poly = it.get("poly") or _box_to_poly(it["box"])
        saved.append({"poly": poly, "label": it["label"]})
    return {"boxes": [{"box": b, "pred": p} for b, p in zip(boxes, preds)], "saved": saved}


def _box_to_poly(b):
    return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]


def _poly_bbox(poly):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def _crop(pil, nb, pad=0.03):
    W, H = pil.size
    x1, y1, x2, y2 = nb[0] * W, nb[1] * H, nb[2] * W, nb[3] * H
    px, py = pad * (x2 - x1), pad * (y2 - y1)
    return pil.crop((int(max(0, x1 - px)), int(max(0, y1 - py)), int(min(W, x2 + px)), int(min(H, y2 + py))))


def _crop_masked(pil, poly, pad=0.03):
    """Crop quanh polygon, phần ngoài polygon tô xám trung tính."""
    W, H = pil.size
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon([(x * W, y * H) for x, y in poly], fill=255)
    bg = Image.new("RGB", (W, H), (128, 128, 128))
    bg.paste(pil, (0, 0), mask)
    return _crop(bg, _poly_bbox(poly), pad)


def save_labels(display_root, rel_dir, image_id, items):
    """items: [{poly:[[nx,ny],...], label:<sku_code|OTHER>}] (còn nhận {box:[...]} cũ). Box không có label thì bỏ qua."""
    prog_dir = os.path.join(display_root, rel_dir)
    t1 = json.load(open(os.path.join(prog_dir, "tier1.json"), encoding="utf-8"))
    item = next((t for t in t1 if str(t.get("imageId")) == str(image_id)), None)
    if not item:
        raise FileNotFoundError("Không thấy ảnh")
    pil = Image.open(os.path.join(prog_dir, item["file"])).convert("RGB")
    # xoá crop cũ của ảnh này (gán lại thì thay thế)
    for root in (CROPS_DIR, MASKED_DIR):
        for f in glob.glob(os.path.join(root, "*", f"{image_id}_*.jpg")):
            os.remove(f)
    n, kept = 0, []
    for it in items:
        label = it.get("label")
        poly = it.get("poly") or (_box_to_poly(it["box"]) if it.get("box") else None)
        if not label or not poly or len(poly) < 3:
            continue
        poly = [[round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)] for x, y in poly]
        c = _crop(pil, _poly_bbox(poly))
        if min(c.size) < 12:
            continue
        sub = label.replace("/", "_")
        for root, img in ((CROPS_DIR, c), (MASKED_DIR, _crop_masked(pil, poly))):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
            img.save(os.path.join(root, sub, f"{image_id}_{n}.jpg"), quality=92)
        kept.append({"poly": poly, "label": label})
        n += 1
    st = _state()
    st[_key(rel_dir, image_id)] = kept
    _save_state(st)
    return n


def stats():
    out = {}
    if os.path.isdir(CROPS_DIR):
        for d in sorted(os.listdir(CROPS_DIR)):
            out[d] = len(glob.glob(os.path.join(CROPS_DIR, d, "*.jpg")))
    info = None
    if os.path.exists(CLF_FILE):
        try:
            ck = torch.load(CLF_FILE, map_location="cpu")
            info = {"classes": ck["classes"], "val_acc": ck.get("val_acc"), "n_train": ck.get("n_train")}
        except Exception:
            pass
    return {"counts": out, "model": info}


# --------------------------------------------------------------------------------- classifier
def _load_dataset():
    paths, labels = [], []
    for d in sorted(os.listdir(CROPS_DIR)) if os.path.isdir(CROPS_DIR) else []:
        fs = sorted(glob.glob(os.path.join(CROPS_DIR, d, "*.jpg")))
        if len(fs) >= MIN_CROPS_PER_CLASS:
            paths += fs
            labels += [d] * len(fs)
    return paths, labels


def _embed_paths(paths, flip=False):
    feats = []
    for i in range(0, len(paths), 32):
        ims = [Image.open(p).convert("RGB") for p in paths[i:i + 32]]
        if flip:
            ims = [im.transpose(Image.FLIP_LEFT_RIGHT) for im in ims]
        feats.append(T._embed_images(ims))
    return torch.cat(feats) if feats else torch.zeros(0, 512)


def _fit(X, y, n_cls, steps=400, wd=1e-3):
    W = torch.zeros(X.shape[1], n_cls, requires_grad=True)
    b = torch.zeros(n_cls, requires_grad=True)
    opt = torch.optim.Adam([W, b], lr=0.05)
    # cân bằng lớp
    cnt = torch.bincount(y, minlength=n_cls).float().clamp(min=1)
    cw = (cnt.sum() / (n_cls * cnt))
    for _ in range(steps):
        opt.zero_grad()
        loss = torch.nn.functional.cross_entropy(X @ W * 20 + b, y, weight=cw) + wd * (W ** 2).sum()
        loss.backward()
        opt.step()
    return W.detach(), b.detach()


def train():
    paths, labels = _load_dataset()
    classes = sorted(set(labels))
    if len(classes) < 2:
        raise ValueError("Cần ít nhất 2 lớp (mỗi lớp >= %d crop), ví dụ 1 SKU + OTHER" % MIN_CROPS_PER_CLASS)
    y = torch.tensor([classes.index(l) for l in labels])
    X = _embed_paths(paths)
    Xf = _embed_paths(paths, flip=True)
    # crop đã che nền (nếu có): chỉ dùng để học thêm, KHÔNG dùng để kiểm tra
    mpaths = [p.replace(os.sep + "crops" + os.sep, os.sep + "crops_masked" + os.sep) for p in paths]
    m_idx = [i for i, mp in enumerate(mpaths) if os.path.exists(mp)]
    Xm = _embed_paths([mpaths[i] for i in m_idx]) if m_idx else torch.zeros(0, X.shape[1])
    ym = y[m_idx] if m_idx else y[:0]
    # tập kiểm tra giữ lại (20%), chỉ khi mỗi lớp >= 6 crop
    random.seed(0)
    val_idx = []
    for ci in range(len(classes)):
        idx = [i for i in range(len(y)) if int(y[i]) == ci]
        random.shuffle(idx)
        if len(idx) >= 6:
            val_idx += idx[:max(1, len(idx) // 5)]
    val_acc = None
    per_class = {}
    if len(val_idx) >= MIN_VAL_SAMPLES:
        vs = set(val_idx)
        tr = [i for i in range(len(y)) if i not in vs]
        trs = set(tr)
        mk = [j for j, i in enumerate(m_idx) if i in trs]
        Xtr = torch.cat([X[tr], Xf[tr], Xm[mk]]); ytr = torch.cat([y[tr], y[tr], ym[mk]])
        W, b = _fit(Xtr, ytr, len(classes))
        pred = (X[val_idx] @ W * 20 + b).argmax(1)
        ok = (pred == y[val_idx])
        val_acc = round(float(ok.float().mean()), 3)
        for ci, c in enumerate(classes):
            m = (y[val_idx] == ci)
            if int(m.sum()):
                per_class[c] = {"n": int(m.sum()), "acc": round(float(ok[m].float().mean()), 3)}
    W, b = _fit(torch.cat([X, Xf, Xm]), torch.cat([y, y, ym]), len(classes))
    os.makedirs(os.path.dirname(CLF_FILE), exist_ok=True)
    torch.save({"classes": classes, "W": W, "b": b, "val_acc": val_acc, "n_train": len(y)}, CLF_FILE)
    T.reset_classifier()
    return {"classes": classes, "n": len(y), "val_acc": val_acc, "per_class": per_class}


def predict_boxes(path, boxes):
    """Dự đoán {sku, prob} cho từng box (nếu đã có model)."""
    clf = T.get_classifier()
    if clf is None or not boxes:
        return [None] * len(boxes)
    pil = Image.open(path).convert("RGB")
    ce = T._embed_images([_crop(pil, b) for b in boxes])
    prob = torch.softmax(ce @ clf["W"] * 20 + clf["b"], dim=1)
    out = []
    for i in range(len(boxes)):
        k = int(prob[i].argmax())
        out.append({"sku": clf["classes"][k], "prob": round(float(prob[i, k]), 3)})
    return out
