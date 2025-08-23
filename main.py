# main.py — core genérico (integrado con config/settings.py)

# 💥💥 CORRECCIÓN FINAL 💥💥
# Usar monkey_patch de eventlet en lugar de gevent
import eventlet
eventlet.monkey_patch()

# -----------------------
# Importaciones estándar
# -----------------------
from flask import Flask, request, session, redirect, url_for, send_file, jsonify, render_template, make_response, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Gather, Connect
from openai import OpenAI
import os, json, time, re, glob, random, hashlib, html, uuid, requests, csv
from io import StringIO
from threading import Thread
from datetime import datetime, timedelta

# 🔹 Twilio REST (para enviar mensajes manuales desde el panel)
from twilio.rest import Client as TwilioClient

# 🔹 Firebase
import firebase_admin
from firebase_admin import credentials, db
# 🔹 NEW: FCM (para notificaciones push)
from firebase_admin import messaging as fcm

# -----------------------
# Configuración centralizada
# -----------------------
# Usamos TODO desde config/settings.py para no duplicar nada en main.py
from config import settings as cfg

# Cliente OpenAI basado en la API key central
client = OpenAI(api_key=cfg.OPENAI_API_KEY)

# Flask app
app = Flask(__name__)
app.secret_key = "supersecreto_sundin_panel_2025"
app.permanent_session_lifetime = timedelta(days=60)
app.config.update({
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_SECURE": False if os.getenv("DEV_HTTP", "").lower() == "true" else True
})

# 🌐 NEW: CORS básico para llamadas desde WordPress / app
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

def _bearer_ok(req) -> bool:
    """Devuelve True si no hay token configurado o si el header Authorization coincide (config central)."""
    if not cfg.API_BEARER_TOKEN:
        return True
    auth = (req.headers.get("Authorization") or "").strip()
    return auth == f"Bearer {cfg.API_BEARER_TOKEN}"

# -----------------------
# Utilidades simples locales (no duplicadas con cfg)
# -----------------------
def _valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

def _split_sentences(text: str):
    parts = re.split(r'(?<=[\.\!\?])\s+', (text or "").strip())
    if len(parts) == 1 and len(text or "") > 280:
        parts = [text[:200].strip(), text[200:].strip()]
    return [p for p in parts if p]

def _apply_style(bot_cfg: dict, text: str) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    short = bool(style.get("short_replies", True))
    max_sents = int(style.get("max_sentences", 2)) if style.get("max_sentences") is not None else 2
    if not text:
        return text
    if short:
        sents = _split_sentences(text)
        text = " ".join(sents[:max_sents]).strip()
    return text

def _next_probe_from_bot(bot_cfg: dict) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    probes = style.get("probes") or []
    probes = [p.strip() for p in probes if isinstance(p, str) and p.strip()]
    if not probes:
        return ""
    return random.choice(probes)

def _ensure_question(bot_cfg: dict, text: str, force_question: bool) -> str:
    txt = re.sub(r"\s+", " ", (text or "")).strip()
    if not force_question:
        return txt
    if "?" in txt:
        return txt
    if not txt.endswith((".", "!", "…")):
        txt += "."
    probe = _next_probe_from_bot(bot_cfg)
    return f"{txt} {probe}".strip() if probe else txt

def _make_system_message(bot_cfg: dict) -> str:
    return (bot_cfg or {}).get("system_prompt", "") or ""

def _canonize_phone(raw: str) -> str:
    return cfg.canonize_phone(raw)

# -----------------------
# Cargar bots (desde config central)
# -----------------------
bots_config = cfg.BOTS_CONFIG
if not bots_config:
    print("⚠️ No se encontraron bots en ./bots/*.json")

# -----------------------
# Inicializar Firebase
# -----------------------
firebase_key_path = cfg.FIREBASE_CRED_PATH
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
    print("❌ FIREBASE_DB_URL no configurado. Define la variable de entorno o crea el Secret File /etc/secrets/FIREBASE_DB_URL con la URL completa de tu RTDB.")

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_path)
    if firebase_db_url:
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_db_url})
        print(f"[BOOT] Firebase inicializado con RTDB: {firebase_db_url}")
    else:
        firebase_admin.initialize_app(cred)
        print("⚠️ Firebase inicializado sin databaseURL (db.reference fallará hasta configurar FIREBASE_DB_URL).")

# -----------------------
# Twilio REST Client
# -----------------------
twilio_client = None
if cfg.TWILIO_ACCOUNT_SID and cfg.TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
        print("[BOOT] Twilio REST client inicializado.")
    except Exception as e:
        print(f"⚠️ No se pudo inicializar Twilio REST client: {e}")
else:
    print("⚠️ TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN no configurados. El envío manual desde panel no funcionará hasta configurarlos.")

# -----------------------
# Blueprints externos
# -----------------------
from billing_api import billing_bp, record_openai_usage
app.register_blueprint(billing_bp, url_prefix="/billing")

from bots.api_mobile import mobile_bp
app.register_blueprint(mobile_bp, url_prefix="/api/mobile")

