"""
Serveur webhook : reçoit les alertes de TradingView et passe les ordres
sur Alpaca en PAPER TRADING (argent fictif, aucun risque réel).

Variables d'environnement requises sur Render :
  ALPACA_API_KEY, ALPACA_SECRET_KEY, WEBHOOK_SECRET
  (le serveur refuse de démarrer si l'une d'elles manque)
Variables optionnelles (notifications) :
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import sys
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================ CONFIGURATION ================================
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

TRADING_ENABLED = True          # passe à False pour désactiver le trading

STOP_LOSS_PERCENT = 2.0         # % de perte max par trade
TAKE_PROFIT_PERCENT = 4.0       # % de gain visé (ratio 1:2 avec le SL)
MAX_DAILY_LOSS_PERCENT = 5.0    # % de perte max sur la journée
MAX_OPEN_POSITIONS = 3          # nombre max de positions ouvertes
MAX_QTY = 10                    # quantité max par ordre
DUPLICATE_WINDOW_SECONDS = 30   # ignore un signal identique dans ces X secondes
SYMBOL_ACTION_COOLDOWN_SECONDS = 120   # délai mini entre 2 trades identiques (symbole+action)
FILL_POLL_ATTEMPTS = 6          # nb de vérifications du statut de l'ordre
FILL_POLL_DELAY_SECONDS = 0.5   # délai entre chaque vérification
# =============================================================================

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

_last_signals = {}       # anti-doublon : {symbole_action: timestamp}
_cooldown = {}           # cooldown : {symbole_action: timestamp}
_daily_start_equity = {"value": None, "date": None}


def log(message):
    print(f"[BOT] {message}", flush=True)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        log(f"Échec envoi Telegram : {e}")


def check_env_vars_or_exit():
    """Bloque le démarrage du serveur si une variable essentielle manque."""
    missing = [n for n in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "WEBHOOK_SECRET")
               if not os.environ.get(n)]
    if missing:
        msg = f"🛑 DÉMARRAGE BLOQUÉ : variables manquantes : {', '.join(missing)}"
        log(msg)
        send_telegram(msg)
        sys.exit(1)
    log("Toutes les variables essentielles sont présentes.")


def get_account():
    r = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_open_positions():
    r = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_position_qty(symbol):
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/v2/positions/{symbol}", headers=HEADERS, timeout=10)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return float(r.json().get("qty", 0))
    except Exception:
        return 0


def check_daily_loss_limit():
    account = get_account()
    equity = float(account["equity"])
    today = time.strftime("%Y-%m-%d")
    if _daily_start_equity["date"] != today:
        _daily_start_equity["date"] = today
        _daily_start_equity["value"] = equity
    start_equity = _daily_start_equity["value"]
    loss_percent = ((start_equity - equity) / start_equity) * 100 if start_equity else 0
    return loss_percent < MAX_DAILY_LOSS_PERCENT


def is_duplicate_signal(symbol, action):
    key = f"{symbol}_{action}"
    now = time.time()
    last_time = _last_signals.get(key)
    _last_signals[key] = now
    return bool(last_time and (now - last_time) < DUPLICATE_WINDOW_SECONDS)


def is_on_cooldown(symbol, action):
    """Cooldown par symbole+action : BUY AAPL et SELL AAPL ne se bloquent pas entre eux."""
    key = f"{symbol}_{action}"
    now = time.time()
    last_trade = _cooldown.get(key)
    return bool(last_trade and (now - last_trade) < SYMBOL_ACTION_COOLDOWN_SECONDS)


def mark_traded(symbol, action):
    _cooldown[f"{symbol}_{action}"] = time.time()


def validate_input(symbol, action, qty_raw):
    """Valide les données reçues. qty doit être un entier STRICT (2.5 est refusé, pas arrondi)."""
    if not symbol or not isinstance(symbol, str) or not symbol.isalnum():
        return False, None, "symbole invalide ou vide"
    if action not in ("buy", "sell"):
        return False, None, "action invalide (doit être buy ou sell)"

    if isinstance(qty_raw, bool):
        return False, None, "qty invalide"
    if isinstance(qty_raw, int):
        qty_int = qty_raw
    elif isinstance(qty_raw, float):
        if not qty_raw.is_integer():
            return False, None, f"qty ({qty_raw}) doit être un entier, pas un nombre décimal"
        qty_int = int(qty_raw)
    elif isinstance(qty_raw, str):
        try:
            qty_int = int(qty_raw)  # échoue déjà si "2.5" (ValueError), pas de troncature
        except ValueError:
            return False, None, f"qty ('{qty_raw}') doit être un entier, pas un nombre décimal"
    else:
        return False, None, "qty invalide"

    if qty_int <= 0:
        return False, None, "qty doit être supérieur à 0"
    if qty_int > MAX_QTY:
        return False, None, f"qty ({qty_int}) dépasse la limite autorisée ({MAX_QTY})"
    return True, qty_int, None


def poll_order_fill(order_id):
    """Interroge le statut de l'ordre jusqu'à obtenir un prix rempli, ou abandonne après N essais."""
    for _ in range(FILL_POLL_ATTEMPTS):
        try:
            r = requests.get(f"{ALPACA_BASE_URL}/v2/orders/{order_id}", headers=HEADERS, timeout=10)
            r.raise_for_status()
            order = r.json()
            if order.get("filled_avg_price"):
                return order
            if order.get("status") in ("canceled", "rejected", "expired"):
                return order
        except Exception as e:
            log(f"Erreur pendant le polling de l'ordre {order_id} : {e}")
        time.sleep(FILL_POLL_DELAY_SECONDS)
    return None


def place_exit_orders(symbol, qty, entry_price):
    """Place un ordre OCO (stop-loss + take-profit) basé sur le prix d'entrée réellement exécuté."""
    stop_price = round(entry_price * (1 - STOP_LOSS_PERCENT / 100), 2)
    take_profit_price = round(entry_price * (1 + TAKE_PROFIT_PERCENT / 100), 2)
    exit_order = {
        "symbol": symbol,
        "qty": qty,
        "side": "sell",
        "type": "limit",
        "limit_price": take_profit_price,
        "time_in_force": "gtc",
        "order_class": "oco",
        "stop_loss": {"stop_price": stop_price},
    }
    r = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=exit_order, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return stop_price, take_profit_price


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    if data.get("secret") != WEBHOOK_SECRET:
        log("Requête reçue avec un secret invalide.")
        return jsonify({"error": "secret invalide"}), 403

    action = str(data.get("action", "")).lower()
    symbol = str(data.get("symbol", "")).upper()
    qty_raw = data.get("qty", 1)

    if not TRADING_ENABLED:
        msg = f"⏸️ [{timestamp}] Signal {action.upper()} {symbol} reçu mais TRADING_ENABLED=False"
        log(msg)
        send_telegram(msg)
        return jsonify({"info": "trading désactivé"}), 200

    ok, qty, error_msg = validate_input(symbol, action, qty_raw)
    if not ok:
        log(f"Signal rejeté ({error_msg}) : {data}")
        send_telegram(f"⚠️ [{timestamp}] Signal rejeté sur {symbol or '?'} : {error_msg}")
        return jsonify({"error": error_msg}), 400

    if is_duplicate_signal(symbol, action):
        log(f"Signal doublon ignoré : {action} {qty} {symbol}")
        return jsonify({"info": "signal ignoré (doublon détecté)"}), 200

    if is_on_cooldown(symbol, action):
        msg = f"⚠️ [{timestamp}] {action.upper()} {symbol} ignoré — cooldown en cours"
        log(msg)
        send_telegram(msg)
        return jsonify({"info": "cooldown en cours pour ce symbole/action"}), 200

    try:
        if not check_daily_loss_limit():
            msg = f"🛑 [{timestamp}] Trading suspendu : perte quotidienne max atteinte"
            log(msg)
            send_telegram(msg)
            return jsonify({"info": "perte quotidienne max atteinte"}), 200

        if action == "buy":
            positions = get_open_positions()
            if len(positions) >= MAX_OPEN_POSITIONS:
                msg = f"⚠️ [{timestamp}] BUY {symbol} ignoré — {len(positions)} positions déjà ouvertes"
                log(msg)
                send_telegram(msg)
                return jsonify({"info": "nombre max de positions atteint"}), 200

            order = {
                "symbol": symbol, "qty": qty, "side": "buy",
                "type": "market", "time_in_force": "gtc",
            }
            response = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=order, headers=HEADERS, timeout=10)
            response.raise_for_status()
            order_result = response.json()
            mark_traded(symbol, action)

            filled_order = poll_order_fill(order_result["id"])
            entry_price = None
            sl_tp_text = "SL/TP non posés (prix de remplissage indisponible)"
            if filled_order and filled_order.get("filled_avg_price"):
                entry_price = float(filled_order["filled_avg_price"])
                try:
                    stop_price, take_profit_price = place_exit_orders(symbol, qty, entry_price)
                    sl_tp_text = f"SL {stop_price} | TP {take_profit_price}"
                except Exception as e:
                    sl_tp_text = f"échec pose SL/TP : {e}"
                    log(sl_tp_text)

            status = (filled_order or order_result).get("status", "inconnu")
            verbe = "exécuté" if status == "filled" else "envoyé"
            prix_text = f"Prix d'entrée : {entry_price}" if entry_price else "Prix d'entrée : en attente"

            msg = (
                f"✅ [{timestamp}] Ordre {verbe} : BUY {qty} {symbol}\n"
                f"{prix_text} | {sl_tp_text}\nStatut : {status}"
            )
            log(msg)
            send_telegram(msg)
            return jsonify(order_result), response.status_code

        else:  # sell
            held_qty = get_position_qty(symbol)
            if held_qty <= 0:
                msg = f"⚠️ [{timestamp}] SELL {symbol} ignoré — aucune position détenue"
                log(msg)
                send_telegram(msg)
                return jsonify({"info": "aucune position à vendre"}), 200

            qty = min(qty, int(held_qty))
            order = {
                "symbol": symbol, "qty": qty, "side": "sell",
                "type": "market", "time_in_force": "gtc",
            }
            response = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=order, headers=HEADERS, timeout=10)
            response.raise_for_status()
            order_result = response.json()
            mark_traded(symbol, action)

            status = order_result.get("status", "inconnu")
            verbe = "exécuté" if status == "filled" else "envoyé"
            msg = f"✅ [{timestamp}] Ordre {verbe} : SELL {qty} {symbol}\nStatut : {status}"
            log(msg)
            send_telegram(msg)
            return jsonify(order_result), response.status_code

    except requests.exceptions.RequestException as e:
        msg = f"🚨 [{timestamp}] Erreur de connexion à Alpaca sur {symbol} : {e}"
        log(msg)
        send_telegram(msg)
        return jsonify({"error": "erreur de connexion à Alpaca", "details": str(e)}), 502
    except Exception as e:
        msg = f"🚨 [{timestamp}] Erreur inattendue sur {symbol} : {e}"
        log(msg)
        send_telegram(msg)
        return jsonify({"error": "erreur inattendue", "details": str(e)}), 500


@app.route("/", methods=["GET"])
def health_check():
    try:
        account = get_account()
        alpaca_status = f"Alpaca OK (equity: {account.get('equity')})"
    except Exception as e:
        alpaca_status = f"Alpaca injoignable : {e}"
    status = "actif" if TRADING_ENABLED else "en pause (TRADING_ENABLED=False)"
    return f"Bot webhook {status}. {alpaca_status}", 200


check_env_vars_or_exit()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
