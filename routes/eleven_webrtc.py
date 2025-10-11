# routes/eleven_webrtc.py
from flask import Blueprint, request, jsonify, Response, current_app
import os, requests

bp = Blueprint("eleven_webrtc", __name__, url_prefix="/eleven")

# Preflight CORS para Safari/iOS
@bp.route("/webrtc", methods=["OPTIONS"])
def eleven_webrtc_options():
    resp = Response("", status=200)
    # Permite origenes que tu app ya valida en main.py (add_cors_headers también actúa)
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    # ⬇️ Incluir headers que usa el front
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, xi-api-key, Accept"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

@bp.route("/webrtc", methods=["POST"])
def eleven_webrtc_post():
    """
    Recibe SDP Offer del navegador y lo reenvía a ElevenLabs ConvAI,
    devolviendo el SDP Answer en texto plano (Content-Type: application/sdp).
    """
    offer_sdp = request.data or b""
    if not offer_sdp:
        return jsonify({"error": "Empty SDP offer"}), 400

    # === Modelo y voz: vienen por query o .env (fallback) ===
    model = request.args.get("model") or os.getenv("ELEVEN_DEFAULT_MODEL", "eleven_multilingual_v2")
    voice_id = request.args.get("voice_id") or os.getenv("ELEVEN_DEFAULT_VOICE_ID", "")

    # === API Key / Token efímero ===
    # 1) Token efímero que te devolvió /realtime/session (frontend lo manda en Authorization)
    bearer = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    # 2) Fallback a API key fija por si lo quieres probar directo
    xi_api_key = bearer or (os.getenv("ELEVEN_API_KEY") or "").strip()
    if not xi_api_key:
        return jsonify({"error": "Missing ElevenLabs token/API key"}), 401

    # === URL base de ConvAI (puedes cambiarla por env si Eleven cambia el path) ===
    base_url = (os.getenv("ELEVEN_CONVAI_URL_BASE") or "https://api.elevenlabs.io/v1/convai/conversation").rstrip("/")
    # En la práctica, el path suele ser /webrtc o /webRTC; usamos "webrtc"
    url = f"{base_url}/webrtc?mode={model}"
    if voice_id:
        url += f"&voice_id={voice_id}"

    headers = {
        # Lo importante: el upstream espera SDP crudo
        "Content-Type": "application/sdp",
        "Accept": "application/sdp",
        # Enviamos ambos formatos por compatibilidad
        "Authorization": f"Bearer {xi_api_key}",
        "xi-api-key": xi_api_key,
    }

    try:
        r = requests.post(url, data=offer_sdp, headers=headers, timeout=30)
    except requests.RequestException as e:
        current_app.logger.error(f"[Eleven] POST failed: {e}")
        return jsonify({"error": "Failed to reach ElevenLabs"}), 502

    if r.status_code >= 400:
        # Loguea para depurar rápido
        current_app.logger.error(f"[Eleven] HTTP {r.status_code} body={r.text[:400]}")
        return jsonify({"error": "ElevenLabs upstream error", "status": r.status_code}), 502

    # Debe devolver SDP puro (text/plain o application/sdp); forzamos application/sdp
    resp = Response(r.text, status=200, mimetype="application/sdp")
    # CORS espejo (el after_request global también agrega)
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    return resp
