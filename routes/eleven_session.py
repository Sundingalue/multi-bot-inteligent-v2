# routes/eleven_session.py
from flask import Blueprint, jsonify, request, current_app
import os

bp = Blueprint("eleven_session", __name__)

@bp.route("/realtime/session", methods=["POST"])
def realtime_session():
    """
    Devuelve un “token efímero” para que el front agregue:
      Authorization: Bearer <token>
    En nuestro caso usamos API_BEARER_TOKEN (ya presente en main.py)
    para proteger /eleven/webrtc. Mantengo compat con tu plugin:
      - session.client_secret.value
      - jwt (top-level)
      - session.model (no bloquea nada)
    """
    api_bearer = os.environ.get("API_BEARER_TOKEN", "").strip()
    # Si no hay token configurado, igual devolvemos algo (modo dev)
    if not api_bearer:
        api_bearer = "dev-token-unsafe"

    # Puedes fijarlo en JSON/bot si quieres, pero aquí damos un default
    model = request.args.get("model") or "eleven_multilingual_v2"

    return jsonify({
        "ok": True,
        "session": {
            "client_secret": { "value": api_bearer },  # compat OpenAI
            "model": model,
            # Opcional: puedes incluir otros hints si quieres
        },
        "jwt": api_bearer  # compat por si el front lee jwt directo
    })
