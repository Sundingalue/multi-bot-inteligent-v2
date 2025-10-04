# voice_webrtc_bridge.py — Twilio <Stream> (PCMU 8k) ↔ OpenAI Realtime (PCM16 16k)
# Fix crítico: VAD por RMS + fallback temporal para asegurar commits.
# Protecciones: no-commit en frío, control de respuesta activa, sample_rate_hz=16000.

import os, json, base64, time, threading
from flask import Blueprint, request, Response, current_app
from twilio.twiml.voice_response import VoiceResponse
import websocket
from threading import Thread
from urllib.parse import urlencode

try:
    import numpy as np
except Exception as e:
    raise RuntimeError("Falta NumPy. Agrega 'numpy' a requirements.txt") from e

bp = Blueprint("voice_webrtc", __name__, url_prefix="/voice-webrtc")

# ---------- Utils ----------
def _canonize_phone(raw: str) -> str:
    s = str(raw or "").strip()
    for p in ("whatsapp:", "tel:", "sip:", "client:"):
        if s.startswith(p): s = s[len(p):]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits: return ""
    if len(digits) == 11 and digits.startswith("1"): return "+" + digits
    if len(digits) == 10: digits = "1" + digits
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
        idx = np.zeros_like(v); vv = v.copy()
        for shift in [8,4,2,1]:
            mask = vv >= (1<<shift)
            idx[mask] += shift
            vv[mask] >>= shift
        return idx
    exp = _msb_index(x >> 7)
    mant = (x >> (exp + 3)) & 0x0F
    ulaw = (~((sign.astype(np.uint8) << 7) | (exp << 4) | mant)) & 0xFF
    return ulaw.tobytes()

def _resample_linear(pcm: np.ndarray, sr_src: int, sr_dst: int) -> np.ndarray:
    if sr_src == sr_dst or pcm.size == 0: return pcm
    ratio = sr_dst / float(sr_src)
    n_dst = int(round(pcm.size * ratio))
    x_old = np.arange(pcm.size)
    x_new = np.linspace(0, pcm.size - 1, num=n_dst)
    y_new = np.interp(x_new, x_old, pcm.astype(np.float32))
    y_new = np.clip(np.round(y_new), -32768, 32767).astype(np.int16)
    return y_new

def mulaw8k_to_pcm16_16k(b64_payload: str) -> bytes:
    mulaw = base64.b64decode(b64_payload)
    pcm8k = _ulaw_to_linear(mulaw)
    pcm16k = _resample_linear(pcm8k, 8000, 16000)
    return pcm16k.tobytes()

def pcm16_16k_to_mulaw8k(pcm16k_bytes: bytes) -> str:
    pcm16k = np.frombuffer(pcm16k_bytes, dtype=np.int16)
    pcm8k = _resample_linear(pcm16k, 16000, 8000)
    mulaw = _linear_to_ulaw(pcm8k)
    return base64.b64encode(mulaw).decode("ascii")

# ---------- Nivel (RMS) ----------
def _pcm16_bytes_rms_norm_0_1(pcm16_bytes: bytes) -> float:
    if not pcm16_bytes: return 0.0
    arr = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32)
    if arr.size == 0: return 0.0
    arr /= 32768.0
    rms = float(np.sqrt(np.mean(arr * arr)))
    return max(0.0, min(1.0, rms))

# ---------- OpenAI Realtime WS ----------
def _openai_ws_connect(model: str, instructions: str, voice: str, debug=False):
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key: raise RuntimeError("OPENAI_API_KEY no está configurada")
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [f"Authorization: Bearer {api_key}", "OpenAI-Beta: realtime=v1"]
    ws_ai = websocket.create_connection(url, header=headers, timeout=20)
    if debug: print(f"[AI ] WS connected model={model} voice={voice}  [webrtc-bridge/1.0.4-vad]")
    ws_ai.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": instructions or "",
            "voice": voice or "alloy",
            "modalities": ["audio", "text"],
            "input_audio_format":  "pcm16",
            "output_audio_format": "pcm16",
            "sample_rate_hz": 16000
        }
    }))
    if debug: print("[AI ] session.update sent")

    def _ai_keepalive():
        try:
            while True:
                time.sleep(15)
                try: ws_ai.ping()
                except Exception: break
        except Exception: pass

    threading.Thread(target=_ai_keepalive, daemon=True).start()
    return ws_ai

