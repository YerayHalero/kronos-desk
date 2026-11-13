# Kronos Desk (modo sombra)

Panel de predicción de velas cripto (BTC, ETH, SOL) sin servidor y sin coste de API.
**Sin dinero real y sin órdenes automáticas.** Es un experimento de seis semanas para medir si el modelo aporta algo frente a simplemente mantener.

## Cómo funciona (cada hora)
1. **GitHub Actions** arranca un ordenador virtual efímero (cron, minuto 7).
2. `src/run.py` descarga las últimas 1.000 velas de 1 h **cerradas** (Binance `data-api.binance.vision`; si falla, Kraken).
3. Corrige predicciones antiguas: si hace 24 h dijimos «sube» y ya existe la vela real, anota qué pasó (`data/history.jsonl`).
4. Kronos-small (24,7 M, CPU) recibe las últimas 512 velas y genera 5 trayectorias de las próximas 24 h. La media es la predicción; el rango entre trayectorias es la incertidumbre.
5. Calcula el marcador: acierto de dirección y rendimiento de «largo si sube / fuera si baja» frente a mantener.
6. Alerta por Telegram solo cuando una condición **entra** (cambio previsto ≥ `alert_move_pct`, o la predicción cruza un nivel de `config.json`). Mientras siga activa no repite.
7. Guarda `docs/data/latest.json` y hace commit. GitHub Pages sirve `docs/index.html`, que lee ese JSON.

Rendimiento medido en CPU de 2 núcleos: ~4 s por trayectoria de 24 h; unos 60 s las tres monedas con 5 trayectorias.

## Puesta en marcha
1. Crea el repo `kronos-desk` y sube estos ficheros.
2. **Telegram:** en @BotFather → `/newbot` → copia el token. Escribe cualquier mensaje a tu bot. Abre en tu navegador `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca `"chat":{"id":NÚMERO}`. Ese número es tu chat id.
3. Repo → Settings → Secrets and variables → Actions → crea `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`. Sin ellos el sistema funciona pero solo escribe las alertas en el log.
4. Settings → Pages → Source: *Deploy from a branch* → `main` / carpeta `/docs`.
5. Pestaña Actions → *kronos-hourly* → *Run workflow* para la primera ejecución.
6. Edita `config.json` → `levels` con los niveles que quieras vigilar, p. ej. `"BTC": [70000, 75000]`.

## Riesgos y límites (Nuria)
- Un token de bot filtrado permite a otros escribir en tu nombre al bot. Va solo como secreto; nunca en el código ni en un chat.
- Repo público = cualquiera ve predicciones y marcador. GitHub Pages en repos privados requiere plan de pago (verifica tu plan).
- El marcador tarda: no hay ninguna comparación válida hasta 24 h después de la primera predicción, y con pocas decenas de datos independientes todo es ruido.
- Las IP de GitHub pueden estar bloqueadas por alguna fuente; por eso hay una segunda fuente (Kraken), **que no se ha podido probar**.
- El código de Kronos queda fijado a un commit concreto: no se actualiza solo.
- Los workflows programados pueden retrasarse y GitHub puede pausarlos tras mucho tiempo sin actividad en el repo; revisa la pestaña Actions de vez en cuando.

## Coste (Álvaro)
Repo público: 0 €. Repo privado: Actions tiene un cupo mensual gratuito (≈2.000 min en el plan gratuito, según recuerdo; verifícalo) y 720 ejecuciones/mes × ~2-3 min está en el límite.
