"""
Gmail-direct module (mail_gmail.py) and the Gmail→Outlook routing in app.py.

    py -3.12 tests/test_gmail.py

Runs against a fake IMAP/SMTP server — no network, no real mailbox, nothing sent.
"""
import imaplib
import os
import smtplib
import socket
import sys
from email import message_from_bytes, policy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mail_gmail as G  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"   {detail}"))


GOOD = "abcdefghijklmnop"

MAILS = {
    b"101": (b"\\Seen", "26-Sep-2026 09:00:00 +0300",
             b"From: Dana Levi <dana@example.com>\r\n"
             b"Subject: =?utf-8?b?16nXnNeV150g15zXmdeV16HXow==?=\r\n"
             b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
             b"Plain body here.\r\n"),
    b"102": (b"\\Seen", "27-Sep-2026 12:30:00 +0300",
             b"From: shop@example.com\r\nSubject: Order shipped\r\n"
             b"Content-Type: text/html; charset=utf-8\r\n\r\n"
             b"<html><style>p{}</style><p>Your &amp; order</p><br><b>is on its way</b></html>\r\n"),
    b"103": (b"", "28-Sep-2026 18:45:00 +0300",
             b"From: LinkedIn <jobs@linkedin.com>\r\nSubject: New jobs\r\n"
             b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
             b"--XX\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
             b"--XX\r\nContent-Type: application/pdf\r\n"
             b"Content-Disposition: attachment; filename=\"cv.pdf\"\r\n\r\nPDF\r\n--XX--\r\n"),
}


class FakeIMAP:
    log = []

    def __init__(self, host, port, timeout=None):
        if FakeIMAP.down:
            raise socket.timeout("timed out")
        self.literal = None
        self.selected = None

    def login(self, user, pw):
        FakeIMAP.log.append(("login", user, pw))
        if pw != GOOD:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/&BdMF1QXQBeg- &BdkF1QXmBdA-"',
                      b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
                      b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"']

    def select(self, name, readonly=False):
        self.selected = (name, readonly)
        FakeIMAP.log.append(("select", name, readonly))
        return "OK", [b"3"]

    def status(self, name, what):
        return "OK", [b'"INBOX" (MESSAGES 3 UNSEEN 1)']

    def uid(self, cmd, *args):
        FakeIMAP.log.append(("uid", cmd) + args)
        if cmd == "SEARCH":
            if args[0] == "CHARSET":
                lit, self.literal = self.literal, None
                FakeIMAP.log.append(("literal", lit))
                return "OK", [b"102" if lit == "הזמנה".encode() else b""]
            return "OK", [b"103" if args[1] == "UNSEEN" else b"101 102 103"]
        if cmd == "FETCH":
            out = []
            for u in args[0].split(","):
                flags, date, raw = MAILS[u.encode()]
                full = "BODY.PEEK[]" in args[1]
                body = raw if full else raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                meta = (f'1 (UID {u} FLAGS ({flags.decode()}) INTERNALDATE "{date}" '
                        f'BODY[] {{{len(body)}}}').encode()
                out += [(meta, body), b")"]
            return "OK", out
        if cmd == "STORE":
            return "OK", [b""]
        return "NO", [b""]

    def logout(self):
        pass

    def shutdown(self):
        pass


FakeIMAP.down = False


