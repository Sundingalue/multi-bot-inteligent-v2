# config/settings.py
# -*- coding: utf-8 -*-

from __future__ import annotations
import os, json, glob, logging, re
from pathlib import Path
from typing import Dict, Any, Optional

# ========= .env opcional =========
try:
    from dotenv import load_dotenv
    # 1) compat con Render secret file
    load_dotenv("/etc/secrets/.env")
    # 2) .env local
    load_dotenv()
except Exception:
    pass

# ========= RUTAS BASE =========
# Asumimos estructura: .../MULTI-BOT-INTELIGENTE/config/settings.py
ROOT_DIR: Path = Path(__file__).resolve().parents[1]
BOTS_DIR: Path = ROOT_DIR / "bots"
LOGS_DIR: Path = ROOT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ========= LOGGING SIMPLE =========
logger = logging.getLogger("settings")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(_h)

# ========= ENV VARS (con defaults seguros) =========
def _env(key: str, default: str = "") -> str:
    return (os.getenv(key, default) or "").strip()

OPENAI_API_KEY: str = _env("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL: str = _env("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
# Si defines OPENAI_VOICE en entorno, tiene prioridad absoluta
OPENAI_VOICE_DEFAULT: str = _env("OPENAI_VOICE", "nova")  # femenina por defecto

# Twilio / App
TWILIO_ACCOUNT_SID: str = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN: str  = _env("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER: str = _env("TWILIO_WHATSAPP_NUMBER")
TWILIO_VOICE_WEBHOOK_URL: str = _env("TWILIO_VOICE_WEBHOOK_URL", "https://multi-bot-inteligente-v1.onrender.com/voice")

APP_URL: str = _env("APP_URL", _env("RENDER_EXTERNAL_URL", "http://localhost:5000"))
ENVIRONMENT: str = _env("ENVIRONMENT", "development").lower()

# Fallbacks globales (coinciden con tu main.py actual)
BOOKING_URL_FALLBACK: str = _env("BOOKING_URL")
APP_DOWNLOAD_URL_FALLBACK: str = _env("APP_DOWNLOAD_URL")

# Seguridad para endpoints JSON (Bearer compartido)
API_BEARER_TOKEN: str = _env("API_BEARER_TOKEN")

# Firebase (solo variables; la inicialización sigue en main.py por ahora)
FIREBASE_DB_URL: str = _env("FIREBASE_DB_URL")  # tu main lo lee también desde /etc/secrets/FIREBASE_DB_URL
FIREBASE_CRED_PATH: str = "/etc/secrets/firebase.json"  # igual que tu main

# ========= CARGA DE BOTS (acepta tu formato) =========
# Tu formato actual: cada archivo .json puede ser un "mapa" { "whatsapp:+1...": {...}, ... }
# Consolidamos todos en un solo dict.
def _safe_json_load(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"No se pudo cargar {path.name}: {e}")
        return {}

def load_bots_folder() -> Dict[str, Dict[str, Any]]:
    bots: Dict[str, Dict[str, Any]] = {}
    for p in glob.glob(str(BOTS_DIR / "*.json")):
        data = _safe_json_load(Path(p))
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    bots[k] = v
    if not bots:
        logger.warning("No se encontraron bots en ./bots/*.json")
    return bots

BOTS_CONFIG: Dict[str, Dict[str, Any]] = load_bots_folder()

# ========= Normalizadores / utilidades comunes =========
def _valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

def canonize_phone(raw: str) -> str:
    """Normaliza a E.164 para emparejar claves: +1XXXXXXXXXX"""
    s = str(raw or "").strip()
    for pref in ("whatsapp:", "tel:", "sip:", "client:"):
        if s.startswith(pref):
            s = s[len(pref):]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits

def find_bot_by_number(to_number: str) -> Optional[Dict[str, Any]]:
    """Busca config por número (acepta clave whatsapp:+..., tel:..., o +1... en E.164)."""
    if not to_number:
        # si sólo hay 1 bot configurado, devuélvelo
        return list(BOTS_CONFIG.values())[0] if len(BOTS_CONFIG) == 1 else None

    canon_to = canonize_phone(to_number)
    # 1) buscar por E.164 equivalente
    for key, cfg in BOTS_CONFIG.items():
        if canonize_phone(key) == canon_to:
            return cfg
    # 2) buscar por clave literal
    return BOTS_CONFIG.get(to_number)

def normalize_bot_name(name: str) -> Optional[str]:
    if not name:
        return None
    for cfg in BOTS_CONFIG.values():
        if isinstance(cfg, dict):
            if (cfg.get("name") or "").strip().lower() == str(name).strip().lower():
                return cfg.get("name")
    return None

def get_bot_cfg_by_name(name: str) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    for cfg in BOTS_CONFIG.values():
        if isinstance(cfg, dict) and (cfg.get("name") or "").strip().lower() == name.strip().lower():
            return cfg
    return None

# ========= Voz por bot =========
# Prioridad de voz (de mayor a menor):
# 1) OPENAI_VOICE (si está definida en entorno)
# 2) bot["voice"]
# 3) bot["realtime"]["voice"]
# 4) bot["openai"]["voice"]
# 5) OPENAI_VOICE_DEFAULT ("nova")
def get_voice(bot_cfg: Optional[Dict[str, Any]]) -> str:
    # 1) override por entorno
    if os.getenv("OPENAI_VOICE"):
        return OPENAI_VOICE_DEFAULT or "nova"

    # 2-4) del JSON
    if isinstance(bot_cfg, dict):
        v = str(bot_cfg.get("voice", "") or "").strip()
        if v:
            return v
        rt = bot_cfg.get("realtime") or {}
        if isinstance(rt, dict):
            v = str(rt.get("voice", "") or "").strip()
            if v:
                return v
        op = bot_cfg.get("openai") or {}
        if isinstance(op, dict):
            v = str(op.get("voice", "") or "").strip()
            if v:
                return v

    # 5) default
    return OPENAI_VOICE_DEFAULT or "nova"

# ========= Links efectivos con fallbacks =========
def _drill_get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def effective_booking_url(bot_cfg: Dict[str, Any]) -> str:
    candidates = [
        "links.booking_url",
        "booking_url",
        "calendar_booking_url",
        "google_calendar_booking_url",
        "agenda.booking_url",
    ]
    for p in candidates:
        val = _drill_get(bot_cfg or {}, p)
        val = (val or "").strip() if isinstance(val, str) else ""
        if _valid_url(val):
            return val
    return BOOKING_URL_FALLBACK if _valid_url(BOOKING_URL_FALLBACK) else ""

def effective_app_url(bot_cfg: Dict[str, Any]) -> str:
    candidates = [
        "links.app_download_url",
        "links.app_url",
        "app_download_url",
        "app_url",
        "download_url",
        "link_app",
    ]
    for p in candidates:
        val = _drill_get(bot_cfg or {}, p)
        val = (val or "").strip() if isinstance(val, str) else ""
        if _valid_url(val):
            return val
    return APP_DOWNLOAD_URL_FALLBACK if _valid_url(APP_DOWNLOAD_URL_FALLBACK) else ""
