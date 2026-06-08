#!/usr/bin/env bash
set -u

BASE_DIR="/opt/iptv/app"
CHANNELS_DIR="${BASE_DIR}/channels"
LOG_DIR="${BASE_DIR}/logs"
PID_DIR="${BASE_DIR}/pids"
WATCHER="${BASE_DIR}/watcher.sh"

mkdir -p "$LOG_DIR" "$PID_DIR"

shopt -s nullglob
for cmd_file in "${CHANNELS_DIR}"/*.cmd; do
  channel="$(basename "$cmd_file" .cmd)"
  pid_file="${PID_DIR}/${channel}.pid"
  launcher_log="${LOG_DIR}/${channel}-launcher.log"

  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$channel ya está corriendo con PID $pid"
      continue
    else
      rm -f "$pid_file"
    fi
  fi

  echo "Lanzando watcher de $channel"
  nohup "$WATCHER" "$channel" >>"$launcher_log" 2>&1 &
  echo $! > "$pid_file"
  
  sleep 2  # Dar 2 segundos entre cada inicio
done
