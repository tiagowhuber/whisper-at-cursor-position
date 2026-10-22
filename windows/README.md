# whisper-at-cursor-position — Windows

Press **F2**, speak, press **F2** again. The transcript is typed wherever your cursor is.
Runs Whisper on your GPU through [whisper.cpp](https://github.com/ggml-org/whisper.cpp) + Vulkan, so AMD, Nvidia and Intel cards all work. No compiler or GPU? Use [Groq's hosted Whisper](#no-compiler-use-groq) instead: `install.ps1 -Groq`.

## How it works

- `whisper_tray.py` — tray app. Starts `whisper-server.exe` with the model pre-loaded in VRAM, listens for the hotkey, records the microphone and types the result via `SendInput`.
- `server.py` — starts/stops `whisper-server.exe` (localhost only). It's tied to a Job Object, so it never outlives the tray app, even if the app crashes.
- `groq_backend.py` — the cloud alternative: uploads the recording to Groq's Whisper API and returns the text. Used when `GROQ_API_KEY` is set.
- `overlay.py` — the floating pill at the bottom of the screen: **● Recording 0:04**, **Transcribing…**. It's click-through and never takes focus.
- `typer.py` — types Unicode text directly (accents, ¿, ñ, emoji) regardless of keyboard layout.
- `hotkeys.py` — the F2 / Esc hotkeys through Windows' `RegisterHotKey`. No keyboard hook, so nothing for Windows to drop after a sleep/resume cycle.

## Requirements

- Windows 10/11, Python 3.9+
- A GPU with a Vulkan driver (the regular AMD/Nvidia/Intel driver includes it)
- To build whisper.cpp:
  ```powershell
  winget install Kitware.CMake KhronosGroup.VulkanSDK
  winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --wait"
  ```

## Installation

```powershell
cd windows
.\build_whisper_vulkan.ps1     # builds bin\whisper-server.exe (a few minutes)
.\install.ps1                  # venv, downloads the model (~3 GB), adds a Startup shortcut
```

`install.ps1` options: `-Model large-v3-turbo` (or `medium`, `small`, …) and `-NoStartup`.

No GPU? Instead of building, unzip `whisper-bin-x64.zip` from the [whisper.cpp releases](https://github.com/ggml-org/whisper.cpp/releases) into `windows\bin\`. The app itself doesn't change.

## No compiler? Use Groq

Transcribe in the cloud instead of building whisper.cpp. Nothing to compile, no 3 GB model.

1. Get an API key at https://console.groq.com/keys (the free tier is plenty for dictation).
2. From `windows\`, run `.\install.ps1 -Groq`. It creates the venv, asks for the key once and adds the Startup shortcut.

The key is stored as the user environment variable `GROQ_API_KEY`, and `WHISPER_BACKEND` is set to `groq`. F2, Esc and the overlay work exactly the same.

What changes:
- **Privacy**: each recording is sent over TLS to Groq and transcribed there. Nothing is kept locally, except that a failed transcription leaves the audio at `%LOCALAPPDATA%\whisper-at-cursor\last-failed.wav` so the dictation isn't lost.
- **Length**: the free tier accepts up to 25 MB per request, about 12 minutes of dictation. The app stops recording by itself at that point and transcribes what it has.
- **Quota**: every request counts as at least 10 seconds of audio against the Groq limits.
- Set `GROQ_WHISPER_MODEL=whisper-large-v3` for the more accurate (slower) model.

## Usage

| Key | Effect |
|-----|--------|
| `F2` | Start recording / stop and transcribe |
| `Esc` | Cancel the current recording (only captured while recording) |

The app lives in the notification area. New icons land in the overflow (**^**). Drag it onto the taskbar if you want it always visible. The icon is gray while loading, white when ready, red while recording and amber while transcribing. Right-click → **Quit** to exit, or **Open log**.

Start it manually (no console window):
```powershell
.\venv\Scripts\pythonw.exe whisper_tray.py
```
Or with a console to watch the log live:
```powershell
.\venv\Scripts\python.exe whisper_tray.py
```

Log: `%LOCALAPPDATA%\whisper-at-cursor\whisper.log`

## Configuration

Set these as user environment variables (System → Advanced → Environment Variables), then restart the app:

| Variable | Default | |
|----------|---------|-|
| `WHISPER_BACKEND` | `auto` | `auto` = Groq if `GROQ_API_KEY` is set, else local. `local` / `groq` force one |
| `WHISPER_MODEL` | `large-v3` | Local only. Must match a `models\ggml-<name>.bin` file |
| `WHISPER_LANG` | `auto` | `auto`, or force one: `es`, `en`, … (Groq needs the 2-letter ISO-639-1 code) |
| `WHISPER_HOTKEY` | `f2` | Optional `ctrl` / `shift` / `alt` / `win` modifiers plus one key: `f1`–`f24`, a letter or digit, `space`, `insert`, … e.g. `ctrl+shift+space` |
| `WHISPER_PORT` | `8178` | Local only. Port for whisper-server |
| `GROQ_API_KEY` | – | Groq only. Set by `install.ps1 -Groq` |
| `GROQ_WHISPER_MODEL` | `whisper-large-v3-turbo` | Groq only. Or `whisper-large-v3` (more accurate, slower) |

## Limitations

- While an app running **as administrator** is focused, Windows blocks both the hotkey and the typing from a non-admin process.
- The hotkey is swallowed, so F2 no longer reaches other apps (e.g. rename in Explorer, edit cell in Excel). Pick another key with `WHISPER_HOTKEY` if you need F2.
- If another program already owns the hotkey, the app says so at start and exits. Pick another key with `WHISPER_HOTKEY`.
- A few programs (some games, remote desktop clients) ignore synthesized Unicode input.
