# voice_realtime.py
import os
import httpx
import requests
from flask import Blueprint, request, Response, send_from_directory, jsonify, make_response
from twilio.twiml.voice_response import VoiceResponse, Gather
from utils.bot_loader import load_bot

bp = Blueprint("voice_realtime", __name__, url_prefix="/voice-realtime")

# Carpeta temporal para audios (PSTN actual)
TMP_DIR = "/tmp"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# =========================
# Helpers comunes
# =========================
def _corsify(resp):
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _system_instructions_from_bot(bot_cfg: dict) -> str:
    """
    Soporta tus formatos de JSON:
    - { instructions: { system_prompt: "..." } }
    - { system_prompt: "..." }
    - { prompt: "..." }
    """
    if not isinstance(bot_cfg, dict):
        return ""
    ins = (bot_cfg.get("instructions") or {})
    if isinstance(ins, dict) and ins.get("system_prompt"):
        return str(ins["system_prompt"])
    if bot_cfg.get("system_prompt"):
        return str(bot_cfg["system_prompt"])
    if bot_cfg.get("prompt"):
        return str(bot_cfg["prompt"])
    return ""

def _synthesize_tts_wav(text: str, voice: str = "cedar", speed: float = 0.95) -> bytes:
    """
    Genera audio con OpenAI TTS en formato WAV 8 kHz (ideal para PSTN).
    Usado por el flujo PSTN/Gather existente (no se elimina).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY no configurada")
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": "gpt-4o-mini-tts",
        "voice": voice or "cedar",
        "input": text or "",
        "format": "wav",
        "sample_rate": 8000,
        "speed": speed
    }
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            "https://api.openai.com/v1/audio/speech",
            headers=headers,
            json=payload
        )
        r.raise_for_status()
        return r.content

# =========================
# 🔴 NUEVO: WebRTC Realtime
# =========================
@bp.get("/webrtc/health")
def webrtc_health():
    # Defaults si no hay bot
    model = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
    voice = os.getenv("REALTIME_VOICE", "cedar")
    resp = jsonify({"ok": True, "service": "webrtc", "model": model, "voice": voice})
    return _corsify(resp)

@bp.route("/webrtc/session", methods=["POST", "OPTIONS"])
def webrtc_session():
    """
    Crea una sesión efímera Realtime (WebRTC) en OpenAI y devuelve el client_secret.
    Front (tarjeta inteligente / web) usa este endpoint para iniciar la llamada WebRTC directa a OpenAI.
    
    Uso:
      POST /voice-realtime/webrtc/session?bot=whatsapp:+18326213202
      POST /voice-realtime/webrtc/session?bot=ninafit
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    if not OPENAI_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "OPENAI_API_KEY no configurada"})), 500

    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or ""
    bot_cfg = {}
    if bot_id:
        try:
            bot_cfg = load_bot(bot_id)
        except Exception:
            # Si falla, no rompemos: bot vacío y seguimos con defaults
            bot_cfg = {}

    # Instrucciones, modelo, voz y modalidades
    instructions = _system_instructions_from_bot(bot_cfg)
    model_from_json = (bot_cfg.get("realtime") or {}).get("model")
    voice_from_json = (bot_cfg.get("realtime") or {}).get("voice")

    model_to_use = model_from_json or os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
    voice_to_use = voice_from_json or os.getenv("REALTIME_VOICE", "cedar")
    modalities_to_use = (bot_cfg.get("realtime") or {}).get("modalities", ["audio", "text"]) or ["audio", "text"]

    # Construcción de payload Realtime
    payload = {
        "model": model_to_use,
        "voice": voice_to_use,
        "modalities": modalities_to_use,
        "instructions": instructions
        # Si quieres forzar VAD server-side, puedes añadir aquí:
        # , "turn_detection": {"type": "server_vad", "silence_duration_ms": 1200}
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1",
            },
            json=payload,
            timeout=25,
        )
        if r.status_code >= 400:
            resp = jsonify({
                "ok": False,
                "error": "OpenAI Realtime error",
                "status": r.status_code,
                "detail": r.text,
                "payload": payload
            })
            return _corsify(resp), 502

        data = r.json()
        # data contiene { client_secret: {value, expires_at}, ... }
        resp = jsonify({
            "ok": True,
            "session": data,
            "bot_id": bot_id or None,
            "applied": {
                "model": model_to_use,
                "voice": voice_to_use,
                "modalities": modalities_to_use
            }
        })
        return _corsify(resp)
    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout creando sesión"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción creando sesión", "detail": str(e)})), 500

# ======================================================
# 🔵 EXISTENTE: PSTN (Twilio Voice con Gather) - INTACTO
# ======================================================

# ──────────────────────────────────────────────
# Llamada entrante (PSTN)
# ──────────────────────────────────────────────
@bp.route("/call", methods=["POST"])
def handle_incoming_call():
    to_number = request.values.get("To")
    bot_cfg = load_bot(f"whatsapp:{to_number}")

    greeting = bot_cfg.get("greeting", "Hola, gracias por llamar.")
    _voice = (bot_cfg.get("realtime", {}) or {}).get("voice", "cedar")

    resp = VoiceResponse()
    try:
        audio_bytes = _synthesize_tts_wav(greeting, voice=_voice, speed=0.95)
        filename = f"greeting_{os.getpid()}.wav"
        audio_path = os.path.join(TMP_DIR, filename)
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
        resp.play(f"{request.url_root}voice-realtime/media/{filename}")
    except Exception:
        resp.say(greeting, voice="Polly.Salli", language="es-ES")

    gather = Gather(
        input="speech",
        action=f"{request.url_root}voice-realtime/response",
        method="POST",
        language="es-US",   # mejor para latinos en EE.UU.
        timeout=5
    )
    gather.say("¿En qué puedo ayudarle hoy?")
    resp.append(gather)

    return Response(str(resp), mimetype="text/xml")

# ──────────────────────────────────────────────
# Respuesta después del Gather (PSTN)
# ──────────────────────────────────────────────
@bp.route("/response", methods=["POST"])
def handle_response():
    to_number = request.values.get("To")
    user_speech = request.values.get("SpeechResult", "")

    bot_cfg = load_bot(f"whatsapp:{to_number}")
    system_prompt = bot_cfg.get("system_prompt", "Eres un asistente en español.")
    model = bot_cfg.get("model", "gpt-4o")
    voice = bot_cfg.get("realtime", {}).get("voice", "cedar")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    # 1) Chat
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

    # 2) TTS → WAV 8 kHz
    with httpx.Client(timeout=60.0) as client:
        r2 = client.post(
            "https://api.openai.com/v1/audio/speech",
            headers=headers,
            json={
                "model": "gpt-4o-mini-tts",
                "voice": voice,
                "input": text_reply,
                "format": "wav",
                "sample_rate": 8000,
                "speed": 0.95
            }
        )
        r2.raise_for_status()

    filename = f"reply_{os.getpid()}.wav"
    audio_path = os.path.join(TMP_DIR, filename)
    with open(audio_path, "wb") as f:
        f.write(r2.content)

    resp = VoiceResponse()
    resp.play(f"{request.url_root}voice-realtime/media/{filename}")
    resp.say("¿Quiere más información? Puede hacer otra pregunta.")

    return Response(str(resp), mimetype="text/xml")

# ──────────────────────────────────────────────
# Servir archivos temporales para Twilio (PSTN)
# ──────────────────────────────────────────────
@bp.route("/media/<filename>", methods=["GET"])
def serve_media(filename):
    return send_from_directory(TMP_DIR, filename, mimetype="audio/wav")
