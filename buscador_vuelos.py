#!/usr/bin/env python3
"""
Agente de vuelos: Arequipa (AQP) -> Lima (LIM) -> Madrid (MAD) y vuelta.

Busca combinaciones de fechas (salida desde el 12-feb-2027 con unos dias de flexibilidad,
estancia de 21 a 28 dias), suma los dos tramos (AQP-LIM + LIM-MAD + hotel en Lima si hay
noche de escala), los compara con un billete unico AQP-MAD y te avisa por Telegram cuando
el mejor precio confiable baja.

Fuente de precios: API de datos de Travelpayouts/Aviasales (gratuita, precios en cache
de los ultimos dias). Los precios son orientativos: confirma siempre en el enlace.
Solo usa la libreria estandar de Python.
"""
import itertools
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

CONFIG = {
    "origen": "AQP",
    "hub": "LIM",
    "destino": "MAD",
    "salida_desde": "2027-02-12",   # primer dia en que podrias salir de Arequipa
    "flex_dias": 3,                 # flexibilidad: se prueba salir entre salida_desde y +N dias
    "dur_min": 21,                  # 21 dias = 3 semanas (duracion minima del viaje)
    "dur_max": 28,                  # 28 dias = 4 semanas (duracion maxima del viaje)
    "noches_lima_opciones": [0, 1], # noches en Lima a probar en cada sentido (0 = mismo dia)
    "hotel_noche_eur": 40,
    "umbral_total_eur": 1100,       # avisa la primera vez que el total baja de esto
    "mejora_minima_pct": 5,         # y despues, solo si mejora el mejor precio anterior en este %
    "moneda": "eur",
    "top": 3,                       # con mas, el aviso supera los 4000 caracteres de Telegram
}

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
STATE_FILE = Path(__file__).with_name("estado.json")


def d(s):
    return date.fromisoformat(s[:10])


def months(d1, d2):
    out, cur = [], date(d1.year, d1.month, 1)
    while cur <= d2:
        out.append(cur.strftime("%Y-%m"))
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
    return out


def api_get(params):
    token = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Access-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_roundtrips(o, dest, dep_months, ret_months):
    """Devuelve {(salida, regreso): oferta mas barata} para ida y vuelta o-dest."""
    best = {}
    for dm in dep_months:
        for rm in ret_months:
            try:
                data = api_get({
                    "origin": o, "destination": dest,
                    "departure_at": dm, "return_at": rm,
                    "one_way": "false", "sorting": "price",
                    "limit": 1000, "unique": "false",
                    "currency": CONFIG["moneda"],
                })
            except Exception as e:  # red caida, token invalido, etc.
                print(f"[aviso] {o}-{dest} {dm}/{rm}: {e}", file=sys.stderr)
                continue
            for it in data.get("data", []):
                if not it.get("return_at"):
                    continue
                key = (d(it["departure_at"]), d(it["return_at"]))
                if key not in best or it["price"] < best[key]["price"]:
                    best[key] = it
    return best


def link(it):
    l = it.get("link", "")
    return ("https://www.aviasales.com" + l) if l.startswith("/") else l


def gflights(o, dest, ida, vuelta):
    """Enlace a Google Flights con ruta y fechas ya cargadas, para confirmar el precio en vivo."""
    q = f"Flights from {o} to {dest} on {ida} through {vuelta}"
    params = {"q": q, "curr": CONFIG["moneda"].upper()}
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote)


def buscar():
    c = CONFIG
    d0 = d(c["salida_desde"])
    d1 = d0 + timedelta(c["flex_dias"])
    r0, r1 = d0 + timedelta(c["dur_min"]), d1 + timedelta(c["dur_max"])
    opciones = c["noches_lima_opciones"]
    max_n = max(opciones)

    lim_mad = fetch_roundtrips(
        c["hub"], c["destino"],
        months(d0, d1 + timedelta(max_n)),
        months(r0 - timedelta(max_n), r1))
    aqp_lim = fetch_roundtrips(c["origen"], c["hub"], months(d0, d1), months(r0, r1))
    aqp_mad = fetch_roundtrips(c["origen"], c["destino"], months(d0, d1), months(r0, r1))

    ref_lim = statistics.median([v["price"] for v in aqp_lim.values()]) if aqp_lim else None

    tramos, unico = [], []
    for no, nb in itertools.product(opciones, opciones):
        hotel = c["hotel_noche_eur"] * (no + nb)
        for (dep_lim, ret_mad), a in lim_mad.items():
            aqp_dep, aqp_ret = dep_lim - timedelta(no), ret_mad + timedelta(nb)
            dur = (aqp_ret - aqp_dep).days
            if not (d0 <= aqp_dep <= d1 and c["dur_min"] <= dur <= c["dur_max"]):
                continue
            b = aqp_lim.get((aqp_dep, aqp_ret))
            estimado = b is None
            precio_b = b["price"] if b else ref_lim
            if precio_b is None:
                continue
            tramos.append({
                "total": a["price"] + precio_b + hotel,
                "lim_mad": a["price"], "aqp_lim": precio_b, "hotel": hotel,
                "aqp_lim_estimado": estimado, "noches": (no, nb),
                "salida": aqp_dep, "regreso": aqp_ret, "dias": dur,
                "lim_mad_salida": dep_lim, "mad_lim_salida": ret_mad,
                "link": link(a),
            })
    for (dep, ret), a in aqp_mad.items():
        dur = (ret - dep).days
        if d0 <= dep <= d1 and c["dur_min"] <= dur <= c["dur_max"]:
            unico.append({"total": a["price"], "salida": dep, "regreso": ret,
                          "dias": dur, "escalas": a.get("transfers"), "link": link(a)})
    tramos.sort(key=lambda x: x["total"])
    unico.sort(key=lambda x: x["total"])
    return tramos[: c["top"]], unico[: c["top"]]


