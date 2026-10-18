// Bot de Telegram para crear y gestionar viajes del agente de vuelos.
// Corre en Cloudflare Workers (plan gratis). La conversacion se guarda en KV y los viajes
// terminan en viajes.json del repositorio, que lee el buscador de GitHub Actions.

const TTL_CONVERSACION = 3600;        // segundos sin responder antes de olvidar la conversacion
const MAX_COMBINACIONES = 40;         // (dias de flexibilidad + 1) x (duraciones distintas)
const MAX_DIAS_ADELANTE = 330;        // Google Flights solo ofrece vuelos hasta ~11 meses
const ORDEN = ["destino", "origen", "fecha", "flex", "dur", "adultos", "presupuesto", "hub",
  "confirmar"];
const LUGARES = ["destino", "origen", "hub"];

const AYUDA = `Comandos:
/viaje - crear un viaje nuevo (te hago unas preguntas)
/viajes - ver tus viajes activos
/borrar <numero o id> - borrar un viaje
/cancelar - cancelar lo que estabas haciendo`;

// ---------------------------------------------------------------- Telegram

async function enviar(env, chat, texto) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chat, text: texto, disable_web_page_preview: true }),
  });
  if (!r.ok) console.error("sendMessage", r.status, await r.text());
}

// ---------------------------------------------------------------- GitHub (viajes.json)

const b64 = (s) => {
  let bin = "";
  for (const b of new TextEncoder().encode(s)) bin += String.fromCharCode(b);
  return btoa(bin);
};
const unb64 = (s) =>
  new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\s/g, "")), (c) => c.charCodeAt(0)));

function ghHeaders(env) {
  return {
    authorization: `Bearer ${env.GITHUB_TOKEN}`,
    accept: "application/vnd.github+json",
    "user-agent": "agente-vuelos-bot",
    "x-github-api-version": "2022-11-28",
  };
}

async function leerViajes(env) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/viajes.json`;
  const r = await fetch(url, { headers: ghHeaders(env) });
  if (r.status === 404) return { datos: { viajes: [] }, sha: undefined };
  if (!r.ok) throw new Error(`GitHub GET ${r.status}`);
  const j = await r.json();
  return { datos: JSON.parse(unb64(j.content)), sha: j.sha };
}

// Lee, modifica y escribe viajes.json. Reintenta si alguien lo cambio entre la lectura y la
// escritura (el sha ya no coincide).
async function modificarViajes(env, mensaje, cambiar) {
  for (let intento = 0; intento < 3; intento++) {
    const { datos, sha } = await leerViajes(env);
    const antes = JSON.stringify(datos);
    const resultado = cambiar(datos);
    if (JSON.stringify(datos) === antes) return resultado; // nada que guardar
    const url = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/viajes.json`;
    const r = await fetch(url, {
      method: "PUT",
      headers: { ...ghHeaders(env), "content-type": "application/json" },
      body: JSON.stringify({
        message: mensaje,
        content: b64(JSON.stringify(datos, null, 1) + "\n"),
        ...(sha ? { sha } : {}),
      }),
    });
    if (r.ok) return resultado;
    if (r.status !== 409 && r.status !== 422) throw new Error(`GitHub PUT ${r.status}`);
  }
  throw new Error("GitHub PUT: conflicto repetido");
}

// ---------------------------------------------------------------- lugares (IATA)

export async function buscarLugar(texto) {
  const url = "https://autocomplete.travelpayouts.com/places2?term=" +
    `${encodeURIComponent(texto)}&locale=es&types[]=city&types[]=airport`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`autocompletado ${r.status}`);
  const vistos = new Set();
  const out = [];
  for (const x of await r.json()) {
    if (!x.code || vistos.has(x.code)) continue;
    vistos.add(x.code);
    out.push({
      code: x.code,
      nombre: x.type === "airport" ? (x.city_name || x.name) : x.name,
      detalle: `${x.name}${x.country_name ? ", " + x.country_name : ""}`,
    });
    if (out.length >= 5) break;
  }
  return out;
}

// ---------------------------------------------------------------- validaciones

export function parseFecha(t) {
  let y, mo, d;
  let m = t.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) [y, mo, d] = [+m[1], +m[2], +m[3]];
  else {
    m = t.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/);
    if (!m) return null;
    [d, mo, y] = [+m[1], +m[2], +m[3]];
  }
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) {
    return null;
  }
  return dt;
}

