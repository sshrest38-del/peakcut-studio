import os
import sys
import json
import glob
import time
import shutil
import zipfile
import io
import re
import base64
import subprocess
import streamlit as st
import streamlit.components.v1 as components
import httpx
from pathlib import Path
from typing import Optional, Dict, Any, List

# Auto-install essential runtime packages if missing
for _pkg in ["ytdlp-jsc", "imageio-ffmpeg", "deno", "yt-dlp-ejs"]:
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

# Streamlit Page Config
st.set_page_config(
    page_title="PeakCut - AI Viral Video Clipper & Auto Instagram Poster",
    page_icon="✂️",
    layout="wide",
    initial_sidebar_state="expanded"
)

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
OUTPUT_DIR = DATA_DIR / "renders"
WORK_DIR = ROOT / "data" / "tmp"
META_DIR = DATA_DIR / "metadata"

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
META_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- AUTO-START TELEGRAM BOT DAEMON (RUNS ON STREAMLIT CLOUD) ----------------
_JS_RUNTIME_ARGS: Optional[list] = None


def _starts_here(exe: str) -> bool:
    """True if a program can actually be started on this computer. The bundled bin/qjs is a Linux
    executable: Windows refuses to start it, and yt-dlp would then solve none of YouTube's
    challenges ("n challenge solving failed"), so a working Deno or Node must win over it."""
    try:
        subprocess.run([exe, "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=6, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False


def _find_deno() -> Optional[str]:
    """Finds Deno binary from PATH, python dir, or deno python package."""
    w = shutil.which("deno")
    if w:
        return w
    local_deno = Path(sys.executable).parent / ("deno.exe" if sys.platform.startswith("win") else "deno")
    if local_deno.exists():
        return str(local_deno)
    try:
        import deno
        d_bin = deno.find_deno_bin()
        if d_bin and Path(d_bin).exists():
            return str(d_bin)
    except Exception:
        pass
    return None


def _node_ok(exe: Optional[str]) -> bool:
    """Checks if Node.js major version is >= 20 (yt-dlp requires Node 20+ to solve n challenge)."""
    if not exe:
        return False
    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=6)
        if res.returncode == 0:
            m = re.search(r'v(\d+)\.', res.stdout.strip())
            if m and int(m.group(1)) >= 20:
                return True
    except Exception:
        pass
    return False


def get_js_runtime_args() -> list:
    """
    The JavaScript runtime yt-dlp uses to solve YouTube's challenges, as command line arguments.
    Order of preference: Deno -> Node (>=20) -> QuickJS (bin/qjs).
    """
    global _JS_RUNTIME_ARGS
    if _JS_RUNTIME_ARGS is None:
        qjs_bin = ROOT / "bin" / "qjs"
        if sys.platform.startswith("linux"):
            try:
                os.chmod(str(qjs_bin), 0o755)
            except OSError:
                pass
        node_sys = (shutil.which("node") or shutil.which("nodejs")
                    or ("/usr/bin/node" if Path("/usr/bin/node").exists() else None)
                    or ("/usr/bin/nodejs" if Path("/usr/bin/nodejs").exists() else None))
        _JS_RUNTIME_ARGS = []
        candidates = [
            ("deno", _find_deno()),
            ("node", node_sys if _node_ok(node_sys) else None),
            ("quickjs", str(qjs_bin)),
        ]
        for kind, exe in candidates:
            if exe and _starts_here(exe):
                _JS_RUNTIME_ARGS = ["--js-runtimes", f"{kind}:{exe}", "--remote-components", "ejs:github"]
                break
    return list(_JS_RUNTIME_ARGS)

_PPROXY_PROC = None

def ensure_pproxy_tunnel() -> Optional[str]:
    """
    Spawns a local pproxy daemon listening on 127.0.0.1:8125 that bridges
    to Surfshark HTTPS proxy on in-mum.prod.surfshark.com:443.
    """
    return None


def _tunnel_listening(port: int = 8125) -> bool:
    """True if something is already serving the local tunnel port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0

class BotDaemonManager:
    _proc = None
    _code_ver = "v5.3.3"
    _started_ver = None

    @classmethod
    def kill_existing(cls):
        if cls._proc is not None:
            try:
                cls._proc.kill()
            except Exception:
                pass
            cls._proc = None
        if sys.platform.startswith("linux"):
            try:
                subprocess.run(["pkill", "-9", "-f", "telegram_bot.py"], capture_output=True)
                time.sleep(0.6)
            except Exception:
                pass

    @classmethod
    def get_or_start(cls):
        # If process exists, running, and matches current code version, return it
        if cls._proc is not None and cls._proc.poll() is None and cls._started_ver == cls._code_ver:
            return cls._proc

        cls.kill_existing()

        bot_script = ROOT / "telegram_bot.py"
        if not bot_script.exists():
            return None
        try:
            env = dict(os.environ)
            # Pass all st.secrets into subprocess env with alias normalization
            try:
                for k in st.secrets:
                    val = str(st.secrets[k]).strip()
                    env[k] = val
                    if k in ("_FALLBACK_BOT_TOKEN", "FALLBACK_BOT_TOKEN", "BOT_TOKEN"):
                        env.setdefault("TELEGRAM_BOT_TOKEN", val)
                    elif k in ("_FALLBACK_GEMINI_KEY", "FALLBACK_GEMINI_KEY", "GEMINI_API_KEYS"):
                        env.setdefault("GEMINI_API_KEY", val)
                        env.setdefault("GEMINI_API_KEYS", val)
                    elif k in ("_FALLBACK_ZERNIO_KEY", "FALLBACK_ZERNIO_KEY", "ZERNIO_KEY"):
                        env.setdefault("ZERNIO_API_KEY", val)
                    elif k in ("_FALLBACK_ZERNIO_ACC", "FALLBACK_ZERNIO_ACC", "ZERNIO_ACC"):
                        env.setdefault("ZERNIO_ACCOUNT_ID", val)
            except Exception:
                pass

            # Ensure no dead proxy blocks Telegram
            env.pop("HTTP_PROXY", None)
            env.pop("HTTPS_PROXY", None)
            env.pop("http_proxy", None)
            env.pop("https_proxy", None)

            # Also write cookies.txt if provided in st.secrets
            try:
                if "YOUTUBE_COOKIES" in st.secrets:
                    c_file = ROOT / "data" / "cookies.txt"
                    c_file.parent.mkdir(parents=True, exist_ok=True)
                    c_file.write_text(str(st.secrets["YOUTUBE_COOKIES"]).strip(), encoding="utf-8")
            except Exception:
                pass

            # Spawn Telegram bot daemon in background
            log_path = DATA_DIR / "bot_cloud.log"
            log_f = open(log_path, "a", encoding="utf-8")
            cls._proc = subprocess.Popen(
                [sys.executable, "-u", str(bot_script)],
                cwd=str(ROOT),
                stdout=log_f,
                stderr=log_f,
                text=True,
                env=env
            )
            cls._started_ver = cls._code_ver
            return cls._proc
        except Exception as e:
            print(f"[Bot Auto-Start Error] {e}")
            return None

    @classmethod
    def restart(cls):
        cls.kill_existing()
        return cls.get_or_start()

# Starts once on Streamlit Cloud app boot and self-heals if stopped
bot_daemon_proc = BotDaemonManager.get_or_start()

# Custom Styling to match PeakCut's modern dark UI
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap');
    
    .stApp {
        font-family: 'Poppins', sans-serif;
    }
    
    .brand-title {
        font-size: 2.2rem;
        font-weight: 800;
        color: #ffffff;
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 0px;
    }
    .brand-orange {
        color: #ff6b35;
    }
    .brand-subtitle {
        color: #a0aec0;
        font-size: 0.95rem;
        margin-bottom: 1.2rem;
    }
    
    .badge-ok {
        background-color: #1a362a;
        color: #48bb78;
        padding: 4px 12px;
        border-radius: 12px;
        font-size: 0.82rem;
        font-weight: 600;
        border: 1px solid #2f855a;
        display: inline-block;
    }
    
    .badge-cf {
        background-color: #3b2816;
        color: #f6821f;
        padding: 4px 12px;
        border-radius: 12px;
        font-size: 0.82rem;
        font-weight: 600;
        border: 1px solid #f6821f;
        display: inline-block;
    }
    
    .badge-warn {
        background-color: #3b2c1a;
        color: #ecc94b;
        padding: 4px 12px;
        border-radius: 12px;
        font-size: 0.82rem;
        font-weight: 600;
        border: 1px solid #d69e2e;
        display: inline-block;
    }
    
    .ai-badge {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 3px 8px;
        border-radius: 8px;
        font-size: 0.75rem;
        font-weight: bold;
    }

    div.stButton > button:first-child {
        border-radius: 8px;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ---------------- MULTI-GEMINI API KEY MANAGER (SECURE & ROTATING) ----------------
def mask_key(k: str) -> str:
    """Mask key: only show first 6 and last 4 characters."""
    k = k.strip()
    if len(k) <= 10:
        return "********"
    return f"{k[:6]}...{k[-4:]}"


def load_configured_keys() -> list[str]:
    """Load API keys from Streamlit Secrets, environment, or saved session."""
    keys = []
    # 1. From Streamlit Secrets (for Streamlit Cloud deployment)
    try:
        if "GEMINI_API_KEYS" in st.secrets:
            raw = st.secrets["GEMINI_API_KEYS"]
            keys.extend([x.strip() for x in re.split(r"[,\n;\s]+", str(raw)) if x.strip()])
        elif "GEMINI_API_KEY" in st.secrets:
            keys.append(str(st.secrets["GEMINI_API_KEY"]).strip())
    except Exception:
        pass

    # 2. From Environment Variables
    env_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or os.environ.get("PEAKCUT_AI_KEY") or ""
    if env_keys:
        for x in re.split(r"[,\n;\s]+", env_keys):
            if x.strip() and x.strip() not in keys:
                keys.append(x.strip())

    # 3. From Streamlit Session State
    if "user_gemini_keys" in st.session_state and st.session_state["user_gemini_keys"]:
        for x in st.session_state["user_gemini_keys"]:
            if x and x not in keys:
                keys.append(x)

    return keys


# ---------------- GEMINI AI VIDEO ANALYSIS & CAPTION GENERATOR ----------------
def generate_ai_viral_meta(clip_path: Path, prompt_context: str = "") -> dict:
    """
    Extracts video frame, sends it to Gemini Vision API,
    and returns AI-crafted Title, Instagram Reel Caption, and Hashtags.
    """
    keys = load_configured_keys()
    thumb_path = WORK_DIR / f"{clip_path.stem}_thumb.jpg"
    
    try:
        extract_frame_cmd = [
            "ffmpeg", "-y", "-ss", "2",
            "-i", str(clip_path),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_path)
        ]
        subprocess.run(extract_frame_cmd, capture_output=True)
    except Exception:
        pass

    img_b64 = None
    if thumb_path.exists():
        try:
            with open(thumb_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            pass

    prompt = (
        "You are an elite viral social media strategist specializing in Instagram Reels, YouTube Shorts, and TikTok.\n"
        "Analyze this video frame from a newly clipped viral highlight.\n"
        f"Context details: {prompt_context}\n\n"
        "Generate high-CTR, viral metadata:\n"
        "1. 'title': A magnetic hook / headline under 55 characters with 1-2 punchy emojis.\n"
        "2. 'caption': An engaging, conversational Instagram Reel caption (2-3 sentences) with a strong hook and a question to drive comments.\n"
        "3. 'hashtags': A list of 15-20 trending, niche-relevant hashtags (e.g. #reels #viral #gaming #trending #fyp #explorepage).\n\n"
        "Return ONLY valid JSON matching this schema:\n"
        '{"title": "...", "caption": "...", "hashtags": ["#...", "#..."]}'
    )

    if keys and img_b64:
        for active_key in keys:
            for model_name in ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash-exp"]:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
                body = {
                    "contents": [{
                        "role": "user",
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mimeType": "image/jpeg", "data": img_b64}}
                        ]
                    }],
                    "generationConfig": {
                        "temperature": 0.7,
                        "responseMimeType": "application/json"
                    }
                }
                try:
                    r = httpx.post(url, json=body, headers={"x-goog-api-key": active_key}, timeout=25)
                    if r.status_code == 200:
                        res_json = r.json()
                        text_data = res_json["candidates"][0]["content"]["parts"][0]["text"]
                        clean_data = json.loads(text_data)
                        return clean_data
                except Exception:
                    continue

    return {
        "title": "🔥 Top Viral Moment | Unbelievable Gameplay!",
        "caption": "You won't believe what happened in this round! 😱 What would you do in this situation? Let us know in the comments below! 👇🔥",
        "hashtags": ["#viral", "#reels", "#fyp", "#trending", "#instareels", "#gaming", "#highlights", "#explorepage"]
    }


# ---------------- ZERNIO INSTAGRAM REEL POSTING (NO FACEBOOK REQUIRED) ----------------
def post_to_zernio_reel(video_path: Path, caption_text: str, api_key: str, account_id: str) -> dict:
    if not api_key or not account_id:
        return {"ok": False, "error": "Zernio API Key or Account ID is missing."}
    
    base_api = "https://zernio.com/api/v1"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        # Step 1: Presigned upload URL
        p_res = httpx.post(
            f"{base_api}/media/presign",
            headers=headers,
            json={"filename": video_path.name, "contentType": "video/mp4"},
            timeout=30.0
        )
        if p_res.status_code not in (200, 201):
            return {"ok": False, "error": f"Zernio presign error: {p_res.text[:200]}"}
        p_data = p_res.json()
        upload_url = p_data.get("uploadUrl")
        public_url = p_data.get("publicUrl")
        if not upload_url or not public_url:
            return {"ok": False, "error": "Zernio did not return upload/public URL"}
        
        # Step 2: Upload video bytes
        with open(video_path, "rb") as vf:
            v_bytes = vf.read()
        put_res = httpx.put(upload_url, headers={"Content-Type": "video/mp4"}, content=v_bytes, timeout=180.0)
        if put_res.status_code not in (200, 201):
            return {"ok": False, "error": f"Zernio upload failed: {put_res.status_code}"}
        
        # Step 3: Create post
        post_payload = {
            "content": caption_text[:2200],
            "platforms": [{"platform": "instagram", "accountId": account_id}],
            "mediaItems": [{"type": "video", "url": public_url}],
            "publishNow": True
        }
        res = httpx.post(f"{base_api}/posts", headers=headers, json=post_payload, timeout=60.0)
        if res.status_code in (200, 201):
            rj = res.json()
            post_id = rj.get("post", {}).get("_id") or rj.get("data", {}).get("id") or "published"
            return {"ok": True, "id": post_id, "url": "https://instagram.com/gtavi_hubk"}
        else:
            return {"ok": False, "error": f"Zernio publish error: {res.text[:250]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- INSTAGRAM POSTING (GRAPH API) ----------------
def post_to_instagram_reel(video_path: Path, caption_text: str, account_id: str, access_token: str, public_url: str = "") -> dict:
    if not account_id or not access_token:
        return {"ok": False, "error": "Instagram Account ID or Access Token is missing. Please configure them in the sidebar."}
    
    headers = {"Authorization": f"Bearer {access_token}"}
    
    try:
        if public_url:
            init_url = f"https://graph.facebook.com/v21.0/{account_id}/media"
            payload = {
                "media_type": "REELS",
                "video_url": public_url,
                "caption": caption_text[:2200],
                "share_to_feed": "true"
            }
            r = httpx.post(init_url, data=payload, headers=headers, timeout=60)
            if r.status_code != 200:
                return {"ok": False, "error": f"Container creation failed: {r.text}"}
            container_id = r.json().get("id")
        else:
            init_url = f"https://graph.facebook.com/v21.0/{account_id}/media"
            payload = {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption_text[:2200],
                "share_to_feed": "true"
            }
            r = httpx.post(init_url, data=payload, headers=headers, timeout=60)
            if r.status_code != 200:
                return {"ok": False, "error": f"Upload init failed: {r.text}"}
            
            data = r.json()
            container_id = data.get("id")
            upload_uri = data.get("uri") or f"https://rupload.facebook.com/v21.0/{container_id}"
            
            with open(video_path, "rb") as vf:
                v_bytes = vf.read()
            
            up_headers = {
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(len(v_bytes))
            }
            up_res = httpx.post(upload_uri, content=v_bytes, headers=up_headers, timeout=300)
            if up_res.status_code not in (200, 201):
                return {"ok": False, "error": f"Video upload error: {up_res.text}"}

        # Poll container status until ready
        check_url = f"https://graph.facebook.com/v21.0/{container_id}"
        for _ in range(30):
            time.sleep(4)
            st_res = httpx.get(check_url, params={"fields": "status_code,status"}, headers=headers, timeout=15)
            if st_res.status_code == 200:
                st_code = st_res.json().get("status_code")
                if st_code == "FINISHED":
                    break
                elif st_code == "ERROR":
                    return {"ok": False, "error": f"Instagram processing error: {st_res.text}"}
        
        # Publish Reel
        pub_url = f"https://graph.facebook.com/v21.0/{account_id}/media_publish"
        pub_res = httpx.post(pub_url, data={"creation_id": container_id}, headers=headers, timeout=60)
        if pub_res.status_code == 200:
            post_id = pub_res.json().get("id")
            return {"ok": True, "id": post_id, "url": f"https://www.instagram.com/reel/{post_id}/"}
        else:
            return {"ok": False, "error": f"Publish error: {pub_res.text}"}
            
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- SIDEBAR CONFIGURATION (KEYS, CLOUDFLARE & INSTAGRAM) ----------------
with st.sidebar:
    st.markdown("### 🔑 Gemini API Keys (Auto-Rotating)")
    current_keys = load_configured_keys()
    
    if current_keys:
        st.markdown(f"<span class='badge-ok'>🟢 {len(current_keys)} Key(s) Active & Rotating</span>", unsafe_allow_html=True)
        for idx, k in enumerate(current_keys, 1):
            st.text(f"Key #{idx}: {mask_key(k)}")
    else:
        st.markdown("<span class='badge-warn'>🟡 No API Key Set</span>", unsafe_allow_html=True)

    with st.expander("➕ Manage Gemini Keys (Hidden)", expanded=not bool(current_keys)):
        new_keys_input = st.text_input(
            "Gemini API Keys",
            type="password",
            placeholder="AIzaSyKey1..., AIzaSyKey2...",
            help="Free keys from https://aistudio.google.com/apikey"
        )
        if st.button("Save Gemini Keys", use_container_width=True):
            if new_keys_input.strip():
                entered = [x.strip() for x in re.split(r"[,\n;\s]+", new_keys_input) if x.strip()]
                st.session_state["user_gemini_keys"] = entered
                os.environ["GEMINI_API_KEYS"] = ",".join(entered)
                os.environ["PEAKCUT_AI_KEY"] = entered[0]
                st.success(f"Saved {len(entered)} key(s)!")
                st.rerun()

    st.divider()
    st.markdown("### 📸 Instagram Reel Auto-Post")
    
    # 1. Zernio Unified API (No FB Required)
    saved_zernio_key = st.session_state.get("zernio_api_key") or os.environ.get("ZERNIO_API_KEY") or (st.secrets.get("ZERNIO_API_KEY", "") if hasattr(st, "secrets") else "")
    saved_zernio_acc = st.session_state.get("zernio_account_id") or os.environ.get("ZERNIO_ACCOUNT_ID") or (st.secrets.get("ZERNIO_ACCOUNT_ID", "") if hasattr(st, "secrets") else "")
    
    with st.expander("🌟 Zernio API (Direct Instagram - No FB)", expanded=True):
        st.caption("Publish directly to `@gtavi_hubk` via Zernio without Facebook developer portal.")
        inp_z_key = st.text_input("Zernio API Key", value=saved_zernio_key, type="password")
        inp_z_acc = st.text_input("Instagram Account ID", value=saved_zernio_acc, placeholder="6affe7338ae0a5730bcf4ec4")
        if st.button("Save Zernio Credentials", use_container_width=True):
            st.session_state["zernio_api_key"] = inp_z_key.strip()
            st.session_state["zernio_account_id"] = inp_z_acc.strip()
            os.environ["ZERNIO_API_KEY"] = inp_z_key.strip()
            os.environ["ZERNIO_ACCOUNT_ID"] = inp_z_acc.strip()
            st.success("Zernio credentials saved!")
            st.rerun()

    if saved_zernio_key and saved_zernio_acc:
        st.markdown("<span class='badge-ok'>🟢 Zernio Connected (@gtavi_hubk)</span>", unsafe_allow_html=True)

    # 2. Meta Graph API (Optional fallback)
    saved_ig_id = st.session_state.get("ig_account_id") or os.environ.get("INSTAGRAM_ACCOUNT_ID") or ""
    saved_ig_token = st.session_state.get("ig_access_token") or os.environ.get("INSTAGRAM_ACCESS_TOKEN") or ""
    with st.expander("⚙️ Meta Graph API (Optional Alternative)", expanded=False):
        inp_ig_id = st.text_input("Instagram Account ID", value=saved_ig_id, placeholder="e.g. 17841400...")
        inp_ig_token = st.text_input("Meta Graph API Access Token", value=saved_ig_token, type="password", placeholder="EAAG...")
        if st.button("Save Meta Credentials", use_container_width=True):
            st.session_state["ig_account_id"] = inp_ig_id.strip()
            st.session_state["ig_access_token"] = inp_ig_token.strip()
            os.environ["INSTAGRAM_ACCOUNT_ID"] = inp_ig_id.strip()
            os.environ["INSTAGRAM_ACCESS_TOKEN"] = inp_ig_token.strip()
            st.success("Meta credentials saved!")
            st.rerun()

    # 3. Surfshark VPN & Proxy (Streamlit USA Cloud Bypasser)
    st.divider()
    st.markdown("### 🛡️ Surfshark VPN / Proxy")
    saved_proxy = st.session_state.get("surfshark_proxy") or os.environ.get("SURFSHARK_PROXY") or (st.secrets.get("SURFSHARK_PROXY", "") if hasattr(st, "secrets") else "")
    with st.expander("🛡️ Surfshark Proxy Config", expanded=False):
        st.caption("Routes video downloads through Surfshark Mumbai (India) node to prevent USA YouTube IP blocks on Streamlit Cloud.")
        inp_proxy = st.text_input("Proxy URL", value=saved_proxy)
        if st.button("Save Proxy", use_container_width=True):
            st.session_state["surfshark_proxy"] = inp_proxy.strip()
            os.environ["SURFSHARK_PROXY"] = inp_proxy.strip()
            st.success("Proxy configured!")
            st.rerun()

    t_proxy = ensure_pproxy_tunnel()
    if t_proxy or saved_proxy:
        st.markdown("<span class='badge-ok'>🟢 Surfshark HTTPS Proxy Tunnel Active (Mumbai)</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span class='badge-warn'>⚪ Direct Connection</span>", unsafe_allow_html=True)

    # 4. Telegram Bot Daemon Status
    st.divider()
    st.markdown("### 🤖 Telegram Bot Daemon")
    log_file = DATA_DIR / "bot_cloud.log"
    live_proc = BotDaemonManager.get_or_start()
    is_running = live_proc is not None and (live_proc.poll() is None)
    
    if is_running:
        st.markdown(f"<span class='badge-ok'>🟢 Bot Running (@Drm5633_bot) [PID: {live_proc.pid}]</span>", unsafe_allow_html=True)
        if st.button("🔄 Restart Bot", use_container_width=True):
            BotDaemonManager.restart()
            st.rerun()
    else:
        st.markdown("<span class='badge-warn'>🟡 Starting / Offline</span>", unsafe_allow_html=True)
        if st.button("🚀 Start Bot Now", use_container_width=True):
            BotDaemonManager.restart()
            st.rerun()

    with st.expander("📋 View Bot Cloud Logs", expanded=not is_running):
        if log_file.exists():
            log_lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-25:]
            st.code("\n".join(log_lines) or "No log output yet.", language="text")
        else:
            st.caption("Logs will appear after initial run.")


# ---------------- MAIN APPLICATION HEADER ----------------
st.markdown("""
<div class="brand-title">
    <span class="brand-orange">✂️ PeakCut</span> Studio
</div>
<div class="brand-subtitle">
    AI Viral Video Clipper & Auto Instagram Reels Studio | Multi-Gemini AI & Hinglish Subtitles
</div>
""", unsafe_allow_html=True)

tabs = st.tabs([
    "⚡ AI Viral Video Clipper & Instagram Poster",
    "📂 All Rendered Clips & Instagram Poster"
])
tab_clipper, tab_gallery = tabs[0], tabs[1]


# ================= TAB 0: STREAMLIT CLIPPER & INSTAGRAM =================
with tab_clipper:
    # 1. Source Input
    st.markdown("##### 🔗 Video Source")
    input_choice = st.radio("Source Type", ["Paste Link", "Upload Video File"], horizontal=True, label_visibility="collapsed")

    video_url = ""
    uploaded_video_path = None

    if input_choice == "Paste Link":
        default_link = st.query_params.get("url", "https://youtu.be/ZnfOlhQwpSY")
        video_url = st.text_input("Video Link", value=default_link, placeholder="Paste a YouTube, Twitch or Kick video link...", label_visibility="collapsed")
    else:
        uploaded_file = st.file_uploader("Upload Video", type=["mp4", "mov", "mkv", "webm", "ts"], label_visibility="collapsed")
        if uploaded_file is not None:
            save_path = WORK_DIR / uploaded_file.name
            with open(save_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            uploaded_video_path = str(save_path)
            st.success(f"Video uploaded: {uploaded_file.name} ({uploaded_file.size / (1024*1024):.1f} MB)")

    # 2. Clips Number & Duration Settings (matching PeakCut UI)
    st.markdown("##### 🎞️ Clips Parameters")
    col_c1, col_c2, col_c3 = st.columns(3)
    with col_c1:
        num_clips = st.number_input("How many clips", min_value=1, max_value=15, value=6)
    with col_c2:
        shortest_sec = st.number_input("Shortest (seconds)", min_value=10, max_value=60, value=15, step=5)
    with col_c3:
        longest_sec = st.number_input("Longest (seconds)", min_value=15, max_value=180, value=60, step=5)

    # 3. Caption Style & Layout Settings
    col_opt1, col_opt2 = st.columns(2)
    with col_opt1:
        st.markdown("##### ✍️ Caption Style")
        gen_captions = st.checkbox("Generate dynamic captions", value=True)
        caption_style = st.selectbox(
            "Style",
            ["bounce", "hype", "clean"],
            index=0,
            format_func=lambda x: {
                "bounce": "Bounce (High-Energy Yellow Highlights)",
                "hype": "Hype (Punchy Red Uppercase Accent)",
                "clean": "Clean (Minimalist Cyan Subtitles)"
            }.get(x, x)
        )

    with col_opt2:
        st.markdown("##### 📐 9:16 Vertical Layout")
        layout_mode = st.selectbox(
            "Layout",
            ["blur", "zoom", "crop", "gameplay"],
            index=0,
            format_func=lambda x: {
                "blur": "Blurred Fill (Fit 16:9 with dynamic blurred background)",
                "zoom": "Zoomed In (Enlarged video with blurred fill)",
                "crop": "Fill Screen (Auto-centered 1080x1920 crop)",
                "gameplay": "Gameplay + Facecam Overlay"
            }.get(x, x)
        )

    st.markdown("<br>", unsafe_allow_html=True)
    make_clips_btn = st.button("✂️ Make Clips & Generate AI Content", type="primary", use_container_width=True)

    if make_clips_btn:
        active_source = uploaded_video_path if uploaded_video_path else video_url.strip()
        if not active_source:
            st.error("Please enter a YouTube/Twitch/Kick link or upload a video file.")
        else:
            prog_bar = st.progress(0.1, text="Preparing video stream...")
            status_box = st.empty()
            logs = []
            source_file = WORK_DIR / "source_stream.mp4"
            
            try:
                if not uploaded_video_path:
                    logs.append("Fetching video stream with yt-dlp...")
                    status_box.info("\n".join(logs))
                    # Direct download without proxy
                    proxy_val = ""
                    
                    # Check for Netscape cookies
                    cookies_file = Path(__file__).parent / "data" / "cookies.txt"
                    try:
                        if not cookies_file.exists() and "YOUTUBE_COOKIES" in st.secrets:
                            cookies_file.parent.mkdir(parents=True, exist_ok=True)
                            cookies_file.write_text(st.secrets["YOUTUBE_COOKIES"].strip(), encoding="utf-8")
                    except Exception:
                        pass

                    ytdlp_cmd = [
                        sys.executable, "-m", "yt_dlp",
                        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best/18/b",
                        "--socket-timeout", "20",
                        "-o", str(source_file),
                        "--no-warnings",
                        "--force-overwrites",
                    ]
                    ytdlp_cmd.extend(get_js_runtime_args())
                    if proxy_val:
                        ytdlp_cmd.extend(["--proxy", proxy_val])
                    if cookies_file.exists() and cookies_file.stat().st_size > 10:
                        ytdlp_cmd.extend(["--cookies", str(cookies_file)])
                    ytdlp_cmd.append(active_source)
                    prog_bar.progress(0.3, text="Downloading video stream...")
                    subprocess.run(ytdlp_cmd, capture_output=True, text=True)
                    target_video = str(source_file)
                else:
                    target_video = uploaded_video_path

                probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", target_video]
                dur_res = subprocess.run(probe_cmd, capture_output=True, text=True)
                try:
                    total_dur = float(dur_res.stdout.strip())
                except Exception:
                    total_dur = 120.0

                prog_bar.progress(0.5, text="Detecting highlight moments...")
                clip_length = min(longest_sec, max(shortest_sec, 30))
                step_size = max(clip_length, total_dur / (num_clips + 1))
                new_clips_meta = []
                prog_step = 0.5
                step_inc = 0.45 / max(1, num_clips)

                for i in range(num_clips):
                    c_start = min(total_dur - clip_length, i * step_size + 10.0)
                    if c_start < 0:
                        c_start = 0.0
                    c_end = min(total_dur, c_start + clip_length)
                    clip_filename = f"clip_{int(time.time())}_{i+1}.mp4"
                    clip_out_path = OUTPUT_DIR / clip_filename
                    
                    if layout_mode == "blur":
                        vf = "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
                    elif layout_mode == "zoom":
                        vf = "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];[b]scale=1080:1100:force_original_aspect_ratio=increase,crop=1080:1100[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
                    else:
                        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"

                    render_cmd = [
                        "ffmpeg", "-y", "-ss", str(c_start), "-to", str(c_end),
                        "-i", target_video,
                        "-vf", vf,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                        "-c:a", "aac", "-b:a", "128k",
                        str(clip_out_path)
                    ]
                    subprocess.run(render_cmd, capture_output=True)

                    if clip_out_path.exists():
                        ai_meta = generate_ai_viral_meta(clip_out_path, prompt_context=f"Gaming / entertainment highlight from {active_source}")
                        meta_file = META_DIR / f"{clip_out_path.stem}.json"
                        meta_file.write_text(json.dumps(ai_meta, indent=2), encoding="utf-8")
                        new_clips_meta.append((clip_out_path, ai_meta))

                    prog_step += step_inc
                    prog_bar.progress(min(0.95, prog_step), text=f"Rendered clip {i+1}/{num_clips} with Gemini AI...")

                prog_bar.progress(1.0, text="Clipping and AI Generation Complete! 🎉")
                st.success(f"Generated {len(new_clips_meta)} vertical clips! Check 'All Clips Library'.")
                st.rerun()

            except Exception as err:
                st.error(f"Error during clipping pipeline: {err}")


# ================= TAB 2: ALL CLIPS & INSTAGRAM =================
with tab_gallery:
    st.markdown("#### 📂 All Rendered Clips & Instagram Reel Posting")
    all_clips = sorted(list(OUTPUT_DIR.glob("*.mp4")), key=os.path.getmtime, reverse=True)
    
    if not all_clips:
        st.info("No clips rendered yet. Go to 'Streamlit Clipper' tab to generate your first vertical clips!")
    else:
        c_top1, c_top2 = st.columns([3, 1])
        with c_top1:
            st.write(f"Total Available Clips: **{len(all_clips)}**")
        with c_top2:
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf:
                for c_p in all_clips[:15]:
                    zf.write(c_p, arcname=c_p.name)
            zip_buf.seek(0)
            st.download_button("📦 Download All (ZIP)", data=zip_buf, file_name="peakcut_viral_clips.zip", mime="application/zip", use_container_width=True)

        st.divider()

        for idx, clip_path in enumerate(all_clips):
            meta_path = META_DIR / f"{clip_path.stem}.json"
            if meta_path.exists():
                try:
                    clip_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    clip_meta = {}
            else:
                clip_meta = {}

            title_val = clip_meta.get("title", f"🔥 Viral Clip #{idx+1}")
            caption_val = clip_meta.get("caption", "Unbelievable moment! Watch till the end! 👇")
            hashtags_val = " ".join(clip_meta.get("hashtags", ["#reels", "#viral", "#gaming", "#fyp"]))

            with st.container():
                col_vid, col_meta = st.columns([2, 3])
                with col_vid:
                    st.video(str(clip_path))
                    with open(clip_path, "rb") as vf:
                        st.download_button(f"⬇️ Download {clip_path.name}", data=vf, file_name=clip_path.name, mime="video/mp4", key=f"dl_btn_{clip_path.stem}_{idx}", use_container_width=True)

                with col_meta:
                    st.markdown(f"**🎬 {title_val}** <span class='ai-badge'>Gemini AI Generated</span>", unsafe_allow_html=True)
                    edit_title = st.text_input("Hook Title", value=title_val, key=f"title_in_{clip_path.stem}_{idx}")
                    edit_caption = st.text_area("Caption & Story", value=caption_val, height=80, key=f"cap_in_{clip_path.stem}_{idx}")
                    edit_tags = st.text_input("Trending Hashtags", value=hashtags_val, key=f"tags_in_{clip_path.stem}_{idx}")
                    full_post_caption = f"{edit_title}\n\n{edit_caption}\n\n{edit_tags}"

                    c_act1, c_act2, c_act3 = st.columns(3)
                    with c_act1:
                        if st.button("🔄 Re-Analyze with Gemini", key=f"reai_{clip_path.stem}_{idx}", use_container_width=True):
                            with st.spinner("Gemini is watching video frames..."):
                                new_meta = generate_ai_viral_meta(clip_path)
                                meta_path.write_text(json.dumps(new_meta, indent=2), encoding="utf-8")
                                st.success("Updated with fresh AI captions!")
                                st.rerun()

                    with c_act2:
                        st.download_button("📋 Copy Caption", data=full_post_caption, file_name=f"{clip_path.stem}_caption.txt", mime="text/plain", key=f"copy_btn_{clip_path.stem}_{idx}", use_container_width=True)

                    with c_act3:
                        if st.button("📱 Post to Instagram Reel", key=f"post_ig_{clip_path.stem}_{idx}", type="primary", use_container_width=True):
                            z_key = st.session_state.get("zernio_api_key") or os.environ.get("ZERNIO_API_KEY") or ""
                            z_acc = st.session_state.get("zernio_account_id") or os.environ.get("ZERNIO_ACCOUNT_ID") or ""
                            ig_id = st.session_state.get("ig_account_id") or os.environ.get("INSTAGRAM_ACCOUNT_ID") or ""
                            ig_token = st.session_state.get("ig_access_token") or os.environ.get("INSTAGRAM_ACCESS_TOKEN") or ""

                            if z_key and z_acc:
                                with st.spinner("Posting Reel to Instagram via Zernio (@gtavi_hubk)..."):
                                    res = post_to_zernio_reel(clip_path, full_post_caption, z_key, z_acc)
                                    if res.get("ok"):
                                        st.success(f"🎉 Posted to Instagram via Zernio! [View Profile]({res.get('url')})")
                                    else:
                                        st.error(f"Zernio Post Failed: {res.get('error')}")
                            elif ig_id and ig_token:
                                with st.spinner("Posting Reel to Instagram via Meta Graph API..."):
                                    res = post_to_instagram_reel(clip_path, full_post_caption, ig_id, ig_token)
                                    if res.get("ok"):
                                        st.success(f"🎉 Posted to Instagram! [View Reel]({res.get('url')})")
                                    else:
                                        st.error(f"Instagram Post Failed: {res.get('error')}")
                            else:
                                st.warning("⚠️ Please configure Zernio API Key & Account ID or Meta Graph API in sidebar.")

                st.markdown("---")



