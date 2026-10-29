"""Regression tests for bodfetch. Run:  python tests/test_fetch.py   (or: pytest tests/)

1. test_plan_matches_uat_folder   - generated filenames for 2026-10-27 == the 53 files really in
                                    /home/greek/Desktop/contract on UAT that day (ground truth).
2. test_local_server              - fetch logic against a fake local HTTP server (no internet needed):
                                    404, HTML error page, empty file, stale zip, zip candidate dates,
                                    optional files, idempotent re-run, atomic writes.
3. test_browser_session           - headless Chromium + cookie-protected URL (skipped if Playwright is missing).
"""
import datetime as dt
import io
import json
import pathlib
import shutil
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402

import bodfetch as b  # noqa: E402

T, P = dt.date(2026, 10, 27), dt.date(2026, 10, 26)


# ---------------------------------------------------------------- 1. filename parity with UAT
def test_plan_matches_uat_folder():
    cfg = yaml.safe_load((ROOT / "manifest.yaml").read_text())
    uat = set((pathlib.Path(__file__).parent / "uat_listing_2026-10-27.txt").read_text().split())
    keep = cfg["settings"]["keep_p_date"]
    mine = {b.name_rule(b.render(e["name"], T, P), T, P, keep) for e in cfg["files"] if e.get("grp") not in ("mcx", "slbm")}
    assert mine == uat, (sorted(uat - mine), sorted(mine - uat))
    ids = {e["id"] for e in cfg["files"] if isinstance(e["id"], int)}
    assert ids == set(range(1, 63)), "every Sr.No. 1..62 from the sheet must be in the manifest"
    assert {e["id"] for e in b.choose_entries(cfg, "sheet")} == ids  # --only sheet = exactly the 62 rows


def test_name_rule():
    keep = yaml.safe_load((ROOT / "manifest.yaml").read_text())["settings"]["keep_p_date"]
    rule = lambda n: b.name_rule(n, T, P, keep)
    assert rule("DailyIndexFile26102026.csv") == "DailyIndexFile27102026.csv"   # P -> T
    assert rule("EQD_CC_CO261026.csv") == "EQD_CC_CO271026.csv"                 # ddmmyy too
    assert rule("BSERISK20261026-00.spn") == "BSERISK20261027-00.spn"           # yyyymmdd too
    assert rule("NSE_FO_contract_27102026.csv") == "NSE_FO_contract_27102026.csv"  # already T
    assert rule("series_change.csv") == "series_change.csv"                     # no date
    for kept in ("BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv", "Bhavcopy_MCX_CO_0_0_0_20261026_F_0000.csv",
                 "Pd261026.csv", "2610SPD0000.md"):
        assert rule(kept) == kept                                               # the three exceptions keep P
    assert b.name_rule("X_26102026.csv", T, P, None) == "X_26102026.csv"        # rule off without settings


def test_crlf():
    srv, U = _server()
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        r = b.Run({}, T, P, tmp / "o")
        assert r.fetch({"id": 1, "name": "v.dat", "type": "http", "url": U + "/ok.csv", "crlf": True})[0] == "OK"
        assert (tmp / "o" / "v.dat").read_bytes() == b"a,b\r\n1,2\r\n"
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_holiday_calendar():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "holidays.txt"
    tmp.write_text("2026-03-03  # extra closure # manual\n")
    cal = b.Calendar(tmp)
    fake = lambda: [(dt.date(2026, 10, 20), "Diwali"), (dt.date(2026, 10, 24), "Saturday one")]
    assert cal.ensure_years({2026}, fetch=fake) == []
    assert cal.holidays == {dt.date(2026, 3, 3), dt.date(2026, 10, 20)}   # manual kept, weekend dropped
    assert cal.prev_before(dt.date(2026, 10, 21)) == dt.date(2026, 10, 19)  # skips the holiday
    assert b.Calendar(tmp).ensure_years({2026}, fetch=lambda: 1 / 0) == []  # already filled: no fetch
    assert b.Calendar(tmp).ensure_years({2027}, fetch=fake)[0].startswith("WARNING: NSE has not published")
    assert "2026-03-03" in tmp.read_text() and "# year 2026 from NSE" in tmp.read_text()


# ---------------------------------------------------------------- 2. fetch logic vs fake server
def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in members.items():
            z.writestr(n, d)
    return buf.getvalue()


