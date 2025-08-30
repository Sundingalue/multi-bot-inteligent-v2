# IG Webhook: replica el pipeline de WhatsApp con detección de link, uso de URLs del JSON
# y ahora respeta el switch ON/OFF desde WordPress (REST) con caché.

import os
import re
import json
import time
import logging
from datetime import datetime
from collections import deque, defaultdict
import requests
from flask import Blueprint, request, jsonify, current_app

logging.basicConfig(level=logging.INFO)
ig_bp = Blueprint("instagram_webhook", __name__)

# ===== Entorno =====
META_VERIFY_TOKEN      = (os.getenv("META_VERIFY_TOKEN") or "").strip()
META_PAGE_ACCESS_TOKEN = (os.getenv("META_PAGE_ACCESS_TOKEN") or "").strip()
META_PAGE_ID           = (os.getenv("META_PAGE_ID") or "").strip()

# NUEVO: credenciales para intercambio de tokens
META_APP_ID     = (os.getenv("META_APP_ID", "279917021820450")).strip()
META_APP_SECRET = (os.getenv("META_APP_SECRET") or "").strip()
REDIRECT_URI    = "https://inhoustontexas.us/?ig_auth_redirect=1"

# NUEVO: URL del estado ON/OFF publicada por WordPress
WP_IG_STATUS_URL = (os.getenv("WP_IG_STATUS_URL") or "").strip()
IG_STATUS_TTL    = int(os.getenv("IG_STATUS_TTL", "20"))       # 20s por defecto
IG_STATUS_DEFAULT_ON = (os.getenv("IG_STATUS_DEFAULT", "on").lower() in ("1","true","on","yes"))

# ===== Anti-duplicados =====
_SEEN_MIDS = deque(maxlen=1000)
_SEEN_SET  = set()
def _seen_mid(mid: str) -> bool:
    if not mid: return False
    if mid in _SEEN_SET: return True
    _SEEN_SET.add(mid); _SEEN_MIDS.append(mid)
    if len(_SEEN_SET) > _SEEN_MIDS.maxlen:
        viejo = _SEEN_MIDS.popleft(); _SEEN_SET.discard(viejo)
    return False

# ===== Estado de sesión IG =====
IG_SESSION_HISTORY = defaultdict(list)
IG_GREETED         = set()

def _clave_sesion(page_id: str, psid: str) -> str:
    return f"ig:{page_id}|{psid}"

# ===== Helpers estilo =====
def _split_sentences(text: str):
    parts = re.split(r'(?<=[\.\!\?])\s+', (text or "").strip())
    if len(parts) == 1 and len(text or "") > 280:
        parts = [text[:200].strip(), text[200:].strip()]
    return [p for p in parts if p]

def _apply_style(bot_cfg: dict, text: str) -> str:
    if not text: return text
    style = (bot_cfg or {}).get("style", {}) or {}
    short = bool(style.get("short_replies", True))
    max_sents = int(style.get("max_sentences", 2)) if style.get("max_sentences") is not None else 2
    if not short:
        return text
    has_url = bool(re.search(r"https?://\S+", text))
    if has_url:
        return text
    sents = _split_sentences(text)
    return " ".join(sents[:max_sents]).strip()

def _next_probe_from_bot(bot_cfg: dict) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    probes = style.get("probes") or []
    probes = [p.strip() for p in probes if isinstance(p, str) and p.strip()]
    if not probes: return ""
    import random
    return random.choice(probes)

def _ensure_question(bot_cfg: dict, text: str, force_question: bool) -> str:
    if not text: return text
    txt = re.sub(r"\s+", " ", text).strip()
    if not force_question: return txt
    if "?" in txt: return txt
    if not txt.endswith((".", "!", "…")):
        txt += "."
    probe = _next_probe_from_bot(bot_cfg)
    return f"{txt} {probe}".strip() if probe else txt

# ===== URLs del JSON =====
def _valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