const iso = (dt) => dt.toISOString().slice(0, 10);
const hoyUTC = () => {
  const t = new Date();
  return new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate()));
};

export function parseDuracion(t) {
  const m = t.replace(/\s/g, "").match(/^(\d{1,3})(?:[-–a](\d{1,3}))?$/);
  if (!m) return null;
  const min = +m[1];
  const max = m[2] ? +m[2] : min;
  if (min < 1 || max < min || max > 120) return null;
  return { min, max };
}

export function slug(t) {
  return t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

const plural = (n, s) => `${n} ${s}${n === 1 ? "" : "s"}`;

function resumen(s) {
  const d = s.datos;
  const fin = new Date(parseFecha(d.fecha).getTime() + d.flex * 86400000);
  return [
    `Destino: ${d.destinoNombre} (${d.destino})`,
    `Sales de: ${d.origenNombre} (${d.origen})`,
    `Salida: desde ${d.fecha}${d.flex ? ` hasta ${iso(fin)}` : ""}`,
    `Duracion: ${d.durMin === d.durMax ? plural(d.durMin, "dia") : `${d.durMin} a ${d.durMax} dias`}`,
    `Personas: ${plural(d.adultos, "adulto")}`,
    `Presupuesto: ${d.presupuesto ? `${d.presupuesto} EUR por persona` : "sin limite (aviso el precio mas bajo)"}`,
    `Escala para comparar: ${d.hub ? `${d.hubNombre} (${d.hub})` : "ninguna"}`,
  ].join("\n");
}

// ---------------------------------------------------------------- conversacion

function pregunta(paso, s, env) {
  const origen = env.DEFAULT_ORIGIN || "AQP";
  switch (paso) {
    case "destino":
      return "A donde quieres viajar? Escribe la ciudad o el codigo del aeropuerto (ej: Madrid, MAD).";
    case "origen":
      return `Desde donde sales? Escribe "ok" para ${origen} o dime otra ciudad.`;
    case "fecha":
      return "Desde que fecha quieres salir? (ej: 12/02/2027)";
    case "flex":
      return "Cuantos dias de flexibilidad tienes para salir? Pruebo la fecha elegida y hasta N dias despues (0 a 7).";
    case "dur":
      return "Cuantos dias dura el viaje? Escribe un numero (ej: 21) o un rango (ej: 21-28).";
    case "adultos":
      return "Cuantas personas viajan? (1 a 9, todas adultas)";
    case "presupuesto":
      return 'Cual es tu presupuesto maximo POR PERSONA, en euros? Escribe un numero, o "no" si solo quieres que te avise del precio mas bajo que encuentre.';
    case "hub":
      return 'Quieres comparar tambien billetes separados con escala en otra ciudad? Suele salir mas barato. Escribe la ciudad (ej: Lima) o "no".';
    case "confirmar":
      return `Listo. Revisa:\n\n${resumen(s)}\n\nLo guardo? Responde "si" o "no".`;
  }
}

function siguiente(s, env) {
  s.paso = ORDEN[ORDEN.indexOf(s.paso) + 1];
  delete s.opciones;
  return pregunta(s.paso, s, env);
}

function fijarLugar(s, campo, lugar) {
  s.datos[campo] = lugar.code;
  s.datos[campo + "Nombre"] = lugar.nombre;
}

async function pasoLugar(env, s, texto) {
  const campo = s.paso;
  const origenPorDefecto = env.DEFAULT_ORIGIN || "AQP";
  if (campo === "hub" && /^(no|ninguna|ninguno|nada)$/i.test(texto)) {
    s.datos.hub = null;
    return siguiente(s, env);
  }
  let lugar;
  if (campo === "origen" && /^(ok|si|sí)$/i.test(texto)) {
    const encontrados = await buscarLugar(origenPorDefecto);
    lugar = encontrados.find((x) => x.code === origenPorDefecto) ||
      { code: origenPorDefecto, nombre: origenPorDefecto };
  } else if (s.opciones && /^[1-5]$/.test(texto) && s.opciones[+texto - 1]) {
    lugar = s.opciones[+texto - 1];
  } else {
    const encontrados = await buscarLugar(texto);
    if (!encontrados.length) {
      return "No encontre ese lugar. Prueba con otro nombre o con el codigo del aeropuerto (ej: MAD).";
    }
    const exacto = /^[A-Za-z]{3}$/.test(texto)
      ? encontrados.find((x) => x.code === texto.toUpperCase()) : null;
    if (exacto) lugar = exacto;
    else if (encontrados.length === 1) lugar = encontrados[0];
    else {
      s.opciones = encontrados;
      return "Cual de estos?\n" + encontrados.map((x, i) => `${i + 1}. ${x.nombre} (${x.code}) - ${x.detalle}`)
        .join("\n") + "\nResponde con el numero, o escribe otra busqueda.";
    }
  }
  const otros = LUGARES.filter((c) => c !== campo).map((c) => s.datos[c]);
  if (otros.includes(lugar.code)) {
    return `${lugar.nombre} (${lugar.code}) ya lo elegiste en otra respuesta. Dime otro lugar.`;
  }
  fijarLugar(s, campo, lugar);
  return siguiente(s, env);
}

async function guardarViaje(env, s) {
  const d = s.datos;
  const id0 = slug(`${d.destinoNombre}-${d.fecha.slice(0, 7)}`);
  return modificarViajes(env, `feat: add trip ${id0} from Telegram`, (datos) => {
    const ids = new Set(datos.viajes.map((v) => v.id));
    let id = id0, n = 2;
    while (ids.has(id)) id = `${id0}-${n++}`;
    datos.viajes.push({
      id, nombre: d.destinoNombre, origen: d.origen, destino: d.destino,
      ...(d.hub ? { hub: d.hub } : {}),
      salida_desde: d.fecha, flex_dias: d.flex, dur_min: d.durMin, dur_max: d.durMax,
      adultos: d.adultos,
      ...(d.presupuesto ? { umbral_por_persona_eur: d.presupuesto } : {}),
    });
    return id;
  });
}

async function avanzar(env, s, texto) {
  const d = s.datos;
  switch (s.paso) {
    case "destino": case "origen": case "hub":
      return pasoLugar(env, s, texto);
    case "fecha": {
      const dt = parseFecha(texto);
      if (!dt) return "No entendi la fecha. Usa el formato dia/mes/ano, por ejemplo 12/02/2027.";
      const dias = (dt - hoyUTC()) / 86400000;
      if (dias < 1) return "Esa fecha ya paso o es hoy. Dime una fecha futura.";
      if (dias > MAX_DIAS_ADELANTE) {
        return `Google Flights solo muestra vuelos hasta unos ${MAX_DIAS_ADELANTE} dias por delante. Elige una fecha mas cercana.`;
      }
      d.fecha = iso(dt);
      return siguiente(s, env);
    }
    case "flex": {
      if (!/^\d$/.test(texto) || +texto > 7) return "Escribe un numero del 0 al 7.";
      d.flex = +texto;
      return siguiente(s, env);
    }
    case "dur": {
      const r = parseDuracion(texto);
      if (!r) return "No entendi. Escribe un numero (ej: 21) o un rango (ej: 21-28).";
      const combos = (d.flex + 1) * (r.max - r.min + 1);
      if (combos > MAX_COMBINACIONES) {
        return `Son demasiadas combinaciones (${combos}, el maximo es ${MAX_COMBINACIONES}). Con ${d.flex} dias de flexibilidad el rango puede abarcar como mucho ${Math.floor(MAX_COMBINACIONES / (d.flex + 1))} duraciones distintas. Dime un rango mas corto.`;
      }
      d.durMin = r.min; d.durMax = r.max;
      return siguiente(s, env);
    }
    case "adultos": {
      if (!/^[1-9]$/.test(texto)) return "Escribe un numero del 1 al 9.";
      d.adultos = +texto;
      return siguiente(s, env);
    }
    case "presupuesto": {
      if (/^(no|ninguno|sin limite|-)$/i.test(texto)) d.presupuesto = null;
      else {
        const n = Number(texto.replace(/(eur|euros|€)/gi, "").replace(/\s/g, "").replace(",", "."));
        if (!Number.isFinite(n) || n < 20 || n > 20000) {
          return 'Escribe un numero entre 20 y 20000 (euros por persona), o "no".';
        }
        d.presupuesto = Math.round(n);
      }
      return siguiente(s, env);
    }
    case "confirmar": {
      if (/^(no|n)$/i.test(texto)) { s.cancelado = true; return "Cancelado. No guarde nada."; }
      if (!/^(si|sí|s|ok|dale)$/i.test(texto)) return 'Responde "si" para guardarlo o "no" para cancelar.';
      const id = await guardarViaje(env, s);
      s.cancelado = true; // fin de la conversacion
      return `Guardado (${id}). Empiezo a buscar ahora: en unos 5 a 10 minutos te llega el primer resultado, y despues reviso cada 6 horas. Te aviso cuando haya una oferta.`;
    }
  }
}

// ---------------------------------------------------------------- comandos

function listar(datos) {
  if (!datos.viajes.length) return "No tienes viajes. Crea uno con /viaje.";
  return "Tus viajes:\n\n" + datos.viajes.map((v, i) => {
    const p = v.umbral_por_persona_eur ? `, hasta ${v.umbral_por_persona_eur} EUR/persona` : "";
    return `${i + 1}. ${v.nombre || v.destino} (${v.origen} -> ${v.destino}${v.hub ? ` via ${v.hub}` : ""})\n` +
      `   salida desde ${v.salida_desde}, ${plural(v.adultos || 1, "adulto")}${p}\n   id: ${v.id}`;
  }).join("\n\n") + "\n\nPara borrar uno: /borrar <numero>";
}

async function comando(env, chat, texto) {
  const [cmd, ...resto] = texto.split(/\s+/);
  const arg = resto.join(" ").trim();
  switch (cmd.toLowerCase().replace(/@.*/, "")) {
    case "/start": case "/ayuda": case "/help":
      return `Soy tu agente de vuelos. Busco los precios mas baratos y te aviso por aqui.\n\n${AYUDA}`;
    case "/viaje": {
      const s = { paso: "destino", datos: {} };
      await guardarEstado(env, chat, s);
      return pregunta("destino", s, env);
    }
    case "/cancelar":
      await borrarEstado(env, chat);
      return "Cancelado.";
    case "/viajes":
      return listar((await leerViajes(env)).datos);
    case "/borrar": {
      if (!arg) return "Dime cual: /borrar <numero o id>. Mira los numeros con /viajes.";
      return modificarViajes(env, `chore: remove trip ${arg} from Telegram`, (datos) => {
        const i = /^\d+$/.test(arg) ? +arg - 1 : datos.viajes.findIndex((v) => v.id === arg);
        const v = datos.viajes[i];
        if (!v) return "No encontre ese viaje. Mira /viajes.";
        datos.viajes.splice(i, 1);
        return `Borrado: ${v.nombre || v.destino} (${v.id}).`;
      });
    }
    default:
      return `No conozco ese comando.\n\n${AYUDA}`;
  }
}

// ---------------------------------------------------------------- estado (KV)

const clave = (chat) => `chat:${chat}`;
const cargarEstado = (env, chat) => env.ESTADO.get(clave(chat), "json");
const guardarEstado = (env, chat, s) =>
  env.ESTADO.put(clave(chat), JSON.stringify(s), { expirationTtl: TTL_CONVERSACION });
const borrarEstado = (env, chat) => env.ESTADO.delete(clave(chat));

export async function manejarMensaje(env, chat, texto) {
  try {
    if (texto.startsWith("/")) return await enviar(env, chat, await comando(env, chat, texto));
    const s = await cargarEstado(env, chat);
    if (!s) return await enviar(env, chat, `Para empezar escribe /viaje.\n\n${AYUDA}`);
    const respuesta = await avanzar(env, s, texto);
    if (s.cancelado) await borrarEstado(env, chat);
    else await guardarEstado(env, chat, s);
    await enviar(env, chat, respuesta);
  } catch (e) {
    console.error(e);
    await enviar(env, chat, "No pude completar eso por un error temporal. Intentalo de nuevo en un momento.");
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("ok");
    // Solo Telegram conoce este secreto: evita que cualquiera llame al Worker.
    if (request.headers.get("x-telegram-bot-api-secret-token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json().catch(() => ({}));
    const msg = update.message;
    // Solo obedece a su dueno: cualquier otro chat se ignora en silencio.
    if (!msg?.text || String(msg.chat?.id) !== String(env.ALLOWED_CHAT_ID)) {
      return new Response("ok");
    }
    // Se responde a Telegram enseguida y el trabajo sigue en segundo plano: asi no reintenta.
    ctx.waitUntil(manejarMensaje(env, msg.chat.id, msg.text.trim()));
    return new Response("ok");
  },
};
