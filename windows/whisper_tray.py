#!/usr/bin/env python3
"""
Whisper Voice Input — Windows tray app
- Transcribes locally with whisper-server.exe (whisper.cpp, Vulkan) or in the cloud via Groq
- F2 toggles recording, Esc cancels while recording (RegisterHotKey, no keyboard hook)
- Transcribes and types the result at the current cursor position
- Floating overlay pill shows recording / transcribing state
- Tray icon (notification area) with Quit
"""

import ctypes
import io
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import wave
from pathlib import Path

# Must happen before any window is created, or Tk renders blurry on scaled displays
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
except (AttributeError, OSError):
    pass

import numpy as np
import pystray
import sounddevice as sd
from PIL import Image, ImageDraw

from hotkeys import Hotkeys
from overlay import Overlay
from server import WhisperServer
from typer import type_text

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent
WHISPER_BACKEND = os.environ.get("WHISPER_BACKEND", "auto").strip().lower()   # auto / local / groq
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")   # tiny / base / small / medium / large-v3 / large-v3-turbo
WHISPER_LANG  = os.environ.get("WHISPER_LANG", "auto")        # auto / en / es / ...
HOTKEY        = os.environ.get("WHISPER_HOTKEY", "f2")
PORT          = int(os.environ.get("WHISPER_PORT", "8178"))
SERVER_EXE    = BASE_DIR / "bin" / "whisper-server.exe"
MODEL_FILE    = BASE_DIR / "models" / f"ggml-{WHISPER_MODEL}.bin"
LOG_DIR       = Path(os.environ.get("LOCALAPPDATA", BASE_DIR)) / "whisper-at-cursor"
LOG_FILE      = LOG_DIR / "whisper.log"
SAMPLE_RATE   = 16000
MIN_SECONDS   = 0.3     # shorter clips are discarded
APP_NAME      = "Whisper at Cursor"

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:          # None under pythonw.exe
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)


def message_box(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)  # MB_ICONERROR


def to_wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\whisper-at-cursor")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ── Tray icon ──────────────────────────────────────────────────────────────────

ICON_COLORS = {
    "loading":      "#8a8a8a",
    "idle":         "#e6e6e6",
    "recording":    "#ff4545",
    "transcribing": "#ffb300",
}

STATUS_TEXT = {
    "loading":      "Loading model…",
    "idle":         f"Ready — press {HOTKEY.upper()}",
    "recording":    "Recording…",
    "transcribing": "Transcribing…",
}