# -----------------------
# Memorias por sesión (runtime)
# -----------------------
session_history = {}         # clave_sesion -> mensajes para OpenAI (texto)
last_message_time = {}       # clave_sesion -> timestamp último mensaje
follow_up_flags = {}         # clave_sesion -> {"5min": bool, "60min": bool}
agenda_state = {}            # clave_sesion -> {"awaiting_confirm": bool, "status": str, "last_update": ts, "last_link_time": ts, "last_bot_hash": "", "closed": bool}
greeted_state = {}           # clave_sesion -> bool (si ya se saludó)

# ✅ VOICE globals
voice_call_cache = {}
voice_conversation_history = {}

# -----------------------
# Helpers que ahora usan cfg (config central)
# -----------------------
def _normalize_bot_name(name: str):
    return cfg.normalize_bot_name(name)

def _get_bot_cfg_by_name(name: str):
    return cfg.get_bot_cfg_by_name(name)

def _get_bot_cfg_by_number(to_number: str):
    return cfg.find_bot_by_number(to_number)

def _drill_get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def _effective_booking_url(bot_cfg: dict) -> str:
    return cfg.effective_booking_url(bot_cfg)

def _effective_app_url(bot_cfg: dict) -> str:
    return cfg.effective_app_url(bot_cfg)

# -----------------------
# Intenciones
# -----------------------
SCHEDULE_OFFER_PAT = re.compile(
    r"\b(enlace|link|calendar|calendario|agendar|agenda|reservar|reserva|cita|schedule|book|appointment|meeting|call)\b",
    re.IGNORECASE
)
def _wants_link(text: str) -> bool:
    return bool(SCHEDULE_OFFER_PAT.search(text or ""))

def _wants_app_download(text: str) -> bool:
    t = (text or "").lower()
    has_app_word = any(w in t for w in ["app", "aplicación", "aplicacion", "ios", "android", "play store", "app store"])
    has_download_intent = any(w in t for w in ["descargar", "download", "bajar", "instalar", "link", "enlace"])
    return ("descargar app" in t) or ("download app" in t) or (has_app_word and has_download_intent)

def _is_affirmative(texto: str) -> bool:
    if not texto: return False
    t = texto.strip().lower()
    afirm = {"si","sí","ok","okay","dale","va","claro","por favor","hagamoslo","hagámoslo","perfecto","de una","yes","yep","yeah","sure","please"}
    return any(t == a or t.startswith(a + " ") for a in afirm)

def _is_negative(texto: str) -> bool:
    if not texto: return False
    t = re.sub(r'[.,;:!?]+$', '', texto.strip().lower())
    t = re.sub(r'\s+', ' ', t)
    negatives = {"no", "nop", "no gracias", "ahora no", "luego", "después", "despues", "not now"}
    return t in negatives

def _is_scheduled_confirmation(texto: str) -> bool:
    if not texto: return False
    t = texto.lower()
    kws = ["ya agende","ya agendé","agende","agendé","ya programe","ya programé","ya agendado","agendado","confirmé","confirmado","listo","done","booked","i booked","i scheduled","scheduled"]
    return any(k in t for k in kws)

def _is_polite_closure(texto: str) -> bool:
    if not texto: return False
    t = texto.strip().lower()
    cierres = {"gracias","muchas gracias","ok gracias","listo gracias","perfecto gracias","estamos en contacto","por ahora está bien","por ahora esta bien","luego te escribo","luego hablamos","hasta luego","buen día","buen dia","buenas noches","nos vemos","chao","bye","eso es todo","todo bien gracias"}
    return any(t == c or t.startswith(c + " ") for c in cierres)

def _now(): return int(time.time())
def _minutes_since(ts): return (_now() - int(ts or 0)) / 60.0
def _hash_text(s: str) -> str:
    return hashlib.md5((s or "").strip().lower().encode("utf-8")).hexdigest()

def _get_agenda(clave):
    return agenda_state.get(clave) or {"awaiting_confirm": False, "status": "none", "last_update": 0, "last_link_time": 0, "last_bot_hash": "", "closed": False}

def _set_agenda(clave, **kw):
    st = _get_agenda(clave)
    st.update(kw)
    st["last_update"] = _now()
    agenda_state[clave] = st
    return st

def _can_send_link(clave, cooldown_min=10):
    st = _get_agenda(clave)
    if st.get("status") in ("link_sent", "confirmed") and _minutes_since(st.get("last_link_time")) < cooldown_min:
        return False
    return True

# -----------------------
# Firebase: helpers de leads
# -----------------------
def _lead_ref(bot_nombre, numero):
    return db.reference(f"leads/{bot_nombre}/{numero}")

def fb_get_lead(bot_nombre, numero):
    ref = _lead_ref(bot_nombre, numero)
    data = ref.get()
    return data or {}

