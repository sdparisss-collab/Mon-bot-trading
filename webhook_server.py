"""

Serveur webhook TradingView -> Alpaca PAPER TRADING.

TradingView envoie uniquement :

{

    "secret": "...",

    "symbol": "AAPL",

    "action": "buy"

}

La quantité N'EST PLUS acceptée depuis TradingView.

Le serveur la calcule lui-même à partir :

- du capital réel du compte Alpaca

- du risque maximal par trade

- du stop-loss

- du prix actuel

- du buying power

- des limites de sécurité

Variables Render obligatoires :

    ALPACA_API_KEY

    ALPACA_SECRET_KEY

    WEBHOOK_SECRET

Variables optionnelles :

    TELEGRAM_BOT_TOKEN

    TELEGRAM_CHAT_ID

"""

import os

import sys

import time

import requests

from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================================

# CONFIGURATION

# ============================================================================

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")

ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# PAPER TRADING UNIQUEMENT

ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

ALPACA_DATA_URL = "https://data.alpaca.markets"

TRADING_ENABLED = True

# ---------------------------------------------------------------------------

# RISQUE

# ---------------------------------------------------------------------------

# Risque maximum théorique du capital par trade.

# Exemple : 1% d'un compte de 500$ = 5$ de risque théorique.

RISK_PER_TRADE_PERCENT = 1.0

# Stop et Take Profit

STOP_LOSS_PERCENT = 2.0

TAKE_PROFIT_PERCENT = 4.0

# ---------------------------------------------------------------------------

# LIMITES DE SECURITE

# ---------------------------------------------------------------------------

# Nombre maximum d'actions achetées par ordre.

MAX_QTY = 10

# Valeur maximum d'une position en % de l'equity.

# Exemple : 20% d'un compte de 500$ = 100$ maximum sur une position.

MAX_POSITION_VALUE_PERCENT = 20.0

# Nombre maximum de positions simultanées.

MAX_OPEN_POSITIONS = 3

# Perte maximale quotidienne.

MAX_DAILY_LOSS_PERCENT = 5.0

# ---------------------------------------------------------------------------

# PROTECTIONS CONTRE LES DOUBLONS

# ---------------------------------------------------------------------------

DUPLICATE_WINDOW_SECONDS = 30

SYMBOL_ACTION_COOLDOWN_SECONDS = 120

# ---------------------------------------------------------------------------

# POLLING DES ORDRES

# ---------------------------------------------------------------------------

FILL_POLL_ATTEMPTS = 10

FILL_POLL_DELAY_SECONDS = 0.5

# ============================================================================

# HEADERS

# ============================================================================

HEADERS = {

    "APCA-API-KEY-ID": ALPACA_API_KEY,

    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,

}

DATA_HEADERS = {

    "APCA-API-KEY-ID": ALPACA_API_KEY,

    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,

}

# ============================================================================

# ETAT INTERNE

# ============================================================================

_last_signals = {}

_cooldown = {}

_daily_start_equity = {

    "value": None,

    "date": None,

}

# ============================================================================

# LOGGING

# ============================================================================

def log(message):

    print(f"[BOT] {message}", flush=True)

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:

        return

    try:

        requests.post(

            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",

            json={

                "chat_id": TELEGRAM_CHAT_ID,

                "text": message,

            },

            timeout=10,

        )

    except Exception as e:

        log(f"Échec Telegram : {e}")

# ============================================================================

# ENVIRONNEMENT

# ============================================================================

def check_env_vars_or_exit():

    missing = [

        name

        for name in (

            "ALPACA_API_KEY",

            "ALPACA_SECRET_KEY",

            "WEBHOOK_SECRET",

        )

        if not os.environ.get(name)

    ]

    if missing:

        msg = (

            "🛑 DÉMARRAGE BLOQUÉ : variables manquantes : "

            + ", ".join(missing)

        )

        log(msg)

        send_telegram(msg)

        sys.exit(1)

    log("Toutes les variables essentielles sont présentes.")

# ============================================================================

# ALPACA - COMPTE

