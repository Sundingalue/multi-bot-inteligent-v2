# instagram_webhook.py
# Webhook de Instagram (Flask Blueprint) - aislado del resto del core
# ENVÍA respuestas por:  POST /{PAGE_ID}/messages  (NO por IG_USER_ID)
# Requiere en Render → Environment:
#   META_VERIFY_TOKEN        = <tu verify token>
#   META_PAGE_ACCESS_TOKEN   = <PAGE TOKEN de /me/accounts con scopes IG messaging>
#   META_PAGE_ID             = 131837286675681        (tu Page ID)
#   META_IG_USER_ID          = 17841460637585682      (connected_instagram_account.id) [opcional]
#   META_IG_BOT_NAME         = Sara                   [opcional]

import os
import re
import json
import logging
from datetime import datetime
import requests
from flask import Blueprint, request, jsonify, current_app

# ===== Logging básico =====
logging.basicConfig(level=logging.INFO)

# ===== Blueprint =====
ig_bp = Blueprint("instagram_webhook", __name__)

# ===== Variables de entorno (Render -> Environment) =====
META_VERIFY_TOKEN       = (os.getenv("META_VERIFY_TOKEN") or "").strip()
META_PAGE_ACCESS_TOKEN  = (os.getenv("META_PAGE_ACCESS_TOKEN") or "").strip()   # Page Token (EAADZB...)
META_PAGE_ID            = (os.getenv("META_PAGE_ID") or "").strip()            # 131837286675681
IG_USER_ID              = (os.getenv("META_IG_USER_ID") or "").strip()         # 17841460637585682 (opcional)
IG_BOT_NAME             = (os.getenv("META_IG_BOT_NAME") or "").strip()        # p.ej. "Sara" (opcional)

