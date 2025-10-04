# voice_webrtc_bridge.py
# Bridge Twilio <Stream> (PCMU 8k) ↔ OpenAI Realtime WS (PCM16 16k)
# Sin 'audioop' (eliminado en Python 3.13). Usamos NumPy.
# + El saludo inicial lo habla OpenAI (no Twilio).
# + Emite “meter” del audio del BOT vía Twilio mark (no rompe el schema).
# + Fixes: formatos Realtime, modalidades, y commit con >100ms de audio.
# + Extra: VAD simple por silencio, logs de commit y keepalive al WS Realtime.

import os, json, base64, time, threading
from flask import Blueprint, request, Response, current_app
from twilio.twiml.voice_response import VoiceResponse
import websocket  # websocket-client
from threading import Thread
from urllib.parse import urlencode  # para URL-encode del '+'

try:
    import numpy as np
except Exception as e:
    raise RuntimeError(
        "Falta NumPy para el bridge de audio. Agrega 'numpy' a requirements.txt"
    ) from e

bp = Blueprint("voice_webrtc", __name__, url_prefix="/voice-webrtc")

# ---------- Utils ----------
def _canonize_phone(raw: str) -> str:
    s = str(raw or "").strip()
    for p in ("whatsapp:", "tel:", "sip:", "client:"):
        if s.startswith(p):
            s = s[len(p):]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits

def _get_bot_cfg_by_any_number(bots_config: dict, to_number: str):
    canon = _canonize_phone(to_number)
    for k, cfg in (bots_config or {}).items():
        if _canonize_phone(k) == canon:
            return cfg
    return (bots_config or {}).get(to_number)

# ---------- μ-law <-> PCM16 y resampling ----------
def _ulaw_to_linear(ulaw_bytes: bytes) -> np.ndarray:
    u = np.frombuffer(ulaw_bytes, dtype=np.uint8)
    u = ~u
    sign = (u & 0x80) != 0
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa.astype(np.int32) << 3) + 0x84) << exponent
    x = magnitude - 0x84
    pcm = x.astype(np.int32)
    pcm[sign] = -pcm[sign]
    pcm = np.clip(pcm, -32768, 32767).astype(np.int16)
    return pcm

def _linear_to_ulaw(pcm16: np.ndarray) -> bytes:
    x = pcm16.astype(np.int32)
    sign = (x < 0)
    x = np.abs(x)
    x = np.clip(x + 0x84, 0, 0x7FFF)
    def _msb_index(v):
        idx = np.zeros_like(v)
        vv = v.copy()
        for shift in [8, 4, 2, 1]:
            mask = vv >= (1 << shift)
            idx[mask] += shift
            vv[mask] >>= shift
        return idx
    exp = _msb_index(x >> 7)
    mant = (x >> (exp + 3)) & 0x0F
    ulaw = (~((sign.astype(np.uint8) << 7) | (exp << 4) | mant)) & 0xFF
    return ulaw.tobytes()

def _resample_linear(pcm: np.ndarray, sr_src: int, sr_dst: int) -> np.ndarray:
    if sr_src == sr_dst or pcm.size == 0:
        return pcm
    ratio = sr_dst / float(sr_src)
    n_dst = int(round(pcm.size * ratio))
    x_old = np.arange(pcm.size)
    x_new = np.linspace(0, pcm.size - 1, num=n_dst)
    y_new = np.interp(x_new, x_old, pcm.astype(np.float32))
    y_new = np.clip(np.round(y_new), -32768, 32767).astype(np.int16)
    return y_new

def mulaw8k_to_pcm16_16k(b64_payload: str) -> bytes:
    mulaw = base64.b64decode(b64_payload)
    pcm8k = _ulaw_to_linear(mulaw)                   # int16 @ 8k
    pcm16k = _resample_linear(pcm8k, 8000, 16000)    # int16 @ 16k
    return pcm16k.tobytes()

def pcm16_16k_to_mulaw8k(pcm16k_bytes: bytes) -> str:
    pcm16k = np.frombuffer(pcm16k_bytes, dtype=np.int16)
    pcm8k = _resample_linear(pcm16k, 16000, 8000)
    mulaw = _linear_to_ulaw(pcm8k)
    return base64.b64encode(mulaw).decode("ascii")

