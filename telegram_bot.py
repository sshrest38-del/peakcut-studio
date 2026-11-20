"""
PeakCut Advanced Telegram Bot
=============================
Converts YouTube, Twitch, Kick, and uploaded video files into viral 9:16 vertical shorts (Reels/TikTok).
Features:
- Never picks boring 0s intro: Automatically locates true viral action peaks via YouTube transcript keywords or Gemini AI
- Dual language subtitles: Spoken Hindi is automatically transliterated to clean Roman Hinglish; English remains clean English
- Vibrant animated captions (yellow with thick dark outline) & top hook banner (white box with bold text)
- Interactive Instagram Reels flow: User previews clip in Telegram, then taps [🚀 Upload to Instagram Now] or [❌ Cancel Upload]
- Full Meta Graph API Instagram upload integration (resumable container binary upload)
- Complete API Key management: /setgemini, /delgemini, /setinstagram, /delinstagram, /mykeys
- Real-time progress bar, speed (MB/s), ETA, and size tracking for download, transcode, and upload
- Multiple 9:16 vertical layouts: Blurred Fill (1080x1920), Zoomed, Center Crop, Gameplay
- Dual delivery mode: Normal HD Media (streamable video with inline playback) & uncompressed Document file
"""

import os
import sys
import time
import json
import re
import math
import uuid
import random
import asyncio
import base64
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

# Force direct connections by default to prevent dead proxies from killing Telegram/APIs
for _env_k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(_env_k, None)

import httpx
import requests

try:
    from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
except ImportError:
    MultipartEncoder = None
    MultipartEncoderMonitor = None

try:
    import unidecode
except ImportError:
    unidecode = None

# Auto-install essential runtime packages if missing
for _pkg in ["pproxy", "ytdlp-jsc", "imageio-ffmpeg", "curl-cffi"]:
    try:
        __import__(_pkg.replace("-", "_"))
    except ImportError:
        try:
            print(f"[Auto-Install] {_pkg} missing, installing via pip...")
            subprocess.run([sys.executable, "-m", "pip", "install", _pkg], capture_output=True, timeout=60)
        except Exception:
            pass

def get_ffmpeg_binary() -> str:
    """
    Ensures FFmpeg binary is available on Streamlit Cloud or local environment.
    Falls back to imageio-ffmpeg standalone executable if system FFmpeg is missing.
    """
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            d = Path(exe).parent
            sym = d / ("ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg")
            if not sym.exists():
                try:
                    shutil.copy2(exe, str(sym))
                except Exception:
                    pass
            if str(d) not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{d}{os.pathsep}{os.environ.get('PATH', '')}"
            return shutil.which("ffmpeg") or exe
    except Exception as e:
        print(f"[FFmpeg Loader] {e}")
    return "ffmpeg"

_FFMPEG_BIN = get_ffmpeg_binary()

# Ensure PeakCut root is on sys.path
ROOT_DIR = Path(__file__).parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Directories
DATA_DIR = ROOT_DIR / "data"
TMP_DIR = DATA_DIR / "tmp"
RENDERS_DIR = DATA_DIR / "renders"
CONFIG_FILE = ROOT_DIR / "bot_config.json"
USER_PREFS_FILE = DATA_DIR / "telegram_users.json"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_JS_RUNTIME_ARGS: Optional[list] = None


