# main.py — columna vertebral (delgado) 🔥
# Mantiene rutas y registro de blueprints; delega utilidades a helpers.py, services.py y state.py

# 💥 Eventlet (como pediste)
import eventlet
eventlet.monkey_patch(os=False)

import os
import sys
import json
import time
import re
import glob
from datetime import datetime, timedelta
from io import StringIO
from flask import (
    Flask, request, session, redirect, url_for, send_file,
    send_from_directory, jsonify, render_template, make_response, Response
)

# ─────────────────────────────────────────────────────────────
# Módulos locales (los 3 nuevos archivos)
# ─────────────────────────────────────────────────────────────
import services as svc
from helpers import (
    valid_url, drill_get, canonize_phone, apply_style, ensure_question,
    make_system_message, hash_text
)
from state import (
    session_history, last_message_time, follow_up_flags, agenda_state, greeted_state,
    voice_call_cache, voice_conversation_history,
    get_agenda, set_agenda, can_send_link, hydrate_session_from_firebase,
    set_last_bot_hash
)

# ─────────────────────────────────────────────────────────────
# SDKs y dependencias originales
# ─────────────────────────────────────────────────────────────
from dotenv import load_dotenv
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
# Twilio REST via services.make_twilio_client

# Blueprints ya existentes en tu repo (se registran si están)
# Si alguno no existe en tu proyecto local, no rompe el arranque.
def _safe_import_blueprint(import_path, bp_name):
    try:
        mod = __import__(import_path, fromlist=[bp_name])
        return getattr(mod, bp_name)
    except Exception as e:
        print(f"⚠️ Blueprint opcional no disponible: {import_path}.{bp_name} -> {e}")
        return None

# ElevenLabs realtime
eleven_rt_bp     = _safe_import_blueprint("eleven_realtime", "bp")
eleven_bp        = _safe_import_blueprint("routes.eleven_session", "bp")
eleven_webrtc_bp = _safe_import_blueprint("routes.eleven_webrtc", "bp")

# Avatares / realtime OpenAI
realtime_bp   = _safe_import_blueprint("avatar_realtime", "bp")
profiles_bp   = _safe_import_blueprint("avatar_profiles", "bp")
voice_rt_bp   = _safe_import_blueprint("voice_realtime", "bp")

# WebRTC bridge (Twilio Media Streams ↔ OpenAI Realtime)
_webrtc_mod = None
webrtc_bridge_bp = None
webrtc_sock = None
try:
    _webrtc_mod = __import__("voice_webrtc_bridge", fromlist=["bp", "sock"])
    webrtc_bridge_bp = getattr(_webrtc_mod, "bp", None)
    webrtc_sock = getattr(_webrtc_mod, "sock", None)
except Exception as e:
    print(f"⚠️ voice_webrtc_bridge opcional: {e}")

# Enlaces / utilidades adicionales
send_link_bp = _safe_import_blueprint("routes.send_link", "bp")

# Instagram
ig_bp        = _safe_import_blueprint("instagram_webhook", "ig_bp")
ig_multi_bp  = _safe_import_blueprint("instagram_api_multi", "ig_multi_bp")

# APIs móviles y billing
mobile_bp    = _safe_import_blueprint("bots.api_mobile", "mobile_bp")
billing_bp   = _safe_import_blueprint("billing_api", "billing_bp")
try:
    from billing_api import record_openai_usage  # opcional
except Exception:
    def record_openai_usage(*args, **kwargs):
        return None

# ─────────────────────────────────────────────────────────────
# App Flask
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)

# Registrar blueprints disponibles
for bp in [
    realtime_bp, profiles_bp, voice_rt_bp,
    eleven_rt_bp, eleven_bp, eleven_webrtc_bp,
    send_link_bp, webrtc_bridge_bp,
    ig_bp, ig_multi_bp,
    mobile_bp, billing_bp
]:
    if bp:
        try:
            # usa url_prefix si el módulo ya trae uno; si no, por defecto
            app.register_blueprint(bp)
        except Exception as e:
            print(f"⚠️ No se pudo registrar blueprint {bp}: {e}")

# WebSocket init (si existe)
if webrtc_sock:
    try:
        webrtc_sock.init_app(app)
    except Exception as e:
        print(f"⚠️ No se pudo inicializar webrtc_sock: {e}")

# ─────────────────────────────────────────────────────────────
# Config / entorno
# ─────────────────────────────────────────────────────────────
load_dotenv("/etc/secrets/.env")
load_dotenv()

OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
TWILIO_ACCOUNT_SID = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN  = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()

BOOKING_URL_FALLBACK     = (os.environ.get("BOOKING_URL") or "").strip()
APP_DOWNLOAD_URL_FALLBACK= (os.environ.get("APP_DOWNLOAD_URL") or "").strip()
API_BEARER_TOKEN         = (os.environ.get("API_BEARER_TOKEN") or "").strip()

if BOOKING_URL_FALLBACK and not valid_url(BOOKING_URL_FALLBACK):
    print(f"⚠️ BOOKING_URL_FALLBACK inválido: '{BOOKING_URL_FALLBACK}'")
if APP_DOWNLOAD_URL_FALLBACK and not valid_url(APP_DOWNLOAD_URL_FALLBACK):
    print(f"⚠️ APP_DOWNLOAD_URL_FALLBACK inválido: '{APP_DOWNLOAD_URL_FALLBACK}'")

client = OpenAI(api_key=OPENAI_API_KEY)
app.secret_key = "supersecreto_sundin_panel_2025"