# ---------- Nivel (RMS) ----------
def _pcm16_bytes_rms_norm_0_1(pcm16_bytes: bytes) -> float:
    if not pcm16_bytes:
        return 0.0
    arr = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32)
    if arr.size == 0:
        return 0.0
    arr /= 32768.0
    rms = float(np.sqrt(np.mean(arr * arr)))
    return max(0.0, min(1.0, rms))

# ---------- OpenAI Realtime (WebSocket) ----------
def _openai_ws_connect(model: str, instructions: str, voice: str, debug=False):
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no está configurada")

    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {api_key}",
        "OpenAI-Beta: realtime=v1"
    ]
    # Conexión con timeout y keepalive
    ws_ai = websocket.create_connection(url, header=headers, timeout=20)
    ws_ai.settimeout(5.0)  # evita bloqueos eternos en recv
    if debug:
        print(f"[AI ] WS connected model={model} voice={voice}")

    # Configuración de sesión
    try:
        ws_ai.send(json.dumps({
            "type": "session.update",
            "session": {
                "instructions": instructions or "",
                "voice": voice or "alloy",
                "modalities": ["audio", "text"],
                "input_audio_format":  "pcm16",
                "output_audio_format": "pcm16"
            }
        }))
        if debug:
            print("[AI ] session.update sent")
    except Exception as e:
        print(f"[AI ] session.update error: {e}")
        raise

    # Keepalive cada 15s (evita timeouts en rutas lentas)
    def _ai_keepalive():
        try:
            while True:
                time.sleep(15)
                try:
                    ws_ai.ping()
                except Exception:
                    break
        except Exception:
            pass

    threading.Thread(target=_ai_keepalive, daemon=True).start()
    return ws_ai

# ---------- TwiML inicial ----------
@bp.route("/call", methods=["POST"])
def call_entry():
    to_number_raw = request.values.get("To", "")
    to_number = _canonize_phone(to_number_raw)

    bots = current_app.config.get("BOTS_CONFIG") or {}
    _ = _get_bot_cfg_by_any_number(bots, to_number) or {}

    # ❌ Sin <Say>: el saludo lo hará OpenAI
    resp = VoiceResponse()

    ws_base = request.url_root.replace("http", "ws").rstrip("/") + "/voice-webrtc/stream"
    qs = urlencode({"to": to_number})
    ws_url = f"{ws_base}?{qs}"

    with resp.connect() as conn:
        conn.stream(url=ws_url)
    return Response(str(resp), mimetype="text/xml")

# ---------- Endpoint WebSocket Twilio <-> OpenAI ----------
try:
    from flask_sock import Sock
    sock = Sock()
except Exception:
    sock = None

