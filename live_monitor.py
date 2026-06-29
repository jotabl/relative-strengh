#!/usr/bin/env python3
"""
Live Monitor — Relative Strength Scanner
Seguimiento en tiempo real de los 10 mejores tickers vs SPY.
Refresca cada N segundos y muestra el estado del mercado.

Uso:
  python3 live_monitor.py          # refresca cada 60s
  python3 live_monitor.py 30       # refresca cada 30s
"""

import sys
import os
import time
import datetime
import statistics
import warnings
import json
import urllib.request
warnings.filterwarnings("ignore")

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

API_KEY    = "PKAUNSXX2KN5TGHXMK7P3CFCKN"
API_SECRET = "ET3LUrxB9SN1sKtG6K5AbvqjYyH41H8AjciocFrMNLcY"

TG_TOKEN   = "8976723197:AAGlyPd7EIVNQG_UpWAaDITnNpv13s63z3Y"
TG_CHAT_ID = "803164602"

# Top 20 — backtest 52 tickers, 365d, cooldown 10d, filtro tendencia
# Ordenados por WR, excluidos tickers con <2 trades históricos
WATCHLIST = [
    "V",     # 100% WR  +7.1%   3 trades
    "MSFT",  # 100% WR  +3.4%   2 trades
    "TGT",   #  83% WR +11.1%   6 trades
    "XOM",   #  75% WR  +5.5%   4 trades
    "CAT",   #  75% WR  +7.0%   4 trades
    "NKE",   #  67% WR  +4.0%   3 trades
    "INTC",  #  67% WR  +4.9%   3 trades
    "AMZN",  #  67% WR  +4.5%   3 trades
    "CVX",   #  62% WR  +7.1%   8 trades
    "JPM",   #  60% WR  +5.1%   5 trades
    "AAPL",  #  57% WR  +6.0%   7 trades
    "MCD",   #  57% WR  +4.3%   7 trades
    "COP",   #  50% WR  +4.8%  10 trades
    "UNH",   #  50% WR  +2.7%   6 trades
    "SBUX",  #  50% WR  +2.9%   6 trades
    "GOOGL", #  50% WR  +1.6%   4 trades
    "KO",    #  47% WR  +8.9%  15 trades
    "COST",  #  46% WR  +7.2%  13 trades
    "NEE",   #  44% WR  +2.9%   9 trades
    "LLY",   #  43% WR  +2.2%   7 trades
]

BT_RANK = {
    "V":     ("+7.1%",  "100%"),
    "MSFT":  ("+3.4%",  "100%"),
    "TGT":   ("+11.1%", " 83%"),
    "XOM":   ("+5.5%",  " 75%"),
    "CAT":   ("+7.0%",  " 75%"),
    "NKE":   ("+4.0%",  " 67%"),
    "INTC":  ("+4.9%",  " 67%"),
    "AMZN":  ("+4.5%",  " 67%"),
    "CVX":   ("+7.1%",  " 62%"),
    "JPM":   ("+5.1%",  " 60%"),
    "AAPL":  ("+6.0%",  " 57%"),
    "MCD":   ("+4.3%",  " 57%"),
    "COP":   ("+4.8%",  " 50%"),
    "UNH":   ("+2.7%",  " 50%"),
    "SBUX":  ("+2.9%",  " 50%"),
    "GOOGL": ("+1.6%",  " 50%"),
    "KO":    ("+8.9%",  " 47%"),
    "COST":  ("+7.2%",  " 46%"),
    "NEE":   ("+2.9%",  " 44%"),
    "LLY":   ("+2.2%",  " 43%"),
}

MAX_RISK_PCT   = 0.01   # 1% del capital por trade
MAX_POSITIONS  = 5
RS_THRESHOLD   = 0.5
TREND_MAX_DD   = 0.10   # no operar si >10% bajo máximo 60d
NEAR_PCT       = 0.02
STOP_BUFFER    = 0.01
ENTRY_BUFFER   = 0.005
KEY_LOOKBACK   = 60
RS_LOOKBACK    = 20
MIN_TOUCHES    = 2

