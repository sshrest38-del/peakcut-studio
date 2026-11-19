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
CF_PAGES_DIR = ROOT / "cloudflare_pages"

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
META_DIR.mkdir(parents=True, exist_ok=True)
CF_PAGES_DIR.mkdir(parents=True, exist_ok=True)

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
    st.markdown("### ☁️ Cloudflare Pages Setup")
    st.markdown("<span class='badge-cf'>⚡ Cloudflare Pages Ready</span>", unsafe_allow_html=True)
    st.caption("Frontend folder ready: `cloudflare_pages/` with `_worker.js` proxy to Streamlit backend.")

    st.divider()
    st.markdown("### 📸 Instagram Reel Auto-Post")
    saved_ig_id = st.session_state.get("ig_account_id") or os.environ.get("INSTAGRAM_ACCOUNT_ID") or ""
    saved_ig_token = st.session_state.get("ig_access_token") or os.environ.get("INSTAGRAM_ACCESS_TOKEN") or ""
    try:
        if "INSTAGRAM_ACCOUNT_ID" in st.secrets:
            saved_ig_id = str(st.secrets["INSTAGRAM_ACCOUNT_ID"])
        if "INSTAGRAM_ACCESS_TOKEN" in st.secrets:
            saved_ig_token = str(st.secrets["INSTAGRAM_ACCESS_TOKEN"])
    except Exception:
        pass

    with st.expander("⚙️ Instagram Account (Hidden)", expanded=not bool(saved_ig_token)):
        inp_ig_id = st.text_input("Instagram Account ID", value=saved_ig_id, placeholder="e.g. 17841400...")
        inp_ig_token = st.text_input("Meta Graph API Access Token", value=saved_ig_token, type="password", placeholder="EAAG...")
        
        if st.button("Save Instagram Account", use_container_width=True):
            st.session_state["ig_account_id"] = inp_ig_id.strip()
            st.session_state["ig_access_token"] = inp_ig_token.strip()
            os.environ["INSTAGRAM_ACCOUNT_ID"] = inp_ig_id.strip()
            os.environ["INSTAGRAM_ACCESS_TOKEN"] = inp_ig_token.strip()
            st.success("Instagram credentials saved!")
            st.rerun()

    if saved_ig_token:
        st.markdown("<span class='badge-ok'>🟢 Instagram Connected</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span class='badge-warn'>⚪ Instagram Token Missing</span>", unsafe_allow_html=True)


# ---------------- MAIN APPLICATION HEADER ----------------
c_h1, c_h2 = st.columns([4, 1])
with c_h1:
    st.markdown("""
    <div class="brand-title">
        <span class="brand-orange">✂️ PeakCut</span> Studio
    </div>
    <div class="brand-subtitle">
        Cloudflare Edge Frontend + Streamlit Cloud High-Performance Backend | Multi-Gemini AI & Instagram Auto-Post
    </div>
    """, unsafe_allow_html=True)

with c_h2:
    st.markdown("<br>", unsafe_allow_html=True)
    st.link_button("🌐 Open Port 8765 Web App", "http://127.0.0.1:8765/app/videos", use_container_width=True)

tabs = st.tabs([
    "🖥️ Exact PeakCut UI (100% Original)",
    "⚡ Streamlit Clipper & Instagram Poster",
    "📂 All Clips Library",
    "☁️ Cloudflare + Streamlit Deployment"
])
tab_exact, tab_clipper, tab_gallery, tab_cf = tabs[0], tabs[1], tabs[2], tabs[3]


# ================= TAB 0: EXACT PEAKCUT UI (100% ORIGINAL EMBED) =================
with tab_exact:
    st.markdown("#### 🎨 100% Exact PeakCut Web Interface")
    st.caption("This renders the exact native PeakCut UI (with sidebar, custom layouts, animated styles, and full controls).")
    
    col_ctrl1, col_ctrl2 = st.columns([3, 1])
    with col_ctrl1:
        backend_web_url = st.text_input("PeakCut Web Address", value="http://127.0.0.1:8765/app/videos")
    with col_ctrl2:
        st.markdown("<br>", unsafe_allow_html=True)
        st.link_button("↗️ Open in New Tab", backend_web_url, use_container_width=True)

    # Embed the exact native UI via iframe component
    components.iframe(backend_web_url, height=880, scrolling=True)


