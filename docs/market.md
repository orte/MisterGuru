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

    50 M + Σ jornadas (puntos × 100.000 € + (puesto − 1) × 200.000 €)
         + ventas − compras

Lo que no se ve (subidas de cláusula, otros ingresos o gastos) queda como error.
**Solo se usan si con el saldo propio fallan en menos de 3 M€**; si no, el
informe lo dice y se limita a listar tus cláusulas más bajas.

**Estado (2026-10-08): no fiable.** Con el saldo propio se sobreestima en
+29,5 M€ (1.034 traspasos reconstruidos desde el 7 de agosto). Hipótesis por
orden de ajuste:

1. **Los premios por jornada no se han ingresado** (27,8 M€ entre puntos y
   puesto): sin ellos el error baja a +1,7 M€, compatible con los pocos
   traspasos antiguos que no se han podido reconstruir.
2. Al vender por cláusula se cobra menos que la cláusula (si se cobrara solo el
   valor, el error quedaría en +7,5 M€).

Se resolverá solo cuando acabe la J8: los snapshots diarios dirán cuánto sube
el saldo al cerrar una jornada, descontados los traspasos.

## Ventas

Jugadores propios que no están en el once ni entre los 4 primeros suplentes,
y cuyo valor se espera que baje más de un 1 % en 7 días.

## Registro

Cada recomendación se guarda en `recommendations` (tipo, jugador, puntuación,
números que la justifican y mensaje). El campo `outcome` queda para la auditoría
de la Fase 5.
