"""
The floating orb — a genuinely circular, always-on-top window.

Why this is its own process instead of the WebView2 window: WebView2 will not
render a non-rectangular window. Five approaches were measured against it
(transparent=True, SetWindowRgn, a retry burst, colour-keying, and a region
watchdog) and every one left an opaque rectangle behind the circle, because the
control composites its own backdrop underneath the page.

This window sidesteps the browser entirely. It is a Win32 layered window fed by
UpdateLayeredWindow with a 32-bit premultiplied ARGB bitmap, which is the only
Windows API that gives true PER-PIXEL alpha. The circle's antialiased edge
blends against whatever is behind it, there are no corners, and there is no
colour-key fringe. Hit-testing follows the alpha too, so clicks in the
transparent area fall through to the window underneath.

Run by desktop.py; it talks to Flask over HTTP for state.
"""
import ctypes
import ctypes.wintypes as wt
import os
import json
import math
import sys
import urllib.request

from PIL import Image, ImageDraw

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# 64-bit Windows: HWND/WPARAM/LPARAM/LRESULT are pointer-sized. Without explicit
# signatures ctypes truncates them and DefWindowProcW raises
# "OverflowError: int too long to convert" on the first mouse message.
LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint,
                                 wt.WPARAM, wt.LPARAM)

user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wt.HWND
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]

# GDI handles are pointer-sized too.
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.c_void_p, ctypes.c_uint]
gdi32.CreateDIBSection.restype = wt.HBITMAP
user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.c_void_p, ctypes.c_uint]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.SetCapture.argtypes = [wt.HWND]
user32.RegisterClassW.restype = ctypes.c_ushort

try:                                  # crisp on high-DPI displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

DEBUG = bool(os.getenv('ORB_DEBUG'))
SIZE = 104
ULW_ALPHA = 0x02
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080      # keep it out of the alt-tab list
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
HWND_TOPMOST = -1

WM_DESTROY, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 0x0002, 0x0201, 0x0202, 0x0200
WM_RBUTTONUP, WM_TIMER, WM_NCHITTEST = 0x0205, 0x0113, 0x0084

PALETTES = {
    # inner, outer  — the same purple/pink the web UI uses
    "idle":  ((139, 92, 246), (236, 72, 153)),
    "live":  ((16, 185, 129), (5, 150, 105)),
    "woke":  ((250, 204, 21), (245, 158, 11)),
}

BRAIN = (
    "M50 22c-5 0-9 3-10 7-5 0-9 4-9 9 0 2 .6 4 1.7 5.5C30 45 29 48 29 51c0 5 4 9 9 9"
    " .8 3 3.6 6 8 6 2 0 4-1 4-3V22z"
)


def _render(state, scale=1.0):
    """The orb as an RGBA image: circle only, everything else fully transparent."""
    s = int(SIZE * scale)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    inner, outer = PALETTES.get(state, PALETTES["idle"])
    r = s / 2.0
    # Diagonal gradient, drawn per-pixel inside the circle with a soft edge so
    # the boundary antialiases into transparency instead of stair-stepping.
    px = img.load()
    for y in range(s):
        for x in range(s):
            dx, dy = x - r + 0.5, y - r + 0.5
            dist = math.hypot(dx, dy)
            if dist > r:
                continue
            t = max(0.0, min(1.0, ((dx + dy) / (2 * r) + 0.5)))
            col = tuple(int(inner[i] + (outer[i] - inner[i]) * t) for i in range(3))
            edge = r - dist
            alpha = 255 if edge >= 1.0 else int(255 * max(0.0, edge))
            px[x, y] = col + (alpha,)

    # Brain glyph: two rounded lobes with a clean gap down the middle.
    # Drawn at 4x and downsampled, because ImageDraw does not antialias.
    gr = s * 0.20
    cx = cy = s / 2
    F = 4
    glyph = Image.new("RGBA", (s * F, s * F), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glyph)
    # Two full lobes with a hairline gap — the same silhouette as the web icon.
    gap = gr * 0.07
    for side in (-1, 1):
        x0 = (cx + (gap if side > 0 else -gr * 1.0)) * F
        x1 = (cx + (gr * 1.0 if side > 0 else -gap)) * F
        gd.ellipse([x0, (cy - gr) * F, x1, (cy + gr) * F], fill=(255, 255, 255, 255))
    glyph = glyph.resize((s, s), Image.LANCZOS)
    img.alpha_composite(glyph)
    return img