data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
trade_client = TradingClient(API_KEY, API_SECRET, paper=True)

# Registro de setups ya notificados (evita spam)
notified_setups: dict[str, datetime.date] = {}
# Registro de trades ejecutados para seguimiento de cierre
open_trades: dict[str, dict] = {}


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg_send(text: str):
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": text,
                           "disable_web_page_preview": True}).encode()
        req  = urllib.request.Request(url, data=data,
                                      headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        print(f"  [Telegram OK]")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  [Telegram error {e.code}] {body[:120]}")
    except Exception as e:
        print(f"  [Telegram error] {e}")


def tg_entry(ticker, price, key, entry, target, stop, rs, rr, qty, equity):
    gain_pct = (target - entry) / entry * 100
    risk_pct = (entry - stop)  / entry * 100
    lines = [
        f"🟢 ENTRADA — {ticker}",
        "━━━━━━━━━━━━━━━━━━━",
        f"💹 Precio:   ${price:.2f}",
        f"🎯 Nivel:    ${key:.2f}",
        f"⚡ RS Score: {rs:+.3f}",
        "",
        "TRADE SETUP:",
        f"  🟢 Entrada:  ${entry:.2f}",
        f"  🎯 Objetivo: ${target:.2f}  (+{gain_pct:.2f}%)",
        f"  🛑 Stop:     ${stop:.2f}  (-{risk_pct:.2f}%)",
        f"  📐 R:R:      1:{rr:.2f}",
        f"  📦 Qty:      {qty} acciones",
        "",
        f"💼 Capital: ${equity:,.0f}",
    ]
    tg_send("\n".join(lines))


def tg_close(ticker, entry, exit_price, qty, result):
    pnl_pct = (exit_price - entry) / entry * 100
    pnl_usd = (exit_price - entry) * qty
    icon    = "✅" if pnl_usd > 0 else "❌"
    lines = [
        f"{icon} CIERRE — {ticker}",
        "━━━━━━━━━━━━━━━━━━━",
        f"Entrada:   ${entry:.2f}",
        f"Salida:    ${exit_price:.2f}",
        f"PnL:       {pnl_pct:+.2f}%  (${pnl_usd:+.0f})",
        f"Qty:       {qty}",
        f"Resultado: {result}",
    ]
    tg_send("\n".join(lines))


# ── Ejecución de órdenes ──────────────────────────────────────────────────────

def place_entry(ticker, entry, stop, target, rs, key_price, quote):
    account   = trade_client.get_account()
    equity    = float(account.equity)
    positions = {p.symbol: p for p in trade_client.get_all_positions()}

    if ticker in positions:
        return
    if len(positions) >= MAX_POSITIONS:
        print(f"  [!] Máximo de posiciones alcanzado, no se opera {ticker}")
        return

    buying_power = float(account.buying_power)
    risk_amt     = min(equity * MAX_RISK_PCT, buying_power * 0.9)
    risk_per     = entry - stop
    if risk_per <= 0:
        return
    qty = max(1, int(risk_amt / risk_per))
    # Verificar que el costo total no supere el buying power disponible
    if qty * entry > buying_power * 0.95:
        qty = max(1, int(buying_power * 0.95 / entry))
    if qty * entry > buying_power:
        print(f"  [!] {ticker}: buying power insuficiente (${buying_power:.0f}), skip")
        return

    rr = (target - entry) / risk_per

    try:
        order = trade_client.submit_order(LimitOrderRequest(
            symbol=ticker, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(entry, 2),
        ))
        print(f"  ✅ ORDEN ENVIADA: {ticker} ×{qty} @ ${entry:.2f}")
        open_trades[ticker] = {
            "entry": entry, "stop": stop, "target": target,
            "qty": qty, "order_id": str(order.id), "date": datetime.date.today(),
        }
        tg_entry(ticker, quote, key_price, entry, target, stop, rs, rr, qty, equity)
    except Exception as e:
        print(f"  [!] Error orden {ticker}: {e}")


# ── Datos ─────────────────────────────────────────────────────────────────────

def fetch_bars(ticker: str, days: int) -> pd.DataFrame:
    end   = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days + 15)
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start, end=end,
        feed=DataFeed.IEX,
    )
    df = data_client.get_stock_bars(req).df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(ticker, level=0)
    df = df.sort_index()
    df.index = pd.to_datetime(df.index).normalize().tz_localize(None)
    return df.tail(days)