# JSON de Tarjeta Inteligente
JSON_DIR = os.path.join(os.path.dirname(__file__), "bots", "tarjeta_inteligente")

# Sesión persistente
app.permanent_session_lifetime = timedelta(days=60)
app.config.update({
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_SECURE": False if (os.getenv("DEV_HTTP","").lower()=="true") else True
})

# CORS básico
ALLOWED_ORIGINS = {
    "https://inhoustontexas.us",
    "https://www.inhoustontexas.us"
}

@app.after_request
def add_cors_headers(resp):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

def _bearer_ok(req) -> bool:
    if not API_BEARER_TOKEN:
        return True
    auth = (req.headers.get("Authorization") or "").strip()
    return auth == f"Bearer {API_BEARER_TOKEN}"

# ─────────────────────────────────────────────────────────────
# Firebase init (idéntico comportamiento)
# ─────────────────────────────────────────────────────────────
firebase_key_path = "/etc/secrets/firebase.json"
firebase_db_url = (os.getenv("FIREBASE_DB_URL") or "").strip()
if not firebase_db_url:
    try:
        with open("/etc/secrets/FIREBASE_DB_URL", "r", encoding="utf-8") as f:
            firebase_db_url = f.read().strip().strip('"').strip("'")
            if firebase_db_url:
                print("[BOOT] FIREBASE_DB_URL leído desde Secret File.")
    except Exception:
        pass

if not firebase_db_url:
    print("❌ FIREBASE_DB_URL no configurado. Define env o /etc/secrets/FIREBASE_DB_URL.")

svc.init_firebase(firebase_key_path, firebase_db_url)

# ─────────────────────────────────────────────────────────────
# Twilio REST Client
# ─────────────────────────────────────────────────────────────
twilio_client = svc.make_twilio_client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
if twilio_client:
    print("[BOOT] Twilio REST client inicializado.")
else:
    print("⚠️ TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN no configurados o inválidos.")

