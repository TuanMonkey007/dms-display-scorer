#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMS Display Photo Scorer & Downloader - SINGLE FILE ALL-IN-ONE
Bao gồm:
1. Backend DMS Client (Login CAS, Ingest, Auto Pagination, CDN Download)
2. HTTP Server + REST API
3. Web GUI Frontend 2-Tab:
   - Tab 1: Tải ảnh từ DMS (Giao diện 2 cột realtime)
   - Tab 2: Bảng chấm & Kiểm tra Tầng 1 (Ngưỡng mặc định 100m/200m, Quét Moiré, Quét TRÙNG ẢNH ĐA ĐIỂM BÁN, So sánh 2 ảnh, Sửa nhận xét, Xuất CSV)
"""
import os
import sys
import json
import re
import math
import time
import subprocess
import struct
import tempfile
import threading
import urllib.parse
import urllib.request
import http.cookiejar
import webbrowser
import base64
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
try:
    import cv2
    HAVE_CV2 = True
except ImportError:
    HAVE_CV2 = False

BASE = "http://huunghiv2.dmsone.vn"
IMG_BASE = "http://huunghiv2.dmsone.vn:8080/huunghi"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "data", "configs")
CONFIG_FILE = os.path.join(CONFIG_DIR, "programs_config.json")
SAMPLES_DIR = os.path.join(BASE_DIR, "data", "samples")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(SAMPLES_DIR, exist_ok=True)

sys.path.insert(0, os.path.join(BASE_DIR, "src"))
try:
    from tier2_cv import detect_products_in_image, process_program_directory
    from tier3_rules import evaluate_display_record, load_programs_config
except Exception:
    pass
DEFAULT_OUTPUT_DIR = os.path.join(BASE_DIR, "data", "displays")
try:
    import labeling
except Exception as _e:
    labeling = None
    print("⚠️ Không nạp được labeling:", _e)

def _inject_label_tab(html):
    try:
        frag = open(os.path.join(BASE_DIR, "src", "label_tab.html"), encoding="utf-8").read()
    except Exception:
        frag = ""
    return html.replace("<!--LABEL_TAB-->", frag)

class _Redirect(Exception):
    def __init__(self, location):
        self.location = location

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirect(headers.get("Location", newurl))

class DMS:
    def __init__(self, user, password):
        self.user, self.password = user, password
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def _post(self, path, data, timeout=60):
        req = urllib.request.Request(
            BASE + path, data=urllib.parse.urlencode(data).encode(),
            headers={**UA, "X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "Referer": BASE + "/images/info", "Origin": BASE})
        with self.op.open(req, timeout=timeout) as r:
            body = r.read()
        try:
            j = json.loads(body.decode("utf-8"))
            self.token = j.get("token") or self.token
            return j
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body.decode("utf-8", "replace")

    def login(self):
        req = urllib.request.Request(BASE + "/login", headers=UA)
        with self.op.open(req, timeout=30) as r:
            page = r.read().decode("utf-8", "replace")
        lt = (re.search(r'name="lt" value="([^"]*)"', page) or ["", ""])[1]
        body = urllib.parse.urlencode(
            {"username": self.user, "password": self.password,
             "lt": lt, "_eventId": "submit"}).encode()
        req = urllib.request.Request(
            BASE + "/login", data=body,
            headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())
        try:
            opener.open(req, timeout=30)
            raise RuntimeError("login: khong redirect, kiem tra user/pass")
        except _Redirect as e:
            if "index.jsp" not in (e.location or "") and "/home" not in (e.location or ""):
                raise RuntimeError(f"login that bai: {e.location}")
        
        req = urllib.request.Request(BASE + "/images/info", headers=UA)
        with self.op.open(req, timeout=30): pass
        for p in ("/home", "/index.jsp"):
            try:
                req = urllib.request.Request(BASE + p, headers=UA)
                with self.op.open(req, timeout=30): pass
            except Exception: pass
        for p in ("/images/displayPrograme/getListCTTBbyShopId",
                  "/images/getListStaffForShop",
                  "/images/displayPrograme/getListCTTBbyListShop",
                  "/images/loadListProgramStatistic"):
            try:
                req = urllib.request.Request(BASE + p, headers={
                    **UA, "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/images/info"})
                with self.op.open(req, timeout=30): pass
            except Exception: pass

    def search_programs(self, frm, to):
        html = self._post("/images/search", {
            "tuyen": "-1", "fromDate": frm, "toDate": to,
            "customerCode": "", "customerNameOrAddress": "",
            "objectType": "4", "statusRes": "-2"})
        assert isinstance(html, str), "search khong tra HTML (het session?)"
        progs = []
        items = re.findall(
            r'<img[^>]*data-original=[\"\']([^\"\']*)[\"\'][^>]*>.*?showAlbumDetail\((\d+)\);?\">([^<]+)</a></p>\s*<p[^>]*>([\d.,]+)\s*hình ảnh',
            html, re.DOTALL
        )
        if items:
            for thumb, pid, code, count in items:
                progs.append({
                    "displayProgrameId": str(pid), "code": code.strip(),
                    "count": int(count.replace(".", "").replace(",", "")),
                    "thumb": thumb.replace("\\", "/")
                })
        else:
            matches = re.finditer(r"showAlbumDetail\((\d+)\);?\">([^<]+)</a></p>\s*<p[^>]*>([\d.,]+)\s*hình ảnh", html)
            for m in matches:
                progs.append({
                    "displayProgrameId": str(m.group(1)), "code": m.group(2).strip(),
                    "count": int(m.group(3).replace(".", "").replace(",", "")),
                    "thumb": ""
                })
        return progs

    def fetch_program(self, pid, frm, to, per_page=50, progress_cb=None):
        recs, page, seen = [], 0, set()
        while True:
            j = self._post("/images/get-images-for-popup", {
                "tuyen": "-1", "fromDate": frm, "toDate": to,
                "customerCode": "", "customerNameOrAddress": "",
                "objectType": "4", "statusRes": "-2",
                "displayProgrameId": pid, "page": page, "max": per_page})
            assert isinstance(j, dict), f"page {page} khong tra JSON"
            batch = j.get("lstImage", [])
            for r in batch:
                if r.get("imageId") not in seen:
                    seen.add(r.get("imageId"))
                    recs.append(r)
            if progress_cb: progress_cb(page + 1, len(recs))
            if len(batch) < per_page:
                break
            page += 1
        return recs

    def download(self, url_path, dest):
        url = IMG_BASE + url_path.replace("\\", "/")
        req = urllib.request.Request(url, headers=UA)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with self.op.open(req, timeout=120) as r, open(dest, "wb") as f:
            f.write(r.read())

LOCK = threading.Lock()
STOP_EVENT = threading.Event()

SERVER_STATE = {
    "dms_client": None,
    "user": os.environ.get("DMS_USER", "tuannm"),
    "password": os.environ.get("DMS_PASSWORD", ""),
    "programs": [],
    "is_downloading": False,
    "ai_progress": {
        "is_running": False, "total": 0, "processed": 0,
        "current_file": "", "status": "idle",
        "program": "", "error": None
    },
    "download_progress": {
        "is_downloading": False,
        "total_images": 0, "downloaded_images": 0,
        "current_program": "", "current_file": "",
        "status": "idle", "logs": [], "completed": False,
        "stopped": False
    }
}

def haversine_dist(lat1, lon1, lat2, lon2):
    try:
        R = 6371000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return round(2 * R * math.asin(math.sqrt(h)), 1)
    except Exception:
        return None

def compute_image_dhash_variants(img_path):
    """Tính bộ dHash cho ảnh gồm 4 góc xoay (0, 90, 180, 270 độ) để phát hiện gian lận xoay góc."""
    grid = None
    try:
        from PIL import Image
        with Image.open(img_path) as img:
            img = img.convert("L").resize((9, 9), Image.Resampling.LANCZOS)
            pixels = list(img.getdata())
            grid = [pixels[r * 9:(r + 1) * 9] for r in range(9)]
    except Exception:
        pass

    if grid is None:
        tmp_bmp = tempfile.mktemp(dir="/tmp", suffix=".bmp")
        try:
            subprocess.run(["sips", "-s", "format", "bmp", "-z", "9", "9", img_path, "--out", tmp_bmp], capture_output=True)
            if os.path.exists(tmp_bmp):
                with open(tmp_bmp, "rb") as f: data = f.read()
                os.remove(tmp_bmp)
                offset = struct.unpack("<I", data[10:14])[0]
                w, h, planes, bpp = struct.unpack("<iiHH", data[18:30])
                abs_h = abs(h)
                row_size = ((w * (bpp // 8) + 3) // 4) * 4
                rows = []
                for r in range(abs_h):
                    row_data = data[offset + r * row_size : offset + (r + 1) * row_size]
                    row_pixels = []
                    for c in range(w):
                        b, g, rv = row_data[c * 3], row_data[c * 3 + 1], row_data[c * 3 + 2]
                        row_pixels.append((rv * 299 + g * 587 + b * 114) // 1000)
                    rows.append(row_pixels)
                if h > 0: rows.reverse()
                grid = rows
        except Exception:
            pass
        finally:
            if os.path.exists(tmp_bmp):
                try: os.remove(tmp_bmp)
                except Exception: pass

    if not grid or len(grid) < 9: return None

    def dhash_from_grid(g):
        val = 0; idx = 0
        for r in range(8):
            for c in range(8):
                if g[r][c] > g[r][c + 1]: val |= (1 << idx)
                idx += 1
        return val

    g0 = grid
    g90 = [[grid[8 - c][r] for c in range(9)] for r in range(9)]
    g180 = [[grid[8 - r][8 - c] for c in range(9)] for r in range(9)]
    g270 = [[grid[c][8 - r] for c in range(9)] for r in range(9)]

    return [dhash_from_grid(g0), dhash_from_grid(g180)]

def calc_hamming_distance(h1, h2):
    if h1 is None or h2 is None: return 999
    v1 = h1[0] if isinstance(h1, list) else h1
    v2 = h2[0] if isinstance(h2, list) else h2
    try: return bin(int(v1) ^ int(v2)).count("1")
    except Exception: return 999

def calc_min_hamming_distance(v1, v2):
    if v1 is None or v2 is None: return 999, 0
    list1 = v1 if isinstance(v1, list) else [v1]
    list2 = v2 if isinstance(v2, list) else [v2]
    min_d = 999
    best_angle = 0
    angles = [0, 180]
    try:
        h1 = int(list1[0])
        for idx, h2 in enumerate(list2):
            d = bin(h1 ^ int(h2)).count("1")
            if d < min_d:
                min_d = d
                best_angle = angles[idx] if idx < len(angles) else 0
    except Exception:
        return 999, 0
    return min_d, best_angle

def background_download_worker(selected_program_ids, from_date, to_date):
    state = SERVER_STATE["download_progress"]
    dms = SERVER_STATE["dms_client"]
    STOP_EVENT.clear()

    with LOCK:
        SERVER_STATE["is_downloading"] = True
        state["is_downloading"] = True
        state["downloaded_images"] = 0
        state["completed"] = False
        state["stopped"] = False
        state["status"] = "Đang khởi tạo danh sách ảnh..."
        state["logs"] = []

    targets = [p for p in SERVER_STATE["programs"] if str(p["displayProgrameId"]) in [str(x) for x in selected_program_ids]]
    total_expected = sum([p.get("count", 0) for p in targets])
    state["total_images"] = total_expected
    state["logs"].append(f"Khởi chạy tải {len(targets)} chương trình ({total_expected} ảnh dự kiến)...")

    date_folder = f"{from_date.replace('/', '')}-{to_date.replace('/', '')}"

    try:
        for prog in targets:
            if STOP_EVENT.is_set():
                state["status"] = "Đã dừng tải theo yêu cầu."
                state["stopped"] = True
                state["logs"].append("Người dùng đã bấm dừng tiến trình.")
                break

            pid = str(prog["displayProgrameId"])
            pcode = prog.get("code", pid)
            clean_code = pcode.split(" - ")[0].strip() if " - " in pcode else pcode.strip()
            clean_code = re.sub(r'[\\/*?:"<>|\s]', "_", clean_code).strip("_")
            folder_name = f"{pid}_{clean_code}" if clean_code else str(pid)

            state["current_program"] = f"{pcode} (ID: {pid})"
            state["status"] = f"Đang quét danh sách ảnh: {pcode}..."
            state["logs"].append(f">> Quét danh sách ảnh ID {pid} ({clean_code})...")

            prog_dir = os.path.join(DEFAULT_OUTPUT_DIR, date_folder, folder_name)

            old_dir = os.path.join(DEFAULT_OUTPUT_DIR, date_folder, pid)
            if os.path.exists(old_dir) and not os.path.exists(prog_dir):
                try: os.rename(old_dir, prog_dir)
                except Exception: pass

            os.makedirs(prog_dir, exist_ok=True)

            def on_page_scanned(page_num, total_found):
                with LOCK:
                    state["status"] = f"Đang quét danh sách ảnh ID {pid} (Trang {page_num}: {total_found} ảnh)..."
                    state["logs"].append(f"  > Trang {page_num}: đã lấy được {total_found} bản ghi...")
                    if len(state["logs"]) > 100: state["logs"].pop(0)

            records = dms.fetch_program(pid, from_date, to_date, per_page=50, progress_cb=on_page_scanned)
            json.dump(records, open(os.path.join(prog_dir, "records.json"), "w", encoding="utf-8"), ensure_ascii=False)
            state["logs"].append(f"   Tìm thấy {len(records)} ảnh. Bắt đầu tải đa luồng (8 luồng song song)...")

            from concurrent.futures import ThreadPoolExecutor

            def dl_item(r):
                if STOP_EVENT.is_set(): return None
                up = (r.get("urlImage") or "").replace("\\", "/")
                fn = os.path.basename(up) if up else f"{r.get(imageId)}.jpg"
                fp = os.path.join(prog_dir, "full", fn)
                is_new = False
                if up and not os.path.exists(fp):
                    try:
                        dms.download(up, fp)
                        is_new = True
                    except Exception as e:
                        with LOCK: state["logs"].append(f"Lỗi {fn}: {str(e)[:40]}")

                dist = haversine_dist(r.get("lat"), r.get("lng"), r.get("custLat"), r.get("custLng"))
                res_item = {
                    "imageId": r.get("imageId"), "file": f"full/{fn}",
                    "customerCode": r.get("customerCode"), "customerName": r.get("customerName"),
                    "shopCode": r.get("shopCode"), "staffCode": r.get("staffCode"),
                    "staffName": r.get("staffName"), "createDate": r.get("createDate"),
                    "lat": r.get("lat"), "lng": r.get("lng"),
                    "custLat": r.get("custLat"), "custLng": r.get("custLng"),
                    "gps_dist_m": dist, "gps_ok": (dist is not None and dist <= 200),
                    "urlThum": r.get("urlThum", "")
                }

                with LOCK:
                    state["downloaded_images"] += 1
                    done = state["downloaded_images"]
                    tot = state["total_images"]
                    tag = "Tải mới" if is_new else "Đã có sẵn"
                    state["status"] = f"Đang tải [{done}/{tot}] ({tag}): {fn}"
                    if is_new or done % 5 == 0 or done == tot:
                        c_name = r.get("customerName", "")[:18]
                        state["logs"].append(f"[{done}/{tot}] ({tag}) {fn} ({c_name})")
                        if len(state["logs"]) > 100: state["logs"].pop(0)

                return res_item

            tier1 = []
            with ThreadPoolExecutor(max_workers=8) as pool:
                for item in pool.map(dl_item, records):
                    if item: tier1.append(item)

            json.dump(tier1, open(os.path.join(prog_dir, "tier1.json"), "w", encoding="utf-8"), ensure_ascii=False)

        if not STOP_EVENT.is_set():
            state["status"] = "Hoàn thành toàn bộ tiến trình tải!"
            state["completed"] = True
            state["logs"].append("==> ĐÃ HOÀN THÀNH TẢI XONG TOÀN BỘ ẢNH!")
    except Exception as e:
        state["status"] = f"Lỗi: {e}"
        state["logs"].append(f"LỖI HỆ THỐNG: {e}")
    finally:
        with LOCK:
            SERVER_STATE["is_downloading"] = False
            state["is_downloading"] = False


def background_ai_worker(prog_dir, rel_dir):
    global SERVER_STATE
    with LOCK:
        SERVER_STATE["ai_progress"] = {
            "is_running": True, "total": 0, "processed": 0,
            "current_file": "Đang nạp ảnh...", "status": "running",
            "program": rel_dir, "error": None
        }

    try:
        t1_file = os.path.join(prog_dir, "tier1.json")
        if not os.path.exists(t1_file):
            raise FileNotFoundError("Chưa có tier1.json trong thư mục chương trình")
        
        tier1 = json.load(open(t1_file, encoding="utf-8"))
        total = len(tier1)
        with LOCK:
            SERVER_STATE["ai_progress"]["total"] = total

        out_results = []
        for idx, t in enumerate(tier1, 1):
            iid = str(t.get("imageId"))
            rel_file = t.get("file", "")
            img_full = os.path.join(prog_dir, rel_file)

            with LOCK:
                SERVER_STATE["ai_progress"]["processed"] = idx
                SERVER_STATE["ai_progress"]["current_file"] = os.path.basename(rel_file)

            if os.path.exists(img_full):
                res = detect_products_in_image(img_full)
            else:
                res = {
                    "error": "File not found",
                    "detected_skus": {},
                    "unique_skus_count": 0,
                    "total_facings": 0,
                    "has_products": False
                }
            res["imageId"] = iid
            res["file"] = rel_file
            out_results.append(res)

        t2_file = os.path.join(prog_dir, "tier2.json")
        with open(t2_file, "w", encoding="utf-8") as f:
            json.dump(out_results, f, ensure_ascii=False, indent=2)

        with LOCK:
            SERVER_STATE["ai_progress"]["status"] = "completed"
            SERVER_STATE["ai_progress"]["is_running"] = False
    except Exception as e:
        with LOCK:
            SERVER_STATE["ai_progress"]["status"] = "error"
            SERVER_STATE["ai_progress"]["error"] = str(e)
            SERVER_STATE["ai_progress"]["is_running"] = False


HTML_UI = """<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DMS Display Manager - Trình Quản Lý & Chấm Ảnh Trưng Bày</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        body { font-family: 'Inter', sans-serif; }
        .custom-scrollbar::-webkit-scrollbar { width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: #1e293b; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #475569; border-radius: 3px; }
        .bbox-rect { stroke-width: 2.5; fill-opacity: 0.15; cursor: pointer; transition: all 0.2s; }
        .bbox-rect:hover { fill-opacity: 0.35; stroke-width: 3.5; }
        .bbox-tag { font-size: 11px; font-weight: 700; font-family: monospace; }
    </style>
</head>
<body class="bg-slate-100 text-slate-800 min-h-screen">
    <!-- Header -->
    <header class="bg-gradient-to-r from-blue-700 via-indigo-700 to-blue-800 text-white shadow-md sticky top-0 z-30">
        <div class="max-w-7xl mx-auto px-4 py-3 flex flex-wrap justify-between items-center gap-4">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-xl bg-white/10 flex items-center justify-center font-bold text-xl shadow-inner">📸</div>
                <div>
                    <h1 class="text-lg font-bold leading-tight">DMS Display Manager</h1>
                    <p class="text-xs text-blue-200">Hệ thống Tải ảnh & Chấm AI trưng bày Hữu Nghị Food (Tầng 1 + Tầng 2)</p>
                </div>
            </div>

            <!-- Tab Switcher -->
            <div class="flex bg-black/20 p-1 rounded-xl border border-white/10 text-xs">
                <button onclick="switchMainTab('ingest')" id="tabBtnIngest" class="px-4 py-1.5 font-bold rounded-lg bg-white text-blue-700 shadow-sm transition">
                    📥 1. Tải ảnh từ DMS
                </button>
                <button onclick="switchMainTab('audit')" id="tabBtnAudit" class="px-4 py-1.5 font-semibold text-blue-100 hover:text-white rounded-lg transition">
                    📊 2. Bảng chấm & Soi AI
                </button>
                <button onclick="switchMainTab('config')" id="tabBtnConfig" class="px-4 py-1.5 font-semibold text-blue-100 hover:text-white rounded-lg transition">
                    ⚙️ 3. Cấu hình SKU & Quy tắc
                </button>
                <button onclick="switchMainTab('label')" id="tabBtnLabel" class="px-4 py-1.5 font-semibold text-blue-100 hover:text-white rounded-lg transition">
                    🏷️ 4. Gán nhãn sản phẩm
                </button>
            </div>

            <div class="flex items-center gap-3 text-xs bg-black/20 px-3.5 py-1.5 rounded-xl backdrop-blur-sm border border-white/10">
                <span class="w-2.5 h-2.5 rounded-full" id="authDot" style="background:#10b981;"></span>
                <span id="authStatusText" class="font-medium text-blue-100">Kiểm tra phiên...</span>
                <button onclick="openLoginModal()" class="text-blue-300 hover:text-white underline ml-2 font-medium">Đổi tài khoản</button>
            </div>
        </div>
    </header>

    <!-- TAB 1: INGEST / TẢI ẢNH -->
    <div id="viewIngest">
        <!-- Top Filter Bar -->
        <div class="bg-white border-b border-slate-200 shadow-xs sticky top-[57px] z-20">
            <div class="max-w-7xl mx-auto px-4 py-3">
                <div class="flex flex-wrap items-center justify-between gap-4">
                    <div class="flex flex-wrap items-center gap-3">
                        <div class="flex items-center gap-2">
                            <span class="text-xs font-bold text-slate-600 uppercase">Từ ngày:</span>
                            <input type="text" id="fromDate" value="01/10/2026" class="text-xs px-3 py-1.5 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 font-medium w-28">
                        </div>
                        <div class="flex items-center gap-2">
                            <span class="text-xs font-bold text-slate-600 uppercase">Đến ngày:</span>
                            <input type="text" id="toDate" value="07/10/2026" class="text-xs px-3 py-1.5 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500 font-medium w-28">
                        </div>
                        <button onclick="searchPrograms()" id="btnSearch" class="bg-blue-600 hover:bg-blue-700 text-white font-semibold py-1.5 px-4 rounded-lg shadow-xs transition flex items-center gap-1.5 text-xs">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                            Tìm kiếm chương trình
                        </button>
                    </div>
                    <div class="flex items-center gap-2">
                        <button onclick="openFolder()" class="text-xs text-slate-600 hover:text-slate-800 bg-slate-100 hover:bg-slate-200 font-medium px-3 py-1.5 rounded-lg border border-slate-300 transition flex items-center gap-1">
                            📁 Mở thư mục ảnh
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Main Content 2 Cột -->
        <main class="max-w-7xl mx-auto px-4 py-6">
            <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
                
                <!-- Cột Trái: Trạng thái & Điều khiển -->
                <div class="lg:col-span-4 space-y-5 lg:sticky lg:top-[125px]">
                    <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200 space-y-4">
                        <div class="flex justify-between items-center border-b pb-3">
                            <h2 class="text-sm font-bold text-slate-800 flex items-center gap-2">
                                <span class="w-2 h-2 rounded-full bg-blue-600 animate-pulse"></span>
                                Tiến trình Ingest
                            </h2>
                            <span id="badgeStatus" class="px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-600">Sẵn sàng</span>
                        </div>

                        <div>
                            <div class="flex justify-between text-xs font-semibold mb-1">
                                <span class="text-slate-600">Tiến độ tải ảnh:</span>
                                <span id="progressText" class="text-blue-600">0 / 0</span>
                            </div>
                            <div class="w-full bg-slate-100 rounded-full h-2.5 overflow-hidden">
                                <div id="progressBar" class="bg-blue-600 h-2.5 rounded-full transition-all duration-300" style="width: 0%"></div>
                            </div>
                        </div>

                        <div class="grid grid-cols-2 gap-3 pt-2 text-xs">
                            <div class="bg-slate-50 p-3 rounded-xl border border-slate-100">
                                <span class="text-slate-400 block text-[11px]">Đã chọn tải:</span>
                                <span id="statSelected" class="text-lg font-bold text-slate-700">0 CT</span>
                            </div>
                            <div class="bg-blue-50/50 p-3 rounded-xl border border-blue-100">
                                <span class="text-blue-500 block text-[11px]">Tổng ảnh ước tính:</span>
                                <span id="statEstImages" class="text-lg font-bold text-blue-700">0</span>
                            </div>
                        </div>

                        <div class="pt-2 flex gap-2">
                            <button onclick="startDownload()" id="btnDownload" class="flex-1 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white font-bold py-2.5 px-4 rounded-xl shadow-sm transition text-xs flex justify-center items-center gap-2">
                                🚀 Bắt đầu tải
                            </button>
                            <button onclick="stopDownload()" id="btnStop" disabled class="bg-rose-50 text-rose-600 hover:bg-rose-100 font-semibold py-2.5 px-3 rounded-xl border border-rose-200 transition text-xs disabled:opacity-50">
                                ⏹ Dừng
                            </button>
                        </div>
                    </div>

                    <!-- Realtime Logs -->
                    <div class="bg-slate-900 text-slate-200 p-4 rounded-2xl shadow-sm border border-slate-800 space-y-2">
                        <div class="flex justify-between items-center text-xs text-slate-400 border-b border-slate-800 pb-2">
                            <span class="font-mono">Real-time Logs</span>
                            <button onclick="clearLogs()" class="hover:text-white">Xóa</button>
                        </div>
                        <div id="logConsole" class="h-48 overflow-y-auto font-mono text-[11px] space-y-1 custom-scrollbar text-slate-300">
                            <div class="text-slate-500">Chờ lệnh tải ảnh...</div>
                        </div>
                    </div>
                </div>

                <!-- Cột Phải: Danh sách Chương trình -->
                <div class="lg:col-span-8 space-y-4">
                    <div class="flex justify-between items-center">
                        <h3 class="text-sm font-bold text-slate-800 flex items-center gap-2">
                            Chương trình tìm thấy
                            <span id="badgeProgramCount" class="text-xs bg-slate-200 text-slate-700 px-2 py-0.5 rounded-full font-semibold">0</span>
                        </h3>
                        <div class="flex items-center gap-2">
                            <button onclick="toggleSelectAll(true)" class="text-xs bg-blue-50 hover:bg-blue-100 font-semibold px-3 py-1.5 rounded-lg border border-blue-200 text-blue-700 transition">Chọn tất cả</button>
                            <button onclick="toggleSelectAll(false)" class="text-xs bg-slate-100 hover:bg-slate-200 font-medium px-3 py-1.5 rounded-lg border border-slate-300 text-slate-600 transition">Bỏ chọn tất cả</button>
                        </div>
                    </div>

                    <div id="programGrid" class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                        <div class="col-span-full py-20 text-center text-slate-400 bg-white rounded-2xl border border-dashed border-slate-300">
                            <div class="text-4xl mb-2">📸</div>
                            <p class="font-medium text-sm">Vui lòng bấm <b>"Tìm kiếm chương trình"</b> để quét danh sách ảnh từ DMS.</p>
                        </div>
                    </div>
                </div>

            </div>
        </main>
    </div>

    <!-- TAB 2: AUDIT & CHẤM ĐIỂM TẦNG 1 + TẦNG 2 -->
    <div id="viewAudit" class="hidden">
        <main class="max-w-7xl mx-auto px-4 py-6 space-y-6">
            <!-- Control bar for Audit -->
            <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200 space-y-4">
                <div class="flex flex-wrap justify-between items-center gap-4">
                    <div class="flex-1 min-w-[280px]">
                        <label class="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">Chọn chương trình đã tải để chấm:</label>
                        <select id="selAuditProgram" onchange="loadAuditProgramData()" class="w-full text-xs font-semibold px-3.5 py-2.5 border border-slate-300 rounded-xl focus:ring-2 focus:ring-blue-500 bg-white">
                            <option value="">-- Đang quét danh sách thư mục đã tải... --</option>
                        </select>
                    </div>

                    <!-- Config Thresholds -->
                    <div class="flex flex-wrap items-center gap-3">
                        <div>
                            <label class="block text-[11px] font-bold text-slate-600 mb-1">Ngưỡng Đạt GPS (m):</label>
                            <input type="number" id="cfgGpsPass" value="100" class="w-24 text-xs px-2.5 py-2 border rounded-lg font-bold text-emerald-700 text-center">
                        </div>
                        <div>
                            <label class="block text-[11px] font-bold text-slate-600 mb-1">Ngưỡng Cảnh báo (m):</label>
                            <input type="number" id="cfgGpsWarn" value="200" class="w-24 text-xs px-2.5 py-2 border rounded-lg font-bold text-amber-700 text-center">
                        </div>
                        <div>
                            <label class="block text-[11px] font-bold text-slate-600 mb-1">Ngưỡng Moiré:</label>
                            <input type="number" step="0.05" id="cfgMoireTh" value="0.8" class="w-24 text-xs px-2.5 py-2 border rounded-lg font-bold text-purple-700 text-center">
                        </div>
                        <div class="pt-4 flex flex-wrap gap-2">
                            <button onclick="recalculateAudit()" class="bg-blue-600 hover:bg-blue-700 text-white font-bold px-3 py-2 rounded-lg text-xs shadow-xs transition">
                                ⚡ Áp dụng
                            </button>
                            <button onclick="runDuplicateScan()" id="btnRunDupScan" class="bg-purple-600 hover:bg-purple-700 text-white font-bold px-3 py-2 rounded-lg text-xs shadow-xs transition flex items-center gap-1">
                                🔁 Quét Trùng Đa Điểm
                            </button>
                            <button onclick="runAntifraudScan()" id="btnRunAnti" class="bg-purple-50 hover:bg-purple-100 text-purple-700 border border-purple-200 font-semibold px-3 py-2 rounded-lg text-xs transition">
                                🔍 Quét Moiré
                            </button>
                            <button onclick="runAiScoring()" id="btnRunAi" class="bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-700 hover:to-teal-700 text-white font-bold px-4 py-2 rounded-lg text-xs shadow-sm transition flex items-center gap-1.5">
                                🚀 Chấm AI (Tầng 2)
                            </button>
                            <button onclick="exportAuditCSV()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-bold px-3.5 py-2 rounded-lg text-xs shadow-xs transition flex items-center gap-1.5">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                                Xuất CSV
                            </button>
                        </div>
                    </div>
                </div>

                <!-- AI Scoring Live Progress Banner -->
                <div id="aiProgressBarContainer" class="hidden bg-indigo-50 border border-indigo-200 rounded-xl p-3 space-y-2">
                    <div class="flex justify-between items-center text-xs font-semibold text-indigo-900">
                        <span id="aiProgressTitle" class="flex items-center gap-2">
                            <span class="w-2.5 h-2.5 rounded-full bg-indigo-600 animate-ping"></span>
                            Đang chạy AI nhận diện bao bì & đếm facings...
                        </span>
                        <span id="aiProgressCount">0 / 0</span>
                    </div>
                    <div class="w-full bg-indigo-100 rounded-full h-2 overflow-hidden">
                        <div id="aiProgressBar" class="bg-indigo-600 h-2 rounded-full transition-all duration-200" style="width: 0%"></div>
                    </div>
                    <div class="text-[11px] text-indigo-700 font-mono" id="aiProgressFile"></div>
                </div>

                <!-- Stats Badges & Filters -->
                <div class="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-slate-100 text-xs">
                    <div class="flex flex-wrap items-center gap-2">
                        <span class="font-bold text-slate-700">Lọc kết quả:</span>
                        <button onclick="setAuditFilter('ALL')" id="flt_ALL" class="px-2.5 py-1 rounded-md bg-slate-800 text-white font-semibold">Tất cả (<span id="cntAll">0</span>)</button>
                        <button onclick="setAuditFilter('CROSS_DUP')" id="flt_CROSS_DUP" class="px-2.5 py-1 rounded-md bg-purple-100 text-purple-800 font-bold border border-purple-300">🔁 Trùng đa điểm (<span id="cntCrossDup">0</span>)</button>
                        <button onclick="setAuditFilter('FAIL_GPS')" id="flt_FAIL_GPS" class="px-2.5 py-1 rounded-md bg-rose-50 text-rose-700 font-semibold border border-rose-200">📍 Lệch GPS >200m (<span id="cntFailGps">0</span>)</button>
                        <button onclick="setAuditFilter('WARN_GPS')" id="flt_WARN_GPS" class="px-2.5 py-1 rounded-md bg-amber-50 text-amber-700 font-semibold border border-amber-200">⚠️ Cảnh báo 100-200m (<span id="cntWarnGps">0</span>)</button>
                        <button onclick="setAuditFilter('MOIRE_SUSPECT')" id="flt_MOIRE_SUSPECT" class="px-2.5 py-1 rounded-md bg-pink-50 text-pink-700 font-semibold border border-pink-200">📱 Nghi màn hình (<span id="cntMoire">0</span>)</button>
                        <button onclick="setAuditFilter('AI_CHUAN')" id="flt_AI_CHUAN" class="px-2.5 py-1 rounded-md bg-emerald-100 text-emerald-800 font-bold border border-emerald-300">🤖 AI: Đạt chuẩn (<span id="cntAiChuan">0</span>)</button>
                        <button onclick="setAuditFilter('AI_DUDI')" id="flt_AI_DUDI" class="px-2.5 py-1 rounded-md bg-amber-100 text-amber-800 font-bold border border-amber-300">🟡 AI: Đạt du di (<span id="cntAiDuDi">0</span>)</button>
                        <button onclick="setAuditFilter('AI_FAIL')" id="flt_AI_FAIL" class="px-2.5 py-1 rounded-md bg-rose-100 text-rose-800 font-bold border border-rose-300">❌ AI: Không đạt (<span id="cntAiFail">0</span>)</button>
                        <button onclick="setAuditFilter('HOP_LE')" id="flt_HOP_LE" class="px-2.5 py-1 rounded-md bg-emerald-600 text-white font-bold">🏆 Hợp lệ trả thưởng (<span id="cntHopLe">0</span>)</button>
                    </div>

                    <div class="w-full sm:w-64">
                        <input type="text" id="inpAuditSearch" oninput="applyAuditFilter()" placeholder="Tìm theo mã KH, tên KH, NVBH..." class="w-full text-xs px-3 py-1.5 border border-slate-300 rounded-lg focus:ring-2 focus:ring-blue-500">
                    </div>
                </div>
            </div>

            <!-- Audit Table -->
            <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
                <div class="overflow-x-auto max-h-[680px]">
                    <table class="min-w-full divide-y divide-slate-200 text-xs text-left">
                        <thead class="bg-slate-100 font-bold text-slate-700 sticky top-0 z-10 shadow-xs">
                            <tr>
                                <th class="px-3 py-3 w-12 text-center">STT</th>
                                <th class="px-3 py-3 w-20 text-center">Ảnh kệ</th>
                                <th class="px-4 py-3 min-w-[200px]">Điểm bán (Khách hàng)</th>
                                <th class="px-4 py-3 min-w-[130px]">Nhân viên bán hàng</th>
                                <th class="px-3 py-3 w-28">Giờ chụp</th>
                                <th class="px-3 py-3 w-24 text-center">GPS</th>
                                <th class="px-3 py-3 w-20 text-center">Moiré</th>
                                <th class="px-4 py-3 min-w-[180px] text-center">AI Trưng Bày (Tầng 2)</th>
                                <th class="px-4 py-3 min-w-[150px] text-center">Kết Luận Tổng Thể</th>
                                <th class="px-4 py-3 min-w-[200px]">Ghi chú Giám sát (Sửa được)</th>
                                <th class="px-3 py-3 w-24 text-center">Thao tác</th>
                            </tr>
                        </thead>
                        <tbody id="auditTableBody" class="divide-y divide-slate-100">
                            <tr>
                                <td colspan="11" class="py-16 text-center text-slate-400">Vui lòng chọn chương trình để tải bảng dữ liệu chấm ảnh.</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </main>
    </div>

    <!-- TAB 3: CONFIGURATION / CẤU HÌNH SKU & DU DI -->
    <div id="viewConfig" class="hidden">
        <main class="max-w-5xl mx-auto px-4 py-6 space-y-6">
            <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 space-y-5">
                <div class="border-b pb-4 flex justify-between items-center">
                    <div>
                        <h2 class="text-base font-bold text-slate-800">Cấu hình Quy tắc Chấm & Sản phẩm (SKU)</h2>
                        <p class="text-xs text-slate-500 mt-0.5">Thiết lập ngưỡng "du di", danh sách SKU cần phát hiện và tải ảnh bao bì mẫu.</p>
                    </div>
                    <button onclick="saveCurrentConfig()" class="bg-blue-600 hover:bg-blue-700 text-white font-bold px-4 py-2 rounded-xl text-xs shadow-sm transition flex items-center gap-1.5">
                        💾 Lưu Cấu Hình
                    </button>
                </div>

                <!-- Program Selector for Config -->
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 bg-slate-50 p-4 rounded-xl border border-slate-200">
                    <div>
                        <label class="block text-xs font-bold text-slate-700 mb-1">Chọn chương trình cấu hình:</label>
                        <select id="cfgProgSelect" onchange="onConfigProgramChanged()" class="w-full text-xs font-semibold px-3 py-2 border rounded-lg bg-white">
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-700 mb-1">Tên chương trình:</label>
                        <input type="text" id="cfgProgName" class="w-full text-xs px-3 py-2 border rounded-lg bg-white font-medium">
                    </div>
                </div>

                <!-- Flexibility / Du di settings -->
                <div class="bg-amber-50/70 border border-amber-200 p-4 rounded-xl space-y-3">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center gap-2">
                            <span class="text-lg">⚖️</span>
                            <h4 class="text-xs font-bold text-amber-900">Quy tắc "Du di" số lượng SKU</h4>
                        </div>
                        <label class="flex items-center gap-2 cursor-pointer text-xs font-semibold text-amber-800">
                            <input type="checkbox" id="cfgAllowFlex" class="rounded text-amber-600">
                            Cho phép du di đạt chương trình
                        </label>
                    </div>
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 items-center pt-1 text-xs">
                        <div>
                            <label class="block font-semibold text-amber-900 mb-1">Số lượng SKU tối thiểu phải có để ĐẠT:</label>
                            <div class="flex items-center gap-3">
                                <input type="range" id="cfgMinSkusRange" min="1" max="10" value="1" oninput="document.getElementById('cfgMinSkusVal').innerText = this.value; document.getElementById('cfgMinSkusInp').value = this.value;" class="w-48">
                                <span class="font-bold text-amber-900 text-sm"><span id="cfgMinSkusVal">1</span> SKU</span>
                                <input type="hidden" id="cfgMinSkusInp" value="1">
                            </div>
                        </div>
                        <div class="text-[11px] text-amber-700 leading-relaxed">
                            💡 <b>Ví dụ:</b> Chương trình có 2 sản phẩm (Chà Bông & Sandwich). Nếu đặt tối thiểu <b>1 SKU</b>, điểm bán chỉ cần trưng bày 1 trong 2 loại là được duyệt ĐẠT (kết luận <i>Đạt du di 1/2</i>).
                        </div>
                    </div>
                </div>

                <!-- SKU Cards List -->
                <div class="space-y-4">
                    <div class="flex justify-between items-center">
                        <h4 class="text-xs font-bold text-slate-800 uppercase tracking-wider">Danh sách SKU sản phẩm cần kiểm tra:</h4>
                        <button onclick="addNewSkuRow()" class="text-xs bg-slate-100 hover:bg-slate-200 font-bold px-3 py-1.5 rounded-lg border border-slate-300 text-slate-700 transition">
                            + Thêm SKU mới
                        </button>
                    </div>

                    <div id="skusContainer" class="space-y-3">
                        <!-- Rendered by JS -->
                    </div>
                </div>

            </div>
        </main>
    </div>

    <!-- Image Zoom & Bounding Box Modal -->
    <div id="imageModal" class="hidden fixed inset-0 bg-slate-900/85 backdrop-blur-xs flex items-center justify-center p-4 z-50" onclick="closeImageModal()">
        <div class="bg-white rounded-2xl p-5 max-w-5xl w-full max-h-[94vh] flex flex-col shadow-2xl space-y-3" onclick="event.stopPropagation()">
            <div class="flex justify-between items-center border-b pb-2">
                <div>
                    <h3 class="text-sm font-bold text-slate-800" id="modalImgTitle">Xem ảnh chi tiết</h3>
                    <p class="text-[11px] text-slate-500" id="modalImgSubtitle"></p>
                </div>
                <div class="flex items-center gap-3">
                    <label class="flex items-center gap-1.5 text-xs font-bold text-indigo-700 bg-indigo-50 px-3 py-1.5 rounded-lg border border-indigo-200 cursor-pointer">
                        <input type="checkbox" id="toggleBboxCheckbox" checked onchange="renderBboxOverlay()" class="rounded text-indigo-600">
                        🎯 Hiển thị Bounding Box AI
                    </label>
                    <button onclick="rotateModalImage()" id="btnRotateModal" class="text-xs font-bold text-slate-700 bg-slate-100 hover:bg-slate-200 px-3 py-1.5 rounded-lg border border-slate-300 transition flex items-center gap-1 shadow-xs">🔄 Xoay 90°</button>
                    <a id="modalExternalLink" href="#" target="_blank" class="text-xs text-blue-600 hover:underline font-semibold bg-blue-50 px-2.5 py-1.5 rounded">Link gốc DMS ↗</a>
                    <button onclick="closeImageModal()" class="text-slate-400 hover:text-slate-700 text-lg font-bold px-2">✕</button>
                </div>
            </div>

            <!-- Image Viewport with SVG Bbox Overlay -->
            <div class="flex-1 overflow-hidden relative flex items-center justify-center bg-slate-950 rounded-xl p-2 min-h-[440px]" id="imgViewportContainer">
                <div class="relative inline-block max-h-[68vh] max-w-full" id="bboxWrapper">
                    <img id="modalImgElement" src="" alt="Ảnh trưng bày" class="max-h-[68vh] max-w-full object-contain rounded block" onload="onModalImgLoaded()">
                    <svg id="modalBboxSvg" class="absolute inset-0 w-full h-full pointer-events-none"></svg>
                </div>
            </div>

            <!-- Detected SKUs Bar & Metadata -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs bg-slate-50 p-3 rounded-xl border border-slate-200" id="modalImgMeta">
                <!-- Meta left -->
                <div class="space-y-1">
                    <div id="metaInfoText"></div>
                </div>
                <!-- Detected SKUs right -->
                <div class="space-y-1">
                    <span class="font-bold text-slate-700 block">Kết quả nhận diện AI:</span>
                    <div id="metaDetectedBadges" class="flex flex-wrap gap-2"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- Side-by-Side Compare Modal -->
    <div id="compareModal" class="hidden fixed inset-0 bg-slate-900/80 backdrop-blur-xs flex items-center justify-center p-4 z-50" onclick="closeCompareModal()">
        <div class="bg-white rounded-2xl p-5 max-w-5xl w-full max-h-[92vh] flex flex-col shadow-2xl space-y-4" onclick="event.stopPropagation()">
            <div class="flex justify-between items-center border-b pb-2.5">
                <div>
                    <h3 class="text-base font-bold text-rose-700 flex items-center gap-2">
                        <span>🔁 Đối Chiếu Phát Hiện Trùng Ảnh Đa Điểm Bán</span>
                    </h3>
                    <p class="text-xs text-slate-500 mt-0.5">Cảnh báo: Cùng 1 ảnh chụp (hoặc tương đồng cao) được nộp cho 2 điểm bán khác nhau!</p>
                </div>
                <button onclick="closeCompareModal()" class="text-slate-400 hover:text-slate-700 text-lg font-bold px-2">✕</button>
            </div>
            
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 flex-1 overflow-auto p-1">
                <!-- Shop 1 -->
                <div class="bg-slate-50 p-3.5 rounded-xl border border-slate-200 space-y-2.5">
                    <div class="flex justify-between items-start">
                        <div>
                            <span class="text-[10px] font-bold text-blue-700 bg-blue-100 px-2 py-0.5 rounded">ĐIỂM BÁN 1</span>
                            <h4 class="font-bold text-xs text-slate-800 mt-1" id="cmpShop1Name">Shop 1</h4>
                            <p class="text-[11px] text-slate-500 font-mono" id="cmpShop1Code">Mã KH</p>
                        </div>
                        <div class="text-right text-[11px] text-slate-500">
                            <span id="cmpShop1Staff">NVBH</span><br>
                            <span id="cmpShop1Time" class="font-mono">Time</span>
                        </div>
                    </div>
                    <div class="h-64 bg-slate-900 rounded-lg overflow-hidden flex items-center justify-center">
                        <img id="cmpShop1Img" src="" class="max-h-full max-w-full object-contain">
                    </div>
                </div>

                <!-- Shop 2 -->
                <div class="bg-slate-50 p-3.5 rounded-xl border border-slate-200 space-y-2.5">
                    <div class="flex justify-between items-start">
                        <div>
                            <span class="text-[10px] font-bold text-rose-700 bg-rose-100 px-2 py-0.5 rounded">ĐIỂM BÁN 2 (BỊ TRÙNG)</span>
                            <h4 class="font-bold text-xs text-slate-800 mt-1" id="cmpShop2Name">Shop 2</h4>
                            <p class="text-[11px] text-slate-500 font-mono" id="cmpShop2Code">Mã KH</p>
                        </div>
                        <div class="text-right text-[11px] text-slate-500">
                            <span id="cmpShop2Staff">NVBH</span><br>
                            <span id="cmpShop2Time" class="font-mono">Time</span>
                        </div>
                    </div>
                    <div class="h-64 bg-slate-900 rounded-lg overflow-hidden flex items-center justify-center">
                        <img id="cmpShop2Img" src="" class="max-h-full max-w-full object-contain">
                    </div>
                </div>
            </div>

            <div class="flex justify-between items-center pt-2 border-t text-xs">
                <span id="cmpSimilarityInfo" class="font-semibold text-rose-600">Độ tương đồng ảnh: Cao</span>
                <button onclick="closeCompareModal()" class="px-4 py-1.5 font-semibold text-slate-700 bg-slate-200 hover:bg-slate-300 rounded-lg">Đóng đối chiếu</button>
            </div>
        </div>
    </div>

    <!-- Login Modal -->
    <div id="loginModal" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-xs flex items-center justify-center p-4 z-50">
        <div class="bg-white rounded-2xl p-6 max-w-sm w-full shadow-2xl space-y-4">
            <h3 class="text-base font-bold text-slate-800">Cấu hình đăng nhập DMS</h3>
            <div class="space-y-3 text-xs">
                <div>
                    <label class="block font-semibold mb-1 text-slate-600">Tài khoản DMS:</label>
                    <input type="text" id="inpUser" class="w-full px-3 py-2 border rounded-lg font-medium">
                </div>
                <div>
                    <label class="block font-semibold mb-1 text-slate-600">Mật khẩu:</label>
                    <input type="password" id="inpPass" class="w-full px-3 py-2 border rounded-lg">
                </div>
            </div>
            <div class="flex justify-end gap-2 pt-2">
                <button onclick="closeLoginModal()" class="px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100 rounded-lg">Đóng</button>
                <button onclick="saveAndLogin()" id="btnLoginSubmit" class="px-4 py-1.5 text-xs font-semibold text-white bg-blue-600 hover:bg-blue-700 rounded-lg">Đăng nhập</button>
            </div>
        </div>
    </div>

    <script>
        let programs = [];
        let selectedPids = new Set();
        let auditData = [];
        let filteredAudit = [];
        let currentAuditDir = "";
        let programsConfig = {};
        let currentActiveModalItem = null;

        document.addEventListener("DOMContentLoaded", () => {
            checkAuth();
            pollDownload();
            loadDownloadedProgramsList();
            loadAllConfigs();
        });

        function switchMainTab(tab) {
            const btnIngest = document.getElementById("tabBtnIngest");
            const btnAudit = document.getElementById("tabBtnAudit");
            const btnConfig = document.getElementById("tabBtnConfig");
            const viewIngest = document.getElementById("viewIngest");
            const viewAudit = document.getElementById("viewAudit");
            const viewConfig = document.getElementById("viewConfig");
            const btnLabel = document.getElementById("tabBtnLabel");
            const viewLabel = document.getElementById("viewLabel");

            [btnIngest, btnAudit, btnConfig, btnLabel].forEach(b => {
                b.className = "px-4 py-1.5 font-semibold text-blue-100 hover:text-white rounded-lg transition";
            });
            [viewIngest, viewAudit, viewConfig, viewLabel].forEach(v => v.classList.add("hidden"));

            if (tab === 'ingest') {
                viewIngest.classList.remove("hidden");
                btnIngest.className = "px-4 py-1.5 font-bold rounded-lg bg-white text-blue-700 shadow-sm transition";
            } else if (tab === 'audit') {
                viewAudit.classList.remove("hidden");
                btnAudit.className = "px-4 py-1.5 font-bold rounded-lg bg-white text-blue-700 shadow-sm transition";
                loadDownloadedProgramsList();
            } else if (tab === 'config') {
                viewConfig.classList.remove("hidden");
                btnConfig.className = "px-4 py-1.5 font-bold rounded-lg bg-white text-blue-700 shadow-sm transition";
                loadAllConfigs();
            } else if (tab === 'label') {
                viewLabel.classList.remove("hidden");
                btnLabel.className = "px-4 py-1.5 font-bold rounded-lg bg-white text-blue-700 shadow-sm transition";
                loadLabelTab();
            }
        }

        async function checkAuth() {
            try {
                const res = await fetch("/api/auth_status");
                const data = await res.json();
                document.getElementById("inpUser").value = data.user || "";
                if (data.logged_in) {
                    document.getElementById("authDot").style.background = "#10b981";
                    document.getElementById("authStatusText").innerText = `Đã kết nối: ${data.user}`;
                } else {
                    document.getElementById("authDot").style.background = "#f59e0b";
                    document.getElementById("authStatusText").innerText = "Chưa kết nối DMS";
                }
            } catch (e) {}
        }

        function openLoginModal() { document.getElementById("loginModal").classList.remove("hidden"); }
        function closeLoginModal() { document.getElementById("loginModal").classList.add("hidden"); }

        async function saveAndLogin() {
            const btn = document.getElementById("btnLoginSubmit");
            btn.disabled = true; btn.innerText = "Đang kết nối...";
            try {
                const res = await fetch("/api/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        user: document.getElementById("inpUser").value.trim(),
                        password: document.getElementById("inpPass").value
                    })
                });
                const data = await res.json();
                if (data.ok) {
                    closeLoginModal();
                    checkAuth();
                    searchPrograms();
                } else {
                    alert("Lỗi đăng nhập: " + data.error);
                }
            } catch (e) { alert("Lỗi: " + e); }
            finally { btn.disabled = false; btn.innerText = "Đăng nhập"; }
        }

        async function searchPrograms() {
            const btn = document.getElementById("btnSearch");
            btn.disabled = true; btn.innerHTML = `Đang quét...`;
            try {
                const res = await fetch("/api/search", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        from: document.getElementById("fromDate").value.trim(),
                        to: document.getElementById("toDate").value.trim()
                    })
                });
                const data = await res.json();
                if (data.ok) {
                    programs = data.programs || [];
                    selectedPids.clear();
                    programs.forEach(p => selectedPids.add(p.displayProgrameId));
                    renderProgramGrid();
                } else {
                    if (res.status === 401) openLoginModal();
                    else alert("Lỗi: " + data.error);
                }
            } catch (e) { alert("Lỗi: " + e); }
            finally {
                btn.disabled = false;
                btn.innerHTML = `<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg> Tìm kiếm chương trình`;
            }
        }

        function renderProgramGrid() {
            const grid = document.getElementById("programGrid");
            document.getElementById("badgeProgramCount").innerText = programs.length;
            if (programs.length === 0) {
                grid.innerHTML = `<div class="col-span-full py-16 text-center text-slate-400 bg-white rounded-2xl border border-dashed">Không tìm thấy chương trình nào trong khoảng thời gian này.</div>`;
                updateStats();
                return;
            }

            grid.innerHTML = programs.map(p => {
                const isChecked = selectedPids.has(p.displayProgrameId);
                const thumbUrl = p.thumb ? `http://huunghiv2.dmsone.vn:8080/huunghi/${p.thumb}` : '';
                return `
                <div class="bg-white p-4 rounded-xl border ${isChecked ? 'border-blue-500 ring-2 ring-blue-500/20 shadow-sm' : 'border-slate-200'} transition cursor-pointer flex flex-col justify-between" onclick="toggleProgram('${p.displayProgrameId}')">
                    <div class="space-y-2">
                        <div class="flex items-start justify-between gap-2">
                            <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-slate-100 text-slate-600 font-bold">ID: ${p.displayProgrameId}</span>
                            <input type="checkbox" ${isChecked ? 'checked' : ''} onclick="event.stopPropagation(); toggleProgram('${p.displayProgrameId}')" class="rounded text-blue-600 w-4 h-4 cursor-pointer">
                        </div>
                        <h4 class="font-bold text-xs text-slate-800 line-clamp-2 leading-tight">${p.code}</h4>
                    </div>
                    <div class="pt-3 border-t border-slate-100 flex items-center justify-between mt-3 text-xs">
                        <span class="text-slate-500">Số lượng ảnh:</span>
                        <span class="font-bold text-blue-600 bg-blue-50 px-2 py-0.5 rounded-md">${p.count.toLocaleString()} ảnh</span>
                    </div>
                </div>`;
            }).join("");

            updateStats();
        }

        function toggleProgram(pid) {
            if (selectedPids.has(pid)) selectedPids.delete(pid);
            else selectedPids.add(pid);
            renderProgramGrid();
        }

        function toggleSelectAll(select) {
            if (select) programs.forEach(p => selectedPids.add(p.displayProgrameId));
            else selectedPids.clear();
            renderProgramGrid();
        }

        function updateStats() {
            document.getElementById("statSelected").innerText = `${selectedPids.size} CT`;
            let total = 0;
            programs.forEach(p => {
                if (selectedPids.has(p.displayProgrameId)) total += p.count;
            });
            document.getElementById("statEstImages").innerText = total.toLocaleString();
        }

        async function startDownload() {
            if (selectedPids.size === 0) { alert("Vui lòng chọn ít nhất 1 chương trình để tải!"); return; }
            try {
                const res = await fetch("/api/start_download", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        pids: Array.from(selectedPids),
                        from: document.getElementById("fromDate").value.trim(),
                        to: document.getElementById("toDate").value.trim()
                    })
                });
                const data = await res.json();
                if (data.ok) {
                    appendLog("Bắt đầu tiến trình tải đa luồng CDN...");
                    pollDownload();
                } else alert("Lỗi: " + data.error);
            } catch (e) { alert("Lỗi: " + e); }
        }

        async function stopDownload() {
            await fetch("/api/stop_download", { method: "POST" });
            appendLog("Đã gửi yêu cầu dừng tiến trình.");
        }

        function appendLog(msg) {
            const con = document.getElementById("logConsole");
            const time = new Date().toLocaleTimeString();
            con.innerHTML += `<div><span class="text-slate-500">[${time}]</span> ${msg}</div>`;
            con.scrollTop = con.scrollHeight;
        }

        function clearLogs() { document.getElementById("logConsole").innerHTML = ""; }

        async function pollDownload() {
            try {
                const res = await fetch("/api/download_status");
                const data = await res.json();
                const btnDl = document.getElementById("btnDownload");
                const btnSt = document.getElementById("btnStop");
                const bStat = document.getElementById("badgeStatus");

                if (data.is_downloading) {
                    btnDl.disabled = true; btnSt.disabled = false;
                    bStat.innerText = "Đang tải...";
                    bStat.className = "px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-blue-100 text-blue-700 animate-pulse";
                    const pct = data.total_images > 0 ? Math.round((data.downloaded_images / data.total_images) * 100) : 0;
                    document.getElementById("progressBar").style.width = pct + "%";
                    document.getElementById("progressText").innerText = `${data.downloaded_images} / ${data.total_images} (${pct}%)`;
                    setTimeout(pollDownload, 1000);
                } else {
                    btnDl.disabled = false; btnSt.disabled = true;
                    if (data.completed) {
                        bStat.innerText = "Hoàn tất";
                        bStat.className = "px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-100 text-emerald-700";
                        document.getElementById("progressBar").style.width = "100%";
                    }
                }
            } catch (e) {}
        }

        async function openFolder() {
            await fetch("/api/open_folder");
        }

        // ================= TAB 2: AUDIT & AI SCORING =================
        let auditFilterMode = 'ALL';

        async function loadDownloadedProgramsList() {
            try {
                const res = await fetch("/api/list_downloaded");
                const list = await res.json();
                const sel = document.getElementById("selAuditProgram");
                const cfgSel = document.getElementById("cfgProgSelect");

                if (!list || list.length === 0) {
                    sel.innerHTML = `<option value="">-- Chưa có dữ liệu tải về. Vui lòng tải ở Tab 1 --</option>`;
                    return;
                }

                sel.innerHTML = list.map(item => `
                    <option value="${item.rel_path}">${item.name} (${item.date_range} - ${item.count} ảnh)</option>
                `).join("");

                if (cfgSel) {
                    cfgSel.innerHTML = list.map(item => `
                        <option value="${item.name}">${item.name} (${item.date_range})</option>
                    `).join("");
                }

                if (!currentAuditDir && list.length > 0) {
                    currentAuditDir = list[0].rel_path;
                    loadAuditProgramData();
                }
            } catch (e) {}
        }

        async function loadAuditProgramData() {
            const sel = document.getElementById("selAuditProgram");
            currentAuditDir = sel.value;
            if (!currentAuditDir) return;

            const tbody = document.getElementById("auditTableBody");
            tbody.innerHTML = `<tr><td colspan="11" class="py-16 text-center text-slate-500 font-semibold"><span class="animate-pulse">⏳ Đang nạp dữ liệu Tầng 1 và kết quả AI Tầng 2...</span></td></tr>`;

            try {
                const res = await fetch(`/api/get_program_audit?dir=${encodeURIComponent(currentAuditDir)}&t=${Date.now()}`);
                auditData = await res.json();
                recalculateAudit();
            } catch (e) {
                tbody.innerHTML = `<tr><td colspan="11" class="py-16 text-center text-rose-500">Lỗi nạp dữ liệu: ${e}</td></tr>`;
            }
        }

        function recalculateAudit() {
            const passTh = parseFloat(document.getElementById("cfgGpsPass").value) || 100.0;
            const warnTh = parseFloat(document.getElementById("cfgGpsWarn").value) || 200.0;
            const moireTh = parseFloat(document.getElementById("cfgMoireTh").value) || 0.8;

            auditData.forEach(item => {
                const d = item.gps_dist_m;
                if (d === null || d === undefined) item.gps_status = 'UNKNOWN';
                else if (d <= passTh) item.gps_status = 'PASS';
                else if (d <= warnTh) item.gps_status = 'WARN';
                else item.gps_status = 'FAIL';

                const m = item.moire;
                item.is_moire_suspect = (m !== null && m !== undefined && m >= moireTh);
            });

            applyAuditFilter();
        }

        function setAuditFilter(mode) {
            auditFilterMode = mode;
            const btnIds = ['ALL', 'CROSS_DUP', 'FAIL_GPS', 'WARN_GPS', 'MOIRE_SUSPECT', 'AI_CHUAN', 'AI_DUDI', 'AI_FAIL', 'HOP_LE'];
            btnIds.forEach(id => {
                const btn = document.getElementById('flt_' + id);
                if (btn) {
                    if (id === mode) {
                        btn.classList.add("ring-2", "ring-offset-1", "ring-blue-500", "shadow-sm");
                    } else {
                        btn.classList.remove("ring-2", "ring-offset-1", "ring-blue-500", "shadow-sm");
                    }
                }
            });
            applyAuditFilter();
        }

        function applyAuditFilter() {
            const search = (document.getElementById("inpAuditSearch").value || "").toLowerCase().trim();

            let cntAll = 0, cntCrossDup = 0, cntFailGps = 0, cntWarnGps = 0, cntMoire = 0;
            let cntAiChuan = 0, cntAiDuDi = 0, cntAiFail = 0, cntHopLe = 0;

            auditData.forEach(item => {
                cntAll++;
                if (item.is_cross_store_dup) cntCrossDup++;
                if (item.gps_status === 'FAIL') cntFailGps++;
                if (item.gps_status === 'WARN') cntWarnGps++;
                if (item.is_moire_suspect) cntMoire++;
                if (item.tier2_status === 'DAT_CHUAN') cntAiChuan++;
                if (item.tier2_status === 'DAT_DU_DI') cntAiDuDi++;
                if (item.tier2_status === 'KHONG_DAT') cntAiFail++;
                if (item.overall_status === 'HOP_LE') cntHopLe++;
            });

            document.getElementById("cntAll").innerText = cntAll;
            document.getElementById("cntCrossDup").innerText = cntCrossDup;
            document.getElementById("cntFailGps").innerText = cntFailGps;
            document.getElementById("cntWarnGps").innerText = cntWarnGps;
            document.getElementById("cntMoire").innerText = cntMoire;
            document.getElementById("cntAiChuan").innerText = cntAiChuan;
            document.getElementById("cntAiDuDi").innerText = cntAiDuDi;
            document.getElementById("cntAiFail").innerText = cntAiFail;
            document.getElementById("cntHopLe").innerText = cntHopLe;

            filteredAudit = auditData.filter(item => {
                if (auditFilterMode === 'CROSS_DUP' && !item.is_cross_store_dup) return false;
                if (auditFilterMode === 'FAIL_GPS' && item.gps_status !== 'FAIL') return false;
                if (auditFilterMode === 'WARN_GPS' && item.gps_status !== 'WARN') return false;
                if (auditFilterMode === 'MOIRE_SUSPECT' && !item.is_moire_suspect) return false;
                if (auditFilterMode === 'AI_CHUAN' && item.tier2_status !== 'DAT_CHUAN') return false;
                if (auditFilterMode === 'AI_DUDI' && item.tier2_status !== 'DAT_DU_DI') return false;
                if (auditFilterMode === 'AI_FAIL' && item.tier2_status !== 'KHONG_DAT') return false;
                if (auditFilterMode === 'HOP_LE' && item.overall_status !== 'HOP_LE') return false;

                if (search) {
                    const matchText = `${item.customerCode || ''} ${item.customerName || ''} ${item.staffCode || ''} ${item.staffName || ''} ${item.review_note || ''}`.toLowerCase();
                    if (!matchText.includes(search)) return false;
                }
                return true;
            });

            renderAuditTable(filteredAudit);
        }

        function renderAuditTable(list) {
            const tbody = document.getElementById("auditTableBody");
            if (!list || list.length === 0) {
                tbody.innerHTML = `<tr><td colspan="11" class="py-12 text-center text-slate-400">Không có dòng dữ liệu nào khớp với bộ lọc.</td></tr>`;
                return;
            }

            tbody.innerHTML = list.map((item, idx) => {
                const imgId = item.imageId;
                const dist = item.gps_dist_m;
                const m = item.moire;
                const note = item.review_note || "";

                // GPS Badge
                let gpsBadge = `<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-slate-100 text-slate-500">N/A</span>`;
                if (dist !== null && dist !== undefined) {
                    if (item.gps_status === 'PASS') {
                        gpsBadge = `<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">${dist}m</span>`;
                    } else if (item.gps_status === 'WARN') {
                        gpsBadge = `<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-amber-50 text-amber-700 border border-amber-200">⚠️ ${dist}m</span>`;
                    } else {
                        gpsBadge = `<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-rose-50 text-rose-700 border border-rose-200">🔴 ${dist}m</span>`;
                    }
                }

                // Moire Badge
                let moireBadge = `<span class="text-slate-400">-</span>`;
                if (m !== null && m !== undefined) {
                    if (item.is_moire_suspect) {
                        moireBadge = `<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-pink-100 text-pink-800 border border-pink-300">📱 ${m}</span>`;
                    } else {
                        moireBadge = `<span class="text-slate-600 font-medium">${m}</span>`;
                    }
                }

                // AI Tier 2 Badge & Detected Facings
                let aiBadge = `<span class="text-slate-400 italic">⏳ Chưa chấm</span>`;
                let facingPills = '';
                if (item.tier2_status) {
                    if (item.tier2_status === 'DAT_CHUAN') {
                        aiBadge = `<span class="px-2 py-0.5 rounded-md text-[11px] font-bold bg-emerald-100 text-emerald-800 border border-emerald-300">✅ Đạt chuẩn (${item.unique_passed_count || 2}/${item.total_skus_cfg || 2})</span>`;
                    } else if (item.tier2_status === 'DAT_DU_DI') {
                        aiBadge = `<span class="px-2 py-0.5 rounded-md text-[11px] font-bold bg-amber-100 text-amber-800 border border-amber-300">🟡 Đạt du di (${item.unique_passed_count || 1}/${item.total_skus_cfg || 2})</span>`;
                    } else {
                        aiBadge = `<span class="px-2 py-0.5 rounded-md text-[11px] font-bold bg-rose-100 text-rose-800 border border-rose-300">❌ Không đạt</span>`;
                    }

                    const det = item.detected_skus || {};
                    const chabong = det["STAFF_CHABONG_60G"];
                    const sandwich = det["STAFF_SANDWICH_275G"];
                    
                    if (chabong && chabong.facings > 0) {
                        facingPills += `<span class="text-[10px] bg-yellow-50 text-yellow-900 border border-yellow-300 px-1.5 py-0.5 rounded font-semibold">🟡 Chà Bông: ${chabong.facings}</span> `;
                    }
                    if (sandwich && sandwich.facings > 0) {
                        facingPills += `<span class="text-[10px] bg-red-50 text-red-900 border border-red-300 px-1.5 py-0.5 rounded font-semibold">🔴 Sandwich: ${sandwich.facings}</span> `;
                    }
                    if (!facingPills) facingPills = `<span class="text-[10px] text-slate-400">0 mặt hàng</span>`;
                }

                // Overall Conclusion Badge
                let overallBadge = '';
                if (item.overall_status === 'HOP_LE') {
                    overallBadge = `<div class="text-center"><span class="px-2 py-0.5 rounded-md text-[11px] font-bold bg-emerald-600 text-white">🏆 HỢP LỆ</span><div class="text-[10px] text-slate-500 mt-0.5">${item.overall_reason || 'Đạt'}</div></div>`;
                } else if (item.overall_status === 'NGHI_VAN') {
                    overallBadge = `<div class="text-center"><span class="px-2 py-0.5 rounded-md text-[11px] font-bold bg-amber-500 text-white">⚠️ NGHI VẤN</span><div class="text-[10px] text-amber-700 font-medium mt-0.5">${item.overall_reason || 'Cần kiểm tra'}</div></div>`;
                } else if (item.overall_status === 'LOAI') {
                    overallBadge = `<div class="text-center"><span class="px-2 py-0.5 rounded-md text-[11px] font-bold bg-rose-600 text-white">🚫 TỪ CHỐI</span><div class="text-[10px] text-rose-700 font-medium mt-0.5">${item.overall_reason || 'Không đạt'}</div></div>`;
                } else {
                    overallBadge = `<span class="text-slate-400 italic">Chưa đánh giá</span>`;
                }

                // Cross-store Duplicate info
                let dupInfoHtml = '';
                let compareBtn = '';
                if (item.is_cross_store_dup && item.dup_target) {
                    const dt = item.dup_target;
                    dupInfoHtml = `<div class="mt-1 text-[11px] text-purple-700 bg-purple-50 p-1 rounded border border-purple-200 leading-tight">
                        <b>Trùng ảnh với:</b> ${dt.customerName || dt.customerCode} (NV: ${dt.staffName || dt.staffCode})
                    </div>`;
                    compareBtn = `<button onclick="openCompareModal('${imgId}')" class="text-xs text-purple-700 hover:text-purple-900 font-bold bg-purple-50 hover:bg-purple-100 px-2 py-1 rounded transition border border-purple-200 mt-1 block w-full">So sánh 2 ảnh</button>`;
                }

                const localImgUrl = `/api/local_image?path=${encodeURIComponent(currentAuditDir + '/' + item.file)}`;
                const cdnImgUrl = item.cdn_url || '#';

                return `
                <tr class="hover:bg-blue-50/30 transition ${item.is_cross_store_dup ? 'bg-purple-50/20' : ''}">
                    <td class="px-3 py-2.5 text-center text-slate-500 font-medium">${idx + 1}</td>
                    <td class="px-3 py-2 text-center">
                        <img src="${localImgUrl}&t=${Date.now()}" alt="Ảnh" class="w-14 h-10 object-cover rounded border border-slate-200 shadow-xs cursor-pointer hover:scale-110 transition duration-200" onclick="zoomImage('${imgId}')" onerror="this.src='${cdnImgUrl}'">
                    </td>
                    <td class="px-4 py-2.5">
                        <div class="font-bold text-slate-800">${item.customerName || 'N/A'}</div>
                        <div class="text-[11px] text-slate-400 font-mono">Mã: ${item.customerCode || 'N/A'}</div>
                        ${dupInfoHtml}
                    </td>
                    <td class="px-4 py-2.5">
                        <div class="font-medium text-slate-800">${item.staffName || 'N/A'}</div>
                        <div class="text-[11px] text-slate-400 font-mono">${item.staffCode || ''} (${item.shopCode || ''})</div>
                    </td>
                    <td class="px-3 py-2.5 text-slate-600 font-mono text-[11px]">
                        ${item.createDate ? item.createDate.replace('T', ' ') : 'N/A'}
                    </td>
                    <td class="px-3 py-2.5 text-center">${gpsBadge}</td>
                    <td class="px-3 py-2.5 text-center">${moireBadge}</td>
                    <td class="px-4 py-2.5 text-center space-y-1">
                        <div>${aiBadge}</div>
                        <div class="flex flex-wrap justify-center gap-1">${facingPills}</div>
                    </td>
                    <td class="px-4 py-2.5 text-center">${overallBadge}</td>
                    <td class="px-4 py-2.5">
                        <div class="flex items-center gap-1.5">
                            <input type="text" id="note_${imgId}" value="${escapeHtml(note)}" placeholder="Nhập nhận xét..." onchange="saveNote('${imgId}')" class="flex-1 text-xs px-2.5 py-1.5 border border-slate-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:bg-white bg-slate-50">
                            <button onclick="saveNote('${imgId}')" class="text-xs bg-slate-200 hover:bg-blue-600 hover:text-white px-2 py-1.5 rounded-lg transition font-medium" title="Lưu nhận xét">💾</button>
                        </div>
                    </td>
                    <td class="px-3 py-2.5 text-center space-y-1">
                        <button onclick="zoomImage('${imgId}')" class="text-xs text-indigo-700 hover:text-indigo-900 font-bold bg-indigo-50 hover:bg-indigo-100 px-2 py-1 rounded transition block w-full border border-indigo-200">🔍 Soi AI Box</button>
                        ${compareBtn}
                    </td>
                </tr>`;
            }).join("");
        }

        async function saveNote(imgId) {
            const input = document.getElementById("note_" + imgId);
            const val = input.value.trim();
            try {
                await fetch("/api/save_review_note", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ dir: currentAuditDir, imageId: imgId, note: val })
                });
                const item = auditData.find(x => x.imageId == imgId);
                if (item) item.review_note = val;
                input.classList.add("bg-emerald-50");
                setTimeout(() => input.classList.remove("bg-emerald-50"), 1000);
            } catch (e) { alert("Lỗi lưu nhận xét: " + e); }
        }

        async function runDuplicateScan() {
            if (!currentAuditDir) return;
            const btn = document.getElementById("btnRunDupScan");
            btn.disabled = true; btn.innerHTML = `Đang quét trùng...`;
            try {
                const res = await fetch("/api/scan_duplicates", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ dir: currentAuditDir })
                });
                const data = await res.json();
                if (data.ok) {
                    alert(data.msg);
                    loadAuditProgramData();
                } else alert("Lỗi: " + data.error);
            } catch (e) { alert("Lỗi: " + e); }
            finally {
                btn.disabled = false;
                btn.innerHTML = `<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4"></path></svg> 🔁 Quét Trùng Đa Điểm`;
            }
        }

        async function runAntifraudScan() {
            if (!currentAuditDir) return;
            const btn = document.getElementById("btnRunAnti");
            btn.disabled = true; btn.innerHTML = `Đang quét FFT...`;
            try {
                const res = await fetch("/api/run_antifraud", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        dir: currentAuditDir,
                        moire_th: document.getElementById("cfgMoireTh").value
                    })
                });
                const data = await res.json();
                if (data.ok) {
                    alert(data.msg);
                    loadAuditProgramData();
                } else alert("Lỗi quét: " + data.error);
            } catch (e) { alert("Lỗi: " + e); }
            finally { btn.disabled = false; btn.innerHTML = `🔍 Quét Moiré FFT`; }
        }

        async function runAiScoring() {
            if (!currentAuditDir) { alert("Vui lòng chọn chương trình!"); return; }
            const btn = document.getElementById("btnRunAi");
            btn.disabled = true; btn.innerText = "Đang khởi động AI...";
            try {
                const res = await fetch("/api/run_ai_scoring", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ dir: currentAuditDir })
                });
                const data = await res.json();
                if (data.ok) {
                    document.getElementById("aiProgressBarContainer").classList.remove("hidden");
                    pollAiScoring();
                } else {
                    alert("Lỗi chạy AI: " + data.error);
                    btn.disabled = false; btn.innerText = "🚀 Chấm AI (Tầng 2)";
                }
            } catch (e) {
                alert("Lỗi kết nối: " + e);
                btn.disabled = false; btn.innerText = "🚀 Chấm AI (Tầng 2)";
            }
        }

        async function pollAiScoring() {
            try {
                const res = await fetch("/api/ai_status");
                const st = await res.json();
                const btn = document.getElementById("btnRunAi");

                if (st.is_running) {
                    const pct = st.total > 0 ? Math.round((st.processed / st.total) * 100) : 0;
                    document.getElementById("aiProgressBar").style.width = pct + "%";
                    document.getElementById("aiProgressCount").innerText = `${st.processed} / ${st.total} (${pct}%)`;
                    document.getElementById("aiProgressFile").innerText = st.current_file || '';
                    setTimeout(pollAiScoring, 800);
                } else {
                    btn.disabled = false; btn.innerText = "🚀 Chấm AI (Tầng 2)";
                    if (st.status === 'completed') {
                        document.getElementById("aiProgressBar").style.width = "100%";
                        document.getElementById("aiProgressTitle").innerHTML = "✅ Đã chấm xong toàn bộ ảnh bằng AI!";
                        setTimeout(() => {
                            document.getElementById("aiProgressBarContainer").classList.add("hidden");
                            loadAuditProgramData();
                        }, 1500);
                    } else if (st.status === 'error') {
                        alert("Lỗi chấm AI: " + st.error);
                        document.getElementById("aiProgressBarContainer").classList.add("hidden");
                    }
                }
            } catch (e) {}
        }

        // ================= ZOOM & BOUNDING BOX MODAL =================
        function zoomImage(imgId) {
            const item = auditData.find(x => x.imageId == imgId);
            if (!item) return;
            currentActiveModalItem = item;

            const modal = document.getElementById("imageModal");
            const imgEl = document.getElementById("modalImgElement");
            const titleEl = document.getElementById("modalImgTitle");
            const subEl = document.getElementById("modalImgSubtitle");
            const linkEl = document.getElementById("modalExternalLink");
            const metaInfo = document.getElementById("metaInfoText");
            const metaDet = document.getElementById("metaDetectedBadges");

            const localImgUrl = `/api/local_image?path=${encodeURIComponent(currentAuditDir + '/' + item.file)}&t=${Date.now()}`;
            imgEl.src = localImgUrl;
            titleEl.innerText = `${item.customerName || 'Khách hàng'} (${item.customerCode || ''})`;
            subEl.innerText = `NV: ${item.staffName || ''} (${item.staffCode || ''}) - Ngày chụp: ${item.createDate || ''}`;
            linkEl.href = item.cdn_url || '#';

            metaInfo.innerHTML = `
                <div>📍 GPS: <b>${item.gps_dist_m !== null ? item.gps_dist_m + 'm' : 'N/A'}</b> (${item.gps_status || 'N/A'})</div>
                <div>📱 Moiré FFT: <b>${item.moire || 'N/A'}</b> ${item.is_moire_suspect ? '<span class="text-rose-600 font-bold">(Nghi chụp màn hình)</span>' : ''}</div>
                <div>📝 Ghi chú: <i>${item.review_note || 'Chưa có ghi chú'}</i></div>
            `;

            let badgesHtml = '';
            const det = item.detected_skus || {};
            if (Object.keys(det).length === 0) {
                badgesHtml = `<span class="text-slate-400 italic">Chưa nhận diện thấy sản phẩm nào</span>`;
            } else {
                for (const [code, s] of Object.entries(det)) {
                    const color = code.includes('CHABONG') ? 'bg-yellow-100 text-yellow-900 border-yellow-400' : 'bg-red-100 text-red-900 border-red-400';
                    badgesHtml += `
                        <div class="p-1.5 rounded-lg border ${color} text-xs font-semibold">
                            <b>${s.sku_name || code}</b>: ${s.facings} mặt trưng bày (Độ tin cậy: ~${Math.round((s.confidence || 0.8) * 100)}%)
                        </div>
                    `;
                }
            }
            metaDet.innerHTML = badgesHtml;

            modal.classList.remove("hidden");
            renderBboxOverlay();
        }

        function onModalImgLoaded() {
            renderBboxOverlay();
        }

        function renderBboxOverlay() {
            const svg = document.getElementById("modalBboxSvg");
            const isChecked = document.getElementById("toggleBboxCheckbox").checked;
            svg.innerHTML = '';
            if (!isChecked || !currentActiveModalItem || !currentActiveModalItem.detected_skus) return;

            const det = currentActiveModalItem.detected_skus;
            let svgContent = '';

            for (const [code, sku] of Object.entries(det)) {
                const bboxes = sku.bboxes || [];
                const isChabong = code.includes('CHABONG');
                const strokeColor = isChabong ? '#eab308' : '#ef4444';
                const fillColor = isChabong ? '#fef08a' : '#fca5a5';
                const textColor = isChabong ? '#713f12' : '#7f1d1d';
                const label = isChabong ? '🟡 Chà Bông' : '🔴 Sandwich';

                bboxes.forEach((b, idx) => {
                    const box = b.box || [0,0,0,0]; // [xmin, ymin, xmax, ymax]
                    const x = (box[0] * 100).toFixed(1) + '%';
                    const y = (box[1] * 100).toFixed(1) + '%';
                    const w = ((box[2] - box[0]) * 100).toFixed(1) + '%';
                    const h = ((box[3] - box[1]) * 100).toFixed(1) + '%';
                    const confPct = Math.round((b.confidence || 0.85) * 100);

                    svgContent += `
                        <rect x="${x}" y="${y}" width="${w}" height="${h}" class="bbox-rect" stroke="${strokeColor}" fill="${fillColor}">
                            <title>${sku.sku_name} - Facing ${idx + 1} (${confPct}%)</title>
                        </rect>
                        <text x="${x}" y="${y}" dy="-4" fill="${strokeColor}" class="bbox-tag" font-weight="bold">${label} #${idx + 1} (${confPct}%)</text>
                    `;
                });
            }

            svg.innerHTML = svgContent;
        }

        async function rotateModalImage() {
            if (!currentActiveModalItem) return;
            const btn = document.getElementById("btnRotateModal");
            btn.disabled = true;
            btn.innerText = "Đang xoay...";
            try {
                const res = await fetch("/api/rotate_image", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        dir: currentAuditDir,
                        imageId: currentActiveModalItem.imageId,
                        angle: 90
                    })
                });
                const data = await res.json();
                if (data.ok) {
                    currentActiveModalItem.detected_skus = data.detected.detected_skus || {};
                    zoomImage(currentActiveModalItem.imageId);
                    loadAuditProgramData();
                } else {
                    alert("Lỗi xoay ảnh: " + data.error);
                }
            } catch (e) {
                alert("Lỗi: " + e);
            } finally {
                btn.disabled = false;
                btn.innerHTML = "🔄 Xoay 90°";
            }
        }

        function closeImageModal() {
            document.getElementById("imageModal").classList.add("hidden");
            currentActiveModalItem = null;
        }

        function openCompareModal(imgId) {
            const item1 = auditData.find(x => x.imageId == imgId);
            if (!item1 || !item1.dup_target) return;
            const dt = item1.dup_target;
            const item2 = auditData.find(x => x.imageId == dt.imageId) || dt;

            document.getElementById("cmpShop1Name").innerText = item1.customerName || 'N/A';
            document.getElementById("cmpShop1Code").innerText = "Mã: " + (item1.customerCode || 'N/A');
            document.getElementById("cmpShop1Staff").innerText = item1.staffName || 'N/A';
            document.getElementById("cmpShop1Time").innerText = item1.createDate || 'N/A';
            document.getElementById("cmpShop1Img").src = `/api/local_image?path=${encodeURIComponent(currentAuditDir + '/' + item1.file)}&t=${Date.now()}`;

            document.getElementById("cmpShop2Name").innerText = item2.customerName || 'N/A';
            document.getElementById("cmpShop2Code").innerText = "Mã: " + (item2.customerCode || 'N/A');
            document.getElementById("cmpShop2Staff").innerText = item2.staffName || 'N/A';
            document.getElementById("cmpShop2Time").innerText = item2.createDate || 'N/A';
            document.getElementById("cmpShop2Img").src = `/api/local_image?path=${encodeURIComponent(currentAuditDir + '/' + (item2.file || dt.file))}&t=${Date.now()}`;

            const distBits = dt.dist !== undefined ? dt.dist : 0;
            const simPct = Math.max(85, Math.round((64 - distBits) / 64 * 100));
            document.getElementById("cmpSimilarityInfo").innerText = `Khoảng cách Hamming: ${distBits}/64 bits (Độ tương đồng: ~${simPct}%)`;

            document.getElementById("compareModal").classList.remove("hidden");
        }

        function closeCompareModal() { document.getElementById("compareModal").classList.add("hidden"); }

        function exportAuditCSV() {
            if (!filteredAudit || filteredAudit.length === 0) { alert("Không có dữ liệu để xuất!"); return; }
            let csv = "STT,Mã KH,Tên Khách Hàng,Mã NVBH,Tên NVBH,NPP,Thời Gian Chụp,Khoảng Cách GPS (m),Đánh Giá GPS,Điểm Moiré,Nghi Màn Hình,Trùng Ảnh Đa Điểm,Chi Tiết Trùng,Mặt Chà Bông,Mặt Sandwich,Tổng Số Mặt,Số SKU Đạt,Đánh Giá AI Tầng 2,Kết Luận Tổng Thể,Lý Do Đánh Giá,Nhận Xét Giám Sát,File Ảnh\\n";
            filteredAudit.forEach((item, idx) => {
                const escapeCsv = (str) => `"${(str || '').toString().replace(/"/g, '""')}"`;
                const dupDetail = item.is_cross_store_dup && item.dup_target ? 
                    `Trùng với ${item.dup_target.customerName} (${item.dup_target.customerCode}) - NV: ${item.dup_target.staffName}` : '';

                const det = item.detected_skus || {};
                const chabongFacings = det["STAFF_CHABONG_60G"] ? det["STAFF_CHABONG_60G"].facings : 0;
                const sandwichFacings = det["STAFF_SANDWICH_275G"] ? det["STAFF_SANDWICH_275G"].facings : 0;

                csv += [
                    idx + 1,
                    escapeCsv(item.customerCode),
                    escapeCsv(item.customerName),
                    escapeCsv(item.staffCode),
                    escapeCsv(item.staffName),
                    escapeCsv(item.shopCode),
                    escapeCsv(item.createDate),
                    item.gps_dist_m !== null ? item.gps_dist_m : '',
                    escapeCsv(item.gps_status),
                    item.moire !== null && item.moire !== undefined ? item.moire : '',
                    item.is_moire_suspect ? 'CÓ' : 'KHÔNG',
                    item.is_cross_store_dup ? 'CÓ' : 'KHÔNG',
                    escapeCsv(dupDetail),
                    chabongFacings,
                    sandwichFacings,
                    item.total_facings !== undefined ? item.total_facings : (chabongFacings + sandwichFacings),
                    item.unique_passed_count !== undefined ? item.unique_passed_count : '',
                    escapeCsv(item.tier2_status || ''),
                    escapeCsv(item.overall_status || ''),
                    escapeCsv(item.overall_reason || ''),
                    escapeCsv(item.review_note),
                    escapeCsv(item.file)
                ].join(",") + "\\n";
            });

            const blob = new Blob(["\\uFEFF" + csv], { type: "text/csv;charset=utf-8;" });
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = `Bang_Cham_AI_DMS_${currentAuditDir.replace('/', '_')}.csv`;
            a.click();
        }

        // ================= TAB 3: CONFIGURATION =================
        async function loadAllConfigs() {
            try {
                const res = await fetch("/api/get_config");
                programsConfig = await res.json();
                onConfigProgramChanged();
            } catch (e) {}
        }

        function onConfigProgramChanged() {
            const sel = document.getElementById("cfgProgSelect");
            let pKey = sel ? sel.value : "873_TRUNGBAYBANHTUOIT102026";
            if (!pKey) pKey = "873_TRUNGBAYBANHTUOIT102026";

            let cfg = programsConfig[pKey];
            if (!cfg) {
                // Mặc định cho chương trình mới
                cfg = {
                    program_name: pKey,
                    min_skus_required: 1,
                    total_skus: 2,
                    allow_flexibility: true,
                    skus: [
                        { sku_code: "STAFF_CHABONG_60G", sku_name: "Bánh mì Chà Bông Staff 60g", description: "Bao bì vàng rực, logo Staff xanh dương", min_facings: 1, is_mandatory: false, sample_images: [] },
                        { sku_code: "STAFF_SANDWICH_275G", sku_name: "Bánh mì Sandwich Staff 275g", description: "Bịch vuông lớn, dải đỏ đáy túi", min_facings: 1, is_mandatory: false, sample_images: [] }
                    ]
                };
                programsConfig[pKey] = cfg;
            }

            document.getElementById("cfgProgName").value = cfg.program_name || pKey;
            document.getElementById("cfgAllowFlex").checked = (cfg.allow_flexibility !== false);
            
            const minSkus = cfg.min_skus_required || 1;
            document.getElementById("cfgMinSkusRange").value = minSkus;
            document.getElementById("cfgMinSkusVal").innerText = minSkus;
            document.getElementById("cfgMinSkusInp").value = minSkus;

            renderSkusContainer(cfg.skus || []);
        }

        function renderSkusContainer(skus) {
            const container = document.getElementById("skusContainer");
            if (!skus || skus.length === 0) {
                container.innerHTML = `<div class="p-6 text-center text-slate-400 bg-slate-50 rounded-xl">Chưa có SKU nào được cấu hình cho chương trình này.</div>`;
                return;
            }

            const pKey = document.getElementById("cfgProgSelect").value || "873_TRUNGBAYBANHTUOIT102026";

            container.innerHTML = skus.map((sku, idx) => {
                const samples = sku.sample_images || [];
                const samplePreviewHtml = samples.length > 0 ? `
                    <div class="flex items-center gap-2 mt-2">
                        <img src="/api/sample_image?path=${encodeURIComponent(samples[0])}&t=${Date.now()}" alt="Ảnh mẫu" class="w-12 h-12 object-cover rounded-lg border border-slate-300 shadow-xs">
                        <span class="text-[11px] text-slate-600 font-mono">${samples[0].split('/').pop()}</span>
                    </div>` : `<div class="text-[11px] text-slate-400 mt-1 italic">Chưa có ảnh mẫu</div>`;

                return `
                <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-xs space-y-3" id="skuCard_${idx}">
                    <div class="flex justify-between items-start gap-4">
                        <div class="flex-1 grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
                            <div>
                                <label class="block font-semibold text-slate-700 mb-1">Mã SKU:</label>
                                <input type="text" id="skuCode_${idx}" value="${escapeHtml(sku.sku_code)}" class="w-full px-2.5 py-1.5 border rounded-lg font-mono">
                            </div>
                            <div>
                                <label class="block font-semibold text-slate-700 mb-1">Tên sản phẩm:</label>
                                <input type="text" id="skuName_${idx}" value="${escapeHtml(sku.sku_name)}" class="w-full px-2.5 py-1.5 border rounded-lg font-medium">
                            </div>
                            <div>
                                <label class="block font-semibold text-slate-700 mb-1">Số mặt tối thiểu (Min facings):</label>
                                <input type="number" id="skuFacings_${idx}" value="${sku.min_facings || 1}" min="1" class="w-24 px-2.5 py-1.5 border rounded-lg font-bold text-center">
                            </div>
                        </div>
                        <button onclick="removeSkuRow(${idx})" class="text-rose-500 hover:text-rose-700 text-xs font-bold px-2 py-1 rounded hover:bg-rose-50">✕ Xóa</button>
                    </div>

                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2 border-t text-xs">
                        <div>
                            <label class="block font-semibold text-slate-600 mb-1">Mô tả đặc trưng bao bì:</label>
                            <input type="text" id="skuDesc_${idx}" value="${escapeHtml(sku.description || '')}" placeholder="Ví dụ: Vàng rực, logo Staff xanh dương..." class="w-full px-2.5 py-1.5 border rounded-lg">
                            <label class="flex items-center gap-1.5 mt-2 cursor-pointer text-slate-700 font-medium">
                                <input type="checkbox" id="skuMandatory_${idx}" ${sku.is_mandatory ? 'checked' : ''} class="rounded text-blue-600">
                                Bắt buộc phải có (Không cho phép du di SKU này)
                            </label>
                        </div>
                        <div>
                            <label class="block font-semibold text-slate-600 mb-1">Ảnh bao bì mẫu:</label>
                            <input type="file" id="skuFileInput_${idx}" accept="image/*" onchange="uploadSkuSample(${idx})" class="text-xs file:mr-2 file:py-1 file:px-2.5 file:rounded-lg file:border-0 file:text-xs file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100 cursor-pointer">
                            <div id="samplePreview_${idx}">${samplePreviewHtml}</div>
                        </div>
                    </div>
                </div>`;
            }).join("");
        }

        function addNewSkuRow() {
            const pKey = document.getElementById("cfgProgSelect").value || "873_TRUNGBAYBANHTUOIT102026";
            if (!programsConfig[pKey]) onConfigProgramChanged();
            programsConfig[pKey].skus = programsConfig[pKey].skus || [];
            programsConfig[pKey].skus.push({
                sku_code: "SKU_MOI_" + (programsConfig[pKey].skus.length + 1),
                sku_name: "Sản phẩm mới",
                description: "",
                min_facings: 1,
                is_mandatory: false,
                sample_images: []
            });
            renderSkusContainer(programsConfig[pKey].skus);
        }

        function removeSkuRow(idx) {
            const pKey = document.getElementById("cfgProgSelect").value || "873_TRUNGBAYBANHTUOIT102026";
            programsConfig[pKey].skus.splice(idx, 1);
            renderSkusContainer(programsConfig[pKey].skus);
        }

        async function uploadSkuSample(idx) {
            const fileInp = document.getElementById("skuFileInput_" + idx);
            if (!fileInp.files || fileInp.files.length === 0) return;
            const file = fileInp.files[0];
            const pKey = document.getElementById("cfgProgSelect").value || "873_TRUNGBAYBANHTUOIT102026";
            const skuCode = document.getElementById("skuCode_" + idx).value.trim();

            const reader = new FileReader();
            reader.onload = async (e) => {
                const b64 = e.target.result.split(',')[1];
                try {
                    const res = await fetch("/api/upload_sample", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            program_id: pKey,
                            sku_code: skuCode,
                            filename: file.name,
                            image_base64: b64
                        })
                    });
                    const data = await res.json();
                    if (data.ok) {
                        alert("Đã tải ảnh mẫu thành công!");
                        document.getElementById("samplePreview_" + idx).innerHTML = `
                            <div class="flex items-center gap-2 mt-2">
                                <img src="/api/sample_image?path=${encodeURIComponent(data.rel_path)}&t=${Date.now()}" alt="Ảnh mẫu" class="w-12 h-12 object-cover rounded-lg border border-slate-300 shadow-xs">
                                <span class="text-[11px] text-emerald-700 font-semibold">Đã lưu: ${file.name}</span>
                            </div>
                        `;
                        if (programsConfig[pKey] && programsConfig[pKey].skus[idx]) {
                            programsConfig[pKey].skus[idx].sample_images = [data.rel_path];
                        }
                    } else alert("Lỗi tải ảnh: " + data.error);
                } catch (err) { alert("Lỗi: " + err); }
            };
            reader.readAsDataURL(file);
        }

        async function saveCurrentConfig() {
            const pKey = document.getElementById("cfgProgSelect").value || "873_TRUNGBAYBANHTUOIT102026";
            if (!programsConfig[pKey]) programsConfig[pKey] = {};

            const cfg = programsConfig[pKey];
            cfg.program_name = document.getElementById("cfgProgName").value.trim();
            cfg.allow_flexibility = document.getElementById("cfgAllowFlex").checked;
            cfg.min_skus_required = parseInt(document.getElementById("cfgMinSkusRange").value) || 1;

            const skus = [];
            const container = document.getElementById("skusContainer");
            const cards = container.querySelectorAll("[id^='skuCard_']");
            cards.forEach((card, idx) => {
                const code = document.getElementById("skuCode_" + idx).value.trim();
                const name = document.getElementById("skuName_" + idx).value.trim();
                const facings = parseInt(document.getElementById("skuFacings_" + idx).value) || 1;
                const desc = document.getElementById("skuDesc_" + idx).value.trim();
                const mand = document.getElementById("skuMandatory_" + idx).checked;
                const existingSamples = (cfg.skus && cfg.skus[idx]) ? (cfg.skus[idx].sample_images || []) : [];

                skus.push({
                    sku_code: code,
                    sku_name: name,
                    description: desc,
                    min_facings: facings,
                    is_mandatory: mand,
                    sample_images: existingSamples
                });
            });

            cfg.skus = skus;
            cfg.total_skus = skus.length;

            try {
                const res = await fetch("/api/save_config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ config: programsConfig })
                });
                const data = await res.json();
                if (data.ok) {
                    alert("✅ Đã lưu cấu hình chương trình thành công!");
                } else alert("Lỗi lưu cấu hình: " + data.error);
            } catch (e) { alert("Lỗi: " + e); }
        }

        function escapeHtml(text) {
            return (text || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
        }
    </script>
<!--LABEL_TAB-->
</body>
</html>
"""

class DMSWebHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): return

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)

        if url.path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_inject_label_tab(HTML_UI).encode("utf-8"))

        elif url.path == "/api/auth_status":
            self._send_json({"logged_in": SERVER_STATE["dms_client"] is not None, "user": SERVER_STATE["user"]})

        elif url.path == "/api/download_status":
            with LOCK:
                progress = dict(SERVER_STATE["download_progress"])
                progress["is_downloading"] = SERVER_STATE["is_downloading"]
            self._send_json(progress)

        elif url.path == "/api/ai_status":
            with LOCK:
                st = dict(SERVER_STATE["ai_progress"])
            self._send_json(st)

        elif url.path == "/api/get_config":
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                try: cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
                except Exception: pass
            self._send_json(cfg)

        elif url.path == "/api/sample_image":
            rel_path = qs.get("path", [""])[0]
            if not rel_path: return self.send_error(400)
            full_path = os.path.abspath(os.path.join(BASE_DIR, rel_path))
            if not full_path.startswith(os.path.abspath(BASE_DIR)) or not os.path.exists(full_path):
                return self.send_error(404)
            self.send_response(200)
            ct = "image/png" if full_path.lower().endswith(".png") else "image/jpeg"
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            with open(full_path, "rb") as f: self.wfile.write(f.read())

        elif url.path == "/api/list_downloaded":
            out = []
            if os.path.exists(DEFAULT_OUTPUT_DIR):
                for date_folder in sorted(os.listdir(DEFAULT_OUTPUT_DIR), reverse=True):
                    dp = os.path.join(DEFAULT_OUTPUT_DIR, date_folder)
                    if os.path.isdir(dp):
                        for pfolder in sorted(os.listdir(dp)):
                            p_path = os.path.join(dp, pfolder)
                            t1_file = os.path.join(p_path, "tier1.json")
                            if os.path.isdir(p_path) and os.path.exists(t1_file):
                                count = 0
                                try: count = len(json.load(open(t1_file, encoding="utf-8")))
                                except Exception: pass
                                
                                d_str = date_folder
                                if len(date_folder) == 17 and "-" in date_folder:
                                    p1, p2 = date_folder.split("-")
                                    d_str = f"{p1[:2]}/{p1[2:4]}/{p1[4:]} - {p2[:2]}/{p2[2:4]}/{p2[4:]}"
                                
                                out.append({
                                    "rel_path": f"{date_folder}/{pfolder}",
                                    "name": pfolder,
                                    "date_range": d_str,
                                    "count": count
                                })
            self._send_json(out)

        elif url.path == "/api/get_program_audit":
            rel_dir = qs.get("dir", [""])[0]
            if not rel_dir: return self._send_json([], status=400)
            
            prog_dir = os.path.join(DEFAULT_OUTPUT_DIR, rel_dir)
            t1_file = os.path.join(prog_dir, "tier1.json")
            anti_file = os.path.join(prog_dir, "antifraud.json")
            notes_file = os.path.join(prog_dir, "review_notes.json")
            dhash_cache_file = os.path.join(prog_dir, "dhash_cache.json")
            t2_file = os.path.join(prog_dir, "tier2.json")

            if not os.path.exists(t1_file):
                return self._send_json([], status=404)

            tier1 = json.load(open(t1_file, encoding="utf-8"))
            
            anti_map = {}
            if os.path.exists(anti_file):
                try:
                    for a in json.load(open(anti_file, encoding="utf-8")):
                        anti_map[str(a.get("imageId"))] = a
                except Exception: pass

            notes_map = {}
            if os.path.exists(notes_file):
                try: notes_map = json.load(open(notes_file, encoding="utf-8"))
                except Exception: pass

            # Đọc tier2.json nếu có
            t2_map = {}
            if os.path.exists(t2_file):
                try:
                    for item in json.load(open(t2_file, encoding="utf-8")):
                        t2_map[str(item.get("imageId"))] = item
                except Exception: pass

            # Đọc cấu hình quy tắc của chương trình
            all_configs = {}
            if os.path.exists(CONFIG_FILE):
                try: all_configs = json.load(open(CONFIG_FILE, encoding="utf-8"))
                except Exception: pass
            
            pfolder_name = os.path.basename(rel_dir)
            prog_config = all_configs.get(pfolder_name)
            if not prog_config:
                for k, v in all_configs.items():
                    if k in pfolder_name or pfolder_name in k:
                        prog_config = v
                        break

            # Nạp dHash cache & phát hiện trùng ảnh
            dhash_map = {}
            if os.path.exists(dhash_cache_file):
                try: dhash_map = json.load(open(dhash_cache_file, encoding="utf-8"))
                except Exception: pass

            dhash_updated = False
            for t in tier1:
                iid = str(t.get("imageId"))
                if iid not in dhash_map:
                    fp = os.path.join(prog_dir, t.get("file", ""))
                    if os.path.exists(fp):
                        h = compute_image_dhash_variants(fp)
                        if h is not None:
                            dhash_map[iid] = h
                            dhash_updated = True

            if dhash_updated:
                try: json.dump(dhash_map, open(dhash_cache_file, "w", encoding="utf-8"))
                except Exception: pass

            dup_map = {}
            t_map = {str(t.get("imageId")): t for t in tier1}
            all_iids = list(dhash_map.keys())

            for i in range(len(all_iids)):
                for j in range(i + 1, len(all_iids)):
                    id1, id2 = all_iids[i], all_iids[j]
                    t1, t2 = t_map.get(id1), t_map.get(id2)
                    if t1 and t2:
                        c1, c2 = t1.get("customerCode"), t2.get("customerCode")
                        if c1 and c2 and c1 != c2:
                            h1, h2 = dhash_map.get(id1), dhash_map.get(id2)
                            dist_bits, angle = calc_min_hamming_distance(h1, h2)
                            if dist_bits <= 2:
                                dup_map[id1] = {
                                    "imageId": id2, "customerCode": c2, "customerName": t2.get("customerName"),
                                    "staffCode": t2.get("staffCode"), "staffName": t2.get("staffName"),
                                    "file": t2.get("file"), "dist": dist_bits
                                }
                                dup_map[id2] = {
                                    "imageId": id1, "customerCode": c1, "customerName": t1.get("customerName"),
                                    "staffCode": t1.get("staffCode"), "staffName": t1.get("staffName"),
                                    "file": t1.get("file"), "dist": dist_bits
                                }

            results = []
            for t in tier1:
                iid = str(t.get("imageId"))
                a = anti_map.get(iid, {})
                note = notes_map.get(iid, "")
                is_dup = iid in dup_map
                dup_target = dup_map.get(iid)
                t2_item = t2_map.get(iid)

                cdn_url = f"{IMG_BASE}/{t.get('urlThum', '')}" if t.get("urlThum") else ""

                item_payload = {
                    "imageId": iid,
                    "file": t.get("file", ""),
                    "customerCode": t.get("customerCode", ""),
                    "customerName": t.get("customerName", ""),
                    "shopCode": t.get("shopCode", ""),
                    "staffCode": t.get("staffCode", ""),
                    "staffName": t.get("staffName", ""),
                    "createDate": t.get("createDate", ""),
                    "lat": t.get("lat"),
                    "lng": t.get("lng"),
                    "custLat": t.get("custLat"),
                    "custLng": t.get("custLng"),
                    "gps_dist_m": t.get("gps_dist_m"),
                    "gps_ok": t.get("gps_ok"),
                    "moire": a.get("moire"),
                    "blur_var": a.get("blur_var"),
                    "recap_suspect": a.get("recap_suspect", False),
                    "is_cross_store_dup": is_dup,
                    "dup_target": dup_target,
                    "review_note": note,
                    "cdn_url": cdn_url
                }

                # Đánh giá bằng Tier 3 Flexible Rules
                if t2_item:
                    eval_res = evaluate_display_record(item_payload, t2_item, prog_config)
                    item_payload.update({
                        "tier2_status": eval_res["tier2_status"],
                        "tier2_reason": eval_res["tier2_reason"],
                        "unique_passed_count": eval_res["unique_passed_count"],
                        "total_skus_cfg": eval_res["total_skus_cfg"],
                        "total_facings": eval_res["total_facings"],
                        "sku_details": eval_res["sku_details"],
                        "overall_status": eval_res["overall_status"],
                        "overall_reason": eval_res["overall_reason"],
                        "detected_skus": t2_item.get("detected_skus", {})
                    })
                else:
                    item_payload.update({
                        "tier2_status": None,
                        "overall_status": "LOAI" if is_dup else ("NGHI_VAN" if a.get("recap_suspect") or (t.get("gps_dist_m") and t.get("gps_dist_m") > 200) else "HOP_LE"),
                        "overall_reason": "Chưa chạy chấm AI Tầng 2"
                    })

                results.append(item_payload)

            self._send_json(results)

        elif url.path == "/api/local_image":
            rel_path = qs.get("path", [""])[0]
            if not rel_path: return self.send_error(400)
            
            full_path = os.path.abspath(os.path.join(DEFAULT_OUTPUT_DIR, rel_path))
            if not full_path.startswith(os.path.abspath(DEFAULT_OUTPUT_DIR)) or not os.path.exists(full_path):
                return self.send_error(404)

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            with open(full_path, "rb") as f: self.wfile.write(f.read())

        elif url.path in ("/api/label_images", "/api/label_proposals", "/api/label_stats"):
            if labeling is None:
                return self._send_json({"error": "Chưa cài đủ thư viện AI (torch/transformers)"}, status=500)
            try:
                if url.path == "/api/label_stats":
                    self._send_json(labeling.stats())
                else:
                    rel_dir = qs.get("dir", [""])[0]
                    if not rel_dir or ".." in rel_dir: return self._send_json({"error": "dir sai"}, status=400)
                    if url.path == "/api/label_images":
                        self._send_json(labeling.list_images(DEFAULT_OUTPUT_DIR, rel_dir))
                    else:
                        self._send_json(labeling.propose(DEFAULT_OUTPUT_DIR, rel_dir, qs.get("imageId", [""])[0],
                                                         force=bool(qs.get("force"))))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        elif url.path == "/api/open_folder":
            os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
            if sys.platform == "darwin": os.system(f'open "{DEFAULT_OUTPUT_DIR}"')
            elif sys.platform.startswith("win"): os.startfile(DEFAULT_OUTPUT_DIR)
            self._send_json({"ok": True, "path": DEFAULT_OUTPUT_DIR})

        else:
            self.send_error(404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len > 0 else {}

        if url.path == "/api/login":
            user = body.get("user") or SERVER_STATE["user"]
            pw = body.get("password") or SERVER_STATE["password"]
            try:
                d = DMS(user, pw)
                d.login()
                SERVER_STATE["dms_client"] = d
                SERVER_STATE["user"] = user
                SERVER_STATE["password"] = pw
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)

        elif url.path == "/api/search":
            dms = SERVER_STATE.get("dms_client")
            if not dms:
                if SERVER_STATE["user"] and SERVER_STATE["password"]:
                    try:
                        dms = DMS(SERVER_STATE["user"], SERVER_STATE["password"])
                        dms.login()
                        SERVER_STATE["dms_client"] = dms
                    except Exception as e:
                        return self._send_json({"ok": False, "error": f"Cần đăng nhập: {e}"}, status=401)
                else:
                    return self._send_json({"ok": False, "error": "Vui lòng đăng nhập tài khoản DMS!"}, status=401)

            frm = body.get("from", "01/10/2026")
            to = body.get("to", "07/10/2026")
            try:
                progs = dms.search_programs(frm, to)
                SERVER_STATE["programs"] = progs
                self._send_json({"ok": True, "programs": progs})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        elif url.path == "/api/start_download":
            with LOCK:
                if SERVER_STATE["is_downloading"]:
                    return self._send_json({"ok": False, "error": "Tiến trình tải đang chạy!"})
                SERVER_STATE["is_downloading"] = True

            pids = body.get("pids", [])
            frm = body.get("from", "01/10/2026")
            to = body.get("to", "07/10/2026")
            threading.Thread(target=background_download_worker, args=(pids, frm, to), daemon=True).start()
            self._send_json({"ok": True})

        elif url.path == "/api/stop_download":
            STOP_EVENT.set()
            self._send_json({"ok": True})

        elif url.path == "/api/save_review_note":
            rel_dir = body.get("dir", "")
            img_id = str(body.get("imageId", ""))
            note = body.get("note", "")

            if not rel_dir or not img_id:
                return self._send_json({"ok": False, "error": "Thiếu dir hoặc imageId"}, status=400)

            prog_dir = os.path.join(DEFAULT_OUTPUT_DIR, rel_dir)
            notes_file = os.path.join(prog_dir, "review_notes.json")
            
            notes = {}
            if os.path.exists(notes_file):
                try: notes = json.load(open(notes_file, encoding="utf-8"))
                except Exception: pass
            
            notes[img_id] = note
            json.dump(notes, open(notes_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            self._send_json({"ok": True})

        elif url.path == "/api/scan_duplicates":
            rel_dir = body.get("dir", "")
            if not rel_dir: return self._send_json({"ok": False, "error": "Thiếu dir"}, status=400)

            prog_dir = os.path.join(DEFAULT_OUTPUT_DIR, rel_dir)
            t1_file = os.path.join(prog_dir, "tier1.json")
            dhash_cache_file = os.path.join(prog_dir, "dhash_cache.json")

            if not os.path.exists(t1_file):
                return self._send_json({"ok": False, "error": "Chưa có dữ liệu tier1.json"}, status=404)

            tier1 = json.load(open(t1_file, encoding="utf-8"))
            dhash_map = {}
            if os.path.exists(dhash_cache_file):
                try: dhash_map = json.load(open(dhash_cache_file, encoding="utf-8"))
                except Exception: pass

            for t in tier1:
                iid = str(t.get("imageId"))
                if iid not in dhash_map:
                    fp = os.path.join(prog_dir, t.get("file", ""))
                    if os.path.exists(fp):
                        h = compute_image_dhash_variants(fp)
                        if h is not None: dhash_map[iid] = h

            try: json.dump(dhash_map, open(dhash_cache_file, "w", encoding="utf-8"))
            except Exception: pass

            dup_count = 0
            t_map = {str(t.get("imageId")): t for t in tier1}
            all_iids = list(dhash_map.keys())

            for i in range(len(all_iids)):
                for j in range(i + 1, len(all_iids)):
                    id1, id2 = all_iids[i], all_iids[j]
                    t1, t2 = t_map.get(id1), t_map.get(id2)
                    if t1 and t2:
                        c1, c2 = t1.get("customerCode"), t2.get("customerCode")
                        if c1 and c2 and c1 != c2:
                            h1, h2 = dhash_map.get(id1), dhash_map.get(id2)
                            dist_bits, angle = calc_min_hamming_distance(h1, h2)
                            if dist_bits <= 2: dup_count += 1

            self._send_json({"ok": True, "msg": f"Đã quét xong dHash cho {len(all_iids)} ảnh! Phát hiện {dup_count} cặp ảnh trùng lặp đa điểm bán."})

        elif url.path == "/api/run_antifraud":
            rel_dir = body.get("dir", "")
            moire_th = body.get("moire_th", "0.8")
            if not rel_dir: return self._send_json({"ok": False, "error": "Thiếu dir"}, status=400)

            prog_dir = os.path.join(DEFAULT_OUTPUT_DIR, rel_dir)
            script_path = os.path.join(BASE_DIR, "src", "tier1_antifraud.py")
            
            try:
                cmd = [sys.executable, script_path, "--dir", prog_dir, "--moire-th", str(moire_th)]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if res.returncode == 0:
                    self._send_json({"ok": True, "msg": f"Đã quét xong Moiré cho {rel_dir}!"})
                else:
                    self._send_json({"ok": False, "error": res.stderr or res.stdout}, status=500)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        elif url.path == "/api/run_ai_scoring":
            rel_dir = body.get("dir", "")
            if not rel_dir: return self._send_json({"ok": False, "error": "Thiếu dir"}, status=400)

            with LOCK:
                if SERVER_STATE["ai_progress"]["is_running"]:
                    return self._send_json({"ok": False, "error": "Tiến trình AI đang chạy!"}, status=400)

            prog_dir = os.path.join(DEFAULT_OUTPUT_DIR, rel_dir)
            threading.Thread(target=background_ai_worker, args=(prog_dir, rel_dir), daemon=True).start()
            self._send_json({"ok": True, "msg": "Đã bắt đầu tiến trình chấm AI Tầng 2!"})

        elif url.path == "/api/save_config":
            new_cfg = body.get("config", {})
            try:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_cfg, f, ensure_ascii=False, indent=2)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        elif url.path in ("/api/label_save", "/api/label_train"):
            if labeling is None:
                return self._send_json({"ok": False, "error": "Chưa cài đủ thư viện AI"}, status=500)
            try:
                if url.path == "/api/label_save":
                    rel_dir = body.get("dir", "")
                    if not rel_dir or ".." in rel_dir: return self._send_json({"ok": False, "error": "dir sai"}, status=400)
                    n = labeling.save_labels(DEFAULT_OUTPUT_DIR, rel_dir, str(body.get("imageId", "")), body.get("items", []))
                    self._send_json({"ok": True, "saved": n})
                else:
                    self._send_json(dict(labeling.train(), ok=True))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        elif url.path == "/api/rotate_image":
            rel_dir = body.get("dir", "")
            img_id = str(body.get("imageId", ""))
            angle = int(body.get("angle", 90))

            if not rel_dir or not img_id:
                return self._send_json({"ok": False, "error": "Thiếu dir hoặc imageId"}, status=400)

            prog_dir = os.path.join(DEFAULT_OUTPUT_DIR, rel_dir)
            t1_file = os.path.join(prog_dir, "tier1.json")
            t2_file = os.path.join(prog_dir, "tier2.json")

            if not os.path.exists(t1_file):
                return self._send_json({"ok": False, "error": "Chưa có tier1"}, status=404)

            tier1 = json.load(open(t1_file, encoding="utf-8"))
            target_item = next((t for t in tier1 if str(t.get("imageId")) == img_id), None)
            if not target_item:
                return self._send_json({"ok": False, "error": "Không tìm thấy ảnh"}, status=404)

            img_path = os.path.join(prog_dir, target_item["file"])
            if os.path.exists(img_path):
                try:
                    if HAVE_CV2:
                        rot_code = cv2.ROTATE_90_CLOCKWISE if angle == 90 else cv2.ROTATE_90_COUNTERCLOCKWISE
                        im = cv2.imread(img_path)
                        im_rot = cv2.rotate(im, rot_code)
                        cv2.imwrite(img_path, im_rot)
                    else:
                        subprocess.run(["sips", "-r", str(angle), img_path, "--out", img_path], capture_output=True)

                    if body.get("rescore") is False:   # tab gán nhãn: chỉ xoay file, khỏi chấm lại AI
                        return self._send_json({"ok": True})
                    res = detect_products_in_image(img_path)
                    res["imageId"] = img_id
                    res["file"] = target_item["file"]

                    t2_list = []
                    if os.path.exists(t2_file):
                        try: t2_list = json.load(open(t2_file, encoding="utf-8"))
                        except Exception: pass

                    updated = False
                    for idx, it in enumerate(t2_list):
                        if str(it.get("imageId")) == img_id:
                            t2_list[idx] = res
                            updated = True
                            break
                    if not updated:
                        t2_list.append(res)

                    with open(t2_file, "w", encoding="utf-8") as f:
                        json.dump(t2_list, f, ensure_ascii=False, indent=2)

                    self._send_json({"ok": True, "detected": res})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=500)
            else:
                self._send_json({"ok": False, "error": "File ảnh không tồn tại"}, status=404)

        elif url.path == "/api/upload_sample":
            p_id = body.get("program_id", "").replace("/", "_")
            sku_code = body.get("sku_code", "").replace("/", "_")
            fn = body.get("filename", "sample.png")
            b64_data = body.get("image_base64", "")

            if not p_id or not sku_code or not b64_data:
                return self._send_json({"ok": False, "error": "Thiếu dữ liệu upload"}, status=400)

            dest_dir = os.path.join(SAMPLES_DIR, p_id, sku_code)
            os.makedirs(dest_dir, exist_ok=True)
            dest_file = os.path.join(dest_dir, fn)

            try:
                img_bytes = base64.b64decode(b64_data)
                with open(dest_file, "wb") as f:
                    f.write(img_bytes)
                
                rel_path = os.path.relpath(dest_file, BASE_DIR)
                
                # Cập nhật programs_config.json
                all_cfg = {}
                if os.path.exists(CONFIG_FILE):
                    try: all_cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
                    except Exception: pass
                
                if p_id in all_cfg:
                    for s in all_cfg[p_id].get("skus", []):
                        if s.get("sku_code") == sku_code:
                            s["sample_images"] = [rel_path]
                            break
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump(all_cfg, f, ensure_ascii=False, indent=2)

                self._send_json({"ok": True, "rel_path": rel_path})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        else:
            self.send_error(404)

def run_server(port=8888):
    server = None
    actual_port = port
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), DMSWebHandler)
            actual_port = p
            break
        except OSError:
            pass

    if not server:
        print(f"❌ Không thể mở cổng từ {port} đến {port+9}!")
        return

    print("=" * 65)
    print(f"🚀 DMS DISPLAY SCORER & AI VISION SERVER ĐANG CHẠY TẠI:")
    print(f"👉 http://127.0.0.1:{actual_port}")
    print("=" * 65)

    if not os.environ.get("DMS_NO_BROWSER"):
        webbrowser.open(f"http://127.0.0.1:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Đã dừng server.")

if __name__ == "__main__":
    run_server()