def fetch_quote(tickers: list) -> dict:
    """Precio bid/ask en tiempo real (o último cierre fuera de horario)."""
    try:
        req    = StockLatestQuoteRequest(symbol_or_symbols=tickers, feed=DataFeed.IEX)
        quotes = data_client.get_stock_latest_quote(req)
        return {t: (q.bid_price + q.ask_price) / 2 for t, q in quotes.items()}
    except Exception:
        return {}


# ── Análisis ──────────────────────────────────────────────────────────────────

def find_resistance_levels_full(df: pd.DataFrame) -> list:
    prices = list(df.tail(KEY_LOOKBACK)["high"]) + list(df.tail(KEY_LOOKBACK)["low"])
    clusters: list[list[float]] = []
    for p in sorted(prices):
        placed = False
        for c in clusters:
            if abs(p - statistics.mean(c)) / statistics.mean(c) <= 0.015:
                c.append(p); placed = True; break
        if not placed:
            clusters.append([p])
    return [(statistics.mean(c), len(c)) for c in clusters if len(c) >= MIN_TOUCHES]


def find_next_res(levels: list, entry: float) -> float:
    above = [(p, t) for p, t in levels if p > entry]
    return min(above, key=lambda x: x[0])[0] if above else entry * 1.03


def find_key_level(df: pd.DataFrame):
    prices = list(df.tail(KEY_LOOKBACK)["high"]) + list(df.tail(KEY_LOOKBACK)["low"])
    clusters: list[list[float]] = []
    for p in sorted(prices):
        placed = False
        for c in clusters:
            if abs(p - statistics.mean(c)) / statistics.mean(c) <= 0.015:
                c.append(p); placed = True; break
        if not placed:
            clusters.append([p])
    current = float(df["close"].iloc[-1])
    levels  = [(statistics.mean(c), len(c)) for c in clusters if len(c) >= MIN_TOUCHES]
    # Resistencia más cercana ≥ precio actual - 2%
    candidates = [(p, t) for p, t in levels if p >= current * 0.98]
    if not candidates:
        return None, None
    key = min(candidates, key=lambda x: x[0])
    return key  # (price, touches)


def calc_rs(stock_df: pd.DataFrame, spx_df: pd.DataFrame) -> float:
    s = stock_df["close"].pct_change().tail(RS_LOOKBACK)
    p = spx_df["close"].pct_change().tail(RS_LOOKBACK)
    common = s.index.intersection(p.index)
    s, p   = s.loc[common], p.loc[common]
    down   = p[p < 0].index
    if len(down) == 0:
        return 1.0
    sm, pm = s.loc[down].mean(), p.loc[down].mean()
    return float(sm / pm) if pm != 0 else 1.0


def is_downtrend(df: pd.DataFrame) -> bool:
    """True si el precio actual está >10% bajo el máximo de 60 días."""
    high60  = float(df.tail(60)["high"].max())
    current = float(df["close"].iloc[-1])
    return (high60 - current) / high60 > TREND_MAX_DD


def rs_label(rs: float) -> str:
    if rs < 0.0:   return "🔥 MUY FUERTE"
    if rs < 0.2:   return "🔥 FUERTE    "
    if rs < 0.4:   return "✅ BUENA     "
    if rs < 0.5:   return "✅ OK        "
    if rs < 0.7:   return "⚠️  DÉBIL    "
    return             "❌ MUY DÉBIL "


def setup_status(current: float, key: float, rs: float, spx_up: bool,
                 downtrend: bool = False) -> str:
    near  = abs(current - key) / key <= NEAR_PCT
    rs_ok = rs < RS_THRESHOLD
    if downtrend:
        return "🔴 TENDENCIA↓ "
    if spx_up and near and rs_ok:
        return "🟢 SETUP LISTO"
    if near and rs_ok:
        return "🟡 ESPERA SPX "
    if near:
        return "🟡 CERCA NIVEL"
    return "⚪ SIN SETUP  "


