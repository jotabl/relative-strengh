#!/usr/bin/env python3
"""
Relative Strength Scanner Bot — Alpaca Paper Trading
Detecta acciones con fuerza relativa positiva vs SPX y ejecuta trades.
"""

import os
import time
import datetime
import statistics
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ─── Configuración ────────────────────────────────────────────────────────────

API_KEY    = "PKAUNSXX2KN5TGHXMK7P3CFCKN"
API_SECRET = "ET3LUrxB9SN1sKtG6K5AbvqjYyH41H8AjciocFrMNLcY"
PAPER      = True

# Top 10 por backtest (365 días, cooldown 10 días)
WATCHLIST = [
    "COST",   # +19.8%  59% WR
    "XOM",    # +14.7%  78% WR
    "CAT",    # + 7.0%  75% WR
    "MCD",    # + 6.7%  62% WR
    "AAPL",   # + 6.0%  57% WR
    "V",      # + 5.6%  75% WR
    "JPM",    # + 5.1%  60% WR
    "AMZN",   # + 4.5%  67% WR
    "META",   # + 4.2%  60% WR
    "MSFT",   # + 3.4% 100% WR
]

# Parámetros de estrategia
RS_LOOKBACK_DAYS      = 20     # días para calcular RS
KEY_LEVEL_LOOKBACK    = 60     # días para detectar niveles clave
MIN_TOUCHES           = 2      # mínimo toques para validar nivel
RS_THRESHOLD          = 0.5    # RS score máximo para considerar fuerte
NEAR_BREAKOUT_PCT     = 0.02   # dentro del 2% del nivel = "cerca"
ENTRY_BUFFER_PCT      = 0.005  # entrada 0.5% sobre resistencia
STOP_BUFFER_PCT       = 0.01   # stop 1% bajo soporte
MIN_RR_RATIO          = 1.5    # ratio mínimo riesgo/beneficio
MAX_RISK_PCT          = 0.01   # máximo 1% del capital por trade
MAX_POSITIONS         = 5      # máximo posiciones simultáneas


# ─── Estructuras de datos ─────────────────────────────────────────────────────

@dataclass
class KeyLevel:
    price: float
    touch_count: int
    level_type: str       # "support" o "resistance"
    strength: str = "MEDIA"

    def __post_init__(self):
        if self.touch_count >= 4:
            self.strength = "ALTA"
        elif self.touch_count >= 2:
            self.strength = "MEDIA"
        else:
            self.strength = "BAJA"


@dataclass
class Signal:
    ticker: str
    date: datetime.date
    current_price: float
    key_level_low: float
    key_level_high: float
    rs_score: float
    entry: float
    target: float
    stop: float
    rr_ratio: float
    confidence: str
    conditions: dict = field(default_factory=dict)

    def print_report(self):
        rs_label = (
            "🔥 MUY FUERTE" if self.rs_score < 0.2 else
            "✅ FUERTE"     if self.rs_score < 0.4 else
            "⚠️  MODERADO"  if self.rs_score < 0.6 else
            "❌ DÉBIL"
        )
        gain_pct = (self.target - self.entry) / self.entry * 100
        risk_pct = (self.entry - self.stop)   / self.entry * 100

        print("═" * 47)
        print(f"📊 SEÑAL DETECTADA — {self.ticker}")
        print("═" * 47)
        print(f"📅 Fecha:          {self.date}")
        print(f"💹 Precio actual:  ${self.current_price:.2f}")
        print(f"🎯 Nivel clave:    ${self.key_level_low:.2f} - ${self.key_level_high:.2f}")
        print(f"⚡ RS Score:       {self.rs_score:.3f}  ({rs_label})")
        print()
        print("TRADE SETUP:")
        print(f"  🟢 Entrada:      ${self.entry:.2f}")
        print(f"  🎯 Objetivo:     ${self.target:.2f}  (+{gain_pct:.2f}%)")
        print(f"  🛑 Stop Loss:    ${self.stop:.2f}  (-{risk_pct:.2f}%)")
        print(f"  📐 Ratio R:R:    1:{self.rr_ratio:.2f}")
        print()
        print("CONDICIONES:")
        icons = {True: "✅", False: "❌"}
        print(f"  {icons[self.conditions.get('spx_reboting',False)]} SPX en rebote")
        print(f"  {icons[self.conditions.get('level_held',False)]} Nivel clave mantenido")
        print(f"  {icons[self.conditions.get('near_breakout',False)]} Cerca de ruptura (<2%)")
        print(f"  {icons[self.conditions.get('rs_positive',False)]} RS positiva confirmada")
        print()
        print(f"CONFIANZA: {self.confidence}")
        print("═" * 47)


