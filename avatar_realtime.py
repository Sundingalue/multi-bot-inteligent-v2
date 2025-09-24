# routes/realtime_session.py
# Endpoint para crear una sesión efímera de voz en tiempo real (OpenAI Realtime)
# Se registra como Blueprint en main.py. No cambia tu Start Command.

import os
import requests
from flask import Blueprint, jsonify, current_app

bp = Blueprint("realtime", __name__, url_prefix="/realtime")

# Opciones por defecto (puedes cambiarlas por variables de entorno si quieres)
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "cedar")  # puedes cambiar por otra en tus env vars

@bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "realtime", "model": REALTIME_MODEL})

@bp.post("/session")
def create_session():
    """
    Crea una sesión efímera con OpenAI Realtime.
    Usa la misma OPENAI_API_KEY que ya tienes configurada en tu proyecto.
    """
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY no configurada"}), 500

    # ⚠️ IMPORTANTE:
    # Antes este backend forzaba "Bienvenida" y "media kit".
    # Ahora dejamos instrucciones neutrales y estrictas para NO decir esas frases.
    instructions = (
        "Habla en español de México con voz masculina natural y profesional. "
        "Tu nombre es 'Sundin Galu\u00E9' (pronuncia ga-lu-É). "
        "La marca se llama 'Revista In Houston Tecsas' (es la revista, no el nombre de la persona). "
        "NO digas 'Bienvenido', 'Bienvenida' ni 'Bienvenidos' en ningún caso salvo que el cliente lo indique textualmente. "
        "NO menciones 'media kit' a menos que el usuario lo pida explícitamente. Si lo pide, responde que actualmente no está disponible. "
        "No generes saludos automáticos por tu cuenta; espera las instrucciones del cliente (el front-end enviará el saludo exacto). "
        "Siempre tu saludo inicial es ' Hola, soy 'Sundin Galu\u00E9'. y pregúntame lo que necesites saber de nuestra revista 'In Houston Tecsas"
    )

    payload = {
        "model": REALTIME_MODEL,
        "voice": REALTIME_VOICE,
        "modalities": ["audio", "text"],
        "instructions": instructions
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )
        if r.status_code >= 400:
            return jsonify({"ok": False, "error": "OpenAI Realtime error", "detail": r.text}), 502

        return jsonify({"ok": True, "session": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": "Excepción creando sesión", "detail": str(e)}), 500