# ─────────────────────────────────────────────────────────────
# Cargar bots
# ─────────────────────────────────────────────────────────────
def load_bots_folder_root() -> dict:
    bots = {}
    for path in glob.glob(os.path.join("bots", "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    bots[k] = v
        except Exception as e:
            print(f"⚠️ No se pudo cargar {path}: {e}")
    return bots

bots_config = load_bots_folder_root()
if not bots_config:
    print("⚠️ No se encontraron bots en ./bots/*.json")

# Exponer mínimos a otros módulos ya registrados
app.config["BOTS_CONFIG"] = bots_config
app.config["OPENAI_CLIENT"] = client
app.config["FB_APPEND_HISTORIAL"] = svc.fb_append_historial

# ─────────────────────────────────────────────────────────────
# Endpoints JSON/Avatar
# ─────────────────────────────────────────────────────────────
@app.route("/clients/<path:filename>", methods=["GET"])
def clients_static(filename):
    return send_from_directory(JSON_DIR, filename, mimetype="application/json")

@app.route("/api/avatar/<slug>.json", methods=["GET"])
def api_avatar(slug):
    fp = os.path.join(JSON_DIR, f"{slug}.json")
    if not os.path.isfile(fp):
        return jsonify({"error": "Perfil no encontrado"}), 404
    with open(fp, "r", encoding="utf-8") as f:
        data = json.load(f) or {}
    data.setdefault("slug", slug)
    data.setdefault("endpoints", {}).setdefault(
        "realtime_session",
        "https://multi-bot-inteligente-v1.onrender.com/realtime/session"
    )
    return jsonify(data)

# ─────────────────────────────────────────────────────────────
# WhatsApp Verify (GET)
# ─────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_whatsapp():
    VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN_WHATSAPP")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Token inválido", 403

# Helpers locales
def _compose_with_link(prefix: str, link: str) -> str:
    if valid_url(link):
        return f"{prefix.strip()} {link}".strip()
    return prefix.strip()

def _effective_booking_url(bot_cfg: dict) -> str:
    return svc.effective_booking_url(bot_cfg, BOOKING_URL_FALLBACK)

def _effective_app_url(bot_cfg: dict) -> str:
    return svc.effective_app_url(bot_cfg, APP_DOWNLOAD_URL_FALLBACK)

def _get_bot_cfg_by_number(to_number: str):
    return bots_config.get(to_number)

def _get_bot_cfg_by_any_number(to_number: str):
    if not to_number and len(bots_config) == 1:
        return list(bots_config.values())[0]
    canon_to = canonize_phone(to_number)
    for key, cfg in bots_config.items():
        if canonize_phone(key) == canon_to:
            return cfg
    return bots_config.get(to_number)

def _get_bot_number_by_name(bot_name: str) -> str:
    for number_key, cfg in bots_config.items():
        if isinstance(cfg, dict) and cfg.get("name","").strip().lower() == (bot_name or "").strip().lower():
            return number_key
    return ""

# ─────────────────────────────────────────────────────────────
# WhatsApp Bot (POST)
# ─────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    incoming_msg  = (request.values.get("Body", "") or "").strip()
    sender_number = request.values.get("From", "")
    bot_number    = request.values.get("To", "")

    clave_sesion = f"{bot_number}|{sender_number}"
    bot = _get_bot_cfg_by_number(bot_number)

    if not bot:
        resp = MessagingResponse()
        resp.message("Este número no está asignado a ningún bot.")
        return str(resp)

    # Hidratar sesión desde Firebase si es necesario
    hydrate_session_from_firebase(
        clave_sesion, bot, sender_number,
        fb_get_lead_func=svc.fb_get_lead,
        make_system_message_func=make_system_message
    )

    # Guardar entrada del usuario en RTDB
    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        svc.fb_append_historial(bot["name"], sender_number, {"tipo": "user", "texto": incoming_msg, "hora": ahora})
    except Exception as e:
        print(f"❌ Error guardando lead: {e}")

    bot_name = bot.get("name", "")
    if bot_name and not svc.fb_is_bot_on(bot_name):
        return str(MessagingResponse())

    if not svc.fb_is_conversation_on(bot_name, sender_number):
        return str(MessagingResponse())

    response = MessagingResponse()
    msg = response.message()

    # Descarga de App
    if "app" in incoming_msg.lower():
        if any(w in incoming_msg.lower() for w in ["descargar","download","instalar","link","enlace","app store","play store","ios","android"]):
            url_app = _effective_app_url(bot)
            if url_app:
                links_cfg = bot.get("links") or {}
                app_msg = (links_cfg.get("app_message") or "").strip() if isinstance(links_cfg, dict) else ""
                if app_msg:
                    texto = app_msg if app_msg.startswith(("http://","https://")) else _compose_with_link(app_msg, url_app)
                else:
                    texto = _compose_with_link("Aquí tienes:", url_app)
                msg.body(texto)
                set_agenda(clave_sesion, status="app_link_sent")
                agenda_state[clave_sesion]["closed"] = True
            else:
                msg.body("No tengo enlace de app disponible.")
            last_message_time[clave_sesion] = time.time()
            return str(response)

    # Negativa / cierre
    t_low = incoming_msg.strip().lower()
    negatives = {"no","nop","no gracias","ahora no","luego","después","despues","not now"}
    if t_low in negatives:
        cierre = _compose_with_link("Entendido.", _effective_booking_url(bot))
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    cierres = {"gracias","muchas gracias","ok gracias","listo gracias","perfecto gracias","estamos en contacto","por ahora está bien","por ahora esta bien","luego te escribo","luego hablamos","hasta luego","buen día","buen dia","buenas noches","nos vemos","chao","bye","eso es todo","todo bien gracias"}
    if t_low in cierres or any(t_low.startswith(c + " ") for c in cierres):
        cierre = bot.get("policies", {}).get("polite_closure_message", "Gracias por contactarnos. ¡Hasta pronto!")
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Estado de agenda
    st = get_agenda(clave_sesion)
    agenda_cfg = (bot.get("agenda") or {}) if isinstance(bot, dict) else {}

    def _subst_link(texto: str) -> str:
        return re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (texto or ""), flags=re.IGNORECASE)

    confirm_q = _subst_link(agenda_cfg.get("confirm_question") or "")
    decline_msg = _subst_link(agenda_cfg.get("decline_message") or "")
    closing_default = _subst_link(agenda_cfg.get("closing_message") or "")

    # Confirmaciones tipo "ya agendé"
    if any(k in t_low for k in ["ya agende","ya agendé","agende","agendé","ya programe","ya programé","ya agendado","agendado","confirmé","confirmado","listo","done","booked","i booked","i scheduled","scheduled"]):
        texto = closing_default or "Agendado."
        msg.body(texto)
        set_agenda(clave_sesion, status="confirmed")
        agenda_state[clave_sesion]["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Esperando confirmación
    affirmative = {"si","sí","ok","okay","dale","va","claro","por favor","hagamoslo","hagámoslo","perfecto","de una","yes","yep","yeah","sure","please"}
    if st.get("awaiting_confirm"):
        if t_low in affirmative or any(t_low.startswith(a+" ") for a in affirmative):
            if can_send_link(clave_sesion, cooldown_min=10):
                link = _effective_booking_url(bot)
                link_message = _subst_link((agenda_cfg.get("link_message") or "").strip())
                texto = link_message if link_message else (_compose_with_link("Enlace:", link) if link else "Sin enlace disponible.")
                msg.body(texto)
                set_agenda(clave_sesion, awaiting_confirm=False, status="link_sent", last_link_time=int(time.time()))
                agenda_state[clave_sesion]["closed"] = True
                try:
                    ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    svc.fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": texto, "hora": ahora_bot})
                except Exception as e:
                    print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")
            else:
                msg.body("Enlace enviado recientemente.")
                set_agenda(clave_sesion, awaiting_confirm=False)
            last_message_time[clave_sesion] = time.time()
            return str(response)
        elif t_low in negatives:
            if decline_msg:
                msg.body(decline_msg)
            set_agenda(clave_sesion, awaiting_confirm=False)
            agenda_state[clave_sesion]["closed"] = True
            last_message_time[clave_sesion] = time.time()
            return str(response)
        else:
            if confirm_q:
                msg.body(confirm_q)
            last_message_time[clave_sesion] = time.time()
            return str(response)

    # Palabras clave de agenda
    if any(k in t_low for k in (bot.get("agenda", {}).get("keywords", []) or [])):
        if confirm_q:
            msg.body(confirm_q)
        set_agenda(clave_sesion, awaiting_confirm=True)
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Inicializar sesión en memoria si no existe
    if clave_sesion not in session_history:
        sysmsg = make_system_message(bot)
        session_history[clave_sesion] = [{"role": "system", "content": sysmsg}] if sysmsg else []
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        greeted_state[clave_sesion] = False

    # Saludo inicial condicionado
    greeting_text = (bot.get("greeting") or "").strip()
    intro_keywords = (bot.get("intro_keywords") or [])
    if (not greeted_state.get(clave_sesion)) and greeting_text and any(w in t_low for w in intro_keywords):
        msg.body(greeting_text)
        greeted_state[clave_sesion] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    # Conversación con OpenAI
    session_history.setdefault(clave_sesion, []).append({"role": "user", "content": incoming_msg})
    last_message_time[clave_sesion] = time.time()

    try:
        model_name = (bot.get("model") or "gpt-4o").strip()
        temperature = float(bot.get("temperature", 0.6)) if isinstance(bot.get("temperature", None), (int, float)) else 0.6

        completion = client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=session_history[clave_sesion]
        )

        respuesta = (completion.choices[0].message.content or "").strip()
        respuesta = apply_style(bot, respuesta)
        must_ask = bool((bot.get("style") or {}).get("always_question", False))
        respuesta = ensure_question(bot, respuesta, force_question=must_ask)

        # Evitar repetición idéntica
        if hash_text(respuesta) == (agenda_state.get(clave_sesion, {}) or {}).get("last_bot_hash"):
            probe = (bot.get("style", {}).get("probes") or [])
            if probe and probe[0] not in respuesta:
                if not respuesta.endswith((".", "!", "…", "¿", "?")):
                    respuesta += "."
                respuesta = f"{respuesta} {probe[0]}".strip()

        session_history[clave_sesion].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)
        set_last_bot_hash(clave_sesion, respuesta)

        # Billing (si está disponible)
        try:
            usage = getattr(completion, "usage", None)
            if usage:
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            else:
                to_dict = getattr(completion, "to_dict", lambda: {})()
                input_tokens = int(((to_dict or {}).get("usage") or {}).get("prompt_tokens", 0))
                output_tokens = int(((to_dict or {}).get("usage") or {}).get("completion_tokens", 0))
            record_openai_usage(bot.get("name", ""), model_name, input_tokens, output_tokens)
        except Exception as e:
            print(f"⚠️ No se pudo registrar tokens en billing: {e}")

        # Guardar respuesta en RTDB
        try:
            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            svc.fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": respuesta, "hora": ahora_bot})
        except Exception as e:
            print(f"⚠️ No se pudo guardar respuesta del bot: {e}")

    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Error generando la respuesta.")

    return str(response)

