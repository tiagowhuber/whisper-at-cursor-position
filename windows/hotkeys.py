"""
hotkeys.py — Global hotkeys through RegisterHotKey, with no keyboard hook.

Windows silently removes a low-level keyboard hook whose procedure stalls (it happens around
modern standby / resume), and the `keyboard` library never notices: the app keeps running but
no longer sees any key until it is restarted. RegisterHotKey is handled by the OS instead:
no hook procedure, no timeout, no stale modifier state, and the key is swallowed so the
focused window never receives it.

The cancel key (Esc) is registered only while recording, so Esc keeps working normally in
every other application the rest of the time.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import threading

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT)]


_user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
_user32.RegisterHotKey.restype = wt.BOOL
_user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wt.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wt.HWND, wt.UINT, wt.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.PeekMessageW.argtypes = [ctypes.POINTER(_MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
_user32.PeekMessageW.restype = wt.BOOL
_user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.PostThreadMessageW.restype = wt.BOOL
_user32.VkKeyScanW.argtypes = [wt.WCHAR]
_user32.VkKeyScanW.restype = ctypes.c_short
_kernel32.GetCurrentThreadId.restype = wt.DWORD

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_APP = 0x8000
_WM_REGISTER_CANCEL = WM_APP + 1
_WM_UNREGISTER_CANCEL = WM_APP + 2

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
VK_ESCAPE = 0x1B
_ID_TOGGLE, _ID_CANCEL = 1, 2

_MOD_NAMES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN,
}
_VK_NAMES = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91, "printscreen": 0x2C,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    **{f"numpad{n}": 0x60 + n for n in range(10)},
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'f2', 'ctrl+shift+space', 'alt+q' → (modifier flags, virtual-key code)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey {spec!r}")
    mods = 0
    for part in parts[:-1]:
        if part not in _MOD_NAMES:
            raise ValueError(f"unknown modifier {part!r} in hotkey {spec!r}")
        mods |= _MOD_NAMES[part]
    key = parts[-1]
    if key in _VK_NAMES:
        vk = _VK_NAMES[key]
    elif len(key) == 1:
        scan = _user32.VkKeyScanW(key)
        if scan == -1:
            raise ValueError(f"no key for {key!r} on this keyboard layout")
        vk = scan & 0xFF
    else:
        raise ValueError(f"unknown key {key!r} in hotkey {spec!r}")
    return mods, vk


class Hotkeys:
    """
    Owns a thread with a Windows message loop. The toggle hotkey posts ("toggle",) to *events*,
    the cancel key posts ("cancel",). Registration errors surface from start().
    """

    def __init__(self, spec: str, events):
        self.spec = spec
        self.mods, self.vk = parse_hotkey(spec)
        self.events = events
        self.thread_id = None
        self.error = None
        self._ready = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="hotkeys", daemon=True).start()
        if not self._ready.wait(5):
            raise RuntimeError("hotkey thread did not start")
        if self.error:
            raise RuntimeError(self.error)

    def stop(self) -> None:
        if self.thread_id:
            _user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    def set_cancel_key(self, enabled: bool) -> None:
        """Register Esc while recording, release it afterwards."""
        if self.thread_id:
            msg = _WM_REGISTER_CANCEL if enabled else _WM_UNREGISTER_CANCEL
            _user32.PostThreadMessageW(self.thread_id, msg, 0, 0)

    def _loop(self) -> None:
        self.thread_id = _kernel32.GetCurrentThreadId()
        msg = _MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # PM_NOREMOVE: creates the message queue
        if not _user32.RegisterHotKey(None, _ID_TOGGLE, self.mods | MOD_NOREPEAT, self.vk):
            err = ctypes.get_last_error()
            self.error = (f"Could not register the hotkey {self.spec!r} (Windows error {err}).\n"
                          "Another program may already use it. Set WHISPER_HOTKEY to a different key.")
            self._ready.set()
            return
        log.info("Hotkey %r registered (mods=0x%x vk=0x%x).", self.spec, self.mods, self.vk)
        self._ready.set()

        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                if msg.wParam == _ID_TOGGLE:
                    self.events.put(("toggle",))
                elif msg.wParam == _ID_CANCEL:
                    self.events.put(("cancel",))
            elif msg.message == _WM_REGISTER_CANCEL:
                if not _user32.RegisterHotKey(None, _ID_CANCEL, MOD_NOREPEAT, VK_ESCAPE):
                    log.warning("Could not register Esc as cancel key (error %d).", ctypes.get_last_error())
            elif msg.message == _WM_UNREGISTER_CANCEL:
                _user32.UnregisterHotKey(None, _ID_CANCEL)

        _user32.UnregisterHotKey(None, _ID_CANCEL)
        _user32.UnregisterHotKey(None, _ID_TOGGLE)
