"""Benchmarks de exposición pasiva para el contraste de hipótesis (H003).

Métodos implementados (todos deterministas, sin parametrización adaptable):

    * `buy_and_hold`: comprar el activo al primer open del período y mantener
      hasta el último close. Exposición = 100% del tiempo, 1 operación
      (entrada + salida), misma estructura de costes y point_value que el
      pipeline. Benchmark "beta direccional pura".
    * `exposure_matched_long`: control neutro a señales con la MISMA fracción
      temporal de exposición long que el tratamiento (p. ej. ~37%). Las
      entradas/salidas se deciden con un ciclo determinista por índice de
      barra (NO usan el precio ni las señales del breakout): "cualquier
      exposición long intermitente". Contesta H003-E.
    * `buy_and_hold_exposure_scaled`: Buy & Hold con la misma EXPOSICIÓN DE
      capital (fracción de patrimonio en riesgo promedio del tratamiento),
      para distinguir "gana porque está long" de "gana porque sabe cuándo".
      Contesta H003-C.
    * `classify_regimes`: clasificación de régimen exógena y pre-registrada
      (H003-D) basada únicamente en el precio: BULL/BEAR/LATERAL respecto a
      una SMA de largo plazo y una banda (±band). Es una VARIABLE DE ANÁLISIS,
      nunca un parámetro de la estrategia: la estrategia no cambia.
    * Métricas anualizadas: CAGR, volatilidad, Sharpe, Sortino, Calmar.
      Frecuencia = retorno simple por barra (1h en el dataset actual);
      anualización por sqrt(bars_por_año). SIEMPRE se documenta la frecuencia.

Convención: las métricas son proporcionales (%, ratios), por lo que la
comparación entre tratamiento y benchmarks no depende de la cantidad operada.
"""

from __future__ import annotations

from statistics import mean, pstdev

BAR = dict

_REGIME_SMA_PERIOD = 720  # ~30 días en barras 1h → horizonte largo
_REGIME_BAND = 0.05  # ±5% alrededor de la SMA lenta


def bars_per_year(interval_seconds: int) -> float:
    """Barras por año para un intervalo dado (365.25 días astronómicos)."""
    return 365.25 * 24 * 3600.0 / float(interval_seconds)


def _simple_returns(curve: list[float]) -> list[float]:
    if len(curve) < 2:
        return []
    return [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve))]


def _downside_deviation(returns: list[float]) -> float:
    downsides = [r for r in returns if r < 0.0]
    if not downsides:
        return 0.0
    return pstdev(downsides)


def max_drawdown(curve: list[float]) -> float:
    """Pico-máximo a valle: máximo retroceso porcentual de la curva."""
    peak = curve[0]
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return max_dd


def curve_metrics(
    curve: list[float],
    n_bars: int,
    interval_seconds: int,
) -> dict[str, float | None]:
    """Métricas normalizadas de una curva de equity (benchmarks y tratamiento).

    Frecuencia y anualización: retornos simples POR BARRA (1h en el dataset
    actual). Sharpe/Sortino/Volatilidad anualizadas con sqrt(bars_por_año).
    `net_return` es la curva [noveno] → [final] / [noveno] - 1.
    """
    bpy = bars_per_year(interval_seconds)
    returns = _simple_returns(curve)
    total_return = curve[-1] / curve[0] - 1.0
    n_years = n_bars / bpy
    cagr = (curve[-1] / curve[0]) ** (1.0 / n_years) - 1.0 if n_years > 0 else None
    dd = max_drawdown(curve)
    vol = pstdev(returns) * (bpy ** 0.5) if len(returns) > 1 else None
    std = pstdev(returns) if len(returns) > 1 else None
    sharpe = (mean(returns) / std) * (bpy ** 0.5) if std else None
    dd_std = _downside_deviation(returns)
    sortino = (mean(returns) / dd_std) * (bpy ** 0.5) if dd_std else None
    calmar = cagr / dd if dd > 0 else None
    return_over_dd = total_return / dd if dd > 0 else None
    return {
        "net_return": total_return,
        "cagr": cagr,
        "max_drawdown": dd,
        "return_over_max_dd": return_over_dd,
        "annual_volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "annualization": f"sqrt({bpy:.1f}) per-bar (1h)",
    }


