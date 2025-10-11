# state.py — Estados en memoria y pequeñas utilidades de sesión
# Objetivo: centralizar los diccionarios globales y helpers de agenda/sesión
# para adelgazar main.py sin cambiar la lógica.

from __future__ import annotations
from typing import Dict, Any, Callable
from helpers import now_ts, minutes_since, hash_text

# ─────────────────────────────────────────────────────────────
# Estados en memoria (idénticos a los que tienes en main.py)
# ─────────────────────────────────────────────────────────────
# Mensajería de texto (WhatsApp/SMS)
session_history: Dict[str, list] = {}       # clave_sesion -> mensajes para OpenAI (texto)
last_message_time: Dict[str, int] = {}      # clave_sesion -> timestamp último mensaje (segundos)
follow_up_flags: Dict[str, Dict[str, bool]] = {}  # clave_sesion -> {"5min": bool, "60min": bool}
agenda_state: Dict[str, Dict[str, Any]] = {}       # clave_sesion -> estado de agenda/probing
greeted_state: Dict[str, bool] = {}         # clave_sesion -> si ya se saludó

# Voz / llamadas
voice_call_cache: Dict[str, Any] = {}
voice_conversation_history: Dict[str, list] = {}


# ─────────────────────────────────────────────────────────────
# Helpers de agenda/sesión (misma semántica que en tu main.py)
# ─────────────────────────────────────────────────────────────
def _now() -> int:
    return now_ts()

def _default_agenda() -> Dict[str, Any]:
    return {
        "awaiting_confirm": False,
        "status": "none",
        "last_update": 0,
        "last_link_time": 0,
        "last_bot_hash": "",
        "closed": False,
    }

def get_agenda(clave_sesion: str) -> Dict[str, Any]:
    return agenda_state.get(clave_sesion) or _default_agenda()

def set_agenda(clave_sesion: str, **kw) -> Dict[str, Any]:
    st = get_agenda(clave_sesion)
    st.update(kw)
    st["last_update"] = _now()
    agenda_state[clave_sesion] = st
    return st

def can_send_link(clave_sesion: str, cooldown_min: int = 10) -> bool:
    st = get_agenda(clave_sesion)
    if st.get("status") in ("link_sent", "confirmed") and minutes_since(st.get("last_link_time")) < cooldown_min:
        return False
    return True


# ─────────────────────────────────────────────────────────────
# Hidratación desde Firebase (inyección de dependencias)
# Evitamos import circulares: pasas las funciones cuando la llames.
# fb_get_lead_func: Callable[[str, str], dict]
# make_system_message_func: Callable[[dict], str]
# ─────────────────────────────────────────────────────────────
def hydrate_session_from_firebase(
    clave_sesion: str,
    bot_cfg: dict,
    sender_number: str,
    *,
    fb_get_lead_func: Callable[[str, str], dict],
    make_system_message_func: Callable[[dict], str],
) -> None:
    """
    Si la sesión no existe en memoria, la reconstruye leyendo el historial
    del lead en Firebase y agregando el system_prompt del bot.
    """
    if clave_sesion in session_history:
        return

    bot_name = (bot_cfg or {}).get("name", "")
    if not bot_name:
        return

    lead = fb_get_lead_func(bot_name, sender_number) or {}
    historial = lead.get("historial", [])
    if isinstance(historial, dict):
        # Normaliza el formato dict {idx: {...}} -> list ordenada
        historial = [historial[k] for k in sorted(historial.keys())]

    msgs = []
    sysmsg = make_system_message_func(bot_cfg)
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

    # Si ya había historial previo, marcamos como saludado
    if len(historial) > 0:
        greeted_state[clave_sesion] = True

    # Flags de seguimiento por defecto
    follow_up_flags[clave_sesion] = {"5min": False, "60min": False}


# ─────────────────────────────────────────────────────────────
# Utilidad para registrar el último hash de respuesta del bot
# (para evitar repetir exactamente el mismo texto)
# ─────────────────────────────────────────────────────────────
def set_last_bot_hash(clave_sesion: str, text: str) -> None:
    st = agenda_state.setdefault(clave_sesion, _default_agenda())
    st["last_bot_hash"] = hash_text(text)

def get_last_bot_hash(clave_sesion: str) -> str:
    return (agenda_state.get(clave_sesion) or {}).get("last_bot_hash", "")
