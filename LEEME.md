# Agente de vuelos AQP - LIM - MAD

Revisa cada 6 horas los precios de Arequipa-Lima + Lima-Madrid (ida y vuelta, 21 a 28 dias,
salida desde el 12-feb-2027 con `flex_dias` de margen, 3 por defecto), los compara con un billete unico y te avisa por Telegram.

## Puesta en marcha (unos 15 minutos, todo gratis)
1. **Token de precios:** crea cuenta en travelpayouts.com y copia tu "API token".
2. **Bot de Telegram:** en Telegram habla con @BotFather, escribe /newbot y copia el token.
   Luego escribele cualquier mensaje a tu bot y abre
   https://api.telegram.org/bot<TU_TOKEN>/getUpdates para ver tu `chat id`.
3. **GitHub:** crea un repositorio privado y sube estos archivos (incluida la carpeta .github).
4. En el repositorio: Settings > Secrets and variables > Actions > New repository secret.
   Crea tres: `TRAVELPAYOUTS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
5. Pestaña Actions > "Agente de vuelos" > Run workflow para probarlo. Desde ahi corre solo.

## Ajustes
Edita el bloque CONFIG al inicio de `buscador_vuelos.py`: fechas, `flex_dias`, duracion, noches en Lima
(`noches_lima_opciones`, 0 = mismo dia), costo del hotel, umbral de aviso (`umbral_total_eur`).

## Probar en tu computadora
    export TRAVELPAYOUTS_TOKEN=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
    python buscador_vuelos.py
    python buscador_vuelos.py --test-telegram

## Limitaciones
- Los precios vienen del cache de Aviasales: son orientativos y pueden no estar ya disponibles.
- Si no hay dato exacto para AQP-LIM en esas fechas, se usa la mediana y se marca como estimado.
- Son billetes separados: no tienes proteccion de conexion en Lima.
