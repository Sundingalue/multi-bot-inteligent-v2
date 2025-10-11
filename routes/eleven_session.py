# routes/eleven_session.py
# → Sesión efímera y proxy SDP para ElevenLabs Realtime
# Usa el agent_id desde la tarjeta inteligente (bots/tarjeta_inteligente/*.json)

import os
import requests
from flask import Blueprint, request, jsonify, make_response, Response
from utils.bot_loader import load_bot

bp = Blueprint("eleven_session", __name__, url_prefix="/eleven")

# Acepta cualquiera de estos nombres de variable
XI_API_KEY = (
    os.getenv("XI_API_KEY")
    or os.getenv("ELEVEN_API_KEY")
    or os.getenv("ELEVENLABS_API_KEY")
    or ""
).strip()

# Endpoints base ElevenLabs
ELEVEN_SDP_URL   = os.getenv("ELEVEN_SDP_URL",   "https://api.elevenlabs.io/v1/realtime/sdp")
ELEVEN_TOKEN_URL = os.getenv("ELEVEN_TOKEN_URL", "https://api.elevenlabs.io/v1/realtime/token")

def _corsify(resp):
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _get_agent_id(bot_id: str) -> str:
    """
    Lee el agent_id desde la tarjeta inteligente.
    Campos soportados:
      - eleven_agent_id
      - eleven.agent_id
      - realtime.voice_agent_id (alias)
    """
    try:
        card = load_bot(bot_id)
    except Exception:
        card = {}
    if not isinstance(card, dict):
        card = {}

    agent_id = (
        (card.get("eleven_agent_id") or "")
        or ((card.get("eleven") or {}).get("agent_id") or "")
        or ((card.get("realtime") or {}).get("voice_agent_id") or "")
    )
    return str(agent_id).strip()

def _get_token() -> str:
    """Solicita un token efímero a ElevenLabs usando la API key del backend."""
    r = requests.post(ELEVEN_TOKEN_URL, headers={"xi-api-key": XI_API_KEY}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"token status={r.status_code} body={r.text[:200]}")
    data = r.json() or {}
    token = (data.get("token") or data.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("token vacío en respuesta de ElevenLabs")
    return token

@bp.route("/health", methods=["GET", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))
    bot_id = request.args.get("bot") or "sundin"
    agent_id = _get_agent_id(bot_id)
    ok = bool(XI_API_KEY) and bool(agent_id)
    return _corsify(jsonify({
        "ok": ok,
        "has_api_key": bool(XI_API_KEY),
        "agent_id": agent_id or "",
        "sdp_url": ELEVEN_SDP_URL
    }))

@bp.route("/session", methods=["POST", "OPTIONS"])
def create_session():
    """
    Devuelve { ok, token, rtc_url, agent_id } para que el front haga el POST SDP directo.
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not XI_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "XI_API_KEY/ELEVENLABS_API_KEY no configurada"})), 500

    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    agent_id = _get_agent_id(bot_id)
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": f"eleven_agent_id no encontrado para bot='{bot_id}'"})), 404

    try:
        token = _get_token()
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Fallo pidiendo token a ElevenLabs", "detail": str(e)})), 502

    rtc_url = f"{ELEVEN_SDP_URL}?agent_id={agent_id}"
    return _corsify(jsonify({"ok": True, "token": token, "rtc_url": rtc_url, "agent_id": agent_id}))

@bp.route("/webrtc", methods=["POST", "OPTIONS"])
def webrtc_proxy():
    """
    SHIM de compatibilidad para front-ends antiguos que POSTean a /eleven/webrtc.
    Espera body = offer.sdp (Content-Type: application/sdp).
    Aquí pedimos el token efímero y hacemos el POST a ElevenLabs, devolviendo el answer.sdp.
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not XI_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "XI_API_KEY/ELEVENLABS_API_KEY no configurada"})), 500

    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    agent_id = _get_agent_id(bot_id)
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": f"eleven_agent_id no encontrado para bot='{bot_id}'"})), 404

    # Validar que realmente nos mandaron SDP
    ctype = (request.headers.get("Content-Type") or "").lower()
    sdp = request.get_data() or b""
    if "application/sdp" not in ctype or not sdp:
        return _corsify(jsonify({"ok": False, "error": "Se esperaba Content-Type: application/sdp con el offer.sdp en el body"})), 400

    # Token + POST a ElevenLabs
    try:
        token = _get_token()
        upstream = requests.post(
            f"{ELEVEN_SDP_URL}?agent_id={agent_id}",
            data=sdp,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/sdp"
            },
            timeout=25
        )
    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout contactando ElevenLabs SDP"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción contactando ElevenLabs SDP", "detail": str(e)})), 502

    if upstream.status_code != 200:
        return _corsify(jsonify({
            "ok": False,
            "error": "ElevenLabs SDP no aceptó el offer",
            "status": upstream.status_code,
            "detail": upstream.text[:300]
        })), 502

    # Responder el answer.sdp crudo como text/plain para el RTCPeerConnection
    resp = Response(upstream.text, status=200, mimetype="application/sdp")
    return _corsify(resp)