def _drill_get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def _effective_booking_url(bot_cfg: dict) -> str:
    candidates = [
        "links.booking_url",
        "booking_url",
        "calendar_booking_url",
        "google_calendar_booking_url",
        "agenda.booking_url",
    ]
    for p in candidates:
        val = _drill_get(bot_cfg or {}, p)
        val = (val or "").strip() if isinstance(val, str) else ""
        if _valid_url(val): return val
    env_fallback = (os.environ.get("BOOKING_URL") or "").strip()
    return env_fallback if _valid_url(env_fallback) else ""

# ===== Intenciones =====
SCHEDULE_OFFER_PAT = re.compile(
    r"\b(enlace|link|calendar|calendario|agendar|agenda|reservar|reserva|cita|schedule|book|appointment|meeting|call)\b",
    re.IGNORECASE
)
def _wants_link(text: str) -> bool:
    return bool(SCHEDULE_OFFER_PAT.search(text or ""))

# ===== Bot config lookup =====
def _get_bot_cfg_for_page(page_id: str) -> dict:
    bots = current_app.config.get("BOTS_CONFIG") or {}
    for _, cfg in bots.items():
        ch = (cfg.get("channels") or {}).get("instagram") or {}
        if (ch.get("page_id") or "").strip() == (page_id or "").strip():
            return cfg
    for _, cfg in bots.items():
        ch = (cfg.get("channels") or {}).get("instagram") or {}
        if (ch.get("page_id") or "").strip() == (META_PAGE_ID or "").strip():
            return cfg
    return list(bots.values())[0] if bots else {}

# ===== Firebase append =====
def _append_historial(bot_nombre: str, user_id: str, tipo: str, texto: str):
    try:
        if str(user_id).startswith("ig:"):
            logging.info("[IG] Mensaje ignorado en leads (solo IG): %s", user_id)
            return
        fb_append = current_app.config.get("FB_APPEND_HISTORIAL")
        if callable(fb_append):
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append(bot_nombre, user_id, {
                "tipo": tipo,
                "texto": texto,
                "hora": ahora
            })
    except Exception as e:
        logging.warning("[IG] No se pudo guardar historial: %s", e)

# ===== OpenAI =====
def _gpt_reply(messages, model_name: str, temperature: float) -> str:
    try:
        client = current_app.config.get("OPENAI_CLIENT")
        if client is None:
            return "¿En qué puedo ayudarte?"
        c = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=messages
        )
        return (c.choices[0].message.content or "").strip()
    except Exception as e:
        logging.error("[IG] Error OpenAI: %s", e)
        return "Estoy teniendo un problema técnico. Intentémoslo de nuevo."

# ===== Estado ON/OFF =====
_IG_STATUS_CACHE = {"ok": IG_STATUS_DEFAULT_ON, "ts": 0.0}
def _ig_is_enabled() -> bool:
    now = time.time()
    if WP_IG_STATUS_URL and (now - _IG_STATUS_CACHE["ts"] < IG_STATUS_TTL):
        return _IG_STATUS_CACHE["ok"]
    if not WP_IG_STATUS_URL:
        _IG_STATUS_CACHE.update({"ok": IG_STATUS_DEFAULT_ON, "ts": now})
        return IG_STATUS_DEFAULT_ON
    try:
        r = requests.get(WP_IG_STATUS_URL, timeout=5)
        if r.status_code == 200:
            data = r.json()
            ok = bool(data.get("enabled", True))
        else:
            logging.warning("[IG] Estado WP HTTP %s — usando default=%s", r.status_code, IG_STATUS_DEFAULT_ON)
            ok = IG_STATUS_DEFAULT_ON
        _IG_STATUS_CACHE.update({"ok": ok, "ts": now})
        return ok
    except Exception as e:
        logging.warning("[IG] No se pudo leer estado desde WP: %s — usando default=%s", e, IG_STATUS_DEFAULT_ON)
        _IG_STATUS_CACHE.update({"ok": IG_STATUS_DEFAULT_ON, "ts": now})
        return IG_STATUS_DEFAULT_ON

