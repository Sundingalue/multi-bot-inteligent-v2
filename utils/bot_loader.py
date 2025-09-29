# utils/bot_loader.py
# Cargador de configuración de “tarjeta inteligente” por cliente (JSON)
import json
import os
from typing import Any, Dict

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

def resolve_bot_path(bot_id: str) -> str:
    """
    Convierte un id como 'sundin' en la ruta absoluta del JSON:
    .../bots/tarjeta_inteligente/sundin.json
    """
    fname = f"{bot_id}.json" if not bot_id.endswith(".json") else bot_id
    path = _safe_join(BOTS_DIR, fname)

    # Evita path traversal: la ruta normalizada debe empezar por BOTS_DIR
    if not path.startswith(BOTS_DIR):
        raise BotConfigNotFound("Ruta de bot inválida.")

    if not os.path.isfile(path):
        raise BotConfigNotFound(f"No existe el JSON de bot: {path}")
    return path

def load_bot(bot_id: str) -> Dict[str, Any]:
    """
    Carga y devuelve el dict del bot. Añade defaults seguros si faltan campos.
    """
    path = resolve_bot_path(bot_id)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Defaults de seguridad
    data.setdefault("instructions", {})
    data.setdefault("realtime", {})
    data["realtime"].setdefault("modalities", ["audio", "text"])

    return data
