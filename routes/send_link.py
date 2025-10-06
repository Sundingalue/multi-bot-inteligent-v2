# routes/send_link.py
import urllib.parse
from flask import Blueprint, request, jsonify, current_app
from twilio.rest import Client
from utils.bot_loader import load_bot  # tu loader existente

bp = Blueprint("send_link", __name__, url_prefix="/actions")

def _get(d: dict | None, path: list[str], default=None):
    cur = d or {}
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default

def _to_e164_us(raw: str) -> str:
    import re
    m = re.match(r'^\D*1?\D*([2-9]\d{2})\D*([2-9]\d{2})\D*(\d{4})\D*$', (raw or ""))
    return f"+1{m.group(1)}{m.group(2)}{m.group(3)}" if m else ""

def _build_url(base: str, name: str, phone: str, source: str):
    if not base:
        return ""
    qs = urllib.parse.urlencode({"name": name or "", "phone": phone or "", "source": source or "bot"})
    return f"{base}{'&' if '?' in base else '?'}{qs}"

@bp.post("/send-link")
def send_link():
    """
    JSON:
    {
      "bot": "whatsapp:+18326213202",   // OBLIGATORIO (clave para load_bot)
      "phone": "8326213202",            // OBLIGATORIO (US 10 dígitos)
      "channel": "sms" | "wa",          // opcional (default sms)
      "name": "Carlos",                 // opcional
      "link": "https://lo-que-sea.com/x"// opcional: si viene, se envía tal cual
      // overrides opcionales si NO están en el JSON del bot:
      // "from": "+1832XXXXXXX", "sid": "AC...", "token": "..."
    }
    """
    data = request.get_json(silent=True) or {}
    bot_key = (data.get("bot") or "").strip()
    if not bot_key:
        return jsonify({"ok": False, "error": "BOT_REQUIRED"}), 400

    phone    = (data.get("phone") or "").strip()
    name     = (data.get("name") or "").strip()
    channel  = (data.get("channel") or "sms").strip().lower()
    req_link = (data.get("link") or "").strip()

    # normaliza teléfono
    to_e164 = _to_e164_us(phone)
    if not to_e164:
        return jsonify({"ok": False, "error": "PHONE_INVALID", "detail": "Se esperan 10 dígitos US."}), 400

    # carga JSON del bot
    try:
        cfg = load_bot(bot_key)
    except Exception as e:
        current_app.logger.exception(f"[send-link] load_bot({bot_key}) error: {e}")
        return jsonify({"ok": False, "error": "BOT_LOAD_ERROR", "detail": str(e)}), 400

    # 1) Si viene 'link' en el body, se usa tal cual
    link = req_link

    # 2) Si no viene, lo armamos desde el JSON del bot
    if not link:
        base_url = (_get(cfg, ["booking", "url"])
                    or _get(cfg, ["calendar", "url"])
                    or _get(cfg, ["booking_url"]))
        link = _build_url(base_url, name=name, phone=phone, source=channel)

    if not link:
        return jsonify({"ok": False, "error": "LINK_MISSING",
                        "detail": "No llegó 'link' y el bot no tiene booking.url/calendar.url/booking_url."}), 400

    # remitente y credenciales por bot (con override opcional del request)
    use_wa = (channel in ("wa", "whatsapp"))
    from_number = (
        _get(cfg, ["twilio", "whatsapp", "from"]) if use_wa else _get(cfg, ["twilio", "sms", "from"])
    ) or _get(cfg, ["channels", "whatsapp" if use_wa else "sms", "from"]) or (data.get("from") or "").strip()

    sid   = _get(cfg, ["twilio", "account_sid"]) or _get(cfg, ["twilio", "sid"]) or (data.get("sid") or "").strip()
    token = _get(cfg, ["twilio", "auth_token"])  or _get(cfg, ["twilio", "token"]) or (data.get("token") or "").strip()

    if not from_number or not sid or not token:
        return jsonify({"ok": False, "error": "CONFIG_MISSING",
                        "detail": "Faltan from/sid/token en el JSON del bot o en el request."}), 400

    client = Client(sid, token)

    # Solo enviamos el link, sin relleno
    body = f"Aquí está tu enlace: {link}"

    to_fmt   = f"whatsapp:{to_e164}" if use_wa else to_e164
    from_fmt = f"whatsapp:{from_number}" if use_wa else from_number

    try:
        msg = client.messages.create(to=to_fmt, from_=from_fmt, body=body)
        return jsonify({"ok": True, "sid": msg.sid, "to": to_fmt, "from": from_fmt, "link": link})
    except Exception as e:
        current_app.logger.exception(f"[send-link] Twilio error: {e}")
        return jsonify({"ok": False, "error": "TWILIO_ERROR", "detail": str(e)}), 500