def _to_premultiplied_bgra(img):
    """UpdateLayeredWindow wants BGRA with colour already multiplied by alpha."""
    out = bytearray(img.width * img.height * 4)
    i = 0
    for r, g, b, a in img.getdata():
        out[i] = b * a // 255
        out[i + 1] = g * a // 255
        out[i + 2] = r * a // 255
        out[i + 3] = a
        i += 4
    return bytes(out)


class Orb:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self.state = "idle"
        self.hwnd = None
        self.dragging = False
        self.drag_off = (0, 0)
        self._cache = {}

    # -- painting -------------------------------------------------------

    def paint(self):
        img = _render(self.state)
        w, h = img.size
        bits = self._cache.get(self.state)
        if bits is None:
            bits = _to_premultiplied_bgra(img)
            self._cache[self.state] = bits

        screen_dc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                        ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h          # negative = top-down
        bi.biPlanes, bi.biBitCount = 1, 32
        bi.biCompression = 0

        ppv = ctypes.c_void_p()
        gdi32.CreateDIBSection.restype = wt.HBITMAP
        bmp = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bi), 0,
                                     ctypes.byref(ppv), None, 0)
        ctypes.memmove(ppv, bits, len(bits))
        old = gdi32.SelectObject(mem_dc, bmp)

        rect = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        size = wt.SIZE(w, h)
        src = wt.POINT(0, 0)
        pos = wt.POINT(rect.left, rect.top)

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                        ("SourceConstantAlpha", ctypes.c_byte),
                        ("AlphaFormat", ctypes.c_byte)]

        blend = BLENDFUNCTION(0, 0, 255, 1)      # AC_SRC_OVER, AC_SRC_ALPHA
        res = user32.UpdateLayeredWindow(self.hwnd, screen_dc, ctypes.byref(pos),
                                         ctypes.byref(size), mem_dc, ctypes.byref(src),
                                         0, ctypes.byref(blend), ULW_ALPHA)
        if not res and DEBUG:
            print(f"[orb] UpdateLayeredWindow failed err={ctypes.get_last_error()} "
                  f"bmp={bmp} bits={len(bits)} size={w}x{h}", flush=True)
        elif DEBUG:
            print(f"[orb] painted {w}x{h} bmp={bmp} nonzero_alpha="
                  f"{sum(1 for i in range(3, len(bits), 4) if bits[i])}", flush=True)

        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.paint()

    # -- server ---------------------------------------------------------

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def _post(self, path, payload=None):
        try:
            data = json.dumps(payload or {}).encode()
            req = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def poll(self):
        d = self._get("/api/shell/state")
        if d:
            self.set_state(d.get("orb", "idle"))

    # -- window ---------------------------------------------------------

    def create(self):
        WNDPROC = WNDPROCTYPE

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        self._wndproc = WNDPROC(self._on_message)
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "BenaOrb"
        cls.hCursor = user32.LoadCursorW(0, ctypes.c_wchar_p(32512))
        user32.RegisterClassW(ctypes.byref(cls))

        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            "BenaOrb", "SmartText Orb", WS_POPUP,
            sw - SIZE - 40, sh - SIZE - 120, SIZE, SIZE,
            0, 0, hinst, None)

        self.paint()
        user32.ShowWindow(self.hwnd, 5)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
        user32.SetTimer(self.hwnd, 1, 400, None)

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            self.poll()
            return 0
        if msg == WM_LBUTTONDOWN:
            self.dragging = True
            self.moved = False
            pt = wt.POINT(); user32.GetCursorPos(ctypes.byref(pt))
            r = wt.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(r))
            self.drag_off = (pt.x - r.left, pt.y - r.top)
            user32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self.dragging:
            pt = wt.POINT(); user32.GetCursorPos(ctypes.byref(pt))
            user32.SetWindowPos(hwnd, HWND_TOPMOST,
                                pt.x - self.drag_off[0], pt.y - self.drag_off[1],
                                0, 0, SWP_NOSIZE)
            self.moved = True
            return 0
        if msg == WM_LBUTTONUP and self.dragging:
            self.dragging = False
            user32.ReleaseCapture()
            if not getattr(self, "moved", False):
                self._post("/api/shell/toggle")    # a click, not a drag
            return 0
        if msg == WM_RBUTTONUP:
            self._post("/api/shell/quit")
            user32.PostQuitMessage(0)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        self.create()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
    Orb(base).run()
