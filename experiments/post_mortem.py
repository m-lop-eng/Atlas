"""Post-mortem cuantitativo (E4): análisis no-optimizador de un report.json.

Responde las 4 preguntas del ciclo de cierre de la hipótesis:

    A. ¿LONG y SHORT se comportan igual?        (tabla por lado)
    B. ¿Entrada o salida?                       (MAE/MFE + razones de salida)
    C. ¿Dónde se concentran las pérdidas?       (duración × exit_reason)
    D. ¿Los costes destruyen una señal bruta?   (gross / costs / net)

Reglas de interpretación (heurísticas, no conclusiones):
    * MAE alto + MFE bajo           → entrada probablemente desfavorable.
    * MAE moderado + MFE alto       → oportunidad condicionada a salida.
    * MFE bajo + stop frecuente     → la ruptura no consigue continuación.
    * MFE alto + TIME frecuente     → investigar gestión de salida.

Uso:
    python experiments/post_mortem.py <report.json> [--extra extra.json ...]

Ejecución conjunta (baseline E1 + real + nulos):
    python experiments/post_mortem.py <E1 report.json> <real report.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = Path(__file__).resolve().parent / "outputs"


def _load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ask_a(side: dict) -> None:
    print("A. LONG vs SHORT")
    print(f"    {'':<7}{'señales':>8}{'trades':>7}{'W/L':>8}{'avg':>10}{'median':>10}"
          f"{'MAE':>9}{'MFE':>9}{'stop%':>7}")
    for name in ("LONG", "SHORT"):
        s = side[name]
        wl = f"{s['wins']}/{s['losses']}"
        print(f"    {name:<7}{s['signals']:>8}{s['trades']:>7}{wl:>8}"
              f"{_fmt(s['avg_pnl']):>10}{_fmt(s['median_pnl']):>10}"
              f"{_fmt(s['avg_mae']):>9}{_fmt(s['avg_mfe']):>9}{_pct(s['stop_rate']):>7}")


def _fmt(x) -> str:
    return f"{x:.0f}" if isinstance(x, (int, float)) else "—"


def _pct(x) -> str:
    return f"{x * 100:.0f}" if isinstance(x, (int, float)) else "—"


def _ask_b(trades: list[dict], dist: dict) -> None:
    n = len(trades)
    if not n:
        print("B. Entrada/salida: sin operaciones (no aplica)")
        return
    mae = [float(t["mae"]) for t in trades]
    mfe = [float(t["mfe"]) for t in trades]
    avg_mae = sum(mae) / n
    avg_mfe = sum(mfe) / n
    stops = sum(1 for t in trades if t.get("exit_reason") == "STOP")
    times = sum(1 for t in trades if t.get("exit_reason") == "TIME")
    stop_rate = stops / n
    time_rate = times / n
    ratio = (avg_mfe / avg_mae) if avg_mae else None
    print("B. Entrada o salida (MAE/MFE en $, promedio por trade)")
    print(f"    MAE avg={_fmt(avg_mae)}  MFE avg={_fmt(avg_mfe)}  "
          f"ratio MFE/MAE={_fmt(ratio) if ratio is not None else '—'}")
    print(f"    stop={_pct(stop_rate)}  TIME={_pct(time_rate)}")
    flags = []
    if avg_mae and avg_mfe and avg_mae > 0:
        if avg_mae >= 2 * avg_mfe:
            flags.append("MAE alto + MFE bajo → entrada probablemente desfavorable")
        elif avg_mfe >= 2 * avg_mae:
            flags.append("MAE moderado + MFE alto → oportunidad condicionada a salida")
    if avg_mfe < (avg_mae or 1) * 1 and stop_rate >= 0.6:
        flags.append("MFE bajo + STOP frecuente → la ruptura no consigue continuación")
    if avg_mfe >= (avg_mae or 1) * 2 and time_rate >= 0.3:
        flags.append("MFE alto + TIME frecuente → investigar gestión de salida")
    for f in flags:
        print(f"    * {f}")


def _ask_c(dist: dict) -> None:
    print("C. ¿Dónde se concentran las pérdidas? (duración × exit_reason, net $)")
    buckets = dist["duration_by_exit"]
    print(f"    {'':<6}", end="")
    reasons = sorted({r for cell in buckets.values() for r in cell})
    for r in reasons:
        print(f"{r:>14}", end="")
    print()
    worst = None
    for bucket in ("0-5", "6-10", "11-20", "21-40", "40+"):
        cell = buckets.get(bucket, {})
        print(f"    {bucket:<6}", end="")
        for r in reasons:
            data = cell.get(r, {"count": 0, "net_pnl": 0.0})
            print(f"{data['net_pnl']:>14.0f}", end="")
            if data["count"] and (worst is None or data["net_pnl"] < worst[2]):
                worst = (bucket, r, data["net_pnl"], data["count"])
        print()
    if worst:
        print(f"    pérdida concentrada: {worst[0]} × {worst[1]} "
              f"({worst[3]} trades, net {worst[2]:.0f})")


def _ask_d(perf: dict) -> None:
    gross, costs, net = perf["gross_pnl"], perf["costs"], perf["net_pnl"]
    print("D. Costes vs señal bruta")
    print(f"    gross={gross:,.0f}  costs={costs:,.0f}  net={net:,.0f}")
    if gross > 0 and net < 0:
        print(f"    * señal POSITIVA en bruto destruida por costes "
              f"(costes = {costs:,.0f}, |costes/gross| = {abs(costs / gross):.2f})")
    elif gross <= 0:
        print("    * la señal bruta ya es negativa: el problema no es el coste")
    else:
        print("    * net positiva: los costes no destruyen la señal")


def post_mortem(report: dict) -> dict:
    line = report["lineage"]
    print(f"\n=== {line['experiment_type']} — {line['hypothesis']} — "
          f"{report['experiment_id']} ===")
    sections = report.get("sides")
    if sections:
        _ask_a(sections)
    else:
        print("(reporte sin secciones; regenéralo con el pipeline actual)")
    _ask_b(report["trades"], report["trade_distribution"])
    _ask_c(report["trade_distribution"])
    _ask_d(report["performance"])
    return {
        "experiment_id": report["experiment_id"],
        "experiment_type": line.get("experiment_type"),
        "hypothesis": line.get("hypothesis"),
        "net_return": report["performance"]["return"],
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    summary = []
    for path in argv:
        report = _load(path)
        summary.append(post_mortem(report))
    out = OUT / "post_mortem.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\npost-mortem guardado en: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())