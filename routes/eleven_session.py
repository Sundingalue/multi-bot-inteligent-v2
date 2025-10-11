# routes/eleven_session.py
# Devuelve token efímero + URL SDP para ElevenLabs Realtime
# y actúa como proxy SDP en /eleven/webrtc (lo que llama tu WordPress).

import os
import requests
from flask import Blueprint, request, jsonify, make_response, Response
from utils.bot_loader import load_bot

bp = Blueprint("eleven_session", __name__, url_prefix="/eleven")

# Clave de servidor (NO exponer en front)
XI_API_KEY = (os.getenv("XI_API_KEY") or os.getenv("ELEVEN_API_KEY") or "").strip()

# Endpoints ElevenLabs
ELEVEN_SDP_URL   = os.getenv("ELEVEN_SDP_URL",   "https://api.elevenlabs.io/v1/realtime/sdp")
ELEVEN_TOKEN_URL = os.getenv("ELEVEN_TOKEN_URL", "https://api.elevenlabs.io/v1/realtime/token")

def _corsify(resp):
    origin = request.headers.get("Origin", "")
    if origin:
        # Si tienes CORS abierto por env, el after_request de main también lo cubre.
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
      - realtime.voice_agent_id (alias)
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

def _need_key_resp():
    return _corsify(jsonify({"ok": False, "error": "XI_API_KEY/ELEVEN_API_KEY no configurada"})), 500

@bp.route("/session", methods=["POST", "OPTIONS"])
def create_eleven_session():
    """
    Crea token efímero de ElevenLabs **en el backend** usando XI_API_KEY
    y devuelve:
      { ok: true, token: "<jwt>", rtc_url: "https://api.elevenlabs.io/v1/realtime/sdp?agent_id=..." }
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not XI_API_KEY:
        return _need_key_resp()

    # Bot (slug) desde ?bot= o header
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    agent_id = _get_agent_id(bot_id)
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": f"No se encontró eleven_agent_id para bot='{bot_id}'"})), 404

    # 1) Pedir token efímero
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

@bp.route("/webrtc", methods=["POST", "OPTIONS"])
def eleven_webrtc_proxy():
    """
    Proxy SDP para WordPress:
      - El front envía OFFER SDP (Content-Type: application/sdp) a /eleven/webrtc?bot=slug
      - Aquí pedimos token efímero a Eleven con XI_API_KEY
      - Reenviamos el OFFER a ELEVEN_SDP_URL?agent_id=...
        con Authorization: Bearer <token>
      - Devolvemos el ANSWER SDP (Content-Type: application/sdp)
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not XI_API_KEY:
        return _need_key_resp()

    # Bot (slug) desde ?bot= o header
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    agent_id = _get_agent_id(bot_id)
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": f"No se encontró eleven_agent_id para bot='{bot_id}'"})), 404

    # Offer SDP (soportamos application/sdp o texto plano)
    offer_sdp = request.get_data(as_text=True) or ""
    if not offer_sdp.strip():
        return _corsify(jsonify({"ok": False, "error": "Body vacío: se esperaba offer SDP"})), 400

    # 1) Token efímero
    try:
        tok = requests.post(ELEVEN_TOKEN_URL, headers={"xi-api-key": XI_API_KEY}, timeout=15)
        if tok.status_code != 200:
            return _corsify(jsonify({
                "ok": False,
                "error": "Eleven token error",
                "status": tok.status_code,
                "detail": tok.text
            })), 502
        token = (tok.json() or {}).get("token") or (tok.json() or {}).get("access_token") or ""
        token = token.strip()
    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout pidiendo token a ElevenLabs"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción pidiendo token a ElevenLabs", "detail": str(e)})), 502

    if not token:
        return _corsify(jsonify({"ok": False, "error": "Eleven respondió sin token"})), 502

    # 2) POST SDP a Eleven
    try:
        sdp_url = f"{ELEVEN_SDP_URL}?agent_id={agent_id}"
        r = requests.post(
            sdp_url,
            data=offer_sdp.encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/sdp",
            },
            timeout=20,
        )
    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout en intercambio SDP con ElevenLabs"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción al enviar SDP a ElevenLabs", "detail": str(e)})), 502

    if r.status_code != 200:
        # Devuelve detalle para depurar en logs
        return _corsify(jsonify({
            "ok": False,
            "error": "Fallo intercambio SDP",
            "status": r.status_code,
            "detail": r.text
        })), 502

    # 3) Devolver ANSWER SDP tal cual, con content-type correcto
    answer_sdp = r.text or ""
    resp = make_response(answer_sdp, 200)
    resp.headers["Content-Type"] = "application/sdp"
    return _corsify(resp)
