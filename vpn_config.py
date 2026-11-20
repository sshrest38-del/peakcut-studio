"""
PeakCut Surfshark VPN & Proxy Integration Helper
Supports:
1. Surfshark HTTP / SOCKS5 proxy (ideal for Streamlit Cloud with zero root requirements)
2. Surfshark OpenVPN (.ovpn) configuration
3. Local and cloud proxy routing for yt-dlp and requests
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict

ROOT_DIR = Path(__file__).parent.resolve()
DATA_DIR = ROOT_DIR / "data"
CONFIG_FILE = ROOT_DIR / "bot_config.json"
OVPN_FILE = DATA_DIR / "surfshark.ovpn"

def get_surfshark_proxy_url(username: str = "", password: str = "", host: str = "in-mum.prod.surfshark.com", port: int = 80) -> str:
    """
    Builds a standard proxy URL for Surfshark.
    Default host: in-mum.prod.surfshark.com (India Mumbai) or in-del.prod.surfshark.com (India Delhi)
    """
    if not username or not password:
        return ""
    return f"http://{username}:{password}@{host}:{port}"

def save_proxy_config(proxy_url: str):
    """
    Saves proxy URL to bot_config.json
    """
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg["proxy"] = proxy_url.strip()
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def get_active_proxy() -> str:
    """
    Returns active proxy URL from env or bot_config.json
    """
    env_p = os.environ.get("SURFSHARK_PROXY") or os.environ.get("HTTP_PROXY") or ""
    if env_p:
        return env_p.strip()
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("proxy", "").strip()
        except Exception:
            pass
    return ""

def configure_ovpn_file(ovpn_content: str, credentials_user: str = "", credentials_pass: str = ""):
    """
    Saves the Surfshark .ovpn configuration and optional auth credentials
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OVPN_FILE.write_text(ovpn_content.strip(), encoding="utf-8")
    if credentials_user and credentials_pass:
        auth_file = DATA_DIR / "vpn_auth.txt"
        auth_file.write_text(f"{credentials_user.strip()}\n{credentials_pass.strip()}\n", encoding="utf-8")
