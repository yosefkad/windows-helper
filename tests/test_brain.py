"""Unit tests for brain.py (memory, conversation search, skills). Uses a temp dir."""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"   {detail}"))


tmp = tempfile.mkdtemp(prefix="bena_brain_")
brain.MEM_DIR = os.path.join(tmp, "memories")
brain.SKILLS_DIR = os.path.join(tmp, "skills")
brain.DB_PATH = os.path.join(tmp, "brain.db")

try:
    print("[memory]")
    r = brain.memory_action("add", "user", "User's name is Yosef; prefers short answers in Hebrew.")
    check("add to USER", r["ok"] and r["entries"] == 1, r)
    r = brain.memory_action("add", "user", "User's name is Yosef; prefers short answers in Hebrew.")
    check("exact duplicate is not stored twice", r["ok"] and "Already" in r.get("note", ""), r)
    brain.memory_action("add", "memory", "Desktop is redirected to OneDrive (שולחן העבודה).")
    snap = brain.memory_snapshot()
    check("snapshot contains both notes", "ABOUT THE USER" in snap and "HER OWN NOTES" in snap, snap)
    r = brain.memory_action("replace", "user", "User's name is Yosef; likes short answers.", old_text="prefers short")
    check("replace by unique substring", r["ok"] and "likes short" in brain.memory_snapshot(), r)
    brain.memory_action("add", "user", "Yosef studies at SCE.")
    r = brain.memory_action("remove", "user", old_text="Yosef")
    check("ambiguous substring refused", not r["ok"] and "matches 2" in r["error"], r)
    r = brain.memory_action("remove", "user", old_text="studies at SCE")
    check("remove by unique substring", r["ok"] and r["entries"] == 1, r)
    r = brain.memory_action("add", "user", "API key is sk-abcdefghijklmnopqrstuvwxyz123")
    check("refuses to store an API key", not r["ok"] and "never stored" in r["error"], r)
    r = brain.memory_action("add", "user", "wifi password: hunter2")
    check("refuses to store a password", not r["ok"], r)
    for i in range(40):
        r = brain.memory_action("add", "user", f"Filler fact number {i} " + "x" * 40)
        if not r["ok"]:
            break
    check("cap enforced, current entries returned", (not r["ok"]) and r.get("full") and r.get("entries"), r)
    check("file never exceeds the cap", brain.memory_state()["user"]["used"] <= 1375)

    print("\n[conversation search]")
    brain.save_session({"id": "s1", "started_at": "2026-09-23 09:10", "language": "he",
                        "summary": "Booked dentist",
                        "turns": [{"role": "user", "text": "תזכירי לי לקבוע תור לרופא שיניים"},
                                  {"role": "agent", "text": "בטח, קבעתי תזכורת."}]})
    brain.save_session({"id": "s2", "started_at": "2026-09-24 18:00", "language": "en",
                        "summary": "Weather", "turns": [{"role": "user", "text": "What's the weather in Haifa?"}]})
    r = brain.session_search("רופא")
    check("Hebrew finds a word with a prefix (רופא in לרופא)", r["ok"] and r["matches"] and
          r["matches"][0]["session"] == "s1", r)
    r = brain.session_search("haifa weather")
    check("English multi-word search", r["matches"] and r["matches"][0]["session"] == "s2", r)
    r = brain.session_search('" OR 1=1 --')
    check("FTS operators / quotes in the query can't break it", r["ok"] or "at least 3" in r.get("error", ""), r)
    brain.save_session({"id": "s2", "started_at": "2026-09-24 18:00",
                        "turns": [{"role": "user", "text": "changed text"}]})
    check("re-saving a session replaces it (no duplicates)",
          not brain.session_search("haifa")["matches"] and brain.recent_sessions()["sessions"] == 2)

    print("\n[skills]")
    brain.ensure_starter_skills()
    check("starter skill installed", any(s["name"] == "morning-briefing" for s in brain.skills_index()))
    r = brain.skill_manage("create", "Tidy Downloads", "Report big old files in Downloads",
                           "1. list_folder downloads\n2. Name the 5 largest files older than 30 days.")
    check("create (name slugged)", r["ok"] and r["name"] == "tidy-downloads", r)
    check("index line in prompt block", "tidy-downloads: Report big old files" in brain.skills_prompt_block())
    r = brain.skill_view("tidy-downloads")
    check("view returns steps", r["ok"] and "5 largest" in r["steps"], r)
    r = brain.skill_manage("patch", "tidy-downloads", old_text="5 largest", new_text="10 largest")
    check("patch bumps version", r["ok"] and r["version"] == "1.0.1", r)
    check("patch applied", "10 largest" in brain.skill_view("tidy-downloads")["steps"])
    r = brain.skill_manage("create", "tidy-downloads", "dup", "x")
    check("create refuses to overwrite", not r["ok"], r)
    r = brain.skill_manage("create", "../../evil", "x", "y")
    check("path traversal in a skill name is neutralised",
          (not r["ok"]) or os.path.isdir(os.path.join(brain.SKILLS_DIR, r["name"])), r)
    r = brain.skill_manage("delete", "tidy-downloads")
    check("delete", r["ok"] and not brain.skill_view("tidy-downloads")["ok"], r)
    with open(os.path.join(brain.SKILLS_DIR, "morning-briefing", "SKILL.md"), "a", encoding="utf-8") as f:
        f.write("\n5. user edit")
    brain.ensure_starter_skills()
    check("a user-edited starter skill is never overwritten",
          "user edit" in brain.skill_view("morning-briefing")["steps"])

    print("\n[database: history + memory import]")
    import sqlite3
    con = sqlite3.connect(brain.DB_PATH)
    rows = con.execute("SELECT target, text FROM memory ORDER BY target, pos").fetchall()
    con.close()
    check("memory is stored in brain.db, not in files",
          ("user", "User's name is Yosef; likes short answers.") in rows
          and not os.path.exists(os.path.join(brain.MEM_DIR, "USER.md")), rows)

    # Notes kept as files by the earlier version are imported once, then renamed.
    old_dir = os.path.join(tmp, "old_memories")
    os.makedirs(old_dir)
    with open(os.path.join(old_dir, "USER.md"), "w", encoding="utf-8") as f:
        f.write("המשתמש אמר ששמו יוסף\n§\nWorks as a nurse")
    brain.MEM_DIR, brain.DB_PATH = old_dir, os.path.join(tmp, "fresh.db")
    check("old USER.md entries are imported into the database",
          brain.memory_state()["user"]["entries"] == ["המשתמש אמר ששמו יוסף", "Works as a nurse"],
          brain.memory_state()["user"])
    check("…and the file is kept as USER.md.imported, not deleted",
          os.path.exists(os.path.join(old_dir, "USER.md.imported"))
          and not os.path.exists(os.path.join(old_dir, "USER.md")))

    v = {"id": "v1", "mode": "voice", "at": 1790000000000, "language": "he", "durationMs": 60000,
         "turns": [{"role": "user", "text": "תזכירי לי לקנות חלב"}, {"role": "model", "text": "בסדר"}],
         "emotions": [{"emotion": "calm"}, {"emotion": "calm"}, {"emotion": "tired"}]}
    check("save a voice record", brain.history_save(v)["ok"])
    v["summary"] = "asked to remember milk"
    brain.history_save(v)
    h = brain.history_list()
    check("saving again updates it instead of duplicating",
          len(h) == 1 and h[0]["summary"] == "asked to remember milk", h)
    brain.history_save({"id": "t1", "mode": "text", "at": 1790000001000, "text": "hello"})
    check("newest first", [e["id"] for e in brain.history_list()] == ["t1", "v1"])
    check("bad records are refused", not brain.history_save({"id": "../x", "mode": "voice", "at": 1})["ok"]
          and not brain.history_save({"id": "x", "mode": "other", "at": 1})["ok"])
    brain.index_voice_record(v)
    r = brain.session_search("חלב")
    check("voice record is searchable", r["ok"] and len(r["matches"]) == 1, r)
    r = brain.history_import([dict(v, summary="IMPORTED COPY"), {"id": "t2", "mode": "text", "at": 5}])
    check("import adds new records and never overwrites existing ones",
          r["added"] == 1 and brain.history_list()[-1]["id"] == "t2"
          and next(e for e in brain.history_list() if e["id"] == "v1")["summary"] != "IMPORTED COPY", r)
    brain.history_delete("v1")
    check("deleting a conversation also removes it from search",
          not brain.session_search("חלב")["matches"] and "v1" not in [e["id"] for e in brain.history_list()])
    brain.history_clear()
    check("clear empties history", brain.history_list() == [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
