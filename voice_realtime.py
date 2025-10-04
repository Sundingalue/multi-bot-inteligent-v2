# voice_realtime.py
import os
import httpx
from flask import Blueprint, request, Response, send_from_directory, current_app
from twilio.twiml.voice_response import VoiceResponse, Gather
from utils.bot_loader import load_bot, BotConfigNotFound

bp = Blueprint("voice_realtime", __name__, url_prefix="/voice-realtime")

# Carpeta temporal para audios
TMP_DIR = "/tmp"

# ──────────────────────────────────────────────
# Helpers mínimos (no rompen nada existente)
# ──────────────────────────────────────────────
def _canonize_phone(raw: str) -> str:
    s = str(raw or "").strip()
    for p in ("whatsapp:", "tel:", "sip:", "client:"):
        if s.startswith(p):
            s = s[len(p):]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits

def _get_bot_cfg_by_any_number(to_number: str):
    """
    1) Intenta tarjeta_inteligente/<slug>.json (como antes).
    2) Si no existe, busca la clave en app.config['BOTS_CONFIG'] (bots/*.json agregado).
    """
    # (1) tarjeta_inteligente: slug = "whatsapp:+1..."
    slug = f"whatsapp:{_canonize_phone(to_number)}"
    try:
        return load_bot(slug)
    except BotConfigNotFound:
        pass  # seguimos al fallback sin romper nada

    # (2) Fallback: diccionarios agregados de bots/*.json
    bots_config = current_app.config.get("BOTS_CONFIG", {}) or {}
    canon = _canonize_phone(to_number)
    # buscar por clave que coincida en E.164 o que sea exactamente "whatsapp:+1..."
    for key, cfg in bots_config.items():
        try:
            if _canonize_phone(key) == canon:
                return cfg
        except Exception:
            continue
    # si no lo encontramos, devolvemos None
    return None

# ──────────────────────────────────────────────
# Llamada entrante
# ──────────────────────────────────────────────
@bp.route("/call", methods=["POST"])
def handle_incoming_call():
    to_number = request.values.get("To")
    bot_cfg = _get_bot_cfg_by_any_number(to_number)

    resp = VoiceResponse()

    if not bot_cfg:
        # No reventamos con 500: devolvemos TwiML claro
        resp.say("Lo siento, este número no está asignado a ningún bot de voz.")
        return Response(str(resp), mimetype="text/xml")

    greeting = bot_cfg.get(
        "greeting",
        "Hola, gracias por llamar. ¿En qué puedo ayudarle?"
    )

    # Reproduce saludo inicial con TTS de Twilio (sin tocar OpenAI)
    resp.say(greeting, voice="Polly.Salli", language="es-ES")

    # Espera respuesta del usuario
    gather = Gather(
        input="speech",
        action=f"{request.url_root}voice-realtime/response",
        method="POST",
        language="es-ES",
        timeout=5
    )
    gather.say("¿En qué puedo ayudarle hoy?")
    resp.append(gather)

    return Response(str(resp), mimetype="text/xml")


# ──────────────────────────────────────────────
# Respuesta después del Gather
# ──────────────────────────────────────────────
@bp.route("/response", methods=["POST"])
def handle_response():
    to_number = request.values.get("To")
    user_speech = (request.values.get("SpeechResult", "") or "").strip()

    bot_cfg = _get_bot_cfg_by_any_number(to_number)

    resp = VoiceResponse()
    if not bot_cfg:
        resp.say("Lo siento, hubo un problema al cargar el bot.")
        return Response(str(resp), mimetype="text/xml")

    system_prompt = bot_cfg.get("system_prompt", "Eres un asistente en español.")
    model = bot_cfg.get("model", "gpt-4o")
    voice = (bot_cfg.get("realtime") or {}).get("voice", "alloy")

    # Si no habló, repreguntamos sin romper
    if not user_speech:
        gather = Gather(
            input="speech",
            action=f"{request.url_root}voice-realtime/response",
            method="POST",
            language="es-ES",
            timeout=5
        )
        gather.say("No escuché nada. ¿Podría repetir, por favor?")
        resp.append(gather)
        return Response(str(resp), mimetype="text/xml")

    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
        "Content-Type": "application/json",
    }

    # 1) Llamada a OpenAI Chat
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
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
        r.raise_for_status()
        text_reply = r.json()["choices"][0]["message"]["content"]

    # 2) Generamos audio con OpenAI TTS
    with httpx.Client(timeout=60.0) as client:
        r2 = client.post(
            "https://api.openai.com/v1/audio/speech",
            headers=headers,
            json={"model": "tts-1", "voice": voice, "input": text_reply}
        )
        r2.raise_for_status()

    # Guardar temporalmente el audio
    os.makedirs(TMP_DIR, exist_ok=True)
    filename = f"reply_{os.getpid()}_{abs(hash(text_reply)) % 10_000}.mp3"
    audio_path = os.path.join(TMP_DIR, filename)
    with open(audio_path, "wb") as f:
        f.write(r2.content)

    # Twilio responde con <Play> usando URL pública
    resp.play(f"{request.url_root}voice-realtime/media/{filename}")
    resp.say("¿Quiere más información? Puede hacer otra pregunta.")

    return Response(str(resp), mimetype="text/xml")


# ──────────────────────────────────────────────
# Servir archivos temporales para Twilio
# ──────────────────────────────────────────────
@bp.route("/media/<filename>", methods=["GET"])
def serve_media(filename):
    return send_from_directory(TMP_DIR, filename, mimetype="audio/mpeg")
