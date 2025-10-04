# voice_webrtc_bridge.py
import os, json, base64, audioop, time
from flask import Blueprint, request, Response, current_app, url_for
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import websocket  # websocket-client
from threading import Thread

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

# ---------- Conversión de audio ----------
# Twilio envía 8kHz μ-law; OpenAI WS Realtime espera PCM16 (recomendado 16k).
# Usamos audioop (stdlib) para μ-law<->lin16 y ratecv para 8k→16k y 16k→8k.
def mulaw8k_to_pcm16_16k(b64_payload: str) -> bytes:
    mulaw = base64.b64decode(b64_payload)
    pcm8k = audioop.ulaw2lin(mulaw, 2)                   # μ-law -> PCM16 @ 8k
    pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)  # 8k -> 16k
    return pcm16k

def pcm16_16k_to_mulaw8k(pcm16k: bytes) -> str:
    pcm8k, _ = audioop.ratecv(pcm16k, 2, 1, 16000, 8000, None)  # 16k -> 8k
    mulaw = audioop.lin2ulaw(pcm8k, 2)
    return base64.b64encode(mulaw).decode("ascii")

# ---------- OpenAI Realtime (WebSocket) ----------
def _openai_ws_connect(model: str, instructions: str, voice: str):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = [
        f"Authorization: Bearer {os.getenv('OPENAI_API_KEY','')}",
        "OpenAI-Beta: realtime=v1"
    ]
    ws = websocket.create_connection(url, header=headers, timeout=20)
    # Enviamos las instrucciones y voz preferida
    ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": instructions or "",
            "voice": voice or "alloy",
            # Generaremos audio de salida
            "modalities": ["audio", "text"]
        }
    }))
    return ws

# Bombas simples para lectura de salida de OpenAI y envío a Twilio
def _pump_openai_to_twilio(ws_ai, twilio_send, stream_sid: str, stop_flag):
    # Recolecta chunks de salida ("output_audio.delta") y los manda a Twilio como μ-law 8k
    buf = b""
    try:
        while not stop_flag["stop"]:
            raw = ws_ai.recv()
            if not raw:
                continue
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "output_audio.delta":
                chunk_b64 = msg.get("audio", "")
                if chunk_b64:
                    pcm16 = base64.b64decode(chunk_b64)
                    payload = pcm16_16k_to_mulaw8k(pcm16)
                    # Formato que Twilio espera de vuelta por el WebSocket:
                    twilio_send(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload}
                    }))
            elif t in ("response.completed", "response.error"):
                # Fin de respuesta: nada especial, seguimos.
                pass
    except Exception as e:
        print(f"[BRIDGE] OpenAI->Twilio loop ended: {e}")

def _commit_and_create(ws_ai):
    # Cierra el buffer de entrada y solicita respuesta
    ws_ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
    ws_ai.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio"]}}))

# ---------- TwiML inicial: conecta el Stream ----------
@bp.route("/call", methods=["POST"])
def call_entry():
    """Twilio Voice webhook que inicia un <Connect><Stream> hacia nuestro WS."""
    to_number = request.values.get("To", "")
    bots = current_app.config.get("BOTS_CONFIG") or {}
    cfg = _get_bot_cfg_by_any_number(bots, to_number) or {}

    greeting = cfg.get("greeting") or f"Hola, gracias por llamar a {cfg.get('business_name', cfg.get('name',''))}."
    resp = VoiceResponse()
    # Saludo cortito para evitar silencio inicial (opcional)
    resp.say(greeting, voice="Polly.Conchita", language="es-ES")

    # Construye URL WSS para Twilio -> nuestro WS endpoint
    ws_url = request.url_root.replace("http", "ws").rstrip("/") + url_for("voice_webrtc.stream_ws")
    # Pasamos el número destino para elegir el JSON correcto dentro del WS handler
    ws_url += f"?to={to_number}"

    with resp.connect() as conn:
        conn.stream(url=ws_url)
    return Response(str(resp), mimetype="text/xml")

# ---------- Endpoint WebSocket que recibe el Media Stream de Twilio ----------
# Requiere: pip install flask-sock websocket-client
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

        # Hilo que bombea salida de OpenAI -> Twilio
        t_out = Thread(target=_pump_openai_to_twilio, args=(ai, ws.send, lambda: stream_sid, stop_flag))
        # Nota: pasamos stream_sid por lambda para capturar valor actualizado
        t_out = Thread(target=_pump_openai_to_twilio, args=(ai, ws.send, None, stop_flag))
        # Pero necesitamos el sid real; ajustamos armando un closure:
        def send_to_twilio(payload_json):
            ws.send(payload_json)

        # Reemplazo con cierre que actualiza el sid dentro del loop:
        def pump():
            while not stop_flag["stop"]:
                try:
                    raw = ai.recv()
                except Exception as e:
                    print(f"[BRIDGE] AI recv ended: {e}")
                    break
                try:
                    msg = json.loads(raw)
                except:
                    continue
                if msg.get("type") == "output_audio.delta":
                    pcm16 = base64.b64decode(msg.get("audio",""))
                    b64 = pcm16_16k_to_mulaw8k(pcm16)
                    if stream_sid:
                        ws.send(json.dumps({"event":"media","streamSid":stream_sid,"media":{"payload": b64}}))
        t_out = Thread(target=pump, daemon=True)
        t_out.start()

        try:
            # ---- Loop principal: eventos desde Twilio ----
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
                    # Inicia una nueva captura/turno
                    ai.send(json.dumps({"type": "input_audio_buffer.clear"}))
                elif et == "media":
                    media = ev.get("media") or {}
                    payload = media.get("payload")
                    if payload:
                        pcm16 = mulaw8k_to_pcm16_16k(payload)
                        # Empujar a buffer de entrada de OpenAI
                        ai.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm16).decode("ascii")}))
                elif et == "mark":
                    pass
                elif et == "stop":
                    break

                # Heurística simple: cuando Twilio hace pausa, pedimos respuesta
                # Twilio no manda VAD explícito; aquí podrías temporizar por chunks o usar marks.
                # Para demo, si llega un 'media' lo vamos solicitando cada ~350 ms de silencio:
                # (Podemos hacer algo más sofisticado con timers. Mantener simple por ahora.)

                # (Opción: podrías insertar timers aquí)

                # Para no saturar: cada N ms comprometemos y pedimos respuesta
                # (muy simple: cada ~1.5s)
                now = time.time()
                last = getattr(stream_ws, "_last_commit", 0.0)
                if now - last > 1.5:
                    _commit_and_create(ai)
                    stream_ws._last_commit = now

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
    # Si no está flask_sock instalado, exponemos un health para avisar.
    @bp.route("/stream", methods=["GET"])
    def stream_ws_missing():
        return Response("❌ Falta dependencia: pip install flask-sock websocket-client", status=500)
