# billing_api.py
# Maestro de Facturación (panel factura clientes) + Gráficos en vivo
# + CRUD de bots (sincroniza con carpeta ./bots para edición desde WP y VSCode)
# - Endpoints: clients, toggle, consumption, service-item, usage, invoice, usage_ts, track/openai
# - NUEVOS: /billing/bots (GET, POST), /billing/bots/<slug> (GET, DELETE)
# - Página /billing/panel: tabla + modal de detalle + sección de gráficos en vivo

from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import os, json, glob, re
import hmac, hashlib  # <-- NUEVO (firma opcional)

from firebase_admin import db
from twilio.rest import Client as TwilioClient

billing_bp = Blueprint("billing_bp", __name__)

# =======================
# Helpers
# =======================
def _here(*p):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *p)

def _bots_dir():
    d = _here("bots")
    os.makedirs(d, exist_ok=True)
    return d

def _bot_path(slug: str):
    safe = re.sub(r"[^a-zA-Z0-9_\-\.]", "-", (slug or "").strip())
    if not safe:
        return None
    return os.path.join(_bots_dir(), f"{safe}.json")

def _read_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _utcdate(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()

def _period_ym(dt=None):
    dt = dt or datetime.utcnow()
    return dt.strftime("%Y-%m")

def _daterange(d1, d2):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)

def _as_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return float(default)

# =======================
# Bots loader (desde ./bots/*.json)
# =======================
def load_bots_folder():
    bots = {}
    for path in glob.glob(os.path.join(_bots_dir(), "*.json")):
        try:
            data = _read_json(path)
            if isinstance(data, dict):
                # Admite dos formatos:
                # 1) {"slug":{"...config..."}}
                # 2) {"slug":"...", "name":"...", ...}
                if "slug" in data:
                    bots[data["slug"]] = data
                else:
                    for k, v in data.items():
                        if isinstance(v, dict):
                            v.setdefault("slug", k)
                            bots[k] = v
        except Exception as e:
            print(f"[billing_api] ⚠️ No se pudo cargar {path}: {e}")
    return bots

def _normalize_bot_name(bots_config: dict, name: str):
    if not name:
        return None
    for cfg in bots_config.values():
        if isinstance(cfg, dict) and cfg.get("name", "").lower() == str(name).lower():
            return cfg.get("name")
    return None

# =======================
# RTDB paths
# =======================
def _status_ref(bot_name: str):
    return db.reference(f"billing/status/{bot_name}")

def _consumption_ref(bot_name: str, period_ym: str):
    return db.reference(f"billing/consumption/{bot_name}/{period_ym}")

def _rates_ref(bot_name: str):
    return db.reference(f"billing/rates/{bot_name}")

def _service_item_ref(bot_name: str):
    return db.reference(f"billing/service_item/{bot_name}")

def _openai_day_ref(bot_name: str, ymd: str):
    return db.reference(f"billing/openai/{bot_name}/{ymd}/aggregate")

# =======================
# ON/OFF
# =======================
def _get_status(bot_name: str) -> str:
    try:
        val = _status_ref(bot_name).get()
        if isinstance(val, bool):
            return "on" if val else "off"
        if isinstance(val, str):
            return "on" if val.lower() == "on" else "off"
        return "off"
    except Exception as e:
        print(f"[billing_api] ⚠️ Error leyendo status: {e}")
        return "off"

def _set_status(bot_name: str, state: str):
    try:
        _status_ref(bot_name).set(True if state == "on" else False)
        return True
    except Exception as e:
        print(f"[billing_api] ❌ Error guardando status: {e}")
        return False

# =======================
# OpenAI usage (aggregate y serie)
# =======================
def record_openai_usage(bot: str, model: str, input_tokens: int, output_tokens: int):
    if not bot:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    ref = _openai_day_ref(bot, today)
    cur = ref.get() or {}
    cur["total_input_tokens"]  = int(cur.get("total_input_tokens", 0)) + int(input_tokens or 0)
    cur["total_output_tokens"] = int(cur.get("total_output_tokens", 0)) + int(output_tokens or 0)
    cur["total_requests"]      = int(cur.get("total_requests", 0)) + 1

    m = (model or "unknown")
    model_counts = cur.get("model_counts", {})
    info = model_counts.get(m, {"requests":0,"input_tokens":0,"output_tokens":0})
    info["requests"]      += 1
    info["input_tokens"]  += int(input_tokens or 0)
    info["output_tokens"] += int(output_tokens or 0)
    model_counts[m] = info
    cur["model_counts"] = model_counts
    ref.set(cur)

