# voice_webrtc_bridge.py
# Bridge Twilio <Stream> (PCMU 8k) ↔ OpenAI Realtime WS (PCM16 16k)
# Sin 'audioop' (eliminado en Python 3.13). Usamos NumPy.
# + Emite “meter” del audio del BOT vía Twilio mark (no rompe el schema).
# + Debug: loopback (&loop=1), verbose (&debug=1), logs de eventos OpenAI.

import os, json, base64, time
from flask import Blueprint, request, Response, current_app
from twilio.twiml.voice_response import VoiceResponse
import websocket  # websocket-client
from threading import Thread
from urllib.parse import urlencode

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
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {os.getenv('OPENAI_API_KEY','')}",
        "OpenAI-Beta: realtime=v1"
    ]
    ws = websocket.create_connection(url, header=headers, timeout=20)
    if debug:
        print(f"[AI ] WS connected model={model} voice={voice}")
    ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": instructions or "",
            "voice": voice or "alloy",
            "modalities": ["audio", "text"],
            "input_audio_format":  { "type": "pcm16", "sample_rate": 16000 },
            "output_audio_format": { "type": "pcm16", "sample_rate": 16000 }
        }
    }))
    if debug:
        print("[AI ] session.update sent")
    return ws

# ---------- TwiML inicial ----------
@bp.route("/call", methods=["POST"])
def call_entry():
    to_number_raw = request.values.get("To", "")
    to_number = _canonize_phone(to_number_raw)

    bots = current_app.config.get("BOTS_CONFIG") or {}
    cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}

    greeting = cfg.get("greeting") or f"Hola, gracias por llamar a {cfg.get('business_name', cfg.get('name',''))}."
    resp = VoiceResponse()
    resp.say(greeting, voice="Polly.Conchita", language="es-ES")

    ws_base = request.url_root.replace("http", "ws").rstrip("/") + "/voice-webrtc/stream"
    # Puedes añadir &loop=1 o &debug=1 temporalmente
    qs = urlencode({"to": to_number})
    ws_url = f"{ws_base}?{qs}"

    with resp.connect() as conn:
        conn.stream(url=ws_url)
    return Response(str(resp), mimetype="text/xml")

# ---------- WS Media Stream ----------
try:
    from flask_sock import Sock
    sock = Sock()
except Exception:
    sock = None

if sock:
    @sock.route("/voice-webrtc/stream")
    def stream_ws(ws):
        """
        WebSocket bidireccional:
          - Recibe JSONs de Twilio (event=start/media/stop)
          - Envía 'media' con audio μ-law para Twilio
          - Bridge con OpenAI Realtime (WebSocket)
          - Emite “meter” del audio del BOT como mark (name="meter:NN")
          - Debug: loopback (&loop=1), verbose (&debug=1)
        """
        to_number = request.args.get("to", "")
        loopback = request.args.get("loop", "0") in ("1", "true", "yes")
        debug = request.args.get("debug", "0") in ("1", "true", "yes")

        bots = current_app.config.get("BOTS_CONFIG") or {}
        cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}
        model = (cfg.get("realtime") or {}).get("model") or os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
        voice = (cfg.get("realtime") or {}).get("voice") or os.getenv("REALTIME_VOICE", "alloy")
        instructions = (cfg.get("system_prompt") or cfg.get("prompt") or "")

        ai = None
        if not loopback:
            ai = _openai_ws_connect(model, instructions, voice, debug=debug)

        stream_sid = None
        stop_flag = {"stop": False}
        have_appended_since_last_commit = {"v": False}
        last_commit = 0.0
        last_commit_wait_start = 0.0  # para detectar si no llega audio del bot tras commit

        # ---------- AI -> Twilio ----------
        def pump_ai_to_twilio():
            nonlocal stream_sid, last_commit_wait_start
            if loopback or ai is None:
                return
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

                        if debug:
                            print(f"[AI ] audio.delta bytes={len(pcm16)}")

                        # meter del BOT
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

                        b64_ulaw = pcm16_16k_to_mulaw8k(pcm16)
                        if stream_sid:
                            ws.send(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": b64_ulaw}
                            }))

                    elif t in ("response.created", "response.completed", "session.updated"):
                        if debug:
                            print(f"[AI ] {t}")
                        if t == "response.created":
                            # arrancó generación → ya no estamos esperando
                            last_commit_wait_start = 0.0

                    elif t == "error":
                        # OpenAI Realtime error explícito
                        print(f"[AI ] ERROR: {msg}")

                    else:
                        if debug:
                            print(f"[AI ] evt {t}")

            except Exception as e:
                print(f"[BRIDGE] AI->Twilio terminado: {e}")

        t_out = Thread(target=pump_ai_to_twilio, daemon=True)
        t_out.start()

        MIN_COMMIT_GAP = 1.2  # s
        COMMIT_TIMEOUT_FOR_RETRY = 2.5  # s sin audio del bot tras commit → reintento suave

        try:
            if not loopback and ai is not None:
                if debug:
                    print("[AI ] pre-hello response.create")
                ai.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))

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
                    last_commit = 0.0
                    have_appended_since_last_commit["v"] = False
                    if debug:
                        print(f"[CALL] start streamSid={stream_sid} to={to_number} loopback={loopback} debug={debug}")
                    if not loopback and ai is not None:
                        ai.send(json.dumps({"type": "input_audio_buffer.clear"}))

                elif et == "media":
                    payload = (ev.get("media") or {}).get("payload")
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

                        if loopback:
                            # ECO
                            b64_ulaw_in = pcm16_16k_to_mulaw8k(pcm16)
                            if stream_sid:
                                ws.send(json.dumps({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": b64_ulaw_in}
                                }))
                        else:
                            # OpenAI input
                            ai.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm16).decode("ascii")
                            }))
                            have_appended_since_last_commit["v"] = True

                    now = time.time()
                    # Heurística de tiempo para "commit"
                    if (not loopback) and have_appended_since_last_commit["v"] and (now - last_commit > MIN_COMMIT_GAP):
                        if debug:
                            print("[AI ] COMMIT + response.create")
                        ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        ai.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))
                        last_commit = now
                        last_commit_wait_start = now
                        have_appended_since_last_commit["v"] = False

                    # Reintento suave si no llega audio del bot tras commit
                    if (not loopback) and last_commit_wait_start and (now - last_commit_wait_start > COMMIT_TIMEOUT_FOR_RETRY):
                        if debug:
                            print("[AI ] retry response.create (no audio yet)")
                        ai.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))
                        last_commit_wait_start = now  # reinicia espera

                elif et == "stop":
                    if debug:
                        print("[CALL] stop")
                    break

        except Exception as e:
            print(f"[BRIDGE] WS error: {e}")
        finally:
            stop_flag["stop"] = True
            try:
                if ai is not None:
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