# ── Render ────────────────────────────────────────────────────────────────────

def clear():
    print("\n" * 2)


def render(spx_df, quotes, ticker_data, positions, account):
    now    = datetime.datetime.now()
    spx_c  = float(spx_df["close"].iloc[-1])
    spx_p  = float(spx_df["close"].iloc[-2])
    spx_ch = (spx_c - spx_p) / spx_p * 100
    spx_up = spx_c > spx_p
    spx_icon = "▲" if spx_up else "▼"

    clear()
    print("═" * 78)
    print(f"  🤖 RS SCANNER — LIVE MONITOR          {now.strftime('%Y-%m-%d  %H:%M:%S')}")
    print(f"  Cuenta: ${float(account.equity):>10,.2f}  │  Buying power: ${float(account.buying_power):>10,.2f}")
    print("═" * 78)

    # SPY status
    spy_quote = quotes.get("SPY", spx_c)
    print(f"\n  SPY  ${spy_quote:>7.2f}  {spx_icon} {spx_ch:+.2f}%  "
          f"({'REBOTANDO ✅' if spx_up else 'BAJANDO   ❌'})   "
          f"Zona: $735  →  Resistencia: $753\n")

    # Posiciones abiertas
    if positions:
        print(f"  {'─'*74}")
        print(f"  POSICIONES ABIERTAS:")
        for sym, pos in positions.items():
            qty    = pos.qty
            entry  = float(pos.avg_entry_price)
            mkt    = float(pos.market_value)
            unrl   = float(pos.unrealized_pl)
            unrl_p = float(pos.unrealized_plpc) * 100
            icon   = "📈" if unrl > 0 else "📉"
            print(f"  {icon} {sym:<6} ×{qty}  entrada ${entry:.2f}  "
                  f"mkt ${mkt:,.0f}  PnL {unrl_p:+.2f}% (${unrl:+.0f})")
        print()

    # Header tabla
    print(f"  {'─'*74}")
    print(f"  {'#':<3} {'Ticker':<6} {'Precio':>8} {'Chg%':>6} {'Nivel':>8} "
          f"{'Dist%':>6} {'RS':>6} {'RS Label':<15} {'Status':<15} {'BT PnL':>8}")
    print(f"  {'─'*74}")

    for i, (ticker, df, rs) in enumerate(ticker_data, 1):
        quote   = quotes.get(ticker, float(df["close"].iloc[-1]))
        prev    = float(df["close"].iloc[-2])
        chg     = (quote - prev) / prev * 100
        chg_ico = "▲" if chg > 0 else "▼"
        dtrend  = is_downtrend(df)

        key_price, touches = find_key_level(df)
        if key_price is None:
            dist, status = 0.0, "⚪ SIN NIVEL  "
        else:
            dist   = (quote - key_price) / key_price * 100
            status = setup_status(quote, key_price, rs, spx_up, dtrend)

        bt_pnl, bt_wr = BT_RANK.get(ticker, ("—", "—"))
        key_str     = f"${key_price:.2f}" if key_price else "  —   "
        touches_str = f"({touches}x)"     if touches   else ""
        trend_tag   = " ↓10%" if dtrend else ""

        in_pos = "💼" if ticker in positions else "  "
        print(f"  {i:<3} {ticker:<6} ${quote:>7.2f} {chg_ico}{abs(chg):>5.2f}% "
              f"{key_str:>8}{touches_str:<5} {dist:>+5.1f}% "
              f"{rs:>+6.2f} {rs_label(rs):<14} {status:<15} {bt_pnl:>7}{trend_tag} {in_pos}")

    print(f"  {'─'*74}")

    # Alertas activas (solo sin downtrend)
    alerts = []
    for t, d, r in ticker_data:
        q = quotes.get(t, float(d["close"].iloc[-1]))
        kp, kt = find_key_level(d)
        if kp and not is_downtrend(d) and setup_status(q, kp, r, spx_up).startswith("🟢"):
            alerts.append((t, d, r, kp, kt))

    if alerts:
        print(f"\n  🚨 ALERTAS — SETUP LISTO PARA OPERAR:")
        for ticker, df, rs, key_price, touches in alerts:
            quote  = quotes.get(ticker, float(df["close"].iloc[-1]))
            entry  = key_price * (1 + ENTRY_BUFFER)
            stop   = key_price * (1 - STOP_BUFFER)
            levels = find_resistance_levels_full(df)
            target = find_next_res(levels, entry)
            rr     = (target - entry) / (entry - stop) if (entry - stop) > 0 else 0
            print(f"     ► {ticker}  precio=${quote:.2f}  nivel=${key_price:.2f}  "
                  f"entrada=${entry:.2f}  stop=${stop:.2f}  target=${target:.2f}  "
                  f"R:R=1:{rr:.1f}  RS={rs:+.3f}")
            # Ejecutar si no fue notificado hoy
            today = datetime.date.today()
            if notified_setups.get(ticker) != today and rr >= 1.5:
                notified_setups[ticker] = today
                place_entry(ticker, entry, stop, target, rs, key_price, quote)

    print(f"\n  Próximo refresco en {INTERVAL}s  │  Ctrl+C para salir")
    print("═" * 78)


