#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="/opt/iptv/app"
PID_DIR="${SCRIPT_DIR}/pids"

# Función interna para reiniciar un canal individual
restart_single_channel() {
  local channel="$1"
  echo "[$(date '+%F %T')] ---> Reiniciando: ${channel}"

  # 1. Detener el canal
  if [[ -f "${SCRIPT_DIR}/stop.sh" ]]; then
    bash "${SCRIPT_DIR}/stop.sh" "$channel"
  else
    echo "ERROR: No se encontró stop.sh en ${SCRIPT_DIR}"
    return 1
  fi

  sleep 1.5

  # 2. Iniciar el watcher en segundo plano
  if [[ -f "${SCRIPT_DIR}/watcher.sh" ]]; then
    nohup bash "${SCRIPT_DIR}/watcher.sh" "$channel" > /dev/null 2>&1 &
    echo "¡Watcher de '${channel}' iniciado en segundo plano!"
  else
    echo "ERROR: No se encontró watcher.sh en ${SCRIPT_DIR}"
    return 1
  fi
}

# --- FLUJO PRINCIPAL ---

# OPCIÓN 1: Reiniciar un canal específico
if [[ $# -gt 0 ]]; then
  TARGET_CHANNEL="$1"
  echo "[$(date '+%F %T')] === INICIANDO REINICIO INDIVIDUAL ==="
  restart_single_channel "$TARGET_CHANNEL"

# OPCIÓN 2: Reiniciar TODOS los canales basados en los archivos .pid actuales
else
  echo "[$(date '+%F %T')] === INICIANDO REINICIO MASIVO (TODOS LOS CANALES) ==="
  
  shopt -s nullglob
  found=0

  # Escaneamos los PIDs que están corriendo actualmente
  for pid_file in "${PID_DIR}"/*.pid; do
    found=1
    channel_name="$(basename "$pid_file" .pid)"
    restart_single_channel "$channel_name"
    echo "--------------------------------------------------"
  done

  if [[ $found -eq 0 ]]; then
    echo "No se encontraron canales activos para reiniciar en $PID_DIR"
  fi
fi

echo "[$(date '+%F %T')] === PROCESO RESTART FINALIZADO ==="