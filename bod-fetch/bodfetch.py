#!/usr/bin/env python3
"""bodfetch - fetch the daily BOD exchange/contract files into a dated staging folder.

    python bodfetch.py plan              # list the files expected for the BOD date (no network)
    python bodfetch.py fetch             # fetch what is published and not fetched yet
    python bodfetch.py fetch --only sheet   # only the 62 rows of the GreekSoft sheet
    python bodfetch.py browser           # open the login browser; it stays open, fetch/record attach to it
    python bodfetch.py fetch --login     # alternative: one-shot browser inside this run (see README)
    python bodfetch.py fetch --watch --interval 20 --until 07:00
    python bodfetch.py record            # human browses/downloads; we log the URLs to put in manifest.yaml
    python bodfetch.py login mcx         # test the MCX login from .env (plain HTTP, no browser)
    python bodfetch.py push --dry-run    # copy the day's files to the UAT server (see bodpush.py)

Design rules
  * manifest.yaml is the single source of truth: one row per file, nothing hard-coded here.
  * Safe to re-run any time: valid files are never downloaded twice, writes are atomic (.part -> rename).
  * A file is only accepted if it is real: not empty, not an HTML error page, and (for zip sources)
    the dated member for THIS BOD date is inside the zip -- a stale "latest" zip is never accepted.
  * Nothing here touches UAT. It only writes to the local staging folder.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import gzip
import html
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import zipfile

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = pathlib.Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

DONE = {"OK", "CACHED", "OPTIONAL_ABSENT"}


class Skip(Exception):
    """Not an error in the code: the file just isn't obtainable right now."""

    def __init__(self, status: str, detail: str = ""):
        super().__init__(detail)
        self.status, self.detail = status, detail


# --------------------------------------------------------------------------- calendar
HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"


def fetch_nse_holidays() -> list[tuple[dt.date, str]]:
    """NSE's public trading-holiday list for the equity (CM) segment. No login: the API only needs the
    cookie NSE sets on its homepage (the homepage itself may answer 403 while setting it)."""
    s = make_http()
    try:
        s.get("https://www.nseindia.com/", timeout=20)
    except requests.RequestException:
        pass
    r = s.get(HOLIDAY_API, headers={"Referer": "https://www.nseindia.com/resources/exchange-communication-holidays"},
              timeout=30)
    r.raise_for_status()
    return [(dt.datetime.strptime(h["tradingDate"], "%d-%b-%Y").date(), h.get("description", "").strip())
            for h in r.json()["CM"]]


