"""Cartera de papel de Katerina. Simulación pura: no toca Binance ni usa claves.

Parte de los saldos reales de Yeray (capturas de Binance) y los divide en dos libros:
  - Libro semanal: posiciones de fondo. Cada semana aplica un filtro de tendencia (cierre diario vs media de 20 días).
  - Libro diario: operaciones de corto plazo (24 h) guiadas por la señal de Kronos, con stop-loss.
Todo con comisión simulada. Se compara siempre con "mantener tal cual" (la cartera original sin tocar).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "paper_wallet.json"
TRADES = ROOT / "data" / "paper_trades.jsonl"
PANEL = ROOT / "docs" / "data" / "wallet.json"
RISK = ["BTC", "BNB", "ETH", "SOL", "ADA"]


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "kronos-desk/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get_prices() -> dict:
    syms = json.dumps([s + "USDT" for s in RISK], separators=(",", ":"))
    d = _get("https://data-api.binance.vision/api/v3/ticker/price?symbols=" + urllib.parse.quote(syms))
    px = {x["symbol"][:-4]: float(x["price"]) for x in d}
    px["USDC"] = 1.0
    return px


def get_trend(sym: str, days: int) -> dict:
    rows = _get("https://data-api.binance.vision/api/v3/klines?symbol=%sUSDT&interval=1d&limit=%d" % (sym, days + 20))
    now_ms = int(time.time() * 1000)
    closes = [float(r[4]) for r in rows if r[6] < now_ms]            # solo velas diarias cerradas
    sma = mean(closes[-days:])
    return {"close": closes[-1], "sma": sma, "on": closes[-1] > sma}


def value(book: dict, px: dict) -> float:
    return sum(q * px.get(k, 0.0) for k, q in book.items())


class Ctx:
    def __init__(self, cfg: dict, px: dict, now: datetime):
        self.fee = float(cfg["fee"])
        self.min_usd = float(cfg["min_trade_usd"])
        self.px, self.now, self.ops = px, now, []

    def log(self, book, side, sym, usd, why):
        self.ops.append({"t": self.now.strftime("%Y-%m-%dT%H:%MZ"), "libro": book, "op": side, "activo": sym,
                         "usd": round(usd, 2), "precio": round(self.px[sym], 6), "motivo": why})


def buy(c: Ctx, book: dict, name: str, sym: str, usd: float, why: str) -> None:
    usd = min(usd, book.get("USDC", 0.0))
    if usd < c.min_usd:
        return
    book["USDC"] -= usd
    book[sym] = book.get(sym, 0.0) + usd * (1 - c.fee) / c.px[sym]
    c.log(name, "compra", sym, usd, why)


def sell(c: Ctx, book: dict, name: str, sym: str, why: str, qty: float | None = None) -> None:
    q = book.get(sym, 0.0) if qty is None else min(qty, book.get(sym, 0.0))
    usd = q * c.px[sym]
    if usd < c.min_usd:
        return
    book[sym] -= q
    book["USDC"] = book.get("USDC", 0.0) + usd * (1 - c.fee)
    c.log(name, "venta", sym, usd, why)


def init_state(cfg: dict, px: dict, now: datetime) -> dict:
    hold = {k: float(v) for k, v in cfg["holdings"].items()}
    dust = float(cfg["dust_usd"])
    v_assets = value(hold, px)
    weekly, daily = dict(hold), {"USDC": 0.0}
    need = float(cfg["daily_book_pct"]) * v_assets                  # efectivo que debe tener el libro diario
    take = min(weekly.get("USDC", 0.0), need)                       # primero se usa el efectivo que ya tienes
    weekly["USDC"] -= take
    daily["USDC"] += take
    need -= take
    c = Ctx(cfg, px, now)
    if need > 0.01:                                                 # el resto se vende a prorrata de lo que tienes
        usdc0 = weekly["USDC"]
        risky = {k: q * px[k] for k, q in weekly.items() if k != "USDC" and q * px[k] >= 1.0}
        tot = sum(risky.values())
        for k, v in risky.items():
            sell(c, weekly, "inicio", k, "dotar el libro diario", qty=need / (1 - c.fee) * v / tot / px[k])
        proceeds = weekly["USDC"] - usdc0
        weekly["USDC"] = usdc0
        daily["USDC"] += proceeds
    return {"start_time": now.strftime("%Y-%m-%dT%H:%MZ"), "start_value": v_assets + dust, "dust": dust,
            "hold": hold, "weekly": weekly, "daily": daily, "entries": {}, "last_week": "", "last_report": "",
            "history": [], "init_ops": c.ops}


def weekly_rebalance(c: Ctx, cfg: dict, st: dict) -> list:
    book = st["weekly"]
    info = {s: get_trend(s, int(cfg["trend_days"])) for s in RISK}
    on = [s for s in RISK if info[s]["on"]]
    w = min(float(cfg["max_weight"]), 1.0 / len(on)) if on else 0.0
    V = value(book, c.px)
    thr = max(c.min_usd, 0.03 * V)
    tgt = {s: (w * V if s in on else 0.0) for s in RISK}
    for s in RISK:
        cur = book.get(s, 0.0) * c.px[s]
        if cur - tgt[s] > thr:
            sell(c, book, "semanal", s, "tendencia bajista" if s not in on else "reajuste de peso",
                 qty=(cur - tgt[s]) / c.px[s])
    for s in on:
        cur = book.get(s, 0.0) * c.px[s]
        if tgt[s] - cur > thr:
            buy(c, book, "semanal", s, tgt[s] - cur, "tendencia alcista")
    return [s for s in RISK if info[s]["on"]]


def daily_trade(c: Ctx, cfg: dict, st: dict, forecasts: dict) -> None:
    book, entries = st["daily"], st["entries"]
    cand = []
    for s in RISK:
        f = forecasts.get(s)
        if not f:
            continue
        chg = (f["pred_close"] / f["base_close"] - 1) * 100
        width = (f["hi"][-1] - f["lo"][-1]) / f["base_close"] * 100
        if f["p_up"] >= float(cfg["signal_min_pup"]) and chg >= float(cfg["signal_min_move_pct"]) and width < 3.0:
            cand.append((chg, s, f))
    cand.sort(reverse=True)
    keep = {s: (chg, f) for chg, s, f in cand[: int(cfg["daily_max_positions"])]}
    for s in list(entries):
        if s not in keep:
            sell(c, book, "diario", s, "la señal ya no se cumple")
            entries.pop(s, None)
    V = value(book, c.px)
    for s, (chg, f) in keep.items():
        if s in entries:
            continue
        n = int(round(f["p_up"] * f["samples"]))
        buy(c, book, "diario", s, V / int(cfg["daily_max_positions"]),
            "señal Kronos %+.1f%%, %d/%d trayectorias" % (chg, n, f["samples"]))
        if book.get(s, 0.0) * c.px[s] >= c.min_usd:
            entries[s] = c.px[s]


def stops(c: Ctx, cfg: dict, st: dict) -> None:
    for s, e in list(st["entries"].items()):
        if c.px[s] <= e * (1 - float(cfg["stop_pct"]) / 100):
            sell(c, st["daily"], "diario", s, "stop-loss -%s%%" % cfg["stop_pct"])
            st["entries"].pop(s, None)


def totals(st: dict, px: dict) -> dict:
    w, d = value(st["weekly"], px), value(st["daily"], px)
    h = value(st["hold"], px) + st["dust"]
    return {"total": w + d + st["dust"], "weekly": w, "daily": d, "hold": h}


def describe(book: dict, px: dict) -> str:
    items = sorted(((k, q * px[k]) for k, q in book.items() if q * px.get(k, 0) >= 1.0), key=lambda x: -x[1])
    return ", ".join("%s %.0f$" % (k, v) for k, v in items) or "todo en efectivo"


def report(st: dict, px: dict, ops: list, tr: dict, now: datetime, prev: dict | None, fee: float) -> str:
    base = prev["total"] if prev else st["start_value"]
    lines = ["Katerina · Cartera de papel · %s" % now.strftime("%d/%m/%Y")]
    lines.append("Valor: %.2f $ (%+.2f%% vs. %s)" % (
        tr["total"], (tr["total"] / base - 1) * 100, "ayer" if prev else "inicio"))
    lines.append("Desde el inicio: %+.2f%% · Mantener sin tocar: %+.2f%% · Diferencia: %+.2f pp" % (
        (tr["total"] / st["start_value"] - 1) * 100, (tr["hold"] / st["start_value"] - 1) * 100,
        (tr["total"] - tr["hold"]) / st["start_value"] * 100))
    lines.append("Libro semanal %.0f $ · Libro diario %.0f $" % (tr["weekly"], tr["daily"]))
    if ops:
        lines.append("Operaciones:")
        for o in ops:
            lines.append(" - %s: %s %s %.0f $ (%s)" % (o["libro"], o["op"], o["activo"], o["usd"], o["motivo"]))
    else:
        lines.append("Operaciones: ninguna hoy.")
    lines.append("Semanal: " + describe(st["weekly"], px))
    lines.append("Diario: " + describe(st["daily"], px))
    lines.append("Simulación con comisión del %.1f%%, sin dinero real. Lectura de estudio; la decisión es tuya." % (
        fee * 100))
    return "\n".join(lines)


def step(cfg: dict, forecasts: dict, now: datetime, force: bool = False):
    """Una pasada horaria. Devuelve el texto del informe si toca enviarlo, o None."""
    P = cfg["paper"]
    try:
        px = get_prices()
    except Exception as e:                                          # noqa: BLE001
        print("Cartera de papel: sin precios (%s), se omite esta pasada" % e.__class__.__name__)
        return None
    first = not STATE.exists()
    st = json.loads(STATE.read_text(encoding="utf-8")) if not first else init_state(P, px, now)
    c = Ctx(P, px, now)
    c.ops += st.pop("init_ops", []) if first else []
    stops(c, P, st)
    wk = "%d-W%02d" % now.isocalendar()[:2]
    if st["last_week"] != wk:
        try:
            weekly_rebalance(c, P, st)
            st["last_week"] = wk
        except Exception as e:                                      # noqa: BLE001
            print("Cartera de papel: sin tendencia diaria (%s), se reintenta" % e.__class__.__name__)
    today = now.strftime("%Y-%m-%d")
    out = None
    if force or (now.hour >= int(P["report_hour_utc"]) and st["last_report"] != today):
        daily_trade(c, P, st, forecasts)
        tr = totals(st, px)
        prev = st["history"][-1] if st["history"] else None
        out = report(st, px, c.ops, tr, now, prev, c.fee)
        st["history"].append({"d": today, "total": round(tr["total"], 2), "hold": round(tr["hold"], 2),
                              "weekly": round(tr["weekly"], 2), "daily": round(tr["daily"], 2)})
        st["last_report"] = today
    if c.ops:
        with TRADES.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in c.ops))
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    tr = totals(st, px)
    last = [json.loads(x) for x in TRADES.read_text(encoding="utf-8").splitlines()[-15:]] if TRADES.exists() else []
    PANEL.write_text(json.dumps({
        "actualizado": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "inicio": st["start_time"], "valor_inicial": st["start_value"],
        "valor": tr["total"], "mantener": tr["hold"], "semanal": tr["weekly"], "diario": tr["daily"],
        "pos_semanal": describe(st["weekly"], px), "pos_diario": describe(st["daily"], px),
        "historial": st["history"][-120:], "operaciones": last, "comision_pct": 0.1}), encoding="utf-8")
    return out