ROUTES = {
    "/ok.csv": (200, b"a,b\n1,2\n"),
    "/missing.csv": (404, b"nope"),
    "/errpage.csv": (200, b"<!DOCTYPE html><html><body>Error</body></html>"),
    "/empty.csv": (200, b""),
    "/fresh.zip": (200, _zip({"DP271026": b"dp-data"})),
    "/stale.zip": (200, _zip({"DP261026": b"old"})),
    "/VAR261026.zip": (200, _zip({"VAR271026.xls": b"var-data"})),
    "/notzip.zip": (200, b"<html>maintenance</html>"),
    "/prevday.zip": (200, _zip({"SUB/CONTRACT_26102026.csv": b"p-data"})),   # BSE style: dated P
    "/older.zip": (200, _zip({"CONTRACT_23102026.csv": b"too-old"})),       # older than P: reject
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/member.csv":
            ok = "sid=1" in (self.headers.get("Cookie") or "")
            return self._send(200, b"member,data\n1,2\n" if ok else b"<html><body>Please login</body></html>")
        self._send(*ROUTES.get(self.path, (404, b"nf")))

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _files(U):
    return [
        {"id": 1, "name": "ok.csv", "type": "http", "url": U + "/ok.csv"},
        {"id": 2, "name": "missing.csv", "type": "http", "url": U + "/missing.csv"},
        {"id": 3, "name": "errpage.csv", "type": "http", "url": U + "/errpage.csv"},
        {"id": 4, "name": "empty.csv", "type": "http", "url": U + "/empty.csv"},
        {"id": 5, "name": "DP{T:%d%m%y}", "type": "zip", "url": U + "/fresh.zip"},
        {"id": 6, "name": "stale", "type": "zip", "member": "DP{T:%d%m%y}", "url": U + "/stale.zip"},
        {"id": 7, "name": "VAR{T:%d%m%y}.xls", "type": "zip", "url": U + "/VAR{D:%d%m%y}.zip", "url_dates": ["T", "P"]},
        {"id": 8, "name": "opt.csv", "type": "http", "optional": True, "url": U + "/missing.csv"},
        {"id": 9, "name": "man.txt", "type": "manual"},
        {"id": 10, "name": "nourl.csv", "type": "session"},
        {"id": 11, "name": "member.csv", "type": "session", "url": U + "/member.csv"},
        {"id": 12, "name": "notzip.csv", "type": "zip", "url": U + "/notzip.zip"},
        {"id": 13, "name": "allow_empty.txt", "type": "http", "allow_empty": True, "url": U + "/empty.csv"},
        {"id": 14, "name": "CONTRACT_{D:%d%m%Y}.csv", "type": "zip", "member_dates": ["T", "P"], "url": U + "/prevday.zip"},
        {"id": 15, "name": "CONTRACT_{D:%d%m%Y}.csv", "type": "zip", "member_dates": ["T", "P"], "url": U + "/older.zip"},
    ]


def test_local_server():
    srv, U = _server()
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        def run():
            r = b.Run({}, T, P, tmp / "o")
            return {e["id"]: r.fetch(e)[0] for e in _files(U)}
        first = run()
        assert first == {1: "OK", 2: "NOT_PUBLISHED", 3: "NOT_PUBLISHED", 4: "FAILED", 5: "OK", 6: "NOT_PUBLISHED",
                         7: "OK", 8: "OPTIONAL_ABSENT", 9: "MANUAL", 10: "NEEDS_URL", 11: "NEEDS_LOGIN",
                         12: "NOT_PUBLISHED", 13: "OK", 14: "OK", 15: "NOT_PUBLISHED"}, first
        assert (tmp / "o" / "VAR271026.xls").read_bytes() == b"var-data"  # found via candidate date P
        assert (tmp / "o" / "CONTRACT_26102026.csv").read_bytes() == b"p-data"  # member_dates fallback to P
        assert not list((tmp / "o").glob("*.part"))
        second = run()
        assert {k for k, v in second.items() if v == "CACHED"} == {1, 5, 7, 13, 14}  # re-run never re-downloads
        state = json.loads((tmp / "o" / "state.json").read_text())
        assert all("sha256" in v for v in state.values())
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 3. real browser, cookie session
def test_browser_session():
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("  (skipped: playwright not installed)")
        return
    srv, U = _server()
    tmp = pathlib.Path(tempfile.mkdtemp())
    e = {"id": 11, "name": "member.csv", "type": "session", "url": U + "/member.csv"}
    try:
        br = b.Browser(tmp / "prof", headless=True, probe_url=U + "/member.csv")
        assert br.logged_in() is False                       # login page body -> logged out
        r = b.Run({}, T, P, tmp / "o"); r.browser = br
        assert r.fetch(e)[0] == "NEEDS_LOGIN"
        br.ctx.add_cookies([{"name": "sid", "value": "1", "url": U}])
        assert br.logged_in() is True
        assert r.fetch(e)[0] == "OK"
        br.close()
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in (test_plan_matches_uat_folder, test_name_rule, test_crlf, test_holiday_calendar, test_local_server,
               test_browser_session):
        fn()
        print("PASS", fn.__name__)
