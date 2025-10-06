# utils/sms_sender.py
import os, re
from twilio.rest import Client

# Normalizador US -> E.164
_US10 = re.compile(r'^\D*1?\D*([2-9]\d{2})\D*([2-9]\d{2})\D*(\d{4})\D*$')

def to_e164_us(phone_raw: str) -> str:
    if not phone_raw:
        return ""
    m = _US10.match(phone_raw)
    if not m:
        return ""
    return f"+1{m.group(1)}{m.group(2)}{m.group(3)}"

def _twilio_client(account_sid: str | None, auth_token: str | None) -> Client:
    # Credenciales por BOT; si no hay, último fallback: ENV
    sid = (account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")).strip()
    tok = (auth_token  or os.environ.get("TWILIO_AUTH_TOKEN", "")).strip()
    if not sid or not tok:
        raise RuntimeError("Config Twilio incompleta (SID/TOKEN). Define en JSON del bot o en variables de entorno.")
    return Client(sid, tok)

def send_message(
    to_e164: str,
    body: str,
    *,
    from_number: str | None,
    account_sid: str | None = None,
    auth_token: str | None = None,
    use_whatsapp: bool = False
) -> str:
    """
    Envía SMS/WhatsApp usando remitente y credenciales por BOT (JSON).
    - from_number: requerido (por BOT). Si use_whatsapp=True, sin 'whatsapp:' (el prefijo se agrega aquí).
    """
    if not to_e164:
        raise ValueError("Destinatario vacío (E.164).")
    if not from_number:
        # último fallback si el JSON no lo trae:
        from_number = os.environ.get("TWILIO_FROM", "").strip()
    if not from_number:
        raise RuntimeError("No hay número remitente (from). Define en JSON del bot (twilio.sms.from / twilio.whatsapp.from).")

    client = _twilio_client(account_sid, auth_token)

    if use_whatsapp:
        to_fmt   = f"whatsapp:{to_e164}"
        from_fmt = f"whatsapp:{from_number}"
    else:
        to_fmt   = to_e164
        from_fmt = from_number

    msg = client.messages.create(to=to_fmt, from_=from_fmt, body=body)
    return msg.sid
