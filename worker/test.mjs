// Prueba la conversacion completa con Telegram, GitHub, KV y el autocompletado simulados.
// Uso: node worker/test.mjs
import worker, { manejarMensaje, parseFecha, parseDuracion, slug } from "./src/index.js";

let fallos = 0;
const check = (nombre, cond) => {
  if (!cond) fallos++;
  console.log(cond ? "OK  " : "FALLA", nombre);
};

// ---- simulacros
const kvDatos = new Map();
const env = {
  TELEGRAM_BOT_TOKEN: "tg", GITHUB_TOKEN: "gh", GITHUB_REPO: "u/r", DEFAULT_ORIGIN: "AQP",
  WEBHOOK_SECRET: "sec", ALLOWED_CHAT_ID: "42",
  ESTADO: {
    get: async (k) => (kvDatos.has(k) ? JSON.parse(kvDatos.get(k)) : null),
    put: async (k, v) => void kvDatos.set(k, v),
    delete: async (k) => void kvDatos.delete(k),
  },
};
let archivo = { viajes: [] };          // viajes.json simulado
let sha = 0, puts = 0, forzarConflicto = 0;
const mensajes = [];
const lugares = {
  madrid: [{ code: "MAD", name: "Madrid", type: "city", country_name: "España" },
    { code: "MAD", name: "Adolfo Suárez Madrid-Barajas", type: "airport", city_name: "Madrid" }],
  lima: [{ code: "LIM", name: "Lima", type: "city", country_name: "Perú" }],
  aqp: [{ code: "AQP", name: "Arequipa", type: "city", country_name: "Perú" }],
  par: [{ code: "PAR", name: "París", type: "city", country_name: "Francia" },
    { code: "PAR", name: "París", type: "city", country_name: "Francia" },
    { code: "PRN", name: "Parma", type: "city", country_name: "Italia" }],
  paris: [{ code: "PAR", name: "París", type: "city", country_name: "Francia" },
    { code: "CDG", name: "Charles de Gaulle", type: "airport", city_name: "París", country_name: "Francia" }],
  xyz: [],
};
globalThis.fetch = async (url, opt = {}) => {
  url = String(url);
  const json = (o, status = 200) => new Response(JSON.stringify(o), { status });
  if (url.includes("api.telegram.org")) {
    mensajes.push(JSON.parse(opt.body).text);
    return json({ ok: true });
  }
  if (url.includes("autocomplete.travelpayouts.com")) {
    const t = decodeURIComponent(url.match(/term=([^&]*)/)[1]).toLowerCase();
    return json(lugares[t] ?? []);
  }
  if (url.includes("api.github.com")) {
    if (opt.method === "PUT") {
      const body = JSON.parse(opt.body);
      if (forzarConflicto > 0) { forzarConflicto--; return json({}, 409); }
      if (body.sha !== undefined && body.sha !== `s${sha}`) return json({}, 409);
      archivo = JSON.parse(new TextDecoder().decode(
        Uint8Array.from(atob(body.content), (c) => c.charCodeAt(0))));
      sha++; puts++;
      return json({ ok: true });
    }
    const content = btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(archivo))));
    return json({ content, sha: `s${sha}` });
  }
  throw new Error("fetch inesperado " + url);
};

async function di(texto) {
  mensajes.length = 0;
  await manejarMensaje(env, 42, texto);
  return mensajes.join("\n");
}
const dias = (n) => new Date(Date.now() + n * 86400000);
const ddmmyyyy = (dt) => `${String(dt.getUTCDate()).padStart(2, "0")}/${String(dt.getUTCMonth() + 1).padStart(2, "0")}/${dt.getUTCFullYear()}`;

// ---- utilidades puras
check("parseFecha acepta dd/mm/aaaa y aaaa-mm-dd",
  parseFecha("12/02/2027")?.toISOString().startsWith("2027-02-12") &&
  parseFecha("2027-2-12")?.toISOString().startsWith("2027-02-12"));
check("parseFecha rechaza 31/02 y texto", parseFecha("31/02/2027") === null && parseFecha("mañana") === null);
check("parseDuracion", JSON.stringify(parseDuracion("21-28")) === '{"min":21,"max":28}' &&
  JSON.stringify(parseDuracion("14")) === '{"min":14,"max":14}' && parseDuracion("30-10") === null && parseDuracion("x") === null);
check("slug quita acentos", slug("París-2027-02") === "paris-2027-02");