# ─────────────────────────────────────────────────────────────
# VOICE — Twilio Media Streams → WebRTC bridge
# ─────────────────────────────────────────────────────────────
@app.route("/voice", methods=["POST"])
def voice_webhook():
    resp = VoiceResponse()
    connect = Connect()
    ws_url = f"wss://{request.host}/voice-webrtc/stream"
    stream = connect.stream(url=ws_url)
    stream.parameter(name="to_number", value=request.values.get("To",""))
    stream.parameter(name="from_number", value=request.values.get("From",""))
    resp.append(connect)
    return str(resp)

# ─────────────────────────────────────────────────────────────
# Panel / Login / Logout / Home
# ─────────────────────────────────────────────────────────────
def _normalize_bot_name(name: str):
    for cfg in bots_config.values():
        if isinstance(cfg, dict):
            if cfg.get("name","").lower() == str(name).lower():
                return cfg.get("name")
    return None

def _load_users():
    # 1) desde bots/*.json
    users_from_json = {}

    def _normalize_list_scope(scope_val):
        if isinstance(scope_val, str):
            scope_val = scope_val.strip()
            if scope_val == "*":
                return ["*"]
            norm = _normalize_bot_name(scope_val) or scope_val
            return [norm]
        elif isinstance(scope_val, list):
            allowed = []
            for s in scope_val:
                s = (s or "").strip()
                if not s:
                    continue
                if s == "*":
                    return ["*"]
                allowed.append(_normalize_bot_name(s) or s)
            return allowed or []
        else:
            return []

    for cfg in bots_config.values():
        if not isinstance(cfg, dict):
            continue
        bot_name = (cfg.get("name") or "").strip()
        if not bot_name:
            continue
        logins = []
        if isinstance(cfg.get("login"), dict):
            logins.append(cfg["login"])
        if isinstance(cfg.get("logins"), list):
            logins.extend([x for x in cfg["logins"] if isinstance(x, dict)])
        if isinstance(cfg.get("auth"), dict):
            logins.append(cfg["auth"])
        for entry in logins:
            username = (entry.get("username") or "").strip()
            password = (entry.get("password") or "").strip()
            scope_val = entry.get("scope")
            panel_hint = (entry.get("panel") or "").strip().lower()
            if not username or not password:
                continue
            allowed_bots = _normalize_list_scope(scope_val)
            if not allowed_bots and panel_hint:
                if panel_hint == "panel":
                    allowed_bots = ["*"]
                elif panel_hint.startswith("panel-bot/"):
                    only_bot = panel_hint.split("/", 1)[1].strip()
                    if only_bot:
                        allowed_bots = [_normalize_bot_name(only_bot) or only_bot]
            if not allowed_bots:
                allowed_bots = [bot_name]
            if username in users_from_json:
                prev_bots = users_from_json[username].get("bots", [])
                if "*" in prev_bots or "*" in allowed_bots:
                    users_from_json[username]["bots"] = ["*"]
                else:
                    merged = list(dict.fromkeys(prev_bots + allowed_bots))
                    users_from_json[username]["bots"] = merged
                if password:
                    users_from_json[username]["password"] = password
            else:
                users_from_json[username] = {"password": password, "bots": allowed_bots}

    if users_from_json:
        return users_from_json

    # 2) env legacy
    env_users = {}
    for key, val in os.environ.items():
        if not key.startswith("USER_"):
            continue
        alias = key[len("USER_"):]
        username = (val or "").strip()
        password = (os.environ.get(f"PASS_{alias}", "") or "").strip()
        panel = (os.environ.get(f"PANEL_{alias}", "") or "").strip()
        if not username or not password or not panel:
            continue
        if panel.lower() == "panel":
            bots_list = ["*"]
        elif panel.lower().startswith("panel-bot/"):
            bot_name = panel.split("/", 1)[1].strip()
            bots_list = [_normalize_bot_name(bot_name) or bot_name] if bot_name else []
        else:
            bots_list = []
        if bots_list:
            env_users[username] = {"password": password, "bots": bots_list}
    if env_users:
        return env_users

    # 3) fallback
    return {"sundin": {"password": "inhouston2025", "bots": ["*"]}}

