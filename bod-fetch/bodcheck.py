"""bodcheck - look INSIDE the fetched files and flag ones that are old, cut off or changed shape.

    python bodcheck.py                          # the newest downloads/<date>/ folder vs the one before it
    python bodcheck.py --date 2026-10-28
    python bodcheck.py --date 2026-10-28 --prev 2026-10-27 --json report.json

Two kinds of checks, rules in checks.yaml:
  1. the file on its own    the date written inside matches T/P, no contract already expired, SPAN XML is complete,
                            the .xls really is an Excel file, enough rows
  2. vs the previous day    same first line (header), row count within max_change
Statuses: OK, WARN (looks unusual - a human should glance at it), FAIL (do not push this file).
Reads only; never changes a file. Exit code 1 if anything FAILs.

What it does NOT prove: that a file equals the one the team puts on the UAT server by hand. That needs the
server's copy (checksum comparison) - see CLAUDE.md roadmap 1a.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import sys

import yaml

import bodfetch

HERE = pathlib.Path(__file__).resolve().parent
NSE_EPOCH = dt.datetime(1980, 1, 1)
csv.field_size_limit(1 << 24)


def parse_date(value: str, fmt: str) -> dt.date | None:
    """None for the 'no date' markers (blank, 0, -1)."""
    value = value.strip()
    if value in ("", "0", "-1"):
        return None
    if fmt == "nse1980":
        return (NSE_EPOCH + dt.timedelta(seconds=int(value))).date()
    return dt.datetime.strptime(value, fmt).date()


def count_lines(path: pathlib.Path) -> int:
    n, last = 0, b"\n"
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    return n + (last != b"\n")  # a last line without a newline still counts


def first_line(path: pathlib.Path) -> str:
    with open(path, "rb") as f:
        return f.readline().decode("latin-1").rstrip("\r\n")


def rows(path: pathlib.Path, col, header: bool):
    """Values of column `col` (a name needs a header row; an int is a 0-based index), one per data row."""
    with open(path, newline="", encoding="latin-1") as f:
        reader = csv.reader(f)
        if isinstance(col, str):
            idx = next(reader).index(col)
        else:
            idx = col
            if header:
                next(reader, None)
        for r in reader:
            if len(r) > idx:
                yield r[idx]


def wanted(spec, T: dt.date, P: dt.date) -> set[dt.date]:
    names = spec if isinstance(spec, list) else [spec]
    return {{"T": T, "P": P}[n] for n in names}


def check_file(path: pathlib.Path, rule: dict, T: dt.date, P: dt.date, prev: pathlib.Path | None,
               max_change: float) -> tuple[str, list[str]]:
    fails, warns = [], []
    header = rule.get("header", True)
    size = path.stat().st_size
    if size == 0:
        return "FAIL", ["empty file"]

    if "magic" in rule:
        with open(path, "rb") as f:
            if f.read(len(rule["magic"]) // 2).hex() != rule["magic"]:
                fails.append("does not start like the expected file type")
        return ("FAIL" if fails else "OK"), fails or ["file type ok"]

    notes = []
    lines = count_lines(path)
    data_rows = lines - (1 if header else 0)
    if data_rows < rule.get("min_rows", 1):
        fails.append(f"only {data_rows} data rows (need {rule.get('min_rows', 1)})")

    if "head_date" in rule:
        hd = rule["head_date"]
        with open(path, "rb") as f:
            head = "".join(f.readline().decode("latin-1") for _ in range(5))
        m = re.search(hd["re"], head, re.M)
        if not m:
            fails.append("date not found in the first lines")
        else:
            d = parse_date(m[1], hd["fmt"])
            ok = wanted(hd["is"], T, P)
            (notes if d in ok else fails).append(f"date inside {d} " + ("ok" if d in ok else f"expected {' or '.join(map(str, sorted(ok)))}"))

    if "col_date" in rule:
        cd = rule["col_date"]
        ok = wanted(cd["is"], T, P)
        bad, first_bad, n = 0, None, 0
        for v in rows(path, cd["col"], header):
            n += 1
            d = parse_date(v, cd["fmt"])
            if d not in ok:
                bad += 1
                first_bad = first_bad or v
        if bad:
            fails.append(f"{bad}/{n} rows have {cd['col']} != {' or '.join(map(str, sorted(ok)))} (e.g. {first_bad!r})")
        elif n:
            notes.append(f"all {n} rows {cd['col']} = {' or '.join(map(str, sorted(ok)))}")

    if "expiry" in rule:
        ex = rule["expiry"]
        expired, first_bad, n, unreadable = 0, None, 0, 0
        for v in rows(path, ex["col"], header):
            try:
                d = parse_date(v, ex["fmt"])
            except ValueError:
                unreadable += 1
                continue
            if d is None:
                continue
            n += 1
            if d < T:
                expired += 1
                first_bad = first_bad or d
        if expired:
            fails.append(f"{expired}/{n} contracts already expired before {T} (e.g. {first_bad})")
        else:
            notes.append(f"{n} contracts, none expired")
        if unreadable:
            warns.append(f"{unreadable} expiry values not readable as {ex['fmt']}")

    if "tail" in rule:
        with open(path, "rb") as f:
            f.seek(max(0, size - 256))
            if not f.read().rstrip().endswith(rule["tail"].encode()):
                fails.append(f"does not end with {rule['tail']} (cut off?)")

    if prev is None:
        warns.append("no previous-day file to compare with")
    else:
        prev_lines = count_lines(prev)
        if prev_lines and abs(lines - prev_lines) / prev_lines > max_change:
            warns.append(f"rows {prev_lines} -> {lines} ({(lines - prev_lines) / prev_lines:+.0%} vs previous day)")
        else:
            notes.append(f"rows {prev_lines} -> {lines}")
        if header and first_line(path) != first_line(prev):
            warns.append("first line (header) differs from the previous day")

    status = "FAIL" if fails else "WARN" if warns else "OK"
    return status, fails + warns + notes


def day_files(day_dir: pathlib.Path) -> dict[str, str]:
    """manifest id -> saved file name, from the day's state.json."""
    state = json.loads((day_dir / "state.json").read_text())
    return {k: v["name"] for k, v in state.items()}


