#!/usr/bin/env bash
set -u
set -o pipefail

BASE_DIR="/opt/iptv/app"
PID_DIR="${BASE_DIR}/pids"

kill_descendants() {
  local parent_pid="$1"
  local child
  for child in $(pgrep -P "$parent_pid" 2>/dev/null || true); do
    kill_descendants "$child"
    kill "$child" 2>/dev/null || true
    kill -9 "$child" 2>/dev/null || true
  done
}

shopt -s nullglob
found=0
for pid_file in "${PID_DIR}"/*.pid; do
  found=1
  channel="$(basename "$pid_file" .pid)"
  pid="$(cat "$pid_file" 2>/dev/null || true)"

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Deteniendo watcher de $channel (PID $pid)"
    kill_descendants "$pid"
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill_descendants "$pid"
    kill -9 "$pid" 2>/dev/null || true
  else
    echo "$channel ya no estaba corriendo"
    if [[ -n "$pid" ]]; then
      kill_descendants "$pid"
    fi
  fi

  rm -f "$pid_file"
done

if [[ $found -eq 0 ]]; then
  echo "No se encontraron archivos PID en $PID_DIR"
fi