# ============================================================================

def get_account():

    response = requests.get(

        f"{ALPACA_BASE_URL}/v2/account",

        headers=HEADERS,

        timeout=10,

    )

    response.raise_for_status()

    return response.json()

def get_equity():

    account = get_account()

    return float(account["equity"])

def get_buying_power():

    account = get_account()

    return float(account["buying_power"])

# ============================================================================

# ALPACA - POSITIONS

# ============================================================================

def get_open_positions():

    response = requests.get(

        f"{ALPACA_BASE_URL}/v2/positions",

        headers=HEADERS,

        timeout=10,

    )

    response.raise_for_status()

    return response.json()

def get_position(symbol):

    try:

        response = requests.get(

            f"{ALPACA_BASE_URL}/v2/positions/{symbol}",

            headers=HEADERS,

            timeout=10,

        )

        if response.status_code == 404:

            return None

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException:

        return None

def get_position_qty(symbol):

    position = get_position(symbol)

    if not position:

        return 0

    try:

        return float(position.get("qty", 0))

    except (TypeError, ValueError):

        return 0

# ============================================================================

# ALPACA - PRIX

# ============================================================================

def get_latest_price(symbol):

    """

    Récupère le dernier prix connu de l'action.

    """

    response = requests.get(

        f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest",

        headers=DATA_HEADERS,

        timeout=10,

        params={

            "feed": "iex",

        },

    )

    response.raise_for_status()

    data = response.json()

    trade = data.get("trade", {})

    price = trade.get("p")

    if price is None:

        raise ValueError(f"Prix indisponible pour {symbol}")

    price = float(price)

    if price <= 0:

        raise ValueError(f"Prix invalide pour {symbol}: {price}")

    return price

# ============================================================================

# PERTE QUOTIDIENNE

# ============================================================================

def check_daily_loss_limit():

    account = get_account()

    equity = float(account["equity"])

    today = time.strftime("%Y-%m-%d")

    if _daily_start_equity["date"] != today:

        _daily_start_equity["date"] = today

        _daily_start_equity["value"] = equity

        log(

            f"Nouvelle journée : equity de référence = "

            f"{equity:.2f}"

        )

    start_equity = _daily_start_equity["value"]

    if not start_equity or start_equity <= 0:

        return True, 0.0

    loss_percent = (

        (start_equity - equity)

        / start_equity

    ) * 100

    return (

        loss_percent < MAX_DAILY_LOSS_PERCENT,

        loss_percent,

    )

# ============================================================================

# ANTI-DOUBLONS

# ============================================================================

def is_duplicate_signal(symbol, action):

    key = f"{symbol}_{action}"

    now = time.time()

    last_time = _last_signals.get(key)

    _last_signals[key] = now

    return bool(

        last_time

        and (now - last_time) < DUPLICATE_WINDOW_SECONDS

    )

def is_on_cooldown(symbol, action):

    key = f"{symbol}_{action}"

    now = time.time()

    last_trade = _cooldown.get(key)

    return bool(

        last_trade

        and (now - last_trade)

        < SYMBOL_ACTION_COOLDOWN_SECONDS

    )

def mark_traded(symbol, action):

    _cooldown[f"{symbol}_{action}"] = time.time()

# ============================================================================

# VALIDATION DU WEBHOOK

# ============================================================================

def validate_webhook(symbol, action):

    if not symbol:

        return False, "symbole vide"

    if not symbol.isalnum():

        return False, "symbole invalide"

    if len(symbol) > 10:

        return False, "symbole trop long"

    if action not in ("buy", "sell"):

        return False, "action invalide"

    return True, None

# ============================================================================

# CALCUL DE POSITION

# ============================================================================

