# dms-display-scorer

Tool AI chấm ảnh trưng bày DMS độc lập. Không code lên app DMS.

## Pipeline
- Tầng 1: metadata + chống gian lận (GPS Haversine ≤100m, timestamp, pHash)
- Tầng 2: YOLO 1-class product → embedding ArcFace → FAISS/Qdrant (TODO)
- Tầng 3: Rule Engine PASS/FAIL (TODO)

## Ingest (done)
```bash
export DMS_USER=tuannm DMS_PASSWORD='...'
python src/dms_ingest_display.py --from 01/10/2026 --to 07/10/2026 --download
python src/dms_ingest_display.py --from 01/11/2026 --to 30/11/2026 --program TRUNGBAYBANHTUOIT102026 --download
```

Endpoints DMS.ONE (cookie CAS):
- `POST /images/search` → list programs trong date-range
- `POST /images/get-images-for-popup` (page từ 0) → records đủ nhất
- Ảnh direct `http://huunghiv2.dmsone.vn:8080/huunghi/<urlImage>` không auth

Output: `<out>/<range>/<programId>/records.json + tier1.json + full/*.jpg`
tier1.json: imageId, file, customer, shop, staff, createDate, lat/lng vs custLat/custLng, gps_dist_m, gps_ok.

## Verified 07/10/2026
- 873: 87/87, 872: 90/90. addAlbumSelect thiếu (67/87) → dùng popup.
- Login xong phải ghé `/home` + `/index.jsp` + 4 GET init, không dính 550.
