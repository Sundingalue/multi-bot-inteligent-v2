# routes/realtime_session.py
# Endpoint para crear una sesión efímera de voz en tiempo real (OpenAI Realtime)
# Se registra como Blueprint en main.py. No cambia tu Start Command.

import os
import requests
from flask import Blueprint, jsonify, current_app, request  # ← añadido request
from utils.timezone_utils import hora_houston

# ← NUEVO: cargador de bots por JSON
from utils.bot_loader import load_bot

bp = Blueprint("realtime", __name__, url_prefix="/realtime")

# Opciones por defecto (puedes cambiarlas por variables de entorno si quieres)
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "cedar")  # puedes cambiar por otra en tus env vars

@bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "realtime", "model": REALTIME_MODEL})

@bp.post("/session")
def create_session():
    """
    Crea una sesión efímera con OpenAI Realtime.
    Usa la misma OPENAI_API_KEY que ya tienes configurada en tu proyecto.
    """
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY no configurada"}), 500
    
    hora_actual = hora_houston()

    # ─────────────────────────────────────────────────────────────
    # NUEVO: Cargar configuración del bot desde JSON (escalable)
    # Prioridad: query ?bot=ID  -> header X-Bot-Id -> 'sundin'
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    try:
        bot_cfg = load_bot(bot_id)
    except Exception:
        # Fallback duro a 'sundin' si el solicitado no existe o hay error de lectura
        bot_cfg = load_bot("sundin")

    # Prompt del sistema desde el JSON del cliente
    instructions = bot_cfg.get("instructions", {}).get("system_prompt", "")

    # Modelo/voz/modalidades desde JSON con fallback a env/defaults
    model_from_json = bot_cfg.get("realtime", {}).get("model")
    voice_from_json = bot_cfg.get("realtime", {}).get("voice")
    modalities_from_json = bot_cfg.get("realtime", {}).get("modalities", ["audio", "text"])

    model_to_use = model_from_json or REALTIME_MODEL
    voice_to_use = voice_from_json or REALTIME_VOICE
    modalities_to_use = modalities_from_json or ["audio", "text"]
    # ─────────────────────────────────────────────────────────────

    payload = {
        "model": model_to_use,
        "voice": voice_to_use,
        "modalities": modalities_to_use,
        "instructions": instructions,

        # ⬇️ Menos sensibilidad al ruido (server VAD)
        "turn_detection": {
    "type": "server_vad",
    "silence_duration_ms": 1200,  # mantiene fluidez
    "min_voice_ms": 220,          # filtra ráfagas cortas (teclado/clics)
    "threshold": 0.8             # 0.0–1.0; más alto = menos sensible
}

    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )
        if r.status_code >= 400:
            return jsonify({"ok": False, "error": "OpenAI Realtime error", "detail": r.text}), 502

        return jsonify({"ok": True, "session": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": "Excepción creando sesión", "detail": str(e)}), 500
