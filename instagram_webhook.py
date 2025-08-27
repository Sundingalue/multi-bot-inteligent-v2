# instagram_webhook.py
# IG webhook con reglas similares a WhatsApp:
# - Greeting en primer mensaje
# - Intents rápidos (cita/dirección/app)
# - Precios solo tras 2 insistencias
# - Respuestas concisas
# - GPT como fallback

import os, re, json, logging
from datetime import datetime
from collections import deque, defaultdict
import requests
from flask import Blueprint, request, jsonify, current_app

logging.basicConfig(level=logging.INFO)
ig_bp = Blueprint("instagram_webhook", __name__)

# ===== Env =====
META_VERIFY_TOKEN       = (os.getenv("META_VERIFY_TOKEN") or "").strip()
META_PAGE_ACCESS_TOKEN  = (os.getenv("META_PAGE_ACCESS_TOKEN") or "").strip()
META_PAGE_ID            = (os.getenv("META_PAGE_ID") or "").strip()
IG_BOT_NAME             = (os.getenv("META_IG_BOT_NAME") or "").strip()

# ===== Estado en memoria de proceso =====
_SEEN_MIDS = deque(maxlen=500)
_SEEN_SET  = set()

def _seen_mid(mid: str) -> bool:
    if not mid: return False
    if mid in _SEEN_SET: return True
    _SEEN_SET.add(mid); _SEEN_MIDS.append(mid)
    while len(_SEEN_SET) > _SEEN_MIDS.maxlen:
        viejo = _SEEN_MIDS.popleft(); _SEEN_SET.discard(viejo)
    return False

# Usuarios saludados y contador de “precio”
_SEEN_USERS = deque(maxlen=1000)
_SEEN_USERS_SET = set()
_PRICE_COUNT = defaultdict(int)   # por psid

def _first_time_user(psid: str) -> bool:
    if not psid: return False
    if psid in _SEEN_USERS_SET: return False
    _SEEN_USERS_SET.add(psid); _SEEN_USERS.append(psid)
    while len(_SEEN_USERS_SET) > _SEEN_USERS.maxlen:
        viejo = _SEEN_USERS.popleft(); _SEEN_USERS_SET.discard(viejo)
    return True

# ===== Util =====
def _apply_style(text: str) -> str:
    if not text: return text
    text = re.sub(r'\s+', ' ', text).strip()
    oraciones = re.split(r'(?<=[.!?])\s+', text)
    text = " ".join(oraciones[:2]).strip()
    if len(text) > 220:
        text = text[:220]; text = re.sub(r'\s+\S*$', '', text).rstrip(' ,;:') + '…'
    return text

def _get_bot_cfg():
    bots = current_app.config.get("BOTS_CONFIG") or {}
    if not bots: return {}
    if IG_BOT_NAME:
        for _, cfg in bots.items():
            if (cfg.get("name","").strip().lower() == IG_BOT_NAME.strip().lower()):
                return cfg
    # heurística
    for _, cfg in bots.items():
        name = (cfg.get("name") or "").lower()
        biz  = (cfg.get("business_name") or "").lower()
        if "houston" in biz or name in ("sara","inh","in houston texas"):
            return cfg
    # primero
    return list(bots.values())[0]

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
    payload = {"recipient":{"id":psid},"message":{"text":(text or "Gracias por escribirnos.")[:1000]}}
    try:
        r = requests.post(url, params={"access_token": META_PAGE_ACCESS_TOKEN}, json=payload, timeout=20)
        try: j = r.json()
        except Exception: j = {"_non_json": r.text}
        logging.info("[IG] SEND status=%s resp=%s", r.status_code, j)
        return r.status_code < 400
    except Exception as e:
        logging.error("[IG] Excepción enviando mensaje: %s", e)
        return False

