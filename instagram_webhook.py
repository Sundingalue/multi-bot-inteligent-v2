# routes/instagram_webhook.py
# Webhook de Instagram (Flask Blueprint) - aislado del resto del core

import os
import json
import re
import time
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app

# ===== Blueprint =====
ig_bp = Blueprint("instagram_webhook", __name__)

# ===== Variables de entorno necesarias (Render -> Environment) =====
META_VERIFY_TOKEN = (os.getenv("inh-instagram-2025") or "").strip()
META_PAGE_ACCESS_TOKEN = (os.getenv("EAADZBlUKm9iIBPWZAgPUNhZA38ZAzfnEM1zg0h3tbipZCDg8auRHxiUTy04OZCNYMlCueoaFLKCH50VH6tu8kvDuGPZB0PeJxzvCt7XfIUBlDurDB4NkUd7U8G2jxvshHnsrbWcL8q3y4BzExbx8BL9dH8sANoVFjPikIwexOZAZCxy5pjkqkraCiUCUCSZCM7sFGU") or "").strip()

# ===== Helpers mínimos (reusarás tu bot JSON ya cargado en main.py) =====
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

def _get_inh_bot_cfg():
    """
    Busca el bot de In Houston Texas.
    Estrategia:
      1) Si en bots/*.json existe un bot cuyo business_name contenga 'Houston' o name 'Sara', úsalo.
      2) Si hay un único bot, úsalo.
      3) fallback: primero.
    """
    bots_config = current_app.config.get("BOTS_CONFIG") or {}
    if not bots_config:
        return None

    # 1) Buscar por business_name o name
    for _key, cfg in bots_config.items():
        name = (cfg.get("name") or "").lower()
        biz  = (cfg.get("business_name") or "").lower()
        if "houston" in biz or name in ("sara", "inh", "in houston texas"):
            return cfg

    # 2) Si solo hay uno
    if len(bots_config) == 1:
        return list(bots_config.values())[0]

    # 3) fallback: primero
    return list(bots_config.values())[0]

def _append_historial(bot_nombre: str, user_id: str, tipo: str, texto: str):
    """Usa las funciones de Firebase ya cargadas en main.py mediante current_app."""
    try:
        fb_append = current_app.config.get("FB_APPEND_HISTORIAL")
        if callable(fb_append):
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append(bot_nombre, f"ig:{user_id}", {"tipo": tipo, "texto": texto, "hora": ahora})
    except Exception as e:
        print(f"[IG] No se pudo guardar historial: {e}")

def _gpt_reply(messages, model_name: str, temperature: float):
    """
    Llama al cliente OpenAI ya creado en main.py.
    Espera que current_app.config['OPENAI_CLIENT'] exista.
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

def _send_ig_text(psid: str, text: str):
    """
    Envía un mensaje de texto al usuario IG (psid) mediante Graph API.
    """
    if not META_PAGE_ACCESS_TOKEN:
        print("[IG] META_PAGE_ACCESS_TOKEN vacío. Configúralo en Render.")
        return False

    url = "https://graph.facebook.com/v21.0/me/messages"
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
    body = request.get_json(silent=True) or {}
    if body.get("object") != "instagram":
        return jsonify({"status": "ignored"}), 200

    bot_cfg = _get_inh_bot_cfg()
    if not bot_cfg:
        print("[IG] No hay bot JSON cargado. Revisa bots/*.json")
        return jsonify({"status": "no-bot"}), 200

    # Prepara contexto para GPT (similar a tu WhatsApp)
    system_prompt = (bot_cfg.get("system_prompt") or "").strip()
    model_name = (bot_cfg.get("model") or "gpt-4o").strip()
    temperature = float(bot_cfg.get("temperature", 0.6)) if isinstance(bot_cfg.get("temperature", None), (int, float)) else 0.6

    for entry in body.get("entry", []):
        for msg in entry.get("messaging", []):
            psid = (msg.get("sender", {}) or {}).get("id")
            message = msg.get("message", {}) or {}
            text = (message.get("text") or "").strip()

            if not psid:
                continue

            if text:
                # Guarda llegada
                _append_historial(bot_cfg.get("name", "INH"), psid, "user", text)

                # Construye prompt
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": text})

                reply = _gpt_reply(messages, model_name=model_name, temperature=temperature)
                reply = _apply_style(bot_cfg, reply) or "Gracias por escribirnos."

                # Envía respuesta
                sent = _send_ig_text(psid, reply)

                # Guarda salida
                if sent:
                    _append_historial(bot_cfg.get("name", "INH"), psid, "bot", reply)

    return jsonify({"status": "ok"}), 200