// ---- conversacion completa con ciudades ambiguas y errores del usuario
let r = await di("hola");
check("sin /viaje: indica como empezar", r.includes("/viaje"));
r = await di("/viaje");
check("/viaje pregunta el destino", r.includes("A donde quieres viajar"));
r = await di("xyz");
check("lugar desconocido: reintenta", r.includes("No encontre"));
r = await di("paris");
check("ciudad con varias opciones: las lista", r.includes("1. París (PAR)") && r.includes("2. París (CDG)"));
r = await di("7");
check("numero fuera de la lista se toma como nueva busqueda", r.includes("No encontre"));
r = await di("2");
check("elige la opcion 2 (CDG) y pregunta el origen", r.includes('Escribe "ok" para AQP'));
await di("/viaje");
r = await di("madrid");
check("ciudad y aeropuerto con el mismo codigo se fusionan: no pregunta", r.includes('Escribe "ok" para AQP'));
r = await di("ok");
check("ok = origen por defecto, pregunta la fecha", r.includes("Desde que fecha"));
r = await di("ayer");
check("fecha invalida", r.includes("No entendi la fecha"));
r = await di("01/01/2020");
check("fecha pasada", r.includes("ya paso"));
r = await di(ddmmyyyy(dias(800)));
check("fecha demasiado lejana", r.includes("solo muestra vuelos"));
const salida = dias(120);
r = await di(ddmmyyyy(salida));
check("fecha valida, pregunta flexibilidad", r.includes("flexibilidad"));
r = await di("9");
check("flexibilidad fuera de rango", r.includes("0 al 7"));
r = await di("3");
check("flex 3, pregunta duracion", r.includes("dura el viaje"));
r = await di("1-30");
check("rango de duracion demasiado amplio se rechaza", r.includes("demasiadas combinaciones"));
r = await di("21-28");
check("duracion valida (4x8=32), pregunta personas", r.includes("Cuantas personas"));
r = await di("0");
check("personas invalido", r.includes("1 al 9"));
r = await di("3");
check("3 personas, pregunta presupuesto", r.includes("POR PERSONA"));
r = await di("barato");
check("presupuesto invalido", r.includes("entre 20 y 20000"));
r = await di("980 eur");
check("presupuesto 980 eur, pregunta escala", r.includes("escala"));
r = await di("madrid");
check("escala no puede repetir el destino (mismo lugar)", r.includes("Cual de estos") || r.includes("ya lo elegiste"));
r = await di("lima");
check("escala Lima, pide confirmar con resumen", r.includes("Destino: Madrid (MAD)") && r.includes("Personas: 3 adultos") && r.includes("980 EUR por persona") && r.includes("Lima (LIM)"));
r = await di("quizas");
check("confirmacion ambigua", r.includes('"si"'));
forzarConflicto = 1;
r = await di("si");
check("guarda y sobrevive a un conflicto 409 de GitHub", r.includes("Guardado") && puts === 1);
check("con presupuesto: avisa que solo escribe si baja de el (no promete resultado)", r.includes("980 EUR por persona") && !r.includes("primer resultado"));
const v = archivo.viajes[0];
check("viajes.json: campos correctos", v && v.destino === "MAD" && v.origen === "AQP" && v.hub === "LIM" && v.adultos === 3 &&
  v.umbral_por_persona_eur === 980 && v.flex_dias === 3 && v.dur_min === 21 && v.dur_max === 28 &&
  v.salida_desde === salida.toISOString().slice(0, 10) && v.id.startsWith("madrid-") && v.nombre === "Madrid");
check("la conversacion termino", !kvDatos.has("chat:42"));

// ---- segundo viaje: sin escala, sin presupuesto, mismo id base -> sufijo
await di("/viaje"); await di("madrid"); await di("1"); await di("ok");
await di(ddmmyyyy(salida)); await di("0"); await di("7"); await di("1"); await di("no"); r = await di("no");
check("sin escala ni presupuesto: resumen", r.includes("sin limite") && r.includes("ninguna") && r.includes("Duracion: 7 dias") && r.includes("1 adulto\n"));
r = await di("si");
check("sin presupuesto: promete el primer resultado", r.includes("primer resultado"));
const v2 = archivo.viajes[1];
check("id repetido recibe sufijo y omite hub/umbral", v2.id === v.id + "-2" && !("hub" in v2) && !("umbral_por_persona_eur" in v2));

// ---- listar y borrar
r = await di("/viajes");
check("/viajes lista los dos", r.includes("1. Madrid") && r.includes("2. Madrid") && r.includes("via LIM"));
const putsAntes = puts;
r = await di("/borrar 9");
check("/borrar inexistente no escribe en GitHub", r.includes("No encontre") && puts === putsAntes);
r = await di("/borrar 2");
check("/borrar 2", r.includes("Borrado") && archivo.viajes.length === 1);
r = await di("/borrar " + archivo.viajes[0].id);
check("/borrar por id", archivo.viajes.length === 0);
r = await di("/viajes");
check("lista vacia", r.includes("No tienes viajes"));

// ---- /cancelar a mitad de conversacion
await di("/viaje"); r = await di("/cancelar");
check("/cancelar borra la conversacion", r.includes("Cancelado") && !kvDatos.has("chat:42"));

// ---- seguridad en el punto de entrada
const post = (body, secreto) => worker.fetch(new Request("https://w/", {
  method: "POST", body: JSON.stringify(body),
  headers: secreto === undefined ? {} : { "x-telegram-bot-api-secret-token": secreto },
}), env, { waitUntil: (p) => p });
mensajes.length = 0;
let resp = await post({ message: { text: "/viajes", chat: { id: 42 } } });
check("sin secreto de webhook: 403", resp.status === 403 && mensajes.length === 0);
resp = await post({ message: { text: "/viajes", chat: { id: 42 } } }, "otro");
check("secreto incorrecto: 403", resp.status === 403);
resp = await post({ message: { text: "/viaje", chat: { id: 999 } } }, "sec");
await new Promise((r) => setTimeout(r, 20));
check("chat ajeno: se ignora en silencio (200, sin respuesta, sin estado)", resp.status === 200 && mensajes.length === 0 && !kvDatos.has("chat:999"));
resp = await post({ message: { text: "/viajes", chat: { id: 42 } } }, "sec");
await new Promise((r) => setTimeout(r, 20));
check("dueno con secreto: responde", resp.status === 200 && mensajes.join("").includes("No tienes viajes"));
resp = await worker.fetch(new Request("https://w/", { method: "GET" }), env, {});
check("GET no hace nada", resp.status === 200);

console.log(fallos ? `\n${fallos} FALLOS` : "\nTODO OK");
process.exit(fallos ? 1 : 0);