# ===== Envío IG =====
def _send_ig_text(psid: str, text: str) -> bool:
    if not _ig_is_enabled():
        logging.info("[IG] Bloqueado envío: bot OFF (panel WP).")
        return False
    if not META_PAGE_ACCESS_TOKEN or not META_PAGE_ID:
        logging.error("[IG] Faltan META_PAGE_ACCESS_TOKEN o META_PAGE_ID")
        return False
    url = f"https://graph.facebook.com/v21.0/{META_PAGE_ID}/messages"
    payload = {
        "recipient": {"id": psid},
        "message": {"text": (text or "Gracias por escribirnos.")[:1000]}
    }
    headers = {
        "Authorization": f"Bearer {META_PAGE_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        try: j = r.json()
        except Exception: j = {"_non_json": r.text}
        logging.info("[IG] SEND status=%s resp=%s", r.status_code, j)
        return r.status_code < 400
    except Exception as e:
        logging.error("[IG] Excepción enviando mensaje: %s", e)
        return False

# ===== Normaliza links =====
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
def _ensure_plain_url(text: str) -> str:
    if not text: return text
    def _rep(m):
        label = m.group(1).strip()
        url = m.group(2).strip()
        if not label: return url
        return f"{label}: {url}"
    text = _MD_LINK.sub(_rep, text)
    return text

# ===== Verificación =====
@ig_bp.route("/webhook_instagram", methods=["GET"])
def ig_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return challenge, 200
    return "Token inválido", 403

# ===== Estado debug =====
@ig_bp.route("/ig_status", methods=["GET"])
def ig_status():
    left = max(0, IG_STATUS_TTL - (time.time() - _IG_STATUS_CACHE["ts"]))
    return jsonify({
        "enabled": _ig_is_enabled(),
        "cache_seconds_remaining": round(left, 1),
        "wp_url_configured": bool(WP_IG_STATUS_URL),
        "default_on_if_wp_fails": IG_STATUS_DEFAULT_ON
    }), 200

# ===== Eventos =====
@ig_bp.route("/webhook_instagram", methods=["POST"])
def ig_events():
    if not _ig_is_enabled():
        logging.info("[IG] Bot OFF por panel WP — ignorando mensaje entrante.")
        return jsonify({"status":"disabled"}), 200
    if not META_PAGE_ACCESS_TOKEN or not META_PAGE_ID:
        logging.error("[IG] Faltan variables: META_PAGE_ACCESS_TOKEN o META_PAGE_ID.")
        return jsonify({"status":"env-missing"}), 200

    body = request.get_json(silent=True) or {}
    logging.info("WEBHOOK IG RAW: %s", json.dumps(body, ensure_ascii=False))
    if body.get("object") not in ("instagram","page"):
        return jsonify({"status":"ignored"}), 200

    senders = []

    def handle_one(page_id: str, psid: str, text: str, mid: str, is_echo: bool):
        if not psid or not text: return
        if is_echo or _seen_mid(mid): return

        bot_cfg = _get_bot_cfg_for_page(page_id)
        if not bot_cfg: return

        system_prompt = (bot_cfg.get("system_prompt") or "").strip()
        model_name    = (bot_cfg.get("model") or "gpt-4o").strip()
        temperature   = float(bot_cfg.get("temperature", 0.6)) if isinstance(bot_cfg.get("temperature", None), (int,float)) else 0.6
        ch_ig         = (bot_cfg.get("channels") or {}).get("instagram") or {}
        intro_keywords = ch_ig.get("intro_keywords") or bot_cfg.get("intro_keywords") or ["hola","buenas","buenos dias","buenas tardes","buenas noches"]

        clave = _clave_sesion(page_id, psid)
        if not IG_SESSION_HISTORY.get(clave):
            IG_SESSION_HISTORY[clave] = [{"role":"system","content":system_prompt}] if system_prompt else []

        low = text.lower()

        # ✅ Solo saludo desde JSON y NO primera respuesta de OpenAI
        saludo_json = ""  # inicializamos la variable

        if (clave not in IG_GREETED) and any(k in low for k in intro_keywords):
          saludo_json = ch_ig.get("intro_message") or bot_cfg.get("intro_message") or ""
        if saludo_json:
         _send_ig_text(psid, _apply_style(bot_cfg, saludo_json))
         IG_GREETED.add(clave)
         _append_historial(bot_cfg.get("name","BOT"), f"ig:{psid}", "bot", saludo_json)
         return  # solo corta en el primer saludo


        if _wants_link(text):
            url = _effective_booking_url(bot_cfg)
            if _valid_url(url):
                msg = ch_ig.get("link_message") or (bot_cfg.get("agenda", {}) or {}).get("link_message") or "Aquí tienes el enlace:"
                final = f"{msg.strip()} {url}".strip()
                _send_ig_text(psid, final)
                _append_historial(bot_cfg.get("name","BOT"), f"ig:{psid}", "bot", final)
                senders.append(psid)
                return

        IG_SESSION_HISTORY[clave].append({"role":"user","content":text})
        _append_historial(bot_cfg.get("name","BOT"), f"ig:{psid}", "user", text)

        if not _ig_is_enabled():
            logging.info("[IG] Bot OFF tras revalidar — no se genera respuesta.")
            return

        respuesta = _gpt_reply(IG_SESSION_HISTORY[clave], model_name, temperature)
        respuesta = _ensure_plain_url(respuesta)
        respuesta = _apply_style(bot_cfg, respuesta)
        must_ask = bool((bot_cfg.get("style") or {}).get("always_question", False))
        respuesta = _ensure_question(bot_cfg, respuesta, force_question=must_ask)

        if IG_SESSION_HISTORY[clave]:
            last_assistant = next((m["content"] for m in reversed(IG_SESSION_HISTORY[clave]) if m["role"]=="assistant"), "")
            if last_assistant and last_assistant.strip() == respuesta.strip():
                probe = _next_probe_from_bot(bot_cfg)
                if probe and probe not in respuesta:
                    if not respuesta.endswith((".", "!", "…", "¿", "?")):
                        respuesta += "."
                    respuesta = f"{respuesta} {probe}".strip()

        _send_ig_text(psid, respuesta)
        IG_SESSION_HISTORY[clave].append({"role":"assistant","content":respuesta})
        _append_historial(bot_cfg.get("name","BOT"), f"ig:{psid}", "bot", respuesta)
        senders.append(psid)

    for entry in (body.get("entry") or []):
        page_id = entry.get("id") or META_PAGE_ID
        for change in (entry.get("changes") or []):
            for ev in (change.get("value",{}).get("messaging") or []):
                psid = ((ev.get("sender") or {}).get("id") or "").strip()
                msg  = (ev.get("message") or {}) or {}
                mid  = (msg.get("mid") or "").strip()
                txt  = (msg.get("text") or "").strip()
                is_echo = bool(msg.get("is_echo"))
                handle_one(page_id, psid, txt, mid, is_echo)

    for entry in (body.get("entry") or []):
        page_id = entry.get("id") or META_PAGE_ID
        for ev in (entry.get("messaging") or []):
            psid = ((ev.get("sender") or {}).get("id") or "").strip()
            msg  = (ev.get("message") or {}) or {}
            mid  = (msg.get("mid") or "").strip()
            txt  = (msg.get("text") or "").strip()
            is_echo = bool(msg.get("is_echo"))
            handle_one(page_id, psid, txt, mid, is_echo)

    logging.info("WEBHOOK IG SENDER_IDS: %s", senders)
    return jsonify({"status":"ok","senders":senders}), 200

# ===== NUEVO: Endpoint para intercambiar "code" -> access_token =====
@ig_bp.route("/ig_exchange_token", methods=["GET"])
def ig_exchange_token():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Falta el parámetro 'code'"}), 400
    try:
        url = "https://graph.facebook.com/v21.0/oauth/access_token"
        params = {
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        }
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
        return jsonify(data), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500
