"""Tests for bodpush (no server needed: LocalRemote stands in for the UAT server). Run:  python tests/test_push.py"""
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bodpush as bp  # noqa: E402

DAY = dt.date(2026, 10, 27)


def _setup():
    """A fetched day folder with 3 files (+ state.json) and a fake server with an older copy of one."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    out = tmp / "downloads" / DAY.isoformat()
    out.mkdir(parents=True)
    state = {}
    for key, name, data in [("1", "A.csv", b"a-new"), ("2", "B.csv", b"b-data"), ("3", "C.spn", b"c" * 5000)]:
        (out / name).write_bytes(data)
        state[key] = {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (out / "state.json").write_text(json.dumps(state))
    srv = tmp / "server"
    target = srv / "contract_test"
    target.mkdir(parents=True)
    (target / "A.csv").write_bytes(b"a-old")            # will be REPLACED (and backed up)
    (target / "B.csv").write_bytes(b"b-data")           # already identical: SAME, untouched
    (target / "keep_me.zip").write_bytes(b"not ours")   # not in our set: must never be touched
    paths = bp.day_paths(str(target), str(srv / "staging"), str(srv / "backup"), DAY)
    entries = [{"id": 1, "name": "A.csv"}, {"id": 2, "name": "B.csv"}, {"id": 3, "name": "C.spn"}]
    return tmp, out, target, paths, entries


def test_full_flow_and_rerun():
    tmp, out, target, paths, entries = _setup()
    try:
        items, missing = bp.collect(out, entries, lambda e: e["name"])
        assert missing == [] and len(items) == 3
        r = bp.LocalRemote()
        assert {it.name: it.action for it in bp.plan(items, r, paths)} == {"A.csv": "REPLACE", "B.csv": "SAME", "C.spn": "NEW"}
        rep = bp.push(items, r, paths, log=lambda m: None)
        assert rep["result"] == "OK"
        assert (target / "A.csv").read_bytes() == b"a-new" and (target / "C.spn").read_bytes() == b"c" * 5000
        assert (pathlib.Path(paths.backup) / "A.csv").read_bytes() == b"a-old"      # previous version kept
        assert (target / "keep_me.zip").read_bytes() == b"not ours"                 # others untouched
        assert not list(pathlib.Path(paths.staging).iterdir())                      # staging emptied by the swap
        assert not pathlib.Path(paths.lock).exists()                                # lock released
        again = bp.push(bp.plan(bp.collect(out, entries, lambda e: e["name"])[0], r, paths), r, paths, log=lambda m: None)
        assert again["result"] == "NOTHING_TO_DO"                                    # idempotent
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_and_tampered_files_are_reported():
    tmp, out, target, paths, entries = _setup()
    try:
        (out / "B.csv").write_bytes(b"changed after fetch")
        items, missing = bp.collect(out, entries + [{"id": 9, "name": "NEVER_FETCHED.csv"}], lambda e: e["name"])
        assert [it.name for it in items] == ["A.csv", "C.spn"]
        assert missing == [("2", "B.csv"), ("9", "NEVER_FETCHED.csv")]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bad_upload_leaves_target_untouched():
    tmp, out, target, paths, entries = _setup()

    class Corrupting(bp.LocalRemote):
        def put(self, local, path):
            pathlib.Path(path).write_bytes(b"corrupted in transit")

    try:
        items = bp.plan(bp.collect(out, entries, lambda e: e["name"])[0], Corrupting(), paths)
        try:
            bp.push(items, Corrupting(), paths, log=lambda m: None)
            raise AssertionError("expected PushError")
        except bp.PushError as ex:
            assert "checksum mismatch" in str(ex)
        assert (target / "A.csv").read_bytes() == b"a-old" and not (target / "C.spn").exists()
        assert not pathlib.Path(paths.lock).exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_lock_and_live_guard():
    tmp, out, target, paths, entries = _setup()
    try:
        pathlib.Path(paths.lock).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(paths.lock).write_text("someone else")
        items = bp.plan(bp.collect(out, entries, lambda e: e["name"])[0], bp.LocalRemote(), paths)
        try:
            bp.push(items, bp.LocalRemote(), paths, log=lambda m: None)
            raise AssertionError("expected PushError")
        except bp.PushError as ex:
            assert "another push is running" in str(ex)
        assert pathlib.Path(paths.lock).read_text() == "someone else"   # a foreign lock is never removed
        assert (target / "A.csv").read_bytes() == b"a-old"
        bp.check_target("/home/greek/Desktop/contract_test", live=False)        # test folder: fine
        for live_path in ("/home/greek/Desktop/contract", "/home/greek/Desktop/contract/"):
            try:
                bp.check_target(live_path, live=False)
                raise AssertionError("expected PushError")
            except bp.PushError:
                pass
        bp.check_target("/home/greek/Desktop/contract", live=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in (test_full_flow_and_rerun, test_missing_and_tampered_files_are_reported,
               test_bad_upload_leaves_target_untouched, test_lock_and_live_guard):
        fn()
        print("PASS", fn.__name__)