def fb_append_historial(bot_nombre, numero, entrada):
    ref = _lead_ref(bot_nombre, numero)
    lead = ref.get() or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    historial.append(entrada)
    lead["historial"] = historial
    lead["last_message"] = entrada.get("texto", "")
    lead["last_seen"] = entrada.get("hora", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lead["messages"] = int(lead.get("messages", 0)) + 1
    lead.setdefault("bot", bot_nombre)
    lead.setdefault("numero", numero)
    lead.setdefault("status", "nuevo")
    lead.setdefault("notes", "")
    ref.set(lead)

def fb_list_leads_all():
    root = db.reference("leads").get() or {}
    leads = {}
    if not isinstance(root, dict):
        return leads
    for bot_nombre, numeros in root.items():
        if not isinstance(numeros, dict):
            continue
        for numero, data in numeros.items():
            clave = f"{bot_nombre}|{numero}"
            leads[clave] = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": data.get("first_seen", ""),
                "last_message": data.get("last_message", ""),
                "last_seen": data.get("last_seen", ""),
                "messages": int(data.get("messages", 0)),
                "status": data.get("status", "nuevo"),
                "notes": data.get("notes", "")
            }
    return leads

def fb_list_leads_by_bot(bot_nombre):
    numeros = db.reference(f"leads/{bot_nombre}").get() or {}
    leads = {}
    if not isinstance(numeros, dict):
        return leads
    for numero, data in numeros.items():
        clave = f"{bot_nombre}|{numero}"
        leads[clave] = {
                "bot": bot_nombre,
                "numero": numero,
                "first_seen": data.get("first_seen", ""),
                "last_message": data.get("last_message", ""),
                "last_seen": data.get("last_seen", ""),
                "messages": int(data.get("messages", 0)),
                "status": data.get("status", "nuevo"),
                "notes": data.get("notes", "")
        }
    return leads

# ✅ NUEVO: eliminar lead completo
def fb_delete_lead(bot_nombre, numero):
    try:
        _lead_ref(bot_nombre, numero).delete()
        return True
    except Exception as e:
        print(f"❌ Error eliminando lead {bot_nombre}/{numero}: {e}")
        return False

# ✅ NUEVO: vaciar solo el historial (mantener lead)
def fb_clear_historial(bot_nombre, numero):
    try:
        ref = _lead_ref(bot_nombre, numero)
        lead = ref.get() or {}
        lead["historial"] = []
        lead["messages"] = 0
        lead["last_message"] = ""
        lead["last_seen"] = ""
        lead.setdefault("status", "nuevo")
        lead.setdefault("notes", "")
        lead.setdefault("bot", bot_nombre)
        lead.setdefault("numero", numero)
        ref.set(lead)
        return True
    except Exception as e:
        print(f"❌ Error vaciando historial {bot_nombre}/{numero}: {e}")
        return False

# ✅ Kill-Switch GLOBAL por bot
def fb_is_bot_on(bot_name: str) -> bool:
    try:
        val = db.reference(f"billing/status/{bot_name}").get()
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() == "on"
    except Exception as e:
        print(f"⚠️ Error leyendo status del bot '{bot_name}': {e}")
    return True  # si no hay dato, asumimos ON

# ✅ Kill-Switch por conversación (ON/OFF individual)
def fb_is_conversation_on(bot_nombre: str, numero: str) -> bool:
    """Devuelve True si la conversación tiene el bot activado; si no existe el flag, asume ON."""
    try:
        ref = _lead_ref(bot_nombre, numero)
        lead = ref.get() or {}
        val = lead.get("bot_enabled", None)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("on", "true", "1", "yes", "si", "sí")
    except Exception as e:
        print(f"⚠️ Error leyendo bot_enabled en {bot_nombre}/{numero}: {e}")
    return True

def fb_set_conversation_on(bot_nombre: str, numero: str, enabled: bool):
    try:
        ref = _lead_ref(bot_nombre, numero)
        cur = ref.get() or {}
        cur["bot_enabled"] = bool(enabled)
        ref.set(cur)
        return True
    except Exception as e:
        print(f"⚠️ Error guardando bot_enabled en {bot_nombre}/{numero}: {e}")
        return False

# -----------------------
# Hidratar sesión desde Firebase
# -----------------------
def _hydrate_session_from_firebase(clave_sesion: str, bot_cfg: dict, sender_number: str):
    if clave_sesion in session_history:
        return
    bot_name = (bot_cfg or {}).get("name", "")
    if not bot_name:
        return
    lead = fb_get_lead(bot_name, sender_number) or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    msgs = []
    sysmsg = _make_system_message(bot_cfg)
    if sysmsg:
        msgs.append({"role": "system", "content": sysmsg})

    for reg in historial:
        texto = reg.get("texto", "")
        if not texto:
            continue
        role = "assistant" if (reg.get("tipo", "user") != "user") else "user"
        msgs.append({"role": role, "content": texto})

    if msgs:
        session_history[clave_sesion] = msgs
    if len(historial) > 0:
        greeted_state[clave_sesion] = True
    follow_up_flags[clave_sesion] = {"5min": False, "60min": False}

