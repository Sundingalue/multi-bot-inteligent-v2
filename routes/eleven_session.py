# routes/eleven_session.py
# Endpoint para crear un token efímero de ElevenLabs Realtime (WebRTC)
# Se registra como Blueprint en main.py.

import os
import requests
from flask import Blueprint, jsonify, request, make_response
from utils.bot_loader import load_bot

bp = Blueprint("eleven", __name__, url_prefix="/eleven")

# Defaults (puedes sobreescribir por bot en bots/*.json)
ELEVEN_API_KEY_ENV   = (os.getenv("ELEVEN_API_KEY") or "").strip()
ELEVEN_AGENT_ID_ENV  = (os.getenv("ELEVEN_AGENT_ID") or "").strip()
ELEVEN_VOICE_ID_ENV  = (os.getenv("ELEVEN_VOICE_ID") or "").strip()  # opcional
ELEVEN_RTC_URL_ENV   = (os.getenv("ELEVEN_RTC_URL") or "https://api.elevenlabs.io/v1/convai/rtc").strip()

# Endpoint oficial para token efímero
ELEVEN_TOKEN_URL     = "https://api.elevenlabs.io/v1/convai/conversation/get-token"

def _corsify(resp):
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _get_bot_cfg(bot_id: str) -> dict:
    try:
        return load_bot(bot_id)
    except Exception:
        return {}

def _effective_eleven_cfg(bot_cfg: dict):
    """
    Prioridad: JSON del bot > variables de entorno.
    Soporta estos campos dentro de bots/*.json:
      {
        "eleven": {
          "agent_id": "...",
          "voice_id": "...",       # opcional
          "rtc_url": "https://api.elevenlabs.io/v1/convai/rtc"  # opcional
        }
      }
    """
    eleven = (bot_cfg.get("eleven") or {}) if isinstance(bot_cfg, dict) else {}
    api_key  = ELEVEN_API_KEY_ENV
    agent_id = (eleven.get("agent_id") or ELEVEN_AGENT_ID_ENV).strip()
    voice_id = (eleven.get("voice_id") or ELEVEN_VOICE_ID_ENV).strip()
    rtc_url  = (eleven.get("rtc_url")  or ELEVEN_RTC_URL_ENV).strip() or "https://api.elevenlabs.io/v1/convai/rtc"
    return api_key, agent_id, voice_id, rtc_url

@bp.get("/health")
def health():
    ok = bool(ELEVEN_API_KEY_ENV or ELEVEN_AGENT_ID_ENV)
    resp = jsonify({
        "ok": ok,
        "service": "eleven_realtime",
        "defaults": {
            "has_api_key": bool(ELEVEN_API_KEY_ENV),
            "agent_id_set": bool(ELEVEN_AGENT_ID_ENV),
            "rtc_url": ELEVEN_RTC_URL_ENV
        }
    })
    return _corsify(resp)

@bp.route("/session", methods=["POST", "OPTIONS"])
def create_session():
    """
    Devuelve un token efímero para ElevenLabs Realtime:
      POST /eleven/session?bot=<slug|id>
    Response:
      { ok: true, token: "...", rtc_url: "https://api.elevenlabs.io/v1/convai/rtc" }
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    bot_cfg = _get_bot_cfg(bot_id)
    api_key, agent_id, voice_id, rtc_url = _effective_eleven_cfg(bot_cfg)

    if not api_key:
        return _corsify(jsonify({"ok": False, "error": "ELEVEN_API_KEY no configurada"})), 500
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": "ELEVEN_AGENT_ID no configurada (env o bots/*.json)"})), 400

    payload = {"agent_id": agent_id}
    if voice_id:
        # voice_id es opcional; si no lo pones, usa el del Agent en Eleven
        payload["voice_id"] = voice_id

    try:
        r = requests.post(
            ELEVEN_TOKEN_URL,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        if r.status_code >= 400:
            return _corsify(jsonify({
                "ok": False,
                "error": "Eleven token error",
                "status": r.status_code,
                "detail": r.text
            })), 502

        data = r.json() or {}
        token = (data.get("token") or "").strip()
        if not token:
            return _corsify(jsonify({"ok": False, "error": "Respuesta sin token desde Eleven"})), 502

        return _corsify(jsonify({
            "ok": True,
            "token": token,
            "rtc_url": rtc_url
        }))
    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout solicitando token Eleven"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción solicitando token Eleven", "detail": str(e)})), 500
