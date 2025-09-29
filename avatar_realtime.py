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

# ── Defaults de VAD (sensibilidad). Solo se enviarán si están definidos/overriden.
#    Nota: mantener 1100 como tu valor actual por compatibilidad.
def _to_int(x):
    try:
        return int(x) if x is not None and str(x) != "" else None
    except Exception:
        return None

def _to_float(x):
    try:
        return float(x) if x is not None and str(x) != "" else None
    except Exception:
        return None

VAD_SILENCE_MS_DEFAULT = _to_int(os.getenv("REALTIME_VAD_SILENCE_MS")) or 1100
VAD_MIN_MS_DEFAULT = _to_int(os.getenv("REALTIME_VAD_MIN_MS"))          # puede ser None
VAD_THRESHOLD_DEFAULT = _to_float(os.getenv("REALTIME_VAD_THRESHOLD"))  # puede ser None


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

    # ── Sensibilidad VAD: permite ajustar sin tocar código (env o query)
    vad_ms = request.args.get("vad_ms", type=int)
    if vad_ms is None:
        vad_ms = VAD_SILENCE_MS_DEFAULT

    vad_min_ms = request.args.get("vad_min_voice_ms", type=int)
    if vad_min_ms is None:
        vad_min_ms = VAD_MIN_MS_DEFAULT  # puede seguir siendo None

    vad_threshold = request.args.get("vad_threshold", type=float)
    if vad_threshold is None:
        vad_threshold = VAD_THRESHOLD_DEFAULT  # puede seguir siendo None

    # Construye el bloque turn_detection solo con claves definidas
    turn_detection = {
        "type": "server_vad",
        "silence_duration_ms": vad_ms
    }
    if vad_min_ms is not None:
        turn_detection["min_voice_ms"] = vad_min_ms
    if vad_threshold is not None:
        turn_detection["threshold"] = vad_threshold

    payload = {
        "model": model_to_use,
        "voice": voice_to_use,
        "modalities": modalities_to_use,
        "instructions": instructions,

        # ⬇️ Sensibilidad al ruido (server VAD) — ahora configurable
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
