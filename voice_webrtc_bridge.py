# voice_webrtc_bridge.py
# Bridge Twilio <Stream> (PCMU 8k) ↔ OpenAI Realtime WS (PCM16 16k)
# Sin 'audioop' (eliminado en Python 3.13). Usamos NumPy.
# + Emite “meter” del audio del BOT vía Twilio mark (no rompe el schema).
# + Fixes: formatos Realtime, modalidades, y commit con >100ms de audio.

import os, json, base64, time
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

# ---------- Utils: resolvemos bot por número desde bots_config ya cargado en main ----------
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

# ---------- μ-law <-> PCM16 y resampling (NumPy) ----------
def _ulaw_to_linear(ulaw_bytes: bytes) -> np.ndarray:
    """Convierte bytes μ-law -> PCM16 (np.int16), a 8 kHz."""
    u = np.frombuffer(ulaw_bytes, dtype=np.uint8)
    u = ~u  # complemento a 1
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
    """Convierte PCM16 (np.int16) -> μ-law bytes."""
    x = pcm16.astype(np.int32)
    sign = (x < 0)
    x = np.abs(x)
    x = np.clip(x + 0x84, 0, 0x7FFF)

    # índice MSB aproximado sin bucles costosos
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
    """Re-muestreo por interpolación lineal (mono)."""
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
    """Twilio -> OpenAI: μ-law 8k (base64) -> PCM16 16k (bytes)."""
    mulaw = base64.b64decode(b64_payload)
    pcm8k = _ulaw_to_linear(mulaw)                   # int16 @ 8k
    pcm16k = _resample_linear(pcm8k, 8000, 16000)    # int16 @ 16k
    return pcm16k.tobytes()

def pcm16_16k_to_mulaw8k(pcm16k_bytes: bytes) -> str:
    """OpenAI -> Twilio: PCM16 16k (bytes) -> μ-law 8k (base64)."""
    pcm16k = np.frombuffer(pcm16k_bytes, dtype=np.int16)
    pcm8k = _resample_linear(pcm16k, 16000, 8000)
    mulaw = _linear_to_ulaw(pcm8k)
    return base64.b64encode(mulaw).decode("ascii")

# ---------- Nivel (RMS) para “esfera” ----------
def _pcm16_bytes_rms_norm_0_1(pcm16_bytes: bytes) -> float:
    """RMS normalizado (0..1) para PCM16LE mono."""
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
    # FIX: formatos deben ser string, no objeto
    ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": instructions or "",
            "voice": voice or "alloy",
            "modalities": ["audio", "text"],
            "input_audio_format":  "pcm16",   # <-- string, no objeto
            "output_audio_format": "pcm16"    # <-- string, no objeto
        }
    }))
    if debug:
        print("[AI ] session.update sent")
    return ws

# ---------- TwiML inicial: conecta el Stream ----------
@bp.route("/call", methods=["POST"])
def call_entry():
    """Twilio Voice webhook que inicia un <Connect><Stream> hacia nuestro WS."""
    to_number_raw = request.values.get("To", "")
    to_number = _canonize_phone(to_number_raw)  # normalizar a +E.164

    bots = current_app.config.get("BOTS_CONFIG") or {}
    cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}

    greeting = cfg.get("greeting") or f"Hola, gracias por llamar a {cfg.get('business_name', cfg.get('name',''))}."
    resp = VoiceResponse()
    # Saludo breve para evitar silencio inicial (lo genera Twilio, no TTS)
    resp.say(greeting, voice="Polly.Conchita", language="es-ES")

    # URL WSS para Twilio -> nuestro WS endpoint (con '+ ' URL-encoded)
    ws_base = request.url_root.replace("http", "ws").rstrip("/") + "/voice-webrtc/stream"
    qs = urlencode({"to": to_number})
    ws_url = f"{ws_base}?{qs}"

    with resp.connect() as conn:
        conn.stream(url=ws_url)
    return Response(str(resp), mimetype="text/xml")

