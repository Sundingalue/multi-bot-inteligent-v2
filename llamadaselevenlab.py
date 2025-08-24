import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/webhooks/eleven/post-call")
def post_call():
    # Por ahora solo responde OK (en el paso 2 añadimos verificación y email)
    print("Webhook recibido:", request.get_json(silent=True))
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