if sock:
    @sock.route("/voice-webrtc/stream")
    def stream_ws(ws):
        # ---- Config ----
        to_number = request.args.get("to", "")
        bots = current_app.config.get("BOTS_CONFIG") or {}
        cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}
        model = (cfg.get("realtime") or {}).get("model") or os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
        voice = (cfg.get("realtime") or {}).get("voice") or os.getenv("REALTIME_VOICE", "alloy")
        instructions = (cfg.get("system_prompt") or cfg.get("prompt") or "")
        greet_text = cfg.get("greeting") or "Hola, gracias por llamar. ¿En qué puedo ayudarte?"

        # ---- OpenAI WS ----
        ai = _openai_ws_connect(model, instructions, voice, debug=True)

        stream_sid = None
        stop_flag = {"stop": False}

        have_appended_since_last_commit = {"v": False}
        appended_samples_since_last_commit = {"n": 0}
        last_commit_time = 0.0
        last_append_time = 0.0
        greeting_sent = False

        # ---- AI -> Twilio ----
        def pump_ai_to_twilio():
            nonlocal stream_sid
            try:
                while not stop_flag["stop"]:
                    try:
                        raw = ai.recv()
                    except Exception as e:
                        # Timeout o cierre: seguimos intentando hasta que pare la llamada
                        continue
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    t = msg.get("type")
                    if t == "response.audio.delta":
                        pcm16 = base64.b64decode(msg.get("audio", "") or b"")
                        if not pcm16:
                            continue

                        # Meter “meter” como mark para UI/depuración (opcional)
                        if stream_sid:
                            level = int(_pcm16_bytes_rms_norm_0_1(pcm16) * 100)
                            try:
                                ws.send(json.dumps({
                                    "event": "mark",
                                    "streamSid": stream_sid,
                                    "mark": {"name": f"meter:{level}"}
                                }))
                            except Exception:
                                pass

                        # A Twilio (μ-law 8k)
                        b64_ulaw = pcm16_16k_to_mulaw8k(pcm16)
                        if stream_sid:
                            ws.send(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": b64_ulaw}
                            }))

                    elif t == "error":
                        print(f"[AI ] ERROR: {msg}")
                    elif t in ("response.created", "response.completed", "session.updated"):
                        # Logs silenciosos
                        pass

            except Exception as e:
                print(f"[BRIDGE] AI->Twilio terminado: {e}")

        t_out = Thread(target=pump_ai_to_twilio, daemon=True)
        t_out.start()

        # Umbrales (≥100ms requerido por Realtime; usamos 200ms para ir seguros)
        MIN_COMMIT_GAP_SEC = 1.2      # no spamear
        MIN_SILENCE_GAP_SEC = 0.6     # silencio desde el último frame
        MIN_COMMIT_SAMPLES = 3200     # 200ms * 16k

        try:
            frames = 0
            while True:
                incoming = ws.receive()
                if incoming is None:
                    break
                try:
                    ev = json.loads(incoming)
                except Exception:
                    continue

                et = ev.get("event")
                if et == "start":
                    stream_sid = (ev.get("start") or {}).get("streamSid")
                    frames = 0
                    last_commit_time = 0.0
                    last_append_time = 0.0
                    have_appended_since_last_commit["v"] = False
                    appended_samples_since_last_commit["n"] = 0
                    ai.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    print(f"[CALL] start streamSid={stream_sid} to={to_number} loopback=False")

                    # ✅ Saludo inicial dicho por OpenAI (misma voz de la sesión)
                    if not greeting_sent:
                        ai.send(json.dumps({
                            "type": "response.create",
                            "response": {
                                "modalities": ["audio", "text"],
                                "instructions": greet_text
                            }
                        }))
                        greeting_sent = True

                elif et == "media":
                    payload = (ev.get("media") or {}).get("payload")
                    now = time.time()

                    if payload:
                        frames += 1
                        pcm16 = mulaw8k_to_pcm16_16k(payload)

                        # DEBUG de entrada
                        try:
                            lvl = _pcm16_bytes_rms_norm_0_1(pcm16)
                            if frames <= 5 or lvl > 0.02 or frames % 50 == 0:
                                print(f"[IN ] frames={frames} RMS={lvl:.3f}")
                        except:
                            pass

                        # Append al buffer Realtime
                        ai.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm16).decode("ascii")
                        }))
                        have_appended_since_last_commit["v"] = True
                        appended_samples_since_last_commit["n"] += len(pcm16) // 2
                        last_append_time = now

                    # Heurística de commit:
                    if have_appended_since_last_commit["v"]:
                        enough_audio = appended_samples_since_last_commit["n"] >= MIN_COMMIT_SAMPLES
                        long_enough_since_last_commit = (now - last_commit_time) >= MIN_COMMIT_GAP_SEC
                        silence_since_last_append = (now - last_append_time) >= MIN_SILENCE_GAP_SEC
                        if enough_audio and silence_since_last_append and long_enough_since_last_commit:
                            print(f"[COMMIT] samples={appended_samples_since_last_commit['n']} "
                                  f"silence={now - last_append_time:.3f}s gap={now - last_commit_time:.3f}s")
                            ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            ai.send(json.dumps({
                                "type": "response.create",
                                "response": { "modalities": ["audio", "text"] }
                            }))
                            last_commit_time = now
                            have_appended_since_last_commit["v"] = False
                            appended_samples_since_last_commit["n"] = 0

                elif et == "stop":
                    print("[CALL] stop")
                    break

        except Exception as e:
            print(f"[BRIDGE] WS error: {e}")
        finally:
            stop_flag["stop"] = True
            try:
                ai.close()
            except:
                pass
            try:
                ws.close()
            except:
                pass
else:
    @bp.route("/stream", methods=["GET"])
    def stream_ws_missing():
        return Response("❌ Falta dependencia: instala flask-sock y websocket-client", status=500)