# -----------------------
# Rutas UI
# -----------------------
def _load_users():
    """
    Prioridad:
    1) Logins definidos en bots/*.json (login, logins y/o auth)
    2) Variables de entorno (LEGACY): USER_*, PASS_*, PANEL_*
    3) Usuario por defecto (admin total)
    """
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

    for cfgbot in bots_config.values():
        if not isinstance(cfgbot, dict):
            continue
        bot_name = (cfgbot.get("name") or "").strip()
        if not bot_name:
            continue

        logins = []
        if isinstance(cfgbot.get("login"), dict):
            logins.append(cfgbot["login"])
        if isinstance(cfgbot.get("logins"), list):
            logins.extend([x for x in cfgbot["logins"] if isinstance(x, dict)])
        if isinstance(cfgbot.get("auth"), dict):
            logins.append(cfgbot["auth"])

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

    # LEGACY por entorno
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

@app.route("/panel-bot/<bot_nombre>")
def panel_exclusivo_bot(bot_nombre):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot_nombre)
    if not bot_normalizado:
        return f"Bot '{bot_nombre}' no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403
    leads_filtrados = fb_list_leads_by_bot(bot_normalizado)
    nombre_comercial = next(
        (config.get("business_name", bot_normalizado)
            for config in bots_config.values()
            if config.get("name") == bot_normalizado),
        bot_normalizado
    )
    return render_template("panel_bot.html", leads=leads_filtrados, bot=bot_normalizado, nombre_comercial=nombre_comercial)

@app.route("/", methods=["GET"])
def home():
    print(f"[BOOT] BOOKING_URL_FALLBACK={cfg.BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={cfg.APP_DOWNLOAD_URL_FALLBACK}")
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
            remember_on = remember_flag in ("on", "1", "true", "yes", "si", "sí")

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
                max_age = 60 * 24 * 60 * 60  # 60 días
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

    leads_todos = fb_list_leads_all()
    bots_disponibles = {}
    for cfgbot in bots_config.values():
        bots_disponibles[cfgbot["name"]] = cfgbot.get("business_name", cfgbot["name"])

    bot_seleccionado = request.args.get("bot")
    if bot_seleccionado:
        bot_norm = _normalize_bot_name(bot_seleccionado) or bot_seleccionado
        leads_filtrados = {k: v for k, v in leads_todos.items() if v.get("bot") == bot_norm}
    else:
        leads_filtrados = leads_todos

    return render_template("panel.html", leads=leads_todos, bots=bots_disponibles, bot_seleccionado=bot_seleccionado)

@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    resp = make_response(redirect(url_for("panel")))
    resp.delete_cookie("remember_login")
    resp.delete_cookie("last_username")
    return resp

# -----------------------
# Guardar/Exportar
# -----------------------
def _hora_to_epoch_ms(hora_str: str) -> int:
    try:
        dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

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
        ref = db.reference(f"leads/{bot_normalizado}/{numero}")
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

@app.route("/exportar")
def exportar():
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    leads = fb_list_leads_all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bot", "Número", "Primer contacto", "Último mensaje", "Última vez", "Mensajes", "Estado", "Notas"])
    for _, datos in leads.items():
        writer.writerow([
            datos.get("bot", ""),
            datos.get("numero", ""),
            datos.get("first_seen", ""),
            datos.get("last_message", ""),
            datos.get("last_seen", ""),
            datos.get("messages", ""),
            datos.get("status", ""),
            datos.get("notes", "")
        ])
    output.seek(0)
    return send_file(output, mimetype="text/csv", download_name="leads.csv", as_attachment=True)

# -----------------------
# Borrar / Vaciar conversaciones (protegido)
# -----------------------
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
    ok = fb_delete_lead(bot_normalizado, numero)
    return jsonify({"ok": ok, "bot": bot_normalizado, "numero": numero})

@app.route("/borrar-conversacion/<bot>/<numero>", methods=["GET"])
def borrar_conversacion_get(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot) or bot
    ok = fb_delete_lead(bot_normalizado, numero)
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
    ok = fb_clear_historial(bot_normalizado, numero)
    return jsonify({"ok": ok, "bot": bot_normalizado, "numero": numero})

@app.route("/vaciar-historial/<bot>/<numero>", methods=["GET"])
def vaciar_historial_get(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot) or bot
    ok = fb_clear_historial(bot_normalizado, numero)
    return redirect(url_for("conversacion_general", bot=bot_normalizado, numero=numero))

@app.route("/api/delete_chat", methods=["POST"])
def api_delete_chat():
    if not session.get("autenticado"):
        return jsonify({"error": "No autenticado"}), 401
    data = request.json or {}
    bot = (data.get("bot") or "").strip()
    numero = (data.get("numero") or "").strip()
    if not bot or not numero:
        return jsonify({"error": "Parámetros inválidos (requiere bot y numero)"}), 400
    bot_normalizado = _normalize_bot_name(bot) or bot
    ok = fb_delete_lead(bot_normalizado, numero)
    return jsonify({"ok": ok, "bot": bot_normalizado, "numero": numero})

