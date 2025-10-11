# routes/eleven_webrtc.py
# POST /eleven/webrtc?bot=<slug>
# - Lee el agent_id desde la tarjeta inteligente
# - Pide token efímero a ElevenLabs (XI_API_KEY)
# - Reenvía tu SDP offer y devuelve el SDP answer
# Incluye logging detallado para diagnosticar 500 rápidamente.

import os
import sys
import traceback
import requests
from flask import Blueprint, request, make_response, jsonify
from utils.bot_loader import load_bot

bp = Blueprint("eleven_webrtc", __name__, url_prefix="/eleven")

XI_API_KEY       = (os.getenv("XI_API_KEY") or os.getenv("ELEVEN_API_KEY") or "").strip()
ELEVEN_TOKEN_URL = os.getenv("ELEVEN_TOKEN_URL", "https://api.elevenlabs.io/v1/realtime/token")
ELEVEN_SDP_URL   = os.getenv("ELEVEN_SDP_URL",   "https://api.elevenlabs.io/v1/realtime/sdp")

# Permite WP dominio (ajústalo si usas otros)
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
        card = None
    if not isinstance(card, dict):
        return ""
    agent_id = (
        card.get("eleven_agent_id")
        or ((card.get("eleven") or {}).get("agent_id"))
        or ((card.get("realtime") or {}).get("voice_agent_id"))
        or ""
    )
    return (agent_id or "").strip()

@bp.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "has_api_key": bool(XI_API_KEY),
        "token_url": ELEVEN_TOKEN_URL,
        "sdp_url": ELEVEN_SDP_URL
    })

@bp.route("/webrtc", methods=["OPTIONS", "POST"])
def eleven_webrtc_sdp():
    # Preflight
    if request.method == "OPTIONS":
        return _cors(make_response(("", 204)))

    try:
        # 0) Validaciones base
        if not XI_API_KEY:
            print("[ELEVEN] ❌ XI_API_KEY no está configurada", file=sys.stderr)
            return _cors(make_response(jsonify({
                "ok": False, "where": "env", "error": "XI_API_KEY/ELEVEN_API_KEY no configurada"
            }), 500))

        bot_slug = (request.args.get("bot") or "sundin").strip()
        agent_id = _agent_id_from_card(bot_slug)
        print(f"[ELEVEN] bot={bot_slug} agent_id={agent_id}", flush=True)
        if not agent_id:
            return _cors(make_response(jsonify({
                "ok": False, "where": "card", "error": f"No se encontró eleven_agent_id para bot '{bot_slug}'"
            }), 404))

        sdp_offer = request.get_data(as_text=True) or ""
        if not sdp_offer.strip():
            return _cors(make_response(jsonify({
                "ok": False, "where": "client", "error": "Body SDP offer vacío"
            }), 400))

        # 1) Token efímero
        try:
            t = requests.post(ELEVEN_TOKEN_URL, headers={"xi-api-key": XI_API_KEY}, timeout=15)
            print(f"[ELEVEN] token status={t.status_code}", flush=True)
            if t.status_code != 200:
                return _cors(make_response(jsonify({
                    "ok": False, "where": "token", "status": t.status_code, "detail": t.text
                }), 502))
            tj = t.json() or {}
            token = (tj.get("token") or tj.get("access_token") or "").strip()
            if not token:
                return _cors(make_response(jsonify({
                    "ok": False, "where": "token", "error": "Respuesta sin token"
                }), 502))
        except requests.Timeout:
            return _cors(make_response(jsonify({"ok": False, "where": "token", "error": "Timeout"}), 504))
        except Exception as e:
            print(f"[ELEVEN] token exception: {e}", file=sys.stderr)
            return _cors(make_response(jsonify({"ok": False, "where": "token", "error": str(e)}), 502))

        # 2) Reenviar SDP → obtener answer
        rtc_url = f"{ELEVEN_SDP_URL}?agent_id={agent_id}"
        try:
            r = requests.post(
                rtc_url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/sdp"},
                data=sdp_offer,
                timeout=20,
            )
            print(f"[ELEVEN] sdp status={r.status_code}", flush=True)
            if r.status_code != 200:
                # Devolver detalle para ver por qué Eleven rechazó
                return _cors(make_response(jsonify({
                    "ok": False, "where": "sdp", "status": r.status_code, "detail": r.text
                }), 502))
            resp = make_response(r.text, 200)
            resp.headers["Content-Type"] = "application/sdp; charset=utf-8"
            return _cors(resp)
        except requests.Timeout:
            return _cors(make_response(jsonify({"ok": False, "where": "sdp", "error": "Timeout"}), 504))
        except Exception as e:
            print(f"[ELEVEN] sdp exception: {e}", file=sys.stderr)
            return _cors(make_response(jsonify({"ok": False, "where": "sdp", "error": str(e)}), 502))

    except Exception as e:
        # Captura de fallas inesperadas (traza al log y JSON al cliente)
        print("[ELEVEN] Unhandled exception in /eleven/webrtc")
        traceback.print_exc()
        return _cors(make_response(jsonify({
            "ok": False, "where": "server", "error": str(e)
        }), 500))
