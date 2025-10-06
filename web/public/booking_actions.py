# web/booking_actions.py
import os, urllib.parse
from flask import Blueprint, request, jsonify, current_app

from utils.sms_sender import to_e164_us, send_message

# ⚠️ IMPORTANTE:
# Tu loader real está en /utils. Si tu función se llama distinto,
# cámbialo aquí.
try:
    from utils.bot_loader import load_bot   # tu función existente
except Exception:
    # Si tu loader está en otro módulo, ajusta este import.
    load_bot = None

bp = Blueprint("booking_actions", __name__, url_prefix="/actions")

def _get(nested: dict | None, path: list[str], default=None):
    cur = nested or {}
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default

def _build_booking_link(base_url: str, name: str, phone: str, source: str):
    base = (base_url or os.environ.get("BOOKING_URL_FALLBACK", "") or "https://example.com/agenda").strip()
    params = {"name": name or "", "phone": phone or "", "source": source or "elevenlabs"}
    qs = urllib.parse.urlencode(params, doseq=False, safe="")
    glue = "&" if "?" in base else "?"
    return f"{base}{glue}{qs}"

@bp.route("/send-booking-sms", methods=["POST"])
def send_booking_sms():
    """
    BODY JSON:
    {
      "bot": "houston_school" | "whatsapp:+18326213202" | "niniafit" | ...   (identificador que tu loader acepte)  ← OBLIGATORIO
      "phone": "8323790809",   ← OBLIGATORIO (US 10 dígitos)
      "name": "Carlos",        ← opcional
      "booking_url": "",       ← opcional (si no viene, se toma del JSON del bot)
      "channel": "web|call|wa|ig", ← opcional (si 'wa' = WhatsApp)
      "from": "+1832XXXXXXX"   ← opcional (override manual del remitente)
    }
    """
    data = request.get_json(silent=True) or {}
    bot_key  = str(data.get("bot", "")).strip()
    raw_phone = str(data.get("phone", "")).strip()
    name      = str(data.get("name", "")).strip()
    channel   = (str(data.get("channel", "")).strip().lower() or "web")
    req_booking_url = str(data.get("booking_url", "")).strip()
    req_from        = str(data.get("from", "")).strip()

    if not bot_key:
        return jsonify({"ok": False, "error": "BOT_REQUIRED"}), 400
    if not raw_phone:
        return jsonify({"ok": False, "error": "PHONE_REQUIRED"}), 400

    # Cargar JSON por BOT
    bot_cfg = None
    try:
        if load_bot is None:
            raise RuntimeError("No encuentro load_bot(). Ajusta import en web/booking_actions.py")
        bot_cfg = load_bot(bot_key)
    except Exception as e:
        current_app.logger.exception(f"[send-booking-sms] No pude cargar bot={bot_key}: {e}")
        return jsonify({"ok": False, "error": "BOT_LOAD_ERROR", "detail": str(e)}), 400

    # Resolver booking URL por-JSON
    bot_booking = (
        _get(bot_cfg, ["booking", "url"]) or
        _get(bot_cfg, ["calendar", "url"]) or
        _get(bot_cfg, ["booking_url"])
    )
    booking_url = req_booking_url or bot_booking  # si no, fallback se agrega en _build_booking_link
    booking_url = _build_booking_link(booking_url, name=name, phone=raw_phone, source=channel)

    # Normalizar teléfono
    to = to_e164_us(raw_phone)
    if not to:
        return jsonify({"ok": False, "error": "PHONE_INVALID", "detail": "Teléfono US inválido. Se esperan 10 dígitos."}), 400

    # Elegir canal/remitente por-JSON
    use_whatsapp = (channel in ("wa", "whatsapp", "ig-wa"))
    if use_whatsapp:
        from_number = (
            _get(bot_cfg, ["twilio", "whatsapp", "from"]) or
            _get(bot_cfg, ["channels", "whatsapp", "from"]) or
            req_from
        )
    else:
        from_number = (
            _get(bot_cfg, ["twilio", "sms", "from"]) or
            _get(bot_cfg, ["channels", "sms", "from"]) or
            req_from
        )

    # Credenciales por-JSON (opcionales; si faltan, toman ENV)
    bot_sid = _get(bot_cfg, ["twilio", "account_sid"]) or _get(bot_cfg, ["twilio", "sid"])
    bot_tok = _get(bot_cfg, ["twilio", "auth_token"])  or _get(bot_cfg, ["twilio", "token"])

    # Mensaje
    body = f"Hola{name and ' ' + name or ''}, aquí está tu enlace para agendar: {booking_url}\n— In Houston Texas"

    try:
        sid = send_message(
            to,
            body,
            from_number=from_number,
            account_sid=bot_sid,
            auth_token=bot_tok,
            use_whatsapp=use_whatsapp
        )
        return jsonify({"ok": True, "sid": sid, "to": to, "booking_url": booking_url, "bot": bot_key, "wa": use_whatsapp})
    except Exception as e:
        current_app.logger.exception(f"[send-booking-sms] Error Twilio: {e}")
        return jsonify({"ok": False, "error": "TWILIO_ERROR", "detail": str(e)}), 500
