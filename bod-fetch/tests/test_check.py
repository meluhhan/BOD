"""Tests for bodcheck (small made-up files, no network). Run:  python tests/test_check.py"""
import datetime as dt
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bodcheck as bc  # noqa: E402
import yaml  # noqa: E402

RULES = yaml.safe_load((ROOT / "checks.yaml").read_text())
T, P = dt.date(2026, 10, 28), dt.date(2026, 10, 27)
BHAV_HEAD = "TradDt,BizDt,Sgmt\n"


def _day(files: dict) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    for i, (name, text) in enumerate(files.items()):
        (d / name).write_bytes(text if isinstance(text, bytes) else text.encode())
    (d / "state.json").write_text(json.dumps({str(i): {"name": n} for i, n in enumerate(files)}))
    return d


def _check(files: dict, prev_files: dict | None = None) -> dict:
    prev = _day(prev_files) if prev_files else None
    return {r["file"]: r for r in bc.run(_day(files), prev, RULES, T, P)}


def _bhav(day: str, n: int = 150) -> str:
    return BHAV_HEAD + f"{day},{day},CM\n" * n


def test_bhavcopy_dates():
    r = _check({"BhavCopy_NSE_CM_0_0_0_20261027_F_0000.csv": _bhav("2026-10-27")})
    assert r["BhavCopy_NSE_CM_0_0_0_20261027_F_0000.csv"]["status"] == "WARN"  # only "no previous day"
    old = _check({"BhavCopy_NSE_CM_0_0_0_20261027_F_0000.csv": _bhav("2026-10-26")})
    assert old["BhavCopy_NSE_CM_0_0_0_20261027_F_0000.csv"]["status"] == "FAIL"   # stale content, right name


def test_compare_with_previous_day():
    name = "BhavCopy_NSE_CM_0_0_0_20261027_F_0000.csv"
    ok = _check({name: _bhav("2026-10-27", 150)}, {"BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv": _bhav("2026-10-26", 160)})
    assert ok[name]["status"] == "OK", ok
    cut = _check({name: _bhav("2026-10-27", 120)}, {"BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv": _bhav("2026-10-26", 400)})
    assert cut[name]["status"] == "WARN" and any("rows 401 -> 121" in m for m in cut[name]["messages"])
    hdr = _check({name: "TradDt,NEWCOL\n" + "2026-10-27,x\n" * 150},
                 {"BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv": _bhav("2026-10-26", 150)})
    assert any("header" in m for m in hdr[name]["messages"])


def test_head_date_and_span():
    good_span = '<?xml version="1.0"?>\n<spanFile>\n<created>202610280201\n</spanFile>\n'
    r = _check({"C_VAR1_28102026_1.DAT": "10,28102026,0.00,1\n" + "20,X\n" * 150,
                "nsccl.20261028.i01.spn": good_span,
                "nsccl_x.20261028.i01.spn": good_span.replace("</spanFile>\n", "<unfinished")})
    assert "date inside 2026-10-28 ok" in r["C_VAR1_28102026_1.DAT"]["messages"]
    assert r["nsccl.20261028.i01.spn"]["status"] != "FAIL"
    assert r["nsccl_x.20261028.i01.spn"]["status"] == "FAIL"                        # cut off
    stale = _check({"C_VAR1_28102026_1.DAT": "10,27102026,0.00,1\n" + "20,X\n" * 150})
    assert stale["C_VAR1_28102026_1.DAT"]["status"] == "FAIL"


def test_contract_expiry():
    head = "FinInstrmId,XpryDt\n"
    nse_28oct = int((dt.datetime(2026, 10, 28, 14, 30) - bc.NSE_EPOCH).total_seconds())
    r = _check({"NSE_FO_contract_28102026.csv": head + f"1,{nse_28oct}\n2,0\n3,-1\n",
                "BSE_EQD_CONTRACT_28102026.csv": head + "1,27-OCT-2026\n2,26-NOV-2026\n"})
    assert r["NSE_FO_contract_28102026.csv"]["status"] != "FAIL"   # expires today = still tradable; 0/-1 ignored
    assert r["BSE_EQD_CONTRACT_28102026.csv"]["status"] == "FAIL"  # one contract expired yesterday


def test_xls_magic_and_empty():
    r = _check({"VAR281026.xls": bytes.fromhex("d0cf11e0a1b11ae1") + b"rest",
                "VAR291026.xls": b"<html>error</html>",
                "DP281026": b""})
    assert r["VAR281026.xls"]["status"] == "OK"
    assert r["VAR291026.xls"]["status"] == "FAIL"
    assert r["DP281026"]["status"] == "FAIL"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
