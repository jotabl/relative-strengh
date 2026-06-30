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
from alpaca.trading.requests import (LimitOrderRequest, MarketOrderRequest,
                                      TrailingStopOrderRequest, StopLimitOrderRequest)
from alpaca.trading.enums import OrderSide, TimeInForce

API_KEY    = "PKAUNSXX2KN5TGHXMK7P3CFCKN"
API_SECRET = "ET3LUrxB9SN1sKtG6K5AbvqjYyH41H8AjciocFrMNLcY"

TG_TOKEN   = "8976723197:AAGlyPd7EIVNQG_UpWAaDITnNpv13s63z3Y"
TG_CHAT_ID = "803164602"

# Top 20 — backtest 52 tickers, 365d, MIN_RR=3.0, soporte corregido
# Ordenados por PnL% total (positivos, mínimo 5 trades)
WATCHLIST = [
    "NEE",   #  50% WR  +28.5%  18 trades
    "CVX",   #  50% WR  +26.0%  22 trades
    "KO",    #  48% WR  +21.8%  21 trades
    "COP",   #  40% WR  +21.8%  20 trades
    "GE",    #  75% WR  +21.5%   8 trades
    "JNJ",   #  39% WR  +21.3%  23 trades
    "SBUX",  #  50% WR  +20.6%  14 trades
    "COST",  #  53% WR  +13.8%  19 trades
    "XOM",   #  36% WR  +13.8%  22 trades
    "UNH",   #  33% WR  +13.7%  15 trades
    "AXP",   #  57% WR  +15.4%   7 trades
    "INTC",  #  60% WR  +11.1%   5 trades
    "MCD",   #  45% WR  +10.0%  22 trades
    "BAC",   #  36% WR   +9.0%  11 trades
    "SLB",   #  36% WR   +9.0%  11 trades
    "TXN",   #  36% WR   +8.3%  11 trades
    "DE",    #  36% WR   +8.0%  11 trades
    "PG",    #  33% WR   +4.8%  15 trades
    "CAT",   #  40% WR   +4.5%   5 trades
    "AAPL",  #  40% WR   +3.9%  10 trades
]

BT_RANK = {
    "NEE":  ("+28.5%", " 50%"),
    "CVX":  ("+26.0%", " 50%"),
    "KO":   ("+21.8%", " 48%"),
    "COP":  ("+21.8%", " 40%"),
    "GE":   ("+21.5%", " 75%"),
    "JNJ":  ("+21.3%", " 39%"),
    "SBUX": ("+20.6%", " 50%"),
    "COST": ("+13.8%", " 53%"),
    "XOM":  ("+13.8%", " 36%"),
    "UNH":  ("+13.7%", " 33%"),
    "AXP":  ("+15.4%", " 57%"),
    "INTC": ("+11.1%", " 60%"),
    "MCD":  ("+10.0%", " 45%"),
    "BAC":  ( "+9.0%", " 36%"),
    "SLB":  ( "+9.0%", " 36%"),
    "TXN":  ( "+8.3%", " 36%"),
    "DE":   ( "+8.0%", " 36%"),
    "PG":   ( "+4.8%", " 33%"),
    "CAT":  ( "+4.5%", " 40%"),
    "AAPL": ( "+3.9%", " 40%"),
}

MAX_RISK_PCT   = 0.01   # 1% del capital por trade
MAX_POSITIONS  = 5
RS_THRESHOLD   = -0.10  # umbral óptimo por backtest: mejor WR en RS <= -0.10
TREND_MAX_DD   = 0.10   # no operar si >10% bajo máximo 60d
NEAR_PCT       = 0.02
STOP_BUFFER    = 0.01
ENTRY_BUFFER   = 0.005
KEY_LOOKBACK   = 60
RS_LOOKBACK    = 20
MIN_TOUCHES    = 2
MIN_RR         = 3.0    # ratio mínimo riesgo:recompensa

data_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
trade_client = TradingClient(API_KEY, API_SECRET, paper=True)

COOLDOWN_FILE = "cooldown.json"

def load_cooldown() -> dict:
    try:
        with open(COOLDOWN_FILE) as f:
            raw = json.load(f)
        today = datetime.date.today()
        # Solo conservar entradas de hoy (cooldown diario)
        return {k: datetime.date.fromisoformat(v) for k, v in raw.items()
                if datetime.date.fromisoformat(v) >= today}
    except Exception:
        return {}

def save_cooldown(d: dict):
    try:
        with open(COOLDOWN_FILE, "w") as f:
            json.dump({k: v.isoformat() for k, v in d.items()}, f)
    except Exception:
        pass

