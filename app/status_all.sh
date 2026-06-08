#!/usr/bin/env bash
set -u

BASE_DIR="/opt/iptv/app"
PID_DIR="${BASE_DIR}/pids"

shopt -s nullglob
for pid_file in "${PID_DIR}"/*.pid; do
  channel="$(basename "$pid_file" .pid)"
  pid="$(cat "$pid_file" 2>/dev/null || true)"

  if [[ -z "$pid" ]]; then
    echo "$channel tiene PID vacío en $pid_file"
    rm -f "$pid_file"
    continue
  fi

  if kill -0 "$pid" 2>/dev/null; then
    elapsed=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
    echo "$channel está corriendo (PID $pid, uptime: ${elapsed:-desconocido})"
  else
    echo "$channel no está corriendo (PID $pid) -- eliminando archivo"
    rm -f "$pid_file"
  fi
done