# CLAUDE.md - BOD Automation (Greeksoft UAT) - project handoff

Read this first. It is the full knowledge transfer from the discovery/design phase (Claude Cowork
session, 27-Oct-2026) to development in Claude Code. Everything marked **VERIFIED** was observed on
the real systems; everything marked **ASSUMPTION** must be checked before it is relied on.

Owner: Sahil Rane (Wohlig, working for Allvest IT Operations). Primary dev machine: MacBook Air (macOS),
on the org wifi, IP 192.168.1.150.

---------------------------------------------------------------------------------------------------

## 1. Goal and scope

Allvest's IT Ops person does a **BOD (Beginning of Day)** routine every trading day for two trading
platforms:

* **Greeksoft** (currently the UAT environment) - this project's first target.
* **Rupeeseed** (production, ~50 checks between 07:15 and 08:55) - later phase, not started.

Automation is being built in stages. **Stage 1 (current): automatically fetch the daily exchange/contract
files from the exchange portals.** Later stages:

2. Push the files to the UAT server and (re)start the Greeksoft apps in the right order (first 5 serially, the
   other 10 in parallel).
3. Verify the apps came up (readiness/health), report.
4. A **human still clicks Login on the Greeksoft Dashboard**; exchange logins are not automated.
5. Then Rupeeseed checks.

Decisions made (with the user):

* **No AI at runtime.** Everything is deterministic: compare output to expected value. Claude Code is the
  development tool, not the thing that runs each morning. (An optional failure-triage helper may come much later.)
* **A scheduled Python script**, not an agent, not pure batch files. Python for orchestration (date logic,
  validation, retries, logs). Batch/WinSCP only as thin glue if ever needed.
* **Time-driven, in waves**, because exchange files publish between ~16:30 the evening before and ~06:00:
  an idempotent job re-runs every 20-30 min and fetches whatever is newly published.
* Skip Prefect/orchestrators for now (an earlier proposal doc suggested Prefect); cron/launchd/systemd + Python is enough
  for one platform.
* **NSE needs a human login (CAPTCHA + OTP).** The human logs in; the script then runs in the same browser
  session. We never try to bypass CAPTCHA/OTP.
* Mac = development only. The scheduled job must later run on an always-on machine (small Linux VM preferred;
  .73 Windows terminal is the fallback). Not yet provisioned.

---------------------------------------------------------------------------------------------------

## 2. Environment and network (all VERIFIED unless noted)

