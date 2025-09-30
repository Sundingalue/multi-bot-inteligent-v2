# routes/realtime_session.py
# Endpoint para crear una sesión efímera de voz en tiempo real (OpenAI Realtime)
# Se registra como Blueprint en main.py. No cambia tu Start Command.

import os
import requests
from flask import Blueprint, jsonify, request
from utils.timezone_utils import hora_houston
from utils.bot_loader import load_bot

bp = Blueprint("realtime", __name__, url_prefix="/realtime")

# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN CENTRAL DEL AVATAR (DEFAULTS DE DESPLIEGUE)
# Puedes sobreescribirlos por:
#   1) Variables de entorno
#   2) Query params (?hold_ms=...&threshold=...&min_voice_ms=...&advanced=1)
#   3) Body JSON: { "vad": { "hold_ms": 900, "threshold": 0.12, "min_voice_ms": 500, "advanced": true } }

# Modelo y voz por defecto
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "cedar")

# 🎙️ VAD server-side (turn_detection)
# Nota: muchas implementaciones de OpenAI Realtime reconocen SIEMPRE:
#   - type = "server_vad"
#   - silence_duration_ms (tiempo de silencio para ceder turno)
# Campos adicionales como "threshold" o "min_voice_ms" pueden variar según versión.
# Por eso los tratamos como "opcionales" detrás de un flag.

# Defaults (puedes fijarlos por ENV)
VAD_SILENCE_MS_DEFAULT    = int(os.getenv("VAD_HOLD_MS", os.getenv("VAD_SILENCE_MS", "1200")))  # 1200–1400 recomendado
ADVANCED_VAD_ENABLED_DEF  = os.getenv("ADVANCED_VAD_ENABLED", "0") in ("1", "true", "True")

# Cuando el backend lo soporta:
# threshold típico en escala 0.0–1.0 (más alto = menos sensible)
VAD_THRESHOLD_DEFAULT     = float(os.getenv("VAD_THRESHOLD", "0.12"))
# ignora ráfagas cortas (en ms)
VAD_MIN_VOICE_MS_DEFAULT  = int(os.getenv("VAD_MIN_VOICE_MS", "500"))


def _to_bool(x, default=False):
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    return str(x).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _clamp(val, lo, hi):
    try:
        v = float(val)
    except Exception:
        return None
    return max(lo, min(hi, v))


def _effective_vad_from_request(req_json) -> dict:
    """
    Calcula el VAD efectivo combinando defaults, ENV, query params y JSON.
    Retorna un dict listo para enviar a OpenAI y otro de auditoría (applied).
    """
    # 1) Defaults/ENV
    hold_ms   = VAD_SILENCE_MS_DEFAULT
    advanced  = ADVANCED_VAD_ENABLED_DEF
    threshold = VAD_THRESHOLD_DEFAULT
    min_voice = VAD_MIN_VOICE_MS_DEFAULT

    # 2) Query params (prioridad sobre defaults/ENV)
    qp = request.args
    if "hold_ms" in qp:
        try: hold_ms = int(qp.get("hold_ms"))
        except: pass
    if "silence_ms" in qp:  # alias
        try: hold_ms = int(qp.get("silence_ms"))
        except: pass
    if "advanced" in qp:
        advanced = _to_bool(qp.get("advanced"), advanced)
    if "threshold" in qp:
        th = _clamp(qp.get("threshold"), 0.0, 1.0)
        if th is not None:
            threshold = th
    if "min_voice_ms" in qp:
        try: min_voice = int(qp.get("min_voice_ms"))
        except: pass

    # 3) JSON body: { "vad": { ... } } (tiene prioridad máxima)
    vad = {}
    if isinstance(req_json, dict):
        vad = (req_json.get("vad") or {}) if "vad" in req_json else {}
    if vad:
        if "hold_ms" in vad:         # alias preferido
            try: hold_ms = int(vad["hold_ms"])
            except: pass
        if "silence_ms" in vad:      # alias alterno
            try: hold_ms = int(vad["silence_ms"])
            except: pass
        if "advanced" in vad:
            advanced = _to_bool(vad["advanced"], advanced)
        if "threshold" in vad:
            th = _clamp(vad["threshold"], 0.0, 1.0)
            if th is not None:
                threshold = th
        if "min_voice_ms" in vad:
            try: min_voice = int(vad["min_voice_ms"])
            except: pass

    # Sanitización final y límites razonables
    # - hold_ms: 200–15000 ms (0.2s – 15s)
    hold_ms = int(max(200, min(15000, hold_ms)))
    # - threshold: 0.0–1.0
    threshold = float(max(0.0, min(1.0, threshold)))
    # - min_voice: 0–3000 ms
    min_voice = int(max(0, min(3000, min_voice)))

    # Construimos el bloque turn_detection compatible
    turn_detection = {
        "type": "server_vad",
        "silence_duration_ms": hold_ms
    }
    # Campos avanzados solo si el flag está activo (y por tanto lo soporta tu backend)
    if advanced:
        # No todos los backends aceptan estos campos. Si el tuyo no, simplemente ignóralos.
        turn_detection["threshold"] = threshold
        turn_detection["min_voice_ms"] = min_voice

    applied = {
        "advanced_enabled": advanced,
        "hold_ms": hold_ms,
        "threshold": threshold if advanced else None,
        "min_voice_ms": min_voice if advanced else None
    }
    return {"turn_detection": turn_detection, "applied": applied}


