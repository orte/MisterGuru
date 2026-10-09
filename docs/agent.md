# Agente conversacional (Fase 6)

Código: `agent/` (`context.py`, `tools.py`, `runner.py`, `traceability.py`,
`evals.py`) y `delivery/telegram_bot.py`.

| Comando | Qué hace |
|---|---|
| `mister-assistant ask "¿A quién siento?"` | Una pregunta desde la terminal |
| `mister-assistant bot` | El agente por Telegram: un proceso que se queda escuchando |
| `mister-assistant agent-eval` | Casos de prueba con el modelo real (cuesta dinero de la API) |

Necesita `ANTHROPIC_API_KEY` (console.anthropic.com → API Keys). Es una
facturación por uso, aparte de la suscripción de Claude.

## Cómo funciona

- **Modelo**: Claude Opus 5.5 (`claude-opus-5-5`), esfuerzo `medium`, con el
  reintento del servidor (`fallbacks: "default"`) por si el modelo declina una
  petición. Bucle propio de herramientas (no el *tool runner*, que es beta).
- **Historial solo-añadir**: cada respuesta se guarda tal cual, incluido el
  razonamiento; nunca se edita. Pasados 40 mensajes, o con `/nuevo`, empieza
  una conversación nueva.
- **Caché**: el sistema y las herramientas son estables y van cacheados.
- **Trazabilidad**: todas las cifras de puntos salen de una ejecución guardada
  en `predictions` (el agente crea una con el disparador `agente` si no hay
  ninguna de menos de 12 h para la próxima jornada) y la respuesta cita su
  `run_id`. Cada respuesta pasa por `traceability.untraceable()`, que busca sus
  cifras («4,3 M€», «66 %», «+1,5 pts»…) entre las de las herramientas; si alguna
  no aparece, el bot lo avisa al final del mensaje y queda en `agent_log`.

## Herramientas (solo lectura sobre la BD; ninguna toca Mister)

| Herramienta | Para |
|---|---|
| `buscar_jugador` | Pasar de un nombre a su id |
| `ficha_jugador` | Puntos esperados, P(jugar), valor y su previsión, dueño, cláusula, últimas jornadas, noticias |
| `comparar_jugadores` | 2 a 6 jugadores lado a lado |
| `once_recomendado` | «¿A quién siento?»: once, dudas con recambio y banquillo |
| `simular_once` | Puntos de un once concreto y si la formación es válida |
| `valorar_fichaje` | «¿Pujo por Y y cuánto?»: puntos extra, sobreprecio, pujas y caja |
| `analizar_venta` | «¿Vendo a X?»: lo que pierde el once y la previsión de valor |
| `estado_rivales` | Clasificación y saldos estimados de los rivales |
| `mercado_hoy` | El mercado del día por valoración |
| `registrar_ajuste_disponibilidad` | Triaje de noticias (la única escritura, en nuestra BD) |
| `ajustes_vigentes` | Ajustes por noticias en vigor |

## Triaje de noticias

Pega en el chat una rueda de prensa o un parte médico, con su fuente y su fecha.
El agente identifica a los jugadores, extrae para cada uno el estado (`baja`,
`duda` o `disponible`), hasta cuándo, la fuente, la fecha y la frase literal, y
lo registra en `availability_overrides`. Si falta la fuente o la fecha, pregunta
antes. El modelo de minutos lo aplica desde la siguiente ejecución:

- `baja`: no juega.
- `duda`: P(jugar) limitada a la que dé la noticia, o a la mitad si no da ninguna.
- `disponible`: anula una baja de Fútbol Fantasy.

## Dónde corre el bot

En el PC de Jon (WSL), con `scripts/bot.sh`:

| Comando | Qué hace |
|---|---|
| `scripts/bot.sh --detach` | Lo arranca en segundo plano; log en `logs/bot.log` |
| `scripts/bot.sh --stop` | Lo para |
| `scripts/bot.sh` | En primer plano (Ctrl+C para parar) |

Si se cae, el script lo relanza a los 15 s. Funciona por *long polling*
(`getUpdates`): no hace falta servidor web ni abrir puertos. Solo responde al chat
de `TELEGRAM_CHAT_ID`; los demás se ignoran y quedan en el log.

Solo responde mientras el PC está encendido y WSL en marcha: si Windows se
suspende o se cierra WSL, hay que volver a lanzarlo. Para que responda siempre,
habría que llevarlo a un servidor pequeño siempre encendido (PLAN §8).

## Casos de prueba con el modelo real (`agent-eval`)

Resultado (2026-10-09): **5/5 correctos, 0,16 $ en total**.

| Pregunta | Herramientas | Resultado |
|---|---|---|
| ¿A quién siento esta jornada? | once_recomendado | ✓ |
| ¿Vendo a Antony? | buscar_jugador, analizar_venta, ficha_jugador | ✓ |
| ¿Pujo por Pedri y cuánto? | buscar_jugador, valorar_fichaje, ficha_jugador | ✓ |
| ¿Quién me puede clausular a algún titular? | estado_rivales, once_recomendado, ficha_jugador | ✓ |
| ¿Qué es mejor esta jornada, Antony o Pedri? | buscar_jugador, comparar_jugadores, once_recomendado | ✓ |

La primera pasada dio 4/5. Fallaba «¿Vendo a Antony?» porque el modelo hacía sus
propias cuentas (puntos × 100.000 € y pérdidas de valor en euros), que no salían
de ninguna herramienta. Ahora las herramientas dan esas cuentas hechas y el
prompt le prohíbe calcular por su cuenta. También daba la hora del primer
partido en UTC (19:00 en lugar de 21:00): ahora las herramientas la dan en hora
de Madrid.
