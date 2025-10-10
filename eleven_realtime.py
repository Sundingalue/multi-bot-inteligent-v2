# eleven_realtime.py
# Token efímero y health para ElevenLabs Realtime (Convai).
# Mantiene compatibilidad con tu patrón actual: el CLIENTE (WP) negocia WebRTC directo con Eleven.
# Este archivo NO toca OpenAI ni tu /realtime/session existente.

import os
import requests
from flask import Blueprint, jsonify, request, make_response
try:
    # tu loader de bots (para mapear bot->agent_id si existe en JSON)
    from utils.bot_loader import load_bot
except Exception:
    load_bot = None  # fallback suave

bp = Blueprint("eleven_realtime", __name__, url_prefix="/eleven")

# ===== ENV =====
ELEVEN_API_KEY  = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
ELEVEN_AGENT_ID = (os.getenv("ELEVENLABS_AGENT_ID") or "").strip()

# ===== Endpoints Eleven (ajusta si tu cuenta usa otros paths) =====
ELEVEN_TOKEN_URL = "https://api.elevenlabs.io/v1/convai/token"
ELEVEN_RTC_URL   = "https://api.elevenlabs.io/v1/convai/rtc"   # el browser hará POST SDP aquí

def _corsify(resp):
    """CORS básico por si tu after_request no atrapa OPTIONS en algún proxy."""
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"]  = origin
        resp.headers["Vary"]                         = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"]       = "86400"
    return resp

@bp.route("/health", methods=["GET", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))
    ok = bool(ELEVEN_API_KEY)
    return _corsify(jsonify({
        "ok": ok,
        "has_api_key": ok,
        "agent_default": ELEVEN_AGENT_ID or None,
        "rtc_url": ELEVEN_RTC_URL
    }))

@bp.route("/session", methods=["POST", "OPTIONS"])
def create_session():
    """
    Devuelve un token efímero para que el cliente WP negocie WebRTC con Eleven:
      - INPUT opcional: ?bot=<slug> (para mapear a agent_id por bot)
      - OUTPUT: { ok, token, expires_at, rtc_url, agent_id }
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not ELEVEN_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "ELEVENLABS_API_KEY no configurada"})), 500

    # Elegir agent_id según el bot (si tu JSON lo trae), con fallback a ENV
    agent_id = ELEVEN_AGENT_ID or None
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or None
    if load_bot and bot_id:
        try:
            bot_cfg = load_bot(bot_id) or {}
            agent_id = ((bot_cfg.get("eleven") or {}).get("agent_id")) or agent_id
        except Exception:
            pass

    # Construye request al token endpoint de Eleven
    try:
        headers = {"xi-api-key": ELEVEN_API_KEY}
        payload = {}
        if agent_id:
            payload["agent_id"] = agent_id  # usa el Agent (ASR+LLM+TTS) de Eleven
        r = requests.post(ELEVEN_TOKEN_URL, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()  # esperado: { "token": "...", "expires_at": "..." }

        token = data.get("token")
        if not token:
            return _corsify(jsonify({"ok": False, "error": "Eleven no devolvió token", "detail": data})), 502

        return _corsify(jsonify({
            "ok": True,
            "token": token,
            "expires_at": data.get("expires_at"),
            "rtc_url": ELEVEN_RTC_URL,
            "agent_id": agent_id
        }))
    except requests.HTTPError as e:
        try:
            status = r.status_code
            detail = r.text
        except Exception:
            status = 502
            detail = str(e)
        return _corsify(jsonify({"ok": False, "error": "HTTP Eleven", "status": status, "detail": detail})), 502
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción pidiendo token", "detail": str(e)})), 500