# ─── Clientes Alpaca ─────────────────────────────────────────────────────────

data_client   = StockHistoricalDataClient(API_KEY, API_SECRET)
trade_client  = TradingClient(API_KEY, API_SECRET, paper=PAPER)


# ─── Obtención de datos ───────────────────────────────────────────────────────

def get_bars(ticker: str, days: int = 90) -> pd.DataFrame:
    """Descarga barras diarias de Alpaca."""
    end   = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days + 10)  # buffer para feriados

    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,  # IEX disponible en plan gratuito
    )
    bars = data_client.get_stock_bars(req).df

    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(ticker, level=0)

    bars = bars.sort_index()
    return bars.tail(days)


# ─── Lógica de análisis ───────────────────────────────────────────────────────

def find_key_levels(df: pd.DataFrame, lookback: int = 60, min_touches: int = 2,
                    tolerance_pct: float = 0.015) -> list[KeyLevel]:
    """Agrupa máximos y mínimos en clusters de precio (niveles clave)."""
    data = df.tail(lookback)
    prices = list(data["high"]) + list(data["low"])

    clusters: list[list[float]] = []
    for p in sorted(prices):
        placed = False
        for c in clusters:
            if abs(p - statistics.mean(c)) / statistics.mean(c) <= tolerance_pct:
                c.append(p)
                placed = True
                break
        if not placed:
            clusters.append([p])

    levels = []
    for c in clusters:
        if len(c) >= min_touches:
            center = statistics.mean(c)
            current_close = df["close"].iloc[-1]
            lvl_type = "resistance" if center > current_close else "support"
            levels.append(KeyLevel(
                price=center,
                touch_count=len(c),
                level_type=lvl_type,
            ))

    return sorted(levels, key=lambda x: abs(x.price - df["close"].iloc[-1]))


def calculate_rs_score(stock_df: pd.DataFrame, spx_df: pd.DataFrame,
                        lookback: int = 20) -> float:
    """
    RS Score = promedio(% cambio acción) / promedio(% cambio SPX)
    calculado solo en días que el SPX bajó.
    """
    stock = stock_df["close"].pct_change().tail(lookback)
    spx   = spx_df["close"].pct_change().tail(lookback)

    # Alinear por fecha
    common = stock.index.intersection(spx.index)
    stock, spx = stock.loc[common], spx.loc[common]

    down_days = spx[spx < 0].index
    if len(down_days) == 0:
        return 0.5  # sin datos suficientes

    stock_moves = stock.loc[down_days].mean()
    spx_moves   = spx.loc[down_days].mean()

    if spx_moves == 0:
        return 0.5

    return float(stock_moves / spx_moves)


def spx_is_reboting(spx_df: pd.DataFrame, lookback: int = 5) -> bool:
    """SPX rebota si el cierre de hoy está sobre el mínimo reciente y sube."""
    recent = spx_df.tail(lookback)
    last_close = recent["close"].iloc[-1]
    prev_close = recent["close"].iloc[-2]
    return bool(last_close > prev_close)


def stock_held_key_level(stock_df: pd.DataFrame, level_price: float,
                          lookback: int = 5, tolerance_pct: float = 0.01) -> bool:
    """La acción mantuvo el nivel si no cerró más de 1% por debajo en los últimos N días."""
    recent_closes = stock_df["close"].tail(lookback)
    floor = level_price * (1 - tolerance_pct)
    return bool((recent_closes >= floor).all())


def find_next_resistance(stock_df: pd.DataFrame, levels: list[KeyLevel],
                          entry: float) -> Optional[float]:
    """Busca la próxima resistencia por encima de la entrada."""
    resistances = [l for l in levels if l.price > entry and l.level_type == "resistance"]
    if not resistances:
        # Estimación: +3% si no hay nivel claro
        return entry * 1.03
    return min(resistances, key=lambda x: x.price).price


def score_confidence(rs_score: float, conditions: dict) -> str:
    score = 0
    if rs_score < 0.2:   score += 40
    elif rs_score < 0.4: score += 25
    elif rs_score < 0.5: score += 10

    if conditions.get("spx_reboting"):   score += 20
    if conditions.get("level_held"):     score += 25
    if conditions.get("near_breakout"):  score += 15

    if score >= 80: return "ALTA"
    if score >= 55: return "MEDIA"
    return "BAJA"


