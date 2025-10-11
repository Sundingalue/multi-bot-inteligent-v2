# helpers.py — utilidades puras (sin Flask), listas para importar desde main.py
from __future__ import annotations
import re
import time
import hashlib
from datetime import datetime
from typing import List

# ───────── URL helpers ─────────
def valid_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))

def drill_get(d: dict, path: str):
    cur = d
    for k in (path or "").split("."):
        if isinstance(cur, dict) and (k in cur):
            cur = cur[k]
        else:
            return None
    return cur

# ───────── Tiempo / hash ─────────
def now_ts() -> int:
    return int(time.time())

def minutes_since(ts) -> float:
    try:
        return (now_ts() - int(ts or 0)) / 60.0
    except Exception:
        return 0.0

def hora_to_epoch_ms(hora_str: str) -> int:
    """Convierte 'YYYY-MM-DD HH:MM:SS' a epoch ms. Devuelve 0 si falla."""
    try:
        dt = datetime.strptime(hora_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def hash_text(s: str) -> str:
    return hashlib.md5((s or "").strip().lower().encode("utf-8")).hexdigest()

# ───────── Texto / estilo ─────────
def split_sentences(text: str) -> List[str]:
    parts = re.split(r'(?<=[\.\!\?])\s+', (text or "").strip())
    if len(parts) == 1 and len(text or "") > 280:
        parts = [text[:200].strip(), text[200:].strip()]
    return [p for p in parts if p]

def apply_style(bot_cfg: dict, text: str) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    short = bool(style.get("short_replies", True))
    max_sents = int(style.get("max_sentences", 2)) if style.get("max_sentences") is not None else 2
    if not text:
        return text
    if short:
        sents = split_sentences(text)
        text = " ".join(sents[:max_sents]).strip()
    return text

def next_probe_from_bot(bot_cfg: dict) -> str:
    style = (bot_cfg or {}).get("style", {}) or {}
    probes = style.get("probes") or []
    probes = [p.strip() for p in probes if isinstance(p, str) and p.strip()]
    if not probes:
        return ""
    import random
    return random.choice(probes)

def ensure_question(bot_cfg: dict, text: str, force_question: bool) -> str:
    txt = re.sub(r"\s+", " ", (text or "")).strip()
    if not force_question:
        return txt
    if "?" in txt:
        return txt
    if not txt.endswith((".", "!", "…")):
        txt += "."
    probe = next_probe_from_bot(bot_cfg)
    return f"{txt} {probe}".strip() if probe else txt

def make_system_message(bot_cfg: dict) -> str:
    return (bot_cfg or {}).get("system_prompt") or (bot_cfg or {}).get("prompt") or ""

# ───────── Teléfonos ─────────
def canonize_phone(raw: str) -> str:
    s = str(raw or "").strip()
    for p in ("whatsapp:", "tel:", "sip:", "client:"):
        if s.startswith(p):
            s = s[len(p):]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits

# ───────── Intents ─────────
SCHEDULE_OFFER_PAT = re.compile(
    r"\b(enlace|link|calendar|calendario|agendar|agenda|reservar|reserva|cita|schedule|book|appointment|meeting|call)\b",
    re.IGNORECASE
)

def wants_link(text: str) -> bool:
    return bool(SCHEDULE_OFFER_PAT.search(text or ""))

def wants_app_download(text: str) -> bool:
    t = (text or "").lower()
    has_app_word = any(w in t for w in ["app", "aplicación", "aplicacion", "ios", "android", "play store", "app store"])
    has_download_intent = any(w in t for w in ["descargar", "download", "bajar", "instalar", "link", "enlace"])
    return ("descargar app" in t) or ("download app" in t) or (has_app_word and has_download_intent)

def is_affirmative(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    afirm = {"si","sí","ok","okay","dale","va","claro","por favor","hagamoslo","hagámoslo","perfecto","de una","yes","yep","yeah","sure","please"}
    return any(t == a or t.startswith(a + " ") for a in afirm)

def is_negative(texto: str) -> bool:
    if not texto:
        return False
    t = re.sub(r'[.,;:!?]+$', '', texto.strip().lower())
    t = re.sub(r'\s+', ' ', t)
    negatives = {"no", "nop", "no gracias", "ahora no", "luego", "después", "despues", "not now"}
    return t in negatives

def is_scheduled_confirmation(texto: str) -> bool:
    if not texto:
        return False
    t = texto.lower()
    kws = ["ya agende","ya agendé","agende","agendé","ya programe","ya programé","ya agendado","agendado","confirmé","confirmado","listo","done","booked","i booked","i scheduled","scheduled"]
    return any(k in t for k in kws)

def is_polite_closure(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    cierres = {"gracias","muchas gracias","ok gracias","listo gracias","perfecto gracias","estamos en contacto","por ahora está bien","por ahora esta bien","luego te escribo","luego hablamos","hasta luego","buen día","buen dia","buenas noches","nos vemos","chao","bye","eso es todo","todo bien gracias"}
    return any(t == c or t.startswith(c + " ") for c in cierres)