def buy_and_hold(
    bars: list[BAR],
    *,
    initial_equity: float,
    per_side: float,
    point_value: float = 1.0,
) -> dict:
    """Buy & Hold puro (H003-B): comprar al primer open, mantener, vender.

    Exposición temporal = 100%. Tamaño = patrimonio completo invertido en el
    activo al open inicial. Costes: 2×per_side por unidad (entrada y salida).
    """
    if not bars:
        raise ValueError("buy_and_hold necesita al menos 1 barra")
    entry_open = float(bars[0]["open"])
    exit_close = float(bars[-1]["close"])
    quantity = initial_equity / entry_open
    costs = quantity * 2 * per_side * point_value
    curve = [initial_equity]
    for bar in bars:
        value = initial_equity + (float(bar["close"]) - entry_open) * quantity * point_value
        curve.append(value)
    curve[-1] -= costs
    metrics = curve_metrics(curve, len(bars), _interval(bars))
    return {
        "label": "H003-B Buy & Hold",
        "curve": curve,
        "metrics": metrics,
        "exposure_frac": 1.0,
        "num_trades": 1,
        "costs": costs,
        "gross_pnl": (exit_close - entry_open) * quantity * point_value,
        "net_pnl": metrics["net_return"] * initial_equity,
        "entry_open": entry_open,
        "exit_close": exit_close,
        "quantity": quantity,
    }


def exposure_matched_long(
    bars: list[BAR],
    *,
    target_exposure: float,
    initial_equity: float,
    per_side: float,
    point_value: float = 1.0,
    risk_per_trade: float = 0.01,
    atr_period: int = 14,
    cycle_bars: int = 48,
    offset: int = 0,
) -> dict:
    """Control neutro a señales con la misma fracción temporal long (H003-E).

    Entra LONG en bloques de `on_bars` barras dentro de cada ciclo de
    `cycle_bars` (duty cycle determinista por índice de barra + offset), de
    modo que la decisión de "estar long" es un reloj predecible que NO usa
    precio ni señales del breakout. Total de barras long ≈ target_exposure × N.

    Entrada al open de la primera barra del bloque, salida al close de la
    última barra del bloque. Sizing idéntico al tratamiento (fixed-fractional
    sobre el riesgo): cantidad = equity × risk_per_trade / (2 × ATR × pv).

    Returns:
        dict con curva, métricas, exposición efectiva, nº bloques y costes.
    """
    if not bars:
        raise ValueError("exposure_matched_long necesita al menos 1 barra")
    if not 0.0 < target_exposure <= 1.0:
        raise ValueError("target_exposure debe estar en (0, 1]")
    if cycle_bars < 1:
        raise ValueError("cycle_bars debe ser >= 1")
    on_bars = max(1, min(cycle_bars, round(cycle_bars * target_exposure)))

    atrs = _rolling_atr(bars, atr_period)
    curve: list[float] = [initial_equity]
    realized = 0.0
    unreal = 0.0
    entry_raw: float | None = None
    quantity = 0.0
    costs_total = 0.0
    exp_count = 0
    block_trades = 0
    block_pnls: list[float] = []
    n = len(bars)

    for i, bar in enumerate(bars):
        phase = (i - offset) % cycle_bars
        close = float(bar["close"])

        if phase == 0 and entry_raw is None:
            stop_dist = 2.0 * atrs[i]
            if stop_dist > 0:
                quantity = initial_equity * risk_per_trade / (stop_dist * point_value)
                entry_raw = float(bar["open"])
                c = quantity * per_side * point_value
                realized -= c
                costs_total += c
                block_trades += 1

        if entry_raw is not None:
            exp_count += 1
            if phase == on_bars - 1:
                block_pnls.append((close - entry_raw) * quantity * point_value - 2.0 * quantity * per_side * point_value)
                realized += (close - entry_raw) * quantity * point_value
                c = quantity * per_side * point_value
                realized -= c
                costs_total += c
                entry_raw = None
                quantity = 0.0

        if entry_raw is not None:
            unreal = (close - entry_raw) * quantity * point_value
        else:
            unreal = 0.0
        curve.append(initial_equity + realized + unreal)

    if entry_raw is not None:
        last_close = float(bars[-1]["close"])
        block_pnls.append((last_close - entry_raw) * quantity * point_value - 2.0 * quantity * per_side * point_value)
        realized += (last_close - entry_raw) * quantity * point_value
        c = quantity * per_side * point_value
        realized -= c
        costs_total += c
    curve[-1] = initial_equity + realized

    metrics = curve_metrics(curve, n, _interval(bars))
    return {
        "label": "H003-E Exposure-matched (neutro)",
        "curve": curve,
        "metrics": metrics,
        "exposure_frac": exp_count / n,
        "num_trades": block_trades,
        "costs": costs_total,
        "net_pnl": metrics["net_return"] * initial_equity,
        "block_pnls": block_pnls,
        "cycle_bars": cycle_bars,
        "on_bars": on_bars,
        "offset": offset,
    }


