#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tier 2 Computer Vision: nhận diện SKU HNF trên ảnh trưng bày DMS (Deep Learning, không heuristic màu).

Pipeline:
1. Proposal  : OWLv2 (open-vocabulary detector) đề xuất box từng gói bánh -> NMS + lọc kích thước.
2. Classify  : CLIP so khớp từng crop với ảnh bao bì mẫu của chương trình (cosine) (max cosine);
               gói khác (phô mai, socola, lá dứa...) có sim thấp hơn ngưỡng nên bị loại.
3. Rotation  : thử 0°, 90° CW, 90° CCW (ảnh DMS hay bị chụp ngang không có EXIF); chọn hướng tốt nhất.
Toạ độ box trả về chuẩn hoá [0..1] theo ẢNH GỐC (đã map ngược khỏi hướng xoay).
"""

import os
import json
import argparse

try:
    import cv2
    import numpy as np
    import torch
    from PIL import Image
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "data", "configs", "programs_config.json")
OWL_NAME = "google/owlv2-base-patch16-ensemble"
CLIP_NAME = "openai/clip-vit-base-patch32"

OWL_QUERIES = ["a plastic bag of bread", "a packaged bread bag on a shelf"]
PROPOSAL_THRESHOLD = 0.12
NMS_IOU = 0.45
MIN_BOX_FRAC = 0.012      # cạnh nhỏ nhất tối thiểu so với cạnh ảnh
MAX_AREA_FRAC = 0.25      # box lớn hơn thế là cả kệ/biển, không phải 1 gói
SIM_MIN = 0.65            # cosine tối thiểu với ảnh mẫu
NEG_MARGIN = 0.0         # sim với SKU phải lớn hơn sim với 'OTHER' ngần này
SIM_MARGIN = 0.05         # phải hơn SKU khác ít nhất ngần này

_models = {}


def _device():
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _load_models():
    if "owl" in _models:
        return _models
    from transformers import Owlv2Processor, Owlv2ForObjectDetection, CLIPModel, CLIPProcessor
    dev = _device()
    _models["owl_p"] = Owlv2Processor.from_pretrained(OWL_NAME)
    # OWLv2 chạy CPU cho ổn định (một số op không hỗ trợ MPS)
    _models["owl"] = Owlv2ForObjectDetection.from_pretrained(OWL_NAME).eval()
    _models["clip_p"] = CLIPProcessor.from_pretrained(CLIP_NAME)
    _models["clip"] = CLIPModel.from_pretrained(CLIP_NAME).to(dev).eval()
    _models["dev"] = dev
    return _models


def _embed_images(pil_list):
    m = _load_models()
    inp = m["clip_p"](images=pil_list, return_tensors="pt").to(m["dev"])
    with torch.no_grad():
        f = m["clip"].get_image_features(**inp)
    return torch.nn.functional.normalize(f, dim=-1).cpu()


def _embed_texts(texts):
    m = _load_models()
    inp = m["clip_p"](text=texts, return_tensors="pt", padding=True).to(m["dev"])
    with torch.no_grad():
        f = m["clip"].get_text_features(**inp)
    return torch.nn.functional.normalize(f, dim=-1).cpu()


def _load_sample(path):
    """Ảnh mẫu PNG trong suốt -> cắt sát bao bì, dán lên nền xám trung tính."""
    im = Image.open(path).convert("RGBA")
    a = np.array(im)[:, :, 3]
    ys, xs = np.where(a > 20)
    if len(xs):
        im = im.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
    bg.alpha_composite(im)
    return bg.convert("RGB")


_proto_cache = {}


def _augment_sample(im, n=14, seed=0):
    """Biến ảnh bao bì render thành các biến thể giống ảnh chụp kệ thật (nhỏ, mờ, lệch sáng/màu, nghiêng nhẹ)."""
    rng = np.random.RandomState(seed)
    from PIL import ImageEnhance, ImageFilter
    outs = [im]
    for _ in range(n):
        v = im.copy()
        w, h = v.size
        sc = rng.uniform(0.12, 0.3) * 600 / max(w, h)
        v = v.resize((max(24, int(w * sc * 1.0)), max(24, int(h * sc * 1.0))), Image.BILINEAR)
        v = v.rotate(rng.uniform(-8, 8), expand=True, fillcolor=(128, 128, 128))
        v = v.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 1.4)))
        v = ImageEnhance.Brightness(v).enhance(rng.uniform(0.7, 1.25))
        v = ImageEnhance.Color(v).enhance(rng.uniform(0.7, 1.2))
        v = ImageEnhance.Contrast(v).enhance(rng.uniform(0.8, 1.15))
        outs.append(v)
    return outs


def _sku_prototypes(sku_cfgs):
    """Trả về {sku_code: tensor [n_mẫu, D]}."""
    out = {}
    for s in sku_cfgs:
        key = (s["sku_code"], tuple(s.get("sample_images", [])))
        if key not in _proto_cache:
            ims = []
            for p in s.get("sample_images", []):
                fp = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
                if os.path.exists(fp):
                    ims.append(_load_sample(fp))
            _proto_cache[key] = _embed_images(ims) if ims else None
        if _proto_cache[key] is not None:
            out[s["sku_code"]] = _proto_cache[key]
    return out


def _load_negatives(sku_cfgs):
    """Ảnh mẫu 'OTHER' (phô mai, socola, bánh cam, lá dứa...) nằm cạnh thư mục SKU: samples/<prog>/OTHER/."""
    dirs = set()
    for s in sku_cfgs:
        for p in s.get("sample_images", []):
            fp = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
            dirs.add(os.path.join(os.path.dirname(os.path.dirname(fp)), "OTHER"))
    ims, key = [], []
    for d in sorted(dirs):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    ims.append(Image.open(os.path.join(d, f)).convert("RGB")); key.append(f)
    key = tuple(key)
    if key not in _proto_cache:
        _proto_cache[key] = _embed_images(ims) if ims else None
    return _proto_cache[key]


def _nms(boxes, scores, thr):
    order = np.argsort(-scores)
    keep = []
    while len(order):
        i = order[0]
        keep.append(i)
        if len(order) == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0]); yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2]); yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        a_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        a_r = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        iou = inter / (a_i + a_r - inter + 1e-9)
        contain = inter / (np.minimum(a_i, a_r) + 1e-9)   # box nhỏ nằm gọn trong box lớn hơn
        order = rest[(iou < thr) & (contain < 0.8)]
    return keep


def _propose(pil):
    """Đề xuất box gói bánh. Trả về mảng [N,4] pixel (x1,y1,x2,y2) + điểm."""
    m = _load_models()
    W, H = pil.size
    inp = m["owl_p"](text=[OWL_QUERIES], images=pil, return_tensors="pt")
    with torch.no_grad():
        out = m["owl"](**inp)
    S = max(W, H)  # OWLv2 pad ảnh thành vuông ở góc trên-trái
    res = m["owl_p"].post_process_object_detection(
        out, threshold=PROPOSAL_THRESHOLD, target_sizes=torch.tensor([[S, S]]))[0]
    boxes = res["boxes"].numpy().astype(np.float32)
    scores = res["scores"].numpy().astype(np.float32)
    if not len(boxes):
        return boxes.reshape(0, 4), scores
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, W)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, H)
    bw = boxes[:, 2] - boxes[:, 0]; bh = boxes[:, 3] - boxes[:, 1]
    ok = (bw > MIN_BOX_FRAC * S) & (bh > MIN_BOX_FRAC * S) & (bw * bh < MAX_AREA_FRAC * W * H)
    boxes, scores = boxes[ok], scores[ok]
    if not len(boxes):
        return boxes, scores
    keep = _nms(boxes, scores, NMS_IOU)
    return boxes[keep], scores[keep]


def _classify(pil, boxes, protos, sku_names, negs=None):
    """Gán SKU cho từng box. Trả về list dict(box, sku, sim, margin, is_match)."""
    if not len(boxes):
        return []
    W, H = pil.size
    crops = []
    for x1, y1, x2, y2 in boxes:
        px, py = 0.03 * (x2 - x1), 0.03 * (y2 - y1)
        crops.append(pil.crop((max(0, x1 - px), max(0, y1 - py), min(W, x2 + px), min(H, y2 + py))))
    ce = _embed_images(crops)                                  # [N, D]
    codes = list(protos.keys())
    sku_sim = torch.stack([(ce @ protos[c].T).max(dim=1).values for c in codes], dim=1)  # [N, K]
    neg_sim = (ce @ negs.T).max(dim=1).values if negs is not None else torch.full((len(boxes),), -1.0)
    results = []
    for i in range(len(boxes)):
        k = int(sku_sim[i].argmax())
        sim = float(sku_sim[i, k])
        second = float(torch.cat([sku_sim[i, :k], sku_sim[i, k + 1:]]).max()) if len(codes) > 1 else -1.0
        results.append({
            "box": [float(v) for v in boxes[i]],
            "sku": codes[k], "sim": sim, "margin": sim - second,
            "neg": float(neg_sim[i]),
            "is_match": sim >= SIM_MIN and (sim - second) >= SIM_MARGIN and sim > float(neg_sim[i]) + NEG_MARGIN,
        })
    return results


def _drop_merged(hits):
    """Bỏ box gộp nhiều gói: diện tích > 2.2 lần trung vị diện tích cùng SKU."""
    out = []
    for sku in {h["sku"] for h in hits}:
        grp = [h for h in hits if h["sku"] == sku]
        areas = [(h["box"][2] - h["box"][0]) * (h["box"][3] - h["box"][1]) for h in grp]
        med = float(np.median(areas))
        out += [h for h, a in zip(grp, areas) if len(grp) < 3 or a <= 2.2 * med]
    return out


def _rotate_pil(pil, rot):
    if rot == 0:
        return pil
    return pil.rotate(-90 if rot == 90 else 90, expand=True)  # 90: xoay CW, 270: xoay CCW


def _unrotate_box(b, rot, W, H):
    """Box trong ảnh đã xoay -> toạ độ chuẩn hoá trong ảnh gốc (W x H)."""
    x1, y1, x2, y2 = b
    if rot == 0:
        ox1, oy1, ox2, oy2 = x1, y1, x2, y2
    elif rot == 90:   # xoay CW: (x', y') = (H - y, x)  =>  x = y', y = H - x'
        ox1, oy1, ox2, oy2 = y1, H - x2, y2, H - x1
    else:             # xoay CCW: (x', y') = (y, W - x)  =>  x = W - y', y = x'
        ox1, oy1, ox2, oy2 = W - y2, x1, W - y1, x2
    return [round(ox1 / W, 4), round(oy1 / H, 4), round(ox2 / W, 4), round(oy2 / H, 4)]


def _empty(err=None):
    r = {"detected_skus": {}, "unique_skus_count": 0, "total_facings": 0, "has_products": False}
    if err:
        r["error"] = err
    return r


def _guess_program_config(img_path):
    """Suy ra cấu hình chương trình từ đường dẫn ảnh (thư mục chứa id chương trình)."""
    try:
        cfgs = json.load(open(CONFIG_FILE, encoding="utf-8"))
    except Exception:
        return None
    for p in reversed(os.path.abspath(img_path).split(os.sep)):
        if p in cfgs:
            return cfgs[p]
    return None


def detect_products_in_image(img_path, program_config=None):
    """Phát hiện SKU trong 1 ảnh. Giữ nguyên schema cũ để app.py / tier3_rules dùng tiếp."""
    if not HAVE_DEPS:
        return _empty("Thiếu thư viện (torch/transformers/opencv/pillow)")
    try:
        pil0 = Image.open(img_path).convert("RGB")
    except Exception:
        return _empty("Cannot load image")
    program_config = program_config or _guess_program_config(img_path)
    skus = (program_config or {}).get("skus", [])
    if not skus:
        return _empty("Chưa cấu hình SKU/ảnh mẫu cho chương trình này")
    protos = _sku_prototypes(skus)
    if not protos:
        return _empty("Chưa có ảnh bao bì mẫu cho các SKU")
    negs = _load_negatives(skus)
    names = {s["sku_code"]: s.get("sku_name", s["sku_code"]) for s in skus}

    W, H = pil0.size
    best = None
    for rot in (0, 90, 270):
        pil = _rotate_pil(pil0, rot)
        boxes, _ = _propose(pil)
        hits = _drop_merged([c for c in _classify(pil, boxes, protos, names, negs) if c["is_match"]])
        score = sum(c["sim"] for c in hits)
        if best is None or score > best[0]:
            best = (score, rot, hits)
        if rot == 0 and len(hits) >= 3:   # ảnh đứng đã nhận đủ rõ, khỏi thử xoay
            break
    _, rot, hits = best

    detected = {}
    for c in hits:
        d = detected.setdefault(c["sku"], {
            "sku_code": c["sku"], "sku_name": names.get(c["sku"], c["sku"]),
            "facings": 0, "confidence": 0.0, "bboxes": []})
        d["bboxes"].append({"box": _unrotate_box(c["box"], rot, W, H), "confidence": round(min(0.99, c["sim"]), 3)})
        d["facings"] += 1
    for d in detected.values():
        d["confidence"] = round(sum(b["confidence"] for b in d["bboxes"]) / d["facings"], 3)

    total = sum(d["facings"] for d in detected.values())
    return {"detected_skus": detected, "unique_skus_count": len(detected),
            "total_facings": total, "has_products": total > 0, "rotation_used": rot}


def process_program_directory(prog_dir, config_file=None):
    """Quét toàn bộ ảnh trong thư mục chương trình và tạo file tier2.json."""
    tier1_file = os.path.join(prog_dir, "tier1.json")
    if not os.path.exists(tier1_file):
        raise FileNotFoundError(f"Không tìm thấy file {tier1_file}")
    tier1 = json.load(open(tier1_file, encoding="utf-8"))
    cfg = _guess_program_config(os.path.join(prog_dir, "x"))
    out_results = []
    print(f"🚀 Bắt đầu chấm AI Tier 2 cho {len(tier1)} ảnh trong {prog_dir}...")
    for idx, t in enumerate(tier1, 1):
        rel_file = t.get("file", "")
        full = os.path.join(prog_dir, rel_file)
        res = detect_products_in_image(full, cfg) if os.path.exists(full) else _empty("File not found")
        res["imageId"] = str(t.get("imageId"))
        res["file"] = rel_file
        out_results.append(res)
        if idx % 10 == 0 or idx == len(tier1):
            print(f"   [{idx}/{len(tier1)}] {rel_file} -> {res['unique_skus_count']} SKU, {res['total_facings']} facings")
    tier2_file = os.path.join(prog_dir, "tier2.json")
    with open(tier2_file, "w", encoding="utf-8") as f:
        json.dump(out_results, f, ensure_ascii=False, indent=2)
    print(f"✅ Đã lưu kết quả Tier 2 vào: {tier2_file}")
    return out_results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tier 2 AI Product Detection for HNF Displays")
    ap.add_argument("--dir", help="Thư mục chương trình (chứa tier1.json)")
    ap.add_argument("--image", help="Chạy thử 1 ảnh, in kết quả JSON")
    a = ap.parse_args()
    if a.image:
        print(json.dumps(detect_products_in_image(a.image), ensure_ascii=False, indent=2))
    elif a.dir:
        process_program_directory(a.dir)
    else:
        ap.error("cần --dir hoặc --image")
