# voice_realtime.py
import os
import glob
import json
import httpx
from flask import Blueprint, request, Response, send_from_directory
from twilio.twiml.voice_response import VoiceResponse, Gather

bp = Blueprint("voice_realtime", __name__, url_prefix="/voice-realtime")

# Carpeta temporal para audios
TMP_DIR = "/tmp"

# ──────────────────────────────────────────────
# Helpers: resolver bot por número EXCLUSIVAMENTE desde bots/*.json
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

def _load_bot_cfg_by_number_only_bots_folder(to_number: str):
    """
    Busca en bots/*.json una entrada que corresponda al número.
    NUNCA lee tarjeta_inteligente.
    """
    canon_to = _canonize_phone(to_number)  # ej: +18326213202
    try:
        for path in glob.glob(os.path.join("bots", "*.json")):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue

            # 1) Claves de primer nivel (p.ej. "whatsapp:+18326213202")
            for key, cfg in data.items():
                if not isinstance(cfg, dict):
                    continue
                if _canonize_phone(key) == canon_to:
                    return cfg

            # 2) channels.whatsapp.number o whatsapp_number dentro del objeto
            for _, cfg in data.items():
                if not isinstance(cfg, dict):
                    continue
                ch = cfg.get("channels") or {}
                wa = ch.get("whatsapp") or {}
                num = wa.get("number") or cfg.get("whatsapp_number") or ""
                if _canonize_phone(num) == canon_to:
                    return cfg
    except Exception:
        pass
    return None

def _effective_system_prompt(bot_cfg: dict) -> str:
    if not isinstance(bot_cfg, dict):
        return ""
    ins = bot_cfg.get("instructions") or {}
    if isinstance(ins, dict) and ins.get("system_prompt"):
        return str(ins["system_prompt"])
    if bot_cfg.get("system_prompt"):
        return str(bot_cfg["system_prompt"])
    if bot_cfg.get("prompt"):
        return str(bot_cfg["prompt"])
    return ""

def _effective_greeting(bot_cfg: dict) -> str:
    if not isinstance(bot_cfg, dict):
        return "Hola, gracias por llamar. ¿En qué puedo ayudarle hoy?"
    return (
        bot_cfg.get("voice_greeting")
        or bot_cfg.get("greeting")
        or f"Hola, gracias por llamar a {bot_cfg.get('business_name', bot_cfg.get('name','nuestra empresa'))}. ¿En qué puedo ayudarle hoy?"
    )

def _effective_voice(bot_cfg: dict) -> str:
    # Prioriza realtime.voice (p. ej. "cedar")
    if not isinstance(bot_cfg, dict):
        return "alloy"
    rt = bot_cfg.get("realtime") or {}
    return (rt.get("voice") or bot_cfg.get("voice") or "alloy")

def _effective_model_text(bot_cfg: dict) -> str:
    # Modelo para chat/completions (texto)
    if not isinstance(bot_cfg, dict):
        return "gpt-4o"
    return (bot_cfg.get("model") or "gpt-4o")

# ──────────────────────────────────────────────
# Llamada entrante
# ──────────────────────────────────────────────
@bp.route("/call", methods=["POST"])
def handle_incoming_call():
    to_number = request.values.get("To")
    bot_cfg = _load_bot_cfg_by_number_only_bots_folder(to_number)

    # Si no hay config, devolvemos un fallback amable
    if not bot_cfg:
        resp = VoiceResponse()
        resp.say("Lo siento, no hay un bot configurado para este número de voz.")
        return Response(str(resp), mimetype="text/xml")

    greeting = _effective_greeting(bot_cfg)
    resp = VoiceResponse()

    # Saludo inicial (TTS de Twilio solo para esta línea)
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
    user_speech = request.values.get("SpeechResult", "")

    bot_cfg = _load_bot_cfg_by_number_only_bots_folder(to_number)
    if not bot_cfg:
        resp = VoiceResponse()
        resp.say("Lo siento, hubo un problema técnico.")
        return Response(str(resp), mimetype="text/xml")

    system_prompt = _effective_system_prompt(bot_cfg) or "Eres un asistente en español."
    model = _effective_model_text(bot_cfg)  # texto
    voice = _effective_voice(bot_cfg)       # p. ej. "cedar"

    headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"}

    # 1) Respuesta en texto del modelo
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

    # 2) TTS en la voz definida por el bot (cedar, etc.)
    with httpx.Client(timeout=60.0) as client:
        r2 = client.post(
            "https://api.openai.com/v1/audio/speech",
            headers=headers,
            json={"model": "gpt-4o-mini-tts", "voice": voice, "input": text_reply}
        )
        r2.raise_for_status()

    # Guardar temporalmente el audio
    os.makedirs(TMP_DIR, exist_ok=True)
    filename = f"reply_{os.getpid()}.mp3"
    audio_path = os.path.join(TMP_DIR, filename)
    with open(audio_path, "wb") as f:
        f.write(r2.content)

    # Twilio responde con <Play> usando URL pública
    resp = VoiceResponse()
    resp.play(f"{request.url_root}voice-realtime/media/{filename}")
    resp.say("¿Quiere más información? Puede hacer otra pregunta.")

    return Response(str(resp), mimetype="text/xml")

# ──────────────────────────────────────────────
# Servir archivos temporales para Twilio
# ──────────────────────────────────────────────
@bp.route("/media/<filename>", methods=["GET"])
def serve_media(filename):
    return send_from_directory(TMP_DIR, filename, mimetype="audio/mpeg")