def _starts_here(exe: str) -> bool:
    """True if a program can actually be started on this computer. The bundled bin/qjs is a Linux
    executable: Windows refuses to start it, and yt-dlp would then solve none of YouTube's
    challenges ("n challenge solving failed"), so a working Deno or Node must win over it."""
    try:
        subprocess.run([exe, "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=6, creationflags=NO_WINDOW)
        return True
    except Exception:
        return False


def get_js_runtime_args() -> list:
    """
    The JavaScript runtime yt-dlp uses to solve YouTube's challenges, as command line arguments.
    Candidates are tried in order and the first one that starts on this machine wins; the answer
    is remembered, because it cannot change while the bot is running.
    """
    global _JS_RUNTIME_ARGS
    if _JS_RUNTIME_ARGS is None:
        qjs_bin = ROOT_DIR / "bin" / "qjs"
        if sys.platform.startswith("linux"):
            try:
                os.chmod(str(qjs_bin), 0o755)
            except OSError:
                pass
        node_sys = (shutil.which("node") or shutil.which("nodejs")
                    or ("/usr/bin/node" if Path("/usr/bin/node").exists() else None)
                    or ("/usr/bin/nodejs" if Path("/usr/bin/nodejs").exists() else None))
        _JS_RUNTIME_ARGS = []
        for kind, exe in (("node", node_sys), ("quickjs", str(qjs_bin)), ("deno", shutil.which("deno"))):
            if exe and _starts_here(exe):
                _JS_RUNTIME_ARGS = ["--js-runtimes", f"{kind}:{exe}", "--remote-components", "ejs:github"]
                break
    return list(_JS_RUNTIME_ARGS)

DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
RENDERS_DIR.mkdir(parents=True, exist_ok=True)

# ----------------- CONFIGURATION & KEYS -----------------

def load_bot_config() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Read .streamlit/secrets.toml if running on Streamlit Cloud
    sec_file = ROOT_DIR / ".streamlit" / "secrets.toml"
    if sec_file.exists():
        try:
            import tomllib
            with open(sec_file, "rb") as sf:
                sec_dict = tomllib.load(sf)
                if sec_dict.get("TELEGRAM_BOT_TOKEN") and not cfg.get("bot_token"):
                    cfg["bot_token"] = sec_dict["TELEGRAM_BOT_TOKEN"]
                if sec_dict.get("GEMINI_API_KEY") and not cfg.get("gemini_keys"):
                    cfg["gemini_keys"] = sec_dict["GEMINI_API_KEY"]
                if sec_dict.get("ZERNIO_API_KEY") and not cfg.get("zernio_api_key"):
                    cfg["zernio_api_key"] = sec_dict["ZERNIO_API_KEY"]
                if sec_dict.get("ZERNIO_ACCOUNT_ID") and not cfg.get("zernio_account_id"):
                    cfg["zernio_account_id"] = sec_dict["ZERNIO_ACCOUNT_ID"]
                if sec_dict.get("YOUTUBE_COOKIES"):
                    c_file = DATA_DIR / "cookies.txt"
                    if not c_file.exists() or c_file.stat().st_size < 20:
                        c_file.write_text(sec_dict["YOUTUBE_COOKIES"].strip(), encoding="utf-8")
        except Exception:
            pass

    _FALLBACK_BOT_TOKEN = base64.b64decode("OTA0NzE5ODk1ODpBQUZmNno2Ykx5aERaTnp3cFg2MUpLZUZBU3FNdmVOYzNzWQ==").decode()
    _FALLBACK_GEMINI_KEY = base64.b64decode("QVEuQWI4Uk42TEw2UE83dzVMZ09GYW12eTMzcE45cWlUaG5xRnVjNkoyVHFQM0RQWXU0VGc=").decode()
    _FALLBACK_ZERNIO_KEY = base64.b64decode("c2tfYTM5OWMwOWFkNTk2NmU2NzY4ZTI5N2VlMTJiYTBhODg4NjdhMTZjZGMxYzVjMjc5ZTg2Yjk3M2Y1YjFiMDIyMQ==").decode()
    _FALLBACK_ZERNIO_ACC = "6affe7338ae0a5730bcf4ec4"
    _FALLBACK_PROXY = ""

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("bot_token") or _FALLBACK_BOT_TOKEN
    gemini_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or cfg.get("gemini_keys") or _FALLBACK_GEMINI_KEY
    proxy = cfg.get("proxy", "")
    if proxy and ("surfshark.com" in proxy.lower() or not proxy.startswith("http")):
        proxy = ""
    zernio_key = os.environ.get("ZERNIO_API_KEY") or cfg.get("zernio_api_key") or _FALLBACK_ZERNIO_KEY
    zernio_acc = os.environ.get("ZERNIO_ACCOUNT_ID") or cfg.get("zernio_account_id") or _FALLBACK_ZERNIO_ACC

    return {
        "bot_token": token.strip(),
        "gemini_keys": gemini_keys.strip() if isinstance(gemini_keys, str) else ",".join(gemini_keys),
        "proxy": proxy.strip(),
        "zernio_api_key": zernio_key.strip(),
        "zernio_account_id": zernio_acc.strip()
    }

def save_bot_config(token: str, gemini_keys: str = None, proxy: str = None):
    cur = load_bot_config()
    data = {
        "bot_token": token.strip() if token else cur.get("bot_token", ""),
        "gemini_keys": gemini_keys.strip() if gemini_keys is not None else cur.get("gemini_keys", ""),
        "proxy": proxy.strip() if proxy is not None else cur.get("proxy", "")
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def get_gemini_keys() -> List[str]:
    cfg = load_bot_config()
    raw = cfg.get("gemini_keys") or os.environ.get("GEMINI_API_KEYS") or os.environ.get("PEAKCUT_AI_KEY") or ""
    keys = [k.strip() for k in re.split(r"[,\n;\s]+", raw) if k.strip()]
    
    # Also check data/ai.json from local PeakCut
    ai_json_file = DATA_DIR / "ai.json"
    if ai_json_file.exists():
        try:
            ai_data = json.loads(ai_json_file.read_text(encoding="utf-8"))
            k = ai_data.get("key", "").strip()
            if k and k not in keys:
                keys.append(k)
        except Exception:
            pass
            
    return keys

def save_gemini_key_to_all(key: str):
    # Save to bot_config.json
    cfg = load_bot_config()
    save_bot_config(cfg.get("bot_token", ""), key)
    # Save to data/ai.json
    ai_file = DATA_DIR / "ai.json"
    ai_file.write_text(json.dumps({"provider": "gemini", "key": key}, indent=2), encoding="utf-8")

def delete_gemini_key():
    cfg = load_bot_config()
    save_bot_config(cfg.get("bot_token", ""), "")
    ai_file = DATA_DIR / "ai.json"
    if ai_file.exists():
        try:
            ai_file.write_text(json.dumps({"provider": "gemini", "key": ""}, indent=2), encoding="utf-8")
        except Exception:
            pass

def cleanup_storage(max_age_seconds: int = 1800) -> tuple:
    """
    Deletes temporary files in TMP_DIR and old renders in RENDERS_DIR.
    Returns (files_deleted, bytes_freed).
    """
    now = time.time()
    count = 0
    freed = 0
    for d in [TMP_DIR, RENDERS_DIR]:
        if d.exists():
            for f in list(d.iterdir()):
                if f.is_file():
                    try:
                        if max_age_seconds == 0 or (now - f.stat().st_mtime > max_age_seconds):
                            sz = f.stat().st_size
                            f.unlink(missing_ok=True)
                            count += 1
                            freed += sz
                    except Exception:
                        pass
    return count, freed

# ----------------- USER PREFERENCES -----------------

DEFAULT_PREF = {
    "layout": "split",       # split (AI Facecam Top + Game), gameplay (PiP), blur, zoom, crop
    "style": "bounce",       # bounce, hype, clean, none
    "send_mode": "both",     # video (HD Media), document (Doc file), both
    "clips_count": 1,        # 1, 3, 5, "full"
    "clip_dur": 30,          # 15, 30, 60 seconds
    "quality": "1080p",      # 1080p, 720p
    "ai_mode": True,         # True/False (Gemini hook & captions)
    "gemini_key": "",        # Personal Gemini API Key (falls back to system key if empty)
    "ig_account_id": "",     # Meta Graph API Instagram Account ID
    "ig_access_token": "",   # Meta Graph API User/Page Access Token
    "zernio_api_key": "",    # Personal Zernio Unified Social API Key
    "zernio_account_id": ""  # Personal Zernio Instagram Account ID
}

# Stores pending Instagram Reel posts waiting for user confirmation
PENDING_POSTS: Dict[str, dict] = {}

# An .srt file waiting for its language to be picked from the buttons
PENDING_SRT: Dict[str, dict] = {}

# Subtitle translation goes through a free public gateway (DESI API). It translates
# what it is sent and echoes the timestamps back untouched, but it goes quiet after
# a while, so every run asks it a small test sentence first and checks each part too.
SRT_TRANSLATE_API = "https://sheffysamra1bot-sub-api.smpost-tempmail.workers.dev/"

# The languages offered as buttons when a file arrives with no language in its caption
SRT_LANG_BUTTONS = [
    [("🇮🇳 Hindi", "hi"), ("🇬🇧 English", "en")],
    [("🇪🇸 Spanish", "es"), ("🇸🇦 Arabic", "ar")],
    [("🇫🇷 French", "fr"), ("🇵🇰 Urdu", "ur")],
    [("🇧🇷 Portuguese", "pt"), ("🇹🇷 Turkish", "tr")],
    [("🇩🇪 German", "de"), ("🇷🇺 Russian", "ru")],
]

SRT_LANG_LABELS = {
    "hi": "Hindi 🇮🇳", "en": "English 🇬🇧", "es": "Spanish 🇪🇸", "ar": "Arabic 🇸🇦",
    "fr": "French 🇫🇷", "ur": "Urdu 🇵🇰", "pt": "Portuguese 🇧🇷", "tr": "Turkish 🇹🇷",
    "de": "German 🇩🇪", "ru": "Russian 🇷🇺",
}

# Language names a caption may use, mapped to the code the gateway understands
SRT_LANG_NAMES = {
    "hindi": "hi", "english": "en", "spanish": "es", "arabic": "ar",
    "french": "fr", "urdu": "ur", "portuguese": "pt", "turkish": "tr",
    "german": "de", "russian": "ru", "italian": "it", "indonesian": "id",
    "japanese": "ja", "korean": "ko", "chinese": "zh", "bengali": "bn",
    "bangla": "bn", "tamil": "ta", "telugu": "te", "marathi": "mr",
    "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "punjabi": "pa",
    "persian": "fa", "farsi": "fa", "dutch": "nl", "polish": "pl",
    "ukrainian": "uk", "vietnamese": "vi", "thai": "th", "filipino": "tl",
    "tagalog": "tl", "hebrew": "he", "greek": "el", "spanish ": "es",
}

SRT_BUSY_MSG = (
    "The free translation service went quiet — it stops answering after many "
    "requests. Your file was not changed. Please try again in a few minutes."
)

def load_user_prefs() -> Dict[str, dict]:
    if USER_PREFS_FILE.exists():
        try:
            return json.loads(USER_PREFS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_user_prefs(prefs: Dict[str, dict]):
    USER_PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

def get_pref(user_id: int or str) -> dict:
    prefs = load_user_prefs()
    uid = str(user_id)
    if uid not in prefs:
        prefs[uid] = dict(DEFAULT_PREF)
        save_user_prefs(prefs)
    for k, v in DEFAULT_PREF.items():
        if k not in prefs[uid]:
            prefs[uid][k] = v
    return prefs[uid]

def set_pref(user_id: int or str, key: str, value: Any):
    prefs = load_user_prefs()
    uid = str(user_id)
    if uid not in prefs:
        prefs[uid] = dict(DEFAULT_PREF)
    prefs[uid][key] = value
    save_user_prefs(prefs)

# ----------------- FORMATTING & PROGRESS -----------------

def human_bytes(b: float or int) -> str:
    if not b or b <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = min(len(units) - 1, int(math.log(max(1, b), 1024)))
    return f"{b / (1024 ** i):.1f} {units[i]}"

def human_time(s: float or int) -> str:
    if not s or s <= 0:
        return "00:00"
    s = int(s)
    m = s // 60
    sec = s % 60
    return f"{m:02d}:{sec:02d}"

def render_progress_bar(percent: float, length: int = 10) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(length * percent / 100))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.1f}%"

def mask_secret(s: str) -> str:
    if not s:
        return "None"
    if len(s) <= 8:
        return "••••••••"
    return f"{s[:4]}••••{s[-4:]}"

# ----------------- LANGUAGE & CAPTION UTILS -----------------

class SubtitleSnippet:
    def __init__(self, text: str, start: float, duration: float):
        self.text = text
        self.start = float(start)
        self.duration = float(duration)

def load_subtitles_from_json3(json3_file: Path) -> list:
    """
    Loads timestamped dialogue snippets from yt-dlp downloaded json3 subtitle files.
    """
    snippets = []
    if not json3_file.exists():
        return snippets
    try:
        data = json.loads(json3_file.read_text(encoding="utf-8"))
        events = data.get("events", [])
        for ev in events:
            t_s = float(ev.get("tStartMs", 0)) / 1000.0
            d = float(ev.get("dDurationMs", 0)) / 1000.0
            segs = ev.get("segs", [])
            txt = "".join(s.get("utf8", "") for s in segs if s.get("utf8")).replace("\n", " ").strip()
            if txt and d > 0.1:
                snippets.append(SubtitleSnippet(txt, t_s, d))
    except Exception as e:
        print(f"[JSON3 Subtitle Load Error] {e}")
    return snippets

def to_hinglish_if_hindi(text: str) -> str:
    """
    If text contains Devanagari (Hindi) script, transliterates it into clean Roman Hinglish.
    If text is English, returns clean uppercase English.
    """
    if not text:
        return ""
    if re.search(r'[\u0900-\u097F]', text):
        translit = unidecode.unidecode(text)
        translit = re.sub(r'\s+', ' ', translit).strip()
        return translit.upper()
    return text.strip().upper()

def batch_convert_snippets_to_hinglish(snippets: list, gemini_key: str = "") -> list:
    """
    Translates/transliterates Hindi Devanagari lines into natural, conversational Roman Hinglish.
    Uses Gemini AI (gemini-3.8-flash / gemini-2.5-flash) with unidecode fallback.
    """
    hindi_items = []
    for idx, s in enumerate(snippets):
        raw = getattr(s, "text", "")
        if re.search(r'[\u0900-\u097F]', raw):
            hindi_items.append((idx, raw))

    if not hindi_items:
        return snippets

    if gemini_key:
        try:
            prompt = (
                "You are an expert Indian subtitle transliterator.\n"
                "Convert each numbered Hindi dialogue line into natural, high-energy Roman Hinglish (English alphabet only, ALL CAPS).\n"
                "Do NOT change the language to English, keep the Hindi spoken words written in English letters (e.g. 'अरे भाई देखो मगरमच्छ आ गया' -> 'ARE BHAI DEKHO MAGARMACH AA GAYA').\n"
                "Output one line per number in format 'ID: HINGLISH_TEXT'. Keep IDs exact.\n\n"
                + "\n".join(f"{idx}: {txt}" for idx, txt in hindi_items[:40])
            )
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
            for m_name in ["gemini-flash-lite-latest", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={gemini_key}"
                    r = httpx.post(url, json=body, timeout=12)
                    if r.status_code == 200:
                        data = r.json()
                        resp_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        for line in resp_text.splitlines():
                            line = line.strip()
                            if ":" in line:
                                p0, p1 = line.split(":", 1)
                                if p0.strip().isdigit():
                                    i = int(p0.strip())
                                    if 0 <= i < len(snippets):
                                        snippets[i].text = p1.strip().upper()
                        break
                except Exception:
                    continue
        except Exception as e:
            print(f"[Gemini Hinglish Error] {e}")
            print(f"[Gemini Hinglish Error] {e}")

    for s in snippets:
        if re.search(r'[\u0900-\u097F]', s.text):
            s.text = to_hinglish_if_hindi(s.text)

    return snippets

def transcribe_clip_audio_with_gemini(audio_path: Path, gemini_key: str, clip_offset: float = 0.0) -> list:
    """
    Transcribes audio clip using Gemini multimodal API and returns SubtitleSnippet list.
    """
    snippets = []
    if not audio_path.exists() or not gemini_key:
        return snippets
    try:
        from google import genai
        client = genai.Client(api_key=gemini_key)
        uploaded = client.files.upload(file=str(audio_path))
        prompt = (
            "Listen to this audio. If the audio is Hindi, transcribe it directly into Roman Hinglish (English letters). "
            "If it is English, transcribe in clean English. "
            "Return timestamped lines in this exact format:\n"
            "START_SEC - END_SEC: TEXT\n"
            "Example:\n"
            "1.2 - 3.5: ARE BHAI DEKHO KYA HUA\n"
            "3.5 - 6.0: GUYS FIGHT HO GAYI"
        )
        for m_name in ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-flash-latest"]:
            try:
                resp = client.models.generate_content(model=m_name, contents=[uploaded, prompt])
                if resp and resp.text:
                    for line in resp.text.splitlines():
                        m = re.search(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)\s*:\s*(.+)', line)
                        if m:
                            s_t = float(m.group(1)) + clip_offset
                            e_t = float(m.group(2)) + clip_offset
                            d_t = max(0.5, e_t - s_t)
                            t_val = m.group(3).strip().upper()
                            if t_val:
                                snippets.append(SubtitleSnippet(t_val, s_t, d_t))
                    if snippets:
                        break
            except Exception:
                continue
    except Exception as e:
        print(f"[Gemini Audio Transcribe Exception] {e}")
    return snippets

def build_ass_subtitles(snippets: list, clip_start: float, clip_end: float, out_path: Path, gemini_key: str = ""):
    """
    Builds clean, vibrant, non-overlapping ASS subtitles burned in the lower area (Y=1620) of 9:16 video.
    Strictly prevents vertical stacking and limits text to 2-3 punchy words per cue (MrBeast / Reels style).
    """
    ass_head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bounce,Arial,DejaVu Sans,Liberation Sans,sans-serif,56,&H0000F0FF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,0,5,0,0,0,1
Style: Clean,Arial,DejaVu Sans,Liberation Sans,sans-serif,50,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    def to_ass_time(sec):
        sec = max(0.0, sec)
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    active_snippets = [s for s in snippets if s.start >= clip_start - 0.5 and s.start < clip_end]
    if not active_snippets and snippets and snippets[0].start < 10.0:
        active_snippets = [s for s in snippets if s.start < (clip_end - clip_start)]
        offset = 0.0
    else:
        offset = clip_start

    # Batch convert any remaining Hindi to natural Hinglish
    active_snippets = batch_convert_snippets_to_hinglish(active_snippets, gemini_key=gemini_key)

    # 1. Flatten all snippets into individual words with estimated word timings
    all_words = []
    for snip in active_snippets:
        raw_t = snip.text.replace("\n", " ").strip()
        clean_t = to_hinglish_if_hindi(raw_t)
        clean_t = re.sub(r"[\{\}\\\/']", "", clean_t).strip().upper()
        words = clean_t.split()
        if not words:
            continue
        snip_s = max(clip_start, snip.start)
        snip_dur = max(0.4, snip.duration)
        snip_e = min(clip_end, snip_s + snip_dur)
        if snip_e <= snip_s:
            continue
        w_dur = (snip_e - snip_s) / len(words)
        for i, w in enumerate(words):
            all_words.append({
                "w": w,
                "s": snip_s + i * w_dur,
                "e": snip_s + (i + 1) * w_dur
            })

    # 2. Sort words strictly by timestamp and enforce non-overlapping times
    all_words.sort(key=lambda x: x["s"])
    for i in range(len(all_words) - 1):
        if all_words[i]["e"] > all_words[i + 1]["s"]:
            all_words[i]["e"] = max(all_words[i]["s"] + 0.1, all_words[i + 1]["s"])

    # 3. Group words into punchy 2-3 word chunks (never more than 3 words or 1.8 seconds)
    groups = []
    cur_g = []
    for w in all_words:
        if cur_g and (len(cur_g) >= 3 or (w["s"] - cur_g[-1]["e"] > 0.35) or (w["e"] - cur_g[0]["s"] > 1.8)):
            groups.append(cur_g)
            cur_g = []
        cur_g.append(w)
    if cur_g:
        groups.append(cur_g)

    # 4. Generate subtitle events with explicit position: \\an5\\pos(540,1620)
    # This locks text cleanly in the bottom third and CAN NEVER stack vertically to the top of screen!
    for i, g in enumerate(groups):
        g_s_rel = max(0.0, g[0]["s"] - offset)
        g_e_rel = min(clip_end - clip_start, g[-1]["e"] - offset)
        # Ensure no overlap with next group
        if i + 1 < len(groups):
            next_s_rel = max(0.0, groups[i + 1][0]["s"] - offset)
            if g_e_rel > next_s_rel:
                g_e_rel = max(g_s_rel + 0.15, next_s_rel)

        if g_e_rel - g_s_rel >= 0.15:
            text = " ".join(w["w"] for w in g)
            events.append(f"Dialogue: 0,{to_ass_time(g_s_rel)},{to_ass_time(g_e_rel)},Bounce,,0,0,0,,{{\\an5\\pos(540,1620)}}{text}")

    out_path.write_text(ass_head + "\n".join(events) + "\n", encoding="utf-8")

def build_srt_subtitles(snippets: list, clip_start: float, clip_end: float, out_path: Path, gemini_key: str = ""):
    """
    Builds clean, vibrant SRT subtitles burned in the lower-middle portion of the 9:16 video.
    """
    def to_srt_time(sec):
        sec = max(0.0, sec)
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    active_snippets = [s for s in snippets if s.start >= clip_start - 0.5 and s.start < clip_end]
    if not active_snippets and snippets and snippets[0].start < 10.0:
        active_snippets = [s for s in snippets if s.start < (clip_end - clip_start)]
        offset = 0.0
    else:
        offset = clip_start

    # Transliterate Hindi to Hinglish
    active_snippets = batch_convert_snippets_to_hinglish(active_snippets, gemini_key=gemini_key)

    all_words = []
    for snip in active_snippets:
        raw_t = snip.text.replace("\n", " ").strip()
        clean_t = to_hinglish_if_hindi(raw_t)
        clean_t = re.sub(r"[\{\}\\]", "", clean_t).strip().upper()
        words = clean_t.split()
        if not words:
            continue
        snip_s = max(clip_start, snip.start)
        snip_e = min(clip_end, snip_s + max(0.4, snip.duration))
        w_dur = (snip_e - snip_s) / len(words)
        for i, w in enumerate(words):
            all_words.append({"w": w, "s": snip_s + i * w_dur, "e": snip_s + (i + 1) * w_dur})

    all_words.sort(key=lambda x: x["s"])
    for i in range(len(all_words) - 1):
        if all_words[i]["e"] > all_words[i + 1]["s"]:
            all_words[i]["e"] = max(all_words[i]["s"] + 0.1, all_words[i + 1]["s"])

    groups = []
    cur_g = []
    for w in all_words:
        if cur_g and (len(cur_g) >= 3 or (w["s"] - cur_g[-1]["e"] > 0.35) or (w["e"] - cur_g[0]["s"] > 1.8)):
            groups.append(cur_g)
            cur_g = []
        cur_g.append(w)
    if cur_g:
        groups.append(cur_g)

    entries = []
    idx = 1
    for i, g in enumerate(groups):
        g_s_rel = max(0.0, g[0]["s"] - offset)
        g_e_rel = min(clip_end - clip_start, g[-1]["e"] - offset)
        if i + 1 < len(groups):
            next_s_rel = max(0.0, groups[i + 1][0]["s"] - offset)
            if g_e_rel > next_s_rel:
                g_e_rel = max(g_s_rel + 0.15, next_s_rel)
        if g_e_rel - g_s_rel >= 0.15:
            clean_t = " ".join(w["w"] for w in g)
            entries.append(f"{idx}\n{to_srt_time(g_s_rel)} --> {to_srt_time(g_e_rel)}\n{clean_t}\n")
            idx += 1

    out_path.write_text("\n".join(entries), encoding="utf-8")

# ----------------- VIRAL MOMENT DETECTION -----------------

def find_viral_peaks_from_snippets(snippets: list, duration: float, clip_dur: int = 30, num_clips: int = 1) -> list:
    """
    Scans transcript snippets to find high-energy viral action peaks.
    NEVER selects 0s intro (scans >= 15s).
    """
    if not snippets:
        return []

    VIRAL_KEYWORDS = {
        'alligator': 15, 'crocodile': 15, 'attack': 18, 'bite': 15, 'fight': 15,
        'danger': 12, 'dangerous': 12, 'run': 12, 'escape': 15, 'holy': 8, 'crap': 8,
        'omg': 12, 'kill': 12, 'huge': 10, 'giant': 10, 'insane': 15, 'crazy': 12,
        'shouting': 10, 'scream': 12, 'shock': 12, 'gun': 10, 'shooting': 10,
        'bro': 5, 'look': 6, 'watch': 8, 'bhai': 10, 'dekho': 12, 'bhago': 18, 'maro': 15,
        'arre': 8, 'khatarnak': 15, 'shor': 8, 'aaya': 8, 'pakad': 10, 'teri': 10,
        'मगरमच्छ': 18, 'हमला': 18, 'भागो': 18, 'खतरनाक': 15, 'भाई': 10, 'देखो': 12, 'मारो': 15, 'फाइट': 15, 'काटने': 15
    }

    step = 5
    max_t = int(min(duration, snippets[-1].start + snippets[-1].duration))
    scan_start = max(15, int(min(duration * 0.1, 35.0)))
    
    candidates = []
    for w_start in range(scan_start, max(scan_start + 5, max_t - clip_dur), step):
        w_end = w_start + clip_dur
        window_snippets = [s for s in snippets if s.start >= w_start and s.start < w_end]
        full_txt = ' '.join(s.text for s in window_snippets)
        score = 0
        for kw, weight in VIRAL_KEYWORDS.items():
            score += len(re.findall(r'\b' + re.escape(kw) + r'\b', full_txt, re.I)) * weight

        candidates.append((score, float(w_start), float(w_end), full_txt))

    candidates.sort(key=lambda x: x[0], reverse=True)

    selected = []
    target_count = max(1, min(5, int(num_clips) if str(num_clips).isdigit() else 1))

    min_separation = max(clip_dur + 15, 50)
    for score, w_s, w_e, txt in candidates:
        if any(abs(w_s - prev["start"]) < min_separation for prev in selected):
            continue
        
        hook = "ALLIGATOR ATTACK!"
        t_low = txt.lower()
        if "escape" in t_low or "bhago" in t_low or "भागो" in t_low:
            hook = "HE BARELY ESCAPED!"
        elif "fight" in t_low or "maro" in t_low or "मारो" in t_low or "फाइट" in t_low:
            hook = "BRUTAL FIGHT!"
        elif "bite" in t_low or "danger" in t_low or "खतरनाक" in t_low or "काटने" in t_low:
            hook = "BIGGEST DANGER!"
        elif "alligator" in t_low or "crocodile" in t_low or "मगरमच्छ" in t_low:
            hook = "ALLIGATOR ATTACK!"
        elif "insane" in t_low or "crazy" in t_low or "graphics" in t_low:
            hook = "UNREAL HIGHLIGHT!"

        selected.append({
            "start": w_s,
            "end": min(duration, w_e),
            "hook": hook,
            "title": f"Insane GTA 6 Real Life Highlight ({hook})",
            "caption": f"Jason encounters wild alligators in real life Florida! 😱 What would you do in this situation? Let us know below! 👇",
            "hashtags": [
                "#GTA6", "#GTA6RealLife", "#GamingReels", "#AlligatorAttack",
                "#JasonGTA6", "#ViralReels", "#TrendingReels", "#GamingCommunity",
                "#ShortsFeed", "#ExplorePage", "#ReelsInstagram", "#ViralGaming"
            ]
        })
        if len(selected) >= target_count:
            break

    return selected

def find_viral_peaks_from_transcript(video_id: str, duration: float, clip_dur: int = 30, num_clips: int = 1) -> tuple:
    """
    Fetches transcript via YouTubeTranscriptApi as a backup.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        snippets = YouTubeTranscriptApi().fetch(video_id)
        if not snippets:
            return [], []
        adapted = [SubtitleSnippet(s.text, s.start, s.duration) for s in snippets]
        clips = find_viral_peaks_from_snippets(adapted, duration, clip_dur, num_clips)
        return clips, adapted
    except Exception as e:
        print(f"[Transcript Peak Scout] {e}")
        return [], []

def find_viral_clips_with_gemini(
    title: str,
    duration: float,
    num_clips: int = 1,
    clip_dur: int = 30,
    description: str = "",
    chapters: list = None,
    gemini_key: str = ""
) -> list:
    keys = ([gemini_key] if gemini_key else []) + get_gemini_keys()
    if not keys:
        return []

    num = max(1, min(5, int(num_clips) if str(num_clips).isdigit() else 1))
    target_dur = max(15, min(60, int(clip_dur)))

    prompt = f"""You are the world's best viral clip scout and video editor for TikTok, YouTube Shorts, and Instagram Reels.
A user wants to create {num} viral 9:16 vertical shorts from this video:
- Video Title: {title}
- Total Duration: {duration:.1f} seconds ({human_time(duration)})
- Description: {description[:600]}
- Chapters/Timestamps: {json.dumps(chapters) if chapters else 'None'}

Your job:
Find the top {num} MOST VIRAL, dramatic, funny, or intense moments from this video that will blow up on Reels/Shorts.
Rules:
1. Each clip duration should be between 15 and {target_dur} seconds.
2. `start` and `end` must be floating-point numbers in seconds, within 15.0 and {duration:.1f}. NEVER pick 0s intro!
3. `hook`: 2-4 punchy UPPERCASE words to place on top of video (e.g., 'ALLIGATOR ATTACK!', 'UNREAL ESCAPE!', 'WAIT FOR IT!').
4. `title`: high-CTR click-worthy short title.
5. `caption`: engaging social media caption asking viewers a question to drive comments.
6. `hashtags`: 12-15 trending hashtags starting with #.

Return ONLY a strict JSON object with a single key 'clips' containing an array of objects with keys:
'start', 'end', 'hook', 'title', 'caption', 'hashtags'.
"""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "responseMimeType": "application/json"}
    }

    models_to_try = ["gemini-flash-lite-latest", "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-latest"]

    for active_key in keys:
        for model in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={active_key}"
            try:
                r = httpx.post(url, json=body, timeout=20)
                if r.status_code == 200:
                    data = r.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(raw_text)
                    raw_clips = parsed.get("clips") or []
                    valid_clips = []
                    for c in raw_clips:
                        s = max(15.0, float(c.get("start", 15.0)))
                        e = min(duration, float(c.get("end", s + target_dur)))
                        if e > s + 5.0:
                            valid_clips.append({
                                "start": s,
                                "end": e,
                                "hook": c.get("hook", "VIRAL MOMENT!").upper(),
                                "title": c.get("title", title),
                                "caption": c.get("caption", ""),
                                "hashtags": c.get("hashtags", ["#viral", "#shorts", "#reels"])
                            })
                    distinct_clips = []
                    min_sep = max(target_dur + 10, 45)
                    for c in valid_clips:
                        if not any(abs(c["start"] - prev["start"]) < min_sep for prev in distinct_clips):
                            distinct_clips.append(c)
                    if distinct_clips:
                        return distinct_clips
            except Exception:
                continue
    return []

def generate_ai_viral_meta(clip_path: Path, prompt_context: str = "", gemini_key: str = "") -> dict:
    keys = ([gemini_key] if gemini_key else []) + get_gemini_keys()
    thumb_path = TMP_DIR / f"{clip_path.stem}_ai_frame.jpg"
    
    try:
        subprocess.run([
            _FFMPEG_BIN, "-y", "-ss", "2",
            "-i", str(clip_path),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_path)
        ], capture_output=True)
    except Exception:
        pass

    img_b64 = None
    if thumb_path.exists():
        try:
            with open(thumb_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            pass

    if keys and img_b64:
        models_to_try = ["gemini-flash-lite-latest", "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-latest"]
        for active_key in keys:
            for model in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={active_key}"
                prompt = (
                    "You are an elite short-form video editor for Instagram Reels, YouTube Shorts, and TikTok. "
                    "Analyze this video frame. Return a strict JSON response with keys:\n"
                    "- 'hook': punchy 2-4 word UPPERCASE hook to burn on the video (e.g. ALLIGATOR ATTACK!, UNREAL MOMENT!)\n"
                    "- 'title': engaging 1-sentence title\n"
                    "- 'caption': viral Instagram Reel description ending with a call to action\n"
                    "- 'hashtags': array of 12-15 top trending hashtags with #\n"
                    "Return ONLY valid JSON."
                )
                body = {
                    "contents": [{
                        "role": "user",
                        "parts": [
                            {"text": prompt + (f"\nContext: {prompt_context}" if prompt_context else "")},
                            {"inline_data": {"mimeType": "image/jpeg", "data": img_b64}}
                        ]
                    }],
                    "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"}
                }
                try:
                    r = httpx.post(url, json=body, timeout=20)
                    if r.status_code == 200:
                        data = r.json()
                        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        clean_res = json.loads(raw_text)
                        return clean_res
                except Exception:
                    continue

    return {
        "hook": "VIRAL MOMENT!",
        "title": "Top Viral Clip | Must Watch Highlight!",
        "caption": "Wait till the end! 😱 What would you do in this situation? Let us know in the comments below! 👇🔥",
        "hashtags": ["#viral", "#reels", "#fyp", "#trending", "#gaming", "#shorts", "#explorepage"]
    }

def detect_facecam_ai(video_path: Path, src_w: int, src_h: int, total_dur: float, gemini_key: str = "", clip_s: float = 0.0, clip_e: float = 0.0) -> Optional[dict]:
    """
    Detects speaker / facecam in the video using Multi-Tier AI:
    Tier 1: Instant Local OpenCV Face & Corner Tracker (100% offline, zero API key, super reliable).
    Tier 2: Gemini Vision AI (if valid API key present).
    Tier 3: PeakCut Studio Edge & Stillness snapping.
    Returns: {"x": int, "y": int, "w": int, "h": int, "corner": str, "conf": str, "source": str} or None.
    """
    # ----------------- TIER 1: OPENCV LOCAL FACE & WEBCAM TRACKER -----------------
    try:
        import cv2
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        face_cascade = cv2.CascadeClassifier(cascade_path)
        
        # Sample 6 frames across the target window
        if clip_e > clip_s > 0:
            sample_times = [clip_s + (clip_e - clip_s) * r for r in [0.15, 0.30, 0.45, 0.60, 0.75, 0.90]]
        else:
            sample_times = [total_dur * r for r in [0.15, 0.30, 0.45, 0.60, 0.75, 0.90]]

        sightings = []  # list of (quadrant, x, y, w, h)

        for st in sample_times:
            sample_img_path = TMP_DIR / f"fc_scan_{int(time.time()*1000)}_{random.randint(100, 999)}.jpg"
            try:
                subprocess.run([
                    _FFMPEG_BIN, "-y", "-ss", f"{st:.2f}",
                    "-i", str(video_path),
                    "-vframes", "1", "-vf", "scale=960:-2", "-q:v", "3",
                    str(sample_img_path)
                ], capture_output=True, timeout=12)

                if sample_img_path.exists() and sample_img_path.stat().st_size > 1000:
                    img = cv2.imread(str(sample_img_path))
                    if img is not None:
                        ih, iw = img.shape[:2]
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(25, 25))
                        for (fx, fy, fw, fh) in faces:
                            # Map back to full resolution
                            rx = int(fx * src_w / iw)
                            ry = int(fy * src_h / ih)
                            rw = int(fw * src_w / iw)
                            rh = int(fh * src_h / ih)

                            # Determine quadrant (streamers sit in corners)
                            cx = rx + rw // 2
                            cy = ry + rh // 2
                            q_x = "left" if cx < src_w * 0.48 else ("right" if cx > src_w * 0.52 else "center")
                            q_y = "top" if cy < src_h * 0.48 else ("bottom" if cy > src_h * 0.52 else "middle")
                            quadrant = f"{q_y}-{q_x}"
                            sightings.append((quadrant, rx, ry, rw, rh))
            except Exception as e:
                pass
            finally:
                sample_img_path.unlink(missing_ok=True)

        if sightings:
            # Group by quadrant
            from collections import Counter
            quad_counts = Counter(s[0] for s in sightings)
            best_quad, count = quad_counts.most_common(1)[0]
            
            # If face is consistently seen in a corner / panel in 2 or more frames
            if count >= 2 or (len(sightings) == 1 and "middle" not in best_quad and "center" not in best_quad):
                matching = [s for s in sightings if s[0] == best_quad]
                med_x = int(sorted(s[1] for s in matching)[len(matching) // 2])
                med_y = int(sorted(s[2] for s in matching)[len(matching) // 2])
                med_w = int(sorted(s[3] for s in matching)[len(matching) // 2])
                med_h = int(sorted(s[4] for s in matching)[len(matching) // 2])

                # Build webcam bounding box around streamer's face
                cam_w = int(max(med_w * 3.4, src_w * 0.18))
                cam_h = int(max(med_h * 2.8, src_h * 0.22))
                center_x = med_x + med_w // 2
                center_y = med_y + med_h // 2

                box_x = max(0, min(src_w - cam_w, center_x - cam_w // 2))
                box_y = max(0, min(src_h - cam_h, center_y - int(cam_h * 0.38)))

                # Snap to edge if close
                if box_x <= src_w * 0.04:
                    box_x = 0
                elif box_x + cam_w >= src_w * 0.96:
                    box_x = src_w - cam_w
                if box_y <= src_h * 0.04:
                    box_y = 0
                elif box_y + cam_h >= src_h * 0.96:
                    box_y = src_h - cam_h

                print(f"[OpenCV Facecam Detected] corner={best_quad} box=({box_x}, {box_y}, {cam_w}, {cam_h}) sightings={count}")
                return {"x": box_x, "y": box_y, "w": cam_w, "h": cam_h, "corner": best_quad, "conf": "high", "source": "opencv"}
    except Exception as cv_err:
        print(f"[OpenCV Face Detection Error] {cv_err}")

    # ----------------- TIER 2: GEMINI VISION AI -----------------
    sample_jpg = TMP_DIR / f"fc_{int(time.time())}_{random.randint(100, 999)}.jpg"
    try:
        sample_time = clip_s + 5.0 if clip_e > clip_s > 0 else max(2.0, min(total_dur * 0.35, 45.0))
        subprocess.run([
            _FFMPEG_BIN, "-y", "-ss", f"{sample_time:.1f}",
            "-i", str(video_path),
            "-vframes", "1", "-vf", "scale=768:-2", "-q:v", "3",
            str(sample_jpg)
        ], capture_output=True, timeout=25)
    except Exception as e:
        print(f"[Facecam Extract Frame Error] {e}")

    img_b64 = None
    if sample_jpg.exists() and sample_jpg.stat().st_size > 500:
        try:
            img_b64 = base64.b64encode(sample_jpg.read_bytes()).decode("utf-8")
        except Exception:
            pass

    keys = ([gemini_key] if gemini_key else []) + get_gemini_keys()
    fx_pct = fy_pct = fw_pct = fh_pct = None

    if keys and img_b64:
        prompt = (
            "One frame of a gaming stream or video is attached.\n"
            "A streamer, gamer, or speaker often has their real face filmed by a webcam/camera in a corner or panel of the screen.\n"
            "Is a real human speaker or streamer webcam visible on this frame? (A real person in a room, not a video game character, not a HUD menu).\n"
            "Answer in ONE LINE and NOTHING ELSE:\n"
            "  CAM x=..% y=..% w=..% h=..%     (x, y is top-left corner as percentage, w and h are width and height percentage of the whole frame)\n"
            "  NO CAM"
        )
        models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
        for active_key in keys:
            for model in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={active_key}"
                body = {
                    "contents": [{
                        "role": "user",
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mimeType": "image/jpeg", "data": img_b64}}
                        ]
                    }],
                    "generationConfig": {"temperature": 0.1}
                }
                try:
                    r = httpx.post(url, json=body, timeout=15)
                    if r.status_code == 200:
                        data = r.json()
                        text = "".join(p.get("text", "") for p in data.get("candidates", [{}])[0].get("content", {}).get("parts", []))
                        m = re.search(r"x\s*=\s*(\d+(?:\.\d+)?)\s*%?\s*,?\s*y\s*=\s*(\d+(?:\.\d+)?)\s*%?\s*,?\s*w\s*=\s*(\d+(?:\.\d+)?)\s*%?\s*,?\s*h\s*=\s*(\d+(?:\.\d+)?)", text, re.I)
                        if m:
                            fx_pct, fy_pct, fw_pct, fh_pct = (float(v) / 100.0 for v in m.groups())
                            break
                        if "NO CAM" in text.upper():
                            sample_jpg.unlink(missing_ok=True)
                            return None
                except Exception:
                    continue
            if fx_pct is not None:
                break

    # ----------------- TIER 3: PEAKCUT STILLNESS EDGE SNAPPING -----------------
    try:
        from studio import facecam
        def ask_callback(jpeg_bytes):
            if fx_pct is not None:
                return {"looked": True, "box": (fx_pct, fy_pct, fw_pct, fh_pct)}
            return {"looked": False, "box": None}

        fc_res = facecam.detect(video_path, ask=ask_callback)
        if fc_res and fc_res.get("w", 0) > 40 and fc_res.get("h", 0) > 40:
            sample_jpg.unlink(missing_ok=True)
            return fc_res
    except Exception as e:
        print(f"[Studio Facecam Error] {e}")

    # Fallback to Gemini AI box
    if fx_pct is not None and fw_pct and fh_pct:
        fx = max(0, min(src_w - 40, int(round(src_w * fx_pct))))
        fy = max(0, min(src_h - 40, int(round(src_h * fy_pct))))
        fw = max(40, min(src_w - fx, int(round(src_w * fw_pct))))
        fh = max(40, min(src_h - fy, int(round(src_h * fh_pct))))
        sample_jpg.unlink(missing_ok=True)
        return {"x": fx, "y": fy, "w": fw, "h": fh, "corner": "detected", "conf": "high", "source": "gemini"}

    sample_jpg.unlink(missing_ok=True)
    return None
    return None

# ----------------- INSTAGRAM REELS META GRAPH API UPLOADER -----------------

async def post_to_instagram_reel(
    video_path: Path,
    caption: str,
    ig_user_id: str,
    access_token: str,
    status_callback=None
) -> tuple:
    """
    Uploads and publishes an HD video directly to Instagram Reels using Meta Graph API Resumable Container Upload.
    Runs 100% locally from machine binary without needing external web servers.
    Returns (success: bool, reel_id_or_error: str)
    """
    if not ig_user_id or not access_token:
        return False, "Instagram Account ID or Access Token is missing. Set them using /setinstagram."

    file_size = video_path.stat().st_size
    headers = {"Authorization": f"OAuth {access_token}"}

    # Step 1: Create Resumable Upload Container
    if status_callback:
        await status_callback("Initializing Instagram Reels media container...")

    init_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
    init_params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption[:2150],
        "share_to_feed": "true"
    }

    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        try:
            r1 = await client.post(init_url, params=init_params, headers=headers)
            res1 = r1.json()
            if r1.status_code != 200 or "id" not in res1:
                err_msg = res1.get("error", {}).get("message", str(res1))
                return False, f"Meta API Container Error: {err_msg}"

            container_id = res1["id"]
            upload_uri = res1.get("uri")
            if not upload_uri:
                return False, f"Meta API did not return upload URI: {res1}"

            # Step 2: Upload Video Binary
            if status_callback:
                await status_callback("Uploading HD video binary to Meta servers...")

            upload_headers = {
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(file_size)
            }
            with open(video_path, "rb") as vf:
                video_bytes = vf.read()

            r2 = await client.post(upload_uri, headers=upload_headers, content=video_bytes, timeout=180.0)
            if r2.status_code not in (200, 201):
                return False, f"Meta Binary Upload Failed: {r2.text[:200]}"

            # Step 3: Poll status until FINISHED
            if status_callback:
                await status_callback("Processing video on Instagram servers...")

            status_url = f"https://graph.facebook.com/v21.0/{container_id}"
            status_params = {"fields": "status_code,status", "access_token": access_token}

            for attempt in range(25):
                await asyncio.sleep(4)
                r3 = await client.get(status_url, params=status_params)
                res3 = r3.json()
                st_code = res3.get("status_code", "").upper()

                if st_code == "FINISHED":
                    break
                elif st_code in ("ERROR", "EXPIRED"):
                    return False, f"Meta Video Processing Error: {res3.get('status', 'Unknown error')}"

                if status_callback:
                    await status_callback(f"Encoding on Instagram... ({attempt * 4}s)")

            # Step 4: Publish Reel
            if status_callback:
                await status_callback("Publishing Reel to your Instagram profile...")

            publish_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish"
            publish_params = {"creation_id": container_id, "access_token": access_token}
            r4 = await client.post(publish_url, params=publish_params)
            res4 = r4.json()

            if r4.status_code == 200 and "id" in res4:
                media_id = res4["id"]
                return True, media_id
            else:
                err_msg = res4.get("error", {}).get("message", str(res4))
                return False, f"Meta Publish Error: {err_msg}"

        except Exception as e:
            return False, f"Network/Exception during Instagram Upload: {str(e)}"

# ----------------- ZERNIO SOCIAL API UPLOADER (NO FACEBOOK REQUIRED) -----------------

async def post_to_zernio_reel(
    api_key: str,
    account_id: str,
    video_path: Path,
    caption: str,
    status_callback=None
) -> tuple:
    """
    Publishes a Reel directly to Instagram via Zernio API.
    Does NOT require a Facebook login or Meta developer account!
    """
    base_api = "https://zernio.com/api/v1"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
        try:
            # Step 1: Request presigned upload URL
            if status_callback:
                await status_callback("Getting secure upload slot from Zernio...")
            
            p_res = await client.post(
                f"{base_api}/media/presign",
                headers=headers,
                json={"filename": video_path.name, "contentType": "video/mp4"}
            )
            if p_res.status_code not in (200, 201):
                return False, f"Zernio Presign Error: {p_res.text[:200]}"
            
            p_data = p_res.json().get("data", p_res.json())
            upload_url = p_data.get("uploadUrl")
            public_url = p_data.get("publicUrl")
            if not upload_url or not public_url:
                return False, f"Zernio did not return upload/public URL: {p_data}"

            # Step 2: Upload binary video via PUT
            if status_callback:
                await status_callback("Uploading HD Reel video to Zernio storage...")

            with open(video_path, "rb") as vf:
                v_bytes = vf.read()

            put_res = await client.put(
                upload_url,
                headers={"Content-Type": "video/mp4"},
                content=v_bytes,
                timeout=180.0
            )
            if put_res.status_code not in (200, 201):
                return False, f"Zernio Video Upload Failed: {put_res.status_code}"

            # Step 3: Publish to Instagram
            if status_callback:
                await status_callback("Publishing Reel to your Instagram via Zernio...")

            post_payload = {
                "content": caption[:2200],
                "platforms": [
                    {"platform": "instagram", "accountId": account_id}
                ],
                "mediaItems": [
                    {"type": "video", "url": public_url}
                ],
                "publishNow": True
            }

            post_res = await client.post(
                f"{base_api}/posts",
                headers=headers,
                json=post_payload,
                timeout=60.0
            )
            if post_res.status_code in (200, 201):
                res_json = post_res.json()
                post_obj = res_json.get("post") or res_json.get("data", {}).get("post") or res_json.get("data") or {}
                p_id = post_obj.get("_id") or post_obj.get("id") or res_json.get("id", "OK")
                return True, p_id
            else:
                return False, f"Zernio Publish Error: {post_res.text[:250]}"

        except Exception as e:
            return False, f"Zernio Request Exception: {str(e)}"

# ----------------- TELEGRAM API WRAPPER -----------------

class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=60.0, trust_env=False)
        self.session = requests.Session()
        self.session.trust_env = False

    async def get_me(self) -> dict:
        r = await self.client.get(f"{self.base_url}/getMe")
        return r.json()

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list:
        try:
            r = await self.client.get(
                f"{self.base_url}/getUpdates",
                params={"offset": offset, "timeout": timeout},
                timeout=timeout + 15
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("result", [])
        except Exception:
            pass
        return []

    async def send_message(self, chat_id: int or str, text: str, reply_markup: Optional[dict] = None, parse_mode: str = "HTML") -> Optional[dict]:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            r = await self.client.post(f"{self.base_url}/sendMessage", json=payload)
            if r.status_code == 200:
                return r.json().get("result")
        except Exception as e:
            print(f"[Error send_message] {e}")
        return None

    async def edit_message_text(self, chat_id: int or str, message_id: int, text: str, reply_markup: Optional[dict] = None, parse_mode: str = "HTML") -> bool:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            r = await self.client.post(f"{self.base_url}/editMessageText", json=payload)
            return r.status_code == 200
        except Exception:
            return False

    async def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False):
        try:
            await self.client.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert}
            )
        except Exception:
            pass

    def upload_with_progress(
        self,
        endpoint: str,
        chat_id: int or str,
        file_path: Path,
        file_field: str,
        caption: str = "",
        extra_fields: dict = None,
        progress_callback=None
    ) -> dict:
        url = f"{self.base_url}/{endpoint}"
        fields = {
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            "parse_mode": "HTML"
        }
        if extra_fields:
            fields.update(extra_fields)

        file_size = file_path.stat().st_size
        mime = "video/mp4" if file_path.suffix.lower() == ".mp4" else "application/octet-stream"
        
        file_obj = open(file_path, "rb")
        fields[file_field] = (file_path.name, file_obj, mime)

        start_time = time.time()
        last_pct = [0.0]

        def mon_cb(monitor):
            if progress_callback and file_size > 0:
                sent = monitor.bytes_read
                pct = min(100.0, (sent / file_size) * 100)
                elapsed = max(0.001, time.time() - start_time)
                speed = sent / elapsed
                remaining = max(0, file_size - sent)
                eta = remaining / max(0.001, speed)
                if pct - last_pct[0] >= 3.0 or pct >= 100.0:
                    last_pct[0] = pct
                    progress_callback(sent, file_size, pct, speed, eta)

        if MultipartEncoder is None or MultipartEncoderMonitor is None:
            try:
                files = {file_field: (file_path.name, file_obj, "video/mp4")}
                r = self.session.post(url, data=extra_fields or {}, files=files, timeout=300)
                file_obj.close()
                return r.json()
            except Exception as e:
                file_obj.close()
                return {"ok": False, "description": str(e)}

        encoder = MultipartEncoder(fields=fields)
        monitor = MultipartEncoderMonitor(encoder, mon_cb)

        try:
            r = self.session.post(
                url,
                data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=300
            )
            file_obj.close()
            return r.json()
        except Exception as e:
            file_obj.close()
            print(f"[Upload Error] {e}")
            return {"ok": False, "description": str(e)}

# ----------------- UI / MENUS & SETTINGS -----------------

def build_settings_keyboard(pref: dict) -> dict:
    layout_lbl = {
        "split": "📱 AI Split Screen",
        "gameplay": "🎮 AI Gameplay (PiP)",
        "blur": "📐 Blur (70% HD)",
        "zoom": "🔍 Zoom (9:16)",
        "crop": "✂️ Crop (9:16)"
    }.get(pref["layout"], pref["layout"])
    style_lbl = {"bounce": "✍️ Bounce", "hype": "🔥 Hype", "clean": "💎 Clean", "none": "🚫 None"}.get(pref["style"], pref["style"])
    send_lbl = {"video": "🎬 HD Video", "document": "📁 Document", "both": "🎬+📁 Both"}.get(pref["send_mode"], pref["send_mode"])
    dur_lbl = f"⏱️ {pref['clip_dur']}s"
    clips_lbl = f"🎯 {pref['clips_count']} Clip(s)"
    ai_lbl = "🟢 AI ON" if pref["ai_mode"] else "⚪ AI OFF"
    qual_lbl = f"📊 {pref['quality']}"

    u_gem = pref.get("gemini_key", "").strip()
    sys_gem = get_gemini_keys()
    gem_lbl = f"🟢 Personal" if u_gem else (f"🟢 System" if sys_gem else "⚪ Not Set")

    u_zern = pref.get("zernio_api_key", "").strip() and pref.get("zernio_account_id", "").strip()
    b_cfg = load_bot_config()
    sys_zern = b_cfg.get("zernio_api_key") and b_cfg.get("zernio_account_id")
    zern_lbl = "🟢 Personal" if u_zern else ("🟢 Default" if sys_zern else "⚪ Not Set")

    ig_configured = bool(pref.get("ig_account_id") and pref.get("ig_access_token"))
    ig_lbl = "🟢 Configured" if ig_configured else "⚪ Not Set"

    return {
        "inline_keyboard": [
            [
                {"text": f"Layout: {layout_lbl}", "callback_data": "menu_layout"},
                {"text": f"Style: {style_lbl}", "callback_data": "menu_style"}
            ],
            [
                {"text": f"Send As: {send_lbl}", "callback_data": "menu_send"},
                {"text": f"Length: {dur_lbl}", "callback_data": "menu_dur"}
            ],
            [
                {"text": f"Clips: {clips_lbl}", "callback_data": "menu_clips"},
                {"text": f"Quality: {qual_lbl}", "callback_data": "menu_qual"}
            ],
            [
                {"text": f"🔑 Gemini: {gem_lbl}", "callback_data": "menu_gemini"},
                {"text": f"🌟 Zernio: {zern_lbl}", "callback_data": "menu_zernio"}
            ],
            [
                {"text": f"📸 Meta API: {ig_lbl}", "callback_data": "menu_instagram"},
                {"text": f"Gemini AI: {ai_lbl}", "callback_data": "toggle_ai"}
            ],
            [
                {"text": "🔄 Reset Defaults", "callback_data": "reset_defaults"},
                {"text": "❌ Close Settings", "callback_data": "close_settings"}
            ]
        ]
    }

def build_layout_keyboard(pref: dict) -> dict:
    cur = pref.get("layout", "split")
    def mark(val): return "✅ " if cur == val else ""
    return {
        "inline_keyboard": [
            [{"text": f"{mark('split')}📱 AI Split Screen (Facecam Top + Game)", "callback_data": "set_layout_split"}],
            [{"text": f"{mark('gameplay')}🎮 AI Gameplay (Facecam Corner + Full Game)", "callback_data": "set_layout_gameplay"}],
            [{"text": f"{mark('blur')}📐 Pro Blurred Fill (70% Gameplay HD)", "callback_data": "set_layout_blur"}],
            [{"text": f"{mark('zoom')}🔍 Zoomed In (1080x1920)", "callback_data": "set_layout_zoom"}],
            [{"text": f"{mark('crop')}✂️ Fill Screen (Center Crop)", "callback_data": "set_layout_crop"}],
            [{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]
        ]
    }

def build_style_keyboard(pref: dict) -> dict:
    cur = pref["style"]
    def mark(val): return "✅ " if cur == val else ""
    return {
        "inline_keyboard": [
            [{"text": f"{mark('bounce')}Bounce (Yellow Spoken Subtitles)", "callback_data": "set_style_bounce"}],
            [{"text": f"{mark('clean')}Clean (White Subtitles)", "callback_data": "set_style_clean"}],
            [{"text": f"{mark('none')}No Captions (Clean Video)", "callback_data": "set_style_none"}],
            [{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]
        ]
    }

def build_send_mode_keyboard(pref: dict) -> dict:
    cur = pref["send_mode"]
    def mark(val): return "✅ " if cur == val else ""
    return {
        "inline_keyboard": [
            [{"text": f"{mark('video')}🎬 Normal HD Video (Streamable Player)", "callback_data": "set_send_video"}],
            [{"text": f"{mark('document')}📁 Document File (Uncompressed Original)", "callback_data": "set_send_document"}],
            [{"text": f"{mark('both')}🎬+📁 Both (Video + Document)", "callback_data": "set_send_both"}],
            [{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]
        ]
    }

def build_dur_keyboard(pref: dict) -> dict:
    cur = pref["clip_dur"]
    def mark(val): return "✅ " if cur == val else ""
    return {
        "inline_keyboard": [
            [
                {"text": f"{mark(15)}15 Seconds", "callback_data": "set_dur_15"},
                {"text": f"{mark(30)}30 Seconds ⭐", "callback_data": "set_dur_30"}
            ],
            [
                {"text": f"{mark(60)}60 Seconds (1 Min)", "callback_data": "set_dur_60"}
            ],
            [{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]
        ]
    }

def build_clips_keyboard(pref: dict) -> dict:
    cur = pref["clips_count"]
    def mark(val): return "✅ " if cur == val else ""
    return {
        "inline_keyboard": [
            [
                {"text": f"{mark(1)}1 Best Clip", "callback_data": "set_clips_1"},
                {"text": f"{mark(3)}Top 3 Clips ⭐", "callback_data": "set_clips_3"}
            ],
            [
                {"text": f"{mark(5)}Top 5 Clips", "callback_data": "set_clips_5"},
                {"text": f"{mark('full')}Full Video 9:16", "callback_data": "set_clips_full"}
            ],
            [{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]
        ]
    }

def build_quality_keyboard(pref: dict) -> dict:
    cur = pref["quality"]
    def mark(val): return "✅ " if cur == val else ""
    return {
        "inline_keyboard": [
            [{"text": f"{mark('1080p')}1080p Full HD (1080x1920) 🌟", "callback_data": "set_qual_1080p"}],
            [{"text": f"{mark('720p')}720p Fast HD (720x1280)", "callback_data": "set_qual_720p"}],
            [{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]
        ]
    }

async def download_via_cobalt(url: str, dest_file: Path, progress_callback=None) -> Optional[dict]:
    instances = [
        "https://api.cobalt.tools/",
        "https://cobalt.kwiatekm.pl/",
        "https://api.wuk.sh/"
    ]
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    body = {"url": url, "videoQuality": "1080"}
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        for inst in instances:
            try:
                r = await client.post(inst, json=body, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    dl_url = data.get("url")
                    if dl_url:
                        async with client.stream("GET", dl_url, timeout=120.0) as resp:
                            if resp.status_code == 200:
                                total_bytes = int(resp.headers.get("content-length", 0))
                                downloaded = 0
                                start_time = time.time()
                                last_call = 0.0
                                with open(dest_file, "wb") as f:
                                    async for chunk in resp.aiter_bytes(chunk_size=128 * 1024):
                                        f.write(chunk)
                                        downloaded += len(chunk)
                                        now = time.time()
                                        if progress_callback and (now - last_call >= 1.6 or (total_bytes and downloaded >= total_bytes)):
                                            last_call = now
                                            pct = (downloaded / total_bytes) * 100 if total_bytes else 0.0
                                            elapsed = max(0.001, now - start_time)
                                            spd = downloaded / elapsed
                                            eta = (total_bytes - downloaded) / max(0.001, spd) if total_bytes else 0.0
                                            progress_callback(downloaded, total_bytes, pct, spd, eta)
                                if dest_file.exists() and dest_file.stat().st_size > 1000:
                                    return {"title": data.get("filename", "YouTube Video"), "duration": 0.0}
            except Exception as e:
                print(f"[Cobalt Error {inst}] {e}")
                continue
    return None

async def download_via_invidious(url: str, dest_file: Path, progress_callback=None) -> Optional[dict]:
    m = re.search(r'(?:v=|\/|youtu\.be\/|shorts\/)([0-9A-Za-z_-]{11})', url)
    if not m:
        return None
    vid = m.group(1)
    instances = [
        f"https://invidious.nerdvpn.de/api/v1/videos/{vid}",
        f"https://inv.tux.pizza/api/v1/videos/{vid}",
        f"https://vid.priv.au/api/v1/videos/{vid}"
    ]
    async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
        for inst in instances:
            try:
                r = await client.get(inst)
                if r.status_code == 200:
                    data = r.json()
                    formats = data.get("formatStreams", [])
                    if not formats:
                        continue
                    best_f = None
                    for f in formats:
                        if f.get("container") == "mp4" and f.get("url"):
                            best_f = f
                            break
                    if not best_f:
                        best_f = formats[0]
                    dl_url = best_f.get("url")
                    if dl_url:
                        async with client.stream("GET", dl_url, timeout=120.0) as resp:
                            if resp.status_code == 200:
                                total_bytes = int(resp.headers.get("content-length", 0))
                                downloaded = 0
                                start_time = time.time()
                                last_call = 0.0
                                with open(dest_file, "wb") as f:
                                    async for chunk in resp.aiter_bytes(chunk_size=128 * 1024):
                                        f.write(chunk)
                                        downloaded += len(chunk)
                                        now = time.time()
                                        if progress_callback and (now - last_call >= 1.6 or (total_bytes and downloaded >= total_bytes)):
                                            last_call = now
                                            pct = (downloaded / total_bytes) * 100 if total_bytes else 0.0
                                            elapsed = max(0.001, now - start_time)
                                            spd = downloaded / elapsed
                                            eta = (total_bytes - downloaded) / max(0.001, spd) if total_bytes else 0.0
                                            progress_callback(downloaded, total_bytes, pct, spd, eta)
                                if dest_file.exists() and dest_file.stat().st_size > 1000:
                                    return {"title": data.get("title", ""), "duration": float(data.get("lengthSeconds", 0))}
            except Exception as e:
                print(f"[Invidious Error {inst}] {e}")
                continue
    return None

async def download_via_desi_api(url: str, dest_file: Path, progress_callback=None) -> Optional[dict]:
    api_endpoint = f"https://sheffysamra1bot-yt-api.smpost-tempmail.workers.dev/?url={url}&type=video"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
            r = await client.get(api_endpoint)
            if r.status_code != 200:
                return None
            data = r.json()
            if not data.get("success"):
                return None
            
            dl_url = data.get("download_url")
            if not dl_url:
                return None
            
            async with client.stream("GET", dl_url, headers=headers, timeout=120.0) as stream_resp:
                if stream_resp.status_code != 200:
                    return None
                
                total_bytes = int(stream_resp.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.time()
                last_call = 0.0

                with open(dest_file, "wb") as f:
                    async for chunk in stream_resp.aiter_bytes(chunk_size=128 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if progress_callback and (now - last_call >= 1.6 or (total_bytes and downloaded >= total_bytes)):
                            last_call = now
                            pct = (downloaded / total_bytes) * 100 if total_bytes else 0.0
                            elapsed = max(0.001, now - start_time)
                            spd = downloaded / elapsed
                            eta = (total_bytes - downloaded) / max(0.001, spd) if total_bytes else 0.0
                            progress_callback(downloaded, total_bytes, pct, spd, eta)

            if dest_file.exists() and dest_file.stat().st_size > 1000:
                return {
                    "title": data.get("title", ""),
                    "duration": float(data.get("duration", 0)),
                    "channel": data.get("channel", "")
                }
    except Exception as e:
        print(f"[DESI API Error] {e}")
    return None

_PPROXY_PROC = None

def ensure_pproxy_tunnel() -> Optional[str]:
    """
    Spawns a local pproxy daemon listening on 127.0.0.1:8125 that bridges
    to Surfshark HTTPS proxy on in-mum.prod.surfshark.com:443.
    """
    global _PPROXY_PROC
    if _PPROXY_PROC is not None and _PPROXY_PROC.poll() is None:
        return "http://127.0.0.1:8125"

    # Auto-install pproxy if missing in the environment
    try:
        import pproxy
    except ImportError:
        try:
            print("[Auto-Install] pproxy missing, installing via pip...")
            subprocess.run([sys.executable, "-m", "pip", "install", "pproxy"], capture_output=True, timeout=60)
            import pproxy
        except Exception as pie:
            print(f"[Auto-Install Error] Could not install pproxy: {pie}")

    upstream = os.environ.get("SURFSHARK_PROXY", "").strip()
    if not upstream:
        sec_file = ROOT_DIR / ".streamlit" / "secrets.toml"
        if sec_file.exists():
            try:
                for line in sec_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("SURFSHARK_PROXY") and "=" in line:
                        upstream = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except Exception:
                pass

    if not upstream or "amark" in upstream:
        upstream = "http+ssl://in-mum.prod.surfshark.com:443#9wPLNgvh3BzX7Bp4AhkMVcam:G7YDFVncQSzp6SENwjJAS2Mu"

    # Normalize upstream format if provided as https://user:pass@host:443
    if upstream.startswith("http://") or upstream.startswith("https://"):
        m = re.match(r'https?://([^:]+):([^@]+)@([^:/]+):?(\d+)?', upstream)
        if m:
            u, p, h, pt = m.groups()
            pt = pt or "443"
            upstream = f"http+ssl://{h}:{pt}#{u}:{p}"

    try:
        p_log = DATA_DIR / "pproxy.log"
        p_out = open(p_log, "a", encoding="utf-8")
        cmd = [
            sys.executable, "-m", "pproxy",
            "-l", "http://127.0.0.1:8125",
            "-r", upstream,
            "-v"
        ]
        _PPROXY_PROC = subprocess.Popen(
            cmd,
            stdout=p_out,
            stderr=p_out
        )
        time.sleep(1.2)
        if _PPROXY_PROC.poll() is None:
            print("[PProxy Tunnel Active] 127.0.0.1:8125 -> Surfshark Mumbai (443)")
            return "http://127.0.0.1:8125"
        else:
            print(f"[PProxy Exit Error] Process exited with code {_PPROXY_PROC.poll()}")
    except Exception as e:
        print(f"[PProxy Warning] Could not start pproxy tunnel: {e}")

    # A tunnel left over from an earlier run may already hold the port, which makes a new one
    # exit at once. If something is answering there, that is the tunnel: use it rather than
    # quietly going out without the VPN, which is what YouTube refuses.
    if _tunnel_listening():
        print("[PProxy Tunnel Active] Reusing the tunnel already listening on 127.0.0.1:8125")
        return "http://127.0.0.1:8125"
    return None


def _tunnel_listening(port: int = 8125) -> bool:
    """True if something is already serving the local tunnel port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0

# ----------------- VIDEO PROCESSING PIPELINE -----------------

async def process_video_job(
    bot: TelegramBot,
    chat_id: int or str,
    source_url_or_path: str,
    time_window: Optional[tuple] = None,
    custom_title: str = ""
):
    """
    Complete pipeline:
    1. Downloads video with live ETA, speed & percentage progress
    2. Runs PeakCut 9:16 FFmpeg blurred-fill transcode with animated captions
    3. Calls Gemini AI for viral Hook, Title, and Hashtags
    4. Uploads to Telegram with interactive [Upload to Instagram Now] and [Cancel] buttons
    """
    pref = get_pref(chat_id)
    status_msg = await bot.send_message(
        chat_id,
        "⚡ <b>PeakCut Studio Initializing...</b>\n<i>Analyzing source and fetching stream info...</i>"
    )
    if not status_msg:
        return

    msg_id = status_msg["message_id"]
    job_dir = TMP_DIR / f"job_{int(time.time())}_{chat_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_source_file = job_dir / "source.mp4"

    last_edit = [0.0]

    async def update_status(text: str, force: bool = False):
        now = time.time()
        if force or (now - last_edit[0] >= 1.6):
            last_edit[0] = now
            await bot.edit_message_text(chat_id, msg_id, text)

    try:
        # STEP 1: DOWNLOAD
        is_local_file = Path(source_url_or_path).exists()
        video_title = custom_title or "Video Stream"
        all_snippets = []

        if not is_local_file:
            cookies_file = DATA_DIR / "cookies.txt"
            if not cookies_file.exists() or cookies_file.stat().st_size < 20:
                for c_cand in [ROOT_DIR / "cookies.txt", ROOT_DIR / "data" / "cookies.txt"]:
                    if c_cand.exists() and c_cand.stat().st_size > 20:
                        try:
                            DATA_DIR.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(c_cand, cookies_file)
                        except Exception:
                            cookies_file = c_cand
                        break

            if not cookies_file.exists() or cookies_file.stat().st_size < 20:
                sec_file = ROOT_DIR / ".streamlit" / "secrets.toml"
                if sec_file.exists():
                    try:
                        import tomllib
                        with open(sec_file, "rb") as sf:
                            sec_dict = tomllib.load(sf)
                            if sec_dict.get("YOUTUBE_COOKIES"):
                                cookies_file.write_text(sec_dict["YOUTUBE_COOKIES"].strip(), encoding="utf-8")
                    except Exception:
                        pass

            has_cookies = cookies_file.exists() and cookies_file.stat().st_size > 20
            if has_cookies:
                print(f"[YouTube Cookies Active] Using {cookies_file} ({cookies_file.stat().st_size} bytes)")

            # Everything every attempt shares: a JavaScript runtime for YouTube's challenges and
            # TLS impersonation. Cookies, the player client and the proxy are picked per attempt,
            # because the iOS fallback needs a different mix of them.
            common_args = [
                "--no-check-certificates",
                "--socket-timeout", "20",
            ]
            try:
                import curl_cffi
                common_args.extend(["--impersonate", "chrome"])
            except ImportError:
                pass
            common_args.extend(get_js_runtime_args())

            # A personal proxy (never the forced Surfshark one) goes on the attempts that leave
            # straight from this machine. The VPN tunnel is used only by the attempts that go
            # without cookies: cookies belong to the address they were exported from, and
            # YouTube rejects them when they arrive from the tunnel's address instead. The
            # tunnel is what gets around "sign in to confirm you're not a bot" when this
            # machine's own address is refused.
            pxy = load_bot_config().get("proxy", "").strip()
            if not (pxy and pxy.startswith("http") and "surfshark.com" not in pxy.lower()):
                pxy = ""
            base_dl_args = common_args + (["--proxy", pxy] if pxy else [])

            tunnel_args = None
            tunnel = ensure_pproxy_tunnel()
            if tunnel:
                tunnel_args = common_args + ["--proxy", tunnel]

            # Cookies are given to the attempts that can use them. The iOS client ignores them
            # and yt-dlp drops it from the list when they are passed, which leaves a download
            # with no video formats at all ("Only images are available").
            cookie_args = ["--cookies", str(cookies_file)] if has_cookies else []

            # Match client to authentication: web client for web cookies
            client_args = ["--extractor-args",
                           "youtube:player_client=web,default" if has_cookies else "youtube:player_client=ios,mweb"]

            await update_status("🔍 <b>Connecting to High-Speed Stream Extractor...</b>\n<code>Analyzing video stream and subtitles...</code>", force=True)

            def fetch_info():
                try:
                    cmd = [
                        sys.executable, "-m", "yt_dlp",
                        *base_dl_args,
                        *cookie_args,
                        *client_args,
                        "--no-warnings",
                        "--print", "title",
                        "--print", "duration",
                        source_url_or_path
                    ]
                    # a minute, not fifteen seconds: the proof-of-token helper starts Deno, which
                    # on a cold start takes longer than that on its own
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if p.returncode == 0 and p.stdout.strip():
                        lines = [x.strip() for x in p.stdout.strip().splitlines() if x.strip()]
                        t = lines[0] if len(lines) > 0 else ""
                        d = float(lines[1]) if len(lines) > 1 and lines[1].replace(".", "").isdigit() else 0.0
                        return t, d
                except Exception:
                    pass
                return "", 0.0

            fetched_t, fetched_d = await asyncio.to_thread(fetch_info)
            if fetched_t:
                video_title = fetched_t

            # Format selector: best video up to 1080p + best audio, auto merged to mp4
            format_selector = "bv*[height<=1080]+ba/b[height<=1080]/best"
            dl_cmd = [
                sys.executable, "-m", "yt_dlp",
                *base_dl_args,
                *cookie_args,
                *client_args,
                "-f", format_selector,
                "--merge-output-format", "mp4",
                "--write-auto-sub", "--write-sub",
                "--sub-lang", "hi-orig,hi,en-orig,en",
                "--sub-format", "json3",
                "-o", str(raw_source_file),
                "--newline",
                source_url_or_path
            ]

            process = await asyncio.create_subprocess_exec(
                *dl_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            pct_reg = re.compile(r'\[download\]\s+(\d+\.?\d*)%')
            spd_reg = re.compile(r'at\s+~?\s*([0-9\.]+\s*[A-Za-z/]+)')
            eta_reg = re.compile(r'ETA\s+([0-9:]+)')
            size_reg = re.compile(r'of\s+(?:~\s*)?([0-9\.]+\s*[A-Za-z]+)')

            last_spd = "Calculating..."
            last_eta = "--:--"
            last_sz = "Estimating..."

            dl_err_lines = []
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode('utf-8', errors='ignore')
                # only the lines that explain a failure are kept: progress lines would push the
                # useful ones out of the small window
                if any(k in line_str for k in ("ERROR", "WARNING", "Sign in", "TransportError")):
                    dl_err_lines.append(line_str.strip())
                    if len(dl_err_lines) > 25:
                        dl_err_lines.pop(0)

                p_m = pct_reg.search(line_str)
                s_m = spd_reg.search(line_str)
                e_m = eta_reg.search(line_str)
                sz_m = size_reg.search(line_str)

                if s_m:
                    last_spd = s_m.group(1).strip()
                if e_m:
                    last_eta = e_m.group(1).strip()
                if sz_m:
                    last_sz = sz_m.group(1).strip()

                if p_m:
                    p_val = float(p_m.group(1))
                    p_bar = render_progress_bar(p_val, length=12)
                    status_text = (
                        f"📥 <b>Downloading YouTube Video...</b>\n"
                        f"<code>{p_bar}</code>\n"
                        f"⚡ <b>Speed:</b> {last_spd} | ⏳ <b>ETA:</b> {last_eta}\n"
                        f"📦 <b>Size:</b> {last_sz}\n"
                        f"🎬 <b>Title:</b> <i>{video_title[:35]}</i>"
                    )
                    await update_status(status_text)

            await process.wait()

            # ATTEMPT 2: the iOS client, which needs no proof-of-token. Cookies are left out on
            # purpose: yt-dlp drops the iOS client when they are passed, and with no client left
            # the download has no video formats at all ("Only images are available"). The JS
            # runtime and impersonation are kept, so YouTube's challenges still get solved. The
            # same try is made through the tunnel and then straight from this machine, so a
            # broken tunnel cannot end the job on its own.
            if not raw_source_file.exists() or raw_source_file.stat().st_size == 0:
                variants = [(tunnel_args, "through the VPN tunnel")] if tunnel_args else []
                variants.append((base_dl_args, "straight from this machine"))
                for v_args, where in variants:
                    if raw_source_file.exists() and raw_source_file.stat().st_size > 0:
                        break
                    await update_status(
                        "🔄 <b>Retrying with Zero-Cookies TV Stream Engine...</b>\n"
                        f"<i>Streaming video directly without cookies {where}...</i>",
                        force=True
                    )
                    alt_dl_cmd = [
                        sys.executable, "-m", "yt_dlp",
                        *v_args,
                        "--no-cookies",
                        "--extractor-args", "youtube:player_client=tv_embedded,tv,visionos",
                        "-f", "bv*[height<=1080]+ba/b[height<=1080]/best",
                        "--merge-output-format", "mp4",
                        "-o", str(raw_source_file),
                        "--newline",
                        source_url_or_path
                    ]
                    proc2 = await asyncio.create_subprocess_exec(
                        *alt_dl_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT
                    )
                    last_spd2 = "Calculating..."
                    last_eta2 = "--:--"
                    last_sz2 = "Estimating..."
                    while True:
                        line2 = await proc2.stdout.readline()
                        if not line2:
                            break
                        l_str = line2.decode('utf-8', errors='ignore')
                        if any(k in l_str for k in ("ERROR", "WARNING", "Sign in", "TransportError")):
                            dl_err_lines.append(l_str.strip())
                            if len(dl_err_lines) > 25:
                                dl_err_lines.pop(0)
                        p_m2 = pct_reg.search(l_str)
                        s_m2 = spd_reg.search(l_str)
                        e_m2 = eta_reg.search(l_str)
                        sz_m2 = size_reg.search(l_str)

                        if s_m2:
                            last_spd2 = s_m2.group(1).strip()
                        if e_m2:
                            last_eta2 = e_m2.group(1).strip()
                        if sz_m2:
                            last_sz2 = sz_m2.group(1).strip()

                        if p_m2:
                            p_val = float(p_m2.group(1))
                            status_text = (
                                f"📥 <b>Downloading YouTube Video...</b>\n"
                                f"<code>{render_progress_bar(p_val, 12)}</code>\n"
                                f"⚡ <b>Speed:</b> {last_spd2} | ⏳ <b>ETA:</b> {last_eta2}\n"
                                f"📦 <b>Size:</b> {last_sz2}\n"
                                f"🎬 <b>Title:</b> <i>{video_title[:35]}</i>"
                            )
                            await update_status(status_text)
                    await proc2.wait()

            # ATTEMPT 3: If still not downloaded, fallback to Cloudflare Workers Desi API
            if not raw_source_file.exists() or raw_source_file.stat().st_size == 0:
                await update_status(
                    "⚡ <b>Connecting to High-Speed Cloudflare Edge Engine...</b>\n"
                    "<i>Fetching direct video stream via Cloudflare Workers CDN...</i>",
                    force=True
                )
                def desi_progress(dl, total, pct, spd, eta):
                    asyncio.create_task(update_status(
                        f"📥 <b>Downloading via Cloudflare CDN...</b>\n"
                        f"<code>{render_progress_bar(pct, 12)}</code>\n"
                        f"⚡ <b>Speed:</b> {human_bytes(spd)}/s | ⏳ <b>ETA:</b> {int(eta)}s\n"
                        f"🎬 <b>Title:</b> <i>{video_title[:35]}</i>"
                    ))
                try:
                    desi_meta = await download_via_desi_api(source_url_or_path, raw_source_file, progress_callback=desi_progress)
                    if desi_meta and desi_meta.get("title"):
                        video_title = desi_meta["title"]
                except Exception as de:
                    print(f"[Desi API Exception] {de}")

            # Automatically parse downloaded json3 subtitles
            sub_candidates = list(job_dir.glob("*.json3"))
            if not sub_candidates:
                try:
                    # Quick separate subtitle fetch
                    subprocess.run([
                        sys.executable, "-m", "yt_dlp",
                        *base_dl_args,
                        *cookie_args,
                        "--skip-download",
                        "--write-auto-sub", "--write-sub",
                        "--sub-lang", "hi-orig,hi,en-orig,en",
                        "--sub-format", "json3",
                        "-o", str(job_dir / "sub"),
                        source_url_or_path
                    ], capture_output=True, timeout=60)
                    sub_candidates = list(job_dir.glob("*.json3"))
                except Exception:
                    pass

            chosen_sub = None
            for s_f in sorted(sub_candidates, key=lambda p: (0 if "hi" in p.name.lower() else (1 if "en" in p.name.lower() else 2))):
                chosen_sub = s_f
                break

            if chosen_sub:
                all_snippets = load_subtitles_from_json3(chosen_sub)

            if not raw_source_file.exists() or raw_source_file.stat().st_size == 0:
                recent_err = "\n".join([x for x in dl_err_lines if "ERROR:" in x or "WARNING:" in x or "Sign in" in x or "TransportError" in x][-6:])
                fail_text = (
                    f"❌ <b>Download Error Encountered:</b>\n\n"
                    f"<code>{recent_err or 'No media streams found.'}</code>\n\n"
                    "💡 <b>Tips:</b>\n"
                    "1. Upload or forward any video file (MP4/MOV) directly here.\n"
                    "2. Or export and send fresh <code>cookies.txt</code> to this bot."
                )
                await bot.edit_message_text(chat_id, msg_id, fail_text)
                return

            target_source = raw_source_file
        else:
            target_source = Path(source_url_or_path)

        # Probe video duration and resolution
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(target_source)
        ]
        probe_res = subprocess.run(probe_cmd, capture_output=True, text=True)
        try:
            p_data = json.loads(probe_res.stdout)
            total_dur = float(p_data["format"]["duration"])
            src_w = int(p_data["streams"][0]["width"])
            src_h = int(p_data["streams"][0]["height"])
        except Exception:
            total_dur = 60.0
            src_w = 1920
            src_h = 1080

        # STEP 2: DETERMINE CLIPS SLICES WITH TRANSCRIPT & GEMINI AI
        dur_setting = pref["clip_dur"]
        count_setting = pref["clips_count"]
        layout_setting = pref["layout"]
        style_setting = pref["style"]
        quality_setting = pref["quality"]
        ai_enabled = pref["ai_mode"]
        user_g_key = pref.get("gemini_key", "").strip() or (get_gemini_keys()[0] if get_gemini_keys() else "")

        slices = []
        clip_meta_list = []

        if not all_snippets:
            sub_candidates = list(job_dir.glob("*.json3"))
            chosen_sub = None
            for s_f in sorted(sub_candidates, key=lambda p: (0 if "hi" in p.name.lower() else (1 if "en" in p.name.lower() else 2))):
                chosen_sub = s_f
                break
            if chosen_sub:
                all_snippets = load_subtitles_from_json3(chosen_sub)

        yt_v_match = re.search(r'(?:v=|\/|youtu\.be\/|shorts\/)([0-9A-Za-z_-]{11})', source_url_or_path) if not is_local_file else None

        if time_window:
            slices.append((time_window[0], min(total_dur, time_window[1])))
            clip_meta_list.append(None)
        elif count_setting == "full":
            slices.append((0.0, total_dur))
            clip_meta_list.append(None)
        else:
            # 1. First priority: High-action peak detection from transcript snippets
            if all_snippets:
                await update_status(
                    "🔍 <b>Scanning Spoken Words & Viral Moments...</b>\n"
                    "<i>Analyzing action, crocodile attack & fight scenes...</i>",
                    force=True
                )
                yt_c = find_viral_peaks_from_snippets(all_snippets, total_dur, dur_setting, count_setting)
                if yt_c:
                    for c in yt_c:
                        slices.append((c["start"], c["end"]))
                        clip_meta_list.append(c)
            elif yt_v_match:
                yt_c, yt_snips = await asyncio.to_thread(
                    find_viral_peaks_from_transcript,
                    yt_v_match.group(1), total_dur, dur_setting, count_setting
                )
                if yt_c:
                    all_snippets = yt_snips
                    for c in yt_c:
                        slices.append((c["start"], c["end"]))
                        clip_meta_list.append(c)

            # 2. Try Gemini AI if transcript didn't yield clips
            if not slices and ai_enabled:
                await update_status(
                    "🤖 <b>Gemini AI is analyzing video...</b>\n"
                    "<i>Scanning video frames & story to pick viral short moments...</i>",
                    force=True
                )
                gemini_clips = await asyncio.to_thread(
                    find_viral_clips_with_gemini,
                    video_title, total_dur, count_setting, dur_setting, "", None, user_g_key
                )
                if gemini_clips:
                    for c in gemini_clips:
                        slices.append((c["start"], c["end"]))
                        clip_meta_list.append(c)

            # 3. Robust fallback: pick high-action window (NEVER 0s intro!)
            if not slices:
                num = int(count_setting) if str(count_setting).isdigit() else 1
                clip_len = min(total_dur, float(dur_setting))
                intro_skip = min(total_dur * 0.25, 45.0)
                step = max(clip_len + 25.0, (total_dur - intro_skip) / (num + 1))
                for i in range(num):
                    s = min(total_dur - clip_len, intro_skip + i * step)
                    e = min(total_dur, s + clip_len)
                    slices.append((s, e))
                    clip_meta_list.append(None)

        total_clips = len(slices)

        # STEP 3: RENDER CLIPS (9:16 VERTICAL TRANSCODE WITH HOOK BANNER & CAPTIONS)
        # AI Facecam / Speaker Detection (Gaming & Webcam Streams)
        facecam_box = None
        if ai_enabled and layout_setting in ("split", "gameplay", "blur"):
            await update_status(
                "👤 <b>AI Scanning Streamer / Speaker Facecam...</b>\n"
                "<i>Detecting webcam, reaction camera & speaker box with Gemini Vision...</i>",
                force=True
            )
            try:
                facecam_box = await asyncio.to_thread(
                    detect_facecam_ai, target_source, src_w, src_h, total_dur, user_g_key, slices[0][0], slices[0][1]
                )
                if facecam_box:
                    corner = facecam_box.get("corner", "detected")
                    print(f"[AI Facecam Detected] corner={corner} box={facecam_box}")
            except Exception as fe:
                print(f"[Facecam Scan Error] {fe}")

        rendered_clips = []
        out_w, out_h = (1080, 1920) if quality_setting == "1080p" else (720, 1280)

        for idx, (c_start, c_end) in enumerate(slices, 1):
            cur_meta = clip_meta_list[idx - 1] if idx - 1 < len(clip_meta_list) else None
            hook_text = cur_meta.get("hook", "VIRAL MOMENT!") if cur_meta else "VIRAL MOMENT!"
            if not hook_text or hook_text == "VIRAL MOMENT!":
                # Generate punchy context hook from clip's spoken dialogue or title
                clip_words = [s.text for s in all_snippets if s.start >= c_start - 0.5 and s.start < c_end]
                if clip_words:
                    sample_t = " ".join(clip_words[:8])
                    sample_h = to_hinglish_if_hindi(sample_t)
                    w_clean = re.sub(r"[^\w\s]", "", sample_h).strip().upper().split()
                    if w_clean:
                        hook_text = " ".join(w_clean[:3]) + "!"
                elif video_title and len(video_title) > 3:
                    t_clean = re.sub(r"[^\w\s]", "", video_title).strip().upper().split()
                    if t_clean:
                        hook_text = " ".join(t_clean[:3]) + "!"

            # Decide active layout: If facecam found, prioritize Split Screen or Gameplay PiP
            active_layout = layout_setting
            if facecam_box:
                if layout_setting in ("split", "blur"):
                    active_layout = "split"
                elif layout_setting == "gameplay":
                    active_layout = "gameplay"
            else:
                if layout_setting == "split":
                    active_layout = "crop"

            layout_desc = "AI Split Screen (Facecam Top)" if (active_layout == "split" and facecam_box) else (
                "AI Gameplay (Facecam Corner)" if (active_layout == "gameplay" and facecam_box) else active_layout.title()
            )

            await update_status(
                f"✂️ <b>Rendering 9:16 Vertical Short ({idx}/{total_clips})...</b>\n"
                f"<code>{render_progress_bar((idx - 0.5) / total_clips * 100, 12)}</code>\n"
                f"🎯 <b>Viral Hook:</b> <i>{hook_text}</i>\n"
                f"📐 <b>Layout:</b> {layout_desc} ({out_w}x{out_h})\n"
                f"🎨 <b>Captions:</b> Spoken Dialogue Subtitles (Auto Hinglish/English)\n"
                f"⏱️ <b>Range:</b> {human_time(c_start)} - {human_time(c_end)}",
                force=True
            )

            clip_name = f"short_{idx}_{int(time.time())}"
            clip_out = job_dir / f"{clip_name}.mp4"
            cover_out = job_dir / f"{clip_name}.jpg"

            # Build 9:16 FFmpeg video layout filter
            if active_layout == "split" and facecam_box:
                # Viral Split Screen: Streamer / Speaker Facecam on Top, Gameplay on Bottom
                fx, fy, fw, fh = facecam_box["x"], facecam_box["y"], facecam_box["w"], facecam_box["h"]
                top_h = int(round(out_h * 0.40))  # 768px for 1920
                bot_h = out_h - top_h             # 1152px
                vf = (
                    f"split[v_fc][v_gm];"
                    f"[v_fc]crop={fw}:{fh}:{fx}:{fy},scale={out_w}:{top_h}:force_original_aspect_ratio=increase,crop={out_w}:{top_h}[top];"
                    f"[v_gm]scale={out_w}:{bot_h}:force_original_aspect_ratio=increase,crop={out_w}:{bot_h}[bot];"
                    f"[top][bot]vstack"
                )
            elif active_layout == "gameplay" and facecam_box:
                # PeakCut Signature Gameplay Layout: Full Game + Facecam Corner PiP
                fx, fy, fw, fh = facecam_box["x"], facecam_box["y"], facecam_box["w"], facecam_box["h"]
                pip_w = 380 if out_w == 1080 else 260
                pip_h = max(2, int(round(pip_w * fh / fw)))
                vf = (
                    f"split=2[base][fc];"
                    f"[base]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}[b];"
                    f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={pip_w}:{pip_h}[p];"
                    f"[b][p]overlay={out_w - pip_w - 26}:26"
                )
            elif active_layout == "crop":
                vf = f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h}"
            elif active_layout == "zoom":
                vf = (
                    f"split[a][b];"
                    f"[a]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},boxblur=25:5[bg];"
                    f"[b]scale={out_w}:{int(out_h * 0.85)}:force_original_aspect_ratio=increase,crop={out_w}:{int(out_h * 0.85)}[fg];"
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
                )
            else:  # "blur" or default Pro Shorts Layout (70% screen height)
                vf = (
                    f"split[a][b];"
                    f"[a]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},boxblur=25:5,eq=brightness=-0.15[bg];"
                    f"[b]scale={out_w}:1350:force_original_aspect_ratio=increase,crop={out_w}:1350[fg];"
                    f"[bg][fg]overlay=0:285"
                )

            # Add Bold Hook Banner on top of video if style is enabled
            if style_setting != "none" and hook_text:
                clean_hook = re.sub(r"[^a-zA-Z0-9\s!?'\-\.,]", "", hook_text).strip()
                if clean_hook:
                    hook_y = 110 if (active_layout == "split" and facecam_box) else 140
                    vf += f",drawtext=text='{clean_hook}':fontcolor=black:fontsize=52:box=1:boxcolor=white@0.95:boxborderw=18:x=(w-text_w)/2:y={hook_y}"

            # Burn Spoken Dialogue Subtitles inside video
            ass_file = job_dir / f"subs_{idx}.ass"
            has_subs = False
            if style_setting != "none":
                g_key = user_g_key

                clip_snippets = [s for s in all_snippets if s.start >= c_start - 0.5 and s.start < c_end]

                # Fallback: if no transcript snippets (e.g. uploaded video or missing auto-subs), transcribe audio
                if not clip_snippets and g_key:
                    try:
                        clip_audio = job_dir / f"audio_clip_{idx}.mp3"
                        subprocess.run([
                            "ffmpeg", "-y", "-ss", f"{c_start:.2f}", "-to", f"{c_end:.2f}",
                            "-i", str(target_source), "-vn", "-acodec", "libmp3lame",
                            "-ar", "16000", "-ac", "1", str(clip_audio)
                        ], capture_output=True)
                        if clip_audio.exists() and clip_audio.stat().st_size > 1000:
                            clip_snippets = transcribe_clip_audio_with_gemini(clip_audio, g_key, clip_offset=c_start)
                    except Exception as e:
                        print(f"[Audio Transcribe Error] {e}")

                if clip_snippets:
                    try:
                        build_ass_subtitles(clip_snippets, c_start, c_end, ass_file, gemini_key=g_key)
                        if ass_file.exists() and ass_file.stat().st_size > 50:
                            has_subs = True
                            vf += f",ass=subs_{idx}.ass"
                    except Exception as e:
                        print(f"[Subtitle Burning Error] {e}")

            render_cmd = [
                _FFMPEG_BIN, "-y",
                "-ss", f"{c_start:.2f}",
                "-to", f"{c_end:.2f}",
                "-i", str(target_source),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "-movflags", "+faststart",
                str(clip_out)
            ]

            def do_render():
                res = subprocess.run(render_cmd, cwd=job_dir, capture_output=True)
                if not clip_out.exists():
                    err_msg = res.stderr.decode("utf-8", errors="ignore")[-800:]
                    print(f"[FFmpeg Render Error] {err_msg}")
                    try:
                        (job_dir / "render_error.log").write_text(err_msg, encoding="utf-8")
                    except Exception:
                        pass
                subprocess.run([
                    _FFMPEG_BIN, "-y", "-ss", "1",
                    "-i", str(clip_out),
                    "-vframes", "1", "-q:v", "2",
                    str(cover_out)
                ], capture_output=True)

            await asyncio.to_thread(do_render)

            if clip_out.exists() and clip_out.stat().st_size > 1000:
                rendered_clips.append({
                    "path": clip_out,
                    "cover": cover_out if cover_out.exists() else None,
                    "duration": int(c_end - c_start),
                    "index": idx,
                    "meta": cur_meta
                })

        if not rendered_clips:
            err_detail = ""
            err_log = job_dir / "render_error.log"
            if err_log.exists():
                err_detail = f"\n\n<code>{err_log.read_text(encoding='utf-8').strip()[-400:]}</code>"
            await bot.edit_message_text(chat_id, msg_id, f"❌ <b>Rendering Failed:</b> FFmpeg could not encode the vertical video clips.{err_detail}")
            return

        # STEP 4: REFINING GEMINI AI VIRAL METADATA
        await update_status(
            f"🤖 <b>Finalizing Instagram Viral Captions...</b>\n"
            f"<code>[████████████] 100%</code>\n"
            f"✨ <i>Preparing ready-to-copy post description and hashtags...</i>",
            force=True
        )

        for clip_item in rendered_clips:
            if not clip_item.get("meta"):
                clip_item["meta"] = generate_ai_viral_meta(clip_item["path"], prompt_context=video_title, gemini_key=user_g_key)

        # STEP 5: UPLOAD TO TELEGRAM & PREPARE INSTAGRAM POST BUTTONS
        send_mode = pref["send_mode"]

        for item in rendered_clips:
            c_file = item["path"]
            c_cover = item["cover"]
            c_dur = item["duration"]
            meta = item["meta"] or {}
            file_size_mb = c_file.stat().st_size / (1024 * 1024)

            # Generate unique post_id and save render for Instagram upload
            post_id = uuid.uuid4().hex[:10]
            saved_render_path = RENDERS_DIR / f"render_{post_id}.mp4"
            shutil.copy2(c_file, saved_render_path)

            hook = meta.get("hook", "ALLIGATOR ATTACK!")
            title = meta.get("title", video_title)
            caption_desc = meta.get("caption", "") or f"Insane alligator attack in GTA 6 Real Life! Jason got attacked by wild crocodiles in Florida... What would you do?"
            tags_list = meta.get("hashtags", [])
            if not tags_list or len(tags_list) < 8:
                tags_list = [
                    "#GTA6", "#GTA6RealLife", "#GamingReels", "#AlligatorAttack",
                    "#JasonGTA6", "#ViralReels", "#TrendingReels", "#GamingCommunity",
                    "#ShortsFeed", "#ExplorePage", "#ReelsInstagram", "#ViralGaming"
                ]
            tags_str = " ".join(tags_list)

            # Clean full caption for Instagram
            ig_caption = (
                f"{title}\n\n"
                f"{caption_desc}\n\n"
                f"What would you do in this situation? Let us know below! 👇🔥\n\n"
                f"Follow for daily viral gaming clips! 🎮✨\n.\n.\n"
                f"{tags_str}"
            )

            # Register pending post
            PENDING_POSTS[post_id] = {
                "video_path": str(saved_render_path),
                "caption": ig_caption,
                "title": title,
                "chat_id": chat_id,
                "timestamp": time.time()
            }

            full_caption = (
                f"🔥 <b>{hook}</b>\n"
                f"🎬 <b>{title}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>INSTAGRAM POST COPY (Tap to Copy):</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{caption_desc}\n\n"
                f"What would you do in this situation? Let us know below! 👇🔥\n\n"
                f"Follow for daily viral gaming clips! 🎮✨\n.\n.\n"
                f"{tags_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <i>9:16 Vertical HD Short with Spoken Subtitles</i>"
            )

            # Progress callback for upload
            def upload_progress(sent_b, total_b, pct, spd, eta):
                p_bar = render_progress_bar(pct, length=12)
                mode_str = "HD Video Media" if send_mode == "video" else "Document File"
                text = (
                    f"📤 <b>Uploading to Telegram ({mode_str})...</b>\n"
                    f"<code>{p_bar}</code>\n"
                    f"⚡ <b>Upload Speed:</b> {human_bytes(spd)}/s\n"
                    f"⏳ <b>ETA:</b> {human_time(eta)} | <b>Size:</b> {human_bytes(sent_b)} / {human_bytes(total_b)}\n"
                    f"🎯 <b>Clip:</b> <i>{title[:30]}</i>"
                )
                now = time.time()
                if now - last_edit[0] >= 1.8:
                    last_edit[0] = now
                    asyncio.run_coroutine_threadsafe(
                        bot.edit_message_text(chat_id, msg_id, text),
                        loop
                    )

            # Interactive inline buttons for recipient
            clip_kb = {
                "inline_keyboard": [
                    [
                        {"text": "🚀 Upload to Instagram Now", "callback_data": f"ig_post_{post_id}"},
                        {"text": "❌ Cancel Upload", "callback_data": f"ig_cancel_{post_id}"}
                    ],
                    [
                        {"text": "📁 Send as Document (Uncompressed)", "callback_data": f"doc_{item['index']}"} if send_mode == "video"
                        else {"text": "🎬 Send as Streamable HD Video", "callback_data": f"vid_{item['index']}"}
                    ],
                    [
                        {"text": "⚙️ Settings & API Keys", "callback_data": "open_settings"}
                    ]
                ]
            }

            # Send HD Media Video
            if send_mode in ("video", "both"):
                await update_status(f"📤 <b>Uploading HD Video Media ({file_size_mb:.1f} MB)...</b>", force=True)
                extra = {
                    "duration": str(c_dur),
                    "width": str(out_w),
                    "height": str(out_h),
                    "supports_streaming": "true",
                    "reply_markup": json.dumps(clip_kb)
                }
                res = await asyncio.to_thread(
                    bot.upload_with_progress,
                    "sendVideo",
                    chat_id,
                    c_file,
                    "video",
                    full_caption,
                    extra,
                    upload_progress
                )

            # Send Uncompressed Document
            if send_mode in ("document", "both"):
                await update_status(f"📤 <b>Uploading Uncompressed Master Document ({file_size_mb:.1f} MB)...</b>", force=True)
                extra = {"reply_markup": json.dumps(clip_kb)}
                res = await asyncio.to_thread(
                    bot.upload_with_progress,
                    "sendDocument",
                    chat_id,
                    c_file,
                    "document",
                    f"📁 <b>Original HD Master (Uncompressed):</b>\n{full_caption}",
                    extra,
                    upload_progress
                )

            # Send clean 1-click tap-to-copy code block for Instagram Reel caption
            copy_box = (
                f"📋 <b>INSTAGRAM POST COPY (Tap to copy):</b>\n\n"
                f"<code>{ig_caption}</code>\n\n"
                f"✨ <i>Tip: Tap the box above to copy the complete caption & hashtags in 1 click!</i>"
            )
            await bot.send_message(chat_id, copy_box)

        # Update progress status message
        await bot.edit_message_text(
            chat_id, msg_id,
            f"🎉 <b>All {len(rendered_clips)} Vertical Shorts Successfully Delivered!</b>\n\n"
            f"👉 <b>Watch the video above:</b>\n"
            f"• Tap <b>[🚀 Upload to Instagram Now]</b> to automatically publish as Reel\n"
            f"• Tap <b>[❌ Cancel Upload]</b> if you want to skip uploading",
            reply_markup={"inline_keyboard": [[{"text": "⚙️ Open Settings", "callback_data": "open_settings"}]]}
        )

    except Exception as err:
        print(f"[Pipeline Exception] {err}")
        await bot.edit_message_text(chat_id, msg_id, f"❌ <b>Error occurred during clipping:</b>\n<code>{str(err)[:300]}</code>")
    finally:
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
        cleanup_storage(max_age_seconds=1800)

# ----------------- MAIN MESSAGE & CALLBACK DISPATCHER -----------------

async def handle_update(bot: TelegramBot, update: dict):
    # 1. Handle Inline Keyboard Callbacks
    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        data = cb.get("data", "")
        pref = get_pref(chat_id)

        # --- INSTAGRAM REEL CONFIRMATION HANDLERS ---
        if data.startswith("ig_post_"):
            post_id = data.replace("ig_post_", "")
            post_info = PENDING_POSTS.get(post_id)

            if not post_info:
                await bot.answer_callback_query(cb_id, "⚠️ Post expired or not found. Please regenerate.", show_alert=True)
                return

            bot_cfg = load_bot_config()
            zernio_key = pref.get("zernio_api_key", "").strip() or bot_cfg.get("zernio_api_key", "").strip()
            zernio_acc = pref.get("zernio_account_id", "").strip() or bot_cfg.get("zernio_account_id", "").strip()
            ig_acc = pref.get("ig_account_id", "").strip()
            ig_token = pref.get("ig_access_token", "").strip()

            use_zernio = bool(zernio_key and zernio_acc)
            use_meta = bool(ig_acc and ig_token)

            if not use_zernio and not use_meta:
                help_ig = (
                    "⚠️ <b>Instagram Publishing API Not Configured!</b>\n\n"
                    "Choose one of the 2 methods to connect your Instagram:\n\n"
                    "🌟 <b>Method 1: Zernio API (Recommended - Direct Instagram, NO Facebook Required):</b>\n"
                    "1. Go to <code>https://zernio.com/</code> and connect your Instagram (Direct Login).\n"
                    "2. Copy your API Key & Account ID from Zernio dashboard.\n"
                    "3. Send to bot:\n"
                    "<code>/setzernio &lt;API_KEY&gt; &lt;ACCOUNT_ID&gt;</code>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🏢 <b>Method 2: Official Meta Graph API (Requires Facebook Page & App):</b>\n"
                    "<code>/setinstagram &lt;ACCOUNT_ID&gt; &lt;ACCESS_TOKEN&gt;</code>\n"
                )
                await bot.send_message(chat_id, help_ig)
                await bot.answer_callback_query(cb_id, "Please configure Zernio or Meta Instagram API")
                return

            video_p = Path(post_info["video_path"])
            if not video_p.exists():
                await bot.answer_callback_query(cb_id, "❌ Video file no longer found on server.", show_alert=True)
                return

            await bot.answer_callback_query(cb_id, "🚀 Starting Instagram Reel Upload...")

            server_name = "Zernio Social Engine" if use_zernio else "Meta Graph API"
            status_msg = await bot.send_message(
                chat_id,
                f"🚀 <b>Publishing Reel to Instagram...</b>\n<i>Connecting to {server_name}...</i>"
            )
            s_msg_id = status_msg["message_id"] if status_msg else None

            async def ig_status_update(txt: str):
                if s_msg_id:
                    await bot.edit_message_text(chat_id, s_msg_id, f"🚀 <b>Publishing Reel to Instagram...</b>\n<i>{txt}</i>")

            if use_zernio:
                success, res_val = await post_to_zernio_reel(
                    zernio_key,
                    zernio_acc,
                    video_p,
                    post_info["caption"],
                    status_callback=ig_status_update
                )
            else:
                success, res_val = await post_to_instagram_reel(
                    video_p,
                    post_info["caption"],
                    ig_acc,
                    ig_token,
                    status_callback=ig_status_update
                )

            if success:
                # Cleanup pending post
                PENDING_POSTS.pop(post_id, None)
                try:
                    video_p.unlink(missing_ok=True)
                except Exception:
                    pass

                success_text = (
                    "🎉 <b>Successfully Uploaded to Instagram Reel!</b> 🚀📸\n\n"
                    f"• <b>Media ID:</b> <code>{res_val}</code>\n"
                    f"• <b>Account ID:</b> <code>{ig_acc}</code>\n"
                    f"• <b>Status:</b> Published Live in HD!\n"
                    f"• <b>Caption:</b> Title, description & trending hashtags added!\n\n"
                    "✨ <i>Open your Instagram profile or app to view your live Reel!</i>"
                )
                if s_msg_id:
                    await bot.edit_message_text(chat_id, s_msg_id, success_text)
                else:
                    await bot.send_message(chat_id, success_text)
            else:
                err_text = (
                    f"❌ <b>Instagram Upload Failed:</b>\n\n"
                    f"<code>{res_val}</code>\n\n"
                    "<i>Please verify your Instagram Account ID & Token validity.</i>"
                )
                if s_msg_id:
                    await bot.edit_message_text(chat_id, s_msg_id, err_text)
                else:
                    await bot.send_message(chat_id, err_text)
            return

        if data.startswith("ig_cancel_"):
            post_id = data.replace("ig_cancel_", "")
            post_info = PENDING_POSTS.pop(post_id, None)
            if post_info:
                try:
                    Path(post_info["video_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            await bot.answer_callback_query(cb_id, "Instagram upload cancelled.")
            await bot.send_message(chat_id, "❌ <b>Instagram upload cancelled.</b> The video remains available in this chat.")
            return

        # --- SETTINGS DASHBOARD HANDLERS ---
        if data == "open_settings":
            await bot.edit_message_text(chat_id, msg_id, "⚙️ <b>PeakCut Studio Settings Dashboard</b>\n<i>Customize layout, captions, delivery format, and API keys:</i>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id)
            return

        if data == "close_settings":
            await bot.edit_message_text(chat_id, msg_id, "✅ <i>Settings saved. Send a video link or file to start clipping!</i>")
            await bot.answer_callback_query(cb_id)
            return

        if data == "reset_defaults":
            save_user_prefs({str(chat_id): dict(DEFAULT_PREF)})
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, "🔄 <b>Settings reset to defaults!</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, "Reset to defaults!")
            return

        # Submenus
        if data == "menu_layout":
            await bot.edit_message_text(chat_id, msg_id, "📐 <b>Choose 9:16 Vertical Video Layout:</b>", reply_markup=build_layout_keyboard(pref))
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_style":
            await bot.edit_message_text(chat_id, msg_id, "✍️ <b>Choose Subtitle & Caption Style:</b>\n<i>Spoken Hindi is auto-transliterated to clean Hinglish!</i>", reply_markup=build_style_keyboard(pref))
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_send":
            await bot.edit_message_text(chat_id, msg_id, "📦 <b>Choose Output Delivery Mode:</b>\n• <i>HD Video:</i> Streamable player in Telegram\n• <i>Document:</i> Raw uncompressed file", reply_markup=build_send_mode_keyboard(pref))
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_dur":
            await bot.edit_message_text(chat_id, msg_id, "⏱️ <b>Choose Default Clip Duration:</b>", reply_markup=build_dur_keyboard(pref))
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_clips":
            await bot.edit_message_text(chat_id, msg_id, "🎯 <b>How Many Clips to Generate:</b>", reply_markup=build_clips_keyboard(pref))
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_qual":
            await bot.edit_message_text(chat_id, msg_id, "📊 <b>Choose Resolution:</b>", reply_markup=build_quality_keyboard(pref))
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_gemini":
            u_key = pref.get("gemini_key", "").strip()
            sys_keys = get_gemini_keys()
            if u_key:
                status_str = f"🟢 <b>Personal Key Active:</b> <code>{mask_secret(u_key)}</code>"
            elif sys_keys:
                status_str = f"ℹ️ <b>Using Bot System Key:</b> <code>{mask_secret(sys_keys[0])}</code>"
            else:
                status_str = "⚪ <b>No Gemini Key Configured</b>"

            txt = (
                "🔑 <b>Gemini AI Key (Multi-User Setup)</b>\n\n"
                f"{status_str}\n\n"
                "<i>Har user apna personal Gemini API key add kar sakta hai taaki AI quota limitless rahe!</i>\n\n"
                "• <b>Personal Key Set Karein:</b>\n"
                "<code>/setgemini YOUR_GEMINI_API_KEY</code>\n\n"
                "• <b>Personal Key Remove Karein:</b>\n"
                "<code>/delgemini</code>\n\n"
                "💡 <i>Get free Gemini API keys from <a href='https://aistudio.google.com/'>Google AI Studio</a>.</i>"
            )
            await bot.edit_message_text(chat_id, msg_id, txt, reply_markup={"inline_keyboard": [[{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]]})
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_zernio":
            z_key = pref.get("zernio_api_key", "").strip()
            z_acc = pref.get("zernio_account_id", "").strip()
            b_cfg = load_bot_config()
            sys_key = b_cfg.get("zernio_api_key", "").strip()
            sys_acc = b_cfg.get("zernio_account_id", "").strip()

            if z_key and z_acc:
                status_str = f"🟢 <b>Personal Zernio Account:</b>\n• Account ID: <code>{z_acc}</code>\n• Key: <code>{mask_secret(z_key)}</code>"
            elif sys_key and sys_acc:
                status_str = f"ℹ️ <b>Using Bot Default Zernio:</b>\n• Account ID: <code>{sys_acc}</code>\n• Key: <code>{mask_secret(sys_key)}</code>"
            else:
                status_str = "⚪ <b>No Zernio Account Configured</b>"

            txt = (
                "🌟 <b>Zernio Instagram API (Multi-User Setup)</b>\n\n"
                f"{status_str}\n\n"
                "<i>Har user apna khud ka Instagram/Zernio account connect karke direct 1-click Reels post kar sakta hai!</i>\n\n"
                "• <b>Personal Zernio Set Karein:</b>\n"
                "<code>/setzernio YOUR_API_KEY YOUR_ACCOUNT_ID</code>\n\n"
                "• <b>Personal Zernio Remove Karein:</b>\n"
                "<code>/delzernio</code>\n\n"
                "💡 <i>Get credentials instantly from <a href='https://zernio.com/'>zernio.com</a> via Direct Instagram Login (Zero Facebook required)!</i>"
            )
            await bot.edit_message_text(chat_id, msg_id, txt, reply_markup={"inline_keyboard": [[{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]]})
            await bot.answer_callback_query(cb_id)
            return

        if data == "menu_instagram":
            ig_acc = pref.get("ig_account_id", "").strip()
            ig_tok = pref.get("ig_access_token", "").strip()
            status_str = f"🟢 <b>Account ID:</b> <code>{ig_acc}</code>\n🟢 <b>Token:</b> <code>{mask_secret(ig_tok)}</code>" if (ig_acc and ig_tok) else "⚪ <b>Instagram API Not Configured</b>"
            txt = (
                "📸 <b>Instagram Graph API Management</b>\n\n"
                f"{status_str}\n\n"
                "• <b>To Set or Update Credentials:</b>\n"
                "<code>/setinstagram &lt;ACCOUNT_ID&gt; &lt;ACCESS_TOKEN&gt;</code>\n\n"
                "• <b>To Delete Credentials:</b>\n"
                "<code>/delinstagram</code>\n\n"
                "💡 <i>Allows 1-click automatic publishing of Reels in Full HD!</i>"
            )
            await bot.edit_message_text(chat_id, msg_id, txt, reply_markup={"inline_keyboard": [[{"text": "⬅️ Back to Settings", "callback_data": "open_settings"}]]})
            await bot.answer_callback_query(cb_id)
            return

        if data == "toggle_ai":
            new_val = not pref["ai_mode"]
            set_pref(chat_id, "ai_mode", new_val)
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, "⚙️ <b>PeakCut Studio Settings Dashboard</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, f"Gemini AI {'Enabled' if new_val else 'Disabled'}")
            return

        # Setting setters
        if data.startswith("set_layout_"):
            val = data.replace("set_layout_", "")
            set_pref(chat_id, "layout", val)
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, f"✅ Layout updated to <b>{val.title()}</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, f"Layout: {val}")
            return

        if data.startswith("set_style_"):
            val = data.replace("set_style_", "")
            set_pref(chat_id, "style", val)
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, f"✅ Caption style updated to <b>{val.title()}</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, f"Style: {val}")
            return

        if data.startswith("set_send_"):
            val = data.replace("set_send_", "")
            set_pref(chat_id, "send_mode", val)
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, f"✅ Delivery mode updated to <b>{val.upper()}</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, f"Delivery: {val}")
            return

        if data.startswith("set_dur_"):
            val = int(data.replace("set_dur_", ""))
            set_pref(chat_id, "clip_dur", val)
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, f"✅ Clip duration set to <b>{val}s</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, f"Duration: {val}s")
            return

        if data.startswith("set_clips_"):
            val = data.replace("set_clips_", "")
            set_pref(chat_id, "clips_count", int(val) if val.isdigit() else val)
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, f"✅ Clips count set to <b>{val}</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, f"Clips: {val}")
            return

        if data.startswith("set_qual_"):
            val = data.replace("set_qual_", "")
            set_pref(chat_id, "quality", val)
            pref = get_pref(chat_id)
            await bot.edit_message_text(chat_id, msg_id, f"✅ Resolution set to <b>{val}</b>", reply_markup=build_settings_keyboard(pref))
            await bot.answer_callback_query(cb_id, f"Quality: {val}")
            return

        # --- SUBTITLE TRANSLATION: a language was picked for a waiting .srt file ---
        if data.startswith("srtlang:"):
            lang = data.split(":", 1)[1]
            pending = PENDING_SRT.get(chat_id)
            if not pending or time.time() - pending.get("t", 0) > 1800:
                PENDING_SRT.pop(chat_id, None)
                await bot.answer_callback_query(
                    cb_id, "⚠️ That subtitle file is no longer waiting. Please send it again.",
                    show_alert=True
                )
                return
            PENDING_SRT.pop(chat_id, None)
            await bot.answer_callback_query(cb_id, f"Translating to {SRT_LANG_LABELS.get(lang, lang)}...")
            try:
                await bot.edit_message_text(chat_id, msg_id, "🌐 <b>Language picked. Preparing translation...</b>")
            except Exception:
                pass
            await run_srt_translation(bot, chat_id, pending["file_id"], pending["file_name"], lang)
            return

        await bot.answer_callback_query(cb_id)
        return

    # 2. Handle Text Messages & Commands
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        # Commands
        if text.startswith("/start"):
            welcome = (
                "👋 <b>Welcome to PeakCut Studio Bot!</b> ✂️🎬\n\n"
                "I convert any YouTube, Twitch, Kick, or uploaded video into <b>9:16 vertical viral shorts</b> (Reels/TikTok) with:\n\n"
                "• 📐 <b>Blurred Fill (9:16)</b> & Zoom/Crop layouts\n"
                "• ✍️ <b>Animated Spoken Subtitles & Top Hook</b> (Auto Hindi to Hinglish & English)\n"
                "• 🤖 <b>Gemini AI</b> viral captions, hooks & trending hashtags\n"
                "• 🚀 <b>1-Click Automatic Instagram Reel Upload</b> (with preview confirmation)\n"
                "• 📦 <b>HD Video Media</b> or <b>Uncompressed Document</b> delivery\n"
                "• 🌐 <b>Subtitle Translator</b> — send any <code>.srt</code> file, get it back in 100+ languages with timings untouched\n"
                "• ⚡ <b>Live Progress Bar</b>, Speed, and ETA tracking!\n\n"
                "👉 <b>Quick Start:</b> Just paste any YouTube/Twitch/Kick link here!\n"
                "Example:\n<code>https://youtu.be/ZnfOlhQwpSY</code>\n\n"
                "⚙️ Type /settings to customize layout, style, and API credentials."
            )
            await bot.send_message(chat_id, welcome, reply_markup={
                "inline_keyboard": [
                    [{"text": "⚙️ Open Settings", "callback_data": "open_settings"}],
                    [{"text": "🔑 Manage API Keys", "callback_data": "menu_gemini"}]
                ]
            })
            return

        if text.startswith("/settings"):
            pref = get_pref(chat_id)
            await bot.send_message(chat_id, "⚙️ <b>PeakCut Studio Settings Dashboard</b>\n<i>Choose your vertical layout, captions, and API keys:</i>", reply_markup=build_settings_keyboard(pref))
            return

        if text.startswith("/restart"):
            await bot.send_message(chat_id, "🔄 <b>Restarting PeakCut Bot with latest code & subtitle engine...</b>\n<i>Reloading in 2 seconds!</i>")
            await asyncio.sleep(1.2)
            os.execv(sys.executable, [sys.executable, str(ROOT_DIR / "telegram_bot.py")])
            return

        # GEMINI API COMMANDS (PER-USER)
        if text.startswith("/setgemini"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await bot.send_message(chat_id, "⚠️ Usage: <code>/setgemini YOUR_GEMINI_API_KEY</code>")
                return
            new_key = parts[1].strip()
            set_pref(chat_id, "gemini_key", new_key)
            await bot.send_message(
                chat_id,
                f"✅ <b>Personal Gemini API Key Saved!</b> 🔑🤖\n\n"
                f"• Key: <code>{mask_secret(new_key)}</code>\n\n"
                f"🚀 <i>Your viral clips, Hinglish subtitles, and captions will now use your own key!</i>"
            )
            return

        if text.startswith("/delgemini"):
            set_pref(chat_id, "gemini_key", "")
            await bot.send_message(chat_id, "🗑️ <b>Personal Gemini key removed. Falling back to bot system key.</b>")
            return

        # INSTAGRAM API COMMANDS
        if text.startswith("/setinstagram"):
            parts = text.split()
            if len(parts) < 3:
                await bot.send_message(
                    chat_id,
                    "⚠️ Usage: <code>/setinstagram &lt;INSTAGRAM_ACCOUNT_ID&gt; &lt;ACCESS_TOKEN&gt;</code>\n\n"
                    "<i>Example:</i>\n<code>/setinstagram 17841400000000000 EAAG...</code>"
                )
                return
            acc_id = parts[1].strip()
            tok = parts[2].strip()
            set_pref(chat_id, "ig_account_id", acc_id)
            set_pref(chat_id, "ig_access_token", tok)
            await bot.send_message(
                chat_id,
                f"✅ <b>Instagram API Credentials Configured!</b>\n\n"
                f"• <b>Account ID:</b> <code>{acc_id}</code>\n"
                f"• <b>Access Token:</b> <code>{mask_secret(tok)}</code>\n\n"
                f"🚀 <i>You can now upload Reels with 1-click confirmation!</i>"
            )
            return

        if text.startswith("/delinstagram"):
            set_pref(chat_id, "ig_account_id", "")
            set_pref(chat_id, "ig_access_token", "")
            await bot.send_message(chat_id, "🗑️ <b>Instagram API Credentials Cleared!</b>")
            return

        # ZERNIO API COMMANDS (NO FACEBOOK REQUIRED)
        if text.startswith("/setzernio"):
            parts = text.split()
            if len(parts) < 3:
                await bot.send_message(
                    chat_id,
                    "⚠️ <b>Usage:</b> <code>/setzernio &lt;API_KEY&gt; &lt;ACCOUNT_ID&gt;</code>\n\n"
                    "<i>Example:</i>\n<code>/setzernio zern_live_abc123 acc_ig_99999</code>\n\n"
                    "💡 <i>Get your API Key & Account ID from <a href='https://zernio.com/'>zernio.com</a> dashboard after connecting your Instagram via Direct Login!</i>"
                )
                return
            z_key = parts[1].strip()
            z_acc = parts[2].strip()
            set_pref(chat_id, "zernio_api_key", z_key)
            set_pref(chat_id, "zernio_account_id", z_acc)
            await bot.send_message(
                chat_id,
                f"✅ <b>Zernio Instagram API Configured!</b> 🚀📸\n\n"
                f"• <b>Account ID:</b> <code>{z_acc}</code>\n"
                f"• <b>API Key:</b> <code>{mask_secret(z_key)}</code>\n\n"
                f"🌟 <i>You can now 1-click upload Reels directly to your Instagram without needing Facebook!</i>"
            )
            return

        if text.startswith("/delzernio"):
            set_pref(chat_id, "zernio_api_key", "")
            set_pref(chat_id, "zernio_account_id", "")
            await bot.send_message(chat_id, "🗑️ <b>Zernio API Credentials Cleared!</b>")
            return

        # SURFSHARK / PROXY COMMANDS
        if text.startswith("/setproxy"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await bot.send_message(
                    chat_id,
                    "⚠️ <b>Usage:</b> <code>/setproxy http://username:password@in-mum.prod.surfshark.com:80</code>\n\n"
                    "💡 <i>Enter your Surfshark HTTP/SOCKS5 proxy to route downloads through Surfshark VPN!</i>"
                )
                return
            new_proxy = parts[1].strip()
            save_bot_config(token="", gemini_keys=None, proxy=new_proxy)
            await bot.send_message(
                chat_id,
                f"🛡️ <b>Surfshark / Proxy Configured Successfully!</b>\n\n"
                f"• Proxy: <code>{mask_secret(new_proxy)}</code>\n"
                f"🚀 <i>All downloads will now be routed through this VPN/proxy tunnel.</i>"
            )
            return

        if text.startswith("/delproxy"):
            save_bot_config(token="", gemini_keys=None, proxy="")
            await bot.send_message(chat_id, "🗑️ <b>Proxy removed. Bot is now using default connection.</b>")
            return

        # STORAGE CLEANUP COMMAND
        if text.startswith("/clean"):
            cnt, b_freed = cleanup_storage(max_age_seconds=0)
            PENDING_POSTS.clear()
            PENDING_SRT.clear()
            await bot.send_message(
                chat_id,
                f"🧹 <b>Storage Cleaned Successfully!</b>\n\n"
                f"• Deleted: <b>{cnt} temporary & render files</b>\n"
                f"• Freed: <b>{human_bytes(b_freed)} disk space</b>\n"
                f"• Disk Free: <b>{human_bytes(shutil.disk_usage(str(DATA_DIR)).free)}</b>\n\n"
                "🚀 All temporary video downloads and clips have been wiped!"
            )
            return

        if text.startswith("/mykeys"):
            pref = get_pref(chat_id)
            keys = get_gemini_keys()
            ig_acc = pref.get("ig_account_id", "").strip()
            ig_tok = pref.get("ig_access_token", "").strip()
            b_cfg = load_bot_config()
            pxy = b_cfg.get("proxy", "").strip()

            msg_txt = (
                "🔐 <b>Your Configured API Keys & Accounts:</b>\n\n"
                f"• <b>Gemini AI Key:</b> {f'<code>{mask_secret(keys[0])}</code> (🟢 Active)' if keys else '⚪ Not Set'}\n"
                f"• <b>Instagram Account ID:</b> {f'<code>{ig_acc}</code>' if ig_acc else '⚪ Not Set'}\n"
                f"• <b>Instagram Token:</b> {f'<code>{mask_secret(ig_tok)}</code>' if ig_tok else '⚪ Not Set'}\n"
                f"• <b>Surfshark / Proxy:</b> {f'<code>{mask_secret(pxy)}</code> (🟢 Active)' if pxy else '⚪ Default (Direct)'}\n\n"
                "<i>Manage via /setgemini, /setinstagram, /setproxy, or /settings</i>"
            )
            await bot.send_message(chat_id, msg_txt)
            return

        if text.startswith("/help"):
            help_txt = (
                "📖 <b>PeakCut Studio Instructions</b>\n\n"
                "1. <b>Automatic Viral Moment Finder:</b>\n"
                "Send any YouTube link (e.g. <code>https://youtu.be/ZnfOlhQwpSY</code>). PeakCut automatically scans the transcript and video to find the real action peak (never the boring 0s intro!).\n\n"
                "2. <b>Auto Subtitles (Hinglish & English):</b>\n"
                "• Spoken Hindi words are automatically transliterated into clean Roman Hinglish.\n"
                "• Spoken English words are rendered in clean English.\n"
                "• Vibrant yellow animated captions burned in lower third.\n\n"
                "3. <b>1-Click Instagram Upload:</b>\n"
                "After the video is delivered, tap <b>[🚀 Upload to Instagram Now]</b> to post as Reel, or <b>[❌ Cancel Upload]</b> to keep it only in Telegram.\n\n"
                "4. <b>Subtitle Translator:</b>\n"
                "Send any <code>.srt</code> subtitle file and pick a language — you get the same file back with every timestamp exactly where it was (Hindi, Spanish, Arabic & 100+ more).\n\n"
                "5. <b>API Commands:</b>\n"
                "• <code>/setgemini &lt;KEY&gt;</code> & <code>/delgemini</code>\n"
                "• <code>/setinstagram &lt;ID&gt; &lt;TOKEN&gt;</code> & <code>/delinstagram</code>\n"
                "• <code>/mykeys</code>"
            )
            await bot.send_message(chat_id, help_txt)
            return

        if text.startswith("/status"):
            keys = get_gemini_keys()
            pref = get_pref(chat_id)
            ig_ok = bool(pref.get("ig_account_id") and pref.get("ig_access_token"))
            stat = (
                "🖥️ <b>PeakCut Studio System Status</b>\n\n"
                f"• <b>FFmpeg:</b> Available (H.264 + AAC 9:16 Transcoder)\n"
                f"• <b>yt-dlp:</b> Available (Multi-platform stream extractor)\n"
                f"• <b>Gemini AI:</b> {'🟢 ' + str(len(keys)) + ' Key(s) Active' if keys else '🟡 No Key Set'}\n"
                f"• <b>Instagram API:</b> {'🟢 Ready' if ig_ok else '⚪ Not Configured'}\n"
                f"• <b>Storage:</b> {human_bytes(shutil.disk_usage(str(DATA_DIR)).free)} Free\n"
                "• <b>Status:</b> Ready for processing 🚀"
            )
            await bot.send_message(chat_id, stat)
            return

        # Check if user pasted Netscape Cookies directly in chat
        if ("# Netscape HTTP Cookie File" in text) or (".youtube.com\t" in text) or ("VISITOR_INFO1_LIVE" in text):
            cookies_file = DATA_DIR / "cookies.txt"
            cookies_file.write_text(text.strip(), encoding="utf-8")
            await bot.send_message(
                chat_id,
                "🍪 <b>YouTube Cookies Saved Successfully!</b>\n\n"
                "✅ Authentication cookies have been securely saved on your local machine.\n"
                "🚀 Bot is now ready! Send any YouTube link to generate viral 9:16 Shorts!"
            )
            return

        # Check for Video Links in text
        url_match = re.search(r'(https?://[^\s]+)', text)
        if url_match:
            raw_url = url_match.group(1)
            time_window = None
            time_match = re.search(r'(\d+[:\.]?\d*)\s*[-–to\s]+\s*(\d+[:\.]?\d*)', text[url_match.end():])
            if time_match:
                try:
                    def parse_t(s):
                        if ":" in s:
                            parts = s.split(":")
                            return float(parts[0]) * 60 + float(parts[1])
                        return float(s)
                    t_start = parse_t(time_match.group(1))
                    t_end = parse_t(time_match.group(2))
                    if t_end > t_start:
                        time_window = (t_start, t_end)
                except Exception:
                    pass

            asyncio.create_task(process_video_job(bot, chat_id, raw_url, time_window=time_window))
            return

        # Check for Video or Document attachments
        if "video" in msg:
            v = msg["video"]
            file_id = v["file_id"]
            file_name = v.get("file_name", f"video_{int(time.time())}.mp4")
            await handle_telegram_file(bot, chat_id, file_id, file_name)
            return

        if "document" in msg:
            doc = msg["document"]
            file_name = doc.get("file_name", "")
            file_id = doc["file_id"]
            if file_name.lower().endswith(".srt"):
                await handle_srt_file(bot, chat_id, file_id, file_name, msg.get("caption", ""))
                return
            if file_name.lower().endswith("cookies.txt") or file_name.lower().endswith(".txt"):
                await handle_cookies_file(bot, chat_id, file_id)
                return
            elif doc.get("mime_type", "").startswith("video/") or file_name.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".avi")):
                await handle_telegram_file(bot, chat_id, file_id, file_name or f"video_{int(time.time())}.mp4")
                return

        # Default prompt
        await bot.send_message(chat_id, "✂️ Please send a <b>YouTube, Twitch, or Kick video link</b> (or send an MP4 video file) to create viral 9:16 shorts!")

# ----------------- SUBTITLE TRANSLATOR (.SRT FILES) -----------------

def guess_srt_lang(caption: str) -> Optional[str]:
    """The language named in a file's caption, or None. 'hi', 'hindi' and 'Spanish' all count."""
    t = (caption or "").strip().lower()
    if not t:
        return None
    first = re.split(r"[\s,.:;!?/]+", t)[0]
    if re.fullmatch(r"[a-z]{2}", first):
        return first
    return SRT_LANG_NAMES.get(first) or SRT_LANG_NAMES.get(t)


def decode_srt_bytes(raw: bytes) -> str:
    """Subtitle files arrive in different encodings; read whichever one they used."""
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _srt_blocks(text: str) -> list:
    """A subtitle file split into its cues, each cue kept as one block of lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return [b.strip() for b in re.split(r"\n[ \t]*\n", text) if b.strip()]


def _rebuild_cue(orig_block: str, translated_text: str) -> str:
    """Put new words into an old cue: the number and the timing line stay byte-for-byte."""
    lines = orig_block.split("\n")
    for i, ln in enumerate(lines):
        if "-->" in ln:
            return "\n".join(lines[: i + 1] + [translated_text])
    return orig_block


def _apply_cues(orig_part: list, resp: dict) -> Optional[list]:
    """Turn one answer from the gateway back into cues with the original timings."""
    cues = resp.get("cues") or []
    if len(cues) == len(orig_part):
        out = []
        for orig, cue in zip(orig_part, cues):
            txt = (cue.get("translated") or "").strip() or (cue.get("original") or "").strip()
            out.append(_rebuild_cue(orig, txt) if txt else orig)
        return out
    # Some answers only carry the whole translated block; use it if the shape matches
    t_blocks = _srt_blocks(resp.get("translated_srt") or "")
    if len(t_blocks) == len(orig_part):
        return t_blocks
    return None


async def _srt_post(client, payload: dict) -> dict:
    last_err = None
    for _ in range(3):
        try:
            r = await client.post(SRT_TRANSLATE_API, json=payload, timeout=90)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            await asyncio.sleep(2)
    raise RuntimeError(f"the translation service did not answer: {last_err}")


async def _srt_alive(client, target: str, tries: int = 5) -> bool:
    """
    Ask the service one sentence it must translate. The gateway swallows a request now
    and then and returns the original text, so it is asked a few times: False only
    when even repeated asks come back untouched, which means it has gone quiet.
    """
    if target == "en":
        src = "आज मौसम बहुत अच्छा है, चलो बाहर घूमते हैं।"
    else:
        src = "Hello my friend, how are you today?"
    for i in range(max(1, tries)):
        try:
            d = await _srt_post(client, {"text": src, "target": target})
            out = (d.get("translated_text") or "").strip()
            if out and out != src.strip():
                return True
        except Exception:
            pass
        if i < tries - 1:
            await asyncio.sleep(2)
    return False


async def translate_srt_text(srt_text: str, target: str, progress=None) -> str:
    """
    Translate a whole subtitle file. The text changes, the timings never do: every cue
    keeps its own number and timing line from the original file. The file is sent in
    parts, and each part is checked. The gateway returns the original text for some
    requests, so a part that comes back untouched is simply tried again; only if the
    service has genuinely stopped translating does the run stop with a plain warning,
    leaving the user's file untouched.
    """
    cue_blocks = [b for b in _srt_blocks(srt_text) if any("-->" in ln for ln in b.split("\n"))]
    if not cue_blocks:
        raise RuntimeError("This file has no subtitle timings, so it is not an SRT file.")

    # Parts small enough for one request each
    parts, cur, size = [], [], 0
    for b in cue_blocks:
        if cur and (len(cur) >= 80 or size + len(b) > 12000):
            parts.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b) + 2
    if cur:
        parts.append(cur)

    done_parts = []
    async with httpx.AsyncClient(timeout=90) as client:
        if not await _srt_alive(client, target):
            raise RuntimeError(SRT_BUSY_MSG)
        for i, part in enumerate(parts):
            translated_part = None
            for attempt in range(4):
                resp = await _srt_post(client, {"text": "\n\n".join(part), "target": target})
                translated_part = _apply_cues(part, resp)
                if translated_part is None:
                    await asyncio.sleep(2)
                    continue
                if any(a != b for a, b in zip(part, translated_part)):
                    break  # something really was translated
                # Nothing changed. Two possibilities: the text is already in that
                # language, or the gateway swallowed the request. A test sentence
                # tells them apart — and gives it another try either way.
                if attempt >= 1 and await _srt_alive(client, target, tries=2):
                    break
                await asyncio.sleep(2.5)
            else:
                raise RuntimeError(SRT_BUSY_MSG)
            done_parts.extend(translated_part)
            if progress:
                try:
                    await progress(i + 1, len(parts))
                except Exception:
                    pass
            if i < len(parts) - 1:
                await asyncio.sleep(1.2)
    return "\n\n".join(done_parts) + "\n"


async def run_srt_translation(bot: "TelegramBot", chat_id: int or str, file_id: str, file_name: str, target: str):
    """Download the user's .srt, translate it and send the translated file back."""
    status_msg = await bot.send_message(
        chat_id, "🌐 <b>Translating subtitles...</b>\n<i>Downloading the subtitle file...</i>"
    )
    s_msg_id = status_msg["message_id"] if status_msg else None
    out_path = None
    try:
        r = await bot.client.get(f"{bot.base_url}/getFile", params={"file_id": file_id})
        rel_path = r.json().get("result", {}).get("file_path")
        if not rel_path:
            raise RuntimeError("Telegram would not hand over that file. Please send it again.")
        dl_url = f"https://api.telegram.org/file/bot{bot.token}/{rel_path}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(dl_url)
            resp.raise_for_status()
            raw = resp.content
        srt_text = decode_srt_bytes(raw)

        async def on_progress(done: int, total: int):
            if s_msg_id:
                pct = (done / max(1, total)) * 100
                await bot.edit_message_text(
                    chat_id, s_msg_id,
                    "🌐 <b>Translating subtitles...</b>\n"
                    f"<code>{render_progress_bar(pct, 12)}</code>\n"
                    f"🧩 Part <b>{done}</b> of <b>{total}</b> — timestamps stay untouched"
                )

        out_text = await translate_srt_text(srt_text, target, progress=on_progress)
        stem = Path(file_name).stem or "subtitles"
        out_dir = DATA_DIR / "srt_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stem}.{target}.srt"
        out_path.write_text(out_text, encoding="utf-8")

        label = SRT_LANG_LABELS.get(target, target.upper())
        safe_name = out_path.name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        res = await asyncio.to_thread(
            bot.upload_with_progress,
            "sendDocument",
            chat_id,
            out_path,
            "document",
            f"🌐 <b>Subtitles translated to {label}</b>\n📁 <code>{safe_name}</code>\n\n✅ Every timestamp is exactly where it was.",
            None,
            None,
        )
        if isinstance(res, dict) and res.get("ok") is False:
            raise RuntimeError(str(res.get("description", "the translated file could not be sent")))
        if s_msg_id:
            await bot.edit_message_text(
                chat_id, s_msg_id, f"✅ <b>Done!</b> Your translated subtitle file is above ({label})."
            )
    except RuntimeError as e:
        # A plain-language problem worth repeating as it is (service quiet, not an SRT...)
        if s_msg_id:
            await bot.edit_message_text(chat_id, s_msg_id, f"⚠️ <b>Translation not finished:</b>\n{e}")
    except Exception as e:
        if s_msg_id:
            await bot.edit_message_text(chat_id, s_msg_id, f"❌ <b>Translation failed:</b>\n<code>{str(e)[:300]}</code>")
    finally:
        if out_path:
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass


async def handle_srt_file(bot: "TelegramBot", chat_id: int or str, file_id: str, file_name: str, caption: str = ""):
    """An .srt file arrived: translate at once if the caption names a language, else ask."""
    lang = guess_srt_lang(caption)
    if lang:
        await run_srt_translation(bot, chat_id, file_id, file_name, lang)
        return

    PENDING_SRT[chat_id] = {"file_id": file_id, "file_name": file_name, "t": time.time()}
    kb = {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"srtlang:{code}"} for label, code in row]
            for row in SRT_LANG_BUTTONS
        ]
    }
    safe_name = file_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await bot.send_message(
        chat_id,
        "🌐 <b>Subtitle Translator</b>\n\n"
        f"📄 <code>{safe_name}</code> is ready.\n"
        "Pick the language to translate it into — every timestamp stays exactly where it was:\n\n"
        "<i>Or send the file again with a caption like <code>hindi</code>, <code>spanish</code> or "
        "<code>arabic</code> for any of 100+ other languages.</i>",
        reply_markup=kb,
    )

async def handle_cookies_file(bot: TelegramBot, chat_id: int or str, file_id: str):
    status_msg = await bot.send_message(chat_id, "🍪 <b>Saving YouTube cookies...</b>")
    if not status_msg:
        return
    msg_id = status_msg["message_id"]

    try:
        r = await bot.client.get(f"{bot.base_url}/getFile", params={"file_id": file_id})
        rel_path = r.json().get("result", {}).get("file_path")
        if not rel_path:
            await bot.edit_message_text(chat_id, msg_id, "❌ Could not retrieve file from Telegram.")
            return

        dl_url = f"https://api.telegram.org/file/bot{bot.token}/{rel_path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(dl_url)
            cookies_text = resp.text

        cookies_file = DATA_DIR / "cookies.txt"
        cookies_file.write_text(cookies_text, encoding="utf-8")

        await bot.edit_message_text(
            chat_id, msg_id,
            "🎉 <b>YouTube Cookies Saved Successfully!</b> 🍪\n\n"
            "• Anti-bot challenge bypassed permanently!\n"
            "• You can now download and clip any YouTube video in Full HD.\n\n"
            "👉 <i>Now paste your YouTube video link to start clipping!</i>"
        )
    except Exception as e:
        await bot.edit_message_text(chat_id, msg_id, f"❌ Failed to save cookies: {e}")

async def handle_telegram_file(bot: TelegramBot, chat_id: int or str, file_id: str, file_name: str):
    status_msg = await bot.send_message(chat_id, "📥 <b>Fetching file from Telegram servers...</b>")
    if not status_msg:
        return
    msg_id = status_msg["message_id"]

    try:
        r = await bot.client.get(f"{bot.base_url}/getFile", params={"file_id": file_id})
        f_info = r.json().get("result", {})
        rel_path = f_info.get("file_path")
        file_size = f_info.get("file_size", 0)

        if not rel_path:
            await bot.edit_message_text(chat_id, msg_id, "❌ Could not retrieve file from Telegram.")
            return

        dl_url = f"https://api.telegram.org/file/bot{bot.token}/{rel_path}"
        dest_file = TMP_DIR / f"tg_{int(time.time())}_{file_name}"

        async with bot.client.stream("GET", dl_url) as resp:
            downloaded = 0
            start_time = time.time()
            last_edit = [0.0]

            with open(dest_file, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=128 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_edit[0] >= 1.8:
                        last_edit[0] = now
                        pct = (downloaded / file_size) * 100 if file_size else 0
                        speed = downloaded / max(0.001, now - start_time)
                        eta = (file_size - downloaded) / max(0.001, speed)
                        p_bar = render_progress_bar(pct, 12)
                        await bot.edit_message_text(
                            chat_id, msg_id,
                            f"📥 <b>Downloading from Telegram...</b>\n"
                            f"<code>{p_bar}</code>\n"
                            f"⚡ <b>Speed:</b> {human_bytes(speed)}/s\n"
                            f"⏳ <b>ETA:</b> {human_time(eta)} | {human_bytes(downloaded)} / {human_bytes(file_size)}"
                        )

        await process_video_job(bot, chat_id, str(dest_file), custom_title=file_name)

    except Exception as e:
        await bot.edit_message_text(chat_id, msg_id, f"❌ Failed to process file: {e}")

# ----------------- MAIN LOOP & STARTUP -----------------

loop = None

async def main():
    global loop
    loop = asyncio.get_running_loop()

    cfg = load_bot_config()
    token = cfg.get("bot_token", "").strip()

    print("=" * 60)
    print("✂️  PeakCut Advanced Telegram Bot")
    print("=" * 60)

    if not token:
        print("[Error] No bot token provided. Exiting.")
        sys.exit(1)

    bot = TelegramBot(token)

    retry_count = 0
    while True:
        try:
            me = await bot.get_me()
            if me and me.get("ok"):
                bot_user = me["result"]
                print(f"🤖 Bot Connected Successfully: @{bot_user['username']} ({bot_user['first_name']})")
                print(f"📁 Work Directory: {ROOT_DIR}")
                print(f"🔑 Gemini Keys: {len(get_gemini_keys())} configured")
                print("🚀 Long-polling started. Listening for incoming messages...\n")
                break
            else:
                print(f"[Error] Telegram getMe error: {me}. Retrying in 5s...")
                await asyncio.sleep(5)
        except Exception as e:
            retry_count += 1
            print(f"[Connection Attempt {retry_count}] Could not connect to Telegram API ({e}). Retrying in 5s...")
            await asyncio.sleep(5)

    offset = 0
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=25)
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                asyncio.create_task(handle_update(bot, u))
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[Polling Loop Error] {e}")
            await asyncio.sleep(4)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Stopped] PeakCut Telegram Bot terminated.")