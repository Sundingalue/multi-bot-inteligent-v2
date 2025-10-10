# routes/eleven_session.py
# Proxy WebRTC para ElevenLabs ConvAI (no expone tu API key al navegador)

import os
import requests
from flask import Blueprint, request, jsonify, make_response

# Si ya tienes estas utilidades, úsalas; de lo contrario, cambia por tus helpers
try:
    from utils.bot_loader import load_bot
except Exception:
    # Fallback mínimo
    def load_bot(slug: str):
        raise RuntimeError("load_bot no disponible")

bp = Blueprint("eleven", __name__, url_prefix="/eleven")

def _cors(resp):
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

@bp.route("/health", methods=["GET"])
def health():
    return _cors(jsonify({"ok": True, "service": "eleven"}))

def _get_eleven_agent(bot_cfg: dict) -> str:
    """
    Prioridades:
      1) bot_cfg["eleven"]["agent_id"]
      2) bot_cfg["realtime"]["eleven_agent_id"]
      3) bot_cfg["eleven_agent_id"]
    """
    if not isinstance(bot_cfg, dict):
        return ""
    eleven = bot_cfg.get("eleven") or {}
    if isinstance(eleven, dict) and eleven.get("agent_id"):
        return str(eleven["agent_id"]).strip()
    rt = bot_cfg.get("realtime") or {}
    if isinstance(rt, dict) and rt.get("eleven_agent_id"):
        return str(rt["eleven_agent_id"]).strip()
    if bot_cfg.get("eleven_agent_id"):
        return str(bot_cfg["eleven_agent_id"]).strip()
    return ""

def _get_eleven_api_key(bot_cfg: dict) -> str:
    """
    Prioridades:
      1) bot_cfg["eleven"]["api_key"]
      2) env ELEVEN_API_KEY
    """
    if isinstance(bot_cfg, dict):
        eleven = bot_cfg.get("eleven") or {}
        if isinstance(eleven, dict) and eleven.get("api_key"):
            return str(eleven["api_key"]).strip()
    return (os.getenv("ELEVEN_API_KEY") or "").strip()

@bp.route("/webrtc", methods=["POST", "OPTIONS"])
def webrtc_proxy():
    """
    El FRONT nos manda su SDP offer (Content-Type: application/sdp).
    Aquí lo reenviamos a ElevenLabs y devolvemos el SDP answer.
    URL destino: https://api.elevenlabs.io/v1/convai/conversation?agent_id=...
    Header: xi-api-key: <API_KEY>
    """
    if request.method == "OPTIONS":
        return _cors(make_response(("", 204)))

    bot_slug = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    try:
        bot_cfg = load_bot(bot_slug)
    except Exception:
        bot_cfg = load_bot("sundin")

    agent_id = _get_eleven_agent(bot_cfg)
    api_key  = _get_eleven_api_key(bot_cfg)

    if not agent_id:
        resp = jsonify({"ok": False, "error": "Falta eleven agent_id en la tarjeta inteligente"})
        return _cors(resp), 400
    if not api_key:
        resp = jsonify({"ok": False, "error": "Falta ELEVEN_API_KEY (o eleven.api_key en el JSON del bot)"})
        return _cors(resp), 500

    sdp_offer = request.get_data(as_text=True) or ""
    if not sdp_offer.strip():
        resp = jsonify({"ok": False, "error": "Cuerpo vacío: envía offer SDP como text/plain o application/sdp"})
        return _cors(resp), 400

    try:
        # Proxy hacia ElevenLabs
        upstream = requests.post(
            "https://api.elevenlabs.io/v1/convai/conversation",
            params={"agent_id": agent_id},
            headers={
                "Content-Type": "application/sdp",
                "xi-api-key": api_key,           # <- clave correcta para Eleven
            },
            data=sdp_offer,
            timeout=25,
        )
        if upstream.status_code >= 400:
            resp = jsonify({
                "ok": False,
                "error": "ElevenLabs error",
                "status": upstream.status_code,
                "detail": upstream.text[:2000],
            })
            return _cors(resp), 502

        # Devolver ANSWER SDP tal cual (para RTCPeerConnection.setRemoteDescription)
        answer_sdp = upstream.text
        resp = make_response(answer_sdp, 200)
        resp.headers["Content-Type"] = "application/sdp"
        return _cors(resp)

    except requests.Timeout:
        return _cors(jsonify({"ok": False, "error": "Timeout con ElevenLabs"})), 504
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": "Proxy exception", "detail": str(e)})), 500