# ── Main loop ─────────────────────────────────────────────────────────────────

INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 60

def main():
    print("\n  Iniciando RS Live Monitor...")
    print(f"  Top 10 tickers del backtest  │  Refresco cada {INTERVAL}s\n")

    # Pre-cargar barras (lento la primera vez)
    print("  Descargando datos históricos...", end=" ", flush=True)
    needed = KEY_LOOKBACK + RS_LOOKBACK + 5
    spx_df = fetch_bars("SPY", needed)

    ticker_data = []
    all_syms = WATCHLIST + ["SPY"]
    for t in WATCHLIST:
        try:
            df = fetch_bars(t, needed)
            rs = calc_rs(df, spx_df)
            ticker_data.append((t, df, rs))
        except Exception as e:
            print(f"\n  [!] {t}: {e}")
    print("✅\n")
    time.sleep(1)

    # Notificación de inicio
    tg_send("🤖 <b>RS Scanner iniciado</b>\nMonitoreando: " + ", ".join(WATCHLIST))

    while True:
        try:
            # Chequear cierres de posiciones abiertas
            current_positions = {p.symbol: p for p in trade_client.get_all_positions()}
            for ticker, trade in list(open_trades.items()):
                if ticker not in current_positions:
                    # Posición cerrada (por stop o target)
                    quote = fetch_quote([ticker]).get(ticker, trade["entry"])
                    result = "WIN ✅" if quote >= trade["entry"] else "LOSS ❌"
                    tg_close(ticker, trade["entry"], quote, trade["qty"], result)
                    del open_trades[ticker]
                    print(f"  📤 {ticker} cerrado — {result}")

            # Actualizar RS con barras frescas
            spx_df = fetch_bars("SPY", needed)
            updated = []
            for ticker, _, _ in ticker_data:
                try:
                    df = fetch_bars(ticker, needed)
                    rs = calc_rs(df, spx_df)
                    updated.append((ticker, df, rs))
                except Exception:
                    pass
            ticker_data = updated

            # Quotes en tiempo real
            quotes = fetch_quote(WATCHLIST + ["SPY"])
            # fallback: usar último cierre si no hay quote
            for ticker, df, _ in ticker_data:
                if ticker not in quotes:
                    quotes[ticker] = float(df["close"].iloc[-1])
            if "SPY" not in quotes:
                quotes["SPY"] = float(spx_df["close"].iloc[-1])

            # Posiciones y cuenta
            positions = {p.symbol: p for p in trade_client.get_all_positions()}
            account   = trade_client.get_account()

            render(spx_df, quotes, ticker_data, positions, account)

        except KeyboardInterrupt:
            print("\n\n  Monitor detenido.\n")
            break
        except Exception as e:
            print(f"\n  [!] Error: {e}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
