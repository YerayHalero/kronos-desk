#!/usr/bin/env python3
"""Kronos Desk: velas -> predicción -> seguimiento en sombra -> alertas.

Se ejecuta una vez por hora (GitHub Actions). Sin dinero real y sin órdenes automáticas.
Pasos: 1) descargar velas cerradas  2) corregir predicciones antiguas con lo que pasó
3) predecir  4) calcular marcador  5) alertas  6) escribir ficheros del panel.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.environ.get("KRONOS_SRC", str(ROOT / "kronos-src")))
sys.path.insert(0, str(ROOT / "src"))
import paper  # noqa: E402  cartera de papel de Katerina

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA = ROOT / "data"
PANEL = ROOT / "docs" / "data"
HIST = DATA / "history.jsonl"
STATE = DATA / "state.json"
H = int(CFG["horizon_hours"])
LOOK = int(CFG["lookback"])
COLS = ["timestamps", "open", "high", "low", "close", "volume", "amount"]


def log(msg: str) -> None:
    print(msg, flush=True)


def http_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "kronos-desk/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------- 1) velas ----------
def fetch_binance(sym: dict) -> pd.DataFrame:
    url = ("https://data-api.binance.vision/api/v3/klines?symbol=%s&interval=1h&limit=1000"
           % sym["binance"])
    now_ms = int(time.time() * 1000)
    rows = [
        (pd.to_datetime(r[0], unit="ms"), float(r[1]), float(r[2]), float(r[3]),
         float(r[4]), float(r[5]), float(r[7]))
        for r in http_json(url) if r[6] < now_ms          # r[6] = cierre de la vela; si es futuro, sigue abierta
    ]
    return pd.DataFrame(rows, columns=COLS)


def fetch_kraken(sym: dict) -> pd.DataFrame:
    if not sym.get("kraken"):
        raise RuntimeError("sin par en Kraken")
    d = http_json("https://api.kraken.com/0/public/OHLC?pair=%s&interval=60" % sym["kraken"])
    if d.get("error"):
        raise RuntimeError(str(d["error"]))
    key = [k for k in d["result"] if k != "last"][0]
    rows = d["result"][key][:-1]                           # la última vela de Kraken está abierta
    out = [(pd.to_datetime(r[0], unit="s"), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
            float(r[6]), float(r[6]) * float(r[5])) for r in rows]
    return pd.DataFrame(out, columns=COLS)


FETCHERS = {"binance": fetch_binance, "kraken": fetch_kraken}


def fetch_candles(sym: dict):
    last_err = None
    for name in CFG["sources"]:
        try:
            df = FETCHERS[name](sym)
            if len(df) < LOOK:
                raise RuntimeError("solo %d velas (<%d)" % (len(df), LOOK))
            return df.reset_index(drop=True), name
        except Exception as e:                              # noqa: BLE001
            last_err = "%s: %s" % (name, e.__class__.__name__)
            log("  fuente %s falló para %s (%s)" % (name, sym["id"], e.__class__.__name__))
    raise RuntimeError("sin datos para %s (%s)" % (sym["id"], last_err))


# ---------- 3) modelo ----------
def load_predictor():
    from model import Kronos, KronosPredictor, KronosTokenizer  # código de Kronos (commit fijado)
    tok = KronosTokenizer.from_pretrained(CFG["tokenizer"])
    mdl = Kronos.from_pretrained(CFG["model"])
    return KronosPredictor(mdl, tok, device="cpu", max_context=LOOK)


def forecast(pred, df: pd.DataFrame) -> dict:
    hist = df.iloc[-LOOK:].reset_index(drop=True)
    x_ts = hist["timestamps"]
    y_ts = pd.Series(pd.date_range(x_ts.iloc[-1] + pd.Timedelta(hours=1), periods=H, freq="h"))
    paths = []
    for _ in range(int(CFG["samples"])):                    # varias trayectorias = medida de incertidumbre
        out = pred.predict(df=hist[["open", "high", "low", "close", "volume", "amount"]],
                           x_timestamp=x_ts, y_timestamp=y_ts, pred_len=H,
                           T=1.0, top_p=0.9, sample_count=1, verbose=False)
        paths.append(out["close"].to_numpy(dtype=float))
    p = np.array(paths)
    base = float(hist["close"].iloc[-1])
    return {
        "base_time": x_ts.iloc[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_close": base,
        "mean": p.mean(axis=0).round(6).tolist(),
        "lo": p.min(axis=0).round(6).tolist(),
        "hi": p.max(axis=0).round(6).tolist(),
        "pred_close": float(p[:, -1].mean()),
        "p_up": float((p[:, -1] > base).mean()),
        "samples": int(p.shape[0]),
    }


# ---------- 2) sombra: corregir lo antiguo ----------
def load_hist() -> list[dict]:
    if not HIST.exists():
        return []
    return [json.loads(x) for x in HIST.read_text(encoding="utf-8").splitlines() if x.strip()]


def save_hist(rows: list[dict]) -> None:
    HIST.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def resolve(rows: list[dict], candles: dict[str, pd.DataFrame]) -> int:
    """Rellena actual_close cuando ya existe la vela objetivo (base + H horas)."""
    done = 0
    for r in rows:
        if r.get("actual_close") is not None or r["symbol"] not in candles:
            continue
        target = pd.Timestamp(r["base_time"]).tz_localize(None) + pd.Timedelta(hours=H)
        df = candles[r["symbol"]]
        hit = df.loc[df["timestamps"] == target, "close"]
        if len(hit):
            r["actual_close"] = float(hit.iloc[0])
            done += 1
    return done


def score(rows: list[dict]) -> dict:
    """Marcador: acierto de dirección y estrategia 'largo si sube / fuera si baja' vs mantener."""
    def calc(sub: list[dict]) -> dict:
        if not sub:
            return {"n": 0}
        sub = sorted(sub, key=lambda r: r["base_time"])
        ind = sub[::H]                                      # predicciones horarias se solapan: ~1 de cada H es independiente
        def stats(s):
            b = np.array([r["base_close"] for r in s]); a = np.array([r["actual_close"] for r in s])
            pu = np.array([r["pred_close"] for r in s]) > b
            ret = a / b - 1
            return {"n": len(s), "hit_rate": float((pu == (a > b)).mean()),
                    "strategy_pct": float(np.where(pu, ret, 0).mean() * 100),
                    "hold_pct": float(ret.mean() * 100)}
        full = stats(sub)
        full["independent"] = stats(ind)
        full["since"] = sub[0]["base_time"]
        return full
    done = [r for r in rows if r.get("actual_close") is not None]
    out = {"ALL": calc(done)}
    for s in sorted({r["symbol"] for r in done}):
        out[s] = calc([r for r in done if r["symbol"] == s])
    return out


# ---------- 5) alertas ----------
def fp(x: float) -> str:
    return "%.4f" % x if x < 10 else "%.2f" % x


def conditions(sym: str, f: dict, move_pct: float) -> dict[str, str]:
    """Devuelve {clave: texto} de las condiciones que se cumplen ahora."""
    base, pc = f["base_close"], f["pred_close"]
    chg = (pc / base - 1) * 100
    out = {}
    if abs(chg) >= move_pct:
        d = "up" if chg > 0 else "down"
        out["move:" + d] = "%s: Kronos prevé %+.2f%% a %dh (%s -> %s)" % (sym, chg, H, fp(base), fp(pc))
    for lv in CFG.get("levels", {}).get(sym, []):
        if base < lv <= pc:
            out["level:%s:up" % lv] = "%s: la predicción CRUZA AL ALZA el nivel %s (ahora %s, previsto %s)" % (sym, lv, fp(base), fp(pc))
        elif pc <= lv < base:
            out["level:%s:down" % lv] = "%s: la predicción CRUZA A LA BAJA el nivel %s (ahora %s, previsto %s)" % (sym, lv, fp(base), fp(pc))
    return out


def lectura(f: dict) -> str:
    """Lectura de Katerina: reglas fijas, sin IA adicional. Describe cuánto se parecen las trayectorias."""
    base, n = f["base_close"], f["samples"]
    ups = int(round(f["p_up"] * n))
    lo = (f["lo"][-1] / base - 1) * 100
    hi = (f["hi"][-1] / base - 1) * 100
    if hi - lo >= 3.0:
        juicio = "rango amplio, señal poco concluyente"
    elif ups in (0, n):
        juicio = "las trayectorias coinciden, señal más consistente"
    else:
        juicio = "trayectorias divididas, sin dirección clara"
    return "%d de %d trayectorias suben; rango %+.1f%% a %+.1f%%: %s." % (ups, n, lo, hi, juicio)


def katerina_body(entries: list, prueba: bool = False) -> str:
    cab = "Katerina · Kronos Desk (modo sombra)"
    lines = [("[PRUEBA] " if prueba else "") + cab]
    for sid, f, texts in entries:
        lines += texts
        lines.append("   Lectura: " + lectura(f))
    lines.append("Es una lectura de estudio, sin dinero real. La decisión es tuya.")
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("[DRY-RUN Telegram] " + text.replace("\n", " | "))
        return True
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=data), timeout=20).read()
        return True
    except Exception as e:                                  # noqa: BLE001
        # NO imprimimos la excepción completa: algunas incluyen la URL y la URL lleva el token
        log("Telegram falló: %s %s" % (e.__class__.__name__, getattr(e, "code", "")))
        return False


# ---------- principal ----------
def main() -> int:
    DATA.mkdir(exist_ok=True)
    PANEL.mkdir(parents=True, exist_ok=True)
    move_pct = float(os.environ.get("KD_ALERT_PCT", CFG["alert_move_pct"]))
    t0 = time.time()
    if os.environ.get("KD_TEST") == "1":                    # prueba manual de Telegram
        log("Secretos de Telegram presentes: token=%s chat_id=%s" % (
            bool(os.environ.get("TELEGRAM_TOKEN")), bool(os.environ.get("TELEGRAM_CHAT_ID"))))

    candles, sources = {}, {}
    for s in CFG["symbols"]:
        try:
            candles[s["id"]], sources[s["id"]] = fetch_candles(s)
        except Exception as e:                              # noqa: BLE001
            log("ERROR " + str(e))
    if not candles:
        log("Ninguna fuente respondió: se aborta (el Action queda en rojo)")
        return 1

    rows = load_hist()
    log("Predicciones corregidas con datos reales: %d" % resolve(rows, candles))

    pred = load_predictor()
    forecasts = {}
    for sid, df in candles.items():
        f = forecast(pred, df)
        f["source"] = sources[sid]
        f["recent"] = [[t.strftime("%Y-%m-%dT%H:%M:%SZ"), round(c, 6)]
                       for t, c in zip(df["timestamps"].iloc[-96:], df["close"].iloc[-96:])]
        forecasts[sid] = f
        if not any(r["symbol"] == sid and r["base_time"] == f["base_time"] for r in rows):
            rows.append({"symbol": sid, "base_time": f["base_time"], "base_close": f["base_close"],
                         "pred_close": f["pred_close"], "p_up": f["p_up"], "actual_close": None,
                         "made_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        log("%s: %.4f -> %.4f (%+.2f%%) p_sube=%.0f%% [%s]" % (
            sid, f["base_close"], f["pred_close"], (f["pred_close"] / f["base_close"] - 1) * 100,
            f["p_up"] * 100, f["source"]))
    save_hist(rows)

    # cartera de papel de Katerina (simulación): paradas, rebalanceo semanal, operaciones diarias e informe
    informe = paper.step(CFG, forecasts, datetime.now(timezone.utc), force=os.environ.get("KD_FORCE_REPORT") == "1")
    if informe:
        send_telegram(informe)

    # alertas: solo cuando una condición ENTRA (no se repite cada hora mientras siga activa)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"active": {}}
    new_active, entries = {}, []
    for sid, f in forecasts.items():
        cond = conditions(sid, f, move_pct)
        new_active[sid] = sorted(cond)
        nuevos = [t for k, t in cond.items() if k not in state["active"].get(sid, [])]
        if nuevos:
            entries.append((sid, f, nuevos))
    if os.environ.get("KD_TEST") == "1":                    # muestra del formato con datos reales de ahora
        log("Mensaje de prueba enviado: %s" % send_telegram(katerina_body(
            [(sid, f, ["%s: Kronos prevé %+.2f%% a %dh (%s -> %s)" % (
                sid, (f["pred_close"] / f["base_close"] - 1) * 100, H, fp(f["base_close"]), fp(f["pred_close"]))])
             for sid, f in forecasts.items()], prueba=True)))
    if entries:
        if not send_telegram(katerina_body(entries)):
            new_active = state["active"]                    # si falló el envío, se reintenta la próxima hora
    for s in CFG["symbols"]:                                # activo que falló hoy: conserva su estado anterior
        new_active.setdefault(s["id"], state["active"].get(s["id"], []))
    STATE.write_text(json.dumps({"active": new_active}, indent=1), encoding="utf-8")

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon_hours": H, "model": CFG["model"], "samples": CFG["samples"],
        "forecasts": forecasts, "scores": score(rows),
    }
    (PANEL / "latest.json").write_text(json.dumps(out), encoding="utf-8")
    log("Hecho en %.0fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
