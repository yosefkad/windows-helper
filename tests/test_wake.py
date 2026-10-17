"""
Rules that decide whether the desktop wake word fires (wake_listener._decide).

    py -3.12 tests/test_wake.py

No microphone or speech model needed: these feed Vosk-shaped results straight
in. The acoustic side was measured separately with synthetic clips (see the
docstring at the top of wake_listener.py).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wake_listener as W  # noqa: E402

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"   {detail}"))


def words(*triples, start=0.5):
    out, t = [], start
    for w, conf, dur in triples:
        out.append({"word": w, "conf": conf, "start": t, "end": t + dur})
        t += dur
    return {"result": out, "text": " ".join(w for w, _, _ in triples)}


L = W.WakeListener("hey gemini")
print("[wake word decisions]")
check("clear 'hey gemini' wakes it", L._decide(words(("hey", 1.0, .3), ("gemini", 1.0, .5)), True))
check("…inside a longer sentence too",
      L._decide(words(("hey", .95, .3), ("gemini", .97, .5), ("what", 1, .2), ("time", 1, .3)), True))
check("a mumbled 'hey' is fine if 'gemini' is sure",
      L._decide(words(("hey", .55, .3), ("gemini", .95, .5)), True))
check("partial (mid-sound) results never wake it",
      not L._decide(words(("hey", 1.0, .3), ("gemini", 1.0, .5)), False))
check("'gemini' on its own does not wake it", not L._decide(words(("gemini", 1.0, .5)), True))
check("an unsure 'gemini' does not wake it",
      not L._decide(words(("hey", 1.0, .3), ("gemini", .6, .5)), True))
check("a very unsure 'hey' does not wake it",
      not L._decide(words(("hey", .3, .3), ("gemini", 1.0, .5)), True))
check("words out of order do not wake it",
      not L._decide(words(("gemini", 1.0, .5), ("hey", 1.0, .3)), True))
check("noise stretched over 4 seconds does not wake it",
      not L._decide(words(("hey", 1.0, 1.5), ("gemini", 1.0, 2.5)), True))
check("[unk] between the words does not wake it",
      not L._decide(words(("hey", 1.0, .3), ("[unk]", 1.0, .3), ("gemini", 1.0, .5)), True))

g = L._grammar()
check("grammar offers decoys (near-misses like 'jimmy', 'germany')",
      '"jimmy"' in g and '"germany"' in g and '"hey gemini"' in g)
check("…but never the wake words on their own",
      '"gemini"' not in g and '"hey"' not in g, g[:200])

C = W.WakeListener("hey computer")
check("a custom phrase works the same way",
      C._decide(words(("hey", .9, .3), ("computer", .92, .6)), True)
      and not C._decide(words(("computer", 1.0, .6)), True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