def _get_openai_rates(bot: str):
    bot_rates = _rates_ref(bot).get() or {}
    return (
        _as_float(bot_rates.get("openai_input_per_1k", os.getenv("OAI_INPUT_PER_1K", "0.00"))),
        _as_float(bot_rates.get("openai_output_per_1k", os.getenv("OAI_OUTPUT_PER_1K", "0.00")))
    )

def _sum_openai(bot: str, d1: str, d2: str):
    start, end = _utcdate(d1), _utcdate(d2)
    t_in = t_out = t_req = 0
    model_counts = {}
    per_day = []
    rate_in, rate_out = _get_openai_rates(bot)

    for d in _daterange(start, end):
        ymd = d.strftime("%Y-%m-%d")
        node = _openai_day_ref(bot, ymd).get() or {}
        di  = int(node.get("total_input_tokens", 0))
        do  = int(node.get("total_output_tokens", 0))
        dr  = int(node.get("total_requests", 0))
        cost = (di/1000.0)*rate_in + (do/1000.0)*rate_out
        per_day.append({
            "date": ymd,
            "input_tokens": di,
            "output_tokens": do,
            "requests": dr,
            "cost_estimate_usd": round(cost, 6)
        })
        t_in  += di; t_out += do; t_req += dr
        for m, info in (node.get("model_counts", {}) or {}).items():
            acc = model_counts.get(m, {"requests":0,"input_tokens":0,"output_tokens":0})
            acc["requests"]      += int(info.get("requests", 0))
            acc["input_tokens"]  += int(info.get("input_tokens", 0))
            acc["output_tokens"] += int(info.get("output_tokens", 0))
            model_counts[m] = acc

    total_cost = (t_in/1000.0)*rate_in + (t_out/1000.0)*rate_out
    return {
        "requests": t_req,
        "input_tokens": t_in,
        "output_tokens": t_out,
        "model_breakdown": model_counts,
        "rate_input_per_1k": rate_in,
        "rate_output_per_1k": rate_out,
        "cost_estimate_usd": round(total_cost, 4),
        "per_day": per_day
    }

# =======================
# Twilio usage (aggregate y serie)
# =======================
def _twilio_client():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not tok:
        return None
    return TwilioClient(sid, tok)

def _get_bot_twilio_number(cfg: dict) -> str:
    return (cfg.get("twilio_number") or cfg.get("whatsapp_number") or "").strip()

def _twilio_sum_prices(bot_cfg: dict, start: str, end: str, from_number_override: str = ""):
    client = _twilio_client()
    res = {"messages": 0, "price_usd": 0.0, "note": "Basado en Message.price; algunos mensajes pueden tardar en reflejar precio definitivo."}
    if not client:
        res["note"] = "Sin credenciales de Twilio en entorno."
        return res

    from_number = (from_number_override or "").strip()
    if not from_number:
        from_number = _get_bot_twilio_number(bot_cfg)

    d1 = datetime.strptime(start, "%Y-%m-%d")
    d2 = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)

    total_msgs = 0
    total_price = 0.0
    try:
        msgs = client.messages.list(date_sent_after=d1, date_sent_before=d2)
        for m in msgs:
            if from_number and (str(m.from_) or "").strip() != from_number:
                continue
            total_msgs += 1
            if m.price and m.price_unit == "USD":
                total_price += _as_float(m.price, 0.0)
    except Exception as e:
        print(f"[billing_api] ⚠️ Error Twilio list: {e}")
        res["note"] = "Error consultando Twilio (revisa SID/TOKEN y rango)."

    res["messages"] = total_msgs
    res["price_usd"] = round(total_price, 4)
    return res