class Calendar:
    """Weekdays minus the holidays in holidays.txt.

    The file is filled automatically from NSE's public holiday list (CM segment), one block per year
    ("# year 2026 from NSE ..."). Lines ending in "# manual" are your own additions and survive a refresh.
    """

    def __init__(self, holidays_file: pathlib.Path):
        self.path = holidays_file
        self.holidays: set[dt.date] = set()
        self.years: set[int] = set()       # years already filled from NSE
        self._read()

    def _read(self):
        self.holidays.clear()
        self.years.clear()
        if not self.path.exists():
            return
        for raw in self.path.read_text().splitlines():
            m = re.match(r"#\s*year (\d{4}) from NSE", raw.strip())
            if m:
                self.years.add(int(m[1]))
                continue
            line = raw.split("#")[0].strip()
            if line:
                self.holidays.add(dt.date.fromisoformat(line))

    def ensure_years(self, years: set[int], fetch=fetch_nse_holidays) -> list[str]:
        """Make sure every year in `years` is filled; fetch from NSE if not. Returns warnings."""
        missing = sorted(y for y in years if y not in self.years)
        if not missing:
            return []
        try:
            got = fetch()
        except Exception as ex:
            return [f"WARNING: could not fetch NSE holidays for {missing} ({type(ex).__name__}); "
                    "only weekends count as non-trading days for those years."]
        new_years = {d.year for d, _ in got}
        manual, auto = [], {}
        if self.path.exists():
            year = None
            for raw in self.path.read_text().splitlines():
                m = re.match(r"#\s*year (\d{4}) from NSE", raw.strip())
                if m:
                    year = int(m[1])
                    auto.setdefault(year, [])
                elif raw.rstrip().endswith("# manual"):
                    manual.append(raw.rstrip())
                elif raw.split("#")[0].strip() and year is not None:
                    auto[year].append(raw.rstrip())
        for y in new_years:  # replace those years with NSE's current list (weekday holidays only)
            auto[y] = [f"{d.isoformat()}  # {desc}" for d, desc in sorted(got) if d.year == y and d.weekday() < 5]
        lines = ["# Trading holidays (weekdays only), one YYYY-MM-DD per line.",
                 f"# Filled automatically by bodfetch from {HOLIDAY_API} (CM segment).",
                 "# Add your own as `YYYY-MM-DD  # reason # manual` - those lines are kept on refresh.",
                 "# MCX trades an evening session on some NSE holidays; MCX P-dated files may differ then.", ""]
        if manual:
            lines += manual + [""]
        for y in sorted(auto):
            stamp = f"fetched {dt.date.today().isoformat()}" if y in new_years else "kept"
            lines += [f"# year {y} from NSE ({stamp})"] + auto[y] + [""]
        self.path.write_text("\n".join(lines))
        self._read()
        still = [y for y in missing if y not in self.years]
        return [f"WARNING: NSE has not published holidays for {still} yet; only weekends count for those."] if still else []

    def is_trading(self, d: dt.date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_on_or_after(self, d: dt.date) -> dt.date:
        while not self.is_trading(d):
            d += dt.timedelta(days=1)
        return d

    def prev_before(self, d: dt.date) -> dt.date:
        d -= dt.timedelta(days=1)
        while not self.is_trading(d):
            d -= dt.timedelta(days=1)
        return d


def pick_bod_date(cal: Calendar, now: dt.datetime, cutoff: dt.time) -> dt.date:
    """Run after the cutoff (evening) -> the next trading day; before it -> today (or next trading day)."""
    base = now.date() + dt.timedelta(days=1) if now.time() >= cutoff else now.date()
    return cal.next_on_or_after(base)


# --------------------------------------------------------------------------- helpers
def render(template: str, T: dt.date, P: dt.date, D: dt.date | None = None) -> str:
    """{T:%d%m%Y} = BOD date, {P:...} = previous trading day, {D:...} = candidate date for a URL."""
    return template.format(T=T, P=P, D=D or T)


def name_rule(name: str, T: dt.date, P: dt.date, keep_p_date: list[str] | None) -> str:
    """Allvest naming rule: a saved file carries T in its name, so a P date in it (ddmmyyyy, yyyymmdd, ddmmyy)
    becomes T - except names matching a `keep_p_date` regex (bhavcopies, Pd, SPD). None = rule off."""
    if keep_p_date is None or any(re.search(k, name) for k in keep_p_date):
        return name
    for fmt in ("%d%m%Y", "%Y%m%d", "%d%m%y"):
        name = re.sub(rf"(?<!\d){P:{fmt}}(?!\d)", f"{T:{fmt}}", name)
    return name


def looks_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml version")) and b"<html" in data[:2048].lower()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expand(p: str, base: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(os.path.expanduser(p))
    return path if path.is_absolute() else (base / path)


def make_http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(500, 502, 503, 504), allowed_methods=("GET",))
    for scheme in ("https://", "http://"):
        s.mount(scheme, HTTPAdapter(max_retries=retry))
    return s


# --------------------------------------------------------------------------- browser (human-logged-in session)
def cdp_alive(port: int) -> bool:
    """True if the `browser` command's window is open and listening on 127.0.0.1:<port>."""
    try:
        return requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).ok
    except requests.RequestException:
        return False


def sync_playwright_or_exit():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is needed for NSE/session files:\n"
                         "  pip install playwright && playwright install chromium")
    return sync_playwright


