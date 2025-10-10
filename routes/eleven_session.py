# routes/eleven_session.py
# Devuelve token efímero + URL SDP para ElevenLabs Realtime
# Usa el agent_id desde la tarjeta inteligente (bots/*.json)

import os
import requests
from flask import Blueprint, request, jsonify, make_response
from utils.bot_loader import load_bot

bp = Blueprint("eleven_session", __name__, url_prefix="/eleven")

XI_API_KEY = (os.getenv("XI_API_KEY") or os.getenv("ELEVEN_API_KEY") or "").strip()

# URL base de ElevenLabs Realtime
ELEVEN_SDP_URL = os.getenv("ELEVEN_SDP_URL", "https://api.elevenlabs.io/v1/realtime/sdp")
ELEVEN_TOKEN_URL = os.getenv("ELEVEN_TOKEN_URL", "https://api.elevenlabs.io/v1/realtime/token")

def _corsify(resp):
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"]  = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _get_agent_id(bot_id: str) -> str:
    """
    Lee el agent_id desde la tarjeta inteligente.
    Campos soportados:
      - eleven_agent_id
      - eleven.agent_id
      - realtime.voice_agent_id  (alias por si lo guardaste así)
    """
    try:
        card = load_bot(bot_id)
    except Exception:
        card = load_bot("sundin")

    if not isinstance(card, dict):
        return ""

    agent_id = (
        card.get("eleven_agent_id")
        or ((card.get("eleven") or {}).get("agent_id"))
        or ((card.get("realtime") or {}).get("voice_agent_id"))
        or ""
    )
    return (agent_id or "").strip()

@bp.route("/session", methods=["POST", "OPTIONS"])
def create_eleven_session():
    """
    Crea token efímero de ElevenLabs **en el backend** usando XI_API_KEY
    y devuelve:
      { ok: true, token: "<jwt>", rtc_url: "https://api.elevenlabs.io/v1/realtime/sdp?agent_id=..." }

    Front (WordPress) luego hace:
      POST rtc_url   (Content-Type: application/sdp, Authorization: Bearer <token>)
      body = offer.sdp
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not XI_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "XI_API_KEY/ELEVEN_API_KEY no configurada"})), 500

    # Bot (slug) desde ?bot= o header
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    agent_id = _get_agent_id(bot_id)
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": f"No se encontró eleven_agent_id para bot='{bot_id}'"})), 404

    # 1) Pedir token efímero a ElevenLabs
    #    (este endpoint devuelve un JWT válido minutos, sin exponer XI_API_KEY al navegador)
    try:
        r = requests.post(
            ELEVEN_TOKEN_URL,
            headers={"xi-api-key": XI_API_KEY},
            timeout=15,
        )
        if r.status_code != 200:
            return _corsify(jsonify({
                "ok": False,
                "error": "Eleven token error",
                "status": r.status_code,
                "detail": r.text
            })), 502
        data = r.json() or {}
        token = (data.get("token") or data.get("access_token") or "").strip()
    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout pidiendo token a ElevenLabs"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción pidiendo token a ElevenLabs", "detail": str(e)})), 502

    if not token:
        return _corsify(jsonify({"ok": False, "error": "Eleven respondió sin token"})), 502

    # 2) Construir la URL SDP con agent_id
    rtc_url = f"{ELEVEN_SDP_URL}?agent_id={agent_id}"

    resp = jsonify({"ok": True, "token": token, "rtc_url": rtc_url, "agent_id": agent_id})
    return _corsify(resp)