# Registro de setups ya notificados (persiste en disco entre reinicios)
notified_setups: dict[str, datetime.date] = load_cooldown()
# Registro de trades ejecutados para seguimiento de cierre
open_trades: dict[str, dict] = {}
# Simulaciones virtuales de setups detectados (sin capital real)
sim_trades: dict[str, dict] = {}


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


def tg_setup(ticker, price, key, entry, target, stop, rs, rr):
    gain_pct = (target - entry) / entry * 100
    risk_pct = (entry - stop)  / entry * 100
    lines = [
        f"👁 SETUP DETECTADO — {ticker}",
        "━━━━━━━━━━━━━━━━━━━",
        f"💹 Precio:   ${price:.2f}",
        f"🎯 Nivel:    ${key:.2f}",
        f"⚡ RS Score: {rs:+.3f}",
        "",
        "NIVELES:",
        f"  🟢 Entrada:  ${entry:.2f}",
        f"  🎯 Objetivo: ${target:.2f}  (+{gain_pct:.2f}%)",
        f"  🛑 Stop:     ${stop:.2f}  (-{risk_pct:.2f}%)",
        f"  📐 R:R:      1:{rr:.2f}",
    ]
    tg_send("\n".join(lines))


def tg_sim_close(ticker, entry, exit_price, target, stop, result):
    pnl_pct = (exit_price - entry) / entry * 100
    risk_pct = (entry - stop) / entry * 100
    gain_pct = (target - entry) / entry * 100
    icon = "✅" if result == "TP" else "❌"
    label = "TARGET ALCANZADO" if result == "TP" else "STOP LOSS TOCADO"
    lines = [
        f"{icon} SIMULACION CERRADA — {ticker}",
        "━━━━━━━━━━━━━━━━━━━",
        f"Resultado:  {label}",
        f"Entrada:    ${entry:.2f}",
        f"Salida:     ${exit_price:.2f}",
        f"P&L:        {pnl_pct:+.2f}%",
        "",
        "Setup original:",
        f"  🎯 Target: ${target:.2f}  (+{gain_pct:.2f}%)",
        f"  🛑 Stop:   ${stop:.2f}  (-{risk_pct:.2f}%)",
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
    # Validaciones del setup
    risk_per = entry - stop
    if risk_per <= 0:
        print(f"  [!] {ticker}: riesgo inválido"); return
    rr = (target - entry) / risk_per
    if rr < MIN_RR:
        print(f"  [!] {ticker}: R:R={rr:.2f} < {MIN_RR} — rechazada"); return
    if (target - entry) / entry * 100 < MIN_TARGET_PCT * 100:
        print(f"  [!] {ticker}: target muy chico — rechazada"); return
    if quote <= stop:
        print(f"  [!] {ticker}: precio bajo stop — rechazada"); return

    account   = trade_client.get_account()
    equity    = float(account.equity)
    positions = {p.symbol: p for p in trade_client.get_all_positions()}
    open_orders = {o.symbol for o in trade_client.get_orders()
                   if o.status.value in ("new", "partially_filled", "accepted")}

    if ticker in positions:
        return
    if ticker in open_orders:
        print(f"  [!] {ticker}: ya tiene orden abierta — skip")
        return
    if len(positions) >= MAX_POSITIONS:
        print(f"  [!] Máximo posiciones — skip {ticker}"); return

    buying_power = float(account.buying_power)
    risk_amt     = min(equity * MAX_RISK_PCT, buying_power * 0.9)
    qty = max(1, int(risk_amt / risk_per))
    if qty * quote > buying_power * 0.95:
        qty = max(1, int(buying_power * 0.95 / quote))
    if qty * quote > buying_power:
        print(f"  [!] {ticker}: buying power insuficiente — skip"); return

    try:
        # Entrada: mercado si precio ya pasó el nivel, límite si aún está debajo
        if quote >= entry:
            order = trade_client.submit_order(MarketOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            exec_price = quote
            print(f"  ✅ MERCADO: {ticker} ×{qty} @ ~${quote:.2f}")
        else:
            order = trade_client.submit_order(LimitOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(entry, 2),
            ))
            exec_price = entry
            print(f"  ✅ LÍMITE:  {ticker} ×{qty} @ ${entry:.2f}")

        # Stop loss real en Alpaca — se ejecuta automáticamente sin depender del bot
        try:
            trade_client.submit_order(StopLimitOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop * 1.001, 2),   # trigger del stop
                limit_price=round(stop * 0.998, 2),  # precio límite de venta
            ))
            print(f"  🛡️  STOP LOSS colocado @ ${stop:.2f}")
        except Exception as se:
            print(f"  [!] Stop loss no colocado: {se}")

        print(f"  ✅ ORDEN ENVIADA: {ticker} ×{qty} @ ${exec_price:.2f}")
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