def calculate_buy_quantity(symbol, price):

    """

    Calcule la quantité à acheter.

    Le serveur ignore totalement la quantité provenant

    de TradingView.

    Formule principale :

        risque $ = equity × risque %

        risque/action =

            prix × stop %

        qty risque =

            risque $ / risque/action

    Puis on applique :

        - MAX_QTY

        - MAX_POSITION_VALUE_PERCENT

        - buying power

    """

    account = get_account()

    equity = float(account["equity"])

    buying_power = float(account["buying_power"])

    if equity <= 0:

        raise ValueError("Equity Alpaca invalide")

    if buying_power <= 0:

        raise ValueError("Buying power insuffisant")

    # ------------------------------------------------------------------------

    # Budget de risque

    # ------------------------------------------------------------------------

    risk_budget = (

        equity

        * (RISK_PER_TRADE_PERCENT / 100.0)

    )

    # ------------------------------------------------------------------------

    # Risque théorique par action

    # ------------------------------------------------------------------------

    risk_per_share = (

        price

        * (STOP_LOSS_PERCENT / 100.0)

    )

    if risk_per_share <= 0:

        raise ValueError("Risque par action invalide")

    # ------------------------------------------------------------------------

    # Quantité basée sur le risque

    # ------------------------------------------------------------------------

    qty_by_risk = int(

        risk_budget / risk_per_share

    )

    # ------------------------------------------------------------------------

    # Valeur maximale de position

    # ------------------------------------------------------------------------

    max_position_value = (

        equity

        * (MAX_POSITION_VALUE_PERCENT / 100.0)

    )

    qty_by_position_value = int(

        max_position_value / price

    )

    # ------------------------------------------------------------------------

    # Buying power

    # ------------------------------------------------------------------------

    qty_by_buying_power = int(

        buying_power / price

    )

    # ------------------------------------------------------------------------

    # Quantité finale

    # ------------------------------------------------------------------------

    qty = min(

        qty_by_risk,

        qty_by_position_value,

        qty_by_buying_power,

        MAX_QTY,

    )

    log(

        f"CALCUL POSITION {symbol} | "

        f"equity={equity:.2f} | "

        f"buying_power={buying_power:.2f} | "

        f"prix={price:.2f} | "

        f"risk_budget={risk_budget:.2f} | "

        f"risk/action={risk_per_share:.2f} | "

        f"qty_risk={qty_by_risk} | "

        f"qty_position={qty_by_position_value} | "

        f"qty_buying_power={qty_by_buying_power} | "

        f"qty_finale={qty}"

    )

    if qty < 1:

        raise ValueError(

            "Capital insuffisant pour ouvrir une position "

            "respectant les règles de risque"

        )

    return qty

# ============================================================================

# ORDRES

# ============================================================================

def submit_market_order(symbol, side, qty):

    order = {

        "symbol": symbol,

        "qty": qty,

        "side": side,

        "type": "market",

        "time_in_force": "day",

    }

    response = requests.post(

        f"{ALPACA_BASE_URL}/v2/orders",

        json=order,

        headers=HEADERS,

        timeout=10,

    )

    response.raise_for_status()

    return response.json()

# ============================================================================

# SUIVI DU REMPLISSAGE

# ============================================================================

def poll_order_fill(order_id):

    for _ in range(FILL_POLL_ATTEMPTS):

        try:

            response = requests.get(

                f"{ALPACA_BASE_URL}/v2/orders/{order_id}",

                headers=HEADERS,

                timeout=10,

            )

            response.raise_for_status()

            order = response.json()

            if order.get("filled_avg_price"):

                return order

            if order.get("status") in (

                "canceled",

                "rejected",

                "expired",

            ):

                return order

        except Exception as e:

            log(

                f"Erreur polling ordre {order_id}: {e}"

            )

        time.sleep(FILL_POLL_DELAY_SECONDS)

    return None

# ============================================================================

# SL / TP

# ============================================================================