# -----------------------
# API manuales y toggles
# -----------------------
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

    # Busca el from_ real en bots_config por nombre
    from_number = ""
    for number_key, cfgbot in bots_config.items():
        if isinstance(cfgbot, dict) and (cfgbot.get("name") or "").strip().lower() == bot_normalizado.strip().lower():
            from_number = number_key
            break
    if not from_number:
        return jsonify({"error": f"No se encontró el número del bot para '{bot_normalizado}'"}), 400

    if not twilio_client:
        return jsonify({"error": "Twilio REST no configurado (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)"}), 500

    try:
        twilio_client.messages.create(
            from_=from_number,
            to=numero,
            body=texto
        )
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fb_append_historial(bot_normalizado, numero, {"tipo": "admin", "texto": texto, "hora": ahora})
        return jsonify({"ok": True})
    except Exception as e:
        print(f"❌ Error enviando manualmente por Twilio: {e}")
        return jsonify({"error": "Fallo enviando el mensaje"}), 500

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

    ok = fb_set_conversation_on(bot_normalizado, numero, bool(enabled))
    return jsonify({"ok": bool(ok), "enabled": bool(enabled)})

# -----------------------
# PUSH/FCM endpoints
# -----------------------
def _push_common_data(payload: dict) -> dict:
    data = {}
    for k, v in (payload or {}).items():
        if v is None:
            continue
        data[str(k)] = str(v)
    return data

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
        "link": body.get("link") or "",
        "screen": body.get("screen") or "",
        "empresaId": body.get("empresaId") or "",
        "categoria": body.get("categoria") or ""
    })

    if not title or not body_text:
        return jsonify({"success": False, "message": "title/body requeridos"}), 400

    try:
        message = fcm.Message(
            topic=topic,
            notification=fcm.Notification(title=title, body=body_text),
            data=data
        )
        msg_id = fcm.send(message)
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
        "link": body.get("link") or "",
        "screen": body.get("screen") or "",
        "empresaId": body.get("empresaId") or "",
        "categoria": body.get("categoria") or ""
    })

    if not title or not body_text:
        return jsonify({"success": False, "message": "title/body requeridos"}), 400

    try:
        if tokens and isinstance(tokens, list) and len(tokens) > 0:
            multi = fcm.MulticastMessage(
                tokens=[str(t) for t in tokens if str(t).strip()],
                notification=fcm.Notification(title=title, body=body_text),
                data=data
            )
            resp = fcm.send_multicast(multi)
            return jsonify({"success": True, "mode": "tokens", "sent": resp.success_count, "failed": resp.failure_count})
        elif token:
            msg = fcm.Message(
                token=token,
                notification=fcm.Notification(title=title, body=body_text),
                data=data
            )
            msg_id = fcm.send(msg)
            return jsonify({"success": True, "mode": "token", "id": msg_id})
        else:
            return jsonify({"success": False, "message": "token(s) requerido(s)"}), 400
    except Exception as e:
        print(f"❌ Error FCM universal: {e}")
        return jsonify({"success": False, "message": "FCM error"}), 500

@app.route("/push/health", methods=["GET"])
def push_health():
    return jsonify({"ok": True, "service": "push"})

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
        "link": body.get("link") or "",
        "screen": body.get("screen") or "",
        "empresaId": body.get("empresaId") or "",
        "categoria": body.get("categoria") or ""
    })

    if not title or not body_text:
        return jsonify({"success": False, "message": "title/body requeridos"}), 400

    try:
        if topic:
            msg = fcm.Message(
                topic=topic or "todos",
                notification=fcm.Notification(title=title, body=body_text),
                data=data
            )
            msg_id = fcm.send(msg)
            return jsonify({"success": True, "mode": "topic", "id": msg_id})
        elif tokens and len(tokens) > 0:
            multi = fcm.MulticastMessage(
                tokens=[str(t) for t in tokens if str(t).strip()],
                notification=fcm.Notification(title=title, body=body_text),
                data=data
            )
            resp = fcm.send_multicast(multi)
            return jsonify({"success": True, "mode": "tokens", "sent": resp.success_count, "failed": resp.failure_count})
        elif token:
            msg = fcm.Message(
                token=token,
                notification=fcm.Notification(title=title, body=body_text),
                data=data
            )
            msg_id = fcm.send(msg)
            return jsonify({"success": True, "mode": "token", "id": msg_id})
        else:
            return jsonify({"success": False, "message": "Falta topic o token(s)"}), 400
    except Exception as e:
        print(f"❌ Error FCM universal: {e}")
        return jsonify({"success": False, "message": "FCM error"}), 500

# -----------------------
# Webhook WhatsApp
# -----------------------
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