| Host | What | Access from the Mac |
|---|---|---|
| Mac `192.168.1.150/24` | dev machine, same subnet as UAT | - |
| **UAT terminal `192.168.1.73`** | Windows 11. Has Greek Dashboard, GETS Client 7.0, a VNC viewer, WinSCP, File Explorer windows. Daily files live in `C:\Users\Admin\Desktop\Daily Files\<dd-mm>\` (e.g. `27-10`). A mapped network drive `e:` (`\\192.168.1.x`, rest of path unseen) exists - purpose unknown. | RDP port 3389 **open**; ping OK. Used via the "Windows App" (Microsoft Remote Desktop) with credentials "ask when required". |
| **UAT server `192.168.1.76`** | Red Hat Enterprise Linux (GNOME desktop, VNC display `:1`), user `greek`, hostname `greek`. MySQL 8.0.31 runs here (the Dashboard shows it). RAM 66 GB, disk 11% used. | SSH 22 **open** (works with password; `greek` login confirmed). VNC **5901** open (5900 refused). |

Notes:

* The Mac can reach .76 directly, so the .73 hop is NOT needed for dev. The old process went
  RDP -> .73 -> VNC -> .76 and WinSCP .73 -> .76.
* `tmux` and `screen` are **NOT installed** on .76. `python3` **is** (`/usr/bin/python3`). `unzip` availability on .76 not checked.
* sudo rights for `greek` on .76: not checked yet (`sudo -n true`).
* The Mac's SSH host key for .76 is already accepted (fingerprint `SHA256:js1qZaT3wM...`).
* Two failed SSH password attempts were made early on (typos) - logged server-side; avoid repeated failures.
* **Today's date in the session was Tuesday 27-Oct-2026.** Server clock on .76 matched the dashboard (~12:11 IST).
* No project folder is connected to Cowork; everything was done through the user pasting terminal output/screenshots.

### Source documents (provided by the user; not in the repo)

1. `Greeksoft UAT BOD Procedure.docx` - connectivity (RDP .73 -> VNC .76), file download to
   `/home/greek/Desktop/contract`, 15-tab application startup order, post-startup dashboard verification,
   **Annexure A = the 53 files**.
2. `Allvest Production Morning Checklist.pdf` - the **Rupeeseed** production BOD runbook (4 sections: file upload
   check ~7:15-7:30; UDP server ~7:30; OMS server after 7:30; Analytical server after 8:20) + completion checklist.
   **It contains plaintext passwords including root SSH and a MySQL host on a public IP - never copy them into
   this repo or any prompt; they should be rotated.**
3. `BOD Automation - Detailed Process.pdf` - an earlier management proposal (15-day plan, Prefect, "agents",
   AI triage). Our approach is deliberately simpler (see section 1).
4. `Servers List with Configuration - BOD_Files GreekSoft.pdf` (a Google Sheet printout) - **the file-to-source map**:
   62 rows (Sr.No 1-62) with exchange, segment, filename pattern, source path/URL, comments like "take previous day file".
   It is a **June 2025 snapshot** (dates like 10062025); patterns are still valid.

---------------------------------------------------------------------------------------------------

## 3. How BOD works today on the Greeksoft UAT server (VERIFIED)

### 3.1 Files

A person puts the day's exchange files into `C:\Users\Admin\Desktop\Daily Files\<dd-mm>\` on .73 (some arrive
as zips, e.g. `BSE_CC_FILE.zip` containing `BFX_CC_CO271026.csv`, `EQ_MAP_CC_271026.csv`, `EQD_CC_CO271026.csv`;
and `NEW_CONTRACTSECURITY.zip`, contents not yet listed), WinSCP copies them to `/home/greek/Desktop/contract/` on .76,
and zips are extracted there. The folder held **53 data files + 2 zips** on 27-Oct (listing in
`tests/uat_listing_2026-10-27.txt`).

Observations:

* Many files show `Oct 27  2026` with no time in `ls`: their mtimes are **in the future** relative to the server
  clock (zip members were stamped 21:57). **Never use mtime for freshness**; use the date in the filename/contents.
* Files with real mtimes show the original publish times (approx., if preserved): NSE CM bhavcopy 16:30, NSE FO/CD
  bhavcopy 18:33-18:45, SPAN 20:07-21:34, BSE bhavcopies 22:40, VAR 22:52, BSERISK 00:52, series_change 05:53,
  fo_secban/zips 09:06-09:14 (those last ones copied after apps started).
* `Settlor.txt` was **0 bytes** (created 09:14, different permissions) - possibly normal; source unknown.
* `fo_secban.csv` is 74 bytes; the sheet comment says "Not Found" -> it may legitimately be absent some days.
* The Manager console on .76 has menu items **"10. Upload Contract" and "11. Upload BhavCopy"** and printed
  "... BhavCopy Uploaded Successfully" at startup; the Dashboard shows per-file "Upload Count". The procedure doc
  never mentions an explicit upload step - **how the contract folder is ingested is still unconfirmed** (ASSUMPTION:
  the Manager reads the folder at startup).

### 3.2 Application startup (already scripted on .76)

`/home/greek/Desktop/AUTO/` holds three small scripts (modified 23-Oct):

* `BOD` - opens one gnome-terminal tab starting only `CTCLManager`.
* `BOD_F` - the full startup. `cd /home/greek/Desktop/AUG_2026/GETSManager_7.0.120925_CAS_RH8_280826`, then one
  `gnome-terminal --tab` per app running `sh -c 'sleep N; ./startapp <App>'` with **fixed sleeps, no readiness checks**:

| Tab | App (`./startapp ...`) | sleep (s) | Procedure-doc wait condition |
|---|---|---|---|
| M | CTCLManager | 1 | option menu appears |
| S | SMM | 120 | ~2 minutes |
| C | ClientCommunicator | 150 | option menu appears |
| R | RMS | 170 | 0-9 sequence created |
| G | GATS | 280 | option menu appears |
| N1 / N2 | NSEInteractive_TM 1 / 2 | 290 / 300 | - |
| ETI1 / ETI2 | ETIInteractive 1 / 2 | 310 / 320 | - |
| F1 / F2 | FixBroadcast_CAS 1 / 2 | 325 / 330 | - |
| B1 / B2 | NSEBroadCast_CAS 1 / NSEBroadCast 2 | 335 / 340 | - |
| MC | MarginCalculator | 345 | - |
| ACM | AdvanceChartManager_CAS | 355 | - |

* `EOD` - `pkill gnome-terminal` (hard kill of all app terminals; no graceful stop).
* On 27-Oct all 15 apps had been running ~3h08m (started ~09:02-09:03), each visible twice in `ps` (sh wrapper).
* The apps show **interactive menus** ("MAIN MENU ... Enter Your Selection"), so they need a TTY. They cannot simply be
  backgrounded over SSH without a pty. Options: run `BOD_F` via ssh with `DISPLAY=:1` (reuses the VNC desktop), install
  `tmux` (needs admin), or write a small Python `pty` supervisor.
* Dashboard (Windows app on .73, "Greek Dashboard - [Test]", server 192.168.1.76) shows: component list (ID, status
  Connected, broadcast counts), exchange login status per segment, **Contract upload counts per file**, MySQL/RAM/HDD,
  message log. **The Dashboard is the human's final verification; Login/Reset/LogOut buttons must never be pressed by automation.**

### 3.3 UAT is LIVE

The UAT apps were running while we explored. **Do not stop, restart, or run `EOD`/`BOD_F` on .76, and do not
touch the Dashboard/GETS Client**, without the user's explicit go-ahead. All work so far on .76 was read-only.

---------------------------------------------------------------------------------------------------

## 4. The files: sources, rules, edge cases

The UAT needs the **53 files of Annexure A**. The sheet lists 62 rows. Reconciliation (VERIFIED by the parity test):

* Sheet-only (11): 8 MCX files + 3 SLBM files - not in the 53.
* Annexure-only (2): `qty_freeze_<ddmmyyyy>.csv` and `Settlor.txt` - **no source known**.
* 62 - 11 + 2 = 53. (The net difference is 9.)

Source groups for the 53:

| Group | # | Source |
|---|---|---|
| BSE open | 16 | 9 HTTP requests: 3 bhavcopy CSVs, `VAR` zip, 2 SPAN zips (`BSERISK`, `BSECDXRISK`), and 3 bulk zips: `downloads/Help/file/CO.zip` (3 files), `.../scrip.zip` (4), `downloads1/Master.zip` (3) |
| NSE public archives | 2 | `Pd<ddmmyy>.csv` (from `PR<ddmmyy>.zip`), `series_change.csv` on `archives.nseindia.com` |
| `member.bseindia.com` | 6 | BFX/EQD/EQ `*_CC_*` and `*_PARTICIPANT*` files - user says open, **unverified**; sheet gives only the portal homepage |
| NSE member/extranet | 27 | paths like `/CMCOMMON/ntneat`, `/FAOCOMMON/Limit Files`, `/CDSCOMMON/tbtmaster` - **login + CAPTCHA + OTP**; no download URLs known |
| No source | 2 | `qty_freeze`, `Settlor.txt` |

Date rules (T = BOD date, P = previous trading day), confirmed against the 27-Oct folder:

* P-dated: all six bhavcopies (`BhavCopy_<EXCH>_<SEG>_0_0_0_<yyyymmdd>_F_0000`), `Pd<ddmmyy>`, spread bhavcopy
  `<ddmm>SPD0000.md` (e.g. `2610SPD0000.md`).
* T-dated: contracts, participants, SPAN (`nsccl.<yyyymmdd>.i01.spn`, `nsccl_x...`, `BSERISK<yyyymmdd>-00.spn`),
  `C_VAR1_<ddmmyyyy>_1.DAT`, `VAR<ddmmyy>.xls`, `ael_*`, DPR/CI/REG_IND files, etc.
* Mixed formats: `ddmmyyyy`, `ddmmyy`, `yyyymmdd`, `ddmm`. Undated: `series_change.csv`, `participant.txt`, `nnf_*`, `ael_NIFTY_Options.csv`, ...
* Trading-calendar aware: `holidays.txt` is auto-filled per year from `https://www.nseindia.com/api/holiday-master?type=trading` (CM segment, public, needs only the homepage cookie). If the fetch fails the run prints a WARNING and uses weekends only. MCX evening sessions on NSE holidays are not modelled.

