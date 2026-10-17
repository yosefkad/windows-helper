"""
Real end-to-end check of every tool the voice assistant can call.

    py -3.12 tests/tool_check.py

Unlike smoke_test.py (sandboxed, fake mail), this exercises the REAL things:
your actual Desktop/Documents/Downloads, calendar, Outlook/Gmail mailbox,
weather service, memory and routines — through the same /api/actions path the
voice agent uses. Everything it creates is named "bena_selftest…" and is
removed again (deleted files go to the Recycle Bin). Nothing pops up on screen:
the timer alarm and the "open Gmail" fallback are checked with the popup
replaced by a recorder. A real email is sent only if Gmail is connected
directly (app password in .env), and only to your own address.

Prints a table: which tools are fully working, and what failed for the rest.
"""
import datetime
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
STAMP = datetime.datetime.now().strftime("%m%d%H%M%S")
BASE_NAME = f"bena_selftest_{STAMP}"

results = {}          # tool -> list of (ok, label, detail)


def rec(tool, ok, label, detail=""):
    results.setdefault(tool, []).append((bool(ok), label, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {tool:16} {label}" + ("" if ok else f"   {str(detail)[:300]}"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def act(tool, confirmed=None, **args):
    body = {"tool": tool, "args": args}
    if confirmed is not None:
        body["confirmed"] = confirmed
    return req("POST", "/api/actions", body)


def start_server():
    code = (f"import sys; sys.path.insert(0, r'{ROOT}'); import app; "
            f"app.app.run(host='127.0.0.1', port={PORT}, use_reloader=False, threaded=True)")
    p = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + "/api/voice-status", timeout=1)
            return p
        except Exception:
            time.sleep(0.5)
    p.kill()
    sys.exit("server did not start")


# ---------------------------------------------------------------------------

def check_files(roots):
    print("\n[files]")
    for loc, path in roots.items():
        base = BASE_NAME
        _, r = act("create_folder", name=f"{base}/a/b", location=loc)
        rec("create_folder", r.get("ok") and os.path.isdir(os.path.join(path, base, "a", "b")),
            f"nested folder on {loc}", r)
        _, r = act("write_note", name=f"{base}/a/note", content="שלום — bena self-test", location=loc)
        note = os.path.join(path, base, "a", "note.txt")
        ok = r.get("ok") and os.path.isfile(note) and "שלום" in open(note, encoding="utf-8").read()
        rec("write_note", ok, f"Hebrew note on {loc}", r)
        _, r = act("list_folder", location=loc, name=f"{base}/a")
        names = [i.get("name") for i in r.get("files", []) + r.get("folders", [])]
        rec("list_folder", r.get("ok") and "note.txt" in names and "b" in names, f"lists {loc}", r)
        _, r = act("file_info", name=f"{base}/a/note.txt", location=loc)
        real = datetime.datetime.fromtimestamp(os.stat(note).st_mtime).strftime("%Y-%m-%d %H:%M") \
            if os.path.exists(note) else None
        shown = str(r.get("modified", ""))
        rec("file_info", r.get("ok") and real and shown.startswith(real), f"real date on {loc}",
            (shown, real))

        if loc == "workspace":
            code, r = act("rename_item", name=f"{base}/a/note.txt", new_name="renamed.txt", location=loc)
            rec("rename_item", code == 403, "refused without your yes", (code, r))
            _, r = act("rename_item", True, name=f"{base}/a/note.txt", new_name="renamed.txt", location=loc)
            rec("rename_item", r.get("ok") and os.path.isfile(os.path.join(path, base, "a", "renamed.txt")),
                "renames after yes", r)
            _, r = act("move_item", True, name=f"{base}/a/renamed.txt", location=loc,
                       to_path=f"{base}/moved/deep")
            rec("move_item", r.get("ok") and os.path.isfile(
                os.path.join(path, base, "moved", "deep", "renamed.txt")),
                "file into a new nested folder", r)
            act("create_folder", name=f"{base}/src", location=loc)
            _, r = act("move_item", True, name=f"{base}/src", location=loc, to_path=f"{base}/moved")
            rec("move_item", r.get("ok") and os.path.isdir(os.path.join(path, base, "moved", "src")),
                "folder into another folder", r)

        code, r = act("delete_item", name=base, location=loc)
        rec("delete_item", code == 403, f"refused without your yes ({loc})", (code, r))
        _, r = act("delete_item", True, name=base, location=loc)
        rec("delete_item", r.get("ok") and not os.path.exists(os.path.join(path, base)),
            f"to Recycle Bin ({loc})", r)


def check_timers(A):
    print("\n[timers]")
    _, r = act("set_timer", seconds=3600, label="bena self-test")
    tid = r.get("id")
    rec("set_timer", r.get("ok") and tid, "sets a 1-hour timer", r)
    _, r = act("list_timers")
    rec("list_timers", r.get("ok") and any(t["id"] == tid for t in r.get("timers", [])), "lists it", r)
    _, r = act("cancel_timer", id=tid)
    _, l = act("list_timers")
    rec("cancel_timer", r.get("ok") and not any(t["id"] == tid for t in l.get("timers", [])),
        "removes it from the list", r)

    # The alarm itself, with the on-screen popup replaced by a recorder.
    fired = []
    real = A._notify_windows
    A._notify_windows = lambda title, body: fired.append(title)
    try:
        r = A._act_set_timer({"seconds": 1, "label": "fire-test"})
        time.sleep(2.2)
        rec("set_timer", any("fire-test" in f for f in fired), "alarm actually goes off", fired)
        r = A._act_set_timer({"seconds": 1.5, "label": "cancel-test"})
        A._act_cancel_timer({"id": r["id"]})
        time.sleep(2.5)
        rec("cancel_timer", not any("cancel-test" in f for f in fired),
            "a cancelled alarm stays silent", fired)
    finally:
        A._notify_windows = real


def check_calendar():
    print("\n[calendar]")
    day = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    title = f"bena self-test {STAMP}"
    _, r = act("add_event", title=title, when=f"{day} 10:00", notes="automatic check")
    eid = r.get("event", {}).get("id") or r.get("id")
    rec("add_event", r.get("ok"), "adds an event tomorrow 10:00", r)
    _, r = act("add_event", title="x", when="next blursday")
    rec("add_event", not r.get("ok") and "YYYY" in r.get("error", ""), "explains a bad date", r)
    _, r = act("list_events", date=day)
    found = [e for e in r.get("events", []) if e.get("title") == title]
    rec("list_events", r.get("ok") and found and found[0]["when"] == f"{day} 10:00", "finds it", r)
    eid = eid or (found[0]["id"] if found else None)
    _, r = act("delete_event", id=eid) if eid else act("delete_event", title=title)
    _, l = act("list_events", date=day)
    rec("delete_event", r.get("ok") and not any(e.get("title") == title for e in l.get("events", [])),
        "deletes it", r)


def check_weather_location():
    print("\n[weather + location]")
    _, r = act("get_weather")
    rec("get_weather", r.get("ok") and r.get("temperature_c") is not None and r.get("forecast"),
        f"your location: {r.get('place')} {r.get('temperature_c')}°C", r)
    _, r = act("get_weather", location="Haifa")
    rec("get_weather", r.get("ok") and "haifa" in str(r.get("place", "")).lower(), "named city (Haifa)", r)
    _, r = act("where_am_i")
    rec("where_am_i", r.get("ok") and r.get("city") and r.get("latitude") is not None,
        f"{r.get('city')}, {r.get('country')}", r)


def check_email(A):
    print("\n[email]")
    import mail_gmail
    direct = mail_gmail.configured()
    _, r = act("email_status")
    rec("email_status", r.get("ok"), f"via {r.get('provider')}: {r.get('inbox_total')} in inbox", r)
    _, r = act("list_emails", count=5)
    emails = r.get("emails", [])
    rec("list_emails", r.get("ok") and emails, f"{len(emails)} newest via {r.get('provider')}", r)
    _, r = act("search_emails", query="LinkedIn", count=3)
    rec("search_emails", r.get("ok") and "emails" in r, f"'LinkedIn': {len(r.get('emails', []))} hits", r)
    already_read = next((e for e in emails if not e.get("unread")), None)
    if already_read:   # only an already-read one, so your unread state doesn't change
        _, r = act("read_email", id=already_read["id"])
        rec("read_email", r.get("ok") and (r.get("body") or r.get("subject")),
            f"opens \"{already_read['subject'][:40]}\"", r)
    else:
        rec("read_email", False, "no already-read email in the newest 5 to test safely")

    code, r = act("send_email", to=mail_gmail.address() or "a@b.com", subject="x", body="y")
    rec("send_email", code == 403, "refused without your yes", (code, r))
    if direct:
        _, r = act("send_email", True, to=mail_gmail.address(),
                   subject=f"bena self-test {STAMP} (safe to delete)",
                   body="This email was sent automatically by bena's tool check.")
        rec("send_email", r.get("ok") and r.get("sent"), "really sent through Gmail to yourself", r)
    else:
        import mail_outlook
        opened = []
        real = mail_outlook._open_url
        mail_outlook._open_url = opened.append
        try:
            r = A._act_send_email({"to": "test@example.com", "subject": "t", "body": "b"})
        finally:
            mail_outlook._open_url = real
        how = "sent automatically" if r.get("sent") else (
            "opens the finished email in Gmail (you press Send)" if r.get("opened_compose") else "fails")
        rec("send_email", r.get("ok") or r.get("opened_compose"), how, r)
    return direct


def check_brain(A):
    print("\n[memory, search, skills]")
    note = f"bena self-test note {STAMP}"
    _, r = act("memory", action="add", target="memory", content=note)
    _, ctx = req("GET", "/api/brain/context")
    rec("memory", r.get("ok") and note in ctx.get("memory", ""), "saves a note she will see next call", r)
    _, r = act("memory", action="remove", target="memory", old_text=note)
    _, ctx = req("GET", "/api/brain/context")
    rec("memory", r.get("ok") and note not in ctx.get("memory", ""), "removes it again", r)
    _, r = act("memory", action="add", target="user", content="password: hunter22")
    rec("memory", not r.get("ok"), "refuses to store a password", r)

    token = f"zebraselftest{STAMP}"
    hid = f"selftest-{STAMP}"
    req("POST", "/api/history", {"id": hid, "mode": "voice", "at": int(time.time() * 1000),
                                 "turns": [{"role": "user", "text": f"remember the {token} plan"}]})
    _, r = act("session_search", query=token)
    rec("session_search", r.get("ok") and r.get("matches"), "finds a saved conversation", r)
    req("DELETE", f"/api/history/{hid}")
    _, r = act("session_search", query=token)
    rec("session_search", not r.get("matches"), "deleted conversation is gone from search", r)

    _, r = act("skill_view", name="morning-briefing")
    rec("skill_view", r.get("ok") and "get_weather" in r.get("steps", ""), "reads morning-briefing", r)
    sk = f"bena-selftest-{STAMP}"
    _, r = act("skill_manage", action="create", name=sk, description="self test",
               content="1. list_timers")
    _, r2 = act("skill_manage", action="patch", name=sk, old_text="list_timers", new_text="get_weather")
    _, v = act("skill_view", name=sk)
    rec("skill_manage", r.get("ok") and r2.get("ok") and "get_weather" in v.get("steps", ""),
        "creates and edits a skill", (r, r2))
    _, r = act("skill_manage", action="delete", name=sk)
    _, v = act("skill_view", name=sk)
    rec("skill_manage", r.get("ok") and not v.get("ok"), "deletes it", r)


def check_routines(A):
    print("\n[routines]")
    name = f"bena self-test {STAMP}"
    code, r = act("schedule_routine", name=name, schedule="in 30m", prompt="x")
    rec("schedule_routine", code == 403, "refused without your yes", (code, r))
    _, r = act("schedule_routine", True, name=name, schedule="every day at 07:30",
               prompt="Tell me today's weather in one short sentence.")
    rec("schedule_routine", r.get("ok") and r.get("when") == "every day at 07:30", r.get("message"), r)
    _, r = act("list_routines")
    rec("list_routines", r.get("ok") and any(j["name"] == name for j in r.get("routines", [])),
        "lists it", r)
    # Run it now with the real model and tools, without the on-screen popup.
    status, text, used = A._routine_runner({"id": "selftest", "name": name, "skill": None,
                                            "prompt": "Tell me today's weather in one short sentence."})
    rec("schedule_routine", status == "ok" and "get_weather" in used and text,
        f"runs: \"{(text or '')[:70]}\"", (status, text, used))
    _, r = act("cancel_routine", name=name)
    _, l = act("list_routines")
    rec("cancel_routine", r.get("ok") and not any(j["name"] == name for j in l.get("routines", [])),
        "cancels it", r)


def check_client_tools():
    print("\n[voice-only tools]")
    js = r"""
const fs=require('fs'),vm=require('vm');
const sb={window:{},document:{},navigator:{},console,WebSocket:function(){},fetch:async()=>({json:async()=>({ok:true})})};
sb.WebSocket.OPEN=1; vm.createContext(sb);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length-1],'utf8'),sb);
(async()=>{
  const seen={};
  const s=new sb.window.LiveSession({onEmotion:(a)=>seen.emotion=a.emotion,onEndRequested:(r)=>seen.end=r,
    onConfirmPending(){},onConfirmResolved(){},onAction(){},onConfirm(){}});
  const a=await s._runTool('report_emotion',{emotion:'happy',confidence:0.9,energy:'high',evidence:'laughing'});
  const b=await s._runTool('end_conversation',{reason:'user said goodbye'});
  console.log(JSON.stringify({a,b,seen}));
})();
"""
    out = subprocess.run(["node", "-e", js, os.path.join(ROOT, "static", "live.js")],
                         capture_output=True, text=True, timeout=30)
    try:
        r = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        rec("report_emotion", False, "node harness failed", out.stderr[-200:])
        return
    rec("report_emotion", r["a"].get("ok") and r["seen"].get("emotion") == "happy", "updates the emotion panel")
    rec("end_conversation", r["b"].get("ended") and r["seen"].get("end") == "user said goodbye",
        "hangs up and returns to standby")


def main():
    server = start_server()
    import app as A
    try:
        _, roots = req("GET", "/api/action-roots")
        _, tools = req("GET", "/api/tools")
        off = tools.get("disabled", [])
        if off:
            print("Switched off in the Tools tab (they will fail):", ", ".join(off))
        check_files(roots["roots"])
        check_timers(A)
        check_calendar()
        check_weather_location()
        direct = check_email(A)
        check_brain(A)
        check_routines(A)
        check_client_tools()
    finally:
        server.kill()

    print("\n" + "=" * 72)
    good = [t for t, rs in results.items() if all(ok for ok, _, _ in rs)]
    bad = {t: [lbl for ok, lbl, _ in rs if not ok] for t, rs in results.items()
           if not all(ok for ok, _, _ in rs)}
    print(f"FULLY WORKING ({len(good)}/{len(results)}):")
    for t in sorted(good):
        print(f"  ✅ {t:18} " + "; ".join(lbl for _, lbl, _ in results[t]))
    if bad:
        print("\nNOT FULLY WORKING:")
        for t, fails in sorted(bad.items()):
            print(f"  ❌ {t:18} failed: " + "; ".join(fails))
    print(f"\nEmail route: {'Gmail direct' if direct else 'Outlook (reading) + Gmail window (sending)'}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
