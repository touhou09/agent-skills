"""Offline tests for `orch hold` / `orch release` (holds.py through prog.py), on invented data.

Run: python3 test_holds.py
"""
import datetime as dt, json, os, pathlib, subprocess, sys, tempfile, time

home = pathlib.Path(tempfile.mkdtemp(prefix="test-holds-"))
env = {**os.environ, "PROGRAMS_HOME": str(home), "ORCH_FLEET_STATE": str(home / "state"), "ORCA_TERMINAL_HANDLE": "term_a"}
PROG = pathlib.Path(__file__).parent / "prog.py"
locks = home / "_locks"


def fleet_time(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def orch(*args, **extra):
    r = subprocess.run([sys.executable, str(PROG), *args], capture_output=True, text=True, env={**env, **extra})
    return r.returncode, r.stdout.strip(), r.stderr.strip()


assert orch("hold", "--list") == (0, "nothing held", "")
assert orch("hold", "browser", "--note", "staging 로그인")[1] == "browser held by term_a (ttl 120m)"
# Someone else is refused and told who has it; the holder refreshing keeps the note.
code, _, err = orch("hold", "browser", ORCA_TERMINAL_HANDLE="term_b")
assert code == 1 and err.startswith("browser is held by term_a for 0m (staging 로그인)"), err
assert orch("hold", "browser")[0] == 0
assert json.loads((locks / "res__browser.lock").read_text())["note"] == "staging 로그인"
assert orch("release", "browser", ORCA_TERMINAL_HANDLE="term_b")[0] == 1
assert orch("hold", "Bad Name")[0] == 1

# A lane lock prog.py wrote sits beside the holds and is listed with them.
(locks / "acme__web@main.lock").write_text(json.dumps({"pr": 45, "program": "search", "ts": "2026-10-09T11:00:00+00:00"}))
rows = json.loads(orch("hold", "--list", "--json")[1])
assert [(r["kind"], r["resource"], r["by"], r["note"]) for r in rows] == [
    ("lane", "acme/web@main", "#45", "search"),
    ("resource", "browser", "term_a", "staging 로그인"),
], rows

# A hold past its ttl is broken by the next reader and the ledger says so.
held = json.loads((locks / "res__browser.lock").read_text())
(locks / "res__browser.lock").write_text(json.dumps({**held, "ts": "2020-01-01T00:00:00Z"}))
assert [r["kind"] for r in json.loads(orch("hold", "--list", "--json")[1])] == ["lane"]
events = [json.loads(l) for l in (locks / "holds.jsonl").read_text().splitlines()]
assert [e["ev"] for e in events] == ["lock_acquired", "lock_released"], events
assert events[1]["note"].startswith("broken after"), events[1]

assert orch("hold", "browser", "--ttl", "5")[0] == 0
assert orch("release", "browser", "--force", ORCA_TERMINAL_HANDLE="term_b")[1] == "released browser"
assert json.loads((locks / "holds.jsonl").read_text().splitlines()[-1])["note"] == "forced by term_b"
assert orch("release", "browser")[1] == "browser was not held"

# A unit with no terminal handle (a subagent's shell, a script outside Orca) names itself: units are never merged
# under one login name, where two of them would both pass as "refreshing my own hold".
code, _, err = orch("hold", "browser", ORCA_TERMINAL_HANDLE="")
assert code == 1 and "--by" in err and not (locks / "res__browser.lock").exists(), err
assert orch("release", "browser", ORCA_TERMINAL_HANDLE="")[0] == 1
assert orch("hold", "browser", "--by", "sub-a", ORCA_TERMINAL_HANDLE="")[1] == "browser held by sub-a (ttl 120m)"
assert orch("hold", "browser", "--by", "sub-b", ORCA_TERMINAL_HANDLE="")[0] == 1
assert orch("release", "browser", "--by", "sub-a", ORCA_TERMINAL_HANDLE="")[1] == "released browser"

# `hold <r> -- <command>` holds it while the command runs, refreshed well inside its ttl, and gives it back after:
# a use longer than the ttl is never broken under it. The command's exit code is the result.
(locks / "holds.jsonl").unlink()
for bad in ("0.01", "nan", "inf", "-1"):  # a ttl the refresh cannot beat, or none that ever ends
    code, _, err = orch("hold", "browser", "--ttl", bad)
    assert code == 1 and "--ttl" in err and not (locks / "res__browser.lock").exists(), (bad, err)
code, out, err = orch("hold", "browser", "--ttl", "0.1", "--", sys.executable, "-c", "import time; time.sleep(8)")
assert code == 0 and out.startswith("browser held by term_a"), (code, out, err)
events = [json.loads(l) for l in (locks / "holds.jsonl").read_text().splitlines()]
assert [(e["ev"], e.get("note")) for e in events] == [("lock_acquired", None), ("lock_released", None)], events
assert not (locks / "res__browser.lock").exists()
assert orch("hold", "browser", "--", sys.executable, "-c", "raise SystemExit(3)")[0] == 3
assert not (locks / "res__browser.lock").exists(), "released when the command fails too"
# Stopping the wrapper stops the command first: the signal goes to its whole process group (a shell's children too),
# and the hold is released only once every one of them has exited, never while one still uses the resource. A command
# a signal ended exits 128 + the signal, as a shell reports it.
mark = home / "stopped"
worker = ("import pathlib, signal, sys, time\n"
          "def stop(*_):\n"
          "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
          f"    time.sleep(8); pathlib.Path({str(mark)!r}).write_text('x'); sys.exit(0)\n"
          "signal.signal(signal.SIGTERM, stop)\ntime.sleep(30)\n")
(locks / "holds.jsonl").unlink()
proc = subprocess.Popen([sys.executable, str(PROG), "hold", "browser", "--ttl", "0.1", "--", "sh", "-c",
                         '"$0" -c "$1" & wait', sys.executable, worker], env=env, stdout=subprocess.PIPE, text=True)
assert proc.stdout.readline().startswith("browser held by term_a")
time.sleep(1)
proc.terminate()
time.sleep(1)
proc.terminate()  # a second stop while the group winds down: still forwarded, still released only at the end
assert proc.wait(timeout=30) == 143 and mark.exists() and not (locks / "res__browser.lock").exists()
events = [json.loads(l) for l in (locks / "holds.jsonl").read_text().splitlines()]
assert [(e["ev"], e.get("note")) for e in events] == [("lock_acquired", None), ("lock_released", None)], \
    "refreshed while the group wound down for longer than the 6 s ttl, never broken"
assert fleet_time(events[-1]["ts"]) >= mark.stat().st_mtime - 1, "released after the shell's child exited"

# A refresh that fails to write (a full disk) stops refreshing; the command still runs out and the hold is released.
sys.path.insert(0, str(PROG.parent))
os.environ.update(PROGRAMS_HOME=str(home))
import prog  # noqa: E402
calls = []


class FullDisk:
    def hold(self, *a):
        calls.append("hold")
        raise OSError(28, "No space left on device")

    def release(self, *a):
        calls.append("release")


assert prog.hold_while(FullDisk(), "browser", "term_a", 0.05, [sys.executable, "-c", "import time; time.sleep(2.5)"]) == 0
assert calls[0] == "hold" and calls[-1] == "release" and calls.count("hold") == 1, calls

print("test_holds: ok")