**VAR ambiguity (ASSUMPTION):** the sheet says "use the previous date for VAR" and its URL is `VAR<date>.zip`, but the real file is
named for T and was published ~22:52 the evening before. The manifest tries the zip dated T then P and accepts only a zip
that contains the member named for T. Verify on the first live run and adjust.

---------------------------------------------------------------------------------------------------

## 5. What has been built (this repo)

```
bod-fetch/
  bodfetch.py        # the tool (single file, ~400 lines)
  manifest.yaml      # 64 rows: 62 sheet rows + qty_freeze + Settlor.txt; single source of truth
  holidays.txt       # auto-filled from NSE's holiday API (CM); '# manual' lines kept
  requirements.txt   # requests, pyyaml, playwright
  README.md          # user-level run instructions
  CLAUDE.md          # this file
  tests/test_fetch.py, tests/uat_listing_2026-10-27.txt
  .gitignore         # staging/, browser-profile/, recorded_urls.json, .env
```

### bodfetch.py

Commands: `plan` (print expected filenames, no network), `browser` (long-lived login window, see below), `fetch` (`--date`, `--only <ids|groups>`, `--login`, `--watch
--interval N --until HH:MM`, `--force`), `record` (human browses/downloads; logs every URL to `recorded_urls.json`).
Default BOD date: after `settings.cutoff` (15:30 local) -> next trading day, else today/next trading day.
Output: `bod-fetch/downloads/<YYYY-MM-DD>/` (git-ignored) + `state.json` (per row id: saved name, size, sha256, url, time) + `report.json`.
Exit code 0 only if all selected files are OK/CACHED/OPTIONAL_ABSENT.

