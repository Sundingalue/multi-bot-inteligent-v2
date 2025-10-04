# utils/bot_loader.py
# Cargador de configuración de “tarjeta inteligente” por cliente (JSON)
import json
import os
import glob
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # .../multi-bot-inteligente/utils
PROJECT_ROOT = os.path.dirname(BASE_DIR)                # .../multi-bot-inteligente
BOTS_DIR = os.path.join(PROJECT_ROOT, "bots", "tarjeta_inteligente")

class BotConfigNotFound(Exception):
    """Se lanza cuando no se encuentra o no es válida la config del bot."""
    pass

def _safe_join(*parts: str) -> str:
    """
    Une rutas y normaliza para evitar escapes fuera del directorio permitido.
    """
    path = os.path.join(*parts)
    return os.path.normpath(path)

# ─────────────────────────────────────────────────────────────
# Normalización de identificadores (número/slug)
# ─────────────────────────────────────────────────────────────
def _only_digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())

def _e164(s: str) -> Optional[str]:
    """
    Devuelve el número en formato +1XXXXXXXXXX si es de 10 u 11 dígitos comunes de US.
    Si no puede, devuelve None.
    """
    digits = _only_digits(s or "")
    if not digits:
        return None
    # 10 dígitos -> asume US y antepone 1
    if len(digits) == 10:
        digits = "1" + digits
    # 11 dígitos empezando por 1 -> válido
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    # Si ya trae más/menos, lo dejamos pasar como None para no falsear
    return None

def _normalize_keys(bot_id: str):
    """
    Dado un bot_id, genera variantes equivalentes que podríamos encontrar
    como nombre de archivo o como clave dentro de un JSON tipo “bundle”.
    """
    bot_id = (bot_id or "").strip()
    keys = []

    # tal cual
    if bot_id:
        keys.append(bot_id)

    # quitar prefijo whatsapp:
    if bot_id.lower().startswith("whatsapp:"):
        no_prefix = bot_id[len("whatsapp:"):]
        keys.append(no_prefix)
    else:
        no_prefix = bot_id

    # e164 (con +1…)
    e = _e164(no_prefix)
    if e:
        keys.append(e)
        keys.append(f"whatsapp:{e}")

    # solo dígitos (por si alguien guarda la clave así)
    dig = _only_digits(no_prefix)
    if dig:
        keys.append(dig)

    # deduplicar conservando orden
    seen = set()
    out = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out

# ─────────────────────────────────────────────────────────────
# Resolución por archivo directo (comportamiento original + variantes)
# ─────────────────────────────────────────────────────────────
def _candidate_filenames(bot_id: str):
    """
    Dado un bot_id devuelve posibles nombres de archivo a probar:
    - {id}.json
    - {variante}.json (e164, sin prefijo, etc.)
    """
    variants = _normalize_keys(bot_id)
    return [f"{v}.json" for v in variants]

def resolve_bot_path(bot_id: str) -> str:
    """
    Convierte un id en la ruta absoluta del JSON si existe archivo directo.
    Intenta múltiples variantes, pero NO busca dentro de bundles aquí.
    """
    for fname in _candidate_filenames(bot_id):
        path = _safe_join(BOTS_DIR, fname)
        # Evita path traversal
        if not path.startswith(BOTS_DIR):
            continue
        if os.path.isfile(path):
            return path
    raise BotConfigNotFound(f"No existe archivo JSON directo para: {bot_id}")

# ─────────────────────────────────────────────────────────────
# Búsqueda dentro de bundles (archivos que contienen varios bots en un dict)
# ─────────────────────────────────────────────────────────────
def _search_in_bundles(bot_id: str) -> Optional[Dict[str, Any]]:
    """
    Recorre todos los .json en BOTS_DIR, y si alguno es un dict “bundle”
    con una clave igual a alguna variante del bot_id, devuelve ese sub-dict.
    """
    keys = _normalize_keys(bot_id)
    for path in glob.glob(os.path.join(BOTS_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        if isinstance(data, dict):
            # match exacto por cualquiera de las variantes
            for k in keys:
                if k in data and isinstance(data[k], dict):
                    return data[k]
    return None

# ─────────────────────────────────────────────────────────────
# Carga final (archivo directo -> bundle -> error)
# ─────────────────────────────────────────────────────────────
def load_bot(bot_id: str) -> Dict[str, Any]:
    """
    Carga y devuelve el dict del bot. Añade defaults seguros si faltan campos.
    Compatibilidad:
      1) Archivo directo: bots/tarjeta_inteligente/{id}.json
      2) Bundle: busca una clave que coincida dentro de cualquier .json del directorio.
    """
    # 1) Intento por archivo directo (comportamiento original + variantes)
    try:
        path = resolve_bot_path(bot_id)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise BotConfigNotFound(f"JSON inválido en {path}")
    except BotConfigNotFound:
        # 2) Intento por bundle (sin romper lo existente)
        data = _search_in_bundles(bot_id)
        if data is None:
            # Para depurar mejor, mostramos todas las variantes que probamos
            tried = ", ".join(_normalize_keys(bot_id))
            raise BotConfigNotFound(
                f"No se encontró configuración para '{bot_id}'. "
                f"Variantes probadas (archivo o clave en bundle): [{tried}]"
            )

    # Defaults de seguridad (sin tocar tu estructura)
    data.setdefault("instructions", {})
    data.setdefault("realtime", {})
    data["realtime"].setdefault("modalities", ["audio", "text"])

    # Si no trae slug/whatsapp_number y podemos inferirlo, no hace daño añadirlo
    if "slug" not in data:
        # usa la primera variante “estable” como slug
        variants = _normalize_keys(bot_id)
        if variants:
            data["slug"] = variants[-1]
    if "whatsapp_number" not in data:
        # si hay una variante tipo whatsapp:+E164 la preferimos
        variants = _normalize_keys(bot_id)
        wa = next((v for v in variants if v.startswith("whatsapp:+")), None)
        if wa:
            data["whatsapp_number"] = wa

    return data
