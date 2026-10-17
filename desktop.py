"""
SmartText AI — desktop shell.

Runs the existing Flask app on a private port and shows two windows:

  * the orb (orb.py): a truly circular, always-on-top layered window. Click it
    to open or hide the assistant, drag to move, right-click to quit.
  * the panel: the normal web UI in a frameless WebView2 window. It is HIDDEN
    while collapsed, never shrunk — WebView2 can't be cut to a circle, and a
    shrunken panel was the square "orb" with dark corners.

    py -3.12 desktop.py

The wake word is detected in Python with Vosk (wake_listener.py), because
WebView2's SpeechRecognition constructs but never starts.
"""
import os
import socket
import sys
import threading
import time

try:
    import webview
except ImportError:
    sys.exit("pywebview is not installed. Run:  py -3.12 -m pip install pywebview")

import app as flask_app


PANEL_W, PANEL_H = 460, 760


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = int(os.getenv("BENA_PORT") or _free_port())
BASE = f"http://127.0.0.1:{PORT}"


def _serve():
    # threaded so the Live session's fetches never queue behind each other
    flask_app.app.run(host="127.0.0.1", port=PORT, debug=False,
                      use_reloader=False, threaded=True)


def _wait_for_server(timeout=20):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/api/voice-status", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


WINDOW_TITLE = "SmartText AI"

# Where the panel's browser profile lives. pywebview's default is a private,
# throw-away profile deleted on exit, which silently wiped the language choice,
# wake phrase and anything else the page keeps in localStorage on every restart.
PROFILE_DIR = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"),
                           "bena", "webview")


class Bridge:
    """
    Exposed to the page as window.pywebview.api.

    EVERY attribute here must be underscore-prefixed. pywebview walks the
    js_api object's PUBLIC attributes to build the JS-side surface, and a
    pywebview Window leads it into the .NET object graph, where it recurses
    forever through Rectangle.Empty:

        Error while processing window.native.AccessibilityObject.Bounds
        .Empty.Empty.Empty…: maximum recursion depth exceeded

    Only the public *methods* are meant to be exposed; state stays private.
    """

    def __init__(self):
        self._window = None
        self._expanded = False

    def _attach(self, window):
        self._window = window

    # -- exposed to JS --------------------------------------------------

    def toggle(self, expand=None):
        """
        Show or hide the panel. It never shrinks into an orb of its own: the
        only orb is orb.py's layered window. A collapsed WebView2 window can't
        be cut to a circle (it paints an opaque rectangle behind the page), so
        shrinking it left a second, square "orb" with dark corners on screen.
        """
        want = (not self._expanded) if expand is None else bool(expand)
        self._expanded = want
        if self._window:
            if want:
                self._window.show()
                self._window.restore()
            else:
                self._window.hide()
        return {"expanded": self._expanded}

    def quit(self):
        if self._window:
            self._window.destroy()
        return {"ok": True}

    def is_desktop(self):
        return True


def _launch_orb():
    """
    The orb is its own process.

    It has to be: WebView2 will not render a non-rectangular window, so the
    circle is a Win32 layered window painted with per-pixel alpha (orb.py).
    Separate process keeps its message loop away from pywebview's.
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return subprocess.Popen([sys.executable, os.path.join(here, "orb.py"), BASE],
                                cwd=here)
    except Exception as e:
        print(f"Warning: could not start the orb window: {e}")
        return None


def main():
    threading.Thread(target=_serve, daemon=True).start()
    if not _wait_for_server():
        sys.exit("Flask did not start in time.")

    orb_proc = _launch_orb()

    bridge = Bridge()
    window = webview.create_window(
        WINDOW_TITLE,
        f"{BASE}/?shell=desktop",
        js_api=bridge,
        width=PANEL_W,
        height=PANEL_H,
        hidden=True,             # the orb is what shows; click it to open this
        frameless=True,
        easy_drag=False,         # drag by the title bar, not the whole page
        on_top=True,
        background_color="#0f172a",
    )
    bridge._attach(window)

    def follow_the_orb():
        # The orb is a separate process; it reports clicks to Flask, which runs
        # in THIS process, so read its state directly. Doing this in Python
        # rather than by polling from the page matters: a hidden page's timers
        # get throttled, and the orb would take up to a minute to respond.
        last = flask_app._shell["toggle"]
        while True:
            time.sleep(0.15)
            st = flask_app._shell
            if st.get("quit"):
                window.destroy()
                return
            if st["toggle"] != last:
                last = st["toggle"]
                bridge.toggle()

    os.makedirs(PROFILE_DIR, exist_ok=True)
    # The panel spends most of its life hidden, but the wake word and the voice
    # session still run in it. Stop Chromium from throttling a hidden page.
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--disable-background-timer-throttling --disable-renderer-backgrounding "
        "--disable-backgrounding-occluded-windows "
        "--autoplay-policy=no-user-gesture-required")
    try:
        webview.start(follow_the_orb, private_mode=False, storage_path=PROFILE_DIR,
                      debug=bool(os.getenv("BENA_DEBUG")))
    finally:
        if orb_proc and orb_proc.poll() is None:
            orb_proc.terminate()
        # Leave nothing behind. An open microphone stream (the wake word) or
        # the .NET runtime behind the window can stop the interpreter from
        # finishing, which left a windowless bena process holding the port.
        try:
            listener = flask_app._wake.get("listener")
            if listener:
                listener.stop()
        except Exception:
            pass
        try:
            flask_app._scheduler.release()    # os._exit skips atexit
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
