#!/usr/bin/env python3
"""bodfetch - fetch the daily BOD exchange/contract files into a dated staging folder.

    python bodfetch.py plan              # list the files expected for the BOD date (no network)
    python bodfetch.py fetch             # fetch what is published and not fetched yet
    python bodfetch.py fetch --only sheet   # only the 62 rows of the GreekSoft sheet
    python bodfetch.py browser           # open the login browser; it stays open, fetch/record attach to it
    python bodfetch.py fetch --login     # alternative: one-shot browser inside this run (see README)
    python bodfetch.py fetch --watch --interval 20 --until 07:00
    python bodfetch.py record            # human browses/downloads; we log the URLs to put in manifest.yaml
    python bodfetch.py login mcx         # auto-login with the ID/password from .env (portals without CAPTCHA/OTP)

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
# Runs inside a logged-in page of the portal, exactly like its own web client: read the CSRF token from
# SessionInfo.json and send it as X-CSRF-Token. Read-only: only List and Download are ever used.
SERVU_JS = """async ([op, arg]) => {
  let tok;
  try { tok = (await (await fetch('/WebClientApi/SessionInfo.json')).json()).data.csrfToken; } catch (e) { return [0, '']; }
  const h = {headers: {'X-CSRF-Token': tok}};
  if (op === 'list') {
    const r = await fetch('/?Command=List&Dir=' + encodeURI(arg) + '&ListFile=List.json&NewWebClient=1', h);
    return [r.status, await r.text()];
  }
  const r = await fetch('/?Command=Download&File=' + encodeURI(arg), h);
  const b = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  for (let i = 0; i < b.length; i += 0x8000) bin += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return [r.status, (r.headers.get('content-disposition') || '') + '|' + btoa(bin)];
}"""


def auto_login(br: "Browser", pc: dict, shot: pathlib.Path | None = None) -> tuple[bool, str]:
    """Fill ID/password from the environment (.env) on a portal WITHOUT CAPTCHA/OTP and submit.
    Stops if a CAPTCHA is visible - that is the human's job, never bypassed."""
    user, pwd = os.environ.get(pc.get("user_env", ""), ""), os.environ.get(pc.get("pass_env", ""), "")
    if not (user and pwd):
        return False, f"set {pc.get('user_env')} and {pc.get('pass_env')} in .env"
    page = next((pg for pg in br.ctx.pages if pg.url.startswith(pc["login_url"])), None) or br.ctx.new_page()
    page.goto(pc["login_url"], wait_until="domcontentloaded", timeout=60_000)
    pw_sel = pc.get("pass_selector", "input[type=password]")
    page.wait_for_selector(pw_sel, state="visible", timeout=30_000)
    user_sel = pc.get("user_selector") or \
        "input:not([type=password]):not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit])"
    page.locator(user_sel).first.fill(user)
    page.locator(pw_sel).first.fill(pwd)
    if page.locator("img[src*=aptcha i], [id*=captcha i], [name*=captcha i], iframe[src*=recaptcha]").count():
        return False, "page shows a CAPTCHA: ID/password are filled, finish the login in the browser window"
    sub_sel = pc.get("submit_selector", "button[type=submit], input[type=submit]")
    if page.locator(sub_sel).count():
        page.locator(sub_sel).first.click()
    else:
        page.locator(pw_sel).first.press("Enter")
    page.wait_for_load_state("networkidle", timeout=60_000)
    ok_sel = pc.get("after_login_click")  # e.g. a "Welcome" dialog's OK button
    if ok_sel and page.locator(ok_sel).count() and page.locator(ok_sel).first.is_visible():
        page.locator(ok_sel).first.click()
        page.wait_for_load_state("networkidle", timeout=30_000)
    if shot:
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot))
    if page.locator(pw_sel).count() and page.locator(pw_sel).first.is_visible():
        return False, f"password box still shown - wrong ID/password? ({page.url})"
    return True, f"{page.title()!r} {page.url}"


class ServU:
    def __init__(self, br: "Browser", pc: dict):
        self.br, self.pc = br, pc
        self.base = pc["login_url"].rstrip("/")
        self.logged_in_once = False

    def _call(self, op: str, arg: str):
        page = next((pg for pg in self.br.ctx.pages if pg.url.startswith(self.base) and "Login" not in pg.url), None)
        if page is not None:
            st, body = page.evaluate(SERVU_JS, [op, arg])
            ok = st == 200 and (body.lstrip().startswith("{") if op == "list" else "attachment" in body.split("|", 1)[0])
            if ok:
                return body
        if self.logged_in_once:  # already re-logged in this run and it still fails
            raise Skip("NEEDS_LOGIN", f"{self.base}: session not accepted")
        self.logged_in_once = True
        ok, msg = auto_login(self.br, self.pc)
        if not ok:
            raise Skip("NEEDS_LOGIN", msg)
        return self._call(op, arg)

    def list(self, d: str) -> list[dict]:
        return [f for f in json.loads(self._call("list", d))["files"] if f]

    def get(self, path: str) -> bytes:
        return base64.b64decode(self._call("get", path).split("|", 1)[1])


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
                if self.browser is None:
                    raise Skip("NEEDS_LOGIN", "no login browser")
                portal = e["portal"]
                if portal not in self.servu:
                    pc = (self.cfg.get("settings", {}).get("session", {}).get("portals") or {}).get(portal)
                    if not pc:
                        raise RuntimeError(f"portal '{portal}' missing in settings.session.portals")
                    self.servu[portal] = ServU(self.browser, pc)
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
        print(f"{str(e['id']):>4}  {e.get('grp', ''):<11} {e.get('type', 'http'):<8} {render(e['name'], T, P)}{alt}")


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
    needs_servu = any(e.get("type") == "servu" for e in entries)
    port = int(sess.get("cdp_port", 9222))
    try:
        if needs_servu and not cdp_alive(port) and not args.login:
            start_browser(sess, base, [])  # Serv-U portals log themselves in from .env
        if (needs_session or needs_servu) and cdp_alive(port):
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
    """Fill in ID/password from .env on a portal's login page, in the shared login browser.
    Only for portals WITHOUT CAPTCHA/OTP (per the firm: MCX). If a CAPTCHA/OTP shows up we stop and
    leave it to the human - it is never bypassed."""
    cfg, s, base, cal = load(pathlib.Path(args.manifest))
    load_env(base / ".env")
    sess = s.get("session", {})
    pc = (sess.get("portals") or {}).get(args.portal)
    if not pc:
        raise SystemExit(f"No portal '{args.portal}' under settings.session.portals in manifest.yaml")
    br = Browser(pathlib.Path("."), headless=False, cdp_port=start_browser(sess, base, []))
    try:
        shot = base / "browser-profile" / f"last_login_{args.portal}.png"  # git-ignored folder
        ok, msg = auto_login(br, pc, shot)
        print(f"{'Logged in' if ok else 'LOGIN NOT DONE'}: {msg}\nScreenshot: {shot}")
        return 0 if ok else 1
    finally:
        br.close()


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
    p = sub.add_parser("login", help="auto-fill ID/password from .env (portals without CAPTCHA/OTP)")
    p.add_argument("portal", help="portal name under settings.session.portals, e.g. mcx")
    p.set_defaults(fn=cmd_login)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()