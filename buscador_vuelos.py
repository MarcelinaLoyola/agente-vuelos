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
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

try:
    import google_flights as gf
except ImportError:  # faltan dependencias: se usa solo el cache de Travelpayouts
    gf = None

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
    "umbral_total_eur": 980,        # avisa la primera vez que el total baja de esto
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


def conecta_ida(aqp_lim, lim_mad, margen_min=180):
    """Con billetes separados y 0 noches en Lima, el LIM-MAD debe salir mucho despues de que
    aterrice el AQP-LIM. Solo se puede comprobar la ida: Google no da el horario de la vuelta."""
    llegada = gf.minutos_dia(aqp_lim["hora_salida"]) + (aqp_lim["duracion_min"] or 0)
    return gf.minutos_dia(lim_mad["hora_salida"]) - llegada >= margen_min


def buscar_vivo():
    """Precios en vivo de Google Flights. Devuelve None si Google no responde."""
    c = CONFIG
    d0 = d(c["salida_desde"])
    opciones = c["noches_lima_opciones"]
    tramos, unico = [], []
    fallos_seguidos = 0

    def consulta(origen, destino, ida, vuelta):
        nonlocal fallos_seguidos
        try:
            r = gf.mas_barato(origen, destino, ida, vuelta, c["moneda"].upper())
            fallos_seguidos = 0
            return r
        except gf.GoogleError as e:
            fallos_seguidos += 1
            print(f"[aviso] Google Flights: {e}", file=sys.stderr)
            if fallos_seguidos >= 3:
                raise
            return None

    try:
        for i in range(c["flex_dias"] + 1):
            dep = d0 + timedelta(i)
            for dur in range(c["dur_min"], c["dur_max"] + 1):
                ret = dep + timedelta(dur)
                b = consulta(c["origen"], c["hub"], dep, ret)
                if b:
                    for no, nb in itertools.product(opciones, opciones):
                        dep_lim, ret_mad = dep + timedelta(no), ret - timedelta(nb)
                        a = consulta(c["hub"], c["destino"], dep_lim, ret_mad)
                        if not a:
                            continue
                        if no == 0 and not conecta_ida(b, a):
                            continue
                        hotel = c["hotel_noche_eur"] * (no + nb)
                        tramos.append({
                            "total": a["precio"] + b["precio"] + hotel,
                            "lim_mad": a["precio"], "aqp_lim": b["precio"], "hotel": hotel,
                            "aqp_lim_estimado": False, "noches": (no, nb),
                            "salida": dep, "regreso": ret, "dias": dur,
                            "lim_mad_salida": dep_lim, "mad_lim_salida": ret_mad,
                            "link": "",
                            "detalle": (f"LIM-MAD: {', '.join(a['aerolineas'])}, "
                                        f"escalas {a['escalas']} | AQP-LIM: "
                                        f"{', '.join(b['aerolineas'])}")})
                m = consulta(c["origen"], c["destino"], dep, ret)
                if m:
                    unico.append({"total": m["precio"], "salida": dep, "regreso": ret,
                                  "dias": dur, "escalas": m["escalas"], "link": ""})
    except gf.GoogleError:
        return None
    print(f"[info] Google Flights: {gf.stats['consultas']} consultas, "
          f"{gf.stats['fallos']} fallidas.", file=sys.stderr)
    tramos.sort(key=lambda x: x["total"])
    unico.sort(key=lambda x: x["total"])
    return tramos[: c["top"]], unico[: c["top"]]


def buscar_vivo_robusto(max_procesos=40):
    """Hace la busqueda en vivo en procesos hijos, que se relanzan si el cliente HTTP los mata.

    Cada hijo guarda sus resultados en el cache en disco de google_flights; al terminar uno
    sin caidas, el proceso principal arma el resultado leyendo solo de ese cache.
    """
    if gf is None:
        return None
    for _ in range(max_procesos):
        hijo = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--precalentar"])
        if hijo.returncode == 0:
            return buscar_vivo()
    print("[aviso] La busqueda en vivo se interrumpio demasiadas veces.", file=sys.stderr)
    return None