# ─── Scanner principal ────────────────────────────────────────────────────────

def scan_ticker(ticker: str, spx_df: pd.DataFrame, verbose: bool = False) -> Optional[Signal]:
    try:
        stock_df = get_bars(ticker, days=KEY_LEVEL_LOOKBACK + 10)
    except Exception as e:
        print(f"  [!] Error obteniendo datos de {ticker}: {e}")
        return None

    if len(stock_df) < 20:
        return None

    current_price = float(stock_df["close"].iloc[-1])

    # Niveles clave
    levels = find_key_levels(stock_df, lookback=KEY_LEVEL_LOOKBACK,
                              min_touches=MIN_TOUCHES)
    if not levels:
        return None

    # Nivel más cercano por encima (resistencia a romper)
    resistance_levels = [l for l in levels if l.price >= current_price * 0.98]
    if not resistance_levels:
        return None

    key = resistance_levels[0]

    # RS Score
    rs_score = calculate_rs_score(stock_df, spx_df, lookback=RS_LOOKBACK_DAYS)

    # Condiciones
    near_breakout = abs(current_price - key.price) / key.price <= NEAR_BREAKOUT_PCT
    conditions = {
        "spx_reboting":  spx_is_reboting(spx_df),
        "level_held":    stock_held_key_level(stock_df, key.price * 0.99),
        "near_breakout": near_breakout,
        "rs_positive":   rs_score < RS_THRESHOLD,
    }

    if verbose:
        print(f"\n    [{ticker}] precio=${current_price:.2f}  nivel=${key.price:.2f}  RS={rs_score:.3f}")
        for k, v in conditions.items():
            print(f"      {'✅' if v else '❌'} {k}")

    if not all(conditions.values()):
        return None

    # Niveles del trade
    entry  = key.price * (1 + ENTRY_BUFFER_PCT)
    stop   = key.price * (1 - STOP_BUFFER_PCT)

    support_levels = [l for l in levels if l.level_type == "support"]
    target = find_next_resistance(stock_df, levels, entry) or entry * 1.03

    risk   = entry - stop
    reward = target - entry
    if risk <= 0:
        return None

    rr_ratio = reward / risk
    if rr_ratio < MIN_RR_RATIO:
        return None

    confidence = score_confidence(rs_score, conditions)

    # Buscar rango del nivel (soporte cercano como low)
    near_support = [l for l in levels if l.price < current_price]
    level_low = near_support[0].price if near_support else key.price * 0.985

    return Signal(
        ticker=ticker,
        date=datetime.date.today(),
        current_price=current_price,
        key_level_low=level_low,
        key_level_high=key.price,
        rs_score=rs_score,
        entry=entry,
        target=target,
        stop=stop,
        rr_ratio=rr_ratio,
        confidence=confidence,
        conditions=conditions,
    )


def scan_watchlist(tickers: list[str], verbose: bool = False) -> list[Signal]:
    print(f"\n🔍 Escaneando {len(tickers)} acciones...\n")

    spx_df = get_bars("SPY", days=RS_LOOKBACK_DAYS + 10)
    signals = []

    for ticker in tickers:
        print(f"  → {ticker}...", end=" ", flush=True)
        sig = scan_ticker(ticker, spx_df, verbose=verbose)
        if sig:
            print(f"✅ SEÑAL (RS={sig.rs_score:.2f}, Conf={sig.confidence})")
            signals.append(sig)
        else:
            print("sin setup")
        time.sleep(0.3)  # rate limit amable

    # Ordenar por confianza luego por RS score
    order = {"ALTA": 0, "MEDIA": 1, "BAJA": 2}
    signals.sort(key=lambda x: (order.get(x.confidence, 3), x.rs_score))
    return signals


# ─── Ejecución de órdenes en Alpaca ──────────────────────────────────────────

def get_account_equity() -> float:
    account = trade_client.get_account()
    return float(account.equity)


def get_open_positions() -> dict:
    positions = trade_client.get_all_positions()
    return {p.symbol: p for p in positions}


