# routes/eleven_webrtc.py
# Endpoint compatible con el front antiguo: POST /eleven/webrtc?bot=<slug>
# - Busca agent_id en la tarjeta inteligente
# - Pide token efímero a ElevenLabs con XI_API_KEY
# - Reenvía el SDP offer y devuelve el SDP answer (text/plain)

import os
import requests
from flask import Blueprint, request, make_response, jsonify
from utils.bot_loader import load_bot

bp = Blueprint("eleven_webrtc", __name__, url_prefix="/eleven")

XI_API_KEY = (os.getenv("XI_API_KEY") or os.getenv("ELEVEN_API_KEY") or "").strip()
ELEVEN_TOKEN_URL = os.getenv("ELEVEN_TOKEN_URL", "https://api.elevenlabs.io/v1/realtime/token")
ELEVEN_SDP_URL   = os.getenv("ELEVEN_SDP_URL",   "https://api.elevenlabs.io/v1/realtime/sdp")

ALLOWED_ORIGINS = {
    "https://inhoustontexas.us",
    "https://www.inhoustontexas.us",
}

def _cors(resp):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _agent_id_from_card(bot_slug: str) -> str:
    try:
        card = load_bot(bot_slug or "sundin")
    except Exception:
        card = load_bot("sundin")
    if not isinstance(card, dict):
        return ""
    # Campos soportados
    agent_id = (
        card.get("eleven_agent_id")
        or ((card.get("eleven") or {}).get("agent_id"))
        or ((card.get("realtime") or {}).get("voice_agent_id"))
        or ""
    )
    return (agent_id or "").strip()

@bp.route("/webrtc", methods=["OPTIONS", "POST"])
def eleven_webrtc_sdp():
    # CORS preflight
    if request.method == "OPTIONS":
        return _cors(make_response(("", 204)))

    # Validaciones básicas
    if not XI_API_KEY:
        return _cors(make_response(jsonify({"ok": False, "error": "XI_API_KEY/ELEVEN_API_KEY no configurada"}), 500))

    bot_slug = request.args.get("bot", "").strip() or "sundin"
    agent_id = _agent_id_from_card(bot_slug)
    if not agent_id:
        return _cors(make_response(jsonify({"ok": False, "error": f"No se encontró eleven_agent_id para bot '{bot_slug}'"}), 404))

    sdp_offer = request.get_data(as_text=True) or ""
    if not sdp_offer.strip():
        return _cors(make_response(jsonify({"ok": False, "error": "Body SDP offer vacío"}), 400))

    # 1) Token efímero
    try:
        t = requests.post(ELEVEN_TOKEN_URL, headers={"xi-api-key": XI_API_KEY}, timeout=15)
        if t.status_code != 200:
            return _cors(make_response(jsonify({
                "ok": False, "error": "Eleven token error", "status": t.status_code, "detail": t.text
            }), 502))
        token = (t.json() or {}).get("token") or (t.json() or {}).get("access_token")
        token = (token or "").strip()
        if not token:
            return _cors(make_response(jsonify({"ok": False, "error": "Eleven respondió sin token"}), 502))
    except requests.Timeout:
        return _cors(make_response(jsonify({"ok": False, "error": "Timeout pidiendo token a ElevenLabs"}), 504))
    except Exception as e:
        return _cors(make_response(jsonify({"ok": False, "error": "Excepción pidiendo token", "detail": str(e)}), 502))

    # 2) Reenviar SDP offer → obtener SDP answer
    rtc_url = f"{ELEVEN_SDP_URL}?agent_id={agent_id}"
    try:
        r = requests.post(
            rtc_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/sdp"},
            data=sdp_offer,
            timeout=20,
        )
        if r.status_code != 200:
            return _cors(make_response(jsonify({
                "ok": False, "error": "Eleven SDP error", "status": r.status_code, "detail": r.text
            }), 502))
        # Devolver el SDP answer como text/plain para que WebRTC pueda setRemoteDescription
        resp = make_response(r.text, 200)
        resp.headers["Content-Type"] = "application/sdp; charset=utf-8"
        return _cors(resp)
    except requests.Timeout:
        return _cors(make_response(jsonify({"ok": False, "error": "Timeout negociando SDP con ElevenLabs"}), 504))
    except Exception as e:
        return _cors(make_response(jsonify({"ok": False, "error": "Excepción negociando SDP", "detail": str(e)}), 502))
