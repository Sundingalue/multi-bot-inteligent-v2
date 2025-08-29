# instagram_api_multi.py
# ===========================================
# API Multiusuario para Instagram OAuth + ON/OFF
# ===========================================

from flask import Blueprint, request, jsonify
from firebase_admin import db
import requests
import os
from datetime import datetime

ig_multi_bp = Blueprint("ig_multi_bp", __name__)

# ================================
# 1. Intercambiar code por access_token
# ================================
@ig_multi_bp.route("/exchange_code", methods=["POST"])
def api_instagram_exchange_code():
    """
    Recibe { "code": "...", "redirect_uri": "..." } desde Flutter.
    Intercambia 'code' por 'access_token' y guarda en Firebase.
    """
    data = request.json or {}
    code = (data.get("code") or "").strip()
    redirect_uri = (data.get("redirect_uri") or "").strip()

    if not code or not redirect_uri:
        return jsonify({"error": "Faltan parámetros code o redirect_uri"}), 400

    try:
        resp = requests.post(
            "https://graph.facebook.com/v21.0/oauth/access_token",
            data={
                "client_id": os.getenv("IG_CLIENT_ID"),
                "client_secret": os.getenv("IG_CLIENT_SECRET"),
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=10,
        )
        token_data = resp.json()
        access_token = token_data.get("access_token")
        user_id = token_data.get("user_id")

        if not access_token or not user_id:
            return jsonify({"error": "No se obtuvo access_token", "detalle": token_data}), 400

        # Guardar en Firebase
        ref = db.reference(f"instagram_users/{user_id}")
        ref.set({
            "access_token": access_token,
            "user_id": user_id,
            "enabled": True,  # Por defecto ON
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

        print(f"[IG] ✅ Nuevo login Instagram user_id={user_id}")
        return jsonify({"ok": True, "user_id": user_id})

    except Exception as e:
        print(f"❌ Error en intercambio de code IG: {e}")
        return jsonify({"error": "Fallo procesando login Instagram"}), 500


# ================================
# 2. Consultar estado ON/OFF
# ================================
@ig_multi_bp.route("/status/<user_id>", methods=["GET"])
def api_instagram_status(user_id):
    try:
        ref = db.reference(f"instagram_users/{user_id}")
        data = ref.get() or {}
        enabled = bool(data.get("enabled", True))
        return jsonify({"enabled": enabled})
    except Exception as e:
        print(f"⚠️ Error leyendo status IG: {e}")
        return jsonify({"enabled": True})


# ================================
# 3. Encender / Apagar bot
# ================================
@ig_multi_bp.route("/toggle", methods=["POST"])
def api_instagram_toggle():
    data = request.json or {}
    user_id = (data.get("user_id") or "").strip()
    enabled = data.get("enabled", None)

    if not user_id or enabled is None:
        return jsonify({"error": "Parámetros inválidos (user_id, enabled)"}), 400

    try:
        ref = db.reference(f"instagram_users/{user_id}")
        cur = ref.get() or {}
        cur["enabled"] = bool(enabled)
        ref.set(cur)
        return jsonify({"ok": True, "enabled": bool(enabled)})
    except Exception as e:
        print(f"⚠️ Error guardando toggle IG: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