def place_trade(signal: Signal, auto: bool = False) -> bool:
    """
    Coloca una orden límite de compra en Alpaca.
    Si auto=False, pide confirmación al usuario.
    """
    equity    = get_account_equity()
    risk_amt  = equity * MAX_RISK_PCT
    risk_per  = signal.entry - signal.stop
    if risk_per <= 0:
        print("  [!] Risk per share inválido, cancelando.")
        return False

    qty = max(1, int(risk_amt / risk_per))

    print(f"\n  💼 Capital: ${equity:,.2f}")
    print(f"  📦 Qty calculada: {qty} acciones (riesgo ${risk_amt:.0f})")

    if not auto:
        resp = input(f"\n  ¿Ejecutar orden LÍMITE {signal.ticker} @ ${signal.entry:.2f}? [s/N]: ")
        if resp.strip().lower() != "s":
            print("  ↩ Orden cancelada por usuario.")
            return False

    try:
        order = trade_client.submit_order(
            LimitOrderRequest(
                symbol=signal.ticker,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(signal.entry, 2),
            )
        )
        print(f"  ✅ Orden enviada: {order.id}")
        print(f"     {signal.ticker} ×{qty} @ ${signal.entry:.2f} (límite)")
        return True
    except Exception as e:
        print(f"  [!] Error al enviar orden: {e}")
        return False


# ─── Modo de ejecución ────────────────────────────────────────────────────────

def run_scanner(auto_trade: bool = False, verbose: bool = False):
    """
    Escanea la watchlist, muestra reportes y opcionalmente coloca órdenes.
    """
    print("\n" + "=" * 47)
    print("  🤖 RELATIVE STRENGTH SCANNER BOT")
    print("  Alpaca Paper Trading")
    print("=" * 47)

    # Estado de cuenta
    try:
        account = trade_client.get_account()
        print(f"\n  Cuenta: {account.account_number}")
        print(f"  Equity: ${float(account.equity):,.2f}")
        print(f"  Buying power: ${float(account.buying_power):,.2f}")
    except Exception as e:
        print(f"  [!] Error conectando con Alpaca: {e}")
        return

    # Posiciones abiertas
    open_pos = get_open_positions()
    if open_pos:
        print(f"\n  Posiciones abiertas: {list(open_pos.keys())}")

    if len(open_pos) >= MAX_POSITIONS:
        print(f"\n  ⚠️  Máximo de {MAX_POSITIONS} posiciones alcanzado. No se abren nuevas.")
        return

    # Escanear
    signals = scan_watchlist(WATCHLIST, verbose=verbose)

    if not signals:
        print("\n❌ No se detectaron setups válidos hoy.")
        return

    print(f"\n\n{'=' * 47}")
    print(f"  📋 {len(signals)} SEÑAL(ES) DETECTADA(S)")
    print(f"{'=' * 47}\n")

    for sig in signals:
        sig.print_report()
        print()

        # No operar si ya hay posición
        if sig.ticker in open_pos:
            print(f"  ↩ Ya existe posición en {sig.ticker}.\n")
            continue

        place_trade(sig, auto=auto_trade)

    print("\n✅ Escaneo completado.")


def run_continuous(interval_minutes: int = 60, auto_trade: bool = False):
    """Corre el scanner en loop cada N minutos (para correr en horario de mercado)."""
    print(f"\n🔄 Modo continuo: escaneando cada {interval_minutes} minutos.")
    print("   Presiona Ctrl+C para detener.\n")

    while True:
        now = datetime.datetime.now()
        # Solo ejecutar en horario de mercado (9:30 AM – 4:00 PM ET, lunes–viernes)
        market_open  = now.replace(hour=9,  minute=30, second=0)
        market_close = now.replace(hour=16, minute=0,  second=0)

        if now.weekday() < 5 and market_open <= now <= market_close:
            run_scanner(auto_trade=auto_trade)
        else:
            print(f"  [{now.strftime('%H:%M')}] Mercado cerrado — esperando...")

        time.sleep(interval_minutes * 60)


# ─── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Argumentos simples:
    #   python rs_scanner_bot.py           → escaneo único, pide confirmación
    #   python rs_scanner_bot.py auto      → escaneo único, ejecuta automáticamente
    #   python rs_scanner_bot.py loop      → loop continuo, pide confirmación
    #   python rs_scanner_bot.py loop auto → loop continuo, ejecuta automáticamente

    args    = sys.argv[1:]
    auto    = "auto"    in args
    loop    = "loop"    in args
    verbose = "debug"   in args

    if loop:
        run_continuous(interval_minutes=60, auto_trade=auto)
    else:
        run_scanner(auto_trade=auto, verbose=verbose)
