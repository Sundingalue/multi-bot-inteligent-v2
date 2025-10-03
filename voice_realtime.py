# voice_realtime.py
import os
import requests
from flask import Blueprint, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from utils.bot_loader import load_bot

bp = Blueprint("voice_realtime", __name__, url_prefix="/voice-realtime")

# Llamada entrante
@bp.route("/call", methods=["POST"])
def handle_incoming_call():
    to_number = request.values.get("To")
    bot_cfg = load_bot(f"whatsapp:{to_number}")

    greeting = bot_cfg.get("greeting", "Hola, gracias por llamar.")
    resp = VoiceResponse()

    # Reproduce saludo
    resp.say(greeting, voice="Polly.Salli", language="es-ES")

    # Espera respuesta del usuario
    gather = Gather(
        input="speech",
        action="/voice-realtime/response",
        method="POST",
        language="es-ES",
        timeout=5
    )
    gather.say("¿En qué puedo ayudarle hoy?")
    resp.append(gather)

    return Response(str(resp), mimetype="text/xml")

# Respuesta después del Gather
@bp.route("/response", methods=["POST"])
def handle_response():
    to_number = request.values.get("To")
    user_speech = request.values.get("SpeechResult")

    bot_cfg = load_bot(f"whatsapp:{to_number}")
    system_prompt = bot_cfg.get("system_prompt", "Eres un asistente en español.")
    model = bot_cfg.get("model", "gpt-4o")
    voice = bot_cfg.get("realtime", {}).get("voice", "alloy")

    # 1) Llamamos a OpenAI para generar respuesta
    headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"}
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_speech}
            ]
        }
    )
    text_reply = r.json()["choices"][0]["message"]["content"]

    # 2) Generamos audio con OpenAI
    r2 = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers=headers,
        json={"model": "gpt-4o-mini-tts", "voice": voice, "input": text_reply}
    )

    # Guardar temporalmente el audio
    audio_file = "/tmp/reply.mp3"
    with open(audio_file, "wb") as f:
        f.write(r2.content)

    # Twilio responde con <Play>
    resp = VoiceResponse()
    resp.play(audio_file)
    resp.say("¿Quiere más información? Puede hacer otra pregunta.")

    return Response(str(resp), mimetype="text/xml")
