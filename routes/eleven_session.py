# routes/eleven_session.py
from flask import Blueprint, request, jsonify
import os, time

bp = Blueprint("eleven_session", __name__, url_prefix="/realtime")

@bp.route("/session", methods=["POST"])
def eleven_session():
    """
    Devuelve un portador utilizable por el front:
    - jwt / token: el que Eleven acepta como Bearer o xi-api-key (aquí usamos ELEVEN_API_KEY fija).
    - model: para que el front lo conozca si quieres.
    """
    # En producción genera un JWT efímero; para simplificar usamos la API key fija:
    token = (os.getenv("ELEVEN_API_KEY") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "ELEVEN_API_KEY missing"}), 500

    model = os.getenv("ELEVEN_DEFAULT_MODEL", "eleven_multilingual_v2")
    return jsonify({
        "ok": True,
        "session": {
            "jwt": token,
            "model": model,
            "issued_at": int(time.time()),
            "expires_in": 3600
        }
    })
