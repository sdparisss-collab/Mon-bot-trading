"""
WEBHOOK SERVER
TradingView -> Render -> Alpaca PAPER TRADING -> Telegram

IMPORTANT :
- Ce fichier est prévu pour Alpaca PAPER TRADING.
- TradingView envoie uniquement : secret / action / symbol.
- La quantité est calculée ici.
- Les BUY utilisent un BRACKET ORDER Alpaca :
      entrée + Take Profit + Stop Loss
- Aucun ordre n'est envoyé à un compte Alpaca LIVE.
"""

import os
import time
import uuid
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify


# ============================================================
# CONFIGURATION
# ============================================================

APP_NAME = "TradingView-Alpaca-Paper-Bot"

# Alpaca PAPER uniquement
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# TRADING SETTINGS
# ============================================================

TRADING_ENABLED = True

RISK_PER_TRADE_PERCENT = 1.0

STOP_LOSS_PERCENT = 2.0
TAKE_PROFIT_PERCENT = 4.0

MAX_DAILY_LOSS_PERCENT = 5.0

MAX_OPEN_POSITIONS = 3

MAX_QTY = 10

MAX_POSITION_VALUE_PERCENT = 20.0

# Anti-doublon
DUPLICATE_WINDOW_SECONDS = 30

# Empêche un nouveau signal immédiat sur le même symbole/action
SYMBOL_ACTION_COOLDOWN_SECONDS = 120

# Temps maximum d'attente pour un ordre
ORDER_POLL_SECONDS = 15

# Feed de données
DATA_FEED = "iex"


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(APP_NAME)


# ============================================================
# VALIDATION ENV
# ============================================================

if not API_KEY:
    raise RuntimeError("ALPACA_API_KEY manquante")

if not SECRET_KEY:
    raise RuntimeError("ALPACA_SECRET_KEY manquante")

if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET manquante")


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json",
})


# ============================================================
# MÉMOIRE ANTI-DOUBLON
# ============================================================

recent_signals = {}

recent_actions = {}


# ============================================================
# OUTILS
# ============================================================

def now_ts():
    return time.time()


def utc_string():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def clean_symbol(symbol):
    if not isinstance(symbol, str):
        return None

    symbol = symbol.strip().upper()

    if not symbol:
        return None

    # On accepte uniquement des tickers simples.
    if not symbol.isalnum():
        return None

    if len(symbol) > 10:
        return None

    return symbol


def clean_action(action):
    if not isinstance(action, str):
        return None

    action = action.strip().lower()

    if action not in ("buy", "sell"):
        return None

    return action


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }

        requests.post(
            url,
            json=payload,
            timeout=8
        )

    except Exception as exc:
        logger.warning("Telegram indisponible : %s", exc)


def notify(message):
    logger.info(message)
    send_telegram(message)


# ============================================================
# ALPACA HTTP
# ============================================================

def alpaca_request(
    method,
    endpoint,
    *,
    params=None,
    json_data=None,
    timeout=10
):
    url = ALPACA_BASE_URL + endpoint

    response = session.request(
        method=method,
        url=url,
        params=params,
        json=json_data,
        timeout=timeout
    )

    if not response.ok:
        try:
            error_body = response.json()
        except Exception:
            error_body = response.text

        raise RuntimeError(
            f"Alpaca HTTP {response.status_code}: {error_body}"
        )

    if not response.text:
        return {}

    try:
        return response.json()
    except Exception:
        return response.text


# ============================================================
# COMPTE ALPACA
# ============================================================

def get_account():
    return alpaca_request(
        "GET",
        "/v2/account"
    )


def get_equity():
    account = get_account()

    equity = safe_float(account.get("equity"))

    if equity is None or equity <= 0:
        raise RuntimeError(
            "Impossible de récupérer l'equity Alpaca."
        )

    return equity


# ============================================================
# PERTE JOURNALIÈRE
# ============================================================

def check_daily_loss():
    account = get_account()

    equity = safe_float(account.get("equity"))
    last_equity = safe_float(account.get("last_equity"))

    if equity is None:
        return False, "Equity Alpaca invalide."

    if last_equity is None or last_equity <= 0:
        return True, "Référence journalière indisponible."

    daily_change_percent = (
        (equity - last_equity)
        / last_equity
        * 100
    )

    if daily_change_percent <= -MAX_DAILY_LOSS_PERCENT:
        return (
            False,
            (
                f"Limite de perte journalière atteinte : "
                f"{daily_change_percent:.2f}%"
            )
        )

    return (
        True,
        f"Variation journalière : {daily_change_percent:+.2f}%"
    )