def _twilio_series(bot_cfg: dict, start: str, end: str, from_number_override: str = ""):
    client = _twilio_client()
    per_day = []
    total_msgs = 0
    total_price = 0.0
    note = "Basado en Message.price; algunos mensajes pueden tardar en reflejar precio definitivo."

    if not client:
        return {"per_day": [], "messages": 0, "price_usd": 0.0, "note": "Sin credenciales de Twilio en entorno."}

    from_number = (from_number_override or "").strip()
    if not from_number:
        from_number = _get_bot_twilio_number(bot_cfg)

    s, e = _utcdate(start), _utcdate(end)
    try:
        for day in _daterange(s, e):
            d1 = datetime(day.year, day.month, day.day)
            d2 = d1 + timedelta(days=1)
            msgs = client.messages.list(date_sent_after=d1, date_sent_before=d2)
            cnt = 0
            cost = 0.0
            for m in msgs:
                if from_number and (str(m.from_) or "").strip() != from_number:
                    continue
                cnt += 1
                if m.price and m.price_unit == "USD":
                    cost += _as_float(m.price, 0.0)
            per_day.append({"date": day.strftime("%Y-%m-%d"), "messages": cnt, "price_usd": round(cost, 6)})
            total_msgs += cnt
            total_price += cost
    except Exception as e:
        print(f"[billing_api] ⚠️ Error Twilio series: {e}")
        note = "Error consultando Twilio (revisa SID/TOKEN y rango)."

    return {"per_day": per_day, "messages": total_msgs, "price_usd": round(total_price, 4), "note": note}

# =======================
# Ítem fijo de servicio
# =======================
def _get_service_item(bot: str):
    n = _service_item_ref(bot).get() or {}
    return {
        "enabled": bool(n.get("enabled", True)),
        "amount":  _as_float(n.get("amount", os.getenv("SERVICE_ITEM_AMOUNT", "200.0"))),
        "label":   str(n.get("label", os.getenv("SERVICE_ITEM_LABEL", "Entrenamiento y mantenimiento de bot (mensual)")))
    }

def _set_service_item(bot: str, enabled: bool, amount: float, label: str):
    payload = {"enabled": bool(enabled), "amount": float(amount), "label": (label or "").strip() or "Servicio"}
    _service_item_ref(bot).set(payload)
    return payload

# =======================
# CRUD de BOTS (sincroniza con ./bots/*.json)
# =======================
@billing_bp.route("/bots", methods=["GET"])
def bots_list():
    data = []
    for slug, cfg in load_bots_folder().items():
        item = {
            "slug": slug,
            "name": cfg.get("name", slug),
            "system_prompt": cfg.get("system_prompt", ""),
            "voice": cfg.get("voice", ""),
            "lang": cfg.get("lang", "es"),
            "tone": cfg.get("tone", ""),
            "temperature": cfg.get("temperature", 0.7),
            "tts_speed": cfg.get("tts_speed", 1.0),
            "tts_pitch": cfg.get("tts_pitch", 0),
            "greeting": cfg.get("greeting", ""),
        }
        data.append(item)
    return jsonify({"success": True, "data": data})

@billing_bp.route("/bots/<slug>", methods=["GET"])
def bots_get(slug):
    p = _bot_path(slug)
    if not p or not os.path.isfile(p):
        return jsonify({"success": False, "message": "No encontrado"}), 404
    data = _read_json(p)
    # Normaliza formato
    if "slug" not in data:
        # formato {"slug": {...}}
        only = list(data.values())[0]
        only["slug"] = slug
        data = only
    return jsonify({"success": True, "data": data})

@billing_bp.route("/bots/<slug>", methods=["DELETE"])
def bots_delete(slug):
    p = _bot_path(slug)
    if not p or not os.path.isfile(p):
        return jsonify({"success": False, "message": "No encontrado"}), 404
    os.remove(p)
    return jsonify({"success": True})

@billing_bp.route("/bots", methods=["POST"])
def bots_upsert():
    data = request.get_json(silent=True) or {}
    slug = (data.get("slug") or "").strip()
    if not slug:
        return jsonify({"success": False, "message": "slug requerido"}), 400
    payload = {
        "slug": slug,
        "name": data.get("name", slug),
        "system_prompt": data.get("system_prompt", ""),
        "voice": data.get("voice", ""),
        "lang": data.get("lang", "es"),
        "tone": data.get("tone", ""),
        "temperature": _as_float(data.get("temperature", 0.7)),
        "tts_speed": _as_float(data.get("tts_speed", 1.0)),
        "tts_pitch": _as_float(data.get("tts_pitch", 0)),
        "greeting": data.get("greeting", ""),
    }
    path = _bot_path(slug)
    _write_json(path, payload)
    return jsonify({"success": True, "data": payload})

# =======================
# Endpoints existentes (clientes, usage, etc.)
# =======================
@billing_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "billing_api", "time": datetime.utcnow().isoformat() + "Z"})