def exposure_scaled_buy_and_hold(
    bars: list[BAR],
    *,
    exposure_frac: float,
    initial_equity: float,
    per_side: float,
    point_value: float = 1.0,
) -> dict:
    """Buy & Hold escalado a la exposición de capital del tratamiento (H003-C).

    El tratamiento está en el mercado una fracción `exposure_frac` del tiempo
    con su capital en riesgo. Para no comparar un B&H 100% expuesto contra un
    sistema intermitente, escalamos el notional B&H por `exposure_frac`: es
    "comprar y mantener" con la misma exposición de capital promedio, pero sin
    timing. Diferencia respecto a exposure_matched_long: aquí el NOTIONAL está
    desplegado de forma continua (100% del tiempo) pero más pequeño.
    """
    if not bars:
        raise ValueError("exposure_scaled_buy_and_hold necesita al menos 1 barra")
    if not 0.0 < exposure_frac <= 1.0:
        raise ValueError("exposure_frac debe estar en (0, 1]")
    entry_open = float(bars[0]["open"])
    exit_close = float(bars[-1]["close"])
    quantity = initial_equity * exposure_frac / entry_open
    costs = quantity * 2 * per_side * point_value
    curve = [initial_equity]
    for bar in bars:
        value = initial_equity + (float(bar["close"]) - entry_open) * quantity * point_value
        curve.append(value)
    curve[-1] -= costs
    metrics = curve_metrics(curve, len(bars), _interval(bars))
    return {
        "label": f"H003-C B&H scale×{exposure_frac:.2f}",
        "curve": curve,
        "metrics": metrics,
        "exposure_frac": 1.0,
        "num_trades": 1,
        "costs": costs,
        "gross_pnl": (exit_close - entry_open) * quantity * point_value,
        "net_pnl": metrics["net_return"] * initial_equity,
        "entry_open": entry_open,
        "exit_close": exit_close,
        "quantity": quantity,
        "capital_exposure_frac": exposure_frac,
    }


def classify_regimes(
    bars: list[BAR],
    *,
    sma_period: int = _REGIME_SMA_PERIOD,
    band: float = _REGIME_BAND,
) -> list[str]:
    """Clasificación exógena y reproducible de régimen (H003-D).

    Pre-registrada (no calibrada con resultados del breakout):
        * trend = SMA de cierres de `sma_period` barras (long plazo).
        * r = close / trend - 1.
        * BULL  si r > band; BEAR si r < -band; LATERAL en otro caso.

    Valor por barra; las primeras `sma_period-1` barras (sin SMA completa) se
    marcan como LATERAL (banda amplia por definición) o None si wrinkle.
    Regime es variable de análisis, nunca parámetro de la estrategia.
    """
    closes = [float(b["close"]) for b in bars]
    n = len(closes)
    regimes: list[str] = []
    for i in range(n):
        if i + 1 < sma_period:
            trend = sum(closes[: i + 1]) / (i + 1)
        else:
            trend = sum(closes[i - sma_period + 1 : i + 1]) / sma_period
        if trend <= 0:
            regimes.append("LATERAL")
            continue
        r = closes[i] / trend - 1.0
        if r > band:
            regimes.append("BULL")
        elif r < -band:
            regimes.append("BEAR")
        else:
            regimes.append("LATERAL")
    return regimes