def _auth_user(username, password):
    users = _load_users()
    rec = users.get(username)
    if rec and rec.get("password") == password:
        return {"username": username, "bots": rec.get("bots", [])}
    return None

def _is_admin():
    bots = session.get("bots_permitidos", [])
    return isinstance(bots, list) and ("*" in bots)

def _first_allowed_bot():
    bots = session.get("bots_permitidos", [])
    if isinstance(bots, list):
        for b in bots:
            if b != "*":
                return b
    return None

def _user_can_access_bot(bot_name):
    if _is_admin():
        return True
    bots = session.get("bots_permitidos", [])
    return bot_name in bots

@app.route("/", methods=["GET"])
def home():
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    return "✅ Bot inteligente activo."

@app.route("/login", methods=["GET"])
def login_redirect():
    return redirect(url_for("panel"))

@app.route("/login.html", methods=["GET"])
def login_html_redirect():
    return redirect(url_for("panel"))

@app.route("/panel", methods=["GET", "POST"])
def panel():
    if not session.get("autenticado"):
        if request.method == "POST":
            usuario = (request.form.get("usuario") or request.form.get("username") or request.form.get("email") or "").strip()
            clave = request.form.get("clave")
            if clave is None or clave == "":
                clave = request.form.get("password")
            clave = (clave or "").strip()
            remember_flag = (request.form.get("recordarme") or request.form.get("remember") or "").strip().lower()
            remember_on = remember_flag in ("on","1","true","yes","si","sí")
            auth = _auth_user(usuario, clave)
            if auth:
                session["autenticado"] = True
                session["usuario"] = auth["username"]
                session["bots_permitidos"] = auth["bots"]
                session.permanent = bool(remember_on)
                if "*" in auth["bots"]:
                    destino_resp = redirect(url_for("panel"))
                else:
                    destino = _first_allowed_bot()
                    destino_resp = redirect(url_for("panel_exclusivo_bot", bot_nombre=destino)) if destino else redirect(url_for("panel"))
                resp = make_response(destino_resp)
                max_age = 60 * 24 * 60 * 60
                if remember_on:
                    resp.set_cookie("remember_login", "1", max_age=max_age, samesite="Lax", secure=app.config["SESSION_COOKIE_SECURE"])
                    resp.set_cookie("last_username", usuario, max_age=max_age, samesite="Lax", secure=app.config["SESSION_COOKIE_SECURE"])
                else:
                    resp.delete_cookie("remember_login")
                    resp.delete_cookie("last_username")
                return resp
            return render_template("login.html", error=True)
        return render_template("login.html")

    if not _is_admin():
        destino = _first_allowed_bot()
        if destino:
            return redirect(url_for("panel_exclusivo_bot", bot_nombre=destino))

    leads_todos = svc.fb_list_leads_all()
    bots_disponibles = {}
    for cfg in bots_config.values():
        bots_disponibles[cfg["name"]] = cfg.get("business_name", cfg["name"])

    bot_seleccionado = request.args.get("bot")
    if bot_seleccionado:
        bot_norm = _normalize_bot_name(bot_seleccionado) or bot_seleccionado
        leads_filtrados = {k: v for k, v in leads_todos.items() if v.get("bot") == bot_norm}
    else:
        leads_filtrados = leads_todos

    return render_template("panel.html", leads=leads_todos, bots=bots_disponibles, bot_seleccionado=bot_seleccionado)

@app.route("/panel-bot/<bot_nombre>")
def panel_exclusivo_bot(bot_nombre):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return f"Bot '{bot_nombre}' no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403
    leads_filtrados = svc.fb_list_leads_by_bot(bot_normalizado)
    nombre_comercial = next(
        (config.get("business_name", bot_normalizado)
            for config in bots_config.values()
            if config.get("name") == bot_normalizado),
        bot_normalizado
    )
    return render_template("panel_bot.html", leads=leads_filtrados, bot=bot_normalizado, nombre_comercial=nombre_comercial)

@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    resp = make_response(redirect(url_for("panel")))
    resp.delete_cookie("remember_login")
    resp.delete_cookie("last_username")
    return resp

# ─────────────────────────────────────────────────────────────
# Instagram OAuth y toggles (multiusuario)
# ─────────────────────────────────────────────────────────────
@app.route("/ig_auth_redirect", methods=["GET"])
def ig_auth_redirect():
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"❌ Error en autenticación de Instagram: {error}", 400
    if not code:
        return "❌ Falta parámetro 'code' en la redirección.", 400
    return f"✅ Login Instagram exitoso. Code recibido: {code}"

