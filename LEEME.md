# Agente de vuelos AQP - LIM - MAD

Busca precios en vivo en Google Flights cada 6 horas para Arequipa-Lima + Lima-Madrid
(ida y vuelta, 21 a 28 dias, salida desde el 12-feb-2027 con `flex_dias` de margen, 3 por
defecto), los compara con un billete unico AQP-MAD y te avisa por Telegram cuando el mejor
precio baja de `umbral_total_eur` (980 EUR) o mejora un 5%.

Si Google no responde, usa como respaldo el cache de Travelpayouts (requiere token).

## Puesta en marcha (todo gratis)
1. **Bot de Telegram:** en Telegram habla con @BotFather, escribe /newbot y copia el token.
   Luego escribele cualquier mensaje a tu bot y abre
   https://api.telegram.org/bot<TU_TOKEN>/getUpdates para ver tu `chat id`.
2. **GitHub:** sube estos archivos a un repositorio (incluida la carpeta .github).
3. En el repositorio: Settings > Secrets and variables > Actions > New repository secret.
   Crea `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
   Opcional, para el respaldo: `TRAVELPAYOUTS_TOKEN` (Perfil > API token en travelpayouts.com).
4. Pestana Actions > "Agente de vuelos" > Run workflow para probarlo. Desde ahi corre solo.

## Ajustes
Edita el bloque CONFIG al inicio de `buscador_vuelos.py`: fechas, `flex_dias`, duracion,
noches en Lima (`noches_lima_opciones`, 0 = mismo dia), costo del hotel, `umbral_total_eur`.

## Probar en tu computadora
    pip install -r requirements.txt
    python buscador_vuelos.py
    python buscador_vuelos.py --test-telegram

Una busqueda completa hace unas 110 consultas y tarda 2 a 4 minutos.

## Como funciona
- `google_flights.py` lee Google Flights con un parser propio (la libreria `fast-flights`
  solo arma la consulta). Es una via **no oficial**: Google puede cambiar el formato o
  bloquear la IP, y en ese caso el agente cae al cache de Travelpayouts.
- El cliente HTTP a veces cierra el proceso cuando Google corta la conexion. Por eso la
  busqueda corre en un proceso hijo que guarda cada resultado en un cache en disco
  (vigencia 1 hora) y se relanza si se cae.

## Limitaciones
- Los precios en vivo pueden cambiar entre la busqueda y tu compra: confirma en el enlace.
- Solo se ve la oferta mas barata de cada consulta, no todas las alternativas.
- Son billetes separados: no tienes proteccion de conexion en Lima. Con 0 noches se
  verifica que la ida tenga 3 horas de margen; la vuelta no se puede verificar.
- El precio de AQP-MAD en un solo billete suele ser mucho mas caro que separar tramos.
