# BOD - bodfetch

Automation of the daily BOD (Beginning of Day) routine for the Greeksoft UAT trading platform.

- `bodfetch.py fetch` downloads the daily exchange/contract files (BSE, NSE, MCX) listed in the GreekSoft
  "Servers List with Configuration - BOD Files" sheet into `downloads/<BOD date>/`.
- `bodfetch.py push` copies them to the UAT server (.76) over SFTP (see "Push to the UAT server" below).

Not in this repo on purpose: the source documents (PDF/DOCX), `.env` credentials and the login browser profile
(see `.gitignore`). `downloads/` IS committed (each day's files + state/report/push logs; ~300 MB per trading day).

## Setup (Mac, once)

```bash
cd bod-fetch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # the login browser
```

`holidays.txt` fills itself from NSE's public holiday list (once per year, on the first run that needs it).
Add extra closures by hand as `YYYY-MM-DD  # reason # manual`.

## Which files need a login

| Source | Files | Login |
|---|---|---|
| BSE public site (incl. the member site's public "Download Common Files" grid) + NSE public archive | 39 of the 62 sheet rows, plus `qty_freeze` | none |
| NSE member extranet | 15 (`nse_member`, 2 `slbm`) | human: CAPTCHA/OTP |
| MCX member file server (Serv-U, sftp.mcxindia.com) | 7 (`mcx`) | automatic, plain HTTP login with ID/password from `.env` |
| MCXCCL daily margin page (refuses scripts) | 1 (`DailyMargin_*`) | open question |

## Daily use

```bash
source .venv/bin/activate
python bodfetch.py plan --only sheet            # what will be fetched, with today's dates

# 1. public files, no login
python bodfetch.py fetch --only bse_open,nse_public,slbm

# 2. login files
python bodfetch.py browser                      # opens a Chromium window; log in to each portal tab
python bodfetch.py fetch --only sheet           # uses that logged-in window; leave it open
```

The `browser` window keeps running after the command returns. Every `fetch` attaches to it, so you
log in once and can re-run `fetch` (or `fetch --watch --interval 20 --until 07:00`) as files publish.
Close the window when done. It listens on 127.0.0.1:9222 only, and its profile (`browser-profile/`)
holds your session cookies: never copy or commit it.

Alternative without a separate window: `fetch --login` opens a browser inside that run and waits
for Enter after you log in.

## MCX login (ID + password, no CAPTCHA/OTP, no browser)

```bash
cp .env.example .env && chmod 600 .env     # then fill MCX_USER, MCX_PASSWORD
python bodfetch.py login mcx               # optional: test the login
python bodfetch.py fetch --only mcx        # logs in over HTTP by itself
```

`.env` is git-ignored; values are never printed. Everything except the NSE extranet runs with plain
HTTP (`requests`); the browser (Playwright) is only for the NSE login.

## Push to the UAT server (no RDP/.73 hop)

`bodfetch.py push` copies the day's files straight from this machine to the UAT server (.76) over SFTP -
the same protocol WinSCP uses - following the procedure's "staging folder first, then replace":
stage in `bod_push/staging/<date>/` -> check sha256 on the server -> back up files it will overwrite to
`bod_push/backup/<date>/` -> swap into the target -> verify. The target is untouched until every file is
staged and verified; nothing else in it is changed or deleted. Target now: `~/Desktop/contract_test`
(testing). Writing into the live `contract` folder needs `--live`.

One-time SSH key setup (Mac Terminal; asks your key passphrase, then greek's password once):
```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
ssh-copy-id greek@192.168.1.76
```

```bash
python bodfetch.py push --dry-run                 # what would change; writes nothing
python bodfetch.py push                           # refuses if any Servers List file is missing
python bodfetch.py push --allow-partial           # push what is ready (while NSE login files are pending)
python bodfetch.py push --local /tmp/fake_uat     # same flow into a local folder, no server needed
```

## One-time: record the download URLs for the login files

The sheet lists NSE folders (`/CMCOMMON/ntneat` ...), not download URLs. With the `browser`
window open and logged in:

```bash
python bodfetch.py record          # press Enter (or Ctrl+C) when done
```

In the window, download ONE file from each folder by hand: NSE `CMCOMMON/ntneat`, `CMCOMMON/tbtmaster`,
`CDSCOMMON`, `CDSCOMMON/tbtmaster`, `FAOCOMMON`, `FAOCOMMON/Bhavcopy`, `FAOCOMMON/Limit Files`,
`FAOCOMMON/tbtmaster`, `SLBCOMMON/NTNEAT`; the BSE member CC files; MCX. `recorded_urls.json` then
holds every request (header names only, no cookie values). Put each download URL into `manifest.yaml`
as `url:` on its row, with `{T:...}` / `{P:...}` date tokens. Also set `session.login_urls` and
`session.probe_url`.

## File dates

T = BOD date, P = previous trading day. BSE and NSE name contract, participant and scrip masters after
the evening they are generated (P), but the content is for T. Checked 27-Oct-2026:
`BSE_EQD_CONTRACT_26102026` and `NSE_FO_contract_26102026` hold contracts starting 27-Oct. Those rows
fetch exactly the P-named file and save it under the T name UAT uses (`member:` / P-dated `url`).
They take exactly P, never "T or P": in the evening the T-named file already exists and is for the NEXT day.

## Statuses

| Status | Meaning |
|---|---|
| OK / CACHED | file fetched and valid / already fetched earlier |
| OPTIONAL_ABSENT | optional file not published (e.g. `fo_secban.csv`) |
| NOT_PUBLISHED | not there yet (404, HTML page, or stale zip) - retried by `--watch` |
| NEEDS_LOGIN | no logged-in browser, or the session expired |
| NEEDS_URL | row has no URL yet: use `record` |
| MANUAL | no known source (`qty_freeze`, `Settlor.txt`; not in the sheet) |
| FAILED | download or validation error: see the detail column |

Exit code is 0 only when every selected file is OK / CACHED / OPTIONAL_ABSENT.
