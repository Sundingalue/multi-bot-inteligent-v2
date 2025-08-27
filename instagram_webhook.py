# instagram_webhook.py
# Webhook de Instagram (Flask Blueprint) - responde por PAGE_ID/messages
# Env vars en Render:
#   META_VERIFY_TOKEN
#   META_PAGE_ACCESS_TOKEN   (Page Token real)
#   META_PAGE_ID             (131837286675681)
#   META_IG_USER_ID          (17841460637585682) [opcional]
#   META_IG_BOT_NAME         (Sara)              [opcional]

import os
import re
import json
import logging
from datetime import datetime
from collections import deque
import requests
from flask import Blueprint, request, jsonify, current_app

# ===== Logging =====
logging.basicConfig(level=logging.INFO)

# ===== Anti-duplicados por message.mid (LRU) =====
_SEEN_MIDS = deque(maxlen=500)
_SEEN_SET  = set()
def _seen_mid(mid: str) -> bool:
    if not mid:
        return False
    if mid in _SEEN_SET:
        return True
    _SEEN_SET.add(mid)
    _SEEN_MIDS.append(mid)
    if len(_SEEN_MIDS) == _SEEN_MIDS.maxlen:
        # Mantener set en tamaño razonable
        while len(_SEEN_SET) > _SEEN_MIDS.maxlen:
            viejo = _SEEN_MIDS.popleft()
            _SEEN_SET.discard(viejo)
    return False

# ===== Blueprint =====
ig_bp = Blueprint("instagram_webhook", __name__)

# ===== Env =====
META_VERIFY_TOKEN       = (os.getenv("META_VERIFY_TOKEN") or "").strip()
META_PAGE_ACCESS_TOKEN  = (os.getenv("META_PAGE_ACCESS_TOKEN") or "").strip()
META_PAGE_ID            = (os.getenv("META_PAGE_ID") or "").strip()
IG_USER_ID              = (os.getenv("META_IG_USER_ID") or "").strip()   # opcional
IG_BOT_NAME             = (os.getenv("META_IG_BOT_NAME") or "").strip()

# ===== Helpers =====
def _apply_style(bot_cfg: dict, text: str) -> str:
    """Ajusta a 1–2 frases máximo, sin redundancias."""
    if not text:
        return text
    # Normaliza espacios
    text = re.sub(r'\s+', ' ', text).strip()
    # Corta a 2 frases como máximo
    oraciones = re.split(r'(?<=[.!?])\s+', text)
    text = " ".join(oraciones[:2]).strip()
    # Si aún está largo, recorta a ~220 chars sin cortar palabra
    if len(text) > 220:
        text = text[:220]
        text = re.sub(r'\s+\S*$', '', text).rstrip(' ,;:')
        text += '…'
    return text

def _get_ig_bot_cfg():
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
    try:
        fb_append = current_app.config.get("FB_APPEND_HISTORIAL")
        if callable(fb_append):
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append(bot_nombre, f"ig:{user_id}", {"tipo": tipo, "texto": texto, "hora": ahora})
    except Exception as e:
        logging.warning("[IG] No se pudo guardar historial: %s", e)

def _gpt_reply(messages, model_name: str, temperature: float) -> str:
    try:
        client = current_app.config.get("OPENAI_CLIENT")
        if client is None:
            logging.warning("[IG] OPENAI_CLIENT no disponible")
            return "¿En qué puedo ayudarte en concreto?"
        completion = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=messages
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error("[IG] Error OpenAI: %s", e)
        return "Tuve un problema técnico. ¿Me repites en breve?"

def _send_ig_text(psid: str, text: str) -> bool:
    if not META_PAGE_ACCESS_TOKEN:
        logging.error("[IG] Falta META_PAGE_ACCESS_TOKEN")
        return False
    if not META_PAGE_ID:
        logging.error("[IG] Falta META_PAGE_ID")
        return False
    url = f"https://graph.facebook.com/v21.0/{META_PAGE_ID}/messages"
    payload = {"recipient": {"id": psid}, "message": {"text": (text or "Gracias por escribirnos.")[:1000]}}
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

