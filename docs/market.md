# Mercado y cláusulas (Fase 4)

Código: `models/value.py`, `decide/market.py`, `decide/balances.py` y
`jobs/market_report.py`. Informe: `mister-assistant market-report`, cada mañana
en el workflow diario, después del snapshot (sin peticiones propias).

## Datos

- **`player_value_daily`**: valor de mercado por jugador y día.
  - Carga inicial con `backfill-values`: la ficha de cada jugador
    (`/ajax/sw/players`, una petición por jugador; Mister ignora el *slug*)
    trae la serie diaria del último año.
  - Después, `snapshot-daily` añade cada día el valor de los jugadores con
    dueño y de los del mercado.
- **Historial de traspasos**: el feed solo sirve unas tres semanas. La misma ficha
  trae `owners`, el historial completo del jugador en la liga (mismo espacio de
  ids que el feed), así que `backfill-values` reconstruye todos los traspasos
  desde el inicio de temporada. Los traspasos «de Mister» son compras al mercado
  del juego, y los que van «a Mister», ventas al juego.

## Modelo de valor (`models/value.py`)

Regresión lineal con regularización (ridge) de log(v[t+h]/v[t]), para h = 7 y
14 días, sobre la tendencia a 1, 3, 7 y 14 días, el logaritmo del valor y los
puntos recientes frente a su posición. Se valida dejando fuera los últimos
28 días. **Si no mejora a «el valor no cambia», se usa 0**: mejor no predecir
que predecir ruido. El informe dice cada día cuál de las dos se está usando.

### Resultado con datos reales (2026-10-08)

Datos: 496 jugadores, 126.695 valores diarios. Validación con los últimos 28 días:

| Horizonte | Error del modelo | «Sin cambio» | «Sigue la tendencia» | ¿Se usa? |
|---|---|---|---|---|
| 7 días | 0,149 | 0,184 | 0,163 | sí (19 % mejor) |
| 14 días | 0,293 | 0,318 | 0,358 | sí (8 % mejor) |

El error es el absoluto medio del logaritmo de la variación: los valores de
Mister se mueven ±15–30 % en una o dos semanas, así que las predicciones son
orientativas. Pesan sobre todo la tendencia a 3 y 7 días (inercia), el nivel
del valor (los caros suben menos) y los puntos recientes.

## Valoración de un fichaje (`decide/market.py`)

El dinero invertido en un jugador se recupera vendiéndolo a su valor (el
juego ofrece ±5 %). Por eso el coste real de un fichaje no es el precio,
sino lo que se paga por encima del valor más lo que se espera que pierda:

    puntuación = puntos extra en 5 jornadas
                 + (variación de valor a 14 días − sobreprecio − 2,5 % de venta) / 100.000 €

- **Puntos extra**: diferencia entre el mejor once con él y sin él (modelo v0 de
  la próxima jornada, multiplicada por el horizonte). Si no entra en el once, son 0.
- **100.000 € por punto**: la bonificación de la liga. Convierte el dinero en
  puntos al tipo que paga la propia liga. Es una elección conservadora:
  penaliza mucho pagar de más.
- **Caja**: si el precio supera el saldo, se avisa de cuánto hay que vender
  antes del inicio de la jornada (con saldo negativo al empezar, 0 puntos).

### Pujas

Tres niveles como múltiplo del valor, sacados de las compras ganadas al
mercado del juego en la liga (precio pagado / valor ese día): **ajustada**
(mediana), **probable** (percentil 75) y **segura** (percentil 90). Si hay menos
de 15 compras en el histórico, se usan 1,02 / 1,10 / 1,25. Con las 412 compras
de la liga (2026-10-08): **×1,04 / ×1,16 / ×1,41** sobre el valor. Cuando el jugador lo
vende un rival, se muestra su precio.

## Cláusulas

- **Ofensivas**: jugadores rivales cuya cláusula cabe en tu puja máxima,
  valorados igual que un fichaje con precio = cláusula. Una cláusula es como
  mínimo el valor + 50 %, así que solo compensa con jugadores que mejoran
  mucho el once.
- **Defensivas**: titulares propios cuya cláusula cabe en el gasto posible
  estimado de algún rival, con la pérdida de puntos si se lo llevan y la subida
  mínima que lo saca de su alcance. Coste de subir (§2.4): 20 % de la cláusula
  máxima (base × 3) / 3 por escalón, es decir, un 20 % de la base por escalón.

### Saldos rivales (`decide/balances.py`)

Ocultos en la liga (`show_balances = 0`). Se estiman así:

    50 M − valor de la plantilla inicial
         + Σ jornadas (puntos × 100.000 € + (puesto − 1) × 200.000 €)
         + ventas − compras

- **La plantilla inicial se paga**: los 15 jugadores iniciales se descuentan
  de los 50 M de partida. Un jugador es inicial si lo primero que se sabe de él
  es una venta de ese mánager, o si lo tiene ahora y nunca se ha movido. Su valor
  es el del día anterior al primer traspaso de la liga.
- Los iniciales vendidos sin dejar rastro (nunca jugaron ni volvieron a tener
  dueño) no se ven, pero su compra inicial y su venta se compensan casi del todo.
- Los mánagers sin plantilla (abandonaron la liga) no cuentan como amenaza.
- **Gasto posible de un rival** = saldo estimado + 25 % del valor de su equipo
  (la deuda que permite la liga). Es la hipótesis prudente: no está comprobado
  que un clausulazo pueda pagarse con deuda.

**Solo se usan si con el saldo propio fallan en menos de 3 M€.** Si no, el
informe lo dice y se limita a listar tus cláusulas más bajas.

**Validación (2026-10-08): error de −2,3 M€** con el saldo propio (estimado
2,0 M€, real 4,3 M€), con 12 de los 15 iniciales identificados. Sin descontar la
plantilla inicial el error era de +29,5 M€. Pendiente de comprobar al cerrar la
J8: que el premio por puesto va al revés (el 1º cobra 0) y si la J6, con un
partido aplazado, ya se pagó.

## Ventas

Jugadores propios que no están en el once ni entre los 4 primeros suplentes,
y cuyo valor se espera que baje más de un 1 % en 7 días.

## Registro

Cada recomendación se guarda en `recommendations` (tipo, jugador, puntuación,
números que la justifican y mensaje). El campo `outcome` queda para la auditoría
de la Fase 5.