def make_icon(color: str) -> Image.Image:
    """Microphone glyph, drawn at 4x and downsampled for smooth edges."""
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = 22
    d.rounded_rectangle((88, 16, 168, 156), radius=40, fill=color)              # capsule
    d.arc((52, 70, 204, 196), start=0, end=180, fill=color, width=w)           # cradle
    d.rectangle((128 - w // 2, 190, 128 + w // 2, 226), fill=color)            # stem
    d.rounded_rectangle((76, 220, 180, 242), radius=10, fill=color)            # base
    return img.resize((64, 64), Image.LANCZOS)


# ── Backend ────────────────────────────────────────────────────────────────────

def make_backend():
    """WHISPER_BACKEND=auto|local|groq. auto → Groq if GROQ_API_KEY is set, else local."""
    choice = WHISPER_BACKEND
    if choice == "auto":
        choice = "groq" if os.environ.get("GROQ_API_KEY", "").strip() else "local"
        log.info("WHISPER_BACKEND=auto → %s", choice)
    if choice == "local":
        for path, what in ((SERVER_EXE, "whisper-server.exe"), (MODEL_FILE, "Model file")):
            if not path.exists():
                message_box(f"{what} not found:\n{path}\n\n"
                            "Run install.ps1 first, or install.ps1 -Groq to transcribe in the cloud.")
                sys.exit(1)
        return WhisperServer(str(SERVER_EXE), str(MODEL_FILE), WHISPER_LANG, PORT)
    if choice == "groq":
        from groq_backend import GroqBackend, GROQ_MODEL, resolve_groq_key   # lazy: the local path never imports openai
        key = resolve_groq_key()
        if not key:
            message_box("GROQ_API_KEY is not set.\n\n"
                        "Run install.ps1 -Groq (get a key at https://console.groq.com/keys),\n"
                        "or set WHISPER_BACKEND=local to use whisper-server.exe.")
            sys.exit(1)
        return GroqBackend(WHISPER_LANG, GROQ_MODEL, key)
    message_box(f"WHISPER_BACKEND must be auto, local or groq (got {WHISPER_BACKEND!r}).")
    sys.exit(1)


# ── App ────────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, backend):
        self.backend = backend
        self.title = f"{APP_NAME} ({backend.name})"
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.withdraw()
        self.overlay = Overlay(self.root)
        self.events: "queue.Queue[tuple]" = queue.Queue()

        self.state = "loading"
        self.stream = None
        self.frames = []
        self.last_toggle = 0.0
        self._limit_job = None
        self.hotkeys = Hotkeys(HOTKEY, self.events)

        self.icons = {k: make_icon(c) for k, c in ICON_COLORS.items()}
        self.tray = pystray.Icon(
            "whisper-at-cursor",
            icon=self.icons["loading"],
            title=f"{self.title} — {STATUS_TEXT['loading']}",
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: STATUS_TEXT[self.state], None, enabled=False),
                pystray.MenuItem("Open log", lambda: os.startfile(LOG_FILE)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: self.events.put(("quit",))),
            ),
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            self.hotkeys.start()
        except RuntimeError as exc:
            log.error("%s", exc)
            message_box(str(exc))
            sys.exit(1)
        threading.Thread(target=self.tray.run, daemon=True).start()
        threading.Thread(target=self._start_server, daemon=True).start()
        self.overlay.show("loading")
        self.root.after(30, self._pump)
        self.root.mainloop()

    def _start_server(self) -> None:
        try:
            t0 = time.monotonic()
            self.backend.start()
            self.backend.wait_ready()
            if isinstance(self.backend, WhisperServer):
                # The first inference compiles GPU shaders; do it now rather than on the first dictation
                self.backend.transcribe(to_wav(np.zeros((SAMPLE_RATE, 1), np.int16)))
            log.info("Backend %s ready in %.1fs.", self.backend.name, time.monotonic() - t0)
            self.events.put(("ready",))
        except Exception as exc:
            log.exception("Backend failed to start")
            self.events.put(("fatal", f"{self.backend.name} backend failed to start:\n{exc}\n\nSee {LOG_FILE}"))

    def _quit(self) -> None:
        log.info("Quitting.")
        self._close_stream()
        self.hotkeys.stop()
        self.backend.stop()
        self.tray.stop()
        self.root.destroy()

    # ── Event loop (Tk main thread) ───────────────────────────────────────────

    def _pump(self) -> None:
        try:
            while self.state is not None:
                event, *args = self.events.get_nowait()
                getattr(self, f"_on_{event}")(*args)
        except queue.Empty:
            pass
        if self.state is not None:
            self.root.after(30, self._pump)

    def _set_state(self, state: str) -> None:
        self.state = state
        self.tray.icon = self.icons[state]
        self.tray.title = f"{self.title} — {STATUS_TEXT[state]}"
        self.tray.update_menu()

    def _on_ready(self) -> None:
        self._set_state("idle")
        self.overlay.show("info", f"Whisper ready ({self.backend.name})", hide_after_ms=1500)

    def _on_fatal(self, msg: str) -> None:
        self.overlay.hide()
        message_box(msg)
        self.state = None
        self._quit()

    def _on_quit(self) -> None:
        self.state = None
        self._quit()

    def _on_toggle(self) -> None:
        now = time.monotonic()
        if now - self.last_toggle < 0.3:       # ignore key auto-repeat
            return
        self.last_toggle = now
        if self.state == "idle":
            self._start_recording()
        elif self.state == "recording":
            self._stop_recording()
        elif self.state == "loading":
            self.overlay.show("loading", "Still loading model…")

    def _on_cancel(self) -> None:
        if self.state != "recording":
            return
        self._close_stream()
        log.info("Recording cancelled.")
        self._set_state("idle")
        self.overlay.show("info", "Cancelled", hide_after_ms=1000)

    def _on_limit(self) -> None:
        """root.after callback (Tk thread): the recording cap of the backend was reached."""
        self._limit_job = None
        if self.state != "recording":
            return
        log.info("Recording reached the %ds cap of the %s backend; stopping.",
                 self.backend.max_seconds, self.backend.name)
        self._stop_recording()
        if self.state == "transcribing":
            self.overlay.show("transcribing", "Max length reached — transcribing…")

    def _on_done(self, text: str) -> None:
        self._set_state("idle")
        if text:
            self.overlay.hide()
        else:
            self.overlay.show("info", "No speech detected", hide_after_ms=1500)

    def _on_error(self, msg: str) -> None:
        self._set_state("idle")
        self.overlay.show("error", msg, hide_after_ms=4000)

    # ── Recording / Transcription ─────────────────────────────────────────────

    def _start_recording(self) -> None:
        self.frames = []
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                callback=lambda data, *_: self.frames.append(data.copy()),
            )
            self.stream.start()
        except Exception as exc:
            log.exception("Could not open microphone")
            self.stream = None
            self.overlay.show("error", f"Microphone error: {exc}", hide_after_ms=4000)
            return
        self.hotkeys.set_cancel_key(True)
        if self.backend.max_seconds:
            # Cloud uploads are capped; stop by ourselves at the limit so nothing is lost
            self._limit_job = self.root.after(int(self.backend.max_seconds * 1000), self._on_limit)
        self._set_state("recording")
        self.overlay.show("recording")
        log.info("Recording started.")

    def _close_stream(self) -> None:
        if self._limit_job is not None:
            self.root.after_cancel(self._limit_job)
            self._limit_job = None
        self.hotkeys.set_cancel_key(False)
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def _stop_recording(self) -> None:
        self._close_stream()
        audio = np.concatenate(self.frames) if self.frames else np.zeros((0, 1), np.int16)
        self.frames = []
        seconds = len(audio) / SAMPLE_RATE
        log.info("Recording stopped (%.1fs).", seconds)
        if seconds < MIN_SECONDS:
            self._set_state("idle")
            self.overlay.show("info", "Too short", hide_after_ms=1000)
            return
        self._set_state("transcribing")
        self.overlay.show("transcribing")
        threading.Thread(target=self._transcribe_and_type, args=(audio,), daemon=True).start()

    def _transcribe_and_type(self, audio: np.ndarray) -> None:
        wav = to_wav(audio)
        try:
            t0 = time.monotonic()
            text = self.backend.transcribe(wav)
            log.info("Transcribed in %.2fs: %r", time.monotonic() - t0, text)

            if text:
                # Brief pause so the window focus is settled after the hotkey press
                time.sleep(0.15)
                type_text(text)
            self.events.put(("done", text))
        except Exception as exc:
            log.exception("Transcription error")
            # Keep the audio so a transient failure (network, rate limit) does not lose the dictation
            try:
                (LOG_DIR / "last-failed.wav").write_bytes(wav)
                log.info("Audio kept at %s", LOG_DIR / "last-failed.wav")
            except OSError:
                pass
            self.events.put(("error", f"Error: {exc}"))


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if not single_instance():
        log.info("Another instance is already running; exiting.")
        return
    backend = make_backend()        # shows a message box and exits when nothing is set up
    log.info("Starting %s (backend=%s, lang=%s, hotkey=%s)", APP_NAME, backend.name, WHISPER_LANG, HOTKEY)
    App(backend).run()


if __name__ == "__main__":
    main()
