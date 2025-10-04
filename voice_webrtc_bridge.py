# voice_webrtc_bridge.py
# Bridge Twilio <Stream> (PCMU 8k) ↔ OpenAI Realtime WS (PCM16 16k)
# Sin 'audioop' (eliminado en Python 3.13). Usamos NumPy.

import os, json, base64, time
from flask import Blueprint, request, Response, current_app, url_for
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import websocket  # websocket-client
from threading import Thread

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

# Constantes μ-law
_MU = 255.0
_BIAS = 132.0

def _ulaw_to_linear(ulaw_bytes: bytes) -> np.ndarray:
    """Convierte bytes μ-law -> PCM16 (np.int16), a 8 kHz."""
    # tabla vectorizada
    u = np.frombuffer(ulaw_bytes, dtype=np.uint8)
    u = ~u  # complemento a 1
    sign = (u & 0x80) != 0
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa.astype(np.int32) << 3) + 0x84) << exponent
    x = magnitude - 0x84
    pcm = x.astype(np.int32)
    pcm[sign] = -pcm[sign]
    # limitar a 16 bits
    pcm = np.clip(pcm, -32768, 32767).astype(np.int16)
    return pcm

def _linear_to_ulaw(pcm16: np.ndarray) -> bytes:
    """Convierte PCM16 (np.int16) -> μ-law bytes."""
    x = pcm16.astype(np.int32)
    sign = (x < 0)
    x = np.abs(x)
    x = np.clip(x + 0x84, 0, 0x7FFF)

    # calcular exponente (posición MSB)
    def _msb_index(v):
        # índice del bit más significativo (0..15); evitamos bucles
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
    # indices de destino en origen
    x_old = np.arange(pcm.size)
    x_new = np.linspace(0, pcm.size - 1, num=n_dst)
    y_new = np.interp(x_new, x_old, pcm.astype(np.float32))
    # clamp y convertir a int16
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

# ---------- OpenAI Realtime (WebSocket) ----------
def _openai_ws_connect(model: str, instructions: str, voice: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {os.getenv('OPENAI_API_KEY','')}",
        "OpenAI-Beta: realtime=v1"
    ]
    ws = websocket.create_connection(url, header=headers, timeout=20)
    # Enviar sesión inicial
    ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": instructions or "",
            "voice": voice or "alloy",
            "modalities": ["audio", "text"],
        }
    }))
    return ws

# ---------- TwiML inicial: conecta el Stream ----------
@bp.route("/call", methods=["POST"])
def call_entry():
    """Twilio Voice webhook que inicia un <Connect><Stream> hacia nuestro WS."""
    to_number = request.values.get("To", "")
    bots = current_app.config.get("BOTS_CONFIG") or {}
    cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}

    greeting = cfg.get("greeting") or f"Hola, gracias por llamar a {cfg.get('business_name', cfg.get('name',''))}."
    resp = VoiceResponse()
    # Saludo breve para evitar silencio inicial (lo genera Twilio, no TTS)
    resp.say(greeting, voice="Polly.Conchita", language="es-ES")

    # URL WSS para Twilio -> nuestro WS endpoint
    ws_url = request.url_root.replace("http", "ws").rstrip("/") + url_for("voice_webrtc.stream_ws")
    ws_url += f"?to={to_number}"

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
        """
        # ---- Resolver bot/config por número ----
        to_number = request.args.get("to", "")
        bots = current_app.config.get("BOTS_CONFIG") or {}
        cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}
        model = (cfg.get("realtime") or {}).get("model") or os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
        voice = (cfg.get("realtime") or {}).get("voice") or os.getenv("REALTIME_VOICE", "alloy")
        instructions = (cfg.get("system_prompt") or cfg.get("prompt") or "")

        # ---- Abrir WS con OpenAI Realtime ----
        ai = _openai_ws_connect(model, instructions, voice)

        stream_sid = None
        stop_flag = {"stop": False}

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
                    if msg.get("type") == "output_audio.delta":
                        pcm16 = base64.b64decode(msg.get("audio", "") or b"")
                        if pcm16:
                            b64_ulaw = pcm16_16k_to_mulaw8k(pcm16)
                            if stream_sid:
                                ws.send(json.dumps({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": b64_ulaw}
                                }))
            except Exception as e:
                print(f"[BRIDGE] AI->Twilio terminado: {e}")

        t_out = Thread(target=pump_ai_to_twilio, daemon=True)
        t_out.start()

        last_commit = 0.0

        try:
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
                    ai.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    last_commit = 0.0

                elif et == "media":
                    payload = (ev.get("media") or {}).get("payload")
                    if payload:
                        pcm16 = mulaw8k_to_pcm16_16k(payload)
                        ai.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm16).decode("ascii")
                        }))
                    # Heurística sencilla de VAD por tiempo
                    now = time.time()
                    if now - last_commit > 1.2:
                        ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        ai.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))
                        last_commit = now

                elif et == "stop":
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
