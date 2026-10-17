"""
Smoke test for the parts that must never silently break.

    py -3.12 tests/smoke_test.py

Starts the Flask app on a free port, then checks:
  * the file sandbox refuses path-traversal / absolute / reserved names
  * destructive actions need confirmation, and move really moves (nested)
  * tool switches are enforced server-side (even with confirmed=True)
  * the Health page endpoints and integration switches agree with Tools
  * the voice client's confirmation key tolerates the model re-ordering args
  * every getElementById the frontend uses exists in index.html
  * Outlook: a stuck app times out, and a never-synced account is never
    reported as an empty inbox or a successful send
  * memory / conversation search / skills / routines: limits, secret
    refusal, Hebrew search, confirmation for routines, switches respected

Nothing touches your real Desktop/Documents: file tests use the project's
own workspace/ folder, settings files are backed up and restored, and the
server's memory/skills/routines live in a throwaway temp folder.
No network calls are made except the Health checks the app itself runs.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "workspace")
SETTINGS = [os.path.join(ROOT, "tool_settings.json")]
DATA = tempfile.mkdtemp(prefix="bena_smoke_")

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None, timeout=40):
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
    # Point the brain and the scheduler at DATA before app imports them.
    code = (f"import sys, os; sys.path.insert(0, r'{ROOT}'); import brain, routines; D = r'{DATA}'; "
            f"brain.MEM_DIR = os.path.join(D, 'memories'); brain.SKILLS_DIR = os.path.join(D, 'skills'); "
            f"brain.DB_PATH = os.path.join(D, 'brain.db'); "
            f"routines.JOBS_PATH = os.path.join(D, 'routines.json'); "
            f"routines.FEED_PATH = os.path.join(D, 'feed.json'); "
            f"routines.LOCK_PATH = os.path.join(D, 'routines.lock'); import app; "
            f"app.app.run(host='127.0.0.1', port={PORT}, use_reloader=False, threaded=True)")
    p = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/voice-status", timeout=1)
            return p
        except Exception:
            time.sleep(0.5)
    p.kill()
    sys.exit("server did not start")


# ---------------------------------------------------------------------------

def test_sandbox():
    print("\n[file sandbox]")
    for label, name in [("parent traversal", "../../evil"), ("mid-path traversal", "ok/../../../evil"),
                        ("backslash traversal", r"ok\..\..\evil"), ("absolute drive", "C:/Windows/evil"),
                        ("UNC share", "//server/share/evil"), ("reserved name", "ok/CON"),
                        ("illegal char", "bad:name"), ("too deep", "/".join(f"d{i}" for i in range(12)))]:
        _, r = act("create_folder", name=name, location="workspace")
        check(f"refuses {label}", not r.get("ok"), r)
    _, r = act("create_folder", name="st_Projects/2026/Q1", location="workspace")
    check("nested create works", r.get("ok") and os.path.isdir(os.path.join(WS, "st_Projects", "2026", "Q1")), r)
    check("every new folder gets an empty notes.txt",
          os.path.isfile(os.path.join(WS, "st_Projects", "2026", "Q1", "notes.txt")), r)
    _, r = act("write_note", name="st_Projects/brand_new/inside", content="x", location="workspace")
    check("a note can go into a folder that didn't exist yet",
          r.get("ok") and os.path.isfile(os.path.join(WS, "st_Projects", "brand_new", "inside.txt")), r)


def test_destructive_and_move():
    print("\n[destructive actions + move]")
    act("create_folder", name="st_Archive", location="workspace")
    act("write_note", name="st_moveme", content="hi", location="workspace")
    code, r = act("move_item", name="st_moveme.txt", location="workspace", to_path="st_Archive")
    check("move without confirmation -> 403", code == 403, (code, r))
    code, r = act("move_item", confirmed=True, name="st_moveme.txt", location="workspace",
                  to_path="st_Archive/deep")
    check("confirmed move into a NEW nested folder", r.get("ok") and
          os.path.isfile(os.path.join(WS, "st_Archive", "deep", "st_moveme.txt")), r)
    _, r = act("move_item", confirmed=True, name="st_Archive", location="workspace", to_path="st_Archive/deep")
    check("refuses moving a folder inside itself", not r.get("ok"), r)
    code, r = act("send_email", to="a@b.com", subject="x", body="y")
    check("send_email without confirmation -> 403", code == 403, (code, r))


def test_tool_switches():
    print("\n[tool switches]")
    req("POST", "/api/tools", {"tool": "get_weather", "enabled": False})
    code, r = act("get_weather")
    check("switched-off tool refused (403)", code == 403 and r.get("disabled"), (code, r))
    req("POST", "/api/tools", {"group": "files_delete", "enabled": False})
    code, _ = act("delete_item", confirmed=True, name="x", location="workspace")
    check("switched-off group refused even with confirmed=True", code == 403)
    _, t = req("GET", "/api/tools")
    check("state reported back", "get_weather" in t["disabled"] and "delete_item" in t["disabled"])
    sys.path.insert(0, ROOT)
    import app as A  # noqa: E402
    check("every tool has a switch and every switch has a tool", set(A.ALL_TOOLS) == set(A.ACTIONS),
          set(A.ALL_TOOLS) ^ set(A.ACTIONS))


def test_health():
    print("\n[health page]")
    t0 = time.time()
    code, h = req("GET", "/api/health", timeout=60)
    dt = time.time() - t0
    check("health endpoint answers", code == 200 and h.get("integrations"), code)
    ids = {i["id"] for i in h.get("integrations", [])}
    check("all 12 integrations reported", ids == {"gemini", "openai", "wake", "folders", "email",
                                                  "calendar", "timers", "weather", "location",
                                                  "memory", "skills", "routines"}, ids)
    slowest = max((i.get("latency_ms") or 0) for i in h["integrations"])
    check(f"checks run in parallel ({dt*1000:.0f}ms total, slowest {slowest}ms)",
          dt * 1000 < slowest * 2 + 1500)
    off = [i for i in h["integrations"] if i["id"] == "weather"][0]
    check("switched-off weather shows as off and is not probed",
          off["status"] == "off" and off["latency_ms"] is None, off)

    req("POST", "/api/integrations", {"id": "email", "enabled": False})
    _, t = req("GET", "/api/tools")
    check("health switch reaches the Tools state",
          all(not g["enabled"] for g in t["groups"] if g["id"] in ("email_read", "email_send")))
    code, _ = act("list_emails")
    check("…and is enforced", code == 403)
    code, _ = req("POST", "/api/integrations", {"id": "gemini", "enabled": False})
    check("core services have no switch", code == 400)


def test_confirm_key():
    print("\n[voice confirmation key]")
    js = r"""
