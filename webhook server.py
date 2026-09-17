"""
Serveur webhook : reçoit les alertes de TradingView et passe les ordres
sur Alpaca en PAPER TRADING (argent fictif, aucun risque réel).

Variables d'environnement à définir sur ton hébergeur (Render, Railway...) :
  ALPACA_API_KEY       -> ta clé API Alpaca (paper)
  ALPACA_SECRET_KEY    -> ta clé secrète Alpaca (paper)
  WEBHOOK_SECRET       -> un mot de passe que tu inventes toi-même,
                          pour que seul TradingView (avec ce mot dans
                          le message) puisse déclencher un ordre.
"""

import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

# URL de l'API Alpaca PAPER (bien vérifier que c'est "paper-api", pas "api")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}

    # 1. Vérification du mot de passe secret envoyé par TradingView
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "secret invalide"}), 403

    action = str(data.get("action", "")).lower()   # "buy" ou "sell"
    symbol = data.get("symbol", "AAPL")
    qty = data.get("qty", 1)

    if action not in ("buy", "sell"):
        return jsonify({"error": "action invalide"}), 400

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": action,
        "type": "market",
        "time_in_force": "gtc",
    }

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

    response = requests.post(
        f"{ALPACA_BASE_URL}/v2/orders", json=order, headers=headers, timeout=10
    )

    return jsonify(response.json()), response.status_code


@app.route("/", methods=["GET"])
def health_check():
    # Sert juste à vérifier que le serveur tourne bien
    return "Bot webhook actif", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