@app.route("/api/instagram/exchange_code", methods=["POST"])
def api_instagram_exchange_code():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    redirect_uri = (data.get("redirect_uri") or "").strip()
    if not code or not redirect_uri:
        return jsonify({"error": "Faltan parámetros code o redirect_uri"}), 400
    try:
        token_data = svc.ig_oauth_exchange(
            code, redirect_uri,
            os.getenv("IG_CLIENT_ID") or "",
            os.getenv("IG_CLIENT_SECRET") or "",
            graph_version="v21.0"
        )
        access_token = token_data.get("access_token")
        user_id = token_data.get("user_id")
        if not access_token:
            return jsonify({"error": "No se obtuvo access_token", "detalle": token_data}), 400
        ref_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        from firebase_admin import db
        db.reference(f"instagram_users/{user_id}").set({
            "access_token": access_token,
            "user_id": user_id,
            "created_at": ref_time
        })
        print(f"[IG] ✅ Nuevo login Instagram guardado user_id={user_id}")
        return jsonify({"ok": True, "user_id": user_id})
    except Exception as e:
        print(f"❌ Error intercambiando code Instagram: {e}")
        return jsonify({"error": "Fallo al procesar login Instagram"}), 500

@app.route("/api/instagram/bot_toggle", methods=["POST"])
def api_instagram_bot_toggle():
    data = request.get_json(force=True) or {}
    user_id = (data.get("user_id") or "").strip()
    enabled = data.get("enabled")
    if not user_id or enabled is None:
        return jsonify({"error": "Parámetros inválidos"}), 400
    try:
        svc.ig_set_enabled(user_id, bool(enabled))
        return jsonify({"ok": True, "user_id": user_id, "enabled": bool(enabled)})
    except Exception as e:
        print(f"❌ Error guardando estado IG {user_id}: {e}")
        return jsonify({"error": "Error interno"}), 500

@app.route("/api/instagram/bot_status/<user_id>", methods=["GET"])
def api_instagram_bot_status(user_id):
    try:
        enabled = svc.ig_get_enabled(user_id)
        return jsonify({"user_id": user_id, "enabled": bool(enabled)})
    except Exception as e:
        print(f"❌ Error leyendo estado IG {user_id}: {e}")
        return jsonify({"enabled": True, "error": str(e)})

# ─────────────────────────────────────────────────────────────
# Leads: guardar / borrar / vaciar
# ─────────────────────────────────────────────────────────────
@app.route("/guardar-lead", methods=["POST"])
def guardar_edicion():
    data = request.json or {}
    numero_key = (data.get("numero") or "").strip()
    estado = (data.get("estado") or "").strip()
    nota = (data.get("nota") or "").strip()
    if "|" not in numero_key:
        return jsonify({"error": "Parámetro 'numero' inválido"}), 400
    bot_nombre, numero = numero_key.split("|", 1)
    bot_normalizado = _normalize_bot_name(bot_nombre) or bot_nombre
    try:
        ref = svc.db.reference(f"leads/{bot_normalizado}/{numero}")
        current = ref.get() or {}
        if estado:
            current["status"] = estado
        if nota != "":
            current["notes"] = nota
        current.setdefault("bot", bot_normalizado)
        current.setdefault("numero", numero)
        ref.set(current)
    except Exception as e:
        print(f"⚠️ No se pudo actualizar en Firebase: {e}")
    return jsonify({"mensaje": "Lead actualizado"})

@app.route("/borrar-conversacion", methods=["POST"])
def borrar_conversacion_post():
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401
    data = request.json or {}
    numero_key = (data.get("numero") or "").strip()
    if "|" not in numero_key:
        return jsonify({"error": "Parámetro 'numero' inválido (esperado 'Bot|whatsapp:+1...')"}), 400
    bot_nombre, numero = numero_key.split("|", 1)
    bot_normalizado = _normalize_bot_name(bot_nombre) or bot_nombre
    ok = svc.fb_delete_lead(bot_normalizado, numero)
    return jsonify({"ok": ok, "bot": bot_normalizado, "numero": numero})

@app.route("/borrar-conversacion/<bot>/<numero>", methods=["GET"])
def borrar_conversacion_get(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot) or bot
    ok = svc.fb_delete_lead(bot_normalizado, numero)
    return redirect(url_for("panel", bot=bot_normalizado))

@app.route("/vaciar-historial", methods=["POST"])
def vaciar_historial_post():
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401
    data = request.json or {}
    numero_key = (data.get("numero") or "").strip()
    if "|" not in numero_key:
        return jsonify({"error": "Parámetro 'numero' inválido (esperado 'Bot|whatsapp:+1...')"}), 400
    bot_nombre, numero = numero_key.split("|", 1)
    bot_normalizado = _normalize_bot_name(bot_nombre) or bot_nombre
    ok = svc.fb_clear_historial(bot_normalizado, numero)
    return jsonify({"ok": ok, "bot": bot_normalizado, "numero": numero})

@app.route("/vaciar-historial/<bot>/<numero>", methods=["GET"])
def vaciar_historial_get(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot) or bot
    ok = svc.fb_clear_historial(bot_normalizado, numero)
    return redirect(url_for("conversacion_general", bot=bot_normalizado, numero=numero))

# Alias compatibilidad
@app.route("/api/delete_chat", methods=["POST"])
def api_delete_chat():
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401
    data = request.json or {}
    bot = (data.get("bot") or "").strip()
    numero = (data.get("numero") or "").strip()
    if not bot or not numero:
        return jsonify({"error": "Parámetros inválidos (bot, numero)"}), 400
    bot_normalizado = _normalize_bot_name(bot) or bot
    ok = svc.fb_delete_lead(bot_normalizado, numero)
    return jsonify({"ok": ok, "bot": bot_normalizado, "numero": numero})

