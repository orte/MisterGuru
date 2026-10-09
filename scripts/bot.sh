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
    if [[ -f $PIDFILE ]] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Bot parado"; rm -f "$PIDFILE"
    else
      echo "No había ningún bot en marcha"
    fi
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

trap 'kill 0' INT TERM
while true; do
  echo "[$(date -Is)] arrancando bot"
  uv run mister-assistant bot || true
  echo "[$(date -Is)] el bot ha terminado; relanzo en 15 s"
  sleep 15
done
