# services.py — Integraciones y servicios en un solo módulo (sin Flask)
# Contiene: OpenAI, Twilio, Firebase (RTDB + FCM), Bots loader, Instagram OAuth,
# helpers de booking/app links y un wrapper para billing.
# Seguro de importar: no ejecuta inicializaciones con efectos secundarios.

from __future__ import annotations
import os
import json
import glob
import requests
from typing import Any, Dict, List, Optional

# SDKs (import seguros; la app debe inicializarlos en main.py)
from openai import OpenAI
from twilio.rest import Client as TwilioClient
import firebase_admin
from firebase_admin import credentials, db
from firebase_admin import messaging as fcm

# Helpers puros del archivo helpers.py (archivo 1)
from helpers import (
    valid_url, drill_get, canonize_phone,
    apply_style, ensure_question, make_system_message,
    hash_text, next_probe_from_bot
)

# ─────────────────────────────────────────────────────────────
# OpenAI
# ─────────────────────────────────────────────────────────────
def make_openai_client(api_key: str) -> OpenAI:
    """Crea el cliente de OpenAI. No levanta excepciones si api_key está vacío (pero fallará al usar)."""
    return OpenAI(api_key=(api_key or "").strip())

def openai_chat_once(client: OpenAI, bot_cfg: dict, messages: list, model_default="gpt-4o") -> dict:
    """Hace una sola completion y aplica estilo/pregunta según config del bot."""
    model_name = (bot_cfg.get("model") or model_default).strip()
    temperature = float(bot_cfg.get("temperature", 0.6)) if isinstance(bot_cfg.get("temperature", None), (int, float)) else 0.6

    completion = client.chat.completions.create(
        model=model_name,
        temperature=temperature,
        messages=messages
    )
    text = (completion.choices[0].message.content or "").strip()
    text = apply_style(bot_cfg, text)
    must_ask = bool((bot_cfg.get("style") or {}).get("always_question", False))
    text = ensure_question(bot_cfg, text, force_question=must_ask)
    return {"text": text, "completion": completion, "model": model_name}

# ─────────────────────────────────────────────────────────────
# Twilio
# ─────────────────────────────────────────────────────────────
def make_twilio_client(account_sid: str, auth_token: str):
    if not (account_sid and auth_token):
        return None
    try:
        return TwilioClient(account_sid.strip(), auth_token.strip())
    except Exception as e:
        print(f"⚠️ No se pudo inicializar Twilio REST client: {e}")
        return None

def send_whatsapp_message(twilio_client, from_number: str, to_number: str, body: str) -> bool:
    if not twilio_client:
        print("⚠️ Twilio REST no configurado.")
        return False
    try:
        twilio_client.messages.create(from_=from_number, to=to_number, body=body)
        return True
    except Exception as e:
        print(f"❌ Error enviando por Twilio: {e}")
        return False

# ─────────────────────────────────────────────────────────────
# Firebase Admin + RTDB
# ─────────────────────────────────────────────────────────────
def init_firebase(firebase_key_path: str, database_url: str | None):
    """Inicializa Firebase si aún no está; idempotente."""
    if not firebase_admin._apps:
        cred = credentials.Certificate(firebase_key_path)
        if (database_url or "").strip():
            firebase_admin.initialize_app(cred, {'databaseURL': database_url.strip()})
            print(f"[BOOT] Firebase inicializado con RTDB: {database_url}")
        else:
            firebase_admin.initialize_app(cred)
            print("⚠️ Firebase inicializado sin databaseURL (db.reference fallará hasta configurar FIREBASE_DB_URL).")

def _lead_ref(bot_nombre: str, numero: str):
    return db.reference(f"leads/{bot_nombre}/{numero}")

def fb_get_lead(bot_nombre: str, numero: str) -> dict:
    ref = _lead_ref(bot_nombre, numero)
    return ref.get() or {}