def _gpt_reply(system_prompt: str, user_text: str, model: str, temperature: float) -> str:
    try:
        client = current_app.config.get("OPENAI_CLIENT")
        if not client: return "¿En qué puedo ayudarte en concreto?"
        messages = []
        if system_prompt: messages.append({"role":"system","content":system_prompt})
        messages.append({"role":"user","content":user_text})
        c = client.chat.completions.create(model=model, temperature=temperature, messages=messages)
        return (c.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error("[IG] Error OpenAI: %s", e)
        return "Tuve un problema técnico. ¿Me repites en breve?"

# ===== Reglas de intención (simples y rápidas) =====
def _intent_reply(psid: str, text: str, cfg: dict) -> str | None:
    t = (text or "").lower()

    # 1) Agendar cita
    if any(k in t for k in ["cita","agendar","agenda","reunión","reunion"]):
        url = (cfg.get("links") or {}).get("calendar") or "https://calendar.app.google/2PAh6A4Lkxw3qxLC9"
        return f"¡Excelente! Aquí tienes el enlace para agendar con el Sr. Sundin Galue: {url}"

    # 2) Dirección
    if any(k in t for k in ["dirección","direccion","donde están","ubicación","ubicacion","oficina","oficinas"]):
        url = (cfg.get("links") or {}).get("maps") or "https://maps.app.goo.gl/EnhXKUehoqe1RzF37"
        return f"Te comparto la ubicación de nuestras oficinas: {url}"

    # 3) App móvil
    if any(k in t for k in ["app","aplicación","aplicacion","descargar","apk","play store","ios"]):
        url = (cfg.get("links") or {}).get("app") or "https://inhoustontexas.us/descargar-app/"
        return f"Descarga nuestra app gratuita aquí: {url}"

    # 4) Precios (requiere insistencia 2 veces)
    if any(k in t for k in ["precio","precios","cuánto cuesta","cuanto cuesta","tarifa","coste","costo"]):
        _PRICE_COUNT[psid] += 1
        if _PRICE_COUNT[psid] >= 2:
            # Usa los precios de tu JSON si están; si no, usa defaults
            s = (cfg.get("services") or {})  # si en tu JSON los guardas distinto, ajusta aquí
            # Defaults (tomados de tu mensaje previo)
            prices = {
                "1/4 página": "$420",
                "1/2 página": "$750",
                "Página completa": "$1300",
                "2 páginas interiores": "$2200",
                "2 páginas centrales/primeras/últimas": "$3000",
            }
            # Componer respuesta breve
            parts = [f"{k}: {v}" for k,v in prices.items()]
            return "Precios (por edición): " + " | ".join(parts)
        else:
            return "Con gusto te comparto opciones. ¿Qué tamaño de anuncio te interesa (1/4, 1/2 o página completa)?"

    return None

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
        return jsonify({"status":"env-missing"}), 200

    body = request.get_json(silent=True) or {}
    logging.info("WEBHOOK IG RAW: %s", json.dumps(body, ensure_ascii=False))
    if body.get("object") not in ("instagram","page"):
        return jsonify({"status":"ignored"}), 200

    bot_cfg = _get_bot_cfg()
    system_prompt = (bot_cfg.get("system_prompt") or "").strip()
    model   = (bot_cfg.get("model") or "gpt-4o").strip()
    try:
        temperature = float(bot_cfg.get("temperature", 0.6))
    except Exception:
        temperature = 0.6

    senders = []

    for entry in body.get("entry", []):
        processed_changes = False

        # A) Preferimos changes.value.messaging
        for change in (entry.get("changes") or []):
            processed_changes = True
            for ev in (change.get("value",{}).get("messaging") or []):
                msg  = (ev.get("message") or {})
                mid  = (msg.get("mid") or "").strip()
                if _seen_mid(mid): continue
                if msg.get("is_echo"): continue

                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                text = (msg.get("text") or "").strip()
                if not psid or not text: continue
                senders.append(psid)

                # 1) Greeting primera vez
                greeting = (bot_cfg.get("greeting") or "").strip()
                if _first_time_user(psid) and greeting:
                    _send_ig_text(psid, _apply_style(greeting))
                    _append_historial(bot_cfg.get("name","INH"), psid, "bot", greeting)

                # 2) Intents rápidos
                quick = _intent_reply(psid, text, bot_cfg)
                if quick:
                    _send_ig_text(psid, _apply_style(quick))
                    _append_historial(bot_cfg.get("name","INH"), psid, "bot", quick)
                    continue

                # 3) GPT fallback
                _append_historial(bot_cfg.get("name","INH"), psid, "user", text)
                reply = _gpt_reply(system_prompt, text, model, temperature)
                _send_ig_text(psid, _apply_style(reply))
                _append_historial(bot_cfg.get("name","INH"), psid, "bot", reply)

        # B) Legacy entry.messaging
        if not processed_changes:
            for ev in (entry.get("messaging") or []):
                msg  = (ev.get("message") or {})
                mid  = (msg.get("mid") or "").strip()
                if _seen_mid(mid): continue
                if msg.get("is_echo"): continue

                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                text = (msg.get("text") or "").strip()
                if not psid or not text: continue
                senders.append(psid)

                greeting = (bot_cfg.get("greeting") or "").strip()
                if _first_time_user(psid) and greeting:
                    _send_ig_text(psid, _apply_style(greeting))
                    _append_historial(bot_cfg.get("name","INH"), psid, "bot", greeting)

                quick = _intent_reply(psid, text, bot_cfg)
                if quick:
                    _send_ig_text(psid, _apply_style(quick))
                    _append_historial(bot_cfg.get("name","INH"), psid, "bot", quick)
                    continue

                _append_historial(bot_cfg.get("name","INH"), psid, "user", text)
                reply = _gpt_reply(system_prompt, text, model, temperature)
                _send_ig_text(psid, _apply_style(reply))
                _append_historial(bot_cfg.get("name","INH"), psid, "bot", reply)

    logging.info("WEBHOOK IG SENDER_IDS: %s", senders)
    return jsonify({"status":"ok","senders":senders}), 200