def buscar_cache():
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


def mensaje(tramos, unico, fuente="cache"):
    c = CONFIG
    en_vivo = fuente == "vivo"
    m = "Vuelos Arequipa - Lima - Madrid " + (
        "(precios en vivo de Google Flights)\n" if en_vivo else "(precios en cache)\n")
    if tramos:
        m += "\nMejores combinaciones por tramos (total con hotel):\n"
        for t in tramos:
            est = " (AQP-LIM estimado)" if t["aqp_lim_estimado"] else ""
            m += (f"- {f(t['total'])} EUR | sale {t['salida']} vuelve {t['regreso']} "
                  f"({t['dias']} dias){est}\n"
                  f"  Noches en Lima ida/vuelta: {t['noches'][0]}/{t['noches'][1]}\n"
                  f"  LIM-MAD ida/vuelta {f(t['lim_mad'])} + AQP-LIM {f(t['aqp_lim'])} "
                  f"+ hotel {f(t['hotel'])}\n"
                  + (f"  {t['detalle']}\n" if t.get("detalle") else "")
                  + (f"  {t['link']}\n" if t["link"] else "")
                  + f"  Confirmar en vivo (Google Flights):\n"
                  f"  AQP-LIM: {gflights(c['origen'], c['hub'], t['salida'], t['regreso'])}\n"
                  f"  LIM-MAD: {gflights(c['hub'], c['destino'], t['lim_mad_salida'], t['mad_lim_salida'])}\n")
    else:
        m += "\nNo encontre combinaciones por tramos en esas fechas.\n"
    if unico:
        m += "\nBillete unico AQP-MAD:\n"
        for u in unico[:3]:
            m += (f"- {f(u['total'])} EUR | sale {u['salida']} vuelve {u['regreso']} "
                  f"({u['dias']} dias, escalas: {u['escalas']})\n"
                  + (f"  {u['link']}\n" if u["link"] else "")
                  + f"  Google Flights: {gflights(c['origen'], c['destino'], u['salida'], u['regreso'])}\n")
    m += ("\nPrecios en vivo, pero pueden cambiar: confirma al abrir el enlace. " if en_vivo
          else "\nPrecios en cache de buscadores: confirma al abrir el enlace. ")
    m += ("Con 0 noches en Lima, la conexion de ida se verifica (3 h minimo) pero la de vuelta "
          "no, y no hay proteccion entre billetes separados.")
    return m


def guia_compra(tramos, unico):
    """Segundo mensaje del aviso: como comprar la opcion mas barata sin llevarse sorpresas."""
    mejor_t = tramos[0] if tramos else None
    mejor_u = unico[0] if unico else None
    g = "Como comprarlo (lo haces tu, desde tu navegador: el agente nunca compra)\n\n"
    if mejor_t and (not mejor_u or mejor_t["total"] <= mejor_u["total"]):
        g += (f"Opcion elegida: {f(mejor_t['total'])} EUR en dos billetes separados.\n"
              + (f"{mejor_t['detalle']}\n" if mejor_t.get("detalle") else "") + "\n"
              "1. Abre cada enlace de Google Flights y confirma que el precio sigue igual. "
              "Los precios cambian en horas.\n"
              "2. Elige 'Reservar' en la web de la aerolinea si cuesta lo mismo que en una agencia: "
              "los cambios y reclamos son mas faciles.\n"
              "3. Compra primero el LIM-MAD, que es el caro y el que manda en las fechas; "
              "el AQP-LIM despues, enseguida.\n"
              "4. Antes de pagar, mira el equipaje: los precios que ves suelen ser la tarifa "
              "basica sin maleta, y una low cost puede cobrarla aparte.\n"
              "5. Con billetes separados debes recoger tu maleta en Lima y facturarla de nuevo. "
              "Deja margen: 4 horas o una noche, sobre todo en la vuelta, que el agente no puede "
              "verificar. Revisa tu mismo la hora de llegada del MAD-LIM y la salida del LIM-AQP.\n"
              "6. Paga con tarjeta de credito (da mas proteccion) y compara el total en EUR y en "
              "PEN: el cambio de moneda de tu banco puede subir el precio.\n")
    elif mejor_u:
        g += (f"Opcion elegida: {f(mejor_u['total'])} EUR en un solo billete AQP-MAD.\n\n"
              "1. Abre el enlace de Google Flights y confirma el precio.\n"
              "2. Reserva en la web de la aerolinea. Un solo billete te protege si pierdes la "
              "conexion en Lima.\n"
              "3. Revisa equipaje y paga con tarjeta de credito, comparando EUR y PEN.\n")
    g += ("\nSi al abrir el enlace el precio es mayor, no pasa nada: el agente seguira buscando "
          "y te avisara si baja otra vez.")
    return g