# ============================================================
# POSITIONS
# ============================================================

def get_positions():
    return alpaca_request(
        "GET",
        "/v2/positions"
    )


def get_position(symbol):
    positions = get_positions()

    for position in positions:
        if position.get("symbol") == symbol:
            return position

    return None


def count_open_positions():
    positions = get_positions()

    return len(positions)


# ============================================================
# ORDRES
# ============================================================

def get_open_orders(symbol=None):
    params = {
        "status": "open",
        "nested": "true",
    }

    if symbol:
        params["symbols"] = symbol

    return alpaca_request(
        "GET",
        "/v2/orders",
        params=params
    )


def cancel_orders_for_symbol(symbol):
    orders = get_open_orders(symbol)

    if not orders:
        return

    for order in orders:
        order_id = order.get("id")

        if not order_id:
            continue

        try:
            alpaca_request(
                "DELETE",
                f"/v2/orders/{order_id}"
            )

            logger.info(
                "Ordre annulé : %s | %s",
                symbol,
                order_id
            )

        except Exception as exc:
            logger.warning(
                "Impossible d'annuler %s : %s",
                order_id,
                exc
            )


def wait_until_no_open_orders(symbol, timeout=5):
    deadline = time.time() + timeout

    while time.time() < deadline:

        try:
            orders = get_open_orders(symbol)

            if not orders:
                return True

        except Exception as exc:
            logger.warning(
                "Erreur vérification ordres %s : %s",
                symbol,
                exc
            )

        time.sleep(0.25)

    return False


# ============================================================
# PRIX ALPACA
# ============================================================

def get_latest_price(symbol):
    url = (
        f"{ALPACA_DATA_URL}"
        f"/v2/stocks/{symbol}/trades/latest"
    )

    response = session.get(
        url,
        params={"feed": DATA_FEED},
        timeout=8
    )

    if not response.ok:

        try:
            body = response.json()
        except Exception:
            body = response.text

        raise RuntimeError(
            f"Prix Alpaca HTTP {response.status_code}: {body}"
        )

    data = response.json()

    trade = data.get("trade", {})

    price = safe_float(trade.get("p"))

    if price is None or price <= 0:
        raise RuntimeError(
            f"Prix invalide reçu pour {symbol}."
        )

    return price


# ============================================================
# CALCUL SL / TP
# ============================================================

def calculate_exit_prices(reference_price):

    stop_price = (
        reference_price
        * (1 - STOP_LOSS_PERCENT / 100)
    )

    take_profit_price = (
        reference_price
        * (1 + TAKE_PROFIT_PERCENT / 100)
    )

    # Alpaca accepte des prix avec décimales.
    stop_price = round(stop_price, 2)
    take_profit_price = round(take_profit_price, 2)

    if stop_price <= 0:
        raise RuntimeError("Stop Loss invalide.")

    if take_profit_price <= stop_price:
        raise RuntimeError(
            "Take Profit doit être supérieur au Stop Loss."
        )

    return stop_price, take_profit_price


# ============================================================
# CALCUL QUANTITÉ
# ============================================================

def calculate_quantity(symbol, price):

    account = get_account()

    equity = safe_float(account.get("equity"))
    buying_power = safe_float(account.get("buying_power"))

    if equity is None or equity <= 0:
        raise RuntimeError("Equity invalide.")

    if buying_power is None or buying_power <= 0:
        raise RuntimeError("Buying power insuffisant.")

    # --------------------------------------------------------
    # 1. Risque maximum autorisé
    # --------------------------------------------------------

    risk_budget = (
        equity
        * RISK_PER_TRADE_PERCENT
        / 100
    )

    risk_per_share = (
        price
        * STOP_LOSS_PERCENT
        / 100
    )

    if risk_per_share <= 0:
        raise RuntimeError("Risque par action invalide.")

    qty_by_risk = int(
        risk_budget / risk_per_share
    )

    # --------------------------------------------------------
    # 2. Valeur maximale d'une position
    # --------------------------------------------------------

    max_position_value = (
        equity
        * MAX_POSITION_VALUE_PERCENT
        / 100
    )

    qty_by_position = int(
        max_position_value / price
    )

    # --------------------------------------------------------
    # 3. Buying power
    # --------------------------------------------------------

    qty_by_buying_power = int(
        buying_power / price
    )

    # --------------------------------------------------------
    # 4. Limite absolue
    # --------------------------------------------------------

    quantity = min(
        qty_by_risk,
        qty_by_position,
        qty_by_buying_power,
        MAX_QTY
    )

    if quantity < 1:
        raise RuntimeError(
            (
                f"Quantité calculée = 0. "
                f"Prix={price:.2f}, "
                f"equity={equity:.2f}, "
                f"buying_power={buying_power:.2f}"
            )
        )

    return quantity