const fs=require('fs'),vm=require('vm');
const sb={window:{},document:{},navigator:{},console,WebSocket:function(){},
          fetch:async()=>({json:async()=>({ok:true})})};
sb.WebSocket.OPEN=1; vm.createContext(sb);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length-1],'utf8'),sb);
(async()=>{
  const mk=()=>new sb.window.LiveSession({onConfirmPending(){},onConfirmResolved(){},onAction(){},onConfirm(){}});
  let s=mk(); await s._runTool('move_item',{name:'a.txt',to_location:'desktop',to_path:'A',location:'desktop'});
  const same=await s._runTool('move_item',{to_path:'A',name:'a.txt',location:'desktop'});
  s=mk(); await s._runTool('move_item',{name:'a.txt',location:'desktop',to_path:'A'});
  const diff=await s._runTool('move_item',{name:'a.txt',location:'desktop',to_path:'SECRET'});
  const setup=sb.window.buildSetupMessage('en',{enabled:['list_folder'],groups:[]});
  const names=setup.setup.tools[0].functionDeclarations.map(d=>d.name);
  console.log(JSON.stringify({same:!same.needs_confirmation, diff:!!diff.needs_confirmation, names}));
})();
"""
    out = subprocess.run(["node", "-e", js, os.path.join(ROOT, "static", "live.js")],
                         capture_output=True, text=True, timeout=30)
    try:
        r = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        check("node harness ran", False, out.stderr[-300:])
        return
    check("reordered args = same request (executes on the yes)", r["same"])
    check("changed destination = new request (asks again)", r["diff"])
    check("only enabled tools declared to Gemini",
          set(r["names"]) == {"list_folder", "report_emotion", "end_conversation"}, r["names"])


def test_dom():
    print("\n[frontend wiring]")
    static = os.path.join(ROOT, "static")
    html = open(os.path.join(static, "index.html"), encoding="utf8").read()
    ids = set(re.findall(r'id="([^"]+)"', html))
    js = ""
    for f in os.listdir(static):
        if f.endswith(".js") and f not in ("desktop.js", "pcm-worklet.js"):
            js += open(os.path.join(static, f), encoding="utf8").read()
    wanted = set(re.findall(r"getElementById\(['\"]([A-Za-z0-9_-]+)['\"]\)", js))
    missing = sorted(wanted - ids)
    check("every getElementById target exists in index.html", not missing, missing)
    for s in re.findall(r'<script src="([^"]+)"', html):
        check(f"script {s} exists", os.path.exists(os.path.join(static, s)))
    css = open(os.path.join(static, "live.css"), encoding="utf8").read()
    check("every pane has an explicit .hidden rule",
          all(f"#{p}Mode.hidden" in css for p in ("text", "voice", "tools", "brain", "health")))


def test_brain():
    print("\n[memory, conversation search, skills, routines]")
    code, r = act("memory", action="add", target="user", content="Prefers short answers in Hebrew")
    check("memory: add a fact about the user", code == 200 and r.get("ok"), r)
    _, r = act("memory", action="add", target="user", content="Prefers short answers in Hebrew")
    _, b = req("GET", "/api/brain")
    check("memory: exact duplicate is not stored twice", r.get("ok") and
          b["memory"]["user"]["entries"].count("Prefers short answers in Hebrew") == 1, r)
    _, r = act("memory", action="add", target="memory", content="my key is sk-abc123def456ghi789jkl")
    check("memory: refuses anything that looks like a key", not r.get("ok"), r)
    _, r = act("memory", action="add", target="memory", content="x" * 2300)
    check("memory: refuses to go past the character limit", not r.get("ok") and r.get("full"), r)
    _, ctx = req("GET", "/api/brain/context")
    check("memory is in the next session's starting context",
          "Prefers short answers" in ctx.get("memory", "") and "morning-briefing" in ctx.get("skills", ""), ctx)

    code, r = req("POST", "/api/sessions", {
        "id": "st-1", "language": "he", "summary": "planning a trip",
        "started_at": "2026-09-01T10:00:00", "ended_at": "2026-09-01T10:05:00",
        "turns": [{"role": "user", "text": "אני רוצה לטוס לאילת בחופש"},
                  {"role": "model", "text": "Sounds good, when?"}]})
    check("conversation saved for search", code == 200 and r.get("ok"), r)
    _, r = act("session_search", query="אילת")
    check("search finds a Hebrew word inside a prefixed form (לאילת)",
          r.get("ok") and any("לאילת" in m["text"] for m in r.get("matches", [])), r)
    _, r = req("GET", "/api/brain/search?q=trip")
    check("Brain tab search endpoint answers", r.get("ok") is not None, r)

    _, r = act("skill_manage", action="create", name="Pack List",
               description="Make a packing list note", content="1. write_note with the list")
    check("skill: create", r.get("ok"), r)
    _, r = act("skill_view", name="pack-list")
    check("skill: view by slug", r.get("ok") and "write_note" in r.get("steps", ""), r)

    code, r = act("schedule_routine", name="st weather", schedule="every day at 07:30",
                  prompt="Tell me the weather")
    check("routine: refused without the user's yes", code == 403, (code, r))
    code, r = act("schedule_routine", True, name="st weather", schedule="every day at 07:30",
                  prompt="Tell me the weather")
    check("routine: created after the yes", code == 200 and r.get("ok"), r)
    _, r = act("schedule_routine", True, name="st bad", schedule="whenever you feel like it",
               prompt="x")
    check("routine: nonsense schedule is refused with a reason", not r.get("ok") and r.get("error"), r)
    _, b = req("GET", "/api/brain")
    job = next((j for j in b.get("routines", []) if j["name"] == "st weather"), None)
    check("Brain tab sees the routine with a readable schedule",
          job and job["when"] == "every day at 07:30" and job["next_run"], job)
    _, r = req("POST", "/api/routines", {"action": "disable", "name": "st weather"})
    _, b = req("GET", "/api/brain")
    check("routine: pause from the Brain tab",
          not next(j for j in b["routines"] if j["name"] == "st weather")["enabled"])
    _, r = act("cancel_routine", name="st weather")
    check("routine: cancel by name", r.get("ok"), r)
    check("scheduler owns the lock in this app", b["scheduler"]["active"], b["scheduler"])

    rec = {"id": "st-h1", "mode": "voice", "at": 1790000000000, "language": "en", "durationMs": 5000,
           "turns": [{"role": "user", "text": "my passport expires in March"}], "emotions": []}
    code, r = req("POST", "/api/history", rec)
    _, h = req("GET", "/api/history")
    _, f = act("session_search", query="passport")
    check("history is saved in the database and indexed for search",
          code == 200 and any(e["id"] == "st-h1" for e in h["entries"])
          and any("passport" in m["text"] for m in f.get("matches", [])), (r, f))
    req("DELETE", "/api/history/st-h1")
    _, f = act("session_search", query="passport")
    check("deleting it from history removes it from search too", not f.get("matches"), f)
    code, _ = req("POST", "/api/history", {"id": "../bad", "mode": "voice", "at": 1})
    check("malformed history record refused", code == 400)

    req("POST", "/api/tools", {"group": "memory", "enabled": False})
    _, ctx = req("GET", "/api/brain/context")
    code, _ = act("memory", action="add", target="user", content="should not be saved")
    _, r = req("POST", "/api/sessions", {"id": "st-2", "turns": [{"role": "user", "text": "hello"}]})
    req("POST", "/api/history", dict(rec, id="st-h2", turns=[{"role": "user", "text": "zebra crossing"}]))
    _, h = req("GET", "/api/history")
    req("POST", "/api/tools", {"group": "memory", "enabled": True})
    _, f = act("session_search", query="zebra")
    check("memory switched off: not injected, not writable, conversations not searchable",
          ctx.get("memory") == "" and code == 403 and r.get("skipped") and not f.get("matches"),
          (ctx, code, r, f))
    check("…but the history sidebar still keeps the conversation",
          any(e["id"] == "st-h2" for e in h["entries"]))
    req("DELETE", "/api/history/st-h2")

    import app as A
    check("routines can never call destructive tools",
          not ({d["name"] for d in A.ROUTINE_DECLARATIONS} & A.DESTRUCTIVE))


def test_mail():
    print("\n[Outlook email]")
    sys.path.insert(0, ROOT)
    import mail_outlook as o

    gmail_bad = [{"address": "me@gmail.com", "server": "imap.gmail.com", "user": "me",
                  "kind": "imap", "profile": "Outlook", "smtp_server": "smtp.gmail.com"}]
    check("a bare Gmail login name is not flagged (Gmail accepts it; the old rule was wrong)",
          o.config_problems(gmail_bad) == [])

    # Fake Outlook object model: a Gmail IMAP store with only local folders.
    class F:
        def __init__(self, name, n=0): self.Name, self._n = name, n
        @property
        def Items(self):
            class I:
                Count = self._n
                def Sort(self, *a): pass
                def Restrict(self, *a): return self
                def __iter__(self): return iter([])
            return I()
        UnReadItemCount = 0
    class Store:
        ExchangeStoreType = 3
        def __init__(self, names): self._names = names
        def GetRootFolder(self):
            names = self._names
            class R:
                Folders = [F(n) for n in names]
            return R()
    class App:
        def __init__(self, caption): self._caption = caption
        def ActiveExplorer(self):
            cap = self._caption
            class E:
                Caption = cap
            return E()
        def CreateItem(self, _):
            # What unactivated Office really does: "The operation failed."
            raise Exception("(-2147352567, 'Exception occurred.', (4096, 'Microsoft Outlook', "
                            "'הפעולה נכשלה.', None, 0, -2147467259), None)")
    class NS:
        def __init__(self, names, caption="Inbox - me@gmail.com - Outlook"):
            self._st = Store(names)
            self.Application = App(caption)
        def GetDefaultFolder(self, _):
            f = F("Inbox"); f.Store = self._st; return f
        def SendAndReceive(self, *_): pass

    real_run, real_cfg, real_open = o._run, o.account_config, o._open_url
    opened = []
    o._open_url = opened.append        # never pop a real browser from a test
    try:
        o.account_config = lambda: gmail_bad
        o._run = lambda fn, *a: fn(NS(["Inbox", "Outbox", "Drafts"]), *a)
        r = o.list_emails()
        check("never-synced account is NOT reported as an empty inbox",
              not r["ok"] and r.get("not_synced") and "NOT an empty inbox" in r["error"], r)
        r = o.send_email("a@b.com", "hi", "x")
        check("never-synced account refuses to 'send' into a dead Outbox",
              not r["ok"] and r.get("not_synced"), r)
        o._run = lambda fn, *a: fn(NS(["Inbox", "[Gmail]", "Drafts"]), *a)
        r = o.list_emails()
        check("synced Gmail store ([Gmail] present) reads normally", r.get("ok"), r)

        synced = ["Inbox", "[Gmail]", "Drafts"]
        o._run = lambda fn, *a: fn(NS(synced, "תיבת דואר נכנס - me@gmail.com - Outlook (מוצר ללא רשיון)"), *a)
        r = o.send_email("a@b.com", "שלום & hi", "line 1\nline 2")
        check("unactivated Office: NOT reported as sent; the email is opened ready in Gmail instead",
              not r["ok"] and r.get("sent") is False and r.get("opened_compose")
              and r.get("where") == "Gmail in your browser" and "Nothing has been sent" in r["message"], r)
        url = opened[-1] if opened else ""
        check("…with recipient, subject and body filled in and safely encoded",
              url.startswith("https://mail.google.com/mail/u/me%40gmail.com/?view=cm")
              and "to=a%40b.com" in url and "%26%20hi" in url and "line%201%0Aline%202" in url, url)
        r = o.status()
        check("…and status reports it: reads OK, can_send false, licence fix listed",
              r["ok"] and r["unlicensed"] and r["can_send"] is False and r["problems"], r)
        o._run = lambda fn, *a: fn(NS(synced, "Inbox - me@gmail.com - Outlook (Unlicensed Product)"), *a)
        check("…English title bar too", o.send_email("a@b.com", "hi", "x").get("opened_compose"))
        o._run = lambda fn, *a: fn(NS(synced), *a)
        n = len(opened)
        r = o.send_email("a@b.com", "hi", "x")
        check("Outlook refusing to create a mail falls back too, never a raw COM error",
              r.get("opened_compose") and len(opened) == n + 1, r)
        other = [{"address": "me@work.com", "server": "imap.work.com", "user": "me",
                  "kind": "imap", "profile": "Outlook", "smtp_server": "smtp.work.com"}]
        url, where = o.compose_url(["a@b.com"], "hi there", "x", accounts=other)
        check("non-Gmail account falls back to the default mail app (mailto:)",
              where == "your mail app" and url == "mailto:a@b.com?subject=hi%20there&body=x", url)
    finally:
        o._run, o.account_config, o._open_url = real_run, real_cfg, real_open

    if o.AVAILABLE:
        old_t, old_d = o.TIMEOUT_S, o.win32com.client.Dispatch
        o.TIMEOUT_S = 2
        o._stuck["since"] = 0
        o.win32com.client.Dispatch = lambda *a, **k: time.sleep(30)
        t0 = time.time()
        r = o.status()
        check(f"stuck Outlook times out instead of hanging ({time.time()-t0:.1f}s)",
              not r["ok"] and time.time() - t0 < 4)
        o.TIMEOUT_S, o.win32com.client.Dispatch = old_t, old_d
        o._stuck["since"] = 0


# ---------------------------------------------------------------------------

def main():
    backups = {}
    for p in SETTINGS:
        if os.path.exists(p):
            backups[p] = open(p, encoding="utf8").read()
            os.remove(p)
    server = start_server()
    try:
        test_sandbox()
        test_destructive_and_move()
        test_tool_switches()
        test_health()
        test_confirm_key()
        test_dom()
        test_brain()
        test_mail()
    finally:
        server.kill()
        server.wait(timeout=10)
        shutil.rmtree(DATA, ignore_errors=True)
        for p in SETTINGS:
            if os.path.exists(p):
                os.remove(p)
        for p, text in backups.items():
            with open(p, "w", encoding="utf8") as f:
                f.write(text)
        for d in ("st_Projects", "st_Archive"):
            shutil.rmtree(os.path.join(WS, d), ignore_errors=True)
        for f in ("st_moveme.txt",):
            p = os.path.join(WS, f)
            if os.path.exists(p):
                os.remove(p)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