Manifest row fields: `id` (sheet Sr.No; `x1`,`x2` for the two extras), `name` (template), `type`
(`http` | `zip` | `session` | `manual`), `url`, `member` (zip member pattern, default = name), `url_dates`
(`[T,P]` candidates for `{D:...}` in the URL), `optional`, `allow_empty`, `min_bytes`, `grp`, `path` (doc only), `note`.
Date tokens: `{T:%d%m%Y}`, `{P:%Y%m%d}`, `{D:...}`.

Statuses: `OK`, `CACHED`, `OPTIONAL_ABSENT` (done) | `NOT_PUBLISHED` (404 / HTML page / stale zip - retried) | `NEEDS_LOGIN` |
`NEEDS_URL` | `MANUAL` | `FAILED`.

Safety rules baked in: atomic writes (`.part` -> rename); never re-download a valid file; reject empty files and HTML
error pages (BSE returns HTML with 200 for missing files); **a stale "latest" zip is never accepted** because the dated member
for this BOD date must be inside it; one bad file never aborts the run; retries with backoff on 5xx; `Referer` headers for BSE/NSE.

NSE login model (preferred, added 27-Oct): `bodfetch.py browser` starts Playwright's Chromium as its own process with
`--remote-debugging-port=9222` bound to 127.0.0.1 and profile `browser-profile/`, opening the `session.login_urls` tabs.
The human logs in; `fetch`/`record` detect the port and attach (`connect_over_cdp`), and only disconnect afterwards, so
one login serves many runs. Fallback: `fetch --login` launches a headed Chromium (Playwright persistent profile); the human logs in
(CAPTCHA/OTP) and presses Enter; the script then fetches with `context.request.get(url)`, which shares the browser's
cookies. **Session cookies usually die when the browser closes, so login and fetch must happen in the same run**; `--watch`
keeps it alive and prompts for re-login if the probe fails.

### Tests (all passing on 27-Oct)

`python tests/test_fetch.py`:

1. **Parity** - the 53 names generated for 2026-10-27 equal the real UAT contract folder exactly, and Sr.No 1-62 are all in the manifest.
2. **Fake-server tests** - 404, HTML-with-200, empty file, stale zip, zip candidate dates, optional file, manual, no-URL, idempotent re-run, no leftover `.part`.
3. **Headless-browser test** - cookie-protected URL: NEEDS_LOGIN without cookie, OK with it.