# ============================================================
# ORDRE BRACKET BUY
# ============================================================

def submit_bracket_buy(symbol, quantity, reference_price):

    stop_price, take_profit_price = (
        calculate_exit_prices(reference_price)
    )

    client_order_id = (
        f"tv-{symbol.lower()}-"
        f"{uuid.uuid4().hex[:20]}"
    )

    payload = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "client_order_id": client_order_id,

        "take_profit": {
            "limit_price": f"{take_profit_price:.2f}"
        },

        "stop_loss": {
            "stop_price": f"{stop_price:.2f}"
        }
    }

    try:
        order = alpaca_request(
            "POST",
            "/v2/orders",
            json_data=payload
        )

    except Exception as exc:

        notify(
            (
                f"🚨 ÉCHEC BUY {symbol}\n"
                f"Erreur : {exc}"
            )
        )

        raise

    return order, stop_price, take_profit_price


# ============================================================
# SELL MARKET
# ============================================================

def submit_market_sell(symbol, quantity):

    client_order_id = (
        f"tv-{symbol.lower()}-sell-"
        f"{uuid.uuid4().hex[:16]}"
    )

    payload = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }

    return alpaca_request(
        "POST",
        "/v2/orders",
        json_data=payload
    )


# ============================================================
# STATUT ORDRE
# ============================================================

def get_order(order_id):

    return alpaca_request(
        "GET",
        f"/v2/orders/{order_id}"
    )


def wait_for_order(order_id):

    deadline = time.time() + ORDER_POLL_SECONDS

    last_status = None

    while time.time() < deadline:

        try:
            order = get_order(order_id)

            status = order.get("status")

            last_status = status

            if status in (
                "filled",
                "partially_filled",
                "canceled",
                "expired",
                "rejected",
                "done_for_day"
            ):
                return order

        except Exception as exc:
            logger.warning(
                "Erreur statut ordre %s : %s",
                order_id,
                exc
            )

        time.sleep(0.5)

    try:
        return get_order(order_id)
    except Exception:
        return {
            "id": order_id,
            "status": last_status or "unknown"
        }


# ============================================================
# ANTI-DOUBLON
# ============================================================

def is_duplicate_signal(action, symbol):

    key = f"{action}:{symbol}"

    current = now_ts()

    previous = recent_signals.get(key)

    if previous is not None:

        if current - previous < DUPLICATE_WINDOW_SECONDS:
            return True

    recent_signals[key] = current

    # Nettoyage léger
    cutoff = current - 600

    for k in list(recent_signals.keys()):

        if recent_signals[k] < cutoff:
            del recent_signals[k]

    return False


def is_action_on_cooldown(action, symbol):

    key = f"{action}:{symbol}"

    current = now_ts()

    previous = recent_actions.get(key)

    if previous is not None:

        if current - previous < SYMBOL_ACTION_COOLDOWN_SECONDS:
            return True

    return False


def register_action(action, symbol):
    recent_actions[f"{action}:{symbol}"] = now_ts()


# ============================================================
# BUY
# ============================================================