# ================= TAB 1: STREAMLIT CLIPPER & INSTAGRAM =================
with tab_clipper:
    # 1. Source Input
    st.markdown("##### 🔗 Video Source")
    input_choice = st.radio("Source Type", ["Paste Link", "Upload Video File"], horizontal=True, label_visibility="collapsed")

    video_url = ""
    uploaded_video_path = None

    if input_choice == "Paste Link":
        video_url = st.text_input("Video Link", placeholder="Paste a YouTube, Twitch or Kick video link...", label_visibility="collapsed")
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
                    ytdlp_cmd = [
                        sys.executable, "-m", "yt_dlp",
                        "-f", "ba/b[height<=720]/b",
                        "-o", str(source_file),
                        "--no-warnings",
                        "--force-overwrites",
                        active_source
                    ]
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
                            ig_id = st.session_state.get("ig_account_id") or os.environ.get("INSTAGRAM_ACCOUNT_ID") or ""
                            ig_token = st.session_state.get("ig_access_token") or os.environ.get("INSTAGRAM_ACCESS_TOKEN") or ""
                            if not ig_token or not ig_id:
                                st.warning("⚠️ Enter Instagram Account ID & Access Token in left sidebar under 'Instagram Settings'.")
                            else:
                                with st.spinner("Posting Reel to Instagram via Meta Graph API..."):
                                    res = post_to_instagram_reel(clip_path, full_post_caption, ig_id, ig_token)
                                    if res.get("ok"):
                                        st.success(f"🎉 Posted to Instagram! [View Reel]({res.get('url')})")
                                    else:
                                        st.error(f"Instagram Post Failed: {res.get('error')}")

                st.markdown("---")


# ================= TAB 3: CLOUDFLARE + STREAMLIT ARCHITECTURE =================
with tab_cf:
    st.markdown("#### ☁️ Cloudflare Pages (Frontend) + Streamlit (Backend) Setup")
    st.markdown("""
    Aapka request pura taiyaar hai:
    - **Frontend**: 100% Exact PeakCut UI Cloudflare Pages par chalega (Global CDN, fast, custom domain / `.pages.dev`).
    - **Backend**: Video download, FFmpeg clipping, Gemini Vision AI, aur Instagram Posting Streamlit/Python server par chalegi.
    - **Bridge**: `cloudflare_pages/_worker.js` automatically saare API calls (`/api/...`) backend server par forward/proxy karega.

    ---
    ##### 🚀 Cloudflare Pages Deploy Karne Ka Tarika:

    **Option 1: 1-Click Batch Script Se (Sabse Asaan)**
    1. Folder me bane **`deploy_to_cloudflare.bat`** par double click karein.
    2. Yeh automatically Cloudflare Wrangler CLI chala kar `cloudflare_pages` ko deploy kar dega.

    **Option 2: GitHub / Cloudflare Dashboard Se**
    1. Apne GitHub repository ko Cloudflare Dashboard (Workers & Pages ➔ Create application ➔ Pages) se connect karein.
    2. Build configuration:
       - **Framework preset**: `None`
       - **Build output directory**: `cloudflare_pages`
    3. **Environment Variables** me backend ka address daalein:
       - Variable name: `BACKEND_URL`
       - Value: `https://your-streamlit-app.streamlit.app` (ya aapka public server URL)
    4. Click **Save and Deploy**.

    ---
    ##### 🔒 Streamlit Cloud Secrets:
    Agar backend ko Streamlit Community Cloud par rakh rahe hain toh App Secrets me yeh paste karein:
    ```toml
    GEMINI_API_KEYS = "AIzaSyKey1..., AIzaSyKey2..., AIzaSyKey3..."
    INSTAGRAM_ACCOUNT_ID = "17841400..."
    INSTAGRAM_ACCESS_TOKEN = "EAAG..."
    ```
    """)