def f(x):
    return f"{x:,.0f}".replace(",", ".")


def mensaje(tramos, unico):
    c = CONFIG
    m ="Vuelos Arequipa - Lima - Madrid\n"
    if tramos:
        m += "\nMejores combinaciones por tramos (total con hotel):\n"
        for t in tramos:
            est = " (AQP-LIM estimado)" if t["aqp_lim_estimado"] else ""
            m += (f"- {f(t['total'])} EUR | sale {t['salida']} vuelve {t['regreso']} "
                  f"({t['dias']} dias){est}\n"
                  f"  Noches en Lima ida/vuelta: {t['noches'][0]}/{t['noches'][1]}\n"
                  f"  LIM-MAD ida/vuelta {f(t['lim_mad'])} + AQP-LIM {f(t['aqp_lim'])} "
                  f"+ hotel {f(t['hotel'])}\n  {t['link']}\n"
                  f"  Confirmar en vivo (Google Flights):\n"
                  f"  AQP-LIM: {gflights(c['origen'], c['hub'], t['salida'], t['regreso'])}\n"
                  f"  LIM-MAD: {gflights(c['hub'], c['destino'], t['lim_mad_salida'], t['mad_lim_salida'])}\n")
    else:
        m += "\nNo encontre combinaciones por tramos en esas fechas.\n"
    if unico:
        m += "\nBillete unico AQP-MAD:\n"
        for u in unico[:3]:
            m += (f"- {f(u['total'])} EUR | sale {u['salida']} vuelve {u['regreso']} "
                  f"({u['dias']} dias, escalas: {u['escalas']})\n  {u['link']}\n"
                  f"  Google Flights: {gflights(c['origen'], c['destino'], u['salida'], u['regreso'])}\n")
    m += ("\nPrecios en cache de buscadores: confirma al abrir el enlace. "
          "Con 0 noches en Lima no hay proteccion de conexion entre billetes separados.")
    return m


def telegram(texto):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[aviso] Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID: no se envio aviso.")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=30)
    except Exception as e:
        print(f"[aviso] No se pudo enviar el mensaje de Telegram: {e}", file=sys.stderr)


def main():
    if "--test-telegram" in sys.argv:
        telegram("Prueba: el agente de vuelos AQP-LIM-MAD esta conectado.")
        return
    tramos, unico = buscar()
    txt = mensaje(tramos, unico)
    print(txt)

    # Solo precios confiables deciden el aviso: las combinaciones con AQP-LIM estimado
    # (mediana) se muestran, pero no disparan alertas.
    confiables = [t["total"] for t in tramos if not t["aqp_lim_estimado"]]
    confiables += [u["total"] for u in unico]
    mejor_hoy = min(confiables, default=None)
    if mejor_hoy is None:
        return
    prev = None
    if STATE_FILE.exists():
        prev = json.loads(STATE_FILE.read_text()).get("mejor")
    if prev is None:
        avisar = mejor_hoy <= CONFIG["umbral_total_eur"]
        motivo = "Precio bajo tu umbral"
    else:
        avisar = mejor_hoy <= prev * (1 - CONFIG["mejora_minima_pct"] / 100)
        motivo = "El precio bajo respecto al anterior"
    if avisar:
        telegram(f"{motivo}: {f(mejor_hoy)} EUR\n\n{txt}")
    if prev is None or mejor_hoy < prev:
        STATE_FILE.write_text(json.dumps({"mejor": mejor_hoy, "fecha": date.today().isoformat()}))


if __name__ == "__main__":
    main()