def handle_buy(symbol):

    if not TRADING_ENABLED:
        return {
            "ok": False,
            "reason": "Trading désactivé."
        }

    # --------------------------------------------------------
    # Daily loss
    # --------------------------------------------------------

    allowed, reason = check_daily_loss()

    if not allowed:

        notify(
            f"🛑 BUY {symbol} BLOQUÉ\n"
            f"Raison : {reason}"
        )

        return {
            "ok": False,
            "reason": reason
        }

    # --------------------------------------------------------
    # Nombre de positions
    # --------------------------------------------------------

    positions_count = count_open_positions()

    if positions_count >= MAX_OPEN_POSITIONS:

        reason = (
            f"Maximum de positions atteint "
            f"({positions_count}/{MAX_OPEN_POSITIONS})."
        )

        notify(
            f"⚠️ BUY {symbol} ignoré\n{reason}"
        )

        return {
            "ok": False,
            "reason": reason
        }

    # --------------------------------------------------------
    # Position déjà existante
    # --------------------------------------------------------

    existing_position = get_position(symbol)

    if existing_position:

        reason = (
            f"Position {symbol} déjà ouverte."
        )

        notify(
            f"⚠️ BUY {symbol} ignoré\n{reason}"
        )

        return {
            "ok": False,
            "reason": reason
        }

    # --------------------------------------------------------
    # Ordre déjà ouvert
    # --------------------------------------------------------

    open_orders = get_open_orders(symbol)

    if open_orders:

        reason = (
            f"{len(open_orders)} ordre(s) déjà ouvert(s) "
            f"sur {symbol}."
        )

        notify(
            f"⚠️ BUY {symbol} ignoré\n{reason}"
        )

        return {
            "ok": False,
            "reason": reason
        }

    # --------------------------------------------------------
    # Prix
    # --------------------------------------------------------

    reference_price = get_latest_price(symbol)

    # --------------------------------------------------------
    # Quantité
    # --------------------------------------------------------

    quantity = calculate_quantity(
        symbol,
        reference_price
    )

    # --------------------------------------------------------
    # BUY BRACKET
    # --------------------------------------------------------

    order, stop_price, take_profit_price = (
        submit_bracket_buy(
            symbol,
            quantity,
            reference_price
        )
    )

    order_id = order.get("id")
    status = order.get("status")

    register_action("buy", symbol)

    notify(
        (
            f"🟢 BUY {symbol}\n"
            f"Quantité : {quantity}\n"
            f"Prix référence : ${reference_price:.2f}\n"
            f"SL : ${stop_price:.2f}\n"
            f"TP : ${take_profit_price:.2f}\n"
            f"Risque/trade : {RISK_PER_TRADE_PERCENT:.2f}%\n"
            f"Order ID : {order_id}\n"
            f"Statut : {status}\n"
            f"Mode : PAPER"
        )
    )

    return {
        "ok": True,
        "action": "buy",
        "symbol": symbol,
        "quantity": quantity,
        "reference_price": reference_price,
        "stop_loss": stop_price,
        "take_profit": take_profit_price,
        "order_id": order_id,
        "status": status,
    }


# ============================================================
# SELL
# ============================================================

def handle_sell(symbol):

    if not TRADING_ENABLED:
        return {
            "ok": False,
            "reason": "Trading désactivé."
        }

    # --------------------------------------------------------
    # Récupérer position
    # --------------------------------------------------------

    position = get_position(symbol)

    if not position:

        reason = (
            f"Aucune position {symbol} à vendre."
        )

        notify(
            f"⚠️ SELL {symbol} ignoré — aucune position"
        )

        return {
            "ok": False,
            "reason": reason
        }

    # --------------------------------------------------------
    # Quantité réellement détenue
    # --------------------------------------------------------

    quantity_float = safe_float(
        position.get("qty")
    )

    if quantity_float is None or quantity_float <= 0:

        return {
            "ok": False,
            "reason": "Quantité de position invalide."
        }

    quantity = int(quantity_float)

    if quantity <= 0:

        return {
            "ok": False,
            "reason": "Quantité entière insuffisante."
        }

    # --------------------------------------------------------
    # Annuler TP / SL / autres ordres
    # --------------------------------------------------------

    cancel_orders_for_symbol(symbol)

    # --------------------------------------------------------
    # Attendre que les ordres soient réellement annulés
    # --------------------------------------------------------

    if not wait_until_no_open_orders(
        symbol,
        timeout=5
    ):

        reason = (
            "Impossible de confirmer "
            "l'annulation des ordres existants."
        )

        notify(
            f"🛑 SELL {symbol} bloqué\n{reason}"
        )

        return {
            "ok": False,
            "reason": reason
        }

    # --------------------------------------------------------
    # Relecture de la position
    # --------------------------------------------------------

    position = get_position(symbol)

    if not position:

        notify(
            f"ℹ️ SELL {symbol} : "
            f"position déjà fermée."
        )

        return {
            "ok": True,
            "action": "sell",
            "symbol": symbol,
            "status": "already_closed"
        }

    quantity_float = safe_float(
        position.get("qty")
    )

    if quantity_float is None:
        raise RuntimeError(
            "Impossible de relire la quantité."
        )

    quantity = int(quantity_float)

    if quantity <= 0:

        return {
            "ok": False,
            "reason": "Position trop petite pour un SELL entier."
        }

    # --------------------------------------------------------
    # MARKET SELL
    # --------------------------------------------------------

    try:

        order = submit_market_sell(
            symbol,
            quantity
        )

    except Exception as exc:

        notify(
            (
                f"🚨 SELL {symbol} ÉCHOUÉ\n"
                f"Quantité : {quantity}\n"
                f"Erreur : {exc}"
            )
        )

        raise

    order_id = order.get("id")
    status = order.get("status")

    register_action("sell", symbol)

    notify(
        (
            f"🔴 SELL {symbol}\n"
            f"Quantité : {quantity}\n"
            f"Order ID : {order_id}\n"
            f"Statut : {status}\n"
            f"Mode : PAPER"
        )
    )

    return {
        "ok": True,
        "action": "sell",
        "symbol": symbol,
        "quantity": quantity,
        "order_id": order_id,
        "status": status,
    }