@billing_bp.route("/clients", methods=["GET"])
def list_clients():
    bots_config = load_bots_folder()
    period = request.args.get("period") or _period_ym()

    items = []
    for cfg in bots_config.values():
        if not isinstance(cfg, dict):
            continue
        bot_name = cfg.get("name") or cfg.get("slug") or ""
        if not bot_name:
            continue

        business_name = cfg.get("business_name", bot_name)
        email = cfg.get("email") or (cfg.get("contact", {}) or {}).get("email") or ""
        phone = cfg.get("phone") or (cfg.get("contact", {}) or {}).get("phone") or ""

        val = _consumption_ref(bot_name, period).get()
        consumo_cents = int((val or {}).get("cents", 0) if isinstance(val, dict) else (val or 0))

        status = _get_status(bot_name)
        svc = _get_service_item(bot_name)

        items.append({
            "id": bot_name,
            "name": business_name,
            "email": email,
            "phone": phone,
            "consumo_cents": consumo_cents,
            "consumo_period": period,
            "bot_status": status,
            "service_item": svc,
        })

    return jsonify({"success": True, "data": items})

@billing_bp.route("/toggle", methods=["POST"])
def toggle_bot():
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    state = (data.get("state") or "").strip().lower()

    if state not in ("on", "off") or not client_id:
        return jsonify({"success": False, "message": "Parámetros inválidos"}), 400

    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, client_id) or client_id
    ok = _set_status(bot_norm, state)
    if not ok:
        return jsonify({"success": False, "message": "No se pudo guardar en Firebase"}), 500

    return jsonify({"success": True})

@billing_bp.route("/consumption/<bot_name>", methods=["GET"])
def get_consumption(bot_name):
    period = request.args.get("period") or _period_ym()
    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, bot_name) or bot_name

    val = _consumption_ref(bot_norm, period).get()
    cents = int((val or {}).get("cents", 0) if isinstance(val, dict) else (val or 0))
    return jsonify({"success": True, "bot": bot_norm, "period": period, "consumo_cents": cents})

@billing_bp.route("/service-item/<bot>", methods=["GET", "POST"])
def service_item(bot):
    bots_config = load_bots_folder()
    bot_norm = _normalize_bot_name(bots_config, bot) or bot

    if request.method == "GET":
        return jsonify({"success": True, "service_item": _get_service_item(bot_norm)})

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    amount  = _as_float(data.get("amount", 0.0))
    label   = str(data.get("label", "") or "")
    saved = _set_service_item(bot_norm, enabled, amount, label)
    return jsonify({"success": True, "service_item": saved})

@billing_bp.route("/usage/<bot>", methods=["GET"])
def usage(bot):
    start = (request.args.get("start") or "").strip()
    end   = (request.args.get("end") or "").strip()
    from_number = (request.args.get("from_number") or "").strip()

    if not start or not end:
        return jsonify({"success": False, "message": "start y end son requeridos (YYYY-MM-DD)"}), 400

    bots_config = load_bots_folder()
    bot_cfg = None
    bot_name = None
    for cfg in bots_config.values():
        if cfg.get("name", "").lower() == bot.lower() or cfg.get("slug","").lower()==bot.lower():
            bot_cfg = cfg
            bot_name = cfg.get("name") or cfg.get("slug")
            break
    if not bot_name:
        bot_name = bot
        bot_cfg = {}

    oa = _sum_openai(bot_name, start, end)
    tw = _twilio_sum_prices(bot_cfg, start, end, from_number_override=from_number)
    svc = _get_service_item(bot_name)

    subtotal = oa.get("cost_estimate_usd", 0.0) + tw.get("price_usd", 0.0)
    total = subtotal + (svc["amount"] if svc["enabled"] else 0.0)

    payload = {
        "bot": bot_name,
        "range": {"start": start, "end": end},
        "twilio": tw,
        "openai": oa,
        "service_item": svc,
        "subtotal_usd": round(subtotal, 4),
        "total_usd": round(total, 4)
    }
    return jsonify(payload)