# ---------- Endpoint WebSocket que recibe el Media Stream de Twilio ----------
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
          - Envía 'media' con audio μ-law para que Twilio lo reproduzca
          - Bridge con OpenAI Realtime (WebSocket)
          - Emite “meter” del audio del BOT como mark (name="meter:NN")
        """
        # ---- Resolver bot/config por número ----
        to_number = request.args.get("to", "")
        bots = current_app.config.get("BOTS_CONFIG") or {}
        cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}
        model = (cfg.get("realtime") or {}).get("model") or os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
        voice = (cfg.get("realtime") or {}).get("voice") or os.getenv("REALTIME_VOICE", "alloy")
        instructions = (cfg.get("system_prompt") or cfg.get("prompt") or "")

        # ---- Abrir WS con OpenAI Realtime ----
        ai = _openai_ws_connect(model, instructions, voice, debug=True)

        stream_sid = None
        stop_flag = {"stop": False}
        have_appended_since_last_commit = {"v": False}
        appended_samples_since_last_commit = {"n": 0}   # <-- acumulador
        last_commit = 0.0

        # Bomba: OpenAI -> Twilio
        def pump_ai_to_twilio():
            nonlocal stream_sid
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
                        # Audio del BOT (PCM16 16k, base64)
                        pcm16 = base64.b64decode(msg.get("audio", "") or b"")
                        if not pcm16:
                            continue

                        # Emite nivel (0..100) como mark
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

                        # Convertir a μ-law 8k y mandar a Twilio para reproducir
                        b64_ulaw = pcm16_16k_to_mulaw8k(pcm16)
                        if stream_sid:
                            ws.send(json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": b64_ulaw}
                            }))

                    elif t in ("response.created", "response.completed", "session.updated"):
                        # opcional: prints
                        # print(f"[AI ] {t}")
                        pass

                    elif t == "error":
                        # Log de errores Realtime
                        print(f"[AI ] ERROR: {msg}")

            except Exception as e:
                print(f"[BRIDGE] AI->Twilio terminado: {e}")

        t_out = Thread(target=pump_ai_to_twilio, daemon=True)
        t_out.start()

        MIN_COMMIT_GAP_SEC = 1.2
        MIN_COMMIT_SAMPLES = 1600  # 100ms * 16kHz

        try:
            # Disparo inicial de bienvenida (modalidades válidas)
            ai.send(json.dumps({
                "type": "response.create",
                "response": { "modalities": ["audio", "text"] }
            }))

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
                    appended_samples_since_last_commit["n"] = 0
                    # limpiar buffer de entrada en OpenAI
                    ai.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    print(f"[CALL] start streamSid={stream_sid} to={to_number} loopback=False")

                elif et == "media":
                    payload = (ev.get("media") or {}).get("payload")
                    if payload:
                        frames += 1
                        # Caller → OpenAI
                        pcm16 = mulaw8k_to_pcm16_16k(payload)

                        # DEBUG de entrada
                        try:
                            lvl = _pcm16_bytes_rms_norm_0_1(pcm16)
                            if frames <= 5 or lvl > 0.02 or frames % 50 == 0:
                                print(f"[IN ] frames={frames} RMS={lvl:.3f}")
                        except:
                            pass

                        ai.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm16).decode("ascii")
                        }))
                        have_appended_since_last_commit["v"] = True
                        # acumular muestras (2 bytes por muestra int16)
                        appended_samples_since_last_commit["n"] += len(pcm16) // 2

                    # Heurística por tiempo + tamaño mínimo de buffer
                    now = time.time()
                    if have_appended_since_last_commit["v"]:
                        enough_time = (now - last_commit) > MIN_COMMIT_GAP_SEC
                        enough_audio = appended_samples_since_last_commit["n"] >= MIN_COMMIT_SAMPLES
                        if enough_time and enough_audio:
                            ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            ai.send(json.dumps({
                                "type": "response.create",
                                "response": { "modalities": ["audio", "text"] }
                            }))
                            last_commit = now
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
