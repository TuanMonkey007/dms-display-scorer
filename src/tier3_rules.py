#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tier 3 Flexible Rule Engine: Cơ chế Đánh giá & Chấm điểm "Du Di"
Chuyên biệt cho Hữu Nghị Food (HNF) trên hệ thống DMS.ONE.

Quy tắc:
1. Đọc cấu hình từ programs_config.json
2. Đánh giá số lượng SKU đạt chuẩn so với ngưỡng min_skus_required (cho phép du di, ví dụ 1/2 hoặc 7/10)
3. Kiểm tra các SKU bắt buộc (is_mandatory)
4. Tổng hợp quyết định Tầng 1 (GPS, Moiré, Chống trùng chéo) + Tầng 2 (CV Nhận diện mặt sản phẩm)
   thành Kết luận cuối cùng:
   - "HOP_LE" (Hợp lệ / Duyệt trả thưởng)
   - "NGHI_VAN" (Cần hậu kiểm: GPS xa hoặc nghi vấn Moiré)
   - "LOAI" (Từ chối: Trùng ảnh đa điểm bán hoặc không đạt yêu cầu trưng bày)
"""

import os
import json

def load_programs_config(config_path=None):
    """Nạp file cấu hình chương trình và quy tắc chấm."""
    if not config_path:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "data", "configs", "programs_config.json")
    
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def evaluate_display_record(tier1_item, tier2_item, program_config):
    """
    Đánh giá 1 bản ghi ảnh trưng bày dựa trên kết quả Tier 1 + Tier 2 + Cấu hình chương trình.
    """
    if not program_config:
        # Cấu hình mặc định nếu chưa cấu hình chương trình
        program_config = {
            "min_skus_required": 1,
            "total_skus": 2,
            "allow_flexibility": True,
            "skus": [
                {"sku_code": "STAFF_CHABONG_60G", "sku_name": "Bánh mì Chà Bông Staff 60g", "min_facings": 1, "is_mandatory": False},
                {"sku_code": "STAFF_SANDWICH_275G", "sku_name": "Bánh mì Sandwich Staff 275g", "min_facings": 1, "is_mandatory": False}
            ]
        }

    cfg_skus = program_config.get("skus", [])
    min_skus_req = int(program_config.get("min_skus_required", 1))
    total_skus_cfg = int(program_config.get("total_skus", len(cfg_skus) or 2))
    
    detected_map = (tier2_item or {}).get("detected_skus", {})
    
    passed_skus = []
    missing_mandatory = []
    sku_details = []

    for s in cfg_skus:
        code = s.get("sku_code")
        name = s.get("sku_name", code)
        min_f = int(s.get("min_facings", 1))
        is_mand = bool(s.get("is_mandatory", False))

        det = detected_map.get(code)
        facings = det.get("facings", 0) if det else 0
        conf = det.get("confidence", 0.0) if det else 0.0

        is_passed = (facings >= min_f)
        if is_passed:
            passed_skus.append(code)
        elif is_mand:
            missing_mandatory.append(name)

        sku_details.append({
            "sku_code": code,
            "sku_name": name,
            "facings": facings,
            "min_facings": min_f,
            "is_mandatory": is_mand,
            "confidence": conf,
            "passed": is_passed
        })

    # Đánh giá Tầng 2 (CV Trưng bày)
    unique_passed_count = len(passed_skus)
    total_detected_facings = sum(s["facings"] for s in sku_details)
    
    if missing_mandatory:
        t2_status = "KHONG_DAT"
        t2_reason = f"Thiếu SKU bắt buộc: {', '.join(missing_mandatory)}"
    elif unique_passed_count >= min_skus_req:
        if unique_passed_count >= total_skus_cfg:
            t2_status = "DAT_CHUAN"
            t2_reason = f"Đạt chuẩn {unique_passed_count}/{total_skus_cfg} SKU ({total_detected_facings} mặt trưng bày)"
        else:
            t2_status = "DAT_DU_DI"
            t2_reason = f"Đạt du di {unique_passed_count}/{total_skus_cfg} SKU (Quy định tối thiểu {min_skus_req} SKU)"
    else:
        t2_status = "KHONG_DAT"
        t2_reason = f"Không đạt trưng bày: Chỉ có {unique_passed_count}/{min_skus_req} SKU tối thiểu"

    # Đánh giá Tổng thể (Tầng 1 + Tầng 2)
    t1 = tier1_item or {}
    is_cross_dup = bool(t1.get("is_cross_store_dup", False))
    recap_suspect = bool(t1.get("recap_suspect", False))
    gps_dist = t1.get("gps_dist_m")
    
    # Logic phân loại tổng thể:
    if is_cross_dup:
        overall_status = "LOAI"
        dup_target = t1.get("dup_target", {})
        dup_cust = dup_target.get("customerCode", "khác")
        overall_reason = f"Từ chối: Trùng lặp ảnh với điểm bán {dup_cust}"
    elif recap_suspect:
        overall_status = "NGHI_VAN"
        overall_reason = "Nghi vấn gian lận: Dấu hiệu chụp lại màn hình (Moiré)"
    elif t2_status == "KHONG_DAT":
        overall_status = "LOAI"
        overall_reason = f"Từ chối: {t2_reason}"
    elif gps_dist is not None and gps_dist > 200:
        overall_status = "NGHI_VAN"
        overall_reason = f"Cảnh báo: GPS chụp cách điểm bán {int(gps_dist)}m (> 200m)"
    elif gps_dist is not None and gps_dist > 100:
        overall_status = "NGHI_VAN"
        overall_reason = f"Lưu ý: GPS lệch {int(gps_dist)}m (100m - 200m)"
    elif t2_status == "DAT_DU_DI":
        overall_status = "HOP_LE"
        overall_reason = f"Hợp lệ: {t2_reason}"
    else:
        overall_status = "HOP_LE"
        overall_reason = "Hợp lệ: Đạt chuẩn 100% (GPS & Trưng bày đều tốt)"

    return {
        "tier2_status": t2_status,
        "tier2_reason": t2_reason,
        "unique_passed_count": unique_passed_count,
        "total_skus_cfg": total_skus_cfg,
        "total_facings": total_detected_facings,
        "sku_details": sku_details,
        "overall_status": overall_status,
        "overall_reason": overall_reason
    }
