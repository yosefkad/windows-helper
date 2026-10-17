import os
import re
import json
import ctypes
import datetime
import threading
import time
import joblib
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables
load_dotenv()

app = Flask(__name__, static_url_path='', static_folder='static')

# Serve JS/CSS fresh. A cached live.js is indistinguishable from a broken app:
# the old file declares no tools, so the model cannot call them and falls back
# to talking about what it "did". Never let the browser hold a stale copy.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0


@app.after_request
def _no_store_static(response):
    if request.path.endswith(('.js', '.css', '.html')) or request.path == '/':
        response.headers['Cache-Control'] = 'no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    return response

# Initialize OpenAI Client
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key)

# --- Gemini Live (voice mode) ---
# The browser never sees this key. It only ever receives a short-lived
# ephemeral token minted by /api/gemini-token.
LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
gemini_api_key = os.getenv("GEMINI_API_KEY")
gemini_client = None

if gemini_api_key and gemini_api_key != "your_gemini_key_here":
    try:
        from google import genai
        gemini_client = genai.Client(api_key=gemini_api_key)
        print("Gemini Live client ready.")
    except ImportError:
        print("Warning: google-genai not installed. Run: pip install google-genai")
    except Exception as e:
        print(f"Warning: could not init Gemini client: {e}")
else:
    print("Warning: GEMINI_API_KEY not set. Voice mode will be disabled.")

# AIS: Load Brain (Decision AI)
try:
    print("Loading Decision AI model...")
    clf = joblib.load('model.pkl')
    vectorizer = joblib.load('vectorizer.pkl')
    print("Decision AI loaded successfully.")
except Exception as e:
    print(f"Warning: Decision AI model not found. Run train_model.py first. Error: {e}")
    clf = None
    vectorizer = None

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/favicon.ico')
def favicon():
    # No icon to serve; 204 keeps it out of the log instead of a 404 per load.
    return ('', 204)

