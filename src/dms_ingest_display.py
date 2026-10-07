#!/usr/bin/env python3
"""Ingest anh trung bay DMS.ONE (huunghiv2.dmsone.vn) — da chuong trinh, tu dong.

Flow: login CAS -> POST /images/search (liet ke program trong date-range)
  -> loop tung displayProgrameId, paginate POST /images/addAlbumSelect
  -> luu JSON records + tai anh full ve disk.
Anh direct http://huunghiv2.dmsone.vn:8080/huunghi/<urlImage> (khong auth).

Dung:
  DMS_PASSWORD='...' python dms_ingest_display.py --from 01/10/2026 --to 07/10/2026
    [--out D:/data/dms_display] [--download] [--max 20]
  Chay lan 2 chi tai record imageId chua co (incremental theo JSON da luu).

Env: DMS_USER (mac dinh tuannm), DMS_PASSWORD (bat buoc).
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://huunghiv2.dmsone.vn"
IMG_BASE = "http://huunghiv2.dmsone.vn:8080/huunghi"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


class DMS:
    def __init__(self, user, password):
        self.user, self.password = user, password
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def _post(self, path, data, timeout=60):
        req = urllib.request.Request(
            BASE + path, data=urllib.parse.urlencode(data).encode(),
            headers={**UA, "X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "Referer": BASE + "/images/info", "Origin": BASE})
        with self.op.open(req, timeout=timeout) as r:
            body = r.read()
        # search tra HTML, addAlbumSelect/get-images-for-popup tra JSON
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
        with self.op.open(req, timeout=30):
            pass
        # Hoan tat context nhu flow catalog: ghe /home + /index.jsp
        # (bo qua = POST /images/search bi da 550 khong quyen)
        for p in ("/home", "/index.jsp"):
            try:
                req = urllib.request.Request(BASE + p, headers=UA)
                with self.op.open(req, timeout=30):
                    pass
            except Exception:
                pass
        # Khoi tao context nhu trinh duyet: cac GET nay mo khoa POST search
        for p in ("/images/displayPrograme/getListCTTBbyShopId",
                  "/images/getListStaffForShop",
                  "/images/displayPrograme/getListCTTBbyListShop",
                  "/images/loadListProgramStatistic"):
            try:
                req = urllib.request.Request(BASE + p, headers={
                    **UA, "X-Requested-With": "XMLHttpRequest",
                    "Referer": BASE + "/images/info"})
                with self.op.open(req, timeout=30):
                    pass
            except Exception:
                pass

    def search_programs(self, frm, to):
        """POST /images/search -> parse HTML album list thanh [(id, code, count)]."""
        html = self._post("/images/search", {
            "tuyen": "-1", "fromDate": frm, "toDate": to,
            "customerCode": "", "customerNameOrAddress": "",
            "objectType": "4", "statusRes": "-2"})
        assert isinstance(html, str), "search khong tra HTML (het session?)"
        out = []
        for m in re.finditer(
                r"showAlbumDetail\((\d+)\);?\">([^<]+)</a></p>\s*<p[^>]*>([\d.,]+)\s*hình ảnh",
                html):
            pid, code = m.group(1), m.group(2).strip()
            out.append({"displayProgrameId": pid, "code": code,
                        "count": int(m.group(3).replace(".", "").replace(",", ""))})
        return out

    def fetch_program(self, pid, frm, to, per_page=40):
        """Paginate get-images-for-popup (page tu 0, du lieu du nhat).
        HAR + verify 07/10: addAlbumSelect chi tra 67/87, popup tra du 87."""
        recs, page, seen = [], 0, set()
        while True:
            j = self._post("/images/get-images-for-popup", {
                "tuyen": "-1", "fromDate": frm, "toDate": to,
                "customerCode": "", "customerNameOrAddress": "",
                "objectType": "4", "statusRes": "-2",
                "displayProgrameId": pid, "page": page, "max": per_page})
            assert isinstance(j, dict), f"page {page} khong tra JSON (het session?)"
            batch = j.get("lstImage", [])
            for r in batch:
                if r.get("imageId") not in seen:
                    seen.add(r.get("imageId"))
                    recs.append(r)
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


class _Redirect(Exception):
    def __init__(self, location):
        self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirect(headers.get("Location", newurl))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "displays"),
        help="thu muc output (mac dinh: <project>/data/displays, da gitignore)")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--program", default=None, help="chi quet 1 displayProgrameId")
    a = ap.parse_args(argv)
    user = os.environ.get("DMS_USER", "tuannm")
    pw = os.environ.get("DMS_PASSWORD", "")
    if not pw:
        sys.exit("thieu DMS_PASSWORD")
    d = DMS(user, pw)
    d.login()
    print("login ok")
    progs = d.search_programs(a.frm, a.to)
    if a.program:
        hit = [p for p in progs
               if a.program == p["displayProgrameId"]
               or a.program in p.get("code", "")]
        if hit:
            progs = hit
        elif a.program.isdigit():
            progs = [{"displayProgrameId": a.program, "code": a.program, "count": "?"}]
        else:
            sys.exit(f"khong thay program '{a.program}' trong range {a.frm}-{a.to}")
    print(f"programs: {len(progs)}")
    for p in progs:
        pid = p["displayProgrameId"]
        pcode = p.get("code", pid)
        clean_code = pcode.split(" - ")[0].strip() if " - " in pcode else pcode.strip()
        clean_code = re.sub(r'[\/*?:"<>|\s]', "_", clean_code).strip("_")
        folder_name = f"{pid}_{clean_code}" if clean_code else str(pid)
        outdir = os.path.join(a.out, f"{a.frm.replace('/','')}-{a.to.replace('/','')}", folder_name)
        old_dir = os.path.join(a.out, f"{a.frm.replace('/','')}-{a.to.replace('/','')}", pid)
        if os.path.exists(old_dir) and not os.path.exists(outdir):
            try:
                os.rename(old_dir, outdir)
            except Exception:
                pass
        os.makedirs(outdir, exist_ok=True)
        jf = os.path.join(outdir, "records.json")
        seen = set()
        if os.path.exists(jf):
            seen = {r.get("imageId") for r in json.load(open(jf, encoding="utf-8"))}
        recs = d.fetch_program(pid, a.frm, a.to, a.max)
        new = [r for r in recs if r.get("imageId") not in seen]
        json.dump(recs, open(jf, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[{pid}] {p.get('code')} total={len(recs)} new={len(new)}")
        if a.download:
            import math
            def hav(lat1, lon1, lat2, lon2):
                try:
                    R = 6371000
                    p1, p2 = math.radians(lat1), math.radians(lat2)
                    dp = math.radians(lat2 - lat1)
                    dl = math.radians(lon2 - lon1)
                    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
                    return round(2 * R * math.asin(math.sqrt(h)), 1)
                except (TypeError, ValueError):
                    return None
            tier1, dl_count = [], 0
            for r in recs:
                up = (r.get("urlImage") or "").replace("\\", "/")
                fn = os.path.basename(up) if up else f"{r.get('imageId')}.jpg"
                fp = os.path.join(outdir, "full", fn)
                if up and not os.path.exists(fp):
                    try:
                        d.download(up, fp)
                        dl_count += 1
                    except Exception as e:
                        print(f"  fail {fn}: {str(e)[:100]}")
                dist = hav(r.get("lat"), r.get("lng"), r.get("custLat"), r.get("custLng"))
                tier1.append({
                    "imageId": r.get("imageId"), "file": f"full/{fn}",
                    "customerCode": r.get("customerCode"), "customerName": r.get("customerName"),
                    "shopCode": r.get("shopCode"), "staffCode": r.get("staffCode"),
                    "staffName": r.get("staffName"), "displayProgrameId": r.get("displayProgrameId"),
                    "createDate": r.get("createDate"),
                    "lat": r.get("lat"), "lng": r.get("lng"),
                    "custLat": r.get("custLat"), "custLng": r.get("custLng"),
                    "gps_dist_m": dist, "gps_ok": (dist is not None and dist <= 100),
                    "urlThum": (r.get("urlThum") or "").replace("\\", "/"),
                })
            json.dump(tier1, open(os.path.join(outdir, "tier1.json"), "w", encoding="utf-8"), ensure_ascii=False)
            bad = sum(1 for t in tier1 if not t["gps_ok"])
            print(f"  downloaded {dl_count} imgs, tier1.json: {len(tier1)} recs, gps_fail(>100m)={bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
