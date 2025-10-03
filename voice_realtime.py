# voice_realtime.py
# Maneja llamadas entrantes de Twilio con OpenAI Realtime
# Ubícalo dentro de tu carpeta multi-bot-inteligente/

import os
import requests
from flask import Blueprint, request, Response, jsonify
from twilio.twiml.voice_response import VoiceResponse, Connect
from utils.bot_loader import load_bot
from utils.timezone_utils import hora_houston

bp = Blueprint("voice_realtime", __name__, url_prefix="/voice-realtime")

# ─────────────────────────────────────────────
# Endpoint principal: llamada entrante desde Twilio
# ─────────────────────────────────────────────
@bp.route("/call", methods=["POST"])
def handle_incoming_call():
    """
    Twilio manda aquí cuando entra una llamada.
    Detectamos el número destino y cargamos el JSON del bot correspondiente.
    """
    to_number = request.values.get("To")  # número de Twilio que recibe la llamada
    from_number = request.values.get("From")  # quién llama

    print(f"📞 Nueva llamada entrante: de {from_number} hacia {to_number}")

    # Cargamos el bot correspondiente desde carpeta BOTS
    try:
        bot_cfg = load_bot(f"whatsapp:{to_number}")  # mismo formato que en tu JSON
    except Exception as e:
        print(f"❌ Error cargando bot para {to_number}: {e}")
        return Response("<Response><Say>No se encontró configuración para este número.</Say></Response>", mimetype="text/xml")

    # TwiML: conectamos el audio entrante al WebSocket interno
    resp = VoiceResponse()
    with Connect() as connect:
        connect.stream(
            url=f"wss://{request.host}/voice-realtime/stream?bot=whatsapp:{to_number}"
        )
    resp.append(connect)

    return Response(str(resp), mimetype="text/xml")

# ─────────────────────────────────────────────
# Endpoint para crear sesión Realtime en OpenAI
# ─────────────────────────────────────────────
@bp.route("/stream", methods=["GET", "POST"])
def start_realtime_stream():
    """
    Este endpoint abre un stream WebSocket con OpenAI Realtime.
    Usa la configuración del JSON cargado.
    """
    bot_id = request.args.get("bot")
    if not bot_id:
        return jsonify({"ok": False, "error": "Falta parámetro bot"}), 400

    try:
        bot_cfg = load_bot(bot_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"No se pudo cargar bot {bot_id}", "detail": str(e)}), 500

    # Extraemos configuración del JSON
    model = bot_cfg.get("realtime", {}).get("model", "gpt-4o-realtime-preview-2024-12-17")
    voice = bot_cfg.get("realtime", {}).get("voice", "alloy")
    instructions = bot_cfg.get("system_prompt", "Eres un asistente virtual.")
    modalities = bot_cfg.get("realtime", {}).get("modalities", ["audio", "text"])

    payload = {
        "model": model,
        "voice": voice,
        "modalities": modalities,
        "instructions": instructions,
        "turn_detection": {"type": "server_vad", "silence_duration_ms": 1200}
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )
        if r.status_code >= 400:
            return jsonify({"ok": False, "error": "OpenAI Realtime error", "detail": r.text, "payload": payload}), 502

        return jsonify({"ok": True, "session": r.json(), "payload": payload})

    except Exception as e:
        return jsonify({"ok": False, "error": "Excepción creando sesión", "detail": str(e)}), 500

# ─────────────────────────────────────────────
# Healthcheck rápido
# ─────────────────────────────────────────────
@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "voice_realtime", "time": hora_houston()})