### Real-world checks already done (from the user's Mac, VERIFIED)

* `curl` with a browser User-Agent returned **HTTP 200** for BSE `BhavCopy_BSE_CM_..._20261026`, `CO.zip` (2,843,787 B), `scrip.zip`
  (2,200,994 B), `Master.zip` (1,453,051 B) and NSE `series_change.csv` (186 B).
* Sizes matched UAT: BSE CM bhavcopy 882,462 B == file on .76; `series_change.csv` 186 B == file on .76.
* **Not yet done:** actually downloading the zips and comparing inner filenames and checksums with `/home/greek/Desktop/contract`
  (commands were given to the user; output not yet seen).

---------------------------------------------------------------------------------------------------

## 5b. Findings and changes, 27-Oct afternoon (Claude Code, VERIFIED by live downloads)

* 31 sheet files download with no login (`fetch --only bse_open,nse_public,slbm` -> 31 OK, re-run -> all CACHED).
* Moved to public (were `session`): NSE bhavcopy CM/FO/CD, `nsccl`/`nsccl_x` SPAN, `C_VAR1`, `ael_<T>`, `fo_secban`
  (all on `nsearchives.nseindia.com`), and BSE `BFX_CC_CO`, `BFX_PARTICIPANT`, `EQD_PARTICIPANT`, `EQ_PARTICIPANT`
  (inside the public BSE zips). Still login: 19 NSE extranet + 2 SLBM, BSE `EQD_CC_CO` + `EQ_MAP_CC`, 8 MCX.
* NSE PR zip member is now `pd<ddmmyyyy>.csv`; saved as the sheet/UAT name `Pd<ddmmyy>.csv` (`member:` field).
* **P-named = T content (RESOLVED):** BSE (CO.zip, scrip.zip, Master.zip) and NSE (`NSE_*_contract/security_<P>.csv.gz`
  on nsearchives) name masters after the evening they are generated. Proof: `BSE_EQD_CONTRACT_26102026` has 256 contracts
  with StartDt 27-Oct; `NSE_FO_contract_26102026` has 272. Rows fetch exactly P and save under UAT's T name (exception:
  scrip.zip `DP<T>` is already T-named). Never "T or P": the evening T-named file is for T+1.
* More public NSE files: `NSE_FO/CD_(spd)contract`, `NSE_CM_security` (.csv.gz, P-dated URL), `F_AEL_OPT_CONTRACTS_<T>.csv.gz`,
  `content/fo/qtyfreeze.csv` (undated; saved as `qty_freeze_<T>.csv`, format vs UAT unverified). NSE's public file index:
  `https://www.nseindia.com/api/daily-reports?key=CM|FO|CD|SLBS` (needs the homepage cookie, no login).
* Confirmed login-only: NSE `SPD0000.md` (extranet only; discontinued from 07-Dec-2026 per NSE/MSD/76877), `ael_NIFTY_Options`,
  `FreeFloat`, `nnf_*`, `participant.txt`, `fo/cd_participant`, all `*_stream_info`, SLB `participant_slb`/`security_slb`;
  BSE `EQD_CC_CO`, `EQ_MAP_CC` (ICCL common-contract-note files, member extranet); all 8 MCX (mcxindia/mcxccl return 403 to
  scripts via Akamai; not bypassed). `Settlor.txt`: no source.
