# routes/eleven_webrtc.py
from flask import Blueprint, request, Response, jsonify
import os
import requests

bp = Blueprint("eleven_webrtc", __name__)

def _auth_ok(req) -> bool:
    """Valida Bearer si API_BEARER_TOKEN está definido (igual que tu helper)."""
    api_bearer = (os.environ.get("API_BEARER_TOKEN") or "").strip()
    if not api_bearer:
        return True  # sin token -> libre (modo dev)
    auth = (req.headers.get("Authorization") or "").strip()
    return auth == f"Bearer {api_bearer}"

@bp.route("/eleven/webrtc", methods=["POST"])
def eleven_webrtc_sdp():
    """
    Bridge WebRTC SDP:
      Front (WordPress) -> (POST SDP) -> ESTE endpoint
      ESTE endpoint -> (POST SDP) -> ElevenLabs ConvAI
      Devuelve answer SDP con Content-Type: application/sdp

    Env necesarios:
      - ELEVENLABS_API_KEY         (obligatorio en prod)
      - ELEVEN_MODEL               (opcional; default eleven_multilingual_v2)
      - ELEVEN_VOICE_ID            (opcional; fija la voz por query)
      - ELEVEN_CONVAI_URL_BASE     (opcional; override del endpoint)
    """
    if not _auth_ok(request):
        return jsonify({"error": "Unauthorized"}), 401

    offer_sdp = request.data or b""
    if not offer_sdp:
        return jsonify({"error": "Missing SDP offer body"}), 400

    eleven_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not eleven_key:
        return jsonify({"error": "ELEVENLABS_API_KEY not set"}), 500

    model = (os.environ.get("ELEVEN_MODEL") or "eleven_multilingual_v2").strip()
    voice_id = (os.environ.get("ELEVEN_VOICE_ID") or "").strip()

    # Endpoint típico de ElevenLabs ConvAI (WebRTC/SDP). Dejo override por si cambia.
    base_url = (os.environ.get("ELEVEN_CONVAI_URL_BASE") or
                "https://api.elevenlabs.io/v1/convai/conversation").rstrip("/")

    # Construimos URL con query params estándar (modelo y voz si la fijas por servidor)
    # Ejemplos posibles:
    #   {base}/webRTC?model=eleven_multilingual_v2&voice_id=<VOICE>
    #   o {base}/webrtc ... (algunas docs alternan el casing). Probamos “webRTC”.
    # Puedes ajustar con ELEVEN_CONVAI_URL_BASE si tu cuenta usa otro path.
    path = "webRTC"
    url = f"{base_url}/{path}?model={model}"
    if voice_id:
        url += f"&voice_id={voice_id}"

    # Headers compatibles: muchas integraciones aceptan 'xi-api-key' o Bearer.
    headers = {
        "Content-Type": "application/sdp",
        "xi-api-key": eleven_key,
        "Authorization": f"Bearer {eleven_key}",  # por compatibilidad
    }

    try:
        r = requests.post(url, data=offer_sdp, headers=headers, timeout=30)
    except requests.RequestException as e:
        return jsonify({"error": "Failed to reach ElevenLabs", "detail": str(e)}), 502

    if r.status_code // 100 != 2:
        # Propagamos info para debug (no devolvemos cuerpo completo por tamaño)
        return jsonify({
            "error": "ElevenLabs SDP negotiation failed",
            "status": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "hint": "Revisa ELEVENLABS_API_KEY / modelo / voice_id / URL base"
        }), 502

    # Devolvemos el SDP ANSWER “as is”
    return Response(r.text, status=200, mimetype="application/sdp")
