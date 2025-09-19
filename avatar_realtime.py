# routes/realtime_session.py
# Endpoint para crear una sesión efímera de voz en tiempo real (OpenAI Realtime)
# Se registra como Blueprint en main.py. No cambia tu Start Command.

import os
import requests
from flask import Blueprint, jsonify, current_app

bp = Blueprint("realtime", __name__, url_prefix="/realtime")

# Opciones por defecto (puedes cambiarlas por variables de entorno si quieres)
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "verse")

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

    payload = {
        "model": REALTIME_MODEL,
        "voice": REALTIME_VOICE,
        "modalities": ["audio", "text"],
        "instructions": (
            "Eres el avatar oficial de Sundin Galue (Revista In Houston Texas). "
            "Tono: profesional, cálido y directo; humor inteligente cuando suma. "
            "Funciones: 1) Bienvenida breve. 2) Explicar revista, alcance y beneficios. "
            "3) Orientar sobre planes y próximos pasos (WhatsApp / agendar). "
            "4) Si no desea hablar largo, ofrecer enviar media kit al email. "
            "5) No pidas instalar apps; mantén la interacción simple. "
            "Si el usuario dice 'hablar después', despídete cordialmente."
        )
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