def regime_block_returns(bars: list[BAR], regimes: list[str]) -> dict[str, dict]:
    """Retorno por bloque contiguo de régimen (H003-D, benchmark por régimen).

    Para cada régimen agrupa bloques contiguos (misma etiqueta) y calcula el
    retorno del precio de cierre del bloque completado. Devuelve, por régimen:
    número de bloques, barras totales, retornos compuestos y suma.
    """
    blocks: dict[str, list[float]] = {}
    block_bars: dict[str, int] = {}
    cur_regime: str | None = None
    first_close: float | None = None
    last_close: float | None = None
    count = 0

    for i, bar in enumerate(bars):
        label = regimes[i]
        close = float(bar["close"])
        if label != cur_regime:
            if cur_regime is not None and first_close is not None and last_close is not None and first_close > 0:
                r = last_close / first_close - 1.0
                blocks.setdefault(cur_regime, []).append(r)
                block_bars[cur_regime] = block_bars.get(cur_regime, 0) + count
            cur_regime = label
            first_close = close
            last_close = close
            count = 1
        else:
            last_close = close
            count += 1

    if cur_regime is not None and first_close is not None and last_close is not None and first_close > 0:
        r = last_close / first_close - 1.0
        blocks.setdefault(cur_regime, []).append(r)
        block_bars[cur_regime] = block_bars.get(cur_regime, 0) + count

    result: dict[str, dict] = {}
    for label in ("BULL", "BEAR", "LATERAL"):
        rs = blocks.get(label, [])
        compounded = 1.0
        for r in rs:
            compounded *= 1.0 + r
        result[label] = {
            "blocks": len(rs),
            "bars": block_bars.get(label, 0),
            "returns": [round(r, 6) for r in rs],
            "compounded_return": compounded - 1.0,
            "avg_block_return": round(mean(rs), 6) if rs else None,
        }
    return result


def exposure_fraction_from_trades(trades: list[dict], n_bars: int) -> float:
    """Fracción temporal de exposición long/shrot de un libro de trades.

    Sigue la convención del pipeline: la barra de cierre cuenta como expuesta
    (max(0, exit_index - entry_index + 1)).
    """
    if not trades or n_bars <= 0:
        return 0.0
    in_market = 0
    for t in trades:
        entry = int(t.get("entry_index") or 0)
        exit_idx = int(t.get("exit_index") or entry)
        in_market += max(0, exit_idx - entry + 1)
    return in_market / n_bars


def capital_exposure_from_trades(
    trades: list[dict],
    equity_curve: list[float],
    closes: list[float],
) -> float:
    """Exposición de capital promedio MIENTRAS en posición (fracción del equity).

    Para cada barra en posición se estima quántum × precio / equity (inyectando
    el precio sobre la curva de equity tomada al cierre de esa barra). Ignora,
    por diseño, las inyecciones de capital del pipeline (usa la equity final).
    Se usa para escalar comparables (H003-C) y para diagnóstico por ventana.
    """
    deployed: list[float] = []
    for t in trades:
        qty = float(t.get("quantity") or 0.0)
        entry = int(t.get("entry_index") or 0)
        exit_idx = int(t.get("exit_index") or entry)
        for i in range(entry, min(exit_idx + 1, len(closes))):
            equity = equity_curve[min(i + 1, len(equity_curve) - 1)] if equity_curve else 0.0
            if equity > 0 and closes[i] > 0:
                deployed.append(qty * closes[i] / equity)
    return mean(deployed) if deployed else 0.0


def _interval(bars: list[BAR]) -> int:
    ts0 = float(bars[0]["ts"])
    ts1 = float(bars[1]["ts"]) if len(bars) > 1 else ts0
    return int(round(ts1 - ts0)) or 3600


def _rolling_atr(bars: list[BAR], period: int) -> list[float]:
    """ATR simple (media de True Range) por barra para sizing de benchmark."""
    out: list[float] = []
    window: list[float] = []
    for i, bar in enumerate(bars):
        high = float(bar["high"])
        low = float(bar["low"])
        if i == 0:
            tr = high - low
        else:
            prev_close = float(bars[i - 1]["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        window.append(tr)
        if len(window) > period:
            window.pop(0)
        out.append(sum(window) / len(window))
    return out