def _compose_with_link(prefix: str, link: str) -> str:
    if _valid_url(link):
        return f"{prefix.strip()} {link}".strip()
    return prefix.strip()

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

    _hydrate_session_from_firebase(clave_sesion, bot, sender_number)

    try:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fb_append_historial(bot["name"], sender_number, {"tipo": "user", "texto": incoming_msg, "hora": ahora})
    except Exception as e:
        print(f"❌ Error guardando lead: {e}")

    bot_name = bot.get("name", "")
    if bot_name and not fb_is_bot_on(bot_name):
        return str(MessagingResponse())

    if not fb_is_conversation_on(bot_name, sender_number):
        return str(MessagingResponse())

    response = MessagingResponse()
    msg = response.message()

    if _wants_app_download(incoming_msg):
        url_app = _effective_app_url(bot)
        if url_app:
            links_cfg = bot.get("links") or {}
            app_msg = (links_cfg.get("app_message") or "").strip() if isinstance(links_cfg, dict) else ""
            if app_msg:
                texto = app_msg if app_msg.startswith(("http://", "https://")) else _compose_with_link(app_msg, url_app)
            else:
                texto = _compose_with_link("Aquí tienes:", url_app)
            msg.body(texto)
            _set_agenda(clave_sesion, status="app_link_sent")
            agenda_state[clave_sesion]["closed"] = True
        else:
            msg.body("No tengo enlace de app disponible.")
        last_message_time[clave_sesion] = time.time()
        return str(response)

    if _is_negative(incoming_msg):
        cierre = _compose_with_link("Entendido.", _effective_booking_url(bot))
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    if _is_polite_closure(incoming_msg):
        cierre = bot.get("policies", {}).get("polite_closure_message", "Gracias por contactarnos. ¡Hasta pronto!")
        msg.body(cierre)
        agenda_state.setdefault(clave_sesion, {})["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    st = _get_agenda(clave_sesion)
    agenda_cfg = (bot.get("agenda") or {}) if isinstance(bot, dict) else {}

    confirm_q = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("confirm_question") or ""), flags=re.IGNORECASE)
    decline_msg = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("decline_message") or ""), flags=re.IGNORECASE)
    closing_default = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (_effective_booking_url(bot) or ""), (agenda_cfg.get("closing_message") or ""), flags=re.IGNORECASE)

    if _is_scheduled_confirmation(incoming_msg):
        texto = closing_default or "Agendado."
        msg.body(texto)
        _set_agenda(clave_sesion, status="confirmed")
        agenda_state[clave_sesion]["closed"] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

    if st.get("awaiting_confirm"):
        if _is_affirmative(incoming_msg):
            if _can_send_link(clave_sesion, cooldown_min=10):
                link = _effective_booking_url(bot)
                link_message = (agenda_cfg.get("link_message") or "").strip()
                link_message = re.sub(r"\{\{?\s*GOOGLE_CALENDAR_BOOKING_URL\s*\}?\}", (link or ""), link_message, flags=re.IGNORECASE)
                texto = link_message if link_message else (_compose_with_link("Enlace:", link) if link else "Sin enlace disponible.")
                msg.body(texto)
                _set_agenda(clave_sesion, awaiting_confirm=False, status="link_sent", last_link_time=int(time.time()), last_bot_hash=_hash_text(texto))
                agenda_state[clave_sesion]["closed"] = True
                try:
                    ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": texto, "hora": ahora_bot})
                except Exception as e:
                    print(f"⚠️ No se pudo guardar respuesta AGENDA: {e}")
            else:
                msg.body("Enlace enviado recientemente.")
                _set_agenda(clave_sesion, awaiting_confirm=False)
            last_message_time[clave_sesion] = time.time()
            return str(response)
        elif _is_negative(incoming_msg):
            if decline_msg:
                msg.body(decline_msg)
            _set_agenda(clave_sesion, awaiting_confirm=False)
            agenda_state[clave_sesion]["closed"] = True
            last_message_time[clave_sesion] = time.time()
            return str(response)
        else:
            if confirm_q:
                msg.body(confirm_q)
            last_message_time[clave_sesion] = time.time()
            return str(response)

    if any(k in (incoming_msg or "").lower() for k in (bot.get("agenda", {}).get("keywords", []) or [])):
        if confirm_q:
            msg.body(confirm_q)
        _set_agenda(clave_sesion, awaiting_confirm=True)
        last_message_time[clave_sesion] = time.time()
        return str(response)

    if clave_sesion not in session_history:
        sysmsg = _make_system_message(bot)
        session_history[clave_sesion] = [{"role": "system", "content": sysmsg}] if sysmsg else []
        follow_up_flags[clave_sesion] = {"5min": False, "60min": False}
        greeted_state[clave_sesion] = False

    greeting_text = (bot.get("greeting") or "").strip()
    intro_keywords = (bot.get("intro_keywords") or [])

    if (not greeted_state.get(clave_sesion)) and greeting_text and any(w in incoming_msg.lower() for w in intro_keywords):
        msg.body(greeting_text)
        greeted_state[clave_sesion] = True
        last_message_time[clave_sesion] = time.time()
        return str(response)

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
        respuesta = _apply_style(bot, respuesta)

        style = (bot.get("style") or {})
        must_ask = bool(style.get("always_question", False))
        respuesta = _ensure_question(bot, respuesta, force_question=must_ask)

        st_prev = agenda_state.get(clave_sesion, {})
        if _hash_text(respuesta) == st_prev.get("last_bot_hash"):
            probe = _next_probe_from_bot(bot)
            if probe and probe not in respuesta:
                if not respuesta.endswith((".", "!", "…", "¿", "?")):
                    respuesta += "."
                respuesta = f"{respuesta} {probe}".strip()

        session_history[clave_sesion].append({"role": "assistant", "content": respuesta})
        msg.body(respuesta)
        agenda_state.setdefault(clave_sesion, {})
        agenda_state[clave_sesion]["last_bot_hash"] = _hash_text(respuesta)

        try:
            usage = getattr(completion, "usage", None)
            if usage:
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            else:
                usage_dict = getattr(completion, "to_dict", lambda: {})()
                input_tokens = int(((usage_dict or {}).get("usage") or {}).get("prompt_tokens", 0))
                output_tokens = int(((usage_dict or {}).get("usage") or {}).get("completion_tokens", 0))
            record_openai_usage(bot.get("name", ""), model_name, input_tokens, output_tokens)
        except Exception as e:
            print(f"⚠️ No se pudo registrar tokens en billing: {e}")

        try:
            ahora_bot = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fb_append_historial(bot["name"], sender_number, {"tipo": "bot", "texto": respuesta, "hora": ahora_bot})
        except Exception as e:
            print(f"⚠️ No se pudo guardar respuesta del bot: {e}")

    except Exception as e:
        print(f"❌ Error con GPT: {e}")
        msg.body("Error generando la respuesta.")

    return str(response)