@billing_bp.route("/usage_ts/<bot>", methods=["GET"])
def usage_ts(bot):
    start = (request.args.get("start") or "").strip()
    end   = (request.args.get("end") or "").strip()
    from_number = (request.args.get("from_number") or "").strip()
    if not start or not end:
        return jsonify({"success": False, "message": "start y end son requeridos (YYYY-MM-DD)"}), 400

    bots_config = load_bots_folder()
    bot_cfg = None
    bot_name = None
    for cfg in bots_config.values():
        if cfg.get("name", "").lower() == bot.lower() or cfg.get("slug","").lower()==bot.lower():
            bot_cfg = cfg
            bot_name = cfg.get("name") or cfg.get("slug")
            break
    if not bot_name:
        bot_name = bot
        bot_cfg = {}

    oa_all = _sum_openai(bot_name, start, end)
    tw_all = _twilio_series(bot_cfg, start, end, from_number_override=from_number)

    return jsonify({
        "success": True,
        "bot": bot_name,
        "range": {"start": start, "end": end},
        "openai": {
            "rate_input_per_1k": oa_all["rate_input_per_1k"],
            "rate_output_per_1k": oa_all["rate_output_per_1k"],
            "totals": {
                "requests": oa_all["requests"],
                "input_tokens": oa_all["input_tokens"],
                "output_tokens": oa_all["output_tokens"],
                "cost_estimate_usd": oa_all["cost_estimate_usd"]
            },
            "per_day": oa_all["per_day"]
        },
        "twilio": {
            "totals": {"messages": tw_all["messages"], "price_usd": tw_all["price_usd"], "note": tw_all["note"]},
            "per_day": tw_all["per_day"]
        }
    })

@billing_bp.route("/invoice/<bot>", methods=["GET"])
def invoice(bot):
    return usage(bot)

@billing_bp.route("/track/openai", methods=["POST"])
def track_openai():
    data = request.get_json(silent=True) or {}
    bot = (data.get("bot") or "").strip()
    model = (data.get("model") or "").strip()
    itok = int(data.get("input_tokens") or 0)
    otok = int(data.get("output_tokens") or 0)
    if not bot:
        return jsonify({"success": False, "message": "bot requerido"}), 400
    record_openai_usage(bot, model, itok, otok)
    return jsonify({"success": True})

# (La página HTML /panel se queda igual que ya tenías)

# =======================
# === Webhook ElevenLabs + Email opcional ===
# =======================
import smtplib, ssl
from email.message import EmailMessage
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

def _smtp_settings():
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587").strip() or "587"),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASS", "").strip(),
        "from_addr": os.getenv("EMAIL_FROM", "").strip() or os.getenv("SMTP_USER", "").strip(),
        "to_addrs": [a.strip() for a in (os.getenv("EMAIL_TO", "").split(",") if os.getenv("EMAIL_TO") else []) if a.strip()],
    }

def _send_email(subject: str, body_text: str, attachments: list = None, to_addrs: list = None):
    """attachments: lista de dicts {"filename": str, "content": bytes, "mime": "audio/mpeg" ...}
       to_addrs: lista de destinatarios (si None, usa EMAIL_TO del entorno)"""
    cfg = _smtp_settings()
    final_to = to_addrs if (to_addrs and len(to_addrs)>0) else cfg["to_addrs"]
    if not (cfg["host"] and cfg["from_addr"] and final_to):
        print("[email] ⚠️ SMTP no configurado o sin destinatarios (SMTP_HOST/PORT/USER/PASS, EMAIL_FROM, EMAIL_TO o to_addrs). Solo log.")
        print("[email] Asunto:", subject)
        print("[email] Para:", final_to or "(vacío)")
        print("[email] Texto:\n", body_text)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(final_to)
    msg.set_content(body_text)

    for att in (attachments or []):
        try:
            msg.add_attachment(att["content"], maintype=(att.get("mime","application/octet-stream").split("/")[0]),
                               subtype=(att.get("mime","application/octet-stream").split("/")[1]),
                               filename=att.get("filename","file.bin"))
        except Exception as e:
            print(f"[email] ⚠️ No se pudo adjuntar {att.get('filename')}: {e}")

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
            server.starttls(context=context)
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        print("[email] ✉️ Enviado OK a:", final_to)
        return True
    except Exception as e:
        print("[email] ❌ Error SMTP:", e)
        return False

def _download_file(url: str, timeout: int = 15):
    try:
        with urlopen(url, timeout=timeout) as r:
            data = r.read()
            ct = r.info().get_content_type() or "application/octet-stream"
            return data, ct
    except (URLError, HTTPError) as e:
        print("[download] ⚠️ Error descargando", url, e)
        return None, None