def fb_append_historial(bot_nombre: str, numero: str, entrada: dict):
    ref = _lead_ref(bot_nombre, numero)
    lead = ref.get() or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        historial = [historial[k] for k in sorted(historial.keys())]
    historial.append(entrada)
    lead["historial"] = historial
    lead["last_message"] = entrada.get("texto", "")
    lead["last_seen"] = entrada.get("hora", "")
    lead["messages"] = int(lead.get("messages", 0)) + 1
    lead.setdefault("bot", bot_nombre)
    lead.setdefault("numero", numero)
    lead.setdefault("status", "nuevo")
    lead.setdefault("notes", "")
    ref.set(lead)

def fb_list_leads_all() -> dict:
    root = db.reference("leads").get() or {}
    leads = {}
    if not isinstance(root, dict):
        return leads
    for bot_nombre, numeros in root.items():
        if not isinstance(numeros, dict):
            continue
        for numero, data in numeros.items():
            if str(numero).startswith("ig:"):
                continue
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

def fb_list_leads_by_bot(bot_nombre: str) -> dict:
    numeros = db.reference(f"leads/{bot_nombre}").get() or {}
    leads = {}
    if not isinstance(numeros, dict):
        return leads
    for numero, data in numeros.items():
        if str(numero).startswith("ig:"):
            continue
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

def fb_delete_lead(bot_nombre: str, numero: str) -> bool:
    try:
        _lead_ref(bot_nombre, numero).delete()
        return True
    except Exception as e:
        print(f"❌ Error eliminando lead {bot_nombre}/{numero}: {e}")
        return False

def fb_clear_historial(bot_nombre: str, numero: str) -> bool:
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

def fb_is_bot_on(bot_name: str) -> bool:
    try:
        val = db.reference(f"billing/status/{bot_name}").get()
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() == "on"
    except Exception as e:
        print(f"⚠️ Error leyendo status del bot '{bot_name}': {e}")
    return True

def fb_is_conversation_on(bot_nombre: str, numero: str) -> bool:
    """True si la conversación está habilitada (default ON)."""
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

def fb_set_conversation_on(bot_nombre: str, numero: str, enabled: bool) -> bool:
    try:
        ref = _lead_ref(bot_nombre, numero)
        cur = ref.get() or {}
        cur["bot_enabled"] = bool(enabled)
        ref.set(cur)
        return True
    except Exception as e:
        print(f"⚠️ Error guardando bot_enabled en {bot_nombre}/{numero}: {e}")
        return False

# ─────────────────────────────────────────────────────────────
# FCM (Push)
# ─────────────────────────────────────────────────────────────
def _sanitize_data(payload: dict) -> dict:
    data = {}
    for k, v in (payload or {}).items():
        if v is None:
            continue
        data[str(k)] = str(v)
    return data

def fcm_send_topic(title: str, body: str, topic: str = "todos", data: dict | None = None) -> str:
    msg = fcm.Message(
        topic=topic or "todos",
        notification=fcm.Notification(title=title, body=body),
        data=_sanitize_data(data or {})
    )
    return fcm.send(msg)

def fcm_send_token(title: str, body: str, token: str, data: dict | None = None) -> str:
    msg = fcm.Message(
        token=token,
        notification=fcm.Notification(title=title, body=body),
        data=_sanitize_data(data or {})
    )
    return fcm.send(msg)

def fcm_send_tokens(title: str, body: str, tokens: list[str], data: dict | None = None):
    multi = fcm.MulticastMessage(
        tokens=[str(t).strip() for t in tokens if str(t).strip()],
        notification=fcm.Notification(title=title, body=body),
        data=_sanitize_data(data or {})
    )
    return fcm.send_multicast(multi)