* Result 27-Oct: 37/53 UAT files fetched with UAT's exact names, no login.
* **BSE CC files solved (27-Oct, VERIFIED; idea from a colleague's bse.py):** member.bseindia.com's login page has a
  PUBLIC "Download Common Files" grid (ASP.NET, every click is a __doPostBack form post): <segment> > COMMON > MON-YYYY >
  DD-MM-YYYY. `EQ_MAP_CC_<P>.csv` (EQ) and `EQD_CC_CO<P>.csv` (FNO) sit in the P folder and are saved under UAT's T names
  (EQD_CC_CO has expiries 27-Oct and none 26-Oct). Row type `bsegrid` (class `BseGrid`). No BSE login needed at all now.
* **MCX solved (27-Oct, VERIFIED):** MCX member files are on a Serv-U web client at https://sftp.mcxindia.com/ (shared
  "Common User ID For FTP"; ID/password only, in `.env`). The `/Web Client/Mobile/MLogin.html` URL is rejected by MCX's
  firewall - use the root. **Plain HTTP, no browser** (class `ServU`): GET `/` (cookies), POST
  `/Web Client/Login.xml?Command=Login&Sync=<ms>` form `user,pword,viewshare,language` -> XML `<CsrfToken>`; then with
  header `X-CSRF-Token`: `/?Command=List&Dir=<path>&ListFile=List.json&NewWebClient=1` and
  `/?Command=Download&File=<path>` (paths unencoded except spaces; encodeURIComponent breaks it). A request WITHOUT the
  token makes the server clear the session. Row type `servu`. Locations: `/Common/DailyIndexFile<P>.csv`,
  `/Common/Bhavcopy_MCX_CO_..._<P>_F_0000.csv`, `/Common/Contract Master/CTCLContractMaster/*.bcp` (~01:51 on T),
  `/Common/RPFFile/MCXRPF-<T>-<hhmm>-01-I.spn` (BOD file ~06:00 on T). 7/8 MCX fetched unattended.
  OPEN: `DailyMargin_<T><hhmmss>.csv` is from mcxccl.com (403 to scripts); extranet has
  `/Common/Daily Margin/<YYYY>/<Month>/Margin Detail Report <T>.csv` - same data? ask ops before switching.
* `BhavCopy_BSE_CD_..._20261026` from BSE is 311 bytes (header only) - probably no BSE currency trades; check vs UAT.
* `--only sheet` = the 62 numbered rows. Root-level duplicate copies of the code were removed; this folder is the project.

## 6. Known gaps / limitations (honest list)

* **No NSE/`member.bseindia.com`/MCX URLs yet.** 31 rows are `type: session` with no `url`. Fill via `record`. Also set
  `settings.session.login_urls` and `probe_url`. NSE portal name/login flow is unconfirmed.
* Rows with a wildcard name (`MCXRPF-<date>-*.spn`, `DailyMargin_<date>*.csv`) are reported `NEEDS_URL` until an exact name rule is known.
* `series_change.csv` and other undated files can't be freshness-checked by name (consider content/hash-vs-yesterday).
* No content-level validation yet (header rows, row counts vs yesterday, date inside file).
* No scheduler, no notifications (trading-group channel unknown), no push-to-UAT, no startup/verify stages.
* Cutoff logic assumes the script runs in IST. Holidays = NSE CM calendar (MCX may differ).
* The logged-in probe is a heuristic (status 200, URL and HTML body not containing `login_marker`).
* The login-expiry prompt needs an interactive terminal; unattended runs just report NEEDS_LOGIN.

---------------------------------------------------------------------------------------------------

## 7. Roadmap (suggested order)

1. **Finish Stage 1 (fetch)**
   a. Re-run the zip download + inner-filename + checksum comparison against `/home/greek/Desktop/contract` (shadow parity). Make it a script (`compare_with_uat.py`, read-only over SSH).
   b. `python bodfetch.py fetch --only bse_open,nse_public,slbm` on a real evening; fix surprises (headers, VAR date rule).
   c. Run `record`, fill NSE/member URLs; set `login_url`/`probe_url`; test `fetch --login --watch`.
   d. Resolve `qty_freeze` and `Settlor.txt` sources; decide MCX/SLBM scope; add content validators.
   f. Shadow-run 5-10 trading days: staged files == human's files (checksums). That is the acceptance gate.
2. **Push stage:** SFTP to .76 with a dedicated key-auth service account (not `greek`, not root), back up the contract folder, unzip,
   atomic swap, `--dry-run`, optional approval flag. Needs confirmation of how the Manager ingests the folder (3.1).
3. **Run environment:** always-on Linux VM with internet + SSH to .76 + scheduler (cron/systemd timer every 20-30 min, evening-to-morning),
   secrets in an env file / vault, logs, notifications to the trading group (channel unknown).