# ====== Resolución de destinatarios por BOT ======
def _extract_emails_from_cfg(cfg: dict) -> list:
    """Devuelve lista de correos en orden de prioridad:
       notify.emails -> emails -> contact.email"""
    emails = []

    # notify.emails (preferido)
    notify = cfg.get("notify") or {}
    if isinstance(notify, dict):
        arr = notify.get("emails")
        if isinstance(arr, list):
            emails.extend([e for e in arr if isinstance(e, str) and e.strip()])

    # emails (array)
    if not emails:
        arr2 = cfg.get("emails")
        if isinstance(arr2, list):
            emails.extend([e for e in arr2 if isinstance(e, str) and e.strip()])

    # contact.email (string)
    if not emails:
        contact = cfg.get("contact") or {}
        if isinstance(contact, dict):
            ce = contact.get("email")
            if isinstance(ce, str) and ce.strip():
                emails.append(ce.strip())

    return [e.strip() for e in emails if e and isinstance(e, str)]

def _find_bot_cfg_for_payload(payload: dict) -> dict:
    """Intenta identificar el bot según varios campos del payload y la carpeta ./bots"""
    bots = load_bots_folder()
    if not bots:
        return {}

    candidates = []
    for key in ("bot_slug","agent_slug","bot","agent","agent_name","assistant","assistant_name"):
        v = (payload.get(key) or "").strip()
        if v: candidates.append(v)
    for key in ("agent_number","twilio_to","to","line","number","assistant_number","recipient_number"):
        v = (payload.get(key) or "").strip()
        if v: candidates.append(v)

    extra = payload.get("extra") or {}
    if isinstance(extra, dict):
        for key in ("bot","agent","agent_number","to","twilio_to","number","recipient_number"):
            v = (extra.get(key) or "").strip()
            if v: candidates.append(v)

    # 1) slug exacto
    for cand in candidates:
        if cand in bots:
            return bots[cand]

    # 2) nombre
    for cand in candidates:
        for cfg in bots.values():
            if str(cfg.get("name","")).strip().lower() == cand.strip().lower():
                return cfg

    # 3) número
    for cand in candidates:
        c = cand.strip().lower()
        for cfg in bots.values():
            if str(cfg.get("twilio_number","")).strip().lower() == c:
                return cfg
            if str(cfg.get("whatsapp_number","")).strip().lower() == c:
                return cfg

    # 4) 'to' con formato que coincida con slug
    to_v = (payload.get("to") or "").strip()
    if to_v and to_v in bots:
        return bots[to_v]

    return {}

def _bot_emails_for_event(payload: dict) -> list:
    """Obtiene lista de emails específicos del bot. Si no hay, usa EMAIL_TO."""
    cfg = _find_bot_cfg_for_payload(payload)
    emails = _extract_emails_from_cfg(cfg) if cfg else []
    if emails:
        return emails
    env_to = [a.strip() for a in (os.getenv("EMAIL_TO","").split(",") if os.getenv("EMAIL_TO") else []) if a.strip()]
    return env_to

# ====== Verificación opcional de firma HMAC de Eleven ======
def _verify_eleven_signature(req) -> bool:
    secret = os.getenv("ELEVEN_WEBHOOK_SECRET", "").strip()
    if not secret:
        return True  # no forzamos si no está configurado
    sig = req.headers.get("ElevenLabs-Signature") or req.headers.get("X-ElevenLabs-Signature") or ""
    if not sig:
        print("[eleven_webhook] ⚠️ Falta header de firma. Se continúa por compatibilidad.")
        return True
    # Eleven suele firmar con HMAC-SHA256 del cuerpo; comparamos el hexdigest
    body = req.get_data() or b""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    ok = hmac.compare_digest(sig.strip(), digest.strip()) or hmac.compare_digest(sig.replace("sha256=","").strip(), digest.strip())
    if not ok:
        print("[eleven_webhook] ⚠️ Firma HMAC no coincide. Header:", sig, " Calculada:", digest)
    return True  # dejamos pasar pero lo registramos (cámbialo a 'ok' si quieres bloquear)