# ---------- TwiML inicial ----------
@bp.route("/call", methods=["POST"])
def call_entry():
    to_number_raw = request.values.get("To", "")
    to_number = _canonize_phone(to_number_raw)
    resp = VoiceResponse()
    ws_base = request.url_root.replace("http", "ws").rstrip("/") + "/voice-webrtc/stream"
    qs = urlencode({"to": to_number})
    ws_url = f"{ws_base}?{qs}"
    with resp.connect() as conn:
     with conn.stream(url=ws_url) as s:
        s.parameter(name="to_number",   value=(request.values.get("To") or ""))
        s.parameter(name="from_number", value=(request.values.get("From") or ""))
        # Opcional: fija el bot por nombre si quieres:
        # s.parameter(name="bot_hint", value="ninafit")

    return Response(str(resp), mimetype="text/xml")

# ---------- WebSocket Twilio <-> OpenAI ----------
try:
    from flask_sock import Sock
    sock = Sock()
except Exception:
    sock = None

if sock:
    @sock.route("/voice-webrtc/stream")
    def stream_ws(ws):
        # Config
        to_number_qs = request.args.get("to", "")
        bots = current_app.config.get("BOTS_CONFIG") or {}
        cfg = _get_bot_cfg_by_any_number(bots, to_number_qs) or {}
        model = (cfg.get("realtime") or {}).get("model") or os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
        voice = (cfg.get("realtime") or {}).get("voice") or os.getenv("REALTIME_VOICE", "alloy")
        instructions = (cfg.get("system_prompt") or cfg.get("prompt") or "")
        greet_text = cfg.get("greeting") or "Hola, gracias por llamar. ¿En qué puedo ayudarte?"

        ai = _openai_ws_connect(model, instructions, voice, debug=True)

        stream_sid = None
        stop_flag = {"stop": False}

        # Estado buffer
        have_appended_since_last_commit = {"v": False}
        appended_voice_samples = {"n": 0}   # solo voz (no silencio)
        last_commit_time = time.time()
        last_voice_time = 0.0               # última vez que DETECTAMOS VOZ
        greeting_sent = False

        # Control respuesta activa
        active_response = {"on": False}

        # VAD params
        VOICE_RMS_TH = 0.02     # ≥ ~ -34 dBFS considera voz
        SILENCE_RMS_TH = 0.008  # < ~ -42 dBFS considera silencio
        # Timings
        MIN_COMMIT_GAP_SEC   = 1.0
        MIN_SILENCE_GAP_SEC  = 0.45
        MIN_COMMIT_SAMPLES   = 1600      # 100 ms @ 16k
        HARD_COMMIT_EVERY_SEC= 2.5

        # ---- AI -> Twilio ----
        def pump_ai_to_twilio():
            nonlocal stream_sid
            try:
                while not stop_flag["stop"]:
                    raw = ai.recv()
                    if not raw: continue
                    try: msg = json.loads(raw)
                    except Exception: continue
                    t = msg.get("type")

                    if t == "response.audio.delta":
                        pcm16 = base64.b64decode(msg.get("audio", "") or b"")
                        if not pcm16: continue
                        if stream_sid:
                            level = int(_pcm16_bytes_rms_norm_0_1(pcm16) * 100)
                            try:
                                ws.send(json.dumps({
                                    "event": "mark",
                                    "streamSid": stream_sid,
                                    "mark": {"name": f"meter:{level}"}
                                }))
                            except Exception: pass
                            b64_ulaw = pcm16_16k_to_mulaw8k(pcm16)
                            ws.send(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": b64_ulaw}
                            }))

                    elif t == "response.created":
                        active_response["on"] = True
                    elif t == "response.completed":
                        active_response["on"] = False
                    elif t == "error":
                        print(f"[AI ] ERROR: {msg}")
                        active_response["on"] = False
            except Exception as e:
                print(f"[BRIDGE] AI->Twilio terminado: {e}")

        Thread(target=pump_ai_to_twilio, daemon=True).start()

        try:
            frames = 0
            while True:
                incoming = ws.receive()
                if incoming is None: break
                try: ev = json.loads(incoming)
                except Exception: continue

                et = ev.get("event")

                if et == "start":
                    stream_sid = (ev.get("start") or {}).get("streamSid")
                    frames = 0
                    last_commit_time = time.time()
                    last_voice_time = 0.0
                    have_appended_since_last_commit["v"] = False
                    appended_voice_samples["n"] = 0
                    active_response["on"] = False
                    try: ai.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    except Exception: pass
                    print(f"[CALL] start streamSid={stream_sid} [webrtc-bridge/1.0.4-vad]")

                    if not greeting_sent and not active_response["on"]:
                        try:
                            ai.send(json.dumps({
                                "type": "response.create",
                                "response": {"modalities": ["audio", "text"], "instructions": greet_text}
                            }))
                            greeting_sent = True
                            active_response["on"] = True
                        except Exception as e:
                            print(f"[AI ] greet error: {e}")
                            active_response["on"] = False

                elif et == "media":
                    payload = (ev.get("media") or {}).get("payload")
                    now = time.time()
                    if payload:
                        frames += 1
                        pcm16 = mulaw8k_to_pcm16_16k(payload)
                        rms = _pcm16_bytes_rms_norm_0_1(pcm16)

                        # VAD: solo consideramos VOZ si RMS supera VOICE_RMS_TH
                        is_voice = rms >= VOICE_RMS_TH

                        # Append SI: hay voz O queremos conservar algo de contexto en onset
                        # (para simplicidad, aquí solo apendemos voz: evita que el "silencio con payload"
                        #  actualice last_voice_time e impida commits)
                        if is_voice:
                            try:
                                ai.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm16).decode("ascii")
                                }))
                                have_appended_since_last_commit["v"] = True
                                appended_voice_samples["n"] += len(pcm16) // 2
                                last_voice_time = now
                            except Exception as e:
                                print(f"[AI ] append error: {e}")
                        # si NO voz: no apendemos ni tocamos last_voice_time

                    # Heurística de commit (requiere que haya habido append de VOZ)
                    if have_appended_since_last_commit["v"]:
                        enough_audio = appended_voice_samples["n"] >= MIN_COMMIT_SAMPLES
                        long_gap = (now - last_commit_time) >= MIN_COMMIT_GAP_SEC
                        silence_ok = (last_voice_time > 0) and ((now - last_voice_time) >= MIN_SILENCE_GAP_SEC)
                        time_fallback = (now - last_commit_time) >= HARD_COMMIT_EVERY_SEC

                        if enough_audio and long_gap and (silence_ok or time_fallback):
                            try:
                                ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                if not active_response["on"]:
                                    ai.send(json.dumps({
                                        "type": "response.create",
                                        "response": {"modalities": ["audio", "text"]}
                                    }))
                                    active_response["on"] = True
                                print(f"[COMMIT] voice_samples={appended_voice_samples['n']} "
                                      f"silence={(now - last_voice_time) if last_voice_time>0 else -1:.3f}s "
                                      f"gap={now - last_commit_time:.3f}s "
                                      f"fallback={'YES' if time_fallback and not silence_ok else 'NO'}")
                            except Exception as e:
                                print(f"[AI ] commit/response error: {e}")

                            last_commit_time = now
                            have_appended_since_last_commit["v"] = False
                            appended_voice_samples["n"] = 0

                elif et == "stop":
                    print("[CALL] stop")
                    break

        except Exception as e:
            print(f"[BRIDGE] WS error: {e}")
        finally:
            stop_flag["stop"] = True
            try: ai.close()
            except: pass
            try: ws.close()
            except: pass
else:
    @bp.route("/stream", methods=["GET"])
    def stream_ws_missing():
        return Response("❌ Falta dependencia: instala flask-sock y websocket-client", status=500)
