# instagram_webhook.py
# IG Webhook simple: usa SOLO lo definido en el JSON (system_prompt, greeting, model, temperature)

import os
import re
import json
import logging
from datetime import datetime
from collections import deque
import requests
from flask import Blueprint, request, jsonify, current_app

logging.basicConfig(level=logging.INFO)
ig_bp = Blueprint("instagram_webhook", __name__)

# ===== Env =====
META_VERIFY_TOKEN       = (os.getenv("META_VERIFY_TOKEN") or "").strip()
META_PAGE_ACCESS_TOKEN  = (os.getenv("META_PAGE_ACCESS_TOKEN") or "").strip()
META_PAGE_ID            = (os.getenv("META_PAGE_ID") or "").strip()

# ===== Anti-duplicados por mid =====
_SEEN_MIDS = deque(maxlen=500)
_SEEN_SET  = set()
def _seen_mid(mid: str) -> bool:
    if not mid:
        return False
    if mid in _SEEN_SET:
        return True
    _SEEN_SET.add(mid)
    _SEEN_MIDS.append(mid)
    if len(_SEEN_SET) > _SEEN_MIDS.maxlen:
        viejo = _SEEN_MIDS.popleft()
        _SEEN_SET.discard(viejo)
    return False

# ===== Anti-repetición de greeting =====
_SEEN_USERS = set()
def _first_time_user(psid: str) -> bool:
    if not psid:
        return False
    if psid in _SEEN_USERS:
        return False
    _SEEN_USERS.add(psid)
    return True

# ===== Helpers =====
def _style_clip(text: str, bot_cfg: dict) -> str:
    """Aplica estilo básico (máx 2 frases y 220 chars)"""
    if not text:
        return text
    style = (bot_cfg.get("style") or {})
    max_sent = int(style.get("max_sentences", 2)) if style.get("max_sentences") is not None else 2
    max_chars = int(style.get("max_chars", 220)) if style.get("max_chars") is not None else 220
    txt = re.sub(r"\s+", " ", text).strip()
    sents = re.split(r'(?<=[.!?])\s+', txt)
    txt = " ".join(sents[:max_sent]).strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars]
        txt = re.sub(r"\s+\S*$", "", txt).rstrip(" ,;:") + "…"
    return txt

def _get_bot_cfg(page_id: str):
    """Busca en BOTS_CONFIG el bot por page_id (Instagram)"""
    bots = current_app.config.get("BOTS_CONFIG") or {}
    for _, cfg in bots.items():
        ch = (cfg.get("channels") or {}).get("instagram") or {}
        if (ch.get("page_id") or "").strip() == (page_id or "").strip():
            return cfg
    # fallback: primero
    return list(bots.values())[0] if bots else {}

def _append_historial(bot_nombre: str, user_id: str, tipo: str, texto: str):
    try:
        fb_append = current_app.config.get("FB_APPEND_HISTORIAL")
        if callable(fb_append):
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append(bot_nombre, f"ig:{user_id}", {"tipo": tipo, "texto": texto, "hora": ahora})
    except Exception as e:
        logging.warning("[IG] No se pudo guardar historial: %s", e)

def _send_ig_text(psid: str, text: str) -> bool:
    if not META_PAGE_ACCESS_TOKEN or not META_PAGE_ID:
        logging.error("[IG] Faltan META_PAGE_ACCESS_TOKEN o META_PAGE_ID")
        return False
    url = f"https://graph.facebook.com/v21.0/{META_PAGE_ID}/messages"
    payload = {
        "recipient": {"id": psid},
        "message": {"text": (text or "Gracias por escribirnos.")[:1000]}
    }
    try:
        r = requests.post(url, params={"access_token": META_PAGE_ACCESS_TOKEN}, json=payload, timeout=20)
        try:
            j = r.json()
        except Exception:
            j = {"_non_json": r.text}
        logging.info("[IG] SEND status=%s resp=%s", r.status_code, j)
        return r.status_code < 400
    except Exception as e:
        logging.error("[IG] Excepción enviando mensaje: %s", e)
        return False

def _gpt_reply(system_prompt: str, user_text: str, model: str, temperature: float) -> str:
    try:
        client = current_app.config.get("OPENAI_CLIENT")
        if not client:
            return "¿En qué puedo ayudarte?"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})
        c = client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=messages
        )
        return (c.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error("[IG] Error OpenAI: %s", e)
        return "Tuve un problema técnico. Intentemos de nuevo."

# ===== Verify =====
@ig_bp.route("/webhook_instagram", methods=["GET"])
def ig_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return challenge, 200
    return "Token inválido", 403

# ===== Events =====
@ig_bp.route("/webhook_instagram", methods=["POST"])
def ig_events():
    if not META_PAGE_ACCESS_TOKEN or not META_PAGE_ID:
        logging.error("[IG] Faltan variables: META_PAGE_ACCESS_TOKEN o META_PAGE_ID.")
        return jsonify({"status": "env-missing"}), 200

    body = request.get_json(silent=True) or {}
    logging.info("WEBHOOK IG RAW: %s", json.dumps(body, ensure_ascii=False))
    if body.get("object") not in ("instagram", "page"):
        return jsonify({"status": "ignored"}), 200

    senders = []

    for entry in body.get("entry", []):
        page_id = entry.get("id") or META_PAGE_ID
        bot_cfg = _get_bot_cfg(page_id)
        system_prompt = (bot_cfg.get("system_prompt") or "").strip()
        model = (bot_cfg.get("model") or "gpt-4o").strip()
        temp = float(bot_cfg.get("temperature", 0.6)) if isinstance(bot_cfg.get("temperature", None), (int, float)) else 0.6
        greeting = (bot_cfg.get("greeting") or "").strip()

        # --- Procesar SOLO una rama por evento ---
        processed_changes = False

        # changes.value.messaging (nuevo esquema)
        for change in (entry.get("changes") or []):
            processed_changes = True
            for ev in (change.get("value", {}).get("messaging") or []):
                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                msg = (ev.get("message") or {})
                mid = (msg.get("mid") or "").strip()
                if not psid or _seen_mid(mid) or msg.get("is_echo"):
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                senders.append(psid)

                # Saludo SOLO la primera vez y NO invocar GPT en ese mensaje
                if _first_time_user(psid) and greeting:
                    _send_ig_text(psid, _style_clip(greeting, bot_cfg))
                    _append_historial(bot_cfg.get("name", "INH"), psid, "bot", greeting)
                    continue

                _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)
                reply = _gpt_reply(system_prompt, text, model, temp)
                _send_ig_text(psid, _style_clip(reply, bot_cfg))
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

        # entry.messaging (legacy) — sólo si no hubo changes
        if not processed_changes:
            for ev in (entry.get("messaging") or []):
                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                msg = (ev.get("message") or {})
                mid = (msg.get("mid") or "").strip()
                if not psid or _seen_mid(mid) or msg.get("is_echo"):
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                senders.append(psid)

                # Saludo SOLO la primera vez y NO invocar GPT en ese mensaje
                if _first_time_user(psid) and greeting:
                    _send_ig_text(psid, _style_clip(greeting, bot_cfg))
                    _append_historial(bot_cfg.get("name", "INH"), psid, "bot", greeting)
                    continue

                _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)
                reply = _gpt_reply(system_prompt, text, model, temp)
                _send_ig_text(psid, _style_clip(reply, bot_cfg))
                _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

    logging.info("WEBHOOK IG SENDER_IDS: %s", senders)
    return jsonify({"status": "ok", "senders": senders}), 200