# ===== Helpers (reutiliza tu bot JSON ya cargado en main.py) =====
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
    1) Si META_IG_BOT_NAME está definida, lo busca por name exacto (case-insensitive).
    2) Si no, usa heurística por business_name/name.
    3) Fallback: el primero del dict.
    """
    bots_config = current_app.config.get("BOTS_CONFIG") or {}
    if not bots_config:
        return None

    if IG_BOT_NAME:
        for _key, cfg in bots_config.items():
            if (cfg.get("name") or "").strip().lower() == IG_BOT_NAME.strip().lower():
                return cfg

    for _key, cfg in bots_config.items():
        name = (cfg.get("name") or "").lower()
        biz  = (cfg.get("business_name") or "").lower()
        if "houston" in biz or name in ("sara", "inh", "in houston texas"):
            return cfg

    return list(bots_config.values())[0]

def _append_historial(bot_nombre: str, user_id: str, tipo: str, texto: str):
    """Usa la función de Firebase expuesta por main.py mediante current_app."""
    try:
        fb_append = current_app.config.get("FB_APPEND_HISTORIAL")
        if callable(fb_append):
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append(bot_nombre, f"ig:{user_id}", {"tipo": tipo, "texto": texto, "hora": ahora})
    except Exception as e:
        logging.warning("[IG] No se pudo guardar historial: %s", e)

def _gpt_reply(messages, model_name: str, temperature: float) -> str:
    """
    Llama al cliente OpenAI ya creado en main.py (current_app.config['OPENAI_CLIENT']).
    """
    try:
        client = current_app.config.get("OPENAI_CLIENT")
        if client is None:
            logging.warning("[IG] OPENAI_CLIENT no disponible en current_app.config")
            return "Hola, ¿en qué puedo ayudarte?"

        completion = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=messages
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error("[IG] Error OpenAI: %s", e)
        return "Estoy teniendo un problema técnico. Intentémoslo de nuevo."

def _send_ig_text(psid: str, text: str) -> bool:
    """
    Envía un mensaje de texto al usuario IG (psid) mediante Graph API.
    IMPORTANTE: Usar endpoint de PÁGINA:  POST /{PAGE_ID}/messages
    """
    if not META_PAGE_ACCESS_TOKEN:
        logging.error("[IG] META_PAGE_ACCESS_TOKEN vacío. Configúralo en Render.")
        return False
    if not META_PAGE_ID:
        logging.error("[IG] META_PAGE_ID vacío. Configúralo en Render.")
        return False

    url = f"https://graph.facebook.com/v21.0/{META_PAGE_ID}/messages"
    payload = {
        "recipient": {"id": psid},
        "message": {"text": (text or "Gracias por escribirnos.")[:1000]}
    }
    params = {"access_token": META_PAGE_ACCESS_TOKEN}

    try:
        r = requests.post(url, params=params, json=payload, timeout=20)
        try:
            j = r.json()
        except Exception:
            j = {"_non_json": r.text}
        logging.info("[IG] SEND status=%s resp=%s", r.status_code, j)
        return r.status_code < 400
    except Exception as e:
        logging.error("[IG] Excepción enviando mensaje: %s", e)
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
        return challenge, 200
    return "Token inválido", 403

# =========================
# 2) Recepción POST de eventos
# =========================
@ig_bp.route("/webhook_instagram", methods=["POST"])
def ig_events():
    """
    Soporta ambos formatos:
    A) entry[].changes[].value.messaging[]   (frecuente en IG vía Webhooks)
    B) entry[].messaging[]                   (legacy/otras integraciones)
    """
    if not META_PAGE_ACCESS_TOKEN or not META_PAGE_ID:
        logging.error("[IG] Faltan variables de entorno: META_PAGE_ACCESS_TOKEN o META_PAGE_ID.")
        return jsonify({"status": "env-missing"}), 200

    body = request.get_json(silent=True) or {}
    logging.info("WEBHOOK IG RAW: %s", json.dumps(body, ensure_ascii=False))

    # IG a veces llega con object:"instagram", a veces "page" con value.source IG
    if body.get("object") not in ("instagram", "page"):
        logging.info("[IG] object no es instagram/page -> %s", body.get("object"))
        return jsonify({"status": "ignored"}), 200

    bot_cfg = _get_ig_bot_cfg()
    if not bot_cfg:
        logging.error("[IG] No hay bot JSON cargado. Revisa bots/*.json y app.config['BOTS_CONFIG'].")
        return jsonify({"status": "no-bot"}), 200

    system_prompt = (bot_cfg.get("system_prompt") or "").strip()
    model_name    = (bot_cfg.get("model") or "gpt-4o").strip()
    temperature   = float(bot_cfg.get("temperature", 0.6)) if isinstance(bot_cfg.get("temperature", None), (int, float)) else 0.6

    sender_ids_detectados = []

    for entry in body.get("entry", []):
        # ----- A) Formato con 'changes' -----
        for change in (entry.get("changes") or []):
            value = change.get("value", {}) or {}
            for ev in value.get("messaging", []) or []:
                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                if not psid:
                    continue
                sender_ids_detectados.append(psid)

                msg = (ev.get("message") or {}) or {}
                if msg.get("is_echo"):
                    # Eco (nuestro propio mensaje). No responder.
                    continue
                text = (msg.get("text") or "").strip()

                if not text:
                    fallback = "Recibí tu mensaje. ¿Podrías escribirlo en texto para ayudarte mejor?"
                    _send_ig_text(psid, fallback)
                    _append_historial(bot_cfg.get("name", "INH"), psid, "bot", fallback)
                    continue

                _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)

                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": text})

                reply = _gpt_reply(messages, model_name=model_name, temperature=temperature)
                reply = _apply_style(bot_cfg, reply) or "Gracias por escribirnos."
                _send_ig_text(psid, reply)
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

        # ----- B) Formato legacy con 'messaging' directo en entry -----
        for ev in (entry.get("messaging") or []):
            psid = ((ev.get("sender") or {}).get("id") or "").strip()
            if not psid:
                continue
            sender_ids_detectados.append(psid)

            msg = (ev.get("message") or {}) or {}
            if msg.get("is_echo"):
                continue
            text = (msg.get("text") or "").strip()

            if not text:
                fallback = "Recibí tu mensaje. ¿Podrías escribirlo en texto para ayudarte mejor?"
                _send_ig_text(psid, fallback)
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", fallback)
                continue

            _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": text})

            reply = _gpt_reply(messages, model_name=model_name, temperature=temperature)
            reply = _apply_style(bot_cfg, reply) or "Gracias por escribirnos."
            _send_ig_text(psid, reply)
            _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

    logging.info("WEBHOOK IG SENDER_IDS: %s", sender_ids_detectados)
    return jsonify({"status": "ok", "senders": sender_ids_detectados}), 200