# ─────────────────────────────────────────────────────────────
# Bots loader y helpers de links
# ─────────────────────────────────────────────────────────────
def load_bots_folder(base_dir=".") -> dict:
    """Lee ./bots/*.json (formato actual de tu proyecto)."""
    bots = {}
    for path in glob.glob(os.path.join(base_dir, "bots", "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    bots[k] = v
        except Exception as e:
            print(f"⚠️ No se pudo cargar {path}: {e}")
    return bots

def normalize_bot_name(name: str, bots_config: dict):
    for cfg in bots_config.values():
        if isinstance(cfg, dict):
            if cfg.get("name", "").lower() == str(name).lower():
                return cfg.get("name")
    return None

def get_bot_cfg_by_name(name: str, bots_config: dict):
    if not name:
        return None
    for cfg in bots_config.values():
        if isinstance(cfg, dict) and cfg.get("name", "").lower() == name.lower():
            return cfg
    return None

def get_bot_cfg_by_number(to_number: str, bots_config: dict):
    return bots_config.get(to_number)

def get_bot_cfg_by_any_number(to_number: str, bots_config: dict):
    if not to_number:
        if len(bots_config) == 1:
            return list(bots_config.values())[0]
    canon_to = canonize_phone(to_number)
    for key, cfg in bots_config.items():
        if canonize_phone(key) == canon_to:
            return cfg
    return bots_config.get(to_number)

def get_bot_number_by_name(bot_name: str, bots_config: dict) -> str:
    for number_key, cfg in bots_config.items():
        if isinstance(cfg, dict) and cfg.get("name", "").strip().lower() == (bot_name or "").strip().lower():
            return number_key
    return ""

def effective_booking_url(bot_cfg: dict, fallback: str = "") -> str:
    candidates = [
        "links.booking_url",
        "booking_url",
        "calendar_booking_url",
        "google_calendar_booking_url",
        "agenda.booking_url",
    ]
    for p in candidates:
        val = drill_get(bot_cfg or {}, p)
        val = (val or "").strip() if isinstance(val, str) else ""
        if valid_url(val):
            return val
    return fallback if valid_url(fallback) else ""

def effective_app_url(bot_cfg: dict, fallback: str = "") -> str:
    candidates = [
        "links.app_download_url",
        "links.app_url",
        "app_download_url",
        "app_url",
        "download_url",
        "link_app",
    ]
    for p in candidates:
        val = drill_get(bot_cfg or {}, p)
        val = (val or "").strip() if isinstance(val, str) else ""
        if valid_url(val):
            return val
    return fallback if valid_url(fallback) else ""

def compose_with_link(prefix: str, link: str) -> str:
    if valid_url(link):
        return f"{prefix.strip()} {link}".strip()
    return prefix.strip()

# ─────────────────────────────────────────────────────────────
# Instagram OAuth / estado de bot
# ─────────────────────────────────────────────────────────────
def ig_oauth_exchange(code: str, redirect_uri: str, client_id: str, client_secret: str, graph_version: str = "v21.0") -> dict:
    resp = requests.post(
        f"https://graph.facebook.com/{graph_version}/oauth_access_token".replace("_access_", ".oauth_"),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=10,
    )
    return resp.json()

def ig_save_user(user_id: str, access_token: str):
    ref = db.reference(f"instagram_users/{user_id}")
    ref.set({
        "access_token": access_token,
        "user_id": user_id,
    })

def ig_set_enabled(user_id: str, enabled: bool):
    ref = db.reference(f"instagram_users/{user_id}")
    cur = ref.get() or {}
    cur["enabled"] = bool(enabled)
    ref.set(cur)

def ig_get_enabled(user_id: str) -> bool:
    ref = db.reference(f"instagram_users/{user_id}")
    data = ref.get() or {}
    return bool(data.get("enabled", True))

# ─────────────────────────────────────────────────────────────
# Billing wrapper (seguro)
# ─────────────────────────────────────────────────────────────
def record_usage_safe(record_openai_usage_func, bot_name: str, model_name: str, input_tokens: int, output_tokens: int):
    try:
        if callable(record_openai_usage_func):
            record_openai_usage_func(bot_name, model_name, input_tokens, output_tokens)
    except Exception as e:
        print(f"⚠️ No se pudo registrar tokens en billing: {e}")