# ─────────────────────────────────────────────────────────────

@bp.get("/health")
def health():
    """Exponer defaults actuales (antes de overrides) para diagnóstico rápido."""
    return jsonify({
        "ok": True,
        "service": "realtime",
        "model": REALTIME_MODEL,
        "voice": REALTIME_VOICE,
        "defaults": {
            "hold_ms": VAD_SILENCE_MS_DEFAULT,
            "advanced_enabled": ADVANCED_VAD_ENABLED_DEF,
            "threshold": VAD_THRESHOLD_DEFAULT if ADVANCED_VAD_ENABLED_DEF else None,
            "min_voice_ms": VAD_MIN_VOICE_MS_DEFAULT if ADVANCED_VAD_ENABLED_DEF else None,
        }
    })


@bp.post("/session")
def create_session():
    """
    Crea una sesión efímera con OpenAI Realtime usando los diales VAD server-side.
    Prioridad de configuración: ENV < query params < JSON body.
    """
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY no configurada"}), 500

    # Hora (si la usas en logs del sistema)
    _ = hora_houston()

    # Cargar tarjeta del bot
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    try:
        bot_cfg = load_bot(bot_id)
    except Exception:
        bot_cfg = load_bot("sundin")

    instructions = bot_cfg.get("instructions", {}).get("system_prompt", "")

    model_from_json = bot_cfg.get("realtime", {}).get("model")
    voice_from_json = bot_cfg.get("realtime", {}).get("voice")
    modalities_from_json = bot_cfg.get("realtime", {}).get("modalities", ["audio", "text"])

    model_to_use = model_from_json or REALTIME_MODEL
    voice_to_use = voice_from_json or REALTIME_VOICE
    modalities_to_use = modalities_from_json or ["audio", "text"]

    # Diales efectivos del VAD (ENV/Query/JSON)
    req_json = request.get_json(silent=True) or {}
    vad_cfg = _effective_vad_from_request(req_json)
    turn_detection = vad_cfg["turn_detection"]
    vad_applied = vad_cfg["applied"]

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
            return jsonify({"ok": False, "error": "OpenAI Realtime error", "detail": r.text, "payload": payload}), 502

        data = r.json()
        # devolvemos también lo que realmente aplicamos en el server
        return jsonify({"ok": True, "session": data, "vad_applied": vad_applied})
    except Exception as e:
        return jsonify({"ok": False, "error": "Excepción creando sesión", "detail": str(e)}), 500
