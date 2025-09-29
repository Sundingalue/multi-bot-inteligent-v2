# routes/realtime_session.py
# Endpoint para crear una sesión efímera de voz en tiempo real (OpenAI Realtime)
# Se registra como Blueprint en main.py. No cambia tu Start Command.

import os
import requests
from flask import Blueprint, jsonify, current_app, request
from utils.timezone_utils import hora_houston

# Cargador de bots por JSON (tarjeta inteligente)
from utils.bot_loader import load_bot

bp = Blueprint("realtime", __name__, url_prefix="/realtime")

# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN CENTRAL DEL AVATAR (AJUSTA SOLO AQUÍ)

# Modelo y voz por defecto (si el JSON del cliente no especifica)
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "cedar")

# 🎙️ VAD (sensibilidad del micrófono)
# Recomendado para fluidez y menos cortes:
VAD_SILENCE_MS   = 1300     # 1200–1400; más alto = espera más silencio para ceder turno
# Campos avanzados: algunas versiones del backend NO los aceptan.
# Por eso los dejamos desactivados por defecto para que nada se rompa.
ADVANCED_VAD_ENABLED = False  # ← Cambia a True solo si tu versión lo soporta

VAD_MIN_VOICE_MS = 450       # 400–500; ignora ráfagas cortas (solo si ADVANCED_VAD_ENABLED=True)
VAD_THRESHOLD    = 0.92      # 0.90–0.96; más alto = menos sensible (solo si ADVANCED_VAD_ENABLED=True)
# ─────────────────────────────────────────────────────────────


@bp.get("/health")
def health():
    # Exponemos los valores vigentes para diagnóstico rápido
    return jsonify({
        "ok": True,
        "service": "realtime",
        "model": REALTIME_MODEL,
        "vad": {
            "silence_ms": VAD_SILENCE_MS,
            "advanced_enabled": ADVANCED_VAD_ENABLED,
            "min_voice_ms": VAD_MIN_VOICE_MS if ADVANCED_VAD_ENABLED else None,
            "threshold": VAD_THRESHOLD if ADVANCED_VAD_ENABLED else None,
        }
    })


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
    # Cargar configuración del bot desde JSON (escalable)
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

    # Construimos el bloque VAD de forma segura (solo enviamos lo soportado)
    turn_detection = {
        "type": "server_vad",
        "silence_duration_ms": int(VAD_SILENCE_MS)
    }
    if ADVANCED_VAD_ENABLED:
        # Estos dos campos SOLO se incluyen si activas el flag.
        # Si tu backend no los acepta, déjalos desactivados para evitar 4xx.
        if VAD_MIN_VOICE_MS is not None:
            turn_detection["min_voice_ms"] = int(VAD_MIN_VOICE_MS)
        if VAD_THRESHOLD is not None:
            turn_detection["threshold"] = float(VAD_THRESHOLD)

    payload = {
        "model": model_to_use,
        "voice": voice_to_use,
        "modalities": modalities_to_use,
        "instructions": instructions,
        "turn_detection": turn_detection
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