# ─────────────────────────────────────────────────────────────
# API: enviar manual (panel/app con Bearer)
# ─────────────────────────────────────────────────────────────
@app.route("/api/send_manual", methods=["POST", "OPTIONS"])
def api_send_manual():
    if request.method == "OPTIONS":
        return ("", 204)
    if not session.get("autenticado") and not _bearer_ok(request):
        return jsonify({"error": "No autenticado"}), 401

    data = request.json or {}
    bot_nombre = (data.get("bot") or "").strip()
    numero = (data.get("numero") or "").strip()
    texto = (data.get("texto") or "").strip()
    if not bot_nombre or not numero or not texto:
        return jsonify({"error": "Parámetros inválidos (bot, numero, texto)"}), 400

    bot_normalizado = _normalize_bot_name(bot_nombre) or bot_nombre
    if session.get("autenticado") and not _user_can_access_bot(bot_normalizado):
        return jsonify({"error": "No autorizado para este bot"}), 403

    from_number = _get_bot_number_by_name(bot_normalizado)
    if not from_number:
        return jsonify({"error": f"No se encontró el número del bot para '{bot_normalizado}'"}), 400

    if not twilio_client:
        return jsonify({"error": "Twilio REST no configurado (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)"}), 500

    ok = svc.send_whatsapp_message(twilio_client, from_number, numero, texto)
    if ok:
        try:
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            svc.fb_append_historial(bot_normalizado, numero, {"tipo": "admin", "texto": texto, "hora": ahora})
        except Exception as e:
            print(f"❌ Error guardando admin msg en Firebase: {e}")
        return jsonify({"ok": True})
    return jsonify({"error": "Fallo enviando el mensaje"}), 500

# ─────────────────────────────────────────────────────────────
# API: ON/OFF por conversación
# ─────────────────────────────────────────────────────────────
@app.route("/api/conversation_bot", methods=["POST", "OPTIONS"])
def api_conversation_bot():
    if request.method == "OPTIONS":
        return ("", 204)
    if not session.get("autenticado") and not _bearer_ok(request):
        return jsonify({"error": "No autenticado"}), 401

    data = request.json or {}
    bot_nombre = (data.get("bot") or "").strip()
    numero = (data.get("numero") or "").strip()
    enabled = data.get("enabled", None)
    if enabled is None or not bot_nombre or not numero:
        return jsonify({"error": "Parámetros inválidos (bot, numero, enabled)"}), 400

    bot_normalizado = _normalize_bot_name(bot_nombre) or bot_nombre
    if session.get("autenticado") and not _user_can_access_bot(bot_normalizado):
        return jsonify({"error": "No autorizado para este bot"}), 403

    ok = svc.fb_set_conversation_on(bot_normalizado, numero, bool(enabled))
    return jsonify({"ok": bool(ok), "enabled": bool(enabled)})

# ─────────────────────────────────────────────────────────────
# Push (topic / token / universal / health)
# ─────────────────────────────────────────────────────────────
def _push_common_data(payload: dict) -> dict:
    data = {}
    for k, v in (payload or {}).items():
        if v is None:
            continue
        data[str(k)] = str(v)
    return data

@app.route("/push/health", methods=["GET"])
def push_health():
    return jsonify({"ok": True, "service": "push"})

@app.route("/push/topic", methods=["POST", "OPTIONS"])
@app.route("/api/push/topic", methods=["POST", "OPTIONS"])
def push_topic():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _bearer_ok(request):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    title = (body.get("title") or body.get("titulo") or "").strip()
    body_text = (body.get("body") or body.get("descripcion") or "").strip()
    topic = (body.get("topic") or body.get("segmento") or "todos").strip() or "todos"
    data = _push_common_data({
        "url": body.get("url") or body.get("link") or "",
        "link": body.get("link") or "",
        "screen": body.get("screen") or "",
        "empresaId": body.get("empresaId") or "",
        "categoria": body.get("categoria") or ""
    })
    if not title or not body_text:
        return jsonify({"success": False, "message": "title/body requeridos"}), 400
    try:
        msg_id = svc.fcm_send_topic(title, body_text, topic, data)
        return jsonify({"success": True, "id": msg_id})
    except Exception as e:
        print(f"❌ Error FCM topic: {e}")
        return jsonify({"success": False, "message": "FCM error"}), 500

@app.route("/push/token", methods=["POST", "OPTIONS"])
@app.route("/api/push/token", methods=["POST", "OPTIONS"])
def push_token():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _bearer_ok(request):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    title = (body.get("title") or body.get("titulo") or "").strip()
    body_text = (body.get("body") or body.get("descripcion") or "").strip()
    token = (body.get("token") or "").strip()
    tokens = body.get("tokens") if isinstance(body.get("tokens"), list) else None
    data = _push_common_data({
        "url": body.get("url") or body.get("link") or "",
        "link": body.get("link") or "",
        "screen": body.get("screen") or "",
        "empresaId": body.get("empresaId") or "",
        "categoria": body.get("categoria") or ""
    })
    if not title or not body_text:
        return jsonify({"success": False, "message": "title/body requeridos"}), 400
    try:
        if tokens and len(tokens) > 0:
            resp = svc.fcm_send_tokens(title, body_text, tokens, data)
            return jsonify({"success": True, "mode": "tokens", "sent": resp.success_count, "failed": resp.failure_count})
        elif token:
            msg_id = svc.fcm_send_token(title, body_text, token, data)
            return jsonify({"success": True, "mode": "token", "id": msg_id})
        else:
            return jsonify({"success": False, "message": "token(s) requerido(s)"}), 400
    except Exception as e:
        print(f"❌ Error FCM universal: {e}")
        return jsonify({"success": False, "message": "FCM error"}), 500