MIN_TARGET_PCT = 0.03   # target mínimo 3% desde entry

def find_next_res(levels: list, entry: float, stop: float = None) -> float:
    """Retorna el target que logra R:R >= MIN_RR Y al menos MIN_TARGET_PCT desde entry.
    Si ningún nivel lo cumple, proyecta el mayor entre ambos requisitos."""
    above = sorted([p for p, _ in levels if p > entry])
    if stop is not None:
        risk = entry - stop
        min_by_rr     = entry + MIN_RR * risk
        min_by_pct    = entry * (1 + MIN_TARGET_PCT)
        min_target    = max(min_by_rr, min_by_pct)
        for p in above:
            if p >= min_target:
                return p
        return min_target
    return above[0] if above else entry * (1 + MIN_TARGET_PCT)


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
    # Soporte: nivel DEBAJO del precio (precio está encima, soporte aguantó)
    candidates = [(p, t) for p, t in levels if p <= current]
    if not candidates:
        return None, None
    key = max(candidates, key=lambda x: x[0])  # soporte más cercano debajo
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
    # Precio debe estar POR ENCIMA del nivel (soporte aguantó)
    above = current >= key
    near  = above and (current - key) / key <= NEAR_PCT
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
            target = find_next_res(levels, entry, stop)
            rr     = (target - entry) / (entry - stop) if (entry - stop) > 0 else 0
            print(f"     ► {ticker}  precio=${quote:.2f}  nivel=${key_price:.2f}  "
                  f"entrada=${entry:.2f}  stop=${stop:.2f}  target=${target:.2f}  "
                  f"R:R=1:{rr:.1f}  RS={rs:+.3f}")
            # Validaciones del setup completo
            target_pct = (target - entry) / entry * 100
            setup_ok = (
                rr >= MIN_RR and                        # R:R mínimo 1:3
                target_pct >= MIN_TARGET_PCT * 100 and  # target mínimo 3%
                quote > stop and                        # precio sobre el stop
                quote >= key_price                      # precio sobre el soporte
            )
            # Notificar y ejecutar si no fue alertado hoy y setup es válido
            today = datetime.date.today()
            if notified_setups.get(ticker) != today and setup_ok:
                notified_setups[ticker] = today
                save_cooldown(notified_setups)
                tg_setup(ticker, quote, key_price, entry, target, stop, rs, rr)
                if ticker not in sim_trades:
                    sim_trades[ticker] = {
                        "entry": entry, "target": target, "stop": stop,
                        "date": today,
                    }
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
    tg_send("🤖 RS Scanner iniciado\nMonitoreando: " + ", ".join(WATCHLIST))

    while True:
        try:
            # Chequear cierres de simulaciones virtuales
            live_quotes = fetch_quote(list(sim_trades.keys())) if sim_trades else {}
            for ticker, sim in list(sim_trades.items()):
                price = live_quotes.get(ticker)
                if not price:
                    continue
                # Marcar cuando el precio alcanzó la entrada (trade "entrado")
                if not sim.get("filled") and price >= sim["entry"]:
                    sim["filled"] = True
                # Solo evaluar TP/SL si el trade ya entró
                if not sim.get("filled"):
                    continue
                if price >= sim["target"]:
                    tg_sim_close(ticker, sim["entry"], sim["target"], sim["target"], sim["stop"], "TP")
                    del sim_trades[ticker]
                    print(f"  🎯 SIM {ticker} → TARGET ALCANZADO ${sim['target']:.2f}")
                elif price <= sim["stop"]:
                    tg_sim_close(ticker, sim["entry"], sim["stop"], sim["target"], sim["stop"], "SL")
                    del sim_trades[ticker]
                    print(f"  🛑 SIM {ticker} → STOP LOSS ${sim['stop']:.2f}")

            # Chequear cierres de posiciones abiertas
            current_positions = {p.symbol: p for p in trade_client.get_all_positions()}
            open_orders_syms  = {o.symbol for o in trade_client.get_orders()
                                 if o.status.value in ("new", "partially_filled", "accepted")}
            for ticker, trade in list(open_trades.items()):
                # Ignorar si todavía hay una orden abierta (límite no llenada aún)
                if ticker in open_orders_syms:
                    continue
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
