"""Unit tests for routines.py scheduling (no network, temp files)."""
import datetime as dt
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import routines as R  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"   {detail}"))


tmp = tempfile.mkdtemp(prefix="bena_routines_")
R.JOBS_PATH = os.path.join(tmp, "routines.json")
R.FEED_PATH = os.path.join(tmp, "feed.json")
R.LOCK_PATH = os.path.join(tmp, "routines.lock")

# Thursday 24 Sep 2026, 21:30  (Python weekday 3 = Thu)
NOW = dt.datetime(2026, 9, 24, 21, 30)


def nxt(text, after=NOW, created=None):
    return R.next_run(R.parse_schedule(text), after, created or R._iso(after))


print("[schedule parsing -> next run]  (now = Thu 2026-09-24 21:30)")
cases = [
    ("in 30m",                   dt.datetime(2026, 9, 24, 22, 0)),
    ("in 2 hours",               dt.datetime(2026, 9, 24, 23, 30)),
    ("every 15m",                dt.datetime(2026, 9, 24, 21, 45)),
    ("every 2h",                 dt.datetime(2026, 9, 24, 23, 30)),
    ("daily at 08:00",           dt.datetime(2026, 9, 25, 8, 0)),
    ("every day at 10:15pm",     dt.datetime(2026, 9, 24, 22, 15)),
    ("weekdays at 08:00",        dt.datetime(2026, 9, 25, 8, 0)),     # Fri counts (Mon-Fri)
    ("every sun-thu at 08:00",   dt.datetime(2026, 9, 27, 8, 0)),     # skips Fri+Sat
    ("every monday at 9am",      dt.datetime(2026, 9, 28, 9, 0)),
    ("every mon,wed at 18:00",   dt.datetime(2026, 9, 28, 18, 0)),
    ("every fri,sat at 10:00",   dt.datetime(2026, 9, 25, 10, 0)),
    ("at 2026-09-26 14:30",      dt.datetime(2026, 9, 26, 14, 30)),
    ("0 8 * * 0-4",              dt.datetime(2026, 9, 27, 8, 0)),     # cron Sun-Thu
    ("*/20 * * * *",             dt.datetime(2026, 9, 24, 21, 40)),
    ("0 9 1 * *",                dt.datetime(2026, 10, 1, 9, 0)),
    ("30 7 * * 7",               dt.datetime(2026, 9, 27, 7, 30)),    # 7 = Sunday too
]
for text, want in cases:
    try:
        got = nxt(text)
    except ValueError as e:
        got = f"ValueError: {e}"
    check(f"{text:26} -> {want:%a %d %H:%M}", got == want, got)

print("\n[bad input is rejected with a useful message]")
for bad in ["whenever", "every 1m", "at 2026-02-30 10:00", "99 * * * *", "in 0m", ""]:
    try:
        R.parse_schedule(bad)
        check(f"rejects {bad!r}", False, "accepted")
    except ValueError as e:
        check(f"rejects {bad!r}", True)

print("\n[one-offs]")
check("past one-off -> None", nxt("at 2026-09-20 10:00") is None)
spec = R.parse_schedule("in 30m")
check("'in 30m' anchors to creation, not to each check",
      R.next_run(spec, NOW + dt.timedelta(minutes=10), R._iso(NOW)) == NOW + dt.timedelta(minutes=30))

print("\n[jobs + scheduler with a fake runner]")
delivered = []
ran = []


def runner(job):
    ran.append(job["name"])
    return "ok", f"done: {job['name']}", ["get_weather"]


sched = R.Scheduler(runner, lambda t, b: delivered.append((t, b)))
r = R.create_job("Morning", "daily at 08:00", "brief me")
check("create", r["ok"] and r["next_run"], r)
r = R.create_job("morning", "every 2h", "dup")
check("duplicate name refused", not r["ok"], r)
r = R.create_job("Nonsense", "whenever you like", "x")
check("bad schedule refused at creation", not r["ok"] and "couldn't read" in r["error"], r)
R.create_job("Soon", "in 5m", "one-off")
jobs = {j["name"]: j for j in R.list_jobs()}
future = R._parse_iso(jobs["Soon"]["next_run"]) + dt.timedelta(minutes=1)
due = sched.tick(now=future)
time.sleep(0.5)
check("due job runs on tick", "Soon" in ran, ran)
check("result delivered", any("done: Soon" in b for _, b in delivered), delivered)
jobs = {j["name"]: j for j in R.list_jobs()}
check("one-off disables itself after running", jobs["Soon"]["enabled"] is False and jobs["Soon"]["runs"] == 1, jobs["Soon"])
check("not-yet-due job untouched", "Morning" not in ran)
check("feed recorded", R.feed()[0]["name"] == "Soon")

r = sched.run_now("morning")
time.sleep(0.5)
check("run now", r["ok"] and "Morning" in ran, r)
nr_before = jobs["Morning"]["next_run"]
check("manual run keeps the schedule", {j["name"]: j for j in R.list_jobs()}["Morning"]["next_run"] == nr_before)

check("disable", R.set_enabled("Morning", False)["ok"])
far = dt.datetime.now() + dt.timedelta(days=3)
ran.clear(); sched.tick(now=far); time.sleep(0.3)
check("disabled job does not run", "Morning" not in ran, ran)
check("delete", R.delete_job("Morning")["ok"] and not any(j["name"] == "Morning" for j in R.list_jobs()))

print("\n[one scheduler per machine]")
other = R.Scheduler(runner, lambda *a: None)
with open(R.LOCK_PATH, "w") as f:
    f.write("999999")                     # another live process holds it
os.utime(R.LOCK_PATH)
check("second scheduler stands by while the lock is fresh", other._acquire() is False)
old = time.time() - 600
os.utime(R.LOCK_PATH, (old, old))
check("stale lock is taken over", other._acquire() is True)
other.release()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
