# routes/avatar_realtime.py
# Endpoint para crear una sesión efímera de voz en tiempo real (OpenAI Realtime)
# Se registra como Blueprint en main.py.

import os
import requests
from flask import Blueprint, jsonify, request, make_response
from utils.timezone_utils import hora_houston
from utils.bot_loader import load_bot

bp = Blueprint("realtime", __name__, url_prefix="/realtime")

# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN CENTRAL DEL AVATAR (DEFAULTS)
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "alloy")  # voz segura por defecto

# VAD server-side (turn_detection)
VAD_SILENCE_MS_DEFAULT    = int(os.getenv("VAD_HOLD_MS", os.getenv("VAD_SILENCE_MS", "1200")))
ADVANCED_VAD_ENABLED_DEF  = os.getenv("ADVANCED_VAD_ENABLED", "0").lower() in ("1", "true", "t", "yes", "on")
VAD_THRESHOLD_DEFAULT     = float(os.getenv("VAD_THRESHOLD", "0.12"))
VAD_MIN_VOICE_MS_DEFAULT  = int(os.getenv("VAD_MIN_VOICE_MS", "500"))

# ─────────────────────────────────────────────────────────────
# Helpers

def _to_bool(x, default=False):
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    return str(x).strip().lower() in ("1", "true", "t", "yes", "y", "on")

def _clamp_01(x):
    try:
        v = float(x)
    except Exception:
        return None
    return max(0.0, min(1.0, v))

def _corsify(resp):
    """Añade CORS básicos si hay Origin (útil cuando el after_request global no aplica)."""
    origin = request.headers.get("Origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"]  = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _effective_vad_from_request(req_json) -> dict:
    # 1) Defaults/ENV
    hold_ms   = VAD_SILENCE_MS_DEFAULT
    advanced  = ADVANCED_VAD_ENABLED_DEF
    threshold = VAD_THRESHOLD_DEFAULT
    min_voice = VAD_MIN_VOICE_MS_DEFAULT

    # 2) Query params
    qp = request.args
    if "hold_ms" in qp:
        try: hold_ms = int(qp.get("hold_ms"))
        except: pass
    if "silence_ms" in qp:
        try: hold_ms = int(qp.get("silence_ms"))
        except: pass
    if "advanced" in qp:
        advanced = _to_bool(qp.get("advanced"), advanced)
    if "threshold" in qp:
        th = _clamp_01(qp.get("threshold"))
        if th is not None: threshold = th
    if "min_voice_ms" in qp:
        try: min_voice = int(qp.get("min_voice_ms"))
        except: pass

    # 3) JSON body
    vad = {}
    if isinstance(req_json, dict):
        vad = (req_json.get("vad") or {}) if "vad" in req_json else {}
    if vad:
        if "hold_ms" in vad:
            try: hold_ms = int(vad["hold_ms"])
            except: pass
        if "silence_ms" in vad:
            try: hold_ms = int(vad["silence_ms"])
            except: pass
        if "advanced" in vad:
            advanced = _to_bool(vad["advanced"], advanced)
        if "threshold" in vad:
            th = _clamp_01(vad["threshold"])
            if th is not None: threshold = th
        if "min_voice_ms" in vad:
            try: min_voice = int(vad["min_voice_ms"])
            except: pass

    # Límites razonables
    hold_ms = int(max(200, min(15000, hold_ms)))
    threshold = float(max(0.0, min(1.0, threshold)))
    min_voice = int(max(0, min(3000, min_voice)))

    turn_detection = {
        "type": "server_vad",
        "silence_duration_ms": hold_ms
    }
    if advanced:
        # Si tu backend no soporta estos campos, OpenAI los ignora sin romper.
        turn_detection["threshold"] = threshold
        turn_detection["min_voice_ms"] = min_voice

    applied = {
        "advanced_enabled": advanced,
        "hold_ms": hold_ms,
        "threshold": threshold if advanced else None,
        "min_voice_ms": min_voice if advanced else None
    }
    return {"turn_detection": turn_detection, "applied": applied}

def _system_instructions_from_bot(bot_cfg: dict) -> str:
    """
    Soporta tus distintos formatos de JSON:
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

# ─────────────────────────────────────────────────────────────

@bp.get("/health")
def health():
    resp = jsonify({
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
    return _corsify(resp)

@bp.route("/session", methods=["POST", "OPTIONS"])
def create_session():
    """
    Crea una sesión efímera con OpenAI Realtime usando diales VAD server-side.
    Prioridad: ENV < query params < JSON body.
    """
    if request.method == "OPTIONS":
        return _corsify(make_response(("", 204)))

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    if not OPENAI_API_KEY:
        return _corsify(jsonify({"ok": False, "error": "OPENAI_API_KEY no configurada"})), 500

    # Hora para logs si la usas
    _ = hora_houston()

    # Bot desde ?bot= / Header o fallback "sundin"
    bot_id = request.args.get("bot") or request.headers.get("X-Bot-Id") or "sundin"
    try:
        bot_cfg = load_bot(bot_id)
    except Exception:
        # fallback duro a "sundin"
        bot_cfg = load_bot("sundin")

    # Construcción robusta de instrucciones/voz/modalidades
    instructions       = _system_instructions_from_bot(bot_cfg)
    model_from_json    = (bot_cfg.get("realtime") or {}).get("model")
    voice_from_json    = (bot_cfg.get("realtime") or {}).get("voice")
    modalities_from_js = (bot_cfg.get("realtime") or {}).get("modalities", ["audio", "text"])

    model_to_use      = model_from_json or REALTIME_MODEL
    voice_to_use      = voice_from_json or REALTIME_VOICE
    modalities_to_use = modalities_from_js or ["audio", "text"]

    # VAD efectivo
    req_json = request.get_json(silent=True) or {}
    vad_cfg = _effective_vad_from_request(req_json)
    turn_detection = vad_cfg["turn_detection"]
    vad_applied    = vad_cfg["applied"]

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
                # 👇 Este header es CLAVE para Realtime
                "OpenAI-Beta": "realtime=v1",
            },
            json=payload,
            timeout=25,
        )
        if r.status_code >= 400:
            # Devolver detalle al front para depurar en la tarjeta
            resp = jsonify({
                "ok": False,
                "error": "OpenAI Realtime error",
                "status": r.status_code,
                "detail": r.text,
                "payload": payload
            })
            return _corsify(resp), 502

        data = r.json()
        resp = jsonify({
            "ok": True,
            "session": data,       # incluye client_secret.value y expires_at
            "vad_applied": vad_applied
        })
        return _corsify(resp)

    except requests.Timeout:
        return _corsify(jsonify({"ok": False, "error": "Timeout creando sesión"})), 504
    except Exception as e:
        return _corsify(jsonify({"ok": False, "error": "Excepción creando sesión", "detail": str(e)})), 500