# -----------------------
# 🔊 VOZ con Twilio + OpenAI TTS
# -----------------------
def _voice_get_bot_config(to_number: str) -> dict:
    bot_cfg = _get_bot_cfg_by_number(to_number)
    if not bot_cfg:
        return None

    config = {
        "bot_name": bot_cfg.get("name", "Unknown"),
        "model": bot_cfg.get("model", "gpt-4o"),
        "system_prompt": bot_cfg.get("system_prompt", "Eres un asistente de voz amable y natural. Habla con una voz humana."),
        "voice_greeting": bot_cfg.get("voice_greeting", f"Hola, soy el asistente de {bot_cfg.get('business_name', bot_cfg.get('name', 'el bot'))}. ¿Cómo puedo ayudarte?"),
        "openai_voice": cfg.get_voice(bot_cfg),  # ✅ toma prioridad de entorno / JSON
    }
    return config

def _generate_and_store_greeting(call_sid: str, bot_config: dict):
    try:
        greeting_text = bot_config["voice_greeting"]
        openai_voice = bot_config["openai_voice"]

        temp_dir = "/tmp"
        os.makedirs(temp_dir, exist_ok=True)
        greeting_file_name = f"{call_sid}_greeting.mp3"
        greeting_file_path = os.path.join(temp_dir, greeting_file_name)

        if not os.path.exists(greeting_file_path):
            tts_response = client.audio.speech.create(
                model="tts-1",
                voice=openai_voice,
                input=greeting_text,
                speed=1.0
            )
            tts_response.stream_to_file(greeting_file_path)

        voice_call_cache[f"{call_sid}_greeting"] = {"audio_file_name": greeting_file_name}

    except Exception as e:
        print(f"❌ Error en el hilo al generar el saludo para {call_sid}: {e}")
        voice_call_cache[f"{call_sid}_greeting"] = {"audio_file_name": ""}

def _thread_target_chat(call_sid, user_speech, bot_config):
    try:
        if call_sid not in voice_conversation_history:
            voice_conversation_history[call_sid] = [{"role": "system", "content": bot_config["system_prompt"]}]

        voice_conversation_history[call_sid].append({"role": "user", "content": user_speech})

        chat_completion = client.chat.completions.create(
            model=bot_config["model"],
            messages=voice_conversation_history[call_sid]
        )
        bot_response_text = chat_completion.choices[0].message.content.strip()

        voice_conversation_history[call_sid].append({"role": "assistant", "content": bot_response_text})

        temp_dir = "/tmp"
        os.makedirs(temp_dir, exist_ok=True)
        audio_file_name = f"{call_sid}_{uuid.uuid4().hex[:8]}.mp3"
        audio_file_path = os.path.join(temp_dir, audio_file_name)

        tts_response = client.audio.speech.create(
            model="tts-1",
            voice=bot_config["openai_voice"],
            input=bot_response_text,
            speed=1.0
        )
        tts_response.stream_to_file(audio_file_path)

        voice_call_cache[call_sid] = {"audio_file_name": audio_file_name}

    except Exception as e:
        print(f"❌ Error en el hilo de chat con OpenAI: {e}")
        voice_call_cache[call_sid] = {"audio_file_name": ""}

def _wait_for_audio(call_sid, cache_key, timeout=15):
    start_time = time.time()
    while cache_key not in voice_call_cache and (time.time() - start_time) < timeout:
        time.sleep(0.1)

    if cache_key in voice_call_cache:
        return voice_call_cache[cache_key].get("audio_file_name", "")
    return ""

@app.route("/voice", methods=["POST"])
def voice_webhook():
    to_number = request.values.get("To")
    call_sid = request.values.get("CallSid")

    bot_config = _voice_get_bot_config(to_number)
    if not bot_config:
        resp = VoiceResponse()
        resp.say("Lo siento, no hay un bot configurado para este número de voz.")
        return str(resp)

    print(f"[VOICE] Llamada a '{bot_config['bot_name']}' iniciada.")

    voice_call_cache[f"{call_sid}_greeting"] = {"audio_file_name": "placeholder"}
    Thread(target=_generate_and_store_greeting, args=(call_sid, bot_config), daemon=True).start()

    resp = VoiceResponse()
    gather = Gather(
        input="speech",
        action=url_for('voice_gather', _external=True),
        speech_model="phone_call",
        speech_timeout="auto",
        language="es-ES"
    )
    resp.append(gather)
    resp.redirect(url_for('voice_gather', _external=True))
    return str(resp)