class FakeSMTP:
    sent = []
    fail = None

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.fail == "down":
            raise ConnectionRefusedError("refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        if pw != GOOD:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)
        return {}


G.imaplib.IMAP4_SSL = FakeIMAP
G.smtplib.SMTP_SSL = FakeSMTP
creds = {"addr": "me@gmail.com", "pw": GOOD}
G._creds = lambda: (creds["addr"], creds["pw"].replace(" ", ""))

print("[gmail direct]")
creds["pw"] = ""
r = G.list_emails()
check("not set up: says so and gives the steps", r.get("not_configured") and r.get("steps"), r)
creds["pw"] = "wrong password"
r = G.status()
check("wrong app password: auth_failed with the fix", r.get("auth_failed") and r.get("steps"), r)
creds["pw"] = "abcd efgh ijkl mnop"
r = G.status()
check("app password with spaces works; inbox counts read",
      r["ok"] and r["inbox_total"] == 3 and r["inbox_unread"] == 1, r)

r = G.list_emails("inbox", 10)
ids = [e["id"] for e in r["emails"]]
check("newest first, ids carry folder + uid", ids == ["g:inbox:103", "g:inbox:102", "g:inbox:101"], ids)
e101 = r["emails"][2]
check("Hebrew subject decoded, sender split into name + address",
      e101["subject"] == "שלום ליוסף" and e101["from"] == "Dana Levi"
      and e101["from_address"] == "dana@example.com", e101)
check("unread and attachments flagged correctly",
      r["emails"][0]["unread"] and not e101["unread"] and r["emails"][0]["has_attachments"], r["emails"][0])
check("date is local, minute precision", len(e101["received"]) == 16, e101["received"])
check("inbox opened read-only for listing", ("select", "INBOX", True) in FakeIMAP.log)

r = G.list_emails("inbox", 10, unread_only=True)
check("unread only", [e["id"] for e in r["emails"]] == ["g:inbox:103"], r)
G.list_emails("sent", 5)
check("Sent found by its \\Sent flag even with a Hebrew folder name",
      any(x[0] == "select" and "&BdMF1QXQBeg-" in x[1] for x in FakeIMAP.log))
r = G.list_emails("outbox")
check("outbox: empty with a note (Gmail sends immediately)", r["ok"] and r["emails"] == [] and r["note"])

r = G.search_emails("הזמנה")
check("Hebrew search goes as a UTF-8 literal via X-GM-RAW",
      r["ok"] and [e["id"] for e in r["emails"]] == ["g:inbox:102"]
      and ("literal", "הזמנה".encode()) in FakeIMAP.log, r)

FakeIMAP.log.clear()
r = G.read_email("g:inbox:102")
check("HTML email read as clean text", r["ok"] and r["body"].startswith("Your & order")
      and "<" not in r["body"] and "is on its way" in r["body"], r.get("body"))
r = G.read_email("g:inbox:103")
check("attachment names listed", r["attachments"] == ["cv.pdf"], r.get("attachments"))
check("reading an unread email marks it read",
      any(x[:2] == ("uid", "STORE") and x[2] == "103" for x in FakeIMAP.log))
FakeIMAP.log.clear()
G.read_email("g:inbox:103", mark_read=False)
check("mark_read=False (routines): read-only, never marks it read",
      not any(x[:2] == ("uid", "STORE") for x in FakeIMAP.log)
      and ("select", "INBOX", True) in FakeIMAP.log)
check("Outlook-style ids are not Gmail ids", not G.is_gmail_id("00000000ABCDEF") and G.is_gmail_id("g:sent:5"))
check("bad id refused", not G.read_email("g:../../etc:1")["ok"])

print("\n[sending]")
r = G.send_email("a@b.com, c@d.org", "תודה רבה", "שורה 1\nשורה 2", cc="e@f.com")
msg = FakeSMTP.sent[-1] if FakeSMTP.sent else None
check("sends: really sent, says so", r.get("ok") and r.get("sent"), r)
check("headers right: From, To, Cc, Hebrew subject",
      msg and msg["From"] == "me@gmail.com" and msg["To"] == "a@b.com, c@d.org"
      and msg["Cc"] == "e@f.com" and str(msg["Subject"]) == "תודה רבה", msg and dict(msg))
parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
check("Hebrew body survives the round trip", parsed.get_content().strip() == "שורה 1\nשורה 2")
n = len(FakeSMTP.sent)
check("invalid address refused before connecting",
      not G.send_email("not-an-address", "x", "y")["ok"] and len(FakeSMTP.sent) == n)
check("missing subject refused", not G.send_email("a@b.com", " ", "y")["ok"])
creds["pw"] = "wrong"
check("SMTP rejects the password: auth_failed", G.send_email("a@b.com", "x", "y").get("auth_failed"))
creds["pw"] = GOOD
FakeSMTP.fail = "down"
check("Gmail unreachable: network (so app.py can fall back)",
      G.send_email("a@b.com", "x", "y").get("network"))
FakeSMTP.fail = None
FakeIMAP.down = True
check("IMAP timeout: network", G.list_emails().get("network"))
FakeIMAP.down = False

print("\n[routing in app.py]")
import app as A  # noqa: E402


class Fake:
    def __init__(self, conf, result):
        self._conf, self._result, self.calls = conf, result, []

    def configured(self):
        return self._conf

    def is_gmail_id(self, i):
        return str(i).startswith("g:")

    def __getattr__(self, op):
        def f(*a, **k):
            self.calls.append(op)
            return dict(self._result)
        return f


real_g, real_o = A._gmail, A._outlook
try:
    g = Fake(True, {"ok": True, "sent": True})
    o = Fake(True, {"ok": True})
    A._gmail, A._outlook = (lambda: g), (lambda: o)
    r = A._act_send_email({"to": "a@b.com", "subject": "x", "body": "y"})
    check("Gmail set up: Gmail sends, Outlook untouched",
          r["provider"] == "gmail" and g.calls == ["send_email"] and o.calls == [], r)

    g = Fake(True, {"ok": False, "network": True, "error": "Could not reach Gmail"})
    o = Fake(True, {"ok": True, "emails": []})
    A._gmail, A._outlook = (lambda: g), (lambda: o)
    r = A._act_list_emails({})
    check("Gmail unreachable: Outlook answers, and the Gmail problem is reported",
          r["provider"] == "outlook" and r.get("gmail_problem") == "Could not reach Gmail", r)

    g = Fake(True, {"ok": False, "error": "Not a valid email address: x"})
    o = Fake(True, {"ok": True})
    A._gmail, A._outlook = (lambda: g), (lambda: o)
    r = A._act_send_email({"to": "x", "subject": "s"})
    check("a real answer from Gmail (bad address) is NOT retried through Outlook",
          r["provider"] == "gmail" and o.calls == [], r)

    g = Fake(False, {})
    o = Fake(True, {"ok": True, "emails": []})
    A._gmail, A._outlook = (lambda: g), (lambda: o)
    r = A._act_email_status({})
    check("Gmail not set up: Outlook, as before", r["provider"] == "outlook" and g.calls == [], r)

    g = Fake(True, {"ok": True})
    o = Fake(True, {"ok": True})
    A._gmail, A._outlook = (lambda: g), (lambda: o)
    A._act_read_email({"id": "g:inbox:5"})
    A._act_read_email({"id": "00000000ABCDEF"})
    check("read_email goes to whichever provider made the id",
          g.calls == ["read_email"] and o.calls == ["read_email"], (g.calls, o.calls))
finally:
    A._gmail, A._outlook = real_g, real_o

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