def place_exit_orders(symbol, qty, entry_price):

    """

    OCO :

        - stop-loss

        - take-profit

    Utilise la quantité réellement remplie.

    """

    if qty <= 0:

        raise ValueError("Quantité SL/TP invalide")

    stop_price = round(

        entry_price

        * (1 - STOP_LOSS_PERCENT / 100),

        2,

    )

    take_profit_price = round(

        entry_price

        * (1 + TAKE_PROFIT_PERCENT / 100),

        2,

    )

    if stop_price <= 0:

        raise ValueError("Stop-loss invalide")

    order = {

        "symbol": symbol,

        "qty": qty,

        "side": "sell",

        "type": "limit",

        "limit_price": take_profit_price,

        "time_in_force": "gtc",

        "order_class": "oco",

        "stop_loss": {

            "stop_price": stop_price,

        },

    }

    response = requests.post(

        f"{ALPACA_BASE_URL}/v2/orders",

        json=order,

        headers=HEADERS,

        timeout=10,

    )

    response.raise_for_status()

    return stop_price, take_profit_price

# ============================================================================

# WEBHOOK

# ============================================================================

@app.route("/webhook", methods=["POST"])

def webhook():

    data = request.get_json(

        force=True,

        silent=True,

    ) or {}

    timestamp = time.strftime(

        "%Y-%m-%d %H:%M:%S"

    )

    # ------------------------------------------------------------------------

    # SECRET

    # ------------------------------------------------------------------------

    if data.get("secret") != WEBHOOK_SECRET:

        log("Requête reçue avec un secret invalide.")

        return jsonify({

            "error": "secret invalide"

        }), 403

    # ------------------------------------------------------------------------

    # DONNÉES

    # ------------------------------------------------------------------------

    action = str(

        data.get("action", "")

    ).lower()

    symbol = str(

        data.get("symbol", "")

    ).upper()

    # IMPORTANT :

    # On ne lit volontairement PAS data["qty"].

    #

    # TradingView peut envoyer 301, 500 ou autre chose :

    # cette valeur est complètement ignorée.

    valid, error = validate_webhook(

        symbol,

        action,

    )

    if not valid:

        msg = (

            f"⚠️ [{timestamp}] "

            f"Signal rejeté : {error}"

        )

        log(msg)

        send_telegram(msg)

        return jsonify({

            "error": error

        }), 400

    # ------------------------------------------------------------------------

    # TRADING ACTIVÉ ?

    # ------------------------------------------------------------------------

    if not TRADING_ENABLED:

        msg = (

            f"⏸️ [{timestamp}] "

            f"Signal {action.upper()} {symbol} reçu "

            f"mais trading désactivé"

        )

        log(msg)

        send_telegram(msg)

        return jsonify({

            "info": "trading désactivé"

        }), 200

    # ------------------------------------------------------------------------

    # ANTI-DOUBLON

    # ------------------------------------------------------------------------

    if is_duplicate_signal(

        symbol,

        action,

    ):

        log(

            f"Signal doublon ignoré : "

            f"{action.upper()} {symbol}"

        )

        return jsonify({

            "info": "doublon"

        }), 200

    # ------------------------------------------------------------------------

    # COOLDOWN

    # ------------------------------------------------------------------------

    if is_on_cooldown(

        symbol,

        action,

    ):

        msg = (

            f"⚠️ [{timestamp}] "

            f"{action.upper()} {symbol} ignoré "

            f"— cooldown"

        )

        log(msg)

        send_telegram(msg)

        return jsonify({

            "info": "cooldown"

        }), 200

    # ------------------------------------------------------------------------

    # RISQUE QUOTIDIEN

    # ------------------------------------------------------------------------

    try:

        daily_ok, daily_loss = (

            check_daily_loss_limit()

        )

        if not daily_ok:

            msg = (

                f"🛑 [{timestamp}] "

                f"Trading suspendu : perte quotidienne "

                f"{daily_loss:.2f}%"

            )

            log(msg)

            send_telegram(msg)

            return jsonify({

                "info": "limite de perte quotidienne atteinte"

            }), 200

        # ====================================================================

        # BUY

        # ====================================================================

        if action == "buy":

            positions = get_open_positions()

            # ---------------------------------------------------------------

            # Nombre maximum de positions

            # ---------------------------------------------------------------

            if len(positions) >= MAX_OPEN_POSITIONS:

                msg = (

                    f"⚠️ [{timestamp}] "

                    f"BUY {symbol} ignoré — "

                    f"{len(positions)} positions déjà ouvertes"

                )

                log(msg)

                send_telegram(msg)

                return jsonify({

                    "info": "nombre maximum de positions atteint"

                }), 200

            # ---------------------------------------------------------------

            # Ne pas racheter le même symbole

            # ---------------------------------------------------------------

            existing_position = get_position(symbol)

            if existing_position:

                existing_qty = float(

                    existing_position.get("qty", 0)

                )

                if existing_qty > 0:

                    msg = (

                        f"⚠️ [{timestamp}] "

                        f"BUY {symbol} ignoré — "

                        f"position déjà ouverte "

                        f"({existing_qty:g} actions)"

                    )

                    log(msg)

                    send_telegram(msg)

                    return jsonify({

                        "info": "position déjà ouverte"

                    }), 200

            # ---------------------------------------------------------------

            # Prix

            # ---------------------------------------------------------------

            price = get_latest_price(symbol)

            # ---------------------------------------------------------------

            # CALCUL DE QUANTITÉ

            # ---------------------------------------------------------------

            qty = calculate_buy_quantity(

                symbol,

                price,

            )

            # ---------------------------------------------------------------

            # Sécurité finale

            # ---------------------------------------------------------------

            if qty < 1:

                raise ValueError(

                    "Quantité finale inférieure à 1"

                )

            if qty > MAX_QTY:

                raise ValueError(

                    f"Protection : qty={qty} > MAX_QTY={MAX_QTY}"

                )

            estimated_value = qty * price

            log(

                f"ORDRE AUTORISÉ : "

                f"BUY {qty} {symbol} "

                f"≈ {estimated_value:.2f}$"

            )

            # ---------------------------------------------------------------

            # ENVOI ALPACA

            # ---------------------------------------------------------------

            order_result = submit_market_order(

                symbol,

                "buy",

                qty,

            )

            mark_traded(

                symbol,

                action,

            )

            order_id = order_result["id"]

            # ---------------------------------------------------------------

            # ATTENTE DU FILL

            # ---------------------------------------------------------------

            filled_order = poll_order_fill(

                order_id

            )

            if not filled_order:

                msg = (

                    f"⚠️ [{timestamp}] "

                    f"BUY {qty} {symbol} envoyé, "

                    f"mais statut de remplissage indisponible"

                )

                log(msg)

                send_telegram(msg)

                return jsonify(

                    order_result

                ), 200

            status = filled_order.get(

                "status",

                "inconnu",

            )

            filled_qty_raw = filled_order.get(

                "filled_qty",

                0,

            )

            filled_qty = int(

                float(filled_qty_raw or 0)

            )

            filled_avg_price = (

                filled_order.get(

                    "filled_avg_price"

                )

            )

            # ---------------------------------------------------------------

            # ORDRE REFUSÉ / ANNULÉ

            # ---------------------------------------------------------------

            if status in (

                "rejected",

                "canceled",

                "expired",

            ):

                msg = (

                    f"❌ [{timestamp}] "

                    f"BUY {symbol} refusé/annulé "

                    f"| statut={status}"

                )

                log(msg)

                send_telegram(msg)

                return jsonify(

                    filled_order

                ), 200

            # ---------------------------------------------------------------

            # SL / TP

            # ---------------------------------------------------------------

            sl_tp_text = (

                "SL/TP non posés"

            )

            if (

                status == "filled"

                and filled_qty > 0

                and filled_avg_price

            ):

                entry_price = float(

                    filled_avg_price

                )

                try:

                    stop_price, take_profit_price = (

                        place_exit_orders(

                            symbol,

                            filled_qty,

                            entry_price,

                        )

                    )

                    sl_tp_text = (

                        f"SL {stop_price:.2f} | "

                        f"TP {take_profit_price:.2f}"

                    )

                except Exception as e:

                    sl_tp_text = (

                        f"⚠️ échec SL/TP : {e}"

                    )

                    log(

                        f"ERREUR CRITIQUE SL/TP "

                        f"{symbol}: {e}"

                    )

                    send_telegram(

                        f"🚨 SL/TP non posé sur "

                        f"{symbol} après BUY : {e}"

                    )

            # ---------------------------------------------------------------

            # TELEGRAM

            # ---------------------------------------------------------------

            if filled_avg_price:

                price_text = (

                    f"Prix : "

                    f"{float(filled_avg_price):.2f}$"

                )

            else:

                price_text = (

                    "Prix : indisponible"

                )

            msg = (

                f"✅ [{timestamp}] "

                f"BUY {filled_qty} {symbol}\n"

                f"{price_text}\n"

                f"{sl_tp_text}\n"

                f"Risque/trade : "

                f"{RISK_PER_TRADE_PERCENT:.2f}%\n"

                f"Statut : {status}"

            )

            log(msg)

            send_telegram(msg)

            return jsonify(

                filled_order

            ), 200

        # ====================================================================

        # SELL

        # ====================================================================

        else:

            position = get_position(symbol)

            if not position:

                msg = (

                    f"⚠️ [{timestamp}] "

                    f"SELL {symbol} ignoré — "

                    f"aucune position"

                )

                log(msg)

                send_telegram(msg)

                return jsonify({

                    "info": "aucune position"

                }), 200

            held_qty = float(

                position.get("qty", 0)

            )

            if held_qty <= 0:

                msg = (

                    f"⚠️ [{timestamp}] "

                    f"SELL {symbol} ignoré — "

                    f"quantité détenue nulle"

                )

                log(msg)

                send_telegram(msg)

                return jsonify({

                    "info": "quantité détenue nulle"

                }), 200

            # Comme les BUY sont limités à MAX_QTY,

            # on ferme ici toute la position détenue.

            sell_qty = int(held_qty)

            if sell_qty <= 0:

                raise ValueError(

                    "Quantité de vente invalide"

                )

            # ---------------------------------------------------------------

            # Envoi SELL

            # ---------------------------------------------------------------

            order_result = submit_market_order(

                symbol,

                "sell",

                sell_qty,

            )

            mark_traded(

                symbol,

                action,

            )

            status = order_result.get(

                "status",

                "inconnu",

            )

            msg = (

                f"✅ [{timestamp}] "

                f"SELL {sell_qty} {symbol}\n"

                f"Statut : {status}"

            )

            log(msg)

            send_telegram(msg)

            return jsonify(

                order_result

            ), 200

    # =========================================================================

    # ERREURS

    # =========================================================================

    except requests.exceptions.RequestException as e:

        msg = (

            f"🚨 [{timestamp}] "

            f"Erreur Alpaca sur {symbol} : {e}"

        )

        log(msg)

        send_telegram(msg)

        return jsonify({

            "error": "erreur de connexion à Alpaca",

            "details": str(e),

        }), 502

    except Exception as e:

        msg = (

            f"🚨 [{timestamp}] "

            f"Erreur sur {symbol} : {e}"

        )

        log(msg)

        send_telegram(msg)

        return jsonify({

            "error": "erreur inattendue",

            "details": str(e),

        }), 500

# ============================================================================

# HEALTH CHECK

# ============================================================================

@app.route("/", methods=["GET"])

def health_check():

    try:

        account = get_account()

        alpaca_status = (

            f"Alpaca OK | "

            f"equity={account.get('equity')} | "

            f"buying_power={account.get('buying_power')}"

        )

    except Exception as e:

        alpaca_status = (

            f"Alpaca injoignable : {e}"

        )

    status = (

        "actif"

        if TRADING_ENABLED

        else "en pause"

    )

    return (

        f"Bot webhook {status}. "

        f"{alpaca_status}",

        200,

    )

# ============================================================================

# DÉMARRAGE

# ============================================================================

check_env_vars_or_exit()

if __name__ == "__main__":

    port = int(

        os.environ.get(

            "PORT",

            5000,

        )

    )

    app.run(

        host="0.0.0.0",

        port=port,

    )