@app.route("/voice-gather", methods=["POST"])
def voice_gather():
    resp = VoiceResponse()
    user_speech = request.values.get("SpeechResult", "").strip()
    call_sid = request.values.get("CallSid")
    to_number = request.values.get("To")

    bot_config = _voice_get_bot_config(to_number)
    if not bot_config:
        resp.say("Lo siento, hubo un problema técnico.")
        return str(resp)

    if user_speech:
        print(f"[VOICE] Mensaje del usuario: {user_speech}")
        _thread_target_chat(call_sid, user_speech, bot_config)
        audio_file_name = _wait_for_audio(call_sid, call_sid, timeout=15)

        if audio_file_name:
            print(f"[VOICE] Reproduciendo respuesta del bot desde: {audio_file_name}")
            resp.play(f"{request.host_url}voice-audio/{audio_file_name}")
        else:
            print(f"❌ Error: No se pudo obtener la URL de audio a tiempo.")
            resp.say("Lo siento, estoy teniendo un problema y no pude responder.")

    else:
        greeting_file_name = _wait_for_audio(call_sid, f"{call_sid}_greeting")
        if greeting_file_name:
            resp.play(f"{request.host_url}voice-audio/{greeting_file_name}")
        else:
            resp.say("Lo siento, no pude generar el saludo.")

    gather = Gather(
        input="speech",
        action=url_for('voice_gather', _external=True),
        speech_model="phone_call",
        speech_timeout="auto",
        language="es-ES"
    )
    resp.append(gather)

    return str(resp)

@app.route("/voice-audio/<filename>", methods=["GET"])
def voice_audio(filename):
    file_path = os.path.join("/tmp", filename)
    if os.path.exists(file_path):
        return send_file(file_path, mimetype="audio/mpeg", as_attachment=False)
    else:
        print(f"❌ Error 404: Archivo no encontrado en {file_path}")
        return "Archivo no encontrado", 404

# -----------------------
# Vistas de conversación
# -----------------------
@app.route("/conversacion_general/<bot>/<numero>")
def chat_general(bot, numero):
    if not session.get("autenticado"):
        return redirect(url_for("panel"))
    bot_normalizado = _normalize_bot_name(bot)
    if not bot_normalizado:
        return "Bot no encontrado", 404
    if not _user_can_access_bot(bot_normalizado):
        return "No autorizado para este bot", 403

    bot_cfg = _get_bot_cfg_by_name(bot_normalizado) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto", ""), "hora": r.get("hora", ""), "tipo": r.get("tipo", "user")} for r in historial]

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

    bot_cfg = _get_bot_cfg_by_name(bot_normalizado) or {}
    company_name = bot_cfg.get("business_name", bot_normalizado)

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    mensajes = [{"texto": r.get("texto", ""), "hora": r.get("hora", ""), "tipo": r.get("tipo", "user")} for r in historial]

    return render_template("chat_bot.html", numero=numero, mensajes=mensajes, bot=bot_normalizado, bot_data=bot_cfg, company_name=company_name)

# -----------------------
# API de polling
# -----------------------
@app.route("/api/chat/<bot>/<numero>", methods=["GET", "OPTIONS"])
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

    since_param = request.args.get("since", "").strip()
    try:
        since_ms = int(since_param) if since_param else 0
    except ValueError:
        since_ms = 0

    data = fb_get_lead(bot_normalizado, numero)
    historial = data.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]

    nuevos = []
    last_ts = since_ms
    for reg in historial:
        ts = _hora_to_epoch_ms(reg.get("hora", ""))
        if ts > since_ms:
            nuevos.append({"texto": reg.get("texto", ""), "hora": reg.get("hora", ""), "tipo": reg.get("tipo", "user"), "ts": ts})
        if ts > last_ts:
            last_ts = ts

    if since_ms == 0 and not nuevos and historial:
        for reg in historial:
            ts = _hora_to_epoch_ms(reg.get("hora", ""))
            if ts > last_ts:
                last_ts = ts
        nuevos = [{"texto": reg.get("texto", ""), "hora": reg.get("hora", ""), "tipo": reg.get("tipo", "user"), "ts": _hora_to_epoch_ms(reg.get("hora", ""))} for reg in historial]

    bot_enabled = fb_is_conversation_on(bot_normalizado, numero)

    return jsonify({"mensajes": nuevos, "last_ts": last_ts, "bot_enabled": bool(bot_enabled)})

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[BOOT] BOOKING_URL_FALLBACK={cfg.BOOKING_URL_FALLBACK}")
    print(f"[BOOT] APP_DOWNLOAD_URL_FALLBACK={cfg.APP_DOWNLOAD_URL_FALLBACK}")
    app.run(host="0.0.0.0", port=port)
