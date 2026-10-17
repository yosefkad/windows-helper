"""
bena's long-term brain — ideas borrowed from Nous Research's Hermes Agent.

Three parts, each deliberately small:

1. MEMORY  (brain.db, table memory)
   Two size-capped notes she curates herself with one tool (add / replace /
   remove). "user" is who you are and what you like; "memory" is her own
   notes — facts about this computer, lessons learned. Both are injected into
   the system prompt as a FROZEN snapshot when a session starts (which is all
   the Gemini Live API allows anyway — its instructions are one-shot). Edits
   land on disk immediately and appear from the next session. The caps force
   her to consolidate rather than hoard; hitting one returns the current
   entries so she can merge or drop something first.

2. CONVERSATION SEARCH  (brain.db, SQLite FTS5)
   Every voice conversation is stored, and session_search finds past messages
   by content — no model call, milliseconds. The index uses the trigram
   tokenizer because Hebrew glues prefixes onto words (ל/ב/ה/ו…): a normal
   word index would never find "רופא" inside "לרופא"; trigrams match
   substrings, so it does.

3. SKILLS  (skills/<name>/SKILL.md)
   Procedural memory: a how-to she writes after working out a multi-step job
   ("morning briefing = weather + today's events + unread count"). Only a
   one-line index sits in the prompt; the full steps are loaded on demand.

4. HISTORY  (brain.db, table history)
   The sidebar's records — text analyses and voice conversations — kept here
   rather than in the browser, where the desktop app's profile could vanish.
   Voice records are also indexed for search as they are saved.

Everything personal is in one SQLite file, brain.db; skills are plain files.
All of it is readable and editable from the Brain tab. Nothing in here can run a tool — skills and memory
are text; every action still goes through /api/actions and its gates.
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
MEM_DIR = os.path.join(ROOT, "memories")
SKILLS_DIR = os.path.join(ROOT, "skills")
DB_PATH = os.path.join(ROOT, "brain.db")

# Caps from Hermes: ~800 and ~500 tokens. Small on purpose.
MEM_TARGETS = {
    "user":   {"file": "USER.md",   "limit": 1375, "title": "About the user"},
    "memory": {"file": "MEMORY.md", "limit": 2200, "title": "Her own notes"},
}
SEP = "\n§\n"          # entry separator, same as Hermes

_lock = threading.Lock()

# Things that must never be written into a note that gets pasted into every
# future prompt and sits on disk in plain text.
_SECRET_PATTERNS = [
    r"\bsk-[A-Za-z0-9_-]{16,}",              # OpenAI-style keys
    r"\bAIza[0-9A-Za-z_-]{20,}",             # Google API keys
    r"\bAQ\.[A-Za-z0-9_-]{20,}",             # Google auth tokens
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",         # GitHub tokens
    r"(?i)\b(pass(word)?|pwd|סיסמ[אה])\s*[:=]\s*\S+",
    r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b",   # card numbers
]


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ===========================================================================
# 1. MEMORY
# ===========================================================================

def _mem_path(target):
    # Only used to import the notes kept as files before the database.
    return os.path.join(MEM_DIR, MEM_TARGETS[target]["file"])


def _read_entries(target):
    con = _db()
    try:
        rows = con.execute("SELECT text FROM memory WHERE target=? ORDER BY pos",
                           (target,)).fetchall()
    finally:
        con.close()
    return [r["text"] for r in rows]


def _write_entries(target, entries):
    con = _db()
    try:
        with con:
            con.execute("DELETE FROM memory WHERE target=?", (target,))
            con.executemany("INSERT INTO memory (target, pos, text) VALUES (?,?,?)",
                            [(target, i, e) for i, e in enumerate(entries)])
    finally:
        con.close()


def _import_memory_files(con):
    """One-time move of memories/*.md into the database. The files are kept,
    renamed to *.imported, so nothing is ever silently thrown away."""
    for t in MEM_TARGETS:
        path = _mem_path(t)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            entries = [e.strip() for e in f.read().split(SEP) if e.strip()]
        have = con.execute("SELECT COUNT(*) FROM memory WHERE target=?", (t,)).fetchone()[0]
        if entries and not have:
            with con:
                con.executemany("INSERT INTO memory (target, pos, text) VALUES (?,?,?)",
                                [(t, i, e) for i, e in enumerate(entries)])
        os.replace(path, path + ".imported")


def _usage(target, entries):
    used = len(SEP.join(entries))
    limit = MEM_TARGETS[target]["limit"]
    return {"used": used, "limit": limit, "percent": round(100 * used / limit)}


def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _looks_secret(text):
    return any(re.search(p, text) for p in _SECRET_PATTERNS)


def memory_state():
    """Both notes, for the Brain tab."""
    out = {}
    for t, meta in MEM_TARGETS.items():
        entries = _read_entries(t)
        out[t] = {"title": meta["title"], "entries": entries, **_usage(t, entries)}
    return out


def memory_snapshot():
    """The frozen block pasted into the system prompt at session start."""
    parts = []
    for t, meta in MEM_TARGETS.items():
        entries = _read_entries(t)
        if not entries:
            continue
        u = _usage(t, entries)
        parts.append(f"{meta['title'].upper()} ({u['percent']}% full):\n" +
                     "\n".join("- " + e for e in entries))
    return "\n\n".join(parts)


def memory_action(action, target="memory", content=None, old_text=None):
    """add / replace / remove — the single memory tool."""
    action = str(action or "").lower()
    target = str(target or "memory").lower()
    if target not in MEM_TARGETS:
        return {"ok": False, "error": "target must be 'user' or 'memory'"}
    content = _clean(content)
    old_text = _clean(old_text)

    if content and _looks_secret(content):
        return {"ok": False, "error": "That looks like a password, key or card number. "
                                      "Those are never stored in memory."}

    with _lock:
        entries = _read_entries(target)

        def find(needle):
            hits = [i for i, e in enumerate(entries) if needle.lower() in e.lower()]
            if not needle:
                return None, "old_text is required to find the entry."
            if not hits:
                return None, f"No entry contains \"{needle}\"."
            if len(hits) > 1:
                return None, (f"\"{needle}\" matches {len(hits)} entries — use a longer, "
                              f"unique piece of the one you mean.")
            return hits[0], None

        if action == "add":
            if not content:
                return {"ok": False, "error": "Nothing to add."}
            if any(content.lower() == e.lower() for e in entries):
                return {"ok": True, "note": "Already remembered.", **_usage(target, entries)}
            candidate = entries + [content]
        elif action == "replace":
            i, err = find(old_text)
            if err:
                return {"ok": False, "error": err, "entries": entries}
            if not content:
                return {"ok": False, "error": "content (the new text) is required."}
            candidate = entries[:i] + [content] + entries[i + 1:]
        elif action == "remove":
            i, err = find(old_text)
            if err:
                return {"ok": False, "error": err, "entries": entries}
            candidate = entries[:i] + entries[i + 1:]
        else:
            return {"ok": False, "error": "action must be add, replace or remove"}

        u = _usage(target, candidate)
        if u["used"] > u["limit"]:
            # Hermes' rule: show what is there so she can consolidate first.
            return {"ok": False, "full": True,
                    "error": (f"{MEM_TARGETS[target]['title']} is full "
                              f"({_usage(target, entries)['used']}/{u['limit']} chars). "
                              f"Merge or remove entries with replace/remove first."),
                    "entries": entries}
        _write_entries(target, candidate)
        res = {"ok": True, "action": action, "target": target, "entries": len(candidate), **u}
        if u["percent"] >= 80:
            res["note"] = "Over 80% full — consolidate soon."
        return res


# ===========================================================================
# 2. CONVERSATION SEARCH
# ===========================================================================

def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT,
            language TEXT, summary TEXT, dominant_emotion TEXT);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, text TEXT, seq INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, session_id UNINDEXED, role UNINDEXED,
            tokenize = 'trigram case_sensitive 0');
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL, pos INTEGER NOT NULL, text TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history (
            id TEXT PRIMARY KEY, mode TEXT NOT NULL, at INTEGER NOT NULL,
            updated_at TEXT, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS history_at ON history(at DESC);
    """)
    if _imported.get("path") != DB_PATH:
        _imported["path"] = DB_PATH
        _import_memory_files(con)
    return con


_imported = {}


def save_session(rec):
    """Store (or overwrite) one conversation. Idempotent per session id."""
    sid = str(rec.get("id") or "").strip()
    turns = [t for t in (rec.get("turns") or [])
             if isinstance(t, dict) and _clean(t.get("text"))]
    if not sid or not turns:
        return {"ok": False, "error": "Nothing to store."}
    with _lock:
        con = _db()
        try:
            with con:
                con.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                con.execute("DELETE FROM messages_fts WHERE session_id=?", (sid,))
                con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?)",
                            (sid, rec.get("started_at"), rec.get("ended_at") or _now(),
                             rec.get("language"), rec.get("summary"),
                             rec.get("dominant_emotion")))
                for i, t in enumerate(turns):
                    role = "user" if t.get("role") == "user" else "assistant"
                    text = _clean(t.get("text"))[:4000]
                    con.execute("INSERT INTO messages (session_id, role, text, seq) "
                                "VALUES (?,?,?,?)", (sid, role, text, i))
                    con.execute("INSERT INTO messages_fts (text, session_id, role) "
                                "VALUES (?,?,?)", (text, sid, role))
        finally:
            con.close()
    return {"ok": True, "id": sid, "messages": len(turns)}


def _fts_query(query):
    # Quote every term (so FTS operators in a query can't misfire) and OR them.
    # Trigrams need 3+ characters per term.
    terms = [w.replace('"', "") for w in re.findall(r"\w+", str(query or ""), re.UNICODE)]
    terms = [w for w in terms if len(w) >= 3]
    return " OR ".join(f'"{w}"' for w in terms)


def session_search(query, limit=8):
    expr = _fts_query(query)
    if not expr:
        return {"ok": False, "error": "Search for a word of at least 3 letters."}
    limit = max(1, min(int(limit or 8), 25))
    con = _db()
    try:
        rows = con.execute("""
            SELECT f.text, f.role, f.session_id, s.started_at, s.summary
            FROM messages_fts f LEFT JOIN sessions s ON s.id = f.session_id
            WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?""",
                           (expr, limit)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        con.close()
    return {"ok": True, "query": query, "searched_sessions": total,
            "matches": [{"when": r["started_at"], "who": r["role"],
                         "text": r["text"][:400], "session": r["session_id"],
                         "session_summary": r["summary"]} for r in rows],
            "note": ("Past conversation excerpts. Quote dates as given; if nothing "
                     "matches, say you don't remember rather than guessing.")}


def recent_sessions(limit=5):
    con = _db()
    try:
        rows = con.execute("SELECT id, started_at, summary, dominant_emotion FROM sessions "
                           "ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
        count = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        con.close()
    return {"ok": True, "sessions": count, "messages": msgs,
            "recent": [dict(r) for r in rows]}


# ===========================================================================
# 2b. HISTORY — the sidebar's records
# ===========================================================================

HISTORY_KEEP = 1000            # oldest beyond this are pruned
_HIST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _check_entry(e):
    if not isinstance(e, dict):
        return "entry must be an object"
    if not _HIST_ID.match(str(e.get("id") or "")):
        return "entry needs a short id"
    if e.get("mode") not in ("text", "voice"):
        return "mode must be text or voice"
    try:
        int(e.get("at"))
    except (TypeError, ValueError):
        return "at must be a timestamp in ms"
    return None


def _put_history(con, e):
    data = json.dumps(e, ensure_ascii=False)
    if len(data) > 2_000_000:
        raise ValueError("entry too large")
    con.execute("INSERT OR REPLACE INTO history (id, mode, at, updated_at, data) "
                "VALUES (?,?,?,?,?)", (e["id"], e["mode"], int(e["at"]), _now(), data))


def history_list(limit=300):
    con = _db()
    try:
        rows = con.execute("SELECT data FROM history ORDER BY at DESC LIMIT ?",
                           (max(1, min(int(limit), HISTORY_KEEP)),)).fetchall()
    finally:
        con.close()
    return [json.loads(r["data"]) for r in rows]


def history_save(entry):
    err = _check_entry(entry)
    if err:
        return {"ok": False, "error": err}
    with _lock:
        con = _db()
        try:
            with con:
                _put_history(con, entry)
                con.execute("DELETE FROM history WHERE id IN (SELECT id FROM history "
                            "ORDER BY at DESC LIMIT -1 OFFSET ?)", (HISTORY_KEEP,))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        finally:
            con.close()
    return {"ok": True, "id": entry["id"]}


def history_import(entries):
    """Bulk upload from the browser's old localStorage. Never overwrites."""
    added = 0
    with _lock:
        con = _db()
        try:
            with con:
                for e in entries or []:
                    if _check_entry(e):
                        continue
                    if con.execute("SELECT 1 FROM history WHERE id=?", (e["id"],)).fetchone():
                        continue
                    try:
                        _put_history(con, e)
                        added += 1
                    except ValueError:
                        pass
        finally:
            con.close()
    return {"ok": True, "added": added}


def _forget_session(con, sid):
    con.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    con.execute("DELETE FROM messages_fts WHERE session_id=?", (sid,))
    con.execute("DELETE FROM sessions WHERE id=?", (sid,))


def history_delete(hid):
    """Deleting a conversation also removes it from what she can search."""
    with _lock:
        con = _db()
        try:
            with con:
                n = con.execute("DELETE FROM history WHERE id=?", (str(hid),)).rowcount
                _forget_session(con, str(hid))
        finally:
            con.close()
    return {"ok": True, "deleted": n}


def history_clear():
    with _lock:
        con = _db()
        try:
            with con:
                n = con.execute("DELETE FROM history").rowcount
                con.execute("DELETE FROM messages")
                con.execute("DELETE FROM messages_fts")
                con.execute("DELETE FROM sessions")
        finally:
            con.close()
    return {"ok": True, "deleted": n}


def _dominant(emotions):
    counts = {}
    for e in emotions or []:
        if isinstance(e, dict) and e.get("emotion"):
            counts[e["emotion"]] = counts.get(e["emotion"], 0) + 1
    return max(counts, key=counts.get) if counts else None


def index_voice_record(e):
    """Make a voice history record searchable (session_search)."""
    if e.get("mode") != "voice" or not e.get("turns"):
        return {"ok": False, "skipped": True}
    try:
        at = int(e["at"])
        dur = int(e.get("durationMs") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad timestamps"}

    def fmt(ms):
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")

    return save_session({
        "id": e["id"], "language": e.get("language"), "summary": e.get("summary"),
        "dominant_emotion": _dominant(e.get("emotions")),
        "started_at": fmt(at), "ended_at": fmt(at + dur),
        "turns": e["turns"]})


# ===========================================================================
# 3. SKILLS
# ===========================================================================

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s[:48]


def _skill_path(name):
    return os.path.join(SKILLS_DIR, name, "SKILL.md")


def _parse_skill(text):
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
        body = m.group(2)
    return meta, body.strip()


def _render_skill(name, description, version, body):
    desc = _clean(description).replace('"', "'")
    return (f"---\nname: {name}\ndescription: \"{desc}\"\nversion: {version}\n"
            f"updated: {_now()}\n---\n\n{body.strip()}\n")


def skills_index():
    out = []
    if not os.path.isdir(SKILLS_DIR):
        return out
    for name in sorted(os.listdir(SKILLS_DIR)):
        p = _skill_path(name)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            meta, body = _parse_skill(f.read())
        out.append({"name": meta.get("name", name), "description": meta.get("description", ""),
                    "version": meta.get("version", "1.0.0"), "updated": meta.get("updated"),
                    "steps": body})
    return out


def skills_prompt_block():
    idx = skills_index()
    if not idx:
        return ""
    return "\n".join(f"- {s['name']}: {s['description']}" for s in idx)


def skill_view(name):
    name = _slug(name)
    p = _skill_path(name)
    if not os.path.isfile(p):
        known = ", ".join(s["name"] for s in skills_index()) or "none yet"
        return {"ok": False, "error": f"No skill called '{name}'. Known skills: {known}."}
    with open(p, encoding="utf-8") as f:
        meta, body = _parse_skill(f.read())
    return {"ok": True, "name": name, "description": meta.get("description", ""),
            "version": meta.get("version"), "steps": body,
            "note": "Follow these steps with your tools. Anything that deletes, moves, "
                    "renames or sends still needs the user's yes."}


def _bump(version):
    try:
        a, b, c = (int(x) for x in str(version).split("."))
        return f"{a}.{b}.{c + 1}"
    except Exception:
        return "1.0.1"


def skill_manage(action, name, description=None, content=None, old_text=None, new_text=None):
    action = str(action or "").lower()
    slug = _slug(name)
    if not _SLUG.match(slug or ""):
        return {"ok": False, "error": "Skill names are 2-48 characters: letters, digits, dashes."}
    p = _skill_path(slug)

    with _lock:
        if action == "create":
            if os.path.exists(p):
                return {"ok": False, "error": f"'{slug}' already exists — use patch to improve it."}
            body = str(content or "").strip()
            if not body or not _clean(description):
                return {"ok": False, "error": "A new skill needs a description and steps."}
            if len(body) > 4000:
                return {"ok": False, "error": "Keep a skill under 4000 characters."}
            if _looks_secret(body):
                return {"ok": False, "error": "Skills must not contain passwords or keys."}
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(_render_skill(slug, description, "1.0.0", body))
            return {"ok": True, "action": "created", "name": slug,
                    "message": f"Saved the skill '{slug}'."}

        if not os.path.isfile(p):
            return {"ok": False, "error": f"No skill called '{slug}'."}
        with open(p, encoding="utf-8") as f:
            meta, body = _parse_skill(f.read())

        if action == "patch":
            desc = _clean(description) or meta.get("description", "")
            if old_text:
                if body.count(old_text) != 1:
                    return {"ok": False, "error": "old_text must match exactly one place in the steps.",
                            "steps": body}
                body = body.replace(old_text, str(new_text or ""))
            elif content:
                body = str(content).strip()
            elif not description:
                return {"ok": False, "error": "Nothing to change."}
            if _looks_secret(body):
                return {"ok": False, "error": "Skills must not contain passwords or keys."}
            version = _bump(meta.get("version", "1.0.0"))
            with open(p, "w", encoding="utf-8") as f:
                f.write(_render_skill(slug, desc, version, body))
            return {"ok": True, "action": "patched", "name": slug, "version": version}

        if action == "delete":
            shutil.rmtree(os.path.dirname(p), ignore_errors=True)
            return {"ok": True, "action": "deleted", "name": slug}

    return {"ok": False, "error": "action must be create, patch or delete"}


# A starter skill so the idea is visible from day one. Only written if absent,
# so edits made by the user (or by her) are never overwritten.
STARTER_SKILLS = {
    "morning-briefing": (
        "Short spoken morning update: weather, today's calendar, unread email.",
        """1. get_weather with no location (uses where the user is). Keep: temperature, conditions, anything notable today.
2. list_events for today's date. Mention the times of what is coming up; say plainly if the day is free.
3. email_status for the unread count. If email is not connected, say so in one line and move on.
4. Speak it as three short sentences, warm and brisk. No lists read aloud, no filler."""),
}


def ensure_starter_skills():
    for name, (desc, body) in STARTER_SKILLS.items():
        if not os.path.exists(_skill_path(name)):
            os.makedirs(os.path.dirname(_skill_path(name)), exist_ok=True)
            with open(_skill_path(name), "w", encoding="utf-8") as f:
                f.write(_render_skill(name, desc, "1.0.0", body))


def health():
    mem = memory_state()
    try:
        s = recent_sessions(1)
        db = f"{s['sessions']} conversations, {s['messages']} messages indexed"
        ok = True
    except Exception as e:
        db, ok = f"database error: {e}", False
    return {
        "ok": ok,
        "memory": {t: f"{v['used']}/{v['limit']} chars ({len(v['entries'])} entries)"
                   for t, v in mem.items()},
        "sessions": db,
        "skills": len(skills_index()),
    }
