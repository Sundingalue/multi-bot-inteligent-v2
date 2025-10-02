# avatar_profiles.py
# Endpoint para exponer los JSON de avatares (ej: /avatars/sundin.json)

import os, json
from flask import Blueprint, jsonify

# 📌 Creamos el blueprint (esto es lo que importa main.py)
bp = Blueprint("avatars", __name__, url_prefix="/avatars")

# 📌 Carpeta donde tienes tus JSON (bots/tarjeta_inteligente)
# Antes: os.path.join(os.path.dirname(__file__), "bots", "tarjeta_inteligente")
# Ahora corregido para subir un nivel
BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bots", "tarjeta_inteligente")

@bp.route("/<slug>.json", methods=["GET"])
def get_avatar(slug):
    """
    Devuelve el JSON del avatar solicitado (ej: /avatars/sundin.json)
    """
    path = os.path.join(BASE_DIR, f"{slug}.json")

    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "Perfil no encontrado", "slug": slug}), 404

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
