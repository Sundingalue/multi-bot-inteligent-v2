# routes/eleven_webhook.py
import requests
from flask import Blueprint, request, jsonify, current_app

bp = Blueprint("eleven_webhook", __name__, url_prefix="/billing/webhooks/eleven")

@bp.post("/post-call")
def post_call():
    data = request.get_json(silent=True) or {}
    current_app.logger.info(f"[eleven_webhook] payload: {data}")

    # Datos del número que llamó y al que llamaron
    try:
        call_info = data.get("data", {}).get("conversation_initiation_client_data", {})
        vars = call_info.get("dynamic_variables", {})
        caller = vars.get("system__caller_id")       # Ej: +18323790809
        called = vars.get("system__called_number")   # Ej: +18325512420
    except Exception:
        caller = None
        called = None

    # Analizamos la conversación para buscar palabras clave
    transcript = data.get("data", {}).get("transcript", [])
    user_msgs = " ".join(
        [t.get("message", "").lower() for t in transcript if t.get("role") == "user"]
    )

    # Palabras que activan el envío del enlace
    triggers = ["cita", "agendar", "agenda", "dirección", "mensaje", "link"]

    if any(k in user_msgs for k in triggers) and caller and called:
        try:
            payload = {
                "bot": f"whatsapp:{called}",
                "phone": caller.replace("+1", "").replace("-", ""),
                "channel": "wa"
            }
            resp = requests.post(
                "https://multi-bot-inteligente-v1.onrender.com/send_link",
                json=payload,
                timeout=10
            )
            current_app.logger.info(f"[eleven_webhook] ✅ send_link respuesta: {resp.text}")
        except Exception as e:
            current_app.logger.exception(f"[eleven_webhook] ❌ error enviando mensaje: {e}")

    return jsonify({"ok": True})