@app.route("/push", methods=["POST", "OPTIONS"])
@app.route("/api/push", methods=["POST", "OPTIONS"])
@app.route("/push/send", methods=["POST", "OPTIONS"])
@app.route("/api/push/send", methods=["POST", "OPTIONS"])
def push_universal():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _bearer_ok(request):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    title = (body.get("title") or body.get("titulo") or "").strip()
    body_text = (body.get("body") or body.get("descripcion") or "").strip()
    topic = (body.get("topic") or body.get("segmento") or "").strip()
    token = (body.get("token") or "").strip()
    tokens = body.get("tokens") if isinstance(body.get("tokens"), list) else None
    data = _push_common_data({
        "url": body.get("url") or body.get("link") or "",
        "link": body.get("link") or "",
        "screen": body.get("screen") or "",
        "empresaId": body.get("empresaId") or "",
        "categoria": body.get("categoria") or ""
    })
    if not title or not body_text:
        return jsonify({"success": False, "message": "title/body requeridos"}), 400
    try:
        if topic:
            msg_id = svc.fcm_send_topic(title, body_text, topic or "todos", data)
            return jsonify({"success": True, "mode": "topic", "id": msg_id})
        elif tokens and len(tokens) > 0:
            resp = svc.fcm_send_tokens(title, body_text, tokens, data)
            return jsonify({"success": True, "mode": "tokens", "sent": resp.success_count, "failed": resp.failure_count})
        elif token:
            msg_id = svc.fcm_send_token(title, body_text, token, data)
            return jsonify({"success": True, "mode": "token", "id": msg_id})
        else:
            return jsonify({"success": False, "message": "Falta topic o token(s)"}), 400
    except Exception as e:
        print(f"❌ Error FCM universal: {e}")
        return jsonify({"success": False, "message": "FCM error"}), 500

# ─────────────────────────────────────────────────────────────
# Vistas conversación + API polling
# ─────────────────────────────────────────────────────────────
@app.route("/conversacion_general/<bot>/<numero>")
def chat_general(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return "Bot no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403
    bot_cfg = next((cfg for cfg in bots_config.values() if cfg.get("name")==bot_normalizado), {}) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)
    data = svc.fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto",""), "hora": r.get("hora",""), "tipo": r.get("tipo","user")} for r in historial]
    return render_template("chat.html", numero=numero, mensajes=mensajes, bot=bot_normalizado, bot_data=bot_cfg, company_name=company_name)

@app.route("/conversacion_bot/<bot>/<numero>")
def chat_bot(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return "Bot no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403
    bot_cfg = next((cfg for cfg in bots_config.values() if cfg.get("name")==bot_normalizado), {}) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)
    data = svc.fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto",""), "hora": r.get("hora",""), "tipo": r.get("tipo","user")} for r in historial]
    return render_template("chat_bot.html", numero=numero, mensajes=mensajes, bot=bot_normalizado, bot_data=bot_cfg, company_name=company_name)

@app.route("/api/chat/<bot>/<numero>", methods=["GET","OPTIONS"])
def api_chat(bot, numero):
    if request.method == "OPTIONS":
        return ("", 204)
    if not session.get("autenticado") and not _bearer_ok(request):
        return jsonify({"error": "No autenticado"}), 401
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return jsonify({"error": "Bot no encontrado"}), 404
    if session.get("autenticado") and not _user_can_access_bot(bot_normalizado):
        return jsonify({"error": "No autorizado"}), 403

    since_param = request.args.get("since","").strip()
    try:
        since_ms = int(since_param) if since_param else 0
    except ValueError:
        since_ms = 0

    data = svc.fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    def _hora_to_epoch_ms(hora_str: str) -> int:
        try:
            dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    nuevos = []
    last_ts = since_ms
    for reg in historial:
        ts = _hora_to_epoch_ms(reg.get("hora",""))
        if ts > since_ms:
            nuevos.append({"texto": reg.get("texto",""), "hora": reg.get("hora",""), "tipo": reg.get("tipo","user"), "ts": ts})
        if ts > last_ts:
            last_ts = ts

    if since_ms == 0 and not nuevos and historial:
        for reg in historial:
            ts = _hora_to_epoch_ms(reg.get("hora",""))
            if ts > last_ts:
                last_ts = ts
        nuevos = [{"texto": r.get("texto",""), "hora": r.get("hora",""), "tipo": r.get("tipo","user"), "ts": _hora_to_epoch_ms(r.get("hora",""))} for r in historial]

    bot_enabled = svc.fb_is_conversation_on(bot_normalizado, numero)
    return jsonify({"mensajes": nuevos, "last_ts": last_ts, "bot_enabled": bool(bot_enabled)})

# ─────────────────────────────────────────────────────────────
# Export CSV
# ─────────────────────────────────────────────────────────────
@app.route("/exportar")
def exportar():
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    leads = svc.fb_list_leads_all()
    output = StringIO()
    import csv
    writer = csv.writer(output)
    writer.writerow(["Bot", "Número", "Primer contacto", "Último mensaje", "Última vez", "Mensajes", "Estado", "Notas"])
    for _, datos in leads.items():
        writer.writerow([
            datos.get("bot",""),
            datos.get("numero",""),
            datos.get("first_seen",""),
            datos.get("last_message",""),
            datos.get("last_seen",""),
            datos.get("messages",""),
            datos.get("status",""),
            datos.get("notes","")
        ])
    output.seek(0)
    return send_file(output, mimetype="text/csv", download_name="leads.csv", as_attachment=True)

# ─────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[BOOT] BOOKING_URL_FALLBACK={BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={APP_DOWNLOAD_URL_FALLBACK}")
    app.run(host="0.0.0.0", port=port)