# ============================================================
# WEBHOOK
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    received_at = utc_string()

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):

        return jsonify({
            "ok": False,
            "error": "JSON invalide."
        }), 400

    # --------------------------------------------------------
    # SECRET
    # --------------------------------------------------------

    secret = data.get("secret")

    if secret != WEBHOOK_SECRET:

        logger.warning(
            "Webhook refusé : mauvais secret."
        )

        return jsonify({
            "ok": False,
            "error": "Unauthorized."
        }), 401

    # --------------------------------------------------------
    # ACTION
    # --------------------------------------------------------

    action = clean_action(
        data.get("action")
    )

    if action is None:

        return jsonify({
            "ok": False,
            "error": "Action invalide."
        }), 400

    # --------------------------------------------------------
    # SYMBOL
    # --------------------------------------------------------

    symbol = clean_symbol(
        data.get("symbol")
    )

    if symbol is None:

        return jsonify({
            "ok": False,
            "error": "Symbole invalide."
        }), 400

    logger.info(
        "Webhook reçu | %s | %s | %s",
        received_at,
        action.upper(),
        symbol
    )

    # --------------------------------------------------------
    # ANTI-DOUBLON
    # --------------------------------------------------------

    if is_duplicate_signal(
        action,
        symbol
    ):

        notify(
            (
                f"⚠️ Signal dupliqué ignoré\n"
                f"{action.upper()} {symbol}"
            )
        )

        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": "duplicate"
        }), 200

    # --------------------------------------------------------
    # COOLDOWN
    # --------------------------------------------------------

    if is_action_on_cooldown(
        action,
        symbol
    ):

        notify(
            (
                f"⏳ Cooldown actif\n"
                f"{action.upper()} {symbol}"
            )
        )

        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": "cooldown"
        }), 200

    # --------------------------------------------------------
    # TRADING ENABLED
    # --------------------------------------------------------

    if not TRADING_ENABLED:

        notify(
            (
                f"⚠️ Signal reçu mais trading désactivé\n"
                f"{action.upper()} {symbol}"
            )
        )

        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": "trading_disabled"
        }), 200

    # --------------------------------------------------------
    # TRAITEMENT
    # --------------------------------------------------------

    try:

        if action == "buy":

            result = handle_buy(symbol)

        else:

            result = handle_sell(symbol)

        return jsonify(result), 200

    except Exception as exc:

        logger.exception(
            "Erreur traitement webhook"
        )

        notify(
            (
                f"🚨 ERREUR BOT\n"
                f"Action : {action.upper()}\n"
                f"Symbole : {symbol}\n"
                f"Erreur : {exc}"
            )
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "ok": True,
        "service": APP_NAME,
        "mode": "PAPER",
        "trading_enabled": TRADING_ENABLED,
        "time": utc_string()
    })


@app.route("/health", methods=["GET"])
def health():

    try:

        account = get_account()

        return jsonify({
            "ok": True,
            "alpaca": "connected",
            "paper": True,
            "account_status": account.get("status"),
            "trading_blocked": account.get(
                "trading_blocked"
            ),
            "time": utc_string()
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "alpaca": "error",
            "error": str(exc),
            "time": utc_string()
        }), 503


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    logger.info(
        "=========================================="
    )

    logger.info(
        "%s",
        APP_NAME
    )

    logger.info(
        "MODE : ALPACA PAPER TRADING"
    )

    logger.info(
        "TRADING ENABLED : %s",
        TRADING_ENABLED
    )

    logger.info(
        "RISK / TRADE : %.2f%%",
        RISK_PER_TRADE_PERCENT
    )

    logger.info(
        "STOP LOSS : %.2f%%",
        STOP_LOSS_PERCENT
    )

    logger.info(
        "TAKE PROFIT : %.2f%%",
        TAKE_PROFIT_PERCENT
    )

    logger.info(
        "MAX QTY : %d",
        MAX_QTY
    )

    logger.info(
        "MAX POSITIONS : %d",
        MAX_OPEN_POSITIONS
    )

    logger.info(
        "MAX POSITION VALUE : %.2f%%",
        MAX_POSITION_VALUE_PERCENT
    )

    logger.info(
        "=========================================="
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