@app.route('/api/analyze', methods=['POST'])
def analyze_text():
    if not api_key or api_key == "your_api_key_here":
         return jsonify({"error": "OpenAI API Key is missing or invalid in .env file"}), 500

    data = request.json
    text = data.get('text')
    
    if not text:
        return jsonify({"error": "No text provided"}), 400

    try:
        # AIS STAGE 1: ANALYSIS (Decision AI)
        decision_meta = {}
        if clf and vectorizer:
            # Predict "Academic" vs "Casual"
            text_vector = vectorizer.transform([text])
            prediction = clf.predict(text_vector)[0]
            probs = clf.predict_proba(text_vector)[0]
            probability = probs.max()
            
            # Logic: If confidence is low, strictly classify as AMBIGUOUS
            if probability < 0.60:
                prediction = "ambiguous"

            decision_meta = {
                "detected_type": prediction,
                "confidence": f"{probability:.2%}"
            }
        else:
            decision_meta = {"detected_type": "unknown", "confidence": "0%"}

        # --- HYBRID AI GUARDRAILS ---
        # Rule: "Code" must look like code. If ML says "Code" but there are no symbols, it's probably slang.
        if decision_meta["detected_type"] == "code":
            code_indicators = ["def ", "class ", "print", "return", "import ", "var ", "const ", "{", "}", "(", ")", ";", "[", "]", "=", "if ", "for ", "while "]
            if not any(indicator in text for indicator in code_indicators):
                print(f"Guardrail triggered: '{text}' looks like Casual/Ambiguous, not Code.")
                decision_meta["detected_type"] = "ambiguous" # Downgrade to ambiguous
        # ----------------------------

        decision_meta["detected_type"] = decision_meta.get("detected_type", "unknown")
        
        # --- LEVEL 3: MANUAL TONE OVERRIDE ---
        target_tone = data.get('target_tone', 'Auto')
        
        if target_tone != 'Auto':
            # Override the detected action based on user selection
            if target_tone == 'Funny':
                action = "Make it Funny"
                system_prompt = "You are a professional comedian. Rewrite the text to be humorous, witty, and entertaining."
                user_prompt = f"Make this text funny:\n\n{text}"
            elif target_tone == 'Legal':
                action = "Legal Formal"
                system_prompt = "You are a lawyer. Rewrite the text to be formal, precise, and legally sound."
                user_prompt = f"Rewrite this as a legal document:\n\n{text}"
            elif target_tone == 'Poetic':
                action = "Poetic Style"
                system_prompt = "You are a poet. Rewrite the text in a poetic, lyrical style."
                user_prompt = f"Rewrite this as a poem:\n\n{text}"
            elif target_tone == 'Hebrew':
                action = "Translate to Hebrew"
                system_prompt = "You are a professional translator. Translate the text to natural, fluent Hebrew."
                user_prompt = f"Translate to Hebrew:\n\n{text}"
            else:
                action = f"Manual: {target_tone}"
                system_prompt = "You are a helpful assistant."
                user_prompt = f"Rewrite this text in a {target_tone} style:\n\n{text}"
                
            # Update meta to show the override
            decision_meta["detected_type"] = f"Manual ({target_tone})"
            
        else:
            # AIS STAGE 2: LOGIC ENGINE (Auto Mode)
            detected_type = decision_meta.get("detected_type", "casual")
            
            system_prompt = "You are a helpful AI assistant."
            user_prompt = f"Process this text:\n\n{text}"
    
            # Dynamic Logic
            if detected_type == "academic":
                action = "Simplify & Summarize"
                system_prompt = "You are an expert academic tutor. Your goal is to simplify complex academic text for students. Summarize the key points clearly."
                user_prompt = f"Analyze this academic text and provide a simple summary:\n\n{text}"
            elif detected_type == "casual":
                action = "Professional Polish"
                system_prompt = "You are a professional editor. Your goal is to take casual text and rewrite it to be more professional and structured."
                user_prompt = f"Rewrite this casual text to be professional:\n\n{text}"
            elif detected_type == "code":
                action = "Debug & Explain"
                system_prompt = "You are an expert software engineer. Review the provided code snippet. Explain what it does and fix any errors if present."
                user_prompt = f"Analyze and fix this code:\n\n{text}"
            elif detected_type == "angry":
                action = "De-escalate & Polite"
                system_prompt = "You are a conflict resolution specialist. Your goal is to rewrite angry or aggressive text into a calm, polite, and constructive message."
                user_prompt = f"Rewrite this angry text to be polite:\n\n{text}"
            else: # ambiguous or unknown
                action = "General Improvement"
                system_prompt = "You are a helpful writing assistant. The text tone is unclear. Improve clarity and grammar while maintaining a neutral, balanced tone."
                user_prompt = f"Improve the clarity and grammar of this text:\n\n{text}"

        decision_meta["action_taken"] = action

        # AIS STAGE 3: EXECUTION (OpenAI)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )

        result = response.choices[0].message.content.strip()
        
        return jsonify({
            "result": result,
            "meta": decision_meta
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/gemini-token', methods=['POST'])
def gemini_token():
    """
    Mint a short-lived ephemeral token for the browser's Live API WebSocket.

    Why: the browser has to hold the WebSocket itself (audio must not round-trip
    through Flask), but we refuse to ship GEMINI_API_KEY to the client. An
    ephemeral token is single-use and expires in minutes, so a leaked one is
    close to worthless.
    """
    if gemini_client is None:
        return jsonify({
            "error": "Voice mode unavailable: set GEMINI_API_KEY in .env "
                     "and run 'pip install google-genai', then restart."
        }), 503

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    try:
        token = gemini_client.auth_tokens.create(config={
            "uses": 1,
            # Window to OPEN a session with this token.
            "new_session_expire_time": now + datetime.timedelta(minutes=2),
            # How long that session may then stay alive.
            "expire_time": now + datetime.timedelta(minutes=30),
        })
        return jsonify({"token": token.name, "model": LIVE_MODEL})
    except Exception as e:
        return jsonify({"error": f"Could not mint Gemini token: {e}"}), 500


# ---------------------------------------------------------------------------
# LOCAL ACTIONS
#
# Gives the voice agent a small, deliberately boring set of real abilities.
# Everything here is CREATE- and READ-only: there is no delete, no overwrite,
# and no command execution. A misheard word should never be able to destroy
# anything, and voice input gets misheard constantly.
# ---------------------------------------------------------------------------

def _resolve_user_folders():
    """
    Real paths for Desktop/Documents/Downloads.

    Not guessable: on Windows these are routinely redirected (OneDrive, and
    localised names — this machine's Desktop is literally
    'OneDrive - .../שולחן העבודה'). Joining home/'Desktop' would silently make
    a phantom folder the user never sees, so read the registry instead.
    """
    home = os.path.expanduser("~")
    folders = {
        "desktop": os.path.join(home, "Desktop"),
        "documents": os.path.join(home, "Documents"),
        "downloads": os.path.join(home, "Downloads"),
    }

    if os.name == "nt":
        try:
            import winreg
            key_path = (r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders")
            guids = {
                "desktop": "Desktop",
                "documents": "Personal",
                "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
            }
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for name, value_name in guids.items():
                    try:
                        raw, _ = winreg.QueryValueEx(key, value_name)
                        expanded = os.path.expandvars(raw)
                        if os.path.isdir(expanded):
                            folders[name] = expanded
                    except OSError:
                        pass
        except Exception as e:
            print(f"Could not read shell folders from registry: {e}")

    # Project-local scratch area, always available.
    workspace = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
    folders["workspace"] = workspace
    return folders


USER_FOLDERS = _resolve_user_folders()
print("Action roots:", {k: v for k, v in USER_FOLDERS.items()})

# Windows reserved device names — creating these breaks things in odd ways.
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
            | {f"lpt{i}" for i in range(1, 10)}


MAX_DEPTH = 8


def _split_segments(rel):
    """Split 'Projects/2026\\Q1' into ['Projects', '2026', 'Q1']."""
    raw = str(rel or "").replace("\\", "/").split("/")
    return [p.strip() for p in raw if p.strip() not in ("", ".")]


def _safe_name(name):
    """Validate a single path SEGMENT. Returns (name, error)."""
    if not name or not str(name).strip():
        return None, "Name is empty"
    name = str(name).strip()

    if len(name) > 100:
        return None, "Name is too long (max 100 characters)"
    if any(sep in name for sep in ("/", "\\")):
        return None, "Name cannot contain path separators"
    if name in (".", "..") or name.startswith(".."):
        return None, "Invalid name"
    if any(ord(c) < 32 for c in name):
        return None, "Name contains control characters"
    # Windows chokes on these and on trailing dots/spaces.
    if any(c in name for c in '<>:"|?*'):
        return None, 'Name cannot contain < > : " | ? *'
    if name != name.rstrip(". "):
        return None, "Name cannot end with a dot or space"
    if name.split(".")[0].lower() in _RESERVED:
        return None, f"'{name}' is a reserved system name"
    return name, None


def _resolve_in_root(location, name=None, nested=True):
    """
    Resolve `name` inside the named root, refusing anything that escapes it.

    `name` may be a nested relative path ("Projects/2026/Q1") so the agent can
    work inside subfolders. Every segment is validated individually and the
    final path is realpath-checked against the root, so nesting does not widen
    the sandbox by one inch. Pass nested=False where only a bare name makes
    sense (a rename must not also move the item).

    Returns (abs_path, root, error).
    """
    root = USER_FOLDERS.get(str(location or "").lower())
    if not root:
        return None, None, (f"Unknown location '{location}'. "
                            f"Choose one of: {', '.join(sorted(USER_FOLDERS))}")

    os.makedirs(root, exist_ok=True)
    root_real = os.path.realpath(root)

    if name is None:
        return root_real, root_real, None

    # Refuse absolute and UNC forms outright rather than silently reinterpreting
    # them as relative. "//server/share/x" landing in the sandbox as
    # "server/share/x" is contained, but it hides the fact that the caller asked
    # for something else entirely.
    raw = str(name).strip()
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        return None, root_real, ("Name must be a relative path inside the folder, "
                                 "not an absolute or network path")

    segments = _split_segments(name)
    if not segments:
        return None, root_real, "Name is empty"
    if not nested and len(segments) > 1:
        return None, root_real, "Name cannot contain path separators"
    if len(segments) > MAX_DEPTH:
        return None, root_real, f"Too deeply nested (max {MAX_DEPTH} levels)"

    for seg in segments:
        _, err = _safe_name(seg)
        if err:
            return None, root_real, err

    target = os.path.realpath(os.path.join(root_real, *segments))
    # The realpath comparison also defeats symlink escapes.
    if target != root_real and not target.startswith(root_real + os.sep):
        return None, root_real, "Path escapes the allowed folder"
    return target, root_real, None


def _act_create_folder(args):
    name = args.get("name")
    location = args.get("location", "desktop")
    path, root, err = _resolve_in_root(location, name)
    if err:
        return {"ok": False, "error": err}

    if os.path.isdir(path):
        return {"ok": True, "already_existed": True, "path": path,
                "message": f"A folder named '{os.path.basename(path)}' is already there."}
    if os.path.exists(path):
        return {"ok": False, "error": "A file with that name already exists"}

    try:
        # makedirs so "Projects/2026/Q1" works in one go — every segment was
        # already validated and the whole path realpath-checked against the root.
        os.makedirs(path)
        # Every folder bena creates gets an empty notes.txt inside (in the
        # folder that was asked for, e.g. "2026" for "Projects/2026").
        # "x" = create only, never overwrite.
        note = os.path.join(path, "notes.txt")
        try:
            open(note, "x", encoding="utf-8").close()
        except OSError:
            note = None
        rel = os.path.relpath(path, USER_FOLDERS[str(location).lower()])
        return {"ok": True, "created": True, "path": path, "note": note,
                "message": f"Created '{rel}' in {location}"
                           + (", with an empty notes.txt inside." if note else ".")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _act_list_folder(args):
    location = args.get("location", "desktop")
    name = args.get("name") or None
    path, root, err = _resolve_in_root(location, name)
    if err:
        return {"ok": False, "error": err}
    if not os.path.isdir(path):
        return {"ok": False, "error": "That folder does not exist"}

    try:
        all_names = sorted(os.listdir(path))
        shown = all_names[:60]
        folders, files = [], []
        for e in shown:
            full = os.path.join(path, e)
            # Real metadata, so the model never has to guess a date. Without
            # this it will cheerfully invent one.
            try:
                st = os.stat(full)
                info = {
                    "name": e,
                    "modified": datetime.datetime.fromtimestamp(st.st_mtime)
                                        .strftime("%Y-%m-%d %H:%M"),
                    "created": datetime.datetime.fromtimestamp(st.st_ctime)
                                       .strftime("%Y-%m-%d %H:%M"),
                }
                if not os.path.isdir(full):
                    info["size_bytes"] = st.st_size
            except OSError:
                info = {"name": e}
            (folders if os.path.isdir(full) else files).append(info)

        return {"ok": True, "path": path, "folders": folders, "files": files,
                "shown": len(shown), "total": len(all_names),
                "truncated": len(all_names) > len(shown),
                "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _act_file_info(args):
    """Exact metadata for one item — dates, size, type."""
    location = args.get("location", "desktop")
    path, _, err = _resolve_in_root(location, args.get("name"))
    if err:
        return {"ok": False, "error": err}
    if not os.path.exists(path):
        return {"ok": False, "error": f"'{args.get('name')}' does not exist in {location}"}
    try:
        st = os.stat(path)
        fmt = "%Y-%m-%d %H:%M:%S"
        return {
            "ok": True,
            "name": os.path.basename(path),
            "path": path,
            "is_folder": os.path.isdir(path),
            "size_bytes": None if os.path.isdir(path) else st.st_size,
            "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime(fmt),
            "created": datetime.datetime.fromtimestamp(st.st_ctime).strftime(fmt),
            "accessed": datetime.datetime.fromtimestamp(st.st_atime).strftime(fmt),
            "now": datetime.datetime.now().strftime(fmt),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _act_write_note(args):
    name = args.get("name")
    content = args.get("content", "")
    location = args.get("location", "desktop")
    if not str(name or "").lower().endswith(".txt"):
        name = f"{name}.txt"

    path, root, err = _resolve_in_root(location, name)
    if err:
        return {"ok": False, "error": err}
    if os.path.exists(path):
        # Never clobber. Refusing is the safe failure for a voice agent.
        return {"ok": False,
                "error": f"'{os.path.basename(path)}' already exists; pick another name"}

    try:
        # A note inside a folder that doesn't exist yet ("Projects/notes")
        # creates the folder too. Gemini may send create_folder and write_note
        # in the same message, and those run at the same time — the note must
        # not depend on the folder winning the race. Every segment was already
        # validated and the whole path realpath-checked against the root.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(content))
        return {"ok": True, "path": path,
                "message": f"Saved '{os.path.basename(path)}' to {location}."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- destructive actions -----------------------------------------------
# These are gated twice: the client must show a confirmation the user physically
# clicks BEFORE calling here, and delete goes to the Recycle Bin rather than
# unlinking, so a mistake is always recoverable.

def _act_delete_item(args):
    name = args.get("name")
    location = args.get("location", "desktop")
    path, root, err = _resolve_in_root(location, name)
    if err:
        return {"ok": False, "error": err}
    if not os.path.exists(path):
        return {"ok": False, "error": f"'{name}' does not exist in {location}"}
    if path == root:
        return {"ok": False, "error": "Refusing to delete the root folder itself"}

    try:
        from send2trash import send2trash
    except ImportError:
        return {"ok": False,
                "error": "send2trash is not installed, so deleting safely is unavailable. "
                         "Run: pip install send2trash"}

    try:
        # Recycle Bin, never os.remove — voice input gets misheard.
        send2trash(os.path.normpath(path))
        return {"ok": True, "recycled": True, "path": path,
                "message": f"Moved '{os.path.basename(path)}' to the Recycle Bin. "
                           f"You can restore it from there."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _act_rename_item(args):
    location = args.get("location", "desktop")
    old_path, root, err = _resolve_in_root(location, args.get("name"))
    if err:
        return {"ok": False, "error": err}
    # Renaming must not relocate: the new name stays a single segment, resolved
    # inside the item's own parent folder.
    new_seg, err2 = _safe_name(args.get("new_name"))
    if err2:
        return {"ok": False, "error": err2}
    new_path = os.path.join(os.path.dirname(old_path), new_seg)

    if not os.path.exists(old_path):
        return {"ok": False, "error": f"'{args.get('name')}' does not exist in {location}"}
    if os.path.exists(new_path):
        return {"ok": False,
                "error": f"'{os.path.basename(new_path)}' already exists; pick another name"}

    try:
        os.rename(old_path, new_path)
        return {"ok": True, "path": new_path,
                "message": f"Renamed to '{os.path.basename(new_path)}'."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _act_move_item(args):
    """
    Move a file or folder, including INTO a subfolder.

    to_path is what makes folder-to-folder possible: without it the destination
    could only ever be the root of a location, so "put this file in that folder"
    was impossible.
    """
    location = args.get("location", "desktop")
    src, _, err = _resolve_in_root(location, args.get("name"))
    if err:
        return {"ok": False, "error": err}
    if not os.path.exists(src):
        return {"ok": False, "error": f"'{args.get('name')}' does not exist in {location}"}

    # Staying in the same location is the common case ("move it into Projects").
    to_location = args.get("to_location") or location
    to_path = args.get("to_path")
    dst_dir, dst_root, err2 = _resolve_in_root(to_location, to_path or None)
    if err2:
        return {"ok": False, "error": err2}

    if os.path.isfile(dst_dir):
        return {"ok": False,
                "error": f"'{to_path}' is a file, not a folder to move into"}

    dst = os.path.join(dst_dir, os.path.basename(src))
    src_real, dst_real = os.path.realpath(src), os.path.realpath(dst)

    if src_real == dst_real:
        return {"ok": False, "error": "That is already where it is"}
    # Moving a folder inside itself would eat it.
    if os.path.isdir(src_real) and dst_real.startswith(src_real + os.sep):
        return {"ok": False, "error": "A folder cannot be moved inside itself"}
    if os.path.exists(dst):
        return {"ok": False,
                "error": f"'{os.path.basename(src)}' already exists at the destination"}

    try:
        if not os.path.isdir(dst_dir):
            os.makedirs(dst_dir)          # create the destination folder
        import shutil
        shutil.move(src, dst)
        where = f"{to_location}/{to_path}" if to_path else to_location
        return {"ok": True, "path": dst,
                "message": f"Moved '{os.path.basename(src)}' to {where}."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- timers, calendar, weather ---------------------------------------------

def _notify_windows(title, body):
    """
    A notification from the machine itself, not the browser tab.

    Deliberately not a web Notification: those die with the page. This fires
    from the Flask process, so the reminder still arrives if the orb is closed.
    """
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass
    try:
        # MB_SYSTEMMODAL|MB_SETFOREGROUND|MB_TOPMOST so it surfaces over
        # whatever is fullscreen.
        ctypes.windll.user32.MessageBoxW(
            0, str(body), str(title), 0x00001000 | 0x00010000 | 0x00040000)
    except Exception as e:
        print(f"[timer] could not show notification: {e}")


_timers = {}
_timer_threads = {}      # id -> threading.Timer, so cancel can really stop it
_timer_seq = [0]


def _act_set_timer(args):
    try:
        seconds = float(args.get("seconds"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "seconds must be a number"}
    if seconds <= 0:
        return {"ok": False, "error": "seconds must be positive"}
    if seconds > 24 * 3600:
        return {"ok": False, "error": "Timers are limited to 24 hours"}

    label = str(args.get("label") or "Timer").strip()[:80]
    _timer_seq[0] += 1
    tid = f"t{_timer_seq[0]}"
    fires = datetime.datetime.now() + datetime.timedelta(seconds=seconds)

    def fire():
        _timers.pop(tid, None)
        _timer_threads.pop(tid, None)
        _notify_windows(f"⏰ {label}", f"Your timer for \"{label}\" has finished.")

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    _timer_threads[tid] = t
    _timers[tid] = {"id": tid, "label": label,
                    "fires_at": fires.strftime("%Y-%m-%d %H:%M:%S")}
    return {"ok": True, "id": tid, "label": label, "seconds": seconds,
            "fires_at": _timers[tid]["fires_at"],
            "message": f"Timer set for {label} at {fires.strftime('%H:%M:%S')}."}


def _act_list_timers(args):
    return {"ok": True, "timers": list(_timers.values()),
            "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _act_cancel_timer(args):
    tid = str(args.get("id") or "")
    if tid not in _timers:
        return {"ok": False, "error": f"No timer '{tid}'"}
    info = _timers.pop(tid)
    # Stop the alarm itself — removing it from the list alone left it ringing.
    t = _timer_threads.pop(tid, None)
    if t:
        t.cancel()
    return {"ok": True, "message": f"Cancelled the timer for {info['label']}."}


# --- calendar (local file, not Outlook/Google) ---
CALENDAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "calendar.json")


def _load_events():
    try:
        with open(CALENDAR_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_events(events):
    with open(CALENDAR_PATH, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


def _parse_when(when):
    """Accept 'YYYY-MM-DD HH:MM', 'YYYY-MM-DD', or ISO with a T."""
    raw = str(when or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw, fmt), None
        except ValueError:
            continue
    return None, ("Could not read that date. Use YYYY-MM-DD HH:MM "
                  "(for example 2026-09-23 14:30).")


def _act_add_event(args):
    title = str(args.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "The event needs a title"}
    when, err = _parse_when(args.get("when"))
    if err:
        return {"ok": False, "error": err}

    events = _load_events()
    ev = {
        "id": f"e{int(when.timestamp())}{len(events)}",
        "title": title[:200],
        "when": when.strftime("%Y-%m-%d %H:%M"),
        "notes": str(args.get("notes") or "")[:500],
    }
    events.append(ev)
    events.sort(key=lambda e: e.get("when", ""))
    _save_events(events)
    return {"ok": True, "event": ev,
            "message": f"Added \"{ev['title']}\" on {ev['when']}."}


def _act_list_events(args):
    events = _load_events()
    now = datetime.datetime.now()
    days = args.get("days_ahead")
    try:
        days = int(days) if days is not None else None
    except (TypeError, ValueError):
        days = None

    start = now
    if args.get("date"):
        day, err = _parse_when(args["date"])
        if err:
            return {"ok": False, "error": err}
        start = day.replace(hour=0, minute=0)
        end = start + datetime.timedelta(days=1)
    else:
        end = now + datetime.timedelta(days=days if days is not None else 3650)

    out = []
    for e in events:
        when, err = _parse_when(e.get("when"))
        if err or when is None:
            continue
        if start <= when < end:
            out.append(e)
    return {"ok": True, "events": out, "count": len(out),
            "now": now.strftime("%Y-%m-%d %H:%M"),
            "today": now.strftime("%Y-%m-%d"),
            "tomorrow": (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")}


def _act_delete_event(args):
    events = _load_events()
    target = str(args.get("id") or "")
    title = str(args.get("title") or "").strip().lower()
    keep, removed = [], []
    for e in events:
        if (target and e.get("id") == target) or \
           (title and e.get("title", "").strip().lower() == title):
            removed.append(e)
        else:
            keep.append(e)
    if not removed:
        return {"ok": False, "error": "No matching event"}
    _save_events(keep)
    return {"ok": True, "removed": removed,
            "message": f"Removed {len(removed)} event(s)."}


# --- weather (Open-Meteo: no API key needed) ---
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


_geo_cache = {}


def _detect_location():
    """
    Where the machine actually is, from its public IP.

    Cached for the process lifetime: it is a network round trip and the answer
    does not change while the app is open. BENA_CITY overrides it entirely for
    anyone who would rather not do the lookup.
    """
    override = os.getenv("BENA_CITY")
    if override:
        return {"ok": True, "city": override, "source": "BENA_CITY"}
    if "loc" in _geo_cache:
        return _geo_cache["loc"]

    import urllib.request
    result = {"ok": False, "error": "Could not determine location"}
    try:
        with urllib.request.urlopen("http://ip-api.com/json/?fields=status,city,"
                                    "regionName,country,lat,lon,timezone",
                                    timeout=8) as r:
            d = json.loads(r.read())
        if d.get("status") == "success":
            result = {
                "ok": True,
                "city": d.get("city"),
                "region": d.get("regionName"),
                "country": d.get("country"),
                "latitude": d.get("lat"),
                "longitude": d.get("lon"),
                "timezone": d.get("timezone"),
                "source": "ip",
            }
    except Exception as e:
        result = {"ok": False, "error": f"Location lookup failed: {e}"}

    _geo_cache["loc"] = result
    return result


def _act_where_am_i(args):
    loc = _detect_location()
    if not loc.get("ok"):
        return loc
    return dict(loc, note="Approximate, derived from the public IP address.")


def _act_get_weather(args):
    import urllib.parse
    import urllib.request

    place = str(args.get("location") or "").strip()
    if not place:
        # No city given: use where the machine actually is, and prefer exact
        # coordinates over a city name so the answer is for the right place.
        loc = _detect_location()
        if loc.get("ok") and loc.get("latitude") is not None:
            return _weather_at(loc["latitude"], loc["longitude"],
                               ", ".join(x for x in (loc.get("city"),
                                                     loc.get("country")) if x),
                               detected=True)
        place = loc.get("city") or "Tel Aviv"
    try:
        geo_url = ("https://geocoding-api.open-meteo.com/v1/search?name="
                   + urllib.parse.quote(place) + "&count=1&language=en&format=json")
        with urllib.request.urlopen(geo_url, timeout=12) as r:
            geo = json.loads(r.read())
        if not geo.get("results"):
            return {"ok": False, "error": f"I could not find a place called '{place}'"}
        g = geo["results"][0]
        label = f"{g.get('name')}, {g.get('country', '')}".strip(", ")
        return _weather_at(g["latitude"], g["longitude"], label, detected=False)
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the weather service: {e}"}


def _weather_at(lat, lon, label, detected=False):
    import urllib.request
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}"
               f"&longitude={lon}"
               f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
               f"weather_code,wind_speed_10m"
               f"&daily=temperature_2m_max,temperature_2m_min,weather_code"
               f"&forecast_days=3&timezone=auto")
        with urllib.request.urlopen(url, timeout=12) as r:
            w = json.loads(r.read())

        cur = w.get("current", {})
        daily = w.get("daily", {})
        days = []
        for i, d in enumerate(daily.get("time", [])[:3]):
            days.append({
                "date": d,
                "high_c": daily["temperature_2m_max"][i],
                "low_c": daily["temperature_2m_min"][i],
                "conditions": _WMO.get(daily["weather_code"][i], "unknown"),
            })
        return {
            "ok": True,
            "place": label,
            "auto_detected": detected,
            "temperature_c": cur.get("temperature_2m"),
            "feels_like_c": cur.get("apparent_temperature"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "conditions": _WMO.get(cur.get("weather_code"), "unknown"),
            "forecast": days,
            "source": "open-meteo.com",
        }
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the weather service: {e}"}


# --- email: Gmail directly (app password), the Outlook app as backup --------
# Gmail is used whenever an app password is in .env: reading and sending are
# then fully automatic and Outlook needn't even be open. If Gmail isn't set up,
# or can't be reached / refuses the password, the same call goes to Outlook —
# which reads fine but, with Office unactivated, can only open a pre-filled
# compose window for sending.
def _gmail():
    import mail_gmail
    return mail_gmail


def _outlook():
    # Imported lazily: pywin32 only exists on Windows, and the web build should
    # still start without it.
    import mail_outlook
    return mail_outlook


def _tag(result, provider):
    if isinstance(result, dict):
        result.setdefault("provider", provider)
    return result


def _mail_call(op, *a, **kw):
    g = _gmail()
    problem = None
    if g.configured():
        r = getattr(g, op)(*a, **kw)
        # Only fall back when Gmail itself is unusable — a bad address or an
        # empty search is an answer, not a reason to ask Outlook instead.
        if r.get("ok") or not (r.get("auth_failed") or r.get("network")):
            return _tag(r, "gmail")
        problem = r.get("error")
    r = _tag(getattr(_outlook(), op)(*a, **kw), "outlook")
    if problem and isinstance(r, dict):
        r["gmail_problem"] = problem
    return r


def _mail_read(email_id, mark_read=True):
    # Each provider only understands its own ids ("g:inbox:123" vs Outlook's).
    if _gmail().is_gmail_id(email_id):
        return _tag(_gmail().read_email(email_id, mark_read=mark_read), "gmail")
    return _tag(_outlook().read_email(email_id, mark_read=mark_read), "outlook")


def _act_email_status(args):
    return _mail_call("status")


def _act_list_emails(args):
    return _mail_call("list_emails", args.get("folder", "inbox"), args.get("count", 10),
                      bool(args.get("unread_only")))


def _act_read_email(args):
    return _mail_read(args.get("id"))


def _act_search_emails(args):
    return _mail_call("search_emails", args.get("query"), args.get("count", 10),
                      args.get("folder", "inbox"))


def _act_send_email(args):
    return _mail_call("send_email", args.get("to"), args.get("subject"),
                      args.get("body"), args.get("cc"))


# --- brain: memory, conversation search, skills (ideas from Hermes Agent) ----
import brain       # noqa: E402
import routines    # noqa: E402

brain.ensure_starter_skills()


def _act_memory(args):
    return brain.memory_action(args.get("action"), args.get("target", "memory"),
                               args.get("content"), args.get("old_text"))


def _act_session_search(args):
    return brain.session_search(args.get("query"), args.get("limit", 8))


def _act_skill_view(args):
    return brain.skill_view(args.get("name"))


def _act_skill_manage(args):
    return brain.skill_manage(args.get("action"), args.get("name"), args.get("description"),
                              args.get("content"), args.get("old_text"), args.get("new_text"))


def _act_schedule_routine(args):
    return routines.create_job(args.get("name"), args.get("schedule"),
                               args.get("prompt"), args.get("skill"))


def _act_list_routines(args):
    jobs = routines.list_jobs()
    return {"ok": True, "count": len(jobs),
            "routines": [{"name": j["name"], "when": j.get("when"), "enabled": j["enabled"],
                          "next_run": j.get("next_run"), "last_run": j.get("last_run"),
                          "last_status": j.get("last_status"),
                          "last_result": (j.get("last_result") or "")[:300]} for j in jobs],
            "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}


def _act_cancel_routine(args):
    return routines.delete_job(args.get("name"))


ACTIONS = {
    "memory": _act_memory,
    "session_search": _act_session_search,
    "skill_view": _act_skill_view,
    "skill_manage": _act_skill_manage,
    "schedule_routine": _act_schedule_routine,
    "list_routines": _act_list_routines,
    "cancel_routine": _act_cancel_routine,
    "email_status": _act_email_status,
    "list_emails": _act_list_emails,
    "read_email": _act_read_email,
    "search_emails": _act_search_emails,
    "send_email": _act_send_email,
    "set_timer": _act_set_timer,
    "list_timers": _act_list_timers,
    "cancel_timer": _act_cancel_timer,
    "add_event": _act_add_event,
    "list_events": _act_list_events,
    "delete_event": _act_delete_event,
    "get_weather": _act_get_weather,
    "where_am_i": _act_where_am_i,
    "create_folder": _act_create_folder,
    "list_folder": _act_list_folder,
    "file_info": _act_file_info,
    "write_note": _act_write_note,
    "delete_item": _act_delete_item,
    "rename_item": _act_rename_item,
    "move_item": _act_move_item,
}

# ---------------------------------------------------------------------------
# TOOL SWITCHES
#
# Every capability the agent has can be switched off by the user. This registry
# is the single source of truth for both the settings UI and ENFORCEMENT: a
# switched-off tool is refused here, server-side. Hiding it in the page alone
# would not be enough — the model could still emit the call.
# ---------------------------------------------------------------------------

TOOL_GROUPS = [
    {"id": "files_read",   "label": "Look at files",          "icon": "📂",
     "tools": ["list_folder", "file_info"]},
    {"id": "files_create", "label": "Create files & folders", "icon": "➕",
     "tools": ["create_folder", "write_note"]},
    {"id": "files_change", "label": "Rename & move",          "icon": "✏️",
     "tools": ["rename_item", "move_item"]},
    {"id": "files_delete", "label": "Delete files",           "icon": "🗑️",
     "tools": ["delete_item"]},
    {"id": "email_read",   "label": "Read email",             "icon": "📥",
     "tools": ["list_emails", "read_email", "search_emails", "email_status"]},
    {"id": "email_send",   "label": "Send email",             "icon": "📤",
     "tools": ["send_email"]},
    {"id": "calendar",     "label": "Calendar",               "icon": "📅",
     "tools": ["list_events", "add_event", "delete_event"]},
    {"id": "timers",       "label": "Timers",                 "icon": "⏰",
     "tools": ["set_timer", "list_timers", "cancel_timer"]},
    {"id": "weather",      "label": "Weather",                "icon": "🌤️",
     "tools": ["get_weather"]},
    {"id": "location",     "label": "Location",               "icon": "📍",
     "tools": ["where_am_i"]},
    {"id": "memory",       "label": "Memory",                 "icon": "🧠",
     "tools": ["memory", "session_search"]},
    {"id": "skills",       "label": "Skills",                 "icon": "📘",
     "tools": ["skill_view", "skill_manage"]},
    {"id": "routines",     "label": "Routines",               "icon": "🔁",
     "tools": ["schedule_routine", "list_routines", "cancel_routine"]},
]

TOOL_LABELS = {
    "list_folder": "See what is in a folder",
    "file_info": "See a file's dates and size",
    "create_folder": "Create folders",
    "write_note": "Save text notes",
    "rename_item": "Rename files & folders",
    "move_item": "Move files & folders",
    "delete_item": "Move to Recycle Bin",
    "list_emails": "List emails",
    "read_email": "Open an email",
    "search_emails": "Search email",
    "email_status": "Check Outlook is reachable",
    "send_email": "Send email (always asks first)",
    "list_events": "See calendar events",
    "add_event": "Add calendar events",
    "delete_event": "Remove calendar events",
    "set_timer": "Set timers",
    "list_timers": "See running timers",
    "cancel_timer": "Cancel timers",
    "get_weather": "Check the weather",
    "where_am_i": "Detect your city",
    "memory": "Remember things about you",
    "session_search": "Search past conversations",
    "skill_view": "Use saved skills",
    "skill_manage": "Write & improve skills",
    "schedule_routine": "Set up routines (always asks first)",
    "list_routines": "See routines",
    "cancel_routine": "Remove routines",
}

ALL_TOOLS = [t for g in TOOL_GROUPS for t in g["tools"]]
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tool_settings.json")
_settings_lock = threading.Lock()


def _load_tool_settings():
    """Enabled state per tool. Anything not recorded defaults to ON."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            saved = {}
    except Exception:
        saved = {}
    return {t: bool(saved.get(t, True)) for t in ALL_TOOLS}


def _save_tool_settings(state):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)          # atomic: never a half-written file


def _tool_enabled(tool):
    # Tools outside the registry (none today) are allowed, so adding an action
    # never silently breaks before it gets a switch.
    if tool not in ALL_TOOLS:
        return True
    return _load_tool_settings().get(tool, True)


@app.route('/api/tools', methods=['GET'])
def tools_get():
    state = _load_tool_settings()
    groups = []
    for g in TOOL_GROUPS:
        tools = [{"id": t, "label": TOOL_LABELS.get(t, t), "enabled": state[t],
                  "confirm": t in DESTRUCTIVE} for t in g["tools"]]
        groups.append({"id": g["id"], "label": g["label"], "icon": g["icon"],
                       "enabled": all(x["enabled"] for x in tools),
                       "partial": any(x["enabled"] for x in tools)
                                  and not all(x["enabled"] for x in tools),
                       "tools": tools})
    return jsonify({"groups": groups,
                    "enabled": [t for t, on in state.items() if on],
                    "disabled": [t for t, on in state.items() if not on]})


@app.route('/api/tools', methods=['POST'])
def tools_set():
    """Body: {"tool": id, "enabled": bool}  or  {"group": id, "enabled": bool}."""
    data = request.json or {}
    on = bool(data.get("enabled"))
    with _settings_lock:
        state = _load_tool_settings()
        if data.get("group"):
            group = next((g for g in TOOL_GROUPS if g["id"] == data["group"]), None)
            if not group:
                return jsonify({"ok": False, "error": "Unknown group"}), 400
            for t in group["tools"]:
                state[t] = on
        elif data.get("tool") in state:
            state[data["tool"]] = on
        else:
            return jsonify({"ok": False, "error": "Unknown tool"}), 400
        _save_tool_settings(state)
    return tools_get()


# The client must get the user's explicit yes before calling these. send_email
# is here because it acts on the outside world in the user's name and cannot
# be taken back — a misheard address sends real mail to a real person.
DESTRUCTIVE = {"delete_item", "rename_item", "move_item", "send_email",
               # a routine keeps acting on its own after the call ends
               "schedule_routine"}


@app.route('/api/actions', methods=['POST'])
def run_action():
    data = request.json or {}
    tool = data.get('tool')
    args = data.get('args') or {}

    handler = ACTIONS.get(tool)
    if not handler:
        return jsonify({"ok": False, "error": f"Unknown action '{tool}'"}), 400

    # The user's switches are enforced HERE, where the work actually happens.
    # A stale page or a model that still remembers the tool gets refused.
    if not _tool_enabled(tool):
        return jsonify({
            "ok": False, "disabled": True,
            "error": (f"The user has switched off '{TOOL_LABELS.get(tool, tool)}' in "
                      f"Tools settings. Tell them it is turned off; do not try to do "
                      f"it another way."),
        }), 403

    # Server-side backstop. The UI asks the user first, but a destructive call
    # must never succeed just because something skipped that step.
    if tool in DESTRUCTIVE and not data.get('confirmed'):
        return jsonify({"ok": False,
                        "error": "This action needs explicit user confirmation"}), 403

    print(f"[action] {tool} {args}")
    result = handler(args)
    print(f"[action] -> {result}")
    return jsonify(result)


# --- offline wake word (desktop shell only) --------------------------------
# Created lazily: the browser build uses the Web Speech API instead and should
# never pay for loading a speech model.
_wake = {"listener": None, "pending": False}


def _get_wake():
    if _wake["listener"] is None:
        from wake_listener import WakeListener

        def fired(text):
            _wake["pending"] = True
            print(f"[wake] heard {text!r}")

        _wake["listener"] = WakeListener(on_wake=fired)
    return _wake["listener"]


_wake_lock = threading.Lock()

# Used when the selected language has no offline model. English words, because
# that is what the English model can actually hear — and what a Hebrew speaker
# is saying anyway when they say "היי ג'מיני".
ENGLISH_WAKE_FALLBACK = "hey gemini"


@app.route('/api/wake/arm', methods=['POST'])
def wake_arm():
    data = request.json or {}
    # Serialised: two concurrent arms could otherwise build two listeners,
    # each holding the microphone.
    with _wake_lock:
        listener = _get_wake()
        # Always re-arm from a stopped state. start() short-circuits when it is
        # already running, so changing the phrase on a live listener would keep
        # the OLD grammar while reporting the new phrase back — and skip the
        # vocabulary check entirely.
        listener.stop()
        if data.get('lang'):
            listener.set_lang(data['lang'])
        if data.get('phrase'):
            listener.phrase = data['phrase']
        listener.error = None          # a past failure must not stick forever
        ok = listener.start()

        # Vosk ships no Hebrew model (36 languages, none of them Hebrew), and a
        # Hebrew phrase is not in the English vocabulary either — so a Hebrew
        # user would just get an error. Fall back to English instead: "היי
        # ג'מיני" IS "hey gemini", so the English model hears it fine. Only the
        # wake phrase changes; the conversation stays in the chosen language.
        fell_back = False
        notice = None
        if not ok and listener.lang != "en-us":
            reason = listener.error
            listener.stop()
            listener.set_lang("en")
            listener.phrase = ENGLISH_WAKE_FALLBACK
            listener.error = None
            ok = listener.start()
            if ok:
                fell_back = True
                # A NOTICE, not an error: listener.error must stay null or the
                # poll loop would treat this as fatal and stop handling wakes.
                notice = (
                    f"No offline model for that language, so hands-free is "
                    f"listening for \"{ENGLISH_WAKE_FALLBACK}\" instead. "
                    f"The conversation itself is unaffected."
                )
            else:
                listener.error = reason or listener.error

        if ok:
            listener.resume()

    return jsonify({"ok": ok, "error": listener.error, "notice": notice,
                    "fell_back": fell_back,
                    "phrase": listener.phrase, "lang": listener.lang})


@app.route('/api/wake/disarm', methods=['POST'])
def wake_disarm():
    listener = _wake["listener"]
    if listener:
        listener.stop()
    _wake["pending"] = False
    return jsonify({"ok": True})


@app.route('/api/wake/pause', methods=['POST'])
def wake_pause():
    """Hand the microphone to the Live session for the duration of a call."""
    listener = _wake["listener"]
    if listener:
        if (request.json or {}).get('paused'):
            listener.pause()
        else:
            listener.resume()
    return jsonify({"ok": True})


@app.route('/api/wake/poll', methods=['GET'])
def wake_poll():
    listener = _wake["listener"]
    fired = _wake["pending"]
    _wake["pending"] = False        # consume it
    return jsonify({
        "triggered": fired,
        "running": bool(listener and listener.running),
        "heard": listener.last_heard if listener else "",
        "error": listener.error if listener else None,
    })


# --- shell state, shared between the orb process and the web page ----------
# orb.py is a separate process (a Win32 layered window, because WebView2 cannot
# render a non-rectangular one), so the two talk through here.
_shell = {"orb": "idle", "toggle": 0, "quit": False}


@app.route('/api/shell/state', methods=['GET'])
def shell_state():
    return jsonify(_shell)


@app.route('/api/shell/orb', methods=['POST'])
def shell_set_orb():
    """The page tells the orb what colour to be: idle / woke / live."""
    state = (request.json or {}).get('state', 'idle')
    if state in ('idle', 'woke', 'live'):
        _shell['orb'] = state
    return jsonify({"ok": True, "orb": _shell['orb']})


@app.route('/api/shell/toggle', methods=['POST'])
def shell_toggle():
    """The orb was clicked; the page picks this up and expands or collapses."""
    _shell['toggle'] += 1
    return jsonify({"ok": True, "toggle": _shell['toggle']})


@app.route('/api/shell/quit', methods=['POST'])
def shell_quit():
    _shell['quit'] = True
    return jsonify({"ok": True})


@app.route('/api/action-roots', methods=['GET'])
def action_roots():
    """Lets the UI show the user exactly which folders are reachable."""
    return jsonify({"roots": {k: v for k, v in sorted(USER_FOLDERS.items())}})


@app.route('/api/summarize', methods=['POST'])
def summarize_session():
    """
    One-line summary of a finished voice conversation, for the history panel.

    Uses a small/cheap text model — this runs once per session and the output
    is a single sentence, so there is no reason to pay for a big one.
    """
    data = request.json or {}
    turns = data.get('turns') or []
    language = data.get('language', 'en')
    emotion = data.get('dominant_emotion')

    if not turns:
        return jsonify({"summary": ""})

    # Keep the prompt bounded regardless of how long the call ran.
    lines = []
    for turn in turns[-40:]:
        who = "User" if turn.get('role') == 'user' else "Assistant"
        text = (turn.get('text') or '').strip()
        if text:
            lines.append(f"{who}: {text[:500]}")
    transcript = "\n".join(lines)[:6000]

    if not transcript:
        return jsonify({"summary": ""})

    out_lang = "Hebrew" if language == "he" else "English"
    mood = f" The speaker mostly sounded {emotion}." if emotion else ""
    prompt = (
        f"Summarize this voice conversation in ONE short sentence "
        f"(max 15 words), written in {out_lang}. Describe what the user "
        f"wanted or talked about — do not mention that it is a summary.{mood}\n\n"
        f"{transcript}"
    )

    # Prefer Gemini (same provider as the voice session); fall back to OpenAI.
    if gemini_client is not None:
        try:
            resp = gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt,
            )
            return jsonify({"summary": (resp.text or "").strip(), "by": "gemini"})
        except Exception as e:
            print(f"Gemini summary failed, falling back to OpenAI: {e}")

    if api_key and api_key != "your_api_key_here":
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=60,
            )
            return jsonify({
                "summary": resp.choices[0].message.content.strip(),
                "by": "openai"
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "No model available to summarize"}), 503


@app.route('/api/voice-status', methods=['GET'])
def voice_status():
    """Lets the frontend grey out the voice button instead of failing on click."""
    return jsonify({"enabled": gemini_client is not None})


@app.route('/api/feedback', methods=['POST'])
def save_feedback():
    data = request.json
    feedback_entry = {
        "text": data.get('text'),
        "predicted_type": data.get('predicted_type'),
        "detected_type": data.get('detected_type'), # keeping consistent naming
        "feedback": data.get('feedback'), # 'up' or 'down'
        "timestamp": data.get('timestamp')
    }
    
    # In a real app, save to DB. Here, append to a log file.
    try:
        with open("feedback_log.json", "a") as f:
            import json
            f.write(json.dumps(feedback_entry) + "\n")
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# HEALTH — is every connection actually working, right now?
#
# Each check does a REAL test (mint a Gemini token, log in to IMAP, reach the
# weather API…) but a non-intrusive one: nothing is written to your folders,
# the microphone is not opened, and Outlook is not launched unless you ask —
# starting it pops its window, and a stuck sign-in there blocks forever.
# Switched-off integrations are skipped entirely (a disabled location service
# should not quietly phone ip-api in the background).
# ---------------------------------------------------------------------------

# Integrations the user can switch on/off map onto the SAME tool groups the
# Tools tab controls — one source of truth, enforced in run_action.
INTEGRATIONS = [
    {"id": "gemini",   "label": "Gemini Live — voice",   "icon": "🎙️", "groups": []},
    {"id": "openai",   "label": "OpenAI — text mode",    "icon": "✍️", "groups": []},
    {"id": "wake",     "label": "Wake word (offline)",   "icon": "👂", "groups": []},
    {"id": "folders",  "label": "Folders",               "icon": "📁",
     "groups": ["files_read", "files_create", "files_change", "files_delete"]},
    {"id": "email",    "label": "Email",                 "icon": "📧",
     "groups": ["email_read", "email_send"]},
    {"id": "calendar", "label": "Calendar",              "icon": "📅", "groups": ["calendar"]},
    {"id": "timers",   "label": "Timers",                "icon": "⏰", "groups": ["timers"]},
    {"id": "weather",  "label": "Weather",               "icon": "🌤️", "groups": ["weather"]},
    {"id": "location", "label": "Location",              "icon": "📍", "groups": ["location"]},
    {"id": "memory",   "label": "Memory",                "icon": "🧠", "groups": ["memory"]},
    {"id": "skills",   "label": "Skills",                "icon": "📘", "groups": ["skills"]},
    {"id": "routines", "label": "Routines",              "icon": "🔁", "groups": ["routines"]},
]


def _integration_enabled(integ, tool_state=None):
    if not integ["groups"]:
        return True                     # core services have no switch
    state = tool_state or _load_tool_settings()
    tools = [t for g in TOOL_GROUPS if g["id"] in integ["groups"] for t in g["tools"]]
    return any(state.get(t, True) for t in tools)


def _integration_partial(integ, tool_state=None):
    if not integ["groups"]:
        return False
    state = tool_state or _load_tool_settings()
    tools = [t for g in TOOL_GROUPS if g["id"] in integ["groups"] for t in g["tools"]]
    vals = [state.get(t, True) for t in tools]
    return any(vals) and not all(vals)


def _outlook_running():
    """Without launching it: is OUTLOOK.EXE already up?"""
    if os.name != "nt":
        return False
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OUTLOOK.EXE", "/NH"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=0x08000000)       # CREATE_NO_WINDOW
        return "outlook.exe" in out.stdout.lower()
    except Exception:
        return False


def _hc_gemini():
    if gemini_client is None:
        return {"status": "unconfigured",
                "detail": "GEMINI_API_KEY is missing from .env — voice is off."}
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    gemini_client.auth_tokens.create(config={
        "uses": 1,
        "new_session_expire_time": now + datetime.timedelta(minutes=1),
        "expire_time": now + datetime.timedelta(minutes=2),
    })
    return {"status": "ok", "detail": "Session token minted — voice is ready."}


def _hc_openai():
    if not api_key or api_key == "your_api_key_here":
        return {"status": "unconfigured", "detail": "OPENAI_API_KEY is missing from .env."}
    client.with_options(timeout=8).models.retrieve("gpt-4o-mini")
    return {"status": "ok", "detail": "API key accepted — gpt-4o-mini reachable."}


def _hc_wake():
    try:
        import vosk  # noqa: F401
        import sounddevice as sd
    except Exception as e:
        return {"status": "error", "detail": f"Speech libraries missing: {e}"}
    cache = os.path.join(os.path.expanduser("~"), ".cache", "vosk")
    models = [d for d in (os.listdir(cache) if os.path.isdir(cache) else [])
              if d.startswith("vosk-model")]
    try:
        mic = sd.query_devices(kind="input")["name"]
    except Exception:
        mic = None
    listener = _wake.get("listener")
    running = bool(listener and listener.running)
    if not mic:
        return {"status": "error", "detail": "No microphone found."}
    if not models:
        return {"status": "warn", "detail": f"No offline model yet — it downloads the "
                                            f"first time hands-free is switched on. Mic: {mic}"}
    return {"status": "ok",
            "detail": f"{'Listening now · ' if running else ''}Model: {models[0]} · Mic: {mic}",
            "extra": {"models": models, "mic": mic, "listening": running,
                      "note": "Vosk has no Hebrew model — the wake phrase is English."}}


def _hc_folders():
    roots = []
    for name, path in sorted(USER_FOLDERS.items()):
        exists = os.path.isdir(path)
        roots.append({"name": name, "path": path, "exists": exists,
                      "writable": exists and os.access(path, os.W_OK)})
    good = sum(1 for r in roots if r["exists"] and r["writable"])
    status = "ok" if good == len(roots) else ("warn" if good else "error")
    return {"status": status, "detail": f"{good}/{len(roots)} folders reachable and writable.",
            "extra": {"roots": roots}}


def _hc_email():
    """Gmail directly when an app password is set up; otherwise the Outlook app."""
    import mail_gmail
    if mail_gmail.configured():
        r = mail_gmail.status(check_send=True)
        extra = {"provider": "gmail", "accounts": [mail_gmail.address()],
                 "gmail": "connected" if r.get("ok") else "error",
                 "steps": r.get("steps"), "can_send": r.get("can_send"),
                 "send_problem": r.get("send_problem")}
        if not r.get("ok"):
            extra["gmail_error"] = r.get("error")
            fallback = _hc_outlook()
            fallback["extra"].update(extra, provider="outlook")
            fallback["status"] = "error" if r.get("auth_failed") else "warn"
            fallback["detail"] = (f"Gmail direct: {r.get('error')} Using Outlook meanwhile — "
                                  + fallback["detail"])
            return fallback
        if not r.get("can_send"):
            return {"status": "warn",
                    "detail": f"{mail_gmail.address()} · {r['inbox_total']} in inbox. Reading "
                              f"works, but sending failed: {r.get('send_problem')}",
                    "extra": extra}
        return {"status": "ok",
                "detail": f"{mail_gmail.address()} · Gmail direct · {r['inbox_total']} in inbox, "
                          f"{r['inbox_unread']} unread · sending is automatic",
                "extra": extra}
    res = _hc_outlook()
    res["extra"].update(provider="outlook", gmail="not_configured",
                        steps=mail_gmail.SETUP_STEPS)
    return res


def _hc_outlook():
    """
    Outlook, installed on this machine. The account settings are read from the
    registry, so a broken login is caught even while Outlook is closed; the
    live mailbox check only runs if Outlook is already open (launching it pops
    its window, and a stuck sign-in there blocks).
    """
    import mail_outlook
    accounts = mail_outlook.account_config()
    problems = mail_outlook.config_problems(accounts)
    addrs = [a["address"] for a in accounts]
    running = _outlook_running()
    extra = {"accounts": addrs, "problems": problems, "running": running,
             "synced": None, "fix": None}

    if not accounts:
        return {"status": "unconfigured",
                "detail": "No email account is set up in Outlook.", "extra": extra}

    if running:
        r = mail_outlook.status()
        if not r.get("ok"):
            return {"status": "error", "detail": r.get("error"), "extra": extra}
        extra.update(synced=r["synced"], fix=r.get("fix"), problems=r.get("problems", []),
                     unlicensed=r.get("unlicensed", False), can_send=r.get("can_send"))
        if r.get("unlicensed"):
            # Reading works and sending falls back to a pre-filled Gmail/mail-app
            # window, so this is a limitation, not an outage: amber, not red.
            return {"status": "warn",
                    "detail": f"{', '.join(addrs)} · {r['inbox_total']} in inbox. Reading works; "
                              f"Office isn't activated, so sending opens the finished email in "
                              f"Gmail for you to press Send.",
                    "extra": extra}
        if not r["synced"]:
            return {"status": "error",
                    "detail": f"{', '.join(addrs)} has never connected to the mail server "
                              f"— Outlook has no mail to read.", "extra": extra}
        return {"status": "ok",
                "detail": f"{', '.join(addrs)} · {r['inbox_total']} in inbox, "
                          f"{r['inbox_unread']} unread", "extra": extra}

    if problems:
        extra["fix"] = mail_outlook._not_synced_error(accounts)["error"]
        return {"status": "error",
                "detail": f"{', '.join(addrs)} is set up wrong, so it cannot connect.",
                "extra": extra}
    return {"status": "warn",
            "detail": f"{', '.join(addrs)} · Outlook is closed, so the mailbox was not "
                      f"checked. Checking would open it.", "extra": extra}


def _hc_calendar():
    events = 0
    if os.path.exists(CALENDAR_PATH):
        with open(CALENDAR_PATH, encoding="utf-8") as f:
            data = json.load(f)                 # raises on a corrupt file -> error
        events = len(data) if isinstance(data, list) else 0
    folder = os.path.dirname(CALENDAR_PATH)
    if not os.access(folder, os.W_OK):
        return {"status": "error", "detail": "Cannot write the calendar file."}
    upcoming = len(_act_list_events({"days_ahead": 7}).get("events", []))
    return {"status": "ok",
            "detail": f"Local calendar · {events} event(s), {upcoming} in the next 7 days.",
            "extra": {"path": CALENDAR_PATH, "note": "Local file, not Google/Outlook calendar."}}


def _hc_timers():
    return {"status": "ok", "detail": f"Runs on this computer · {len(_timers)} running."}


def _hc_weather():
    loc = _geo_cache.get("loc") or {}
    lat, lon = loc.get("latitude", 32.08), loc.get("longitude", 34.78)
    r = _weather_at(lat, lon, loc.get("city") or "test")
    if not r.get("ok"):
        return {"status": "error", "detail": r.get("error")}
    return {"status": "ok",
            "detail": f"open-meteo.com reachable · {r['temperature_c']}°C, {r['conditions']}"}


def _hc_location():
    loc = _detect_location()
    if not loc.get("ok"):
        return {"status": "error", "detail": loc.get("error")}
    if loc.get("source") == "BENA_CITY":
        return {"status": "ok", "detail": f"Fixed in .env: {loc['city']}"}
    return {"status": "ok",
            "detail": f"{loc.get('city')}, {loc.get('country')} · approximate, from your IP"}


def _hc_memory():
    h = brain.health()
    if not h["ok"]:
        return {"status": "error", "detail": h["sessions"]}
    return {"status": "ok",
            "detail": f"About you: {h['memory']['user']} · her notes: {h['memory']['memory']} "
                      f"· {h['sessions']}"}


def _hc_skills():
    idx = brain.skills_index()
    names = ", ".join(s["name"] for s in idx)
    return {"status": "ok", "detail": f"{len(idx)} skill(s)" + (f": {names}" if names else "")}


def _hc_routines():
    if gemini_client is None:
        return {"status": "error", "detail": "GEMINI_API_KEY is missing — routines cannot run."}
    jobs = routines.list_jobs()
    active = [j for j in jobs if j.get("enabled")]
    upcoming = sorted(j["next_run"] for j in active if j.get("next_run"))
    if _scheduler.active:
        who = "scheduler running here"
    elif _scheduler.last_tick:
        who = "another bena window is running them"
    else:
        who = "scheduler starts with the first page load"
    return {"status": "ok",
            "detail": f"{len(active)} active · next {upcoming[0] if upcoming else '—'} · {who} "
                      f"· model {routines.MODEL}"}


HEALTH_CHECKS = {
    "gemini": _hc_gemini, "openai": _hc_openai, "wake": _hc_wake,
    "folders": _hc_folders, "email": _hc_email, "calendar": _hc_calendar,
    "timers": _hc_timers, "weather": _hc_weather, "location": _hc_location,
    "memory": _hc_memory, "skills": _hc_skills, "routines": _hc_routines,
}
HEALTH_TIMEOUT_S = 20


def _run_check(integ, tool_state):
    base = {"id": integ["id"], "label": integ["label"], "icon": integ["icon"],
            "toggleable": bool(integ["groups"]),
            "enabled": _integration_enabled(integ, tool_state),
            "partial": _integration_partial(integ, tool_state)}
    if not base["enabled"]:
        return dict(base, status="off", latency_ms=None,
                    detail="Switched off — she can't use this, and it isn't being checked.")

    box = {}

    def work():
        t0 = time.time()
        try:
            r = HEALTH_CHECKS[integ["id"]]()
        except Exception as e:
            r = {"status": "error", "detail": f"{type(e).__name__}: {str(e)[:180]}"}
        r["latency_ms"] = int((time.time() - t0) * 1000)
        box["r"] = r

    # Daemon thread + deadline, like the mail providers: a wedged dependency
    # must not freeze the page or block the app from exiting.
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(HEALTH_TIMEOUT_S)
    r = box.get("r") or {"status": "error", "latency_ms": HEALTH_TIMEOUT_S * 1000,
                         "detail": f"No answer within {HEALTH_TIMEOUT_S}s."}
    return dict(base, **r)


def _health(ids=None):
    state = _load_tool_settings()
    targets = [i for i in INTEGRATIONS if ids is None or i["id"] in ids]
    results = [None] * len(targets)

    def one(idx, integ):
        results[idx] = _run_check(integ, state)

    threads = [threading.Thread(target=one, args=(i, g), daemon=True)
               for i, g in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(HEALTH_TIMEOUT_S + 2)
    results = [r for r in results if r]

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": counts, "integrations": results}


@app.route('/api/health', methods=['GET'])
def health_all():
    return jsonify(_health())


@app.route('/api/health/<integ_id>', methods=['GET'])
def health_one(integ_id):
    if integ_id not in HEALTH_CHECKS:
        return jsonify({"error": "Unknown integration"}), 404
    return jsonify(_health([integ_id]))


@app.route('/api/health/outlook-probe', methods=['POST'])
def health_outlook_probe():
    """The user explicitly asked: check Outlook even if that opens it."""
    import mail_outlook
    r = mail_outlook.status()
    return jsonify({"ok": bool(r.get("ok")), "result": r})


@app.route('/api/outlook/open', methods=['POST'])
def outlook_open():
    """Put Outlook on screen so the user can fix the account (their Google sign-in)."""
    import mail_outlook
    return jsonify(mail_outlook.open_outlook())


@app.route('/api/integrations', methods=['POST'])
def integrations_set():
    """Body: {"id": "email", "enabled": false} -> flips all of its tool groups."""
    data = request.json or {}

    integ = next((i for i in INTEGRATIONS if i["id"] == data.get("id")), None)
    if not integ:
        return jsonify({"ok": False, "error": "Unknown integration"}), 400
    if not integ["groups"]:
        return jsonify({"ok": False, "error": f"{integ['label']} has no switch"}), 400

    on = bool(data.get("enabled"))
    with _settings_lock:
        state = _load_tool_settings()
        for g in TOOL_GROUPS:
            if g["id"] in integ["groups"]:
                for t in g["tools"]:
                    state[t] = on
        _save_tool_settings(state)
    return jsonify(_health([integ["id"]]))


# ---------------------------------------------------------------------------
# ROUTINES — the unattended runner (Hermes-style cron)
#
# A routine runs with nobody watching, so it gets an explicit ALLOW-LIST:
# reading, looking things up and creating things. Nothing destructive (no
# human to confirm), and nothing that edits memory, skills or routines (no
# scheduling loops, and nothing an email says can make itself permanent).
# The user's tool switches still apply on top.
# ---------------------------------------------------------------------------
import atexit  # noqa: E402

_LOCS = {"type": "STRING", "enum": ["desktop", "documents", "downloads", "workspace"]}


def _obj(required=None, **props):
    s = {"type": "OBJECT", "properties": props}
    if required:
        s["required"] = required
    return s


def _s(desc=""):
    return {"type": "STRING", "description": desc}


ROUTINE_DECLARATIONS = [
    {"name": "get_weather", "description": "Current weather and 3-day forecast. Omit location "
     "for the user's own city.", "parameters": _obj(location=_s("City; omit for the user's location"))},
    {"name": "where_am_i", "description": "The user's city and country."},
    {"name": "list_events", "description": "Calendar events. Also returns today/tomorrow dates.",
     "parameters": _obj(date=_s("One day, YYYY-MM-DD"),
                        days_ahead={"type": "NUMBER", "description": "Days ahead to include"})},
    {"name": "add_event", "description": "Add a calendar event.",
     "parameters": _obj(["title", "when"], title=_s(), when=_s("YYYY-MM-DD HH:MM"), notes=_s())},
    {"name": "list_timers", "description": "Timers currently running."},
    {"name": "list_folder", "description": "What is in a folder, with dates and sizes.",
     "parameters": _obj(["location"], location=_LOCS, name=_s("Optional subfolder"))},
    {"name": "file_info", "description": "Exact dates and size of one item.",
     "parameters": _obj(["name", "location"], name=_s(), location=_LOCS)},
    {"name": "create_folder", "description": "Create a folder (never overwrites).",
     "parameters": _obj(["name"], name=_s("Name or nested path"), location=_LOCS)},
    {"name": "write_note", "description": "Save a new .txt note (never overwrites).",
     "parameters": _obj(["name", "content"], name=_s(), content=_s(), location=_LOCS)},
    {"name": "email_status", "description": "Whether email is connected, and the unread count."},
    {"name": "list_emails", "description": "Recent emails, newest first.",
     "parameters": _obj(folder=_s("inbox, sent, drafts, deleted, junk"),
                        count={"type": "NUMBER"}, unread_only={"type": "BOOLEAN"})},
    {"name": "search_emails", "description": "Find emails by words.",
     "parameters": _obj(["query"], query=_s(), count={"type": "NUMBER"})},
    {"name": "read_email", "description": "Open one email by id (stays unread).",
     "parameters": _obj(["id"], id=_s())},
    {"name": "session_search", "description": "Search past conversations with the user.",
     "parameters": _obj(["query"], query=_s())},
    {"name": "skill_view", "description": "Load a saved skill's steps.",
     "parameters": _obj(["name"], name=_s())},
]
_ROUTINE_ALLOWED = {d["name"] for d in ROUTINE_DECLARATIONS}
assert not (_ROUTINE_ALLOWED & DESTRUCTIVE), "a destructive tool leaked into routines"


def _routine_execute(name, args):
    if name in DESTRUCTIVE or name not in _ROUTINE_ALLOWED:
        return {"ok": False, "error": f"'{name}' is not available in routines."}
    if not _tool_enabled(name):
        return {"ok": False, "disabled": True,
                "error": f"The user has switched off '{TOOL_LABELS.get(name, name)}'."}
    if name == "read_email":                  # looking is not the same as reading
        return _mail_read(args.get("id"), mark_read=False)
    return ACTIONS[name](args)


def _routine_runner(job):
    if gemini_client is None:
        return "error", "GEMINI_API_KEY is missing, so routines cannot run.", []
    context = []
    if _tool_enabled("memory"):
        snap = brain.memory_snapshot()
        if snap:
            context.append("WHAT YOU KNOW ABOUT THE USER AND THIS COMPUTER:\n" + snap)
    if job.get("skill"):
        sv = brain.skill_view(job["skill"])
        if sv.get("ok"):
            context.append(f"FOLLOW THE SKILL '{sv['name']}':\n{sv['steps']}")
    decls = [d for d in ROUTINE_DECLARATIONS if _tool_enabled(d["name"])]
    return routines.run_job(job, gemini_client, decls, _routine_execute, "\n\n".join(context))


def _deliver(title, body):
    # The message box blocks its own thread until dismissed; never the scheduler.
    threading.Thread(target=_notify_windows, args=(title, body), daemon=True).start()


_scheduler = routines.Scheduler(_routine_runner, _deliver)
_scheduler_started = {"v": False}
atexit.register(_scheduler.release)


@app.before_request
def _start_scheduler_once():
    # Started on the first request rather than at import: under Flask's debug
    # reloader the parent process imports app.py too but never serves a
    # request, so this keeps it from running a second scheduler.
    if not _scheduler_started["v"]:
        _scheduler_started["v"] = True
        _scheduler.start()


# --- brain endpoints (Brain tab + voice session context) -------------------

@app.route('/api/brain', methods=['GET'])
def brain_get():
    return jsonify({
        "memory": brain.memory_state(),
        "skills": brain.skills_index(),
        "routines": routines.list_jobs(),
        "feed": routines.feed(10),
        "sessions": brain.recent_sessions(5),
        "scheduler": {"active": _scheduler.active, "last_tick": _scheduler.last_tick,
                      "model": routines.MODEL},
    })


@app.route('/api/brain/context', methods=['GET'])
def brain_context():
    """Frozen snapshot for the start of a voice session. Respects the switches."""
    return jsonify({
        "memory": brain.memory_snapshot() if _tool_enabled("memory") else "",
        "skills": brain.skills_prompt_block() if _tool_enabled("skill_view") else "",
    })


@app.route('/api/brain/search', methods=['GET'])
def brain_search():
    return jsonify(brain.session_search(request.args.get("q", ""), 15))


@app.route('/api/brain/memory', methods=['POST'])
def brain_memory():
    d = request.json or {}
    return jsonify(brain.memory_action(d.get("action"), d.get("target"),
                                       d.get("content"), d.get("old_text")))


@app.route('/api/brain/skill', methods=['POST'])
def brain_skill():
    d = request.json or {}
    return jsonify(brain.skill_manage(d.get("action"), d.get("name"), d.get("description"),
                                      d.get("content"), d.get("old_text"), d.get("new_text")))


@app.route('/api/sessions', methods=['POST'])
def sessions_save():
    """Store a finished voice conversation for session_search."""
    if not _tool_enabled("session_search"):
        # Memory switched off means conversations are not kept either.
        return jsonify({"ok": False, "skipped": True, "error": "Memory is switched off."})
    return jsonify(brain.save_session(request.json or {}))


@app.route('/api/history', methods=['GET'])
def history_get():
    return jsonify({"ok": True, "entries": brain.history_list(request.args.get("limit", 300))})


@app.route('/api/history', methods=['POST'])
def history_save():
    """Upsert one sidebar record. Voice records are saved while the call runs,
    so a conversation survives the app closing mid-call."""
    entry = request.json or {}
    res = brain.history_save(entry)
    if res.get("ok") and entry.get("mode") == "voice" and _tool_enabled("session_search"):
        brain.index_voice_record(entry)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route('/api/history/import', methods=['POST'])
def history_import():
    entries = (request.json or {}).get("entries") or []
    res = brain.history_import(entries)
    if _tool_enabled("session_search"):
        for e in entries:
            if not brain._check_entry(e) and e.get("mode") == "voice":
                brain.index_voice_record(e)
    return jsonify(res)


@app.route('/api/history/<hid>', methods=['DELETE'])
def history_delete(hid):
    return jsonify(brain.history_delete(hid))


@app.route('/api/history', methods=['DELETE'])
def history_clear():
    return jsonify(brain.history_clear())


@app.route('/api/routines', methods=['POST'])
def routines_action():
    d = request.json or {}
    a = d.get("action")
    if a == "create":
        return jsonify(routines.create_job(d.get("name"), d.get("schedule"),
                                           d.get("prompt"), d.get("skill")))
    if a == "delete":
        return jsonify(routines.delete_job(d.get("name")))
    if a in ("enable", "disable"):
        return jsonify(routines.set_enabled(d.get("name"), a == "enable"))
    if a == "run":
        return jsonify(_scheduler.run_now(d.get("name")))
    return jsonify({"ok": False, "error": "action must be create, delete, enable, disable or run"}), 400


if __name__ == '__main__':
    app.run(debug=True)
