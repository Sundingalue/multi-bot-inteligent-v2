# voice_webrtc_bridge.py
# Bridge Twilio <Stream> (PCMU 8k) ↔ OpenAI Realtime WS (PCM16 16k)
# Sin 'audioop' (eliminado en Python 3.13). Usamos NumPy.
# - El saludo inicial lo habla OpenAI (no Twilio).
# - “meter” del audio BOT vía Twilio mark (no rompe el schema).
# - Formatos Realtime correctos (pcm16 in/out), commit solo con >100ms audio real.
# - VAD por silencio (simple), logs útiles y keepalive al WS Realtime.
# - BLOQUEOS CLAVE: sin commits por tiempo y una sola respuesta activa (ai_busy).

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

BRIDGE_VERSION = "webrtc-bridge/1.0.3-no-time-commit"

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
    ws_ai = websocket.create_connection(url, header=headers, timeout=20)
    if debug:
        print(f"[AI ] WS connected model={model} voice={voice}  [{BRIDGE_VERSION}]")

    # Configuración inicial de sesión (se puede actualizar luego)
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

    # Sin <Say>: el saludo lo hará OpenAI
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
        # ---- Config base (puede ajustarse al recibir 'start') ----
        to_number_qs = request.args.get("to", "")
        bots = current_app.config.get("BOTS_CONFIG") or {}
        cfg = _get_bot_cfg_by_any_number(bots, to_number_qs) or {}
        model = (cfg.get("realtime") or {}).get("model") or os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
        voice = (cfg.get("realtime") or {}).get("voice") or os.getenv("REALTIME_VOICE", "alloy")
        instructions = (cfg.get("system_prompt") or cfg.get("prompt") or "")
        greet_text = cfg.get("greeting") or "Hola, gracias por llamar. ¿En qué puedo ayudarte?"

        print(f"[BOOT] {BRIDGE_VERSION} up — model={model} voice={voice}")

        # ---- OpenAI WS ----
        ai = _openai_ws_connect(model, instructions, voice, debug=True)

        stream_sid = None
        stop_flag = {"stop": False}

        have_appended_since_last_commit = {"v": False}
        appended_samples_since_last_commit = {"n": 0}
        last_commit_time = 0.0
        last_append_time = 0.0
        greeting_sent = False

        # Gate para UNA respuesta a la vez
        ai_busy = {"v": False}

        # ---- AI -> Twilio ----
        out_frames = 0
        def pump_ai_to_twilio():
            nonlocal stream_sid, out_frames
            try:
                while not stop_flag["stop"]:
                    raw = ai.recv()
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

                        # Meter “meter” como mark (opcional)
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
                            out_frames += 1
                            if out_frames in (1, 50, 200) or out_frames % 200 == 0:
                                print(f"[OUT] frames={out_frames}")

                    elif t in ("response.completed", "response.stopped"):
                        ai_busy["v"] = False
                        # print("[AI ] response completed")

                    elif t == "error":
                        # No cambiamos ai_busy aquí; el modelo gestiona su estado
                        print(f"[AI ] ERROR: {msg}")

                    # silenciar logs de 'response.created', 'session.updated' para no saturar
            except Exception as e:
                print(f"[BRIDGE] AI->Twilio terminado: {e}")

        t_out = Thread(target=pump_ai_to_twilio, daemon=True)
        t_out.start()

        # Umbrales (Realtime exige ≥100ms; usamos 200ms para ir seguros)
        MIN_COMMIT_GAP_SEC = 1.2      # evita spam
        MIN_SILENCE_GAP_SEC = 0.6     # silencio desde último frame
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
                    start_obj = ev.get("start") or {}
                    stream_sid = start_obj.get("streamSid")

                    # (customParameters desde Twilio <Stream>) — opcional
                    custom = (start_obj.get("customParameters") or {})
                    to_p = custom.get("to_number") or to_number_qs or ""
                    bot_hint = (custom.get("bot_hint") or "").strip().lower()

                    # Intentamos refrescar config de bot por nombre/numero
                    cfg2 = None
                    if bot_hint:
                        for c in (bots or {}).values():
                            if isinstance(c, dict) and c.get("name", "").strip().lower() == bot_hint:
                                cfg2 = c
                                break
                    if not cfg2:
                        cfg2 = _get_bot_cfg_by_any_number(bots, to_p) or cfg

                    # Si hay cambios relevantes, actualizar la sesión:
                    new_instructions = (cfg2.get("system_prompt") or cfg2.get("prompt") or "") if isinstance(cfg2, dict) else instructions
                    new_voice = (cfg2.get("realtime") or {}).get("voice") if isinstance(cfg2, dict) else None
                    if new_instructions != instructions or (new_voice and new_voice != voice):
                        try:
                            ai.send(json.dumps({
                                "type": "session.update",
                                "session": {
                                    "instructions": new_instructions or "",
                                    "voice": (new_voice or voice or "alloy"),
                                }
                            }))
                            instructions = new_instructions or instructions
                            voice = new_voice or voice
                            print("[AI ] session.update (customParameters) applied")
                        except Exception as e:
                            print(f"[AI ] session.update error: {e}")

                    # Reset de contadores
                    frames = 0
                    last_commit_time = 0.0
                    last_append_time = 0.0
                    have_appended_since_last_commit["v"] = False
                    appended_samples_since_last_commit["n"] = 0
                    ai_busy["v"] = False
                    try:
                        ai.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    except Exception:
                        pass
                    print(f"[CALL] start streamSid={stream_sid} to={to_p} [{BRIDGE_VERSION}]")

                    # Saludo inicial por OpenAI (una sola vez)
                    if not greeting_sent:
                        try:
                            greet_text2 = (cfg2.get("greeting") or greet_text) if isinstance(cfg2, dict) else greet_text
                            ai.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "modalities": ["audio", "text"],
                                    "instructions": greet_text2
                                }
                            }))
                            ai_busy["v"] = True
                            greeting_sent = True
                        except Exception as e:
                            print(f"[AI ] greet error: {e}")

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
                        try:
                            ai.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm16).decode("ascii")
                            }))
                            have_appended_since_last_commit["v"] = True
                            appended_samples_since_last_commit["n"] += len(pcm16) // 2  # muestras int16
                            last_append_time = now
                        except Exception as e:
                            print(f"[AI ] append error: {e}")

                    # Heurística de commit (SIN fallback por tiempo)
                    if have_appended_since_last_commit["v"] and not ai_busy["v"]:
                        enough_audio = appended_samples_since_last_commit["n"] >= MIN_COMMIT_SAMPLES
                        long_enough_since_last_commit = (now - last_commit_time) >= MIN_COMMIT_GAP_SEC
                        silence_since_last_append = (now - last_append_time) >= MIN_SILENCE_GAP_SEC

                        if enough_audio and long_enough_since_last_commit and silence_since_last_append:
                            print(f"[COMMIT] samples={appended_samples_since_last_commit['n']} "
                                  f"since_last_append={now - last_append_time:.3f}s "
                                  f"gap={now - last_commit_time:.3f}s")
                            try:
                                ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                ai.send(json.dumps({
                                    "type": "response.create",
                                    "response": { "modalities": ["audio", "text"] }
                                }))
                                ai_busy["v"] = True
                            except Exception as e:
                                print(f"[AI ] commit error: {e}")

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
