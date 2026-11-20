"""The phone's "Remove Fixer everywhere" on the computer side (11-20): only with the confirmation word, only when the
install has its uninstaller, and it hands a TEMP copy to a scheduled task (stubbed here; the real launch was tested
against a throwaway install). Run: python uninstall_test.py"""
import tempfile
import threading
from pathlib import Path

import fixer_desktop as fd

fails = checks = 0


def check(ok, what):
    global fails, checks
    checks += 1
    if not ok:
        fails += 1
        print("FAIL", what)


home = Path(tempfile.mkdtemp(prefix="fxhome-"))
fd.ROOT = home
calls = []
fd._schedule_uninstall = lambda script, task: calls.append((Path(script), task))

for p in ({}, {"confirm": "yes"}):
    try:
        fd.job_uninstall(p, [].append, threading.Event())
        check(False, f"refused without the word: {p}")
    except fd.UserFacing:
        check(not calls, "nothing scheduled when not confirmed")
try:
    fd.job_uninstall({"confirm": "remove-everything"}, [].append, threading.Event())
    check(False, "refused when the install has no uninstaller")
except fd.UserFacing as e:
    check("too old" in str(e), "says the install is too old")

(home / "uninstall.ps1").write_text("# the uninstaller", encoding="utf-8")
out = []
fd.job_uninstall({"confirm": "remove-everything"}, out.append, threading.Event())
check(len(calls) == 1 and calls[0][1].startswith("FixerUninstall-"), "a one-off task is scheduled")
script = calls[0][0]
check(script.is_file() and script.parent != home and script.read_text(encoding="utf-8") == "# the uninstaller",
      "the uninstaller runs from a copy outside the install")
check(out == [{"t": "info", "uninstalling": True, "home": str(home)}], "the phone is told it is uninstalling")
check("uninstall" in fd.IMMEDIATE and "uninstall" not in fd.QUEUED_OPS, "answers at once, never waits in line")
script.unlink(missing_ok=True)

print(f"{checks} checks, {fails} failed")
raise SystemExit(1 if fails else 0)