4. **Startup stage:** replace fixed sleeps with readiness checks; first 5 apps serial, other 10 parallel (small stagger until Greeksoft confirms).
   Readiness sources to investigate: log files (`logsfile_*` folder on the .76 desktop), listening ports (`ss -ltn`), process list,
   MySQL tables behind the Dashboard (read-only), terminal output ("MAIN MENU"). Needs a pty (tmux not installed).
5. **Verify + report stage**, then **Rupeeseed** BOD checks (separate runbook, read-only SSH checks + DB queries).

Pending discovery commands for .76 (read-only; the user had not yet pasted the output):
`ls -la ~/Desktop`; `ls -la ~/Desktop/AUG_2026/GETSManager_*`; `ss -ltn`; `systemctl list-units --type=service | grep -i -E "vnc|mysql"`;
`sudo -n true`; `python3 -m zipfile -l` on the two zips in `contract/`. On .73: screenshot the `27-10` folder listing and the `e:` drive path.

---------------------------------------------------------------------------------------------------

## 8. Working rules for Claude Code in this repo

* **Read-only on the servers by default.** Anything that writes, starts, stops or restarts on .76/.73 needs the user's explicit OK.
* **Never put secrets in the repo, prompts or logs.** No passwords from the source PDFs, no portal credentials, no cookies/tokens
  (`recorded_urls.json` and the browser profile are git-ignored for that reason). Credentials go in env vars / `.env` / a vault.
* **Do not bypass CAPTCHA/OTP or hammer exchange sites.** Human logs in; poll politely (20-30 min cadence, backoff).
* Check the NSE/BSE portal terms of use before scripting member-credential logins; prefer an official programmatic route (SFTP/API) if NSE offers one.
* Keep the manifest the single source of truth; add files by editing it, not the code. Keep the parity test green
  (`python tests/test_fetch.py`) and add a new fixture listing when UAT's folder changes.
* Don't invent URLs, holidays or file rules: record or ask.
* Prefer small, verifiable steps; show real output before claiming something works.
* The user is not an experienced Linux/Windows admin: explain commands, give one step at a time, say where to run each command
  (Mac Terminal vs the `greek@greek` SSH session vs RDP/.73).

### Dev quick start (Mac)

```bash
cd bod-fetch && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
python tests/test_fetch.py
python bodfetch.py plan --date 2026-10-27
python bodfetch.py fetch --only bse_open
```

---------------------------------------------------------------------------------------------------

## 9. Open questions for the user / vendors

* Who puts the files in `Daily Files\<dd-mm>` today and from which portals? Where do `qty_freeze` and `Settlor.txt` come from? What is on drive `e:` and in `NEW_CONTRACTSECURITY.zip`?
* NSE: which portal/URL, and is there an official SFTP/API for members? How long does a login session last?
* Is `member.bseindia.com` really open for the six CC/participant files?
* Greeksoft: how does the Manager ingest the contract folder; is there a readiness log/API per component; can components be restarted individually;
  is it safe to start components 6-15 in parallel; is production identical to UAT; does the licence allow scripted startup?
* Who approves file swaps/restarts and who receives alerts; which channel is the "trading group" and can a bot post there?
* Who owns the always-on runner VM and the dedicated service account/keys on .76?
* Who last edited `AUTO/BOD_F` (23-Oct) and is anyone else changing UAT startup?

---------------------------------------------------------------------------------------------------

## 10. Glossary

BOD/EOD = beginning/end of day. **Bhavcopy** = exchange end-of-day price file. **SPAN** (`.spn`) = risk parameter file from the
clearing corp; **VaR** = value-at-risk margin file. **CC/CO** = BSE clearing/contract files; **DPR** (`DP*`) = daily price range;
**CI**, **REG_IND**, **scrip** = BSE security masters. **TBT / stream_info** = tick-by-tick feed contract masters. **AEL/exposure** = NSE limit files.
Segments: CM = cash/equity, FO = futures & options, CD/CDS = currency derivatives, SLBM = securities lending, MCX = commodities.
Greeksoft apps: CTCLManager, SMM, ClientCommunicator, RMS, GATS (core five); NSEInteractive_TM, ETIInteractive, *Broadcast*,
MarginCalculator, AdvanceChartManager (the rest). Rupeeseed = the production platform vendor/system, with UDP, OMS and Analytical servers.