class Browser:
    """A real Chromium window the human logs into (CAPTCHA/OTP stay with the human).

    Two ways to get one:
      * cdp_port given: attach to the window started by `bodfetch.py browser`. It outlives this run,
        so the human logs in once and any number of fetch/record runs reuse that session.
      * otherwise: launch our own window for this run only (`fetch --login`). Session cookies usually
        die when it closes, so log in and fetch in the SAME run.
    """

    def __init__(self, profile_dir: pathlib.Path, headless: bool, probe_url: str = "", login_marker: str = "login",
                 cdp_port: int | None = None):
        self._pw = sync_playwright_or_exit()().start()
        self.attached = cdp_port is not None
        if self.attached:
            self.ctx = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}").contexts[0]
        else:
            profile_dir.mkdir(parents=True, exist_ok=True)
            self.ctx = self._pw.chromium.launch_persistent_context(
                str(profile_dir), headless=headless, accept_downloads=True, user_agent=UA)
        self.headless, self.probe_url, self.marker = headless and not self.attached, probe_url, login_marker.lower()

    def close(self):
        try:
            if not self.attached:  # never close the human's logged-in window, only disconnect from it
                self.ctx.close()
        finally:
            self._pw.stop()

    def open_tabs(self, urls: list[str]):
        for i, url in enumerate(urls):
            page = self.ctx.pages[0] if i == 0 and self.ctx.pages else self.ctx.new_page()
            page.goto(url)

    def open(self, url: str):
        page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if url:
            page.goto(url)
        return page

    def logged_in(self) -> bool:
        if not self.probe_url:
            return True  # unknown -> assume yes; download errors will tell us
        r = self.ctx.request.get(self.probe_url, timeout=60_000)
        if r.status != 200 or self.marker in r.url.lower():
            return False
        body = r.body()
        # an HTML page that talks about "login" where a file/data page was expected = logged out
        return not (looks_html(body) and self.marker in body[:20000].decode("utf-8", "ignore").lower())

    def get(self, url: str) -> bytes:
        r = self.ctx.request.get(url, timeout=120_000)
        if r.status in (401, 403):
            raise Skip("NEEDS_LOGIN", f"HTTP {r.status} (session expired?)")
        if r.status == 404:
            raise Skip("NOT_PUBLISHED", "HTTP 404")
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status}")
        return r.body()

    def record(self, start_url: str, out: pathlib.Path, seconds: int = 0):
        """Log every non-static request while the human browses. The file is rewritten after each
        event, so stopping the run any way (Enter, Ctrl+C, kill) loses nothing. Only header NAMES are
        kept, never values (cookies/tokens stay out of the file)."""
        events: list[dict] = []

        def add(ev: dict):
            events.append({"t": dt.datetime.now().isoformat(timespec="seconds"), **ev})
            out.write_text(json.dumps(events, indent=1))

        def on_response(resp):
            ct = resp.headers.get("content-type", "")
            if resp.request.resource_type in ("image", "stylesheet", "script", "font", "media") \
                    or any(x in ct for x in ("image/", "text/css", "javascript", "font/")):
                return
            req = resp.request
            add({"status": resp.status, "method": req.method, "url": resp.url, "type": req.resource_type,
                 "content_type": ct, "content_disposition": resp.headers.get("content-disposition", ""),
                 "post_data": (req.post_data or "")[:2000], "request_headers": sorted(req.headers)})

        def hook(page):
            page.on("download", lambda d: add({"DOWNLOAD": d.url, "filename": d.suggested_filename}))

        self.ctx.on("response", on_response)
        for p in self.ctx.pages:
            hook(p)
        self.ctx.on("page", hook)
        if not self.attached:
            self.open(start_url)
        out.write_text("[]")
        print(f"Recording to {out}. Log in if needed, then download ONE file from each portal folder by hand.")
        try:
            if seconds:
                self.ctx.pages[0].wait_for_timeout(seconds * 1000) if self.ctx.pages else time.sleep(seconds)
            elif sys.stdin.isatty():
                input("Press Enter here when done...")
            else:
                while True:  # non-interactive (e.g. started by another tool): run until killed
                    (self.ctx.pages[0].wait_for_timeout(5000) if self.ctx.pages else time.sleep(5))
        except KeyboardInterrupt:
            pass
        n = sum(1 for e in events if "DOWNLOAD" in e)
        print(f"Saved {len(events)} events ({n} downloads) to {out}")
        return


# --------------------------------------------------------------------------- Serv-U (MCX member file server)
class ServU:
    """MCX's member file server (Serv-U web client), spoken to with plain HTTP - no browser.

    Same calls as its own web page: POST /Web Client/Login.xml?Command=Login (form: user, pword) returns
    XML with a CsrfToken; then GET /?Command=List|Download with header X-CSRF-Token. Read-only: only
    List and Download are used. ID/password come from .env (MCX: no CAPTCHA/OTP)."""

    def __init__(self, http: requests.Session, pc: dict):
        self.http, self.pc = http, pc
        self.base = pc["login_url"].rstrip("/")
        self.token = ""

    def login(self):
        user, pwd = os.environ.get(self.pc["user_env"], ""), os.environ.get(self.pc["pass_env"], "")
        if not (user and pwd):
            raise Skip("NEEDS_LOGIN", f"set {self.pc['user_env']} and {self.pc['pass_env']} in .env")
        self.http.get(self.base + "/", timeout=(10, 60))  # sets the firewall/session cookies
        r = self.http.post(f"{self.base}/Web Client/Login.xml?Command=Login&Sync={int(time.time() * 1000)}",
                           data={"user": user, "pword": pwd, "viewshare": "", "language": "en,US"},
                           headers={"Referer": self.base + "/", "Origin": self.base, "X-User-Agent": UA},
                           timeout=(10, 60), allow_redirects=False)
        tok = re.search(r"<CsrfToken>([^<]+)</CsrfToken>", r.text, re.I)
        if r.status_code != 200 or not tok:
            msg = re.search(r"<ResultText>([^<]*)</ResultText>", r.text, re.I)
            raise Skip("NEEDS_LOGIN", f"login refused: {msg.group(1) if msg else f'HTTP {r.status_code}'}")
        self.token = tok.group(1)

    def _get(self, query: str) -> requests.Response:
        for attempt in (1, 2):  # log in on first use, and once more if the session has expired
            if not self.token:
                self.login()
            r = self.http.get(f"{self.base}/?{query}", headers={"X-CSRF-Token": self.token},
                              timeout=(10, 300), allow_redirects=False)
            if r.status_code == 200 and (r.text.lstrip().startswith("{") if "Command=List" in query
                                         else "attachment" in r.headers.get("Content-Disposition", "")):
                return r
            self.token = ""
        raise Skip("NEEDS_LOGIN", f"{self.base}: session not accepted (HTTP {r.status_code})")

    def list(self, d: str) -> list[dict]:
        return [f for f in self._get(f"Command=List&Dir={d}&ListFile=List.json&NewWebClient=1").json()["files"] if f]

    def get(self, path: str) -> bytes:
        return self._get(f"Command=Download&File={path}").content


