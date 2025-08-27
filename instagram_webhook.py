# routes/instagram_webhook.py
# Webhook de Instagram (Flask Blueprint) - aislado del resto del core

import os
import re
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app

# ===== Blueprint =====
ig_bp = Blueprint("instagram_webhook", __name__)

# ===== Variables de entorno necesarias (Render -> Environment) =====
# Deben estar definidas en Render → Environment (sin comillas)
META_VERIFY_TOKEN     = (os.getenv("META_VERIFY_TOKEN") or "").strip()
META_PAGE_ACCESS_TOKEN = (os.getenv("META_PAGE_ACCESS_TOKEN") or "").strip()
IG_USER_ID            = (os.getenv("META_IG_USER_ID") or "").strip()       # p.ej. 17841460637585682
IG_BOT_NAME           = (os.getenv("META_IG_BOT_NAME") or "").strip()      # opcional: p.ej. "Sara"

# ===== Helpers mínimos (reutiliza tu bot JSON ya cargado en main.py) =====
def _apply_style(bot_cfg: dict, text: str) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    short = bool(style.get("short_replies", True))
    max_sents = int(style.get("max_sentences", 2)) if style.get("max_sentences") is not None else 2
    if not text:
        return text
    parts = re.split(r'(?<=[\.\!\?])\s+', (text or "").strip())
    if short and parts:
        text = " ".join(parts[:max_sents]).strip()
    return text

def _get_ig_bot_cfg():
    """
    Devuelve el bot a usar para Instagram.
    1) Si se definió META_IG_BOT_NAME, lo busca por name exacto.
    2) Si no, intenta heurística por business_name/name.
    3) Si falla, usa el primero.
    """
    bots_config = current_app.config.get("BOTS_CONFIG") or {}
    if not bots_config:
        return None

    # 1) Forzar por nombre si viene por env (exacto, case-insensitive)
    if IG_BOT_NAME:
        for _key, cfg in bots_config.items():
            if (cfg.get("name") or "").strip().lower() == IG_BOT_NAME.strip().lower():
                return cfg

    # 2) Heurística previa (compatibilidad)
    for _key, cfg in bots_config.items():
        name = (cfg.get("name") or "").lower()
        biz  = (cfg.get("business_name") or "").lower()
        if "houston" in biz or name in ("sara", "inh", "in houston texas"):
            return cfg

    # 3) fallback: primero
    return list(bots_config.values())[0]

def _append_historial(bot_nombre: str, user_id: str, tipo: str, texto: str):
    """Usa la función de Firebase expuesta por main.py mediante current_app."""
    try:
        fb_append = current_app.config.get("FB_APPEND_HISTORIAL")
        if callable(fb_append):
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append(bot_nombre, f"ig:{user_id}", {"tipo": tipo, "texto": texto, "hora": ahora})
    except Exception as e:
        print(f"[IG] No se pudo guardar historial: {e}")

def _gpt_reply(messages, model_name: str, temperature: float) -> str:
    """
    Llama al cliente OpenAI ya creado en main.py (current_app.config['OPENAI_CLIENT']).
    """
    try:
        client = current_app.config.get("OPENAI_CLIENT")
        if client is None:
            print("[IG] OPENAI_CLIENT no disponible en current_app.config")
            return "Hola, ¿en qué puedo ayudarte?"

        completion = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=messages
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[IG] Error OpenAI: {e}")
        return "Estoy teniendo un problema técnico. Intentémoslo de nuevo."

def _send_ig_text(psid: str, text: str) -> bool:
    """
    Envía un mensaje de texto al usuario IG (psid) mediante Graph API.
    Para Instagram se usa: POST /{IG_USER_ID}/messages
    """
    if not META_PAGE_ACCESS_TOKEN:
        print("[IG] META_PAGE_ACCESS_TOKEN vacío. Configúralo en Render.")
        return False
    if not IG_USER_ID:
        print("[IG] META_IG_USER_ID vacío. Configúralo en Render.")
        return False

    url = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/messages"
    payload = {
        "recipient": {"id": psid},
        "message": {"text": text}
    }
    params = {"access_token": META_PAGE_ACCESS_TOKEN}

    try:
        r = requests.post(url, params=params, json=payload, timeout=20)
        if r.status_code >= 400:
            print(f"[IG] Error enviando mensaje: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        print(f"[IG] Excepción enviando mensaje: {e}")
        return False

# =========================
# 1) Verificación GET (hub.challenge)
# =========================
@ig_bp.route("/webhook_instagram", methods=["GET"])
def ig_verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        # Debe devolver el challenge en texto plano (200)
        return challenge, 200
    return "Token inválido", 403

# =========================
# 2) Recepción POST de eventos
# =========================
@ig_bp.route("/webhook_instagram", methods=["POST"])
def ig_events():
    """
    Estructura típica:
    {
      "object":"instagram",
      "entry":[
        {"id":"<page_id>","time":...,"messaging":[
            {"sender":{"id":"<ig_psid>"},"recipient":{"id":"<page_id>"},
             "timestamp":..., "message":{"mid":"...","text":"Hola"}}
        ]}]
    }
    """
    # 0) Validaciones básicas de entorno
    if not META_PAGE_ACCESS_TOKEN or not IG_USER_ID:
        print("[IG] Faltan variables de entorno: META_PAGE_ACCESS_TOKEN o META_IG_USER_ID.")
        return jsonify({"status": "env-missing"}), 200

    body = request.get_json(silent=True) or {}
    if body.get("object") != "instagram":
        return jsonify({"status": "ignored"}), 200

    bot_cfg = _get_ig_bot_cfg()
    if not bot_cfg:
        print("[IG] No hay bot JSON cargado. Revisa bots/*.json y app.config['BOTS_CONFIG'].")
        return jsonify({"status": "no-bot"}), 200

    # Prepara contexto para GPT (similar a tu WhatsApp)
    system_prompt = (bot_cfg.get("system_prompt") or "").strip()
    model_name    = (bot_cfg.get("model") or "gpt-4o").strip()
    temperature   = float(bot_cfg.get("temperature", 0.6)) if isinstance(bot_cfg.get("temperature", None), (int, float)) else 0.6

    for entry in body.get("entry", []):
        # page_id = (entry or {}).get("id")  # No lo necesitamos para IG; enviamos por /{IG_USER_ID}/messages
        for msg in (entry.get("messaging") or []):
            psid    = ((msg.get("sender") or {}).get("id") or "").strip()
            message = (msg.get("message") or {}) or {}

            # Evitar responder a nuestros propios envíos
            if message.get("is_echo"):
                continue

            text = (message.get("text") or "").strip()
            if not psid:
                continue

            # Si no hay texto (p.ej., adjuntos), responde con un fallback corto
            if not text:
                fallback = "Recibí tu mensaje. ¿Podrías escribirlo en texto para ayudarte mejor?"
                _send_ig_text(psid, fallback)
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", fallback)
                continue

            # Guarda llegada
            _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)

            # Construye prompt
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": text})

            # Llama a GPT
            reply = _gpt_reply(messages, model_name=model_name, temperature=temperature)
            reply = _apply_style(bot_cfg, reply) or "Gracias por escribirnos."

            # Envía respuesta
            sent = _send_ig_text(psid, reply)

            # Guarda salida si se envió
            if sent:
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

    return jsonify({"status": "ok"}), 200
