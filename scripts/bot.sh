#!/usr/bin/env bash
# Arranca el bot de Telegram del agente y lo relanza si se cae.
#   scripts/bot.sh            en primer plano (Ctrl+C para parar)
#   scripts/bot.sh --detach   en segundo plano; log en logs/bot.log
#   scripts/bot.sh --stop     para el que esté en segundo plano
set -euo pipefail
cd "$(dirname "$0")/.."
PIDFILE=logs/bot.pid
mkdir -p logs

case "${1:-}" in
  --stop)
    if [[ ! -f $PIDFILE ]] || ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "No había ningún bot en marcha"; rm -f "$PIDFILE"; exit 0
    fi
    # Al grupo entero (el bucle y el bot), y se espera a que muera de verdad:
    # dos bots leyendo a la vez dan 409 en Telegram.
    pid=$(cat "$PIDFILE")
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid"
    for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill -KILL -- "-$pid" 2>/dev/null
    rm -f "$PIDFILE"; echo "Bot parado"
    exit 0 ;;
  --detach)
    if [[ -f $PIDFILE ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Ya está en marcha (pid $(cat "$PIDFILE"))"; exit 0
    fi
    setsid nohup "$0" >>logs/bot.log 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    echo "Bot en segundo plano (pid $!). Log: logs/bot.log · Parar: scripts/bot.sh --stop"
    exit 0 ;;
esac

# El bot va en segundo plano y se espera con `wait`: así la señal de parada se
# atiende al momento, sin esperar a que acabe la consulta larga a Telegram.
child=""
trap '[[ -n $child ]] && kill "$child" 2>/dev/null; exit 0' INT TERM
while true; do
  echo "[$(date -Is)] arrancando bot"
  uv run mister-assistant bot &
  child=$!
  wait "$child" || true
  echo "[$(date -Is)] el bot ha terminado; relanzo en 15 s"
  sleep 15
done