# --------------------------------------------------------------------------- BSE public "Download Common Files" grid
class BseGrid:
    """member.bseindia.com shows a public file browser on its login page (no login needed): HOME > segment >
    COMMON > MON-YYYY > DD-MM-YYYY > file. It is ASP.NET: every folder/file opens with a form post
    (__doPostBack), so we replay those posts with the page's hidden fields. Idea from a colleague's bse.py."""

    BASE = "https://member.bseindia.com/"
    ITEM = re.compile(r"__doPostBack\(&#39;(FileGridVB1\$gvFiles\$ctl\d+\$(?:lbFolderItem|ltlFileItem))"
                      r"&#39;,&#39;&#39;\)\"[^>]*>(.*?)</a>", re.S)

    def __init__(self, http: requests.Session):
        self.http = http

    def _home(self) -> str:
        r = self.http.get(self.BASE, timeout=(10, 60))
        r.raise_for_status()
        m = re.search(r'<form[^>]*action="([^"]*)"', r.text)
        self.action = requests.compat.urljoin(r.url, html.unescape(m.group(1))) if m else r.url
        return r.text

    def _post(self, page: str, target: str) -> requests.Response:
        fields = {html.unescape(n): html.unescape(v) for n, v in
                  re.findall(r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"', page)}
        fields.update(__EVENTTARGET=target, __EVENTARGUMENT="")
        r = self.http.post(self.action, data=fields, timeout=(10, 120), headers={"Referer": self.BASE})
        r.raise_for_status()
        return r

    @classmethod
    def items(cls, page: str) -> dict[str, str]:
        out = {}
        for target, inner in cls.ITEM.findall(page):
            name = html.unescape(re.sub(r"<[^>]+>", "", inner)).replace("\xa0", " ").strip()
            if name:
                out[name] = target
        return out

    def open(self, path: list[str]) -> str:
        page = self._home()
        for i, name in enumerate(path):
            listing = {k.upper(): v for k, v in self.items(page).items()}
            if name.upper() not in listing:
                raise Skip("NOT_PUBLISHED", f"folder '{'/'.join(path[:i + 1])}' not listed")
            page = self._post(page, listing[name.upper()]).text
        return page

    def get(self, path: list[str], name: str) -> bytes:
        page = self.open(path)
        listing = self.items(page)
        if name not in listing:
            raise Skip("NOT_PUBLISHED", f"'{name}' not in {'/'.join(path)}")
        r = self._post(page, listing[name])
        if name.lower() not in r.headers.get("Content-Disposition", "").lower():
            raise Skip("FAILED", f"expected {name}, got {r.headers.get('content-type', '?')} page")
        return r.content


# --------------------------------------------------------------------------- run context
class Run:
    def __init__(self, cfg: dict, T: dt.date, P: dt.date, out_dir: pathlib.Path, force: bool = False):
        self.cfg, self.T, self.P, self.dir, self.force = cfg, T, P, out_dir, force
        self.dir.mkdir(parents=True, exist_ok=True)
        self.http = make_http()
        self.zips: dict[str, bytes] = {}
        self.browser: Browser | None = None
        self.servu: dict[str, ServU] = {}
        self.state_path = out_dir / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    # -- network
    def get_http(self, url: str) -> bytes:
        headers = {"Referer": "https://www.bseindia.com/"} if "bseindia.com" in url else \
                  {"Referer": "https://www.nseindia.com/"} if "nseindia.com" in url else {}
        try:
            r = self.http.get(url, headers=headers, timeout=(10, 120))
        except requests.RequestException as ex:
            raise RuntimeError(f"network error: {ex}")
        if r.status_code == 404:
            raise Skip("NOT_PUBLISHED", "HTTP 404")
        if r.status_code in (401, 403):
            raise Skip("FAILED", f"HTTP {r.status_code} (blocked? check headers/cookies)")
        r.raise_for_status()
        return r.content

    def get_zip(self, url: str) -> zipfile.ZipFile:
        if url not in self.zips:
            self.zips[url] = self.get_http(url)
        data = self.zips[url]
        if not zipfile.is_zipfile(io.BytesIO(data)):
            self.zips.pop(url, None)
            raise Skip("NOT_PUBLISHED", "not a zip (HTML/empty page?)")
        return zipfile.ZipFile(io.BytesIO(data))

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state, indent=1, sort_keys=True))

    # -- one manifest row
    def fetch(self, e: dict) -> tuple[str, str, str]:
        """Returns (status, detail, file name). State is keyed by row id: a zip row with
        member_dates [T, P] only learns its real file name once it finds the member."""
        T, P = self.T, self.P
        key = str(e["id"])
        name = render(e["name"], T, P)
        kind = e.get("type", "http")
        optional = bool(e.get("optional"))
        try:
            st = self.state.get(key)
            if not self.force and st and (self.dir / st["name"]).exists() \
                    and st.get("size") == (self.dir / st["name"]).stat().st_size:
                return "CACHED", "", st["name"]
            if "*" in name and kind != "servu":
                raise Skip("NEEDS_URL", "name has a wildcard; record the real download, then set an exact name")

            if kind == "manual":
                raise Skip("MANUAL", e.get("note", "no automated source known yet"))

            if kind == "http":
                data = self.get_http(render(e["url"], T, P))
                if looks_html(data) and not name.lower().endswith((".htm", ".html")):
                    raise Skip("NOT_PUBLISHED", "got an HTML page instead of the file")
                if e.get("gunzip"):  # NSE publishes contract/security masters as .csv.gz
                    if data[:2] != b"\x1f\x8b":
                        raise Skip("NOT_PUBLISHED", "expected a .gz file")
                    data = gzip.decompress(data)
                used_url = render(e["url"], T, P)

            elif kind == "zip":
                # url_dates: dates to try in the zip URL ({D} in url).
                # member_dates: dates to try for the member ({D} in name/member) - T first, then P.
                # Nothing older than P is ever accepted, so a stale zip is still rejected.
                cand = {"T": T, "P": P}
                mkeys = e.get("member_dates", ["T"])
                data, used_url, why = None, "", "zip still older than P?"
                for ukey in e.get("url_dates", ["T"]):
                    url = render(e["url"], T, P, cand[ukey])
                    try:
                        zf = self.get_zip(url)
                    except Skip as sk:
                        why = sk.detail
                        continue
                    for mkey in mkeys:
                        pat = render(e.get("member", e["name"]), T, P, cand[mkey]).lower()
                        hit = next((n for n in zf.namelist()
                                    if fnmatch.fnmatchcase(os.path.basename(n).lower(), pat)), None)
                        if hit:
                            data, used_url, name = zf.read(hit), url, render(e["name"], T, P, cand[mkey])
                            break
                    if data is not None:
                        break
                if data is None:
                    tried = " / ".join(render(e.get("member", e["name"]), T, P, cand[k]) for k in mkeys)
                    raise Skip("NOT_PUBLISHED", f"'{tried}' not available ({why})")

            elif kind == "servu":
                # file on a Serv-U portal (MCX): list the folder, pick the file, download it.
                portal = e["portal"]
                if portal not in self.servu:
                    pc = (self.cfg.get("settings", {}).get("session", {}).get("portals") or {}).get(portal)
                    if not pc:
                        raise RuntimeError(f"portal '{portal}' missing in settings.session.portals")
                    self.servu[portal] = ServU(self.http, pc)
                su = self.servu[portal]
                files = [f for f in su.list(e["dir"]) if not f["isDir"]]
                hits = sorted((f for f in files if fnmatch.fnmatchcase(
                    requests.utils.unquote(f["name"]).lower(), name.lower())), key=lambda f: requests.utils.unquote(f["name"]))
                if e.get("min_modified") == "T":  # undated names: only a copy made on/after the BOD date counts
                    t0 = dt.datetime.combine(T, dt.time()).timestamp()
                    hits = [f for f in hits if int(f["modified"]) >= t0]
                if not hits:
                    raise Skip("NOT_PUBLISHED", f"'{name}' not in {e['dir']}"
                               + (" (or only an older copy)" if e.get("min_modified") else ""))
                f = hits[0]  # first match: for MCXRPF-<T>-*-01-I that is the single beginning-of-day file
                name = requests.utils.unquote(f["name"])
                used_url = f"{su.base}{e['dir']}/{name}"
                data = su.get(f"{e['dir']}/{name}")
                if int(f.get("size", -1)) not in (-1, len(data)):
                    raise Skip("FAILED", f"size {len(data)} != listed {f['size']}")

            elif kind == "bsegrid":
                # BSE public "Download Common Files" grid (no login): dir = folder names, member = file name there
                path = [render(x, T, P) for x in e["dir"]]
                member = render(e.get("member", e["name"]), T, P)
                data = BseGrid(self.http).get(path, member)
                used_url = f"{BseGrid.BASE} > {' > '.join(path)} > {member}"

            elif kind == "session":
                if not e.get("url"):
                    raise Skip("NEEDS_URL", "run `bodfetch.py record`, then put the download URL in manifest.yaml")
                if self.browser is None:
                    raise Skip("NEEDS_LOGIN", "run with --login so a human can log in first")
                used_url = render(e["url"], T, P)
                data = self.browser.get(used_url)
                if looks_html(data) and not name.lower().endswith((".htm", ".html")):
                    raise Skip("NEEDS_LOGIN", "got an HTML page - session expired?")
            else:
                raise RuntimeError(f"unknown type '{kind}'")

            problem = self.validate(data, e, name)
            if problem:
                raise Skip("FAILED", problem)
            if e.get("crlf"):  # UAT's copy has Windows line endings
                data = re.sub(rb"(?<!\r)\n", b"\r\n", data)
            name = name_rule(name, T, P, self.cfg.get("settings", {}).get("keep_p_date"))

            dest = self.dir / name
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            self.state[key] = {"name": name, "size": len(data), "sha256": sha256(data), "url": used_url,
                               "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
            self.save_state()
            return "OK", f"{len(data):,} bytes", name

        except Skip as s:
            if optional and s.status in ("NOT_PUBLISHED",):
                return "OPTIONAL_ABSENT", s.detail, name
            return s.status, s.detail, name
        except Exception as ex:  # keep going: one bad file must not stop the run
            return "FAILED", f"{type(ex).__name__}: {ex}", name

    @staticmethod
    def validate(data: bytes, e: dict, name: str) -> str | None:
        if len(data) == 0 and not e.get("allow_empty"):
            return "empty file"
        if len(data) < e.get("min_bytes", 0):
            return f"only {len(data)} bytes (expected >= {e['min_bytes']})"
        return None


# --------------------------------------------------------------------------- commands
def load(manifest_path: pathlib.Path):
    cfg = yaml.safe_load(manifest_path.read_text())
    s = cfg.get("settings", {})
    base = manifest_path.parent
    cal = Calendar(expand(s.get("holidays_file", "holidays.txt"), base))
    return cfg, s, base, cal


def load_env(path: pathlib.Path):
    """Read KEY=VALUE lines from .env into os.environ (real environment variables win).
    Values are never printed or logged."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def login_urls(sess: dict) -> list[str]:
    urls = sess.get("login_urls") or [sess.get("login_url") or "https://www.nseindia.com"]
    return [u for u in urls if u]


def choose_entries(cfg: dict, only: str | None) -> list[dict]:
    files = cfg["files"]
    if not only:
        return files
    want = {x.strip() for x in only.split(",")}
    # "sheet" = the 62 numbered rows of the GreekSoft sheet (excludes the x1/x2 extras)
    return [f for f in files if str(f["id"]) in want or f.get("grp") in want
            or ("sheet" in want and isinstance(f["id"], int))]


def bod_dates(args, cal: Calendar, s: dict):
    # make sure the holiday list covers the dates we are about to compute (fetched from NSE once a year)
    around = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    for w in cal.ensure_years({(around - dt.timedelta(days=15)).year, (around + dt.timedelta(days=15)).year}):
        print(w)
    if args.date:
        T = dt.date.fromisoformat(args.date)
    else:
        cutoff = dt.time.fromisoformat(s.get("cutoff", "15:30"))
        T = pick_bod_date(cal, dt.datetime.now(), cutoff)
    return T, cal.prev_before(T)


def cmd_plan(args):
    cfg, s, base, cal = load(pathlib.Path(args.manifest))
    T, P = bod_dates(args, cal, s)
    print(f"BOD date T = {T:%a %d-%b-%Y}   previous trading day P = {P:%a %d-%b-%Y}")
    if not cal.holidays:
        print("WARNING: no holidays known - only weekends are treated as non-trading days.")
    for e in choose_entries(cfg, args.only):
        alt = f"  (or {render(e['name'], T, P, P)})" if "P" in e.get("member_dates", []) else ""
        name = name_rule(render(e["name"], T, P), T, P, s.get("keep_p_date"))
        print(f"{str(e['id']):>4}  {e.get('grp', ''):<11} {e.get('type', 'http'):<8} {name}{alt}")


def cmd_fetch(args):
    cfg, s, base, cal = load(pathlib.Path(args.manifest))
    T, P = bod_dates(args, cal, s)
    entries = choose_entries(cfg, args.only)
    out = expand(s.get("staging_dir", "staging"), base) / f"{T:%Y-%m-%d}"
    run = Run(cfg, T, P, out, force=args.force)
    sess = s.get("session", {})
    interactive = sys.stdin.isatty()
    print(f"BOD date {T:%a %d-%b-%Y} | previous trading day {P:%a %d-%b-%Y} | staging: {out}")

    load_env(base / ".env")
    needs_session = any(e.get("type") == "session" and e.get("url") for e in entries)
    port = int(sess.get("cdp_port", 9222))
    try:
        if needs_session and cdp_alive(port):
            print(f"Using the logged-in browser window (started by `bodfetch.py browser`, port {port}).")
            run.browser = Browser(pathlib.Path("."), headless=False, probe_url=sess.get("probe_url", ""),
                                  login_marker=sess.get("login_marker", "login"), cdp_port=port)
        elif args.login or (needs_session and sess.get("use_saved_profile")):
            run.browser = Browser(expand(sess.get("profile_dir", "browser-profile"), base),
                                  headless=not args.login, probe_url=sess.get("probe_url", ""),
                                  login_marker=sess.get("login_marker", "login"))
            if args.login:
                run.browser.open_tabs(login_urls(sess))
                input("\nLog in to every portal tab in the browser window (CAPTCHA/OTP are yours).\n"
                      "Press Enter here once you are fully logged in...")
        until = None
        if args.watch and args.until:
            hh, mm = map(int, args.until.split(":"))
            until = dt.datetime.combine(dt.date.today(), dt.time(hh, mm))
            if until <= dt.datetime.now():
                until += dt.timedelta(days=1)

        while True:
            results = []
            for e in entries:
                results.append((e, *run.fetch(e)))
            print_report(results, out)
            pending = [r for r in results if r[1] not in DONE]
            if not pending or not args.watch:
                break
            expired = any(r[1] == "NEEDS_LOGIN" for r in pending)
            if expired and run.browser and not run.browser.headless and interactive:
                input("\nNSE session looks expired. Log in again in the browser window, then press Enter...")
                continue
            if until and dt.datetime.now() + dt.timedelta(minutes=args.interval) > until:
                print("Deadline reached with files still pending.")
                break
            print(f"\n{len(pending)} pending - next check in {args.interval} min (Ctrl+C to stop)")
            time.sleep(args.interval * 60)
        return 0 if all(r[1] in DONE for r in results) else 1
    finally:
        if run.browser:
            run.browser.close()


def print_report(results, out: pathlib.Path):
    counts: dict[str, int] = {}
    print()
    for e, status, detail, name in results:
        counts[status] = counts.get(status, 0) + 1
        print(f"{status:<16} {str(e['id']):>4}  {name:<52} {detail}")
    print("\nSUMMARY: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    (out / "report.json").write_text(json.dumps(
        [{"id": e["id"], "name": n, "status": s, "detail": d} for e, s, d, n in results],
        indent=1))


def cmd_record(args):
    cfg, s, base, cal = load(pathlib.Path(args.manifest))
    sess = s.get("session", {})
    port = int(sess.get("cdp_port", 9222))
    b = Browser(expand(sess.get("profile_dir", "browser-profile"), base), headless=False,
                cdp_port=port if cdp_alive(port) else None)
    try:
        b.record(login_urls(sess)[0], base / "recorded_urls.json", seconds=args.seconds or 0)
    finally:
        b.close()


def cmd_browser(args):
    """Start the login window and return. It keeps running (own process, own profile folder) until the
    human closes it; fetch/record attach to it over a debugging port bound to 127.0.0.1 only."""
    cfg, s, base, cal = load(pathlib.Path(args.manifest))
    start_browser(s.get("session", {}), base, login_urls(s.get("session", {})))
    print("Log in to each portal tab; CAPTCHA/OTP are yours.\n"
          "Leave the window open: `fetch` and `record` will use it. Close it when you are done.")
    return 0


def start_browser(sess: dict, base: pathlib.Path, urls: list[str]) -> int:
    port = int(sess.get("cdp_port", 9222))
    if cdp_alive(port):
        print(f"The login browser is already open (port {port}).")
        return port
    with sync_playwright_or_exit()() as pw:
        exe = pw.chromium.executable_path
    profile = expand(sess.get("profile_dir", "browser-profile"), base)
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([exe, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                      "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                      *urls],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if cdp_alive(port):
            print(f"Login browser open (port {port}).")
            return port
        time.sleep(1)
    raise SystemExit("The browser did not start - is `playwright install chromium` done?")


def cmd_login(args):
    """Test a portal login from .env (plain HTTP, no browser): log in and list the home folder."""
    cfg, s, base, cal = load(pathlib.Path(args.manifest))
    load_env(base / ".env")
    pc = (s.get("session", {}).get("portals") or {}).get(args.portal)
    if not pc:
        raise SystemExit(f"No portal '{args.portal}' under settings.session.portals in manifest.yaml")
    try:
        names = [requests.utils.unquote(f["name"]) for f in ServU(make_http(), pc).list("/")]
    except Skip as sk:
        print(f"LOGIN NOT DONE: {sk.detail}")
        return 1
    print(f"Logged in to {pc['login_url']} - top-level folders: {names}")
    return 0


def cmd_push(args):
    """Copy the day's fetched files to the UAT server (or a local folder with --local). See bodpush.py."""
    import bodpush as bp
    cfg, s, base, cal = load(pathlib.Path(args.manifest))
    load_env(base / ".env")
    T, P = bod_dates(args, cal, s)
    out = expand(s.get("staging_dir", "staging"), base) / f"{T:%Y-%m-%d}"
    entries = choose_entries(cfg, args.only)
    items, missing = bp.collect(out, entries, lambda e: render(e["name"], T, P))
    ps = s.get("push", {})
    target = args.target or ps.get("target_dir")
    if not target:
        raise SystemExit("set settings.push.target_dir in manifest.yaml (or pass --target)")
    staging_root, backup_root = ps.get("staging_dir"), ps.get("backup_dir")
    if args.local:  # same flow into a local folder: <local>/<target name>, <local>/staging, <local>/backup
        root = pathlib.Path(args.local).expanduser().resolve()
        target = str(root / pathlib.PurePosixPath(target).name)
        staging_root, backup_root = str(root / "staging"), str(root / "backup")
    where = f"local folder {target}" if args.local else f"{ps.get('user')}@{ps.get('host')}:{target}"
    print(f"BOD date {T:%a %d-%b-%Y} | {len(items)} file(s) ready in {out} | -> {where}")
    if missing:
        print(f"{len(missing)} selected file(s) not fetched (or changed since fetch):")
        for key, name in missing:
            print(f"   {key:>4}  {name}")
        if not args.allow_partial:
            print("Nothing pushed. Fetch them first, or pass --allow-partial to push only what is ready.")
            return 2
    if not items:
        print("No files to push.")
        return 2
    try:
        bp.check_target(target, args.live)
        remote = bp.LocalRemote() if args.local else bp.SshRemote(
            ps["host"], ps.get("user", "greek"), int(ps.get("port", 22)),
            password=os.environ.get(ps.get("password_env", ""), "") or None, key_file=ps.get("key_file"),
            passphrase=os.environ.get(ps.get("passphrase_env", ""), "") or None)
    except bp.PushError as ex:
        print(f"ERROR: {ex}")
        return 1
    try:
        paths = bp.day_paths(target, staging_root, backup_root, T)
        bp.plan(items, remote, paths)
        counts = {a: sum(it.action == a for it in items) for a in ("NEW", "REPLACE", "SAME")}
        for it in items:
            print(f"  {it.action:<8} {it.name}")
        print("PLAN: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if args.dry_run:
            print("Dry run: nothing written.")
            return 0
        report = bp.push(items, remote, paths)
        report.update(date=T.isoformat(), missing=[n for _, n in missing], dry_run=False)
        log = out / f"push-{dt.datetime.now():%H%M%S}.json"
        log.write_text(json.dumps(report, indent=1))
        print(f"RESULT: {report['result']}  (log: {log})")
        return 0
    except bp.PushError as ex:
        print(f"ERROR: {ex}")
        return 1
    finally:
        remote.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(HERE / "manifest.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("fetch", cmd_fetch), ("record", cmd_record), ("browser", cmd_browser)):
        p = sub.add_parser(name)
        p.add_argument("--date", help="BOD date YYYY-MM-DD (default: next trading day after the cutoff)")
        p.add_argument("--only", help="comma list of ids or groups, e.g. 17,18 or bse_open; 'sheet' = the 62 sheet rows")
        p.set_defaults(fn=fn)
        if name == "fetch":
            p.add_argument("--login", action="store_true", help="open a browser so a human can log in to NSE first")
            p.add_argument("--watch", action="store_true", help="keep retrying until everything is fetched")
            p.add_argument("--interval", type=int, default=20, help="minutes between checks in --watch mode")
            p.add_argument("--until", help="HH:MM deadline for --watch")
            p.add_argument("--force", action="store_true", help="re-download even if already fetched")
        if name == "record":
            p.add_argument("--seconds", type=int, help="stop recording after N seconds (default: Enter / Ctrl+C)")
    p = sub.add_parser("push", help="copy the day's files to the UAT server: stage, verify, back up, swap")
    p.add_argument("--date", help="BOD date YYYY-MM-DD (default: next trading day after the cutoff)")
    p.add_argument("--only", default="sheet", help="ids/groups to push (default: sheet = the 62 Servers List rows)")
    p.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    p.add_argument("--allow-partial", action="store_true", help="push what is ready even if some files are missing")
    p.add_argument("--target", help="override settings.push.target_dir")
    p.add_argument("--local", metavar="DIR", help="push into a local folder instead of the server (testing)")
    p.add_argument("--live", action="store_true", help="allow writing into Greeksoft's live 'contract' folder")
    p.set_defaults(fn=cmd_push)
    p = sub.add_parser("login", help="test a portal login from .env (MCX)")
    p.add_argument("portal", help="portal name under settings.session.portals, e.g. mcx")
    p.set_defaults(fn=cmd_login)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()