# ===== Verificación =====
@ig_bp.route("/webhook_instagram", methods=["GET"])
def ig_verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return challenge, 200
    return "Token inválido", 403

# ===== Recepción de eventos =====
@ig_bp.route("/webhook_instagram", methods=["POST"])
def ig_events():
    """
    Procesa UN solo esquema por entrada para evitar duplicados:
    - Si hay entry[].changes[].value.messaging[] -> usa ese y NO procesa entry[].messaging[].
    - Si no hay changes -> intenta entry[].messaging[] (legacy).
    Además, descarta mids ya vistos (idempotencia).
    """
    if not META_PAGE_ACCESS_TOKEN or not META_PAGE_ID:
        logging.error("[IG] Faltan variables de entorno: META_PAGE_ACCESS_TOKEN o META_PAGE_ID.")
        return jsonify({"status": "env-missing"}), 200

    body = request.get_json(silent=True) or {}
    logging.info("WEBHOOK IG RAW: %s", json.dumps(body, ensure_ascii=False))

    if body.get("object") not in ("instagram", "page"):
        return jsonify({"status": "ignored"}), 200

    bot_cfg = _get_ig_bot_cfg()
    if not bot_cfg:
        logging.error("[IG] No hay bot JSON cargado")
        return jsonify({"status": "no-bot"}), 200

    system_prompt = (bot_cfg.get("system_prompt") or "").strip()
    model_name    = (bot_cfg.get("model") or "gpt-4o").strip()
    try:
        temperature = float(bot_cfg.get("temperature", 0.6)) if isinstance(bot_cfg.get("temperature", None), (int, float)) else 0.6
    except Exception:
        temperature = 0.6

    sender_ids_detectados = []

    for entry in body.get("entry", []):
        processed_changes = False

        # ---- Preferimos el esquema con 'changes' (si existe) ----
        for change in (entry.get("changes") or []):
            processed_changes = True
            value = change.get("value", {}) or {}
            for ev in value.get("messaging", []) or []:
                msg = (ev.get("message") or {}) or {}
                mid = (msg.get("mid") or "").strip()
                if _seen_mid(mid):
                    logging.info("[IG] MID duplicado, ignorado: %s", mid)
                    continue

                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                if not psid:
                    continue
                sender_ids_detectados.append(psid)

                if msg.get("is_echo"):
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    _send_ig_text(psid, "¿Podrías escribirlo en texto para ayudarte mejor?")
                    _append_historial(bot_cfg.get("name", "INH"), psid, "bot", "¿Podrías escribirlo en texto para ayudarte mejor?")
                    continue

                _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)

                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": text})

                reply = _gpt_reply(messages, model_name=model_name, temperature=temperature)
                reply = _apply_style(bot_cfg, reply) or "¿En qué puedo ayudarte puntualmente?"
                _send_ig_text(psid, reply)
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

        # ---- Si NO hubo 'changes', intentamos legacy entry[].messaging[] ----
        if not processed_changes:
            for ev in (entry.get("messaging") or []):
                msg = (ev.get("message") or {}) or {}
                mid = (msg.get("mid") or "").strip()
                if _seen_mid(mid):
                    logging.info("[IG] MID duplicado, ignorado: %s", mid)
                    continue

                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                if not psid:
                    continue
                sender_ids_detectados.append(psid)

                if msg.get("is_echo"):
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    _send_ig_text(psid, "¿Podrías escribirlo en texto para ayudarte mejor?")
                    _append_historial(bot_cfg.get("name", "INH"), psid, "bot", "¿Podrías escribirlo en texto para ayudarte mejor?")
                    continue

                _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)

                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": text})

                reply = _gpt_reply(messages, model_name=model_name, temperature=temperature)
                reply = _apply_style(bot_cfg, reply) or "¿En qué puedo ayudarte puntualmente?"
                _send_ig_text(psid, reply)
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

    logging.info("WEBHOOK IG SENDER_IDS: %s", sender_ids_detectados)
    return jsonify({"status": "ok", "senders": sender_ids_detectados}), 200
