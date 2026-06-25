#!/usr/bin/env bash
set -u
set -o pipefail

BASE_DIR="/opt/iptv/app"
PID_DIR="${BASE_DIR}/pids"

# Función recursiva para matar procesos hijos
kill_descendants() {
  local parent_pid="$1"
  local child
  for child in $(pgrep -P "$parent_pid" 2>/dev/null || true); do
    kill_descendants "$child"
    kill "$child" 2>/dev/null || true
    kill -9 "$child" 2>/dev/null || true
  done
}

# Función específica para detener un PID
stop_pid_file() {
  local pid_file="$1"
  local channel
  local pid
  
  channel="$(basename "$pid_file" .pid)"
  pid="$(cat "$pid_file" 2>/dev/null || true)"

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Deteniendo watcher de $channel (PID $pid)..."
    kill_descendants "$pid"
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill_descendants "$pid"
    kill -9 "$pid" 2>/dev/null || true
  else
    echo "El canal '$channel' no estaba corriendo (o no se encontró el proceso $pid)."
    if [[ -n "$pid" ]]; then
      kill_descendants "$pid"
    fi
  fi

  rm -f "$pid_file"
}

# --- FLUJO PRINCIPAL ---

# Si se pasa un parámetro (ej: ./stop.sh telefe)
if [[ $# -gt 0 ]]; then
  TARGET_CHANNEL="$1"
  PID_FILE="${PID_DIR}/${TARGET_CHANNEL}.pid"

  if [[ -f "$PID_FILE" ]]; then
    stop_pid_file "$PID_FILE"
  else
    echo "Error: No se encontró el archivo PID para el canal '$TARGET_CHANNEL' en $PID_DIR"
    exit 1
  fi

# Si NO se pasa parámetro, procesa TODOS los archivos .pid de la carpeta
else
  echo "No se especificó ningún canal. Deteniendo TODOS los procesos..."
  shopt -s nullglob
  found=0
  
  for pid_file in "${PID_DIR}"/*.pid; do
    found=1
    stop_pid_file "$pid_file"
  done

  if [[ $found -eq 0 ]]; then
    echo "No se encontraron archivos PID activos en $PID_DIR"
  fi
fi