@billing_bp.route("/webhooks/eleven/post-call", methods=["POST"])
def eleven_post_call():
    """
    Webhook genérico para ElevenLabs Post-Call.
    Espera un JSON con (ejemplos):
      - caller / phone / from
      - started_at / ended_at / duration_seconds
      - transcript (texto)
      - summary (opcional)
      - recordings: [ { "url": "...", "format": "mp3" }, ... ]
      - agent / bot (nombre o slug)
      - extra: { name, lastname, email, ... }
    """
    _verify_eleven_signature(request)  # no bloquea, solo avisa si no coincide

    payload = request.get_json(silent=True) or {}
    print("[eleven_webhook] payload:", payload)

    # Fallbacks robustos para campos comunes
    caller = (
        payload.get("caller") or payload.get("from") or payload.get("phone") or
        (payload.get("call") or {}).get("from") or ""
    )
    agent  = (
        payload.get("agent")  or payload.get("bot") or payload.get("agent_name") or
        (payload.get("call") or {}).get("agent") or ""
    )
    started= payload.get("started_at") or (payload.get("call") or {}).get("started_at") or ""
    ended  = payload.get("ended_at")   or (payload.get("call") or {}).get("ended_at") or ""
    dur    = (
        payload.get("duration_seconds") or payload.get("duration") or
        (payload.get("call") or {}).get("duration_seconds") or ""
    )
    trans  = payload.get("transcript") or (payload.get("call") or {}).get("transcript") or ""
    summ   = payload.get("summary")    or (payload.get("call") or {}).get("summary") or ""
    extra  = payload.get("extra") or payload.get("contact") or {}

    # Si no traen 'agent', lo resolvemos por el bot del payload/carpeta
    if not agent:
        cfg_guess = _find_bot_cfg_for_payload(payload)
        if cfg_guess:
            agent = cfg_guess.get("name") or cfg_guess.get("slug") or "Agente"

    recs   = payload.get("recordings") or payload.get("recording_urls") or []
    # normaliza recordings si vienen como string
    if isinstance(recs, str):
        recs = [recs]

    # Construir cuerpo de email (con fallback al JSON crudo)
    lines = []
    lines.append(f"Agente/Bot: {agent or 'Agente'}")
    lines.append(f"Llamada de: {caller or 'desconocido'}")
    if started or ended:
        lines.append(f"Inicio: {started or '-'}  |  Fin: {ended or '-'}")
    if dur:
        lines.append(f"Duración (s): {dur}")

    if isinstance(extra, dict) and any(k in extra for k in ("name","lastname","email")):
        lines.append("— Datos capturados —")
        if extra.get("name"):     lines.append(f"Nombre: {extra.get('name')}")
        if extra.get("lastname"): lines.append(f"Apellido: {extra.get('lastname')}")
        if extra.get("email"):    lines.append(f"Email: {extra.get('email')}")

    if summ:
        lines.append("\n== Resumen ==")
        lines.append(str(summ))
    if trans:
        lines.append("\n== Transcripción ==")
        lines.append(str(trans))

    # Si todo vino vacío, mete el JSON crudo para depurar
    if len("\n".join(lines).strip()) == 0 or (not trans and not summ and not caller and not agent):
        lines.append("\n== Payload recibido ==")
        try:
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception:
            lines.append(str(payload))

    # Adjuntar primer audio si hay
    attachments = []
    first_audio = None
    if isinstance(recs, list) and recs:
        first = recs[0]
        if isinstance(first, dict):
            first_audio = first.get("url") or first.get("href")
        elif isinstance(first, str):
            first_audio = first
    if first_audio:
        data, mime = _download_file(first_audio)
        if data:
            ext = "mp3"
            if "wav" in (mime or "") or first_audio.endswith(".wav"):
                ext = "wav"
            attachments.append({
                "filename": f"call_recording.{ext}",
                "content": data,
                "mime": mime or "audio/mpeg"
            })
        else:
            lines.append(f"\n(No se pudo descargar grabación: {first_audio})")

    # Destinatarios por BOT
    recipients = _bot_emails_for_event(payload)
    print("[eleven_webhook] Destinatarios resueltos:", recipients or "(vacío)")

    subj = f"[Post-Call] {agent or 'Agente'} – {caller or 'desconocido'}"
    _send_email(subj, "\n".join(lines), attachments, to_addrs=recipients)

    return jsonify({"ok": True})

@billing_bp.route("/webhooks/test-email", methods=["GET"])
def test_email():
    """Envía un email de prueba usando las variables SMTP_*/EMAIL_* del entorno."""
    ok = _send_email(
        subject="Prueba INH Billing – SMTP",
        body_text="Esto es un correo de prueba desde /billing/webhooks/test-email.\nSi lo recibes, la configuración SMTP está OK."
    )
    return jsonify({"ok": bool(ok)})