def telegram(texto):
    """Envia el mensaje. Devuelve True solo si Telegram lo acepto."""
    # strip: un secret pegado con espacios, salto de linea o BOM (﻿) rompe la peticion
    tok, chat = (os.environ.get(v, "").strip("﻿ \t\r\n")
                 for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))
    if not tok or not chat:
        print("[aviso] Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID: no se envio aviso.")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=30)
        return True
    except Exception as e:
        print(f"[aviso] No se pudo enviar el mensaje de Telegram: {e}", file=sys.stderr)
        return False


def main():
    if "--test-telegram" in sys.argv:
        telegram("Prueba: el agente de vuelos AQP-LIM-MAD esta conectado.")
        return
    if "--precalentar" in sys.argv:  # modo hijo: solo llena el cache en disco
        buscar_vivo()
        return
    resultado = buscar_vivo_robusto()
    fuente = "vivo"
    if resultado is None or not (resultado[0] or resultado[1]):
        print("[aviso] Sin datos en vivo de Google Flights: uso el cache de Travelpayouts.",
              file=sys.stderr)
        resultado, fuente = buscar_cache(), "cache"
    tramos, unico = resultado
    txt = mensaje(tramos, unico, fuente)
    print(txt, flush=True)

    # Solo precios confiables deciden el aviso: las combinaciones con AQP-LIM estimado
    # (mediana) se muestran, pero no disparan alertas.
    confiables = [t["total"] for t in tramos if not t["aqp_lim_estimado"]]
    confiables += [u["total"] for u in unico]
    mejor_hoy = min(confiables, default=None)
    if mejor_hoy is None:
        return
    # El estado guarda el ultimo precio por el que SI se aviso, no el mejor visto: asi un
    # precio bueno que no llego a Telegram (sin configurar, o con error) se avisa despues.
    estado = {}
    if STATE_FILE.exists():
        estado = json.loads(STATE_FILE.read_text())
    avisado = estado.get("avisado")
    if avisado is None:
        avisar = mejor_hoy <= CONFIG["umbral_total_eur"]
        motivo = "Precio bajo tu umbral"
    else:
        avisar = mejor_hoy <= avisado * (1 - CONFIG["mejora_minima_pct"] / 100)
        motivo = "El precio bajo respecto al ultimo aviso"
    if avisar and telegram(f"{motivo}: {f(mejor_hoy)} EUR\n\n{txt}"):
        avisado = mejor_hoy
        telegram(guia_compra(tramos, unico))
    nuevo = {"mejor": min(mejor_hoy, estado.get("mejor", mejor_hoy)), "avisado": avisado,
             "fecha": date.today().isoformat()}
    if (nuevo["mejor"], nuevo["avisado"]) != (estado.get("mejor"), estado.get("avisado")):
        STATE_FILE.write_text(json.dumps(nuevo))


if __name__ == "__main__":
    main()
