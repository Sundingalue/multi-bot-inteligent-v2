# eleven_session.py
# Endpoint para crear sesión efímera de voz (ElevenLabs) y proxy de SDP WebRTC.
# Toma el agent_id desde tu JSON de "tarjeta inteligente", NO desde .env.

import os
import time
import uuid
import requests
from flask import Blueprint, jsonify, request, make_response
from utils.bot_loader import load_bot

bp = Blueprint("eleven", __name__, url_prefix="/eleven")

# ==============================
# Config
# ==============================
XI_API_KEY = (os.getenv("ELEVEN_API_KEY") or os.getenv("XI_API_KEY") or "").strip()
ELEVEN_SDP_ENDPOINT = "https://api.elevenlabs.io/v1/convai/conversations"  # endpoint WebRTC (SDP)

# Tokens efímeros (memoria de proceso)
# token -> {"agent_id": str, "exp": epoch_s}
_ISSUED = {}
_TTL_SECONDS = 120  # 2 minutos de validez para el token efímero

# ==============================
# Helpers
# ==============================
def _corsify(resp):
    """CORS básico por origen (útil cuando no aplica after_request global)."""
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _scheme():
    # respeta proxy/CDN
    xfp = (request.headers.get("X-Forwarded-Proto") or "").lower()
    if xfp in ("http", "https"):
        return xfp
    return "https" if request.is_secure else "http"

def _host_base():
    return f"{_scheme()}://{request.host}"

def _trim_issued():
    now = int(time.time())
    bad = [t for t, rec in _ISSUED.items() if int(rec.get("exp", 0)) < now]
    for t in bad:
        _ISSUED.pop(t, None)

def _get_eleven_agent_id(bot_cfg: dict) -> str:
    if not isinstance(bot_cfg, dict):
        return ""
    # Campo principal en tu JSON:
    aid = (bot_cfg.get("eleven_agent_id") or "").strip()
    if aid:
        return aid
    # fallback opcional por si lo mueven dentro de "realtime"
    rt = bot_cfg.get("realtime") or {}
    return (rt.get("eleven_agent_id") or "").strip()

# ==============================
# Health
# ==============================
@bp.get("/health")
def health():
    ok = bool(XI_API_KEY)
    resp = jsonify({
        "ok": ok,
        "service": "eleven",
        "has_api_key": bool(XI_API_KEY),
        "token_count": len(_ISSUED),
        "ttl_seconds": _TTL_SECONDS
    })
    return _corsify(resp)

# ==============================
# POST /eleven/session
# Crea token efímero y devuelve rtc_url a nuestro proxy /eleven/webrtc
# ==============================
@bp.route("/session", methods=["POST", "OPTIONS"])
def create_session():
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not XI_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "ELEVEN_API_KEY no configurada"})), 500

    # Descubrir bot desde ?bot= | X-Bot-Id | body.bot | fallback "sundin"
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id")
    body = request.get_json(silent=True) or {}
    if not bot_id:
        bot_id = body.get("bot") or "sundin"

    try:
        bot_cfg = load_bot(bot_id)
    except Exception:
        bot_cfg = load_bot("sundin")

    agent_id = _get_eleven_agent_id(bot_cfg)
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": "eleven_agent_id no definido en la tarjeta"})), 400

    # Generar token efímero propio
    tok = uuid.uuid4().hex
    _ISSUED[tok] = {"agent_id": agent_id, "exp": int(time.time()) + _TTL_SECONDS}
    _trim_issued()

    # rtc_url = proxy local; el front hará POST SDP aquí (no exponemos el XI key)
    rtc_url = f"{_host_base()}/eleven/webrtc?token={tok}"

    # (Opcional) devolvemos eco de configuración para debug front
    resp = jsonify({
        "ok": True,
        "token": tok,          # solo para debug local (no se usa como Bearer)
        "rtc_url": rtc_url,
        "agent_id_hint": agent_id[:6] + "…" if len(agent_id) > 6 else agent_id
    })
    return _corsify(resp)

# ==============================
# POST /eleven/webrtc?token=...
# Proxy de SDP → ElevenLabs (con XI_API_KEY) → devuelve SDP answer (text/plain)
# ==============================
@bp.route("/webrtc", methods=["POST", "OPTIONS"])
def webrtc_proxy():
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not XI_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "ELEVEN_API_KEY no configurada"})), 500

    token = (request.args.get("token") or "").strip()
    if not token:
        return _corsify(jsonify({"ok": False, "error": "Falta token"})), 400

    _trim_issued()
    rec = _ISSUED.get(token)
    if not rec:
        return _corsify(jsonify({"ok": False, "error": "Token inválido o expirado"})), 401

    agent_id = rec.get("agent_id", "")
    if not agent_id:
        return _corsify(jsonify({"ok": False, "error": "agent_id no disponible"})), 400

    # Leemos el SDP offer como texto puro
    offer_sdp = request.get_data(as_text=True) or ""
    if not offer_sdp.strip():
        return _corsify(jsonify({"ok": False, "error": "SDP offer vacío"})), 400

    # Reenvío a ElevenLabs (authorization del servidor)
    try:
        # ElevenLabs espera el agent_id (query) y el body SDP (text/plain con content-type application/sdp)
        url = f"{ELEVEN_SDP_ENDPOINT}?agent_id={agent_id}"
        rr = requests.post(
            url,
            data=offer_sdp.encode("utf-8"),
            headers={
                "Authorization": f"Bearer {XI_API_KEY}",
                "Content-Type": "application/sdp",
            },
            timeout=25,
        )
    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout al contactar ElevenLabs"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción al contactar ElevenLabs", "detail": str(e)})), 502

    # Si ElevenLabs falla, devolvemos info para depurar
    if rr.status_code >= 400:
        try_detail = rr.text[:600] if rr.text else ""
        return _corsify(jsonify({
            "ok": False,
            "error": "ElevenLabs error",
            "status": rr.status_code,
            "detail": try_detail
        })), 502

    # Éxito → devolvemos el SDP answer como texto plano (lo espera el RTCPeerConnection)
    resp = make_response(rr.text, 200)
    resp.headers["Content-Type"] = "application/sdp"
    return _corsify(resp)