def run(day_dir: pathlib.Path, prev_dir: pathlib.Path | None, rules_cfg: dict, T: dt.date, P: dt.date) -> list[dict]:
    rules = rules_cfg["rules"]
    default_change = rules_cfg.get("defaults", {}).get("max_change", 0.5)
    prev_names = day_files(prev_dir) if prev_dir else {}
    out = []
    for key, name in sorted(day_files(day_dir).items(), key=lambda kv: kv[1].lower()):
        path = day_dir / name
        if not path.exists():
            out.append({"id": key, "file": name, "status": "FAIL", "messages": ["missing (in state.json, not on disk)"]})
            continue
        rule = next((r for r in rules if re.search(r["match"], name)), {})
        prev = prev_dir / prev_names[key] if key in prev_names and (prev_dir / prev_names[key]).exists() else None
        status, msgs = check_file(path, rule, T, P, prev, rule.get("max_change", default_change))
        out.append({"id": key, "file": name, "status": status, "messages": msgs})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="BOD date YYYY-MM-DD (default: newest folder in downloads/)")
    ap.add_argument("--prev", help="folder to compare with (default: the one before --date)")
    ap.add_argument("--downloads", type=pathlib.Path, default=HERE / "downloads")
    ap.add_argument("--rules", type=pathlib.Path, default=HERE / "checks.yaml")
    ap.add_argument("--json", type=pathlib.Path, help="also write the results here")
    a = ap.parse_args(argv)

    days = sorted(p.name for p in a.downloads.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))
    if not days:
        print(f"no day folders in {a.downloads}")
        return 2
    day = a.date or days[-1]
    earlier = [d for d in days if d < day]
    prev = a.prev or (earlier[-1] if earlier else None)
    T = dt.date.fromisoformat(day)
    _, _, _, cal = bodfetch.load(HERE / "manifest.yaml")  # reads holidays.txt only
    P = cal.prev_before(T)

    results = run(a.downloads / day, a.downloads / prev if prev else None, yaml.safe_load(a.rules.read_text()), T, P)
    print(f"BOD {T} (P = {P}), compared with {prev or 'nothing'}\n")
    for r in results:
        print(f"{r['status']:<5} {r['file']}")
        for m in r["messages"]:
            print(f"        {m}")
    counts = {s: sum(r["status"] == s for r in results) for s in ("OK", "WARN", "FAIL")}
    print(f"\n{counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL")
    if a.json:
        a.json.write_text(json.dumps({"date": day, "prev": prev, "results": results}, indent=2))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
