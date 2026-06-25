#!/usr/bin/env bash
set -u
set -o pipefail
if [[ $# -lt 1 ]]; then
  echo "Uso: $0 <nombre_canal>"
  exit 1
fi
CHANNEL_NAME="$1"
# =====================================================================

# PROTECCIÓN MULTI-INSTANCIA (LOCK)

# =====================================================================

LOCK_FILE="/tmp/watcher_${CHANNEL_NAME}.lock"

exec 9>"$LOCK_FILE"

if ! flock -n 9; then

  echo "[$(date '+%F %T')] [${CHANNEL_NAME}] ERROR: Ya hay un watcher activo para este canal. Abortando."

  exit 1

fi



BASE_DIR="/opt/iptv/app"

CHANNELS_DIR="${BASE_DIR}/channels"

LOG_DIR="${BASE_DIR}/logs"



# =====================================================================

# RUTAS SYMLINK SWAP (Arquitectura Cero Cortes)

# =====================================================================

OUT_ROOT="/mnt/hls-ram"

CHANNEL_LINK="${OUT_ROOT}/${CHANNEL_NAME}"                 # Lo que lee el servidor web/usuarios

CHANNEL_DIR_LIVE="${OUT_ROOT}/${CHANNEL_NAME}_live"        # Área de trabajo exclusiva de Streamlink

CHANNEL_DIR_FALLBACK="${OUT_ROOT}/${CHANNEL_NAME}_fallback" # Área de trabajo exclusiva de FFmpeg Offline



# Variables para que las funciones de monitoreo miren SIEMPRE al Live

CHANNEL_DIR="${CHANNEL_DIR_LIVE}"

PLAYLIST="${CHANNEL_DIR}/index.m3u8"

SEGMENT_PATTERN="${CHANNEL_DIR}/%06d.ts"



CMD_FILE="${CHANNELS_DIR}/${CHANNEL_NAME}.cmd"

RUN_LOG="${LOG_DIR}/${CHANNEL_NAME}-run.log"

WATCH_LOG="${LOG_DIR}/${CHANNEL_NAME}-watch.log"



VIDEO_OFFLINE="http://localhost:8097/error/offline.m3u8"



STARTUP_WAIT=20

CHECK_INTERVAL=10

PLAYLIST_MAX_AGE=20

RESTART_DELAY=5

FORCE_RESTART_EVERY=0

MAX_FAILURES=3  



LAST_SEGMENT_NAME=""

LAST_SEGMENT_SIZE=""

LAST_SEGMENT_STUCK_COUNT=0

FREEZE_THRESHOLD=3



mkdir -p "$LOG_DIR"



# =====================================================================

# FUNCIONES AUXILIARES

# =====================================================================



log() {

  printf '[%s] [%s] %s\n' "$(date '+%F %T')" "$CHANNEL_NAME" "$1" | tee -a "$WATCH_LOG"

}



atomic_symlink_swap() {
  local target_dir="$1"
  local tmp_link="${CHANNEL_LINK}.tmp.$$"
  
  # SALVAVIDAS: Si por algún error CHANNEL_LINK es un directorio real y no un symlink,
  # lo destruimos para que no atrape nuestros enlaces temporales.
  if [[ -d "$CHANNEL_LINK" && ! -L "$CHANNEL_LINK" ]]; then
    log "ADVERTENCIA: $CHANNEL_LINK era un directorio real. Destruyéndolo para restaurar symlink..."
    rm -rf "$CHANNEL_LINK"
  fi
  
  # Creamos un enlace temporal
  ln -sfn "$target_dir" "$tmp_link"
  
  # Renombramos atómicamente. 
  # -T le dice a mv: "Trata el destino como un archivo, NO lo metas en una carpeta"
  mv -Tf "$tmp_link" "$CHANNEL_LINK"
}



clean_channel_dir() {

  # Solo limpiamos la zona live de forma segura

  rm -f "${CHANNEL_DIR_LIVE}"/* 2>/dev/null || true

}



clean_fallback_dir() {

  # Limpiamos la zona fallback completamente

  rm -f "${CHANNEL_DIR_FALLBACK}"/* 2>/dev/null || true

}



reset_freeze_state() {

  LAST_SEGMENT_NAME=""

  LAST_SEGMENT_SIZE=""

  LAST_SEGMENT_STUCK_COUNT=0

}



get_playlist_age() {

  if [[ ! -f "$PLAYLIST" ]]; then

    echo 999999

    return

  fi

  local now mtime

  now=$(date +%s)

  mtime=$(stat -c %Y "$PLAYLIST" 2>/dev/null || echo 0)

  echo $((now - mtime))

}



pipeline_alive() {

  [[ -n "${PIPE_PID:-}" ]] && kill -0 "$PIPE_PID" 2>/dev/null

}



kill_pipeline() {

  if [[ -n "${PIPE_PID:-}" ]]; then

    log "Matando grupo de procesos del pipeline (PGID=${PIPE_PID})"

    # Matar todo el grupo de procesos

    kill -TERM -"$PIPE_PID" 2>/dev/null || true

    sleep 1

    # Forzar kill si sigue vivo

    kill -KILL -"$PIPE_PID" 2>/dev/null || true

    sleep 0.5

    PIPE_PID=""

  fi

}



kill_fallback() {

  if [[ -n "${FALLBACK_PID:-}" ]]; then

    log "Cortando video offline (PID=${FALLBACK_PID})"

    # Matar el proceso y sus hijos

    kill -TERM "$FALLBACK_PID" 2>/dev/null || true

    sleep 1

    kill -KILL "$FALLBACK_PID" 2>/dev/null || true

    wait "$FALLBACK_PID" 2>/dev/null || true

    FALLBACK_PID=""

  fi

}



# Función de limpieza unificada

cleanup() {

  # Desactivar traps para evitar bucles

  trap - EXIT INT TERM

  

  log "Saliendo, matando procesos..."

  kill_pipeline

  kill_fallback

  

  # Limpiar lock

  exec 9>&-

  rm -f "$LOCK_FILE"

  

  exit 0

}



# Un solo trap que lo maneja todo

trap cleanup EXIT INT TERM



log_tail() {

  [[ -f "$RUN_LOG" ]] || return 0

  log "Últimas 20 líneas del log:"

  tail -n 20 "$RUN_LOG" | tee -a "$WATCH_LOG" >/dev/null

}



log_has_pattern() {

  local pattern="$1"

  [[ -f "$RUN_LOG" ]] || return 1

  grep -Eqi "$pattern" "$RUN_LOG"

}



should_restart_due_to_log() {

  log_has_pattern 'Read timeout|Error while reading from substream|Stream ended|Broken pipe|Conversion failed|Immediate exit requested' && return 0

  log_has_pattern 'No such file or directory' && return 0

  log_has_pattern 'Codec AVOption b .* has not been used' && return 0

  return 1

}



detect_freeze_basic() {

  # Verificar si hay segmentos en el directorio

  local latest_segment

  latest_segment="$(ls -1t "${CHANNEL_DIR_LIVE}"/*.ts 2>/dev/null | head -n 1)"



  # Si no hay segmentos pero la playlist existe, podría ser un problema

  if [[ -z "$latest_segment" ]]; then

    if [[ -f "$PLAYLIST" ]]; then

      log "Playlist existe pero no hay segmentos .ts - posible freeze"

      return 0

    fi

    return 1

  fi



  local seg_name seg_size

  seg_name="$(basename "$latest_segment")"

  seg_size="$(stat -c %s "$latest_segment" 2>/dev/null || echo 0)"



  if [[ "$seg_name" == "$LAST_SEGMENT_NAME" && "$seg_size" == "$LAST_SEGMENT_SIZE" ]]; then

    LAST_SEGMENT_STUCK_COUNT=$((LAST_SEGMENT_STUCK_COUNT + 1))

    log "Segmento ${seg_name} no cambia (${LAST_SEGMENT_STUCK_COUNT}/${FREEZE_THRESHOLD})"

  else

    LAST_SEGMENT_NAME="$seg_name"

    LAST_SEGMENT_SIZE="$seg_size"

    LAST_SEGMENT_STUCK_COUNT=0

  fi



  if (( LAST_SEGMENT_STUCK_COUNT >= FREEZE_THRESHOLD )); then

    return 0

  fi



  return 1

}



check_stream_integrity() {

  local streams_info

  streams_info=$(timeout 5 ffprobe -v error -show_entries stream=codec_type -of default=noprint_wrappers=1:nokey=1 "$PLAYLIST" 2>/dev/null)



  # Si timeout mató ffprobe o hubo error

  if [[ $? -ne 0 || -z "$streams_info" ]]; then

    log "ffprobe falló o timeout al analizar playlist"

    return 1

  fi



  if [[ ! "$streams_info" =~ "video" ]]; then

    log "Falta VIDEO en el stream"

    return 1

  fi

  

  if [[ ! "$streams_info" =~ "audio" ]]; then

    log "Falta AUDIO en el stream"

    return 1

  fi



  return 0

}



fallback_offline() {

  local channel="$1"

  log "Emitiendo video offline aislado para ${channel}"



  mkdir -p "$CHANNEL_DIR_FALLBACK"

  clean_fallback_dir



  # FFmpeg escribe directo al Fallback

  ffmpeg -re -stream_loop -1 -i "$VIDEO_OFFLINE" \
    -c copy \
    -f hls \
    -hls_time 2 \
    -hls_list_size 10 \
    -hls_flags delete_segments \
    -hls_segment_filename "${CHANNEL_DIR_FALLBACK}/%06d.ts" \
    "${CHANNEL_DIR_FALLBACK}/index.m3u8" >/dev/null 2>&1 &
  FALLBACK_PID=$!



  # SWAP atómico: Movemos el symlink sin cortes

  atomic_symlink_swap "$CHANNEL_DIR_FALLBACK"

}



build_command() {

  local template_file="$1"

  local segment_path="$2"

  local playlist_path="$3"



  if [[ ! -f "$template_file" ]]; then

    log "Error: No se encuentra el archivo de comando $template_file"

    return 1

  fi



  sed -e "s|__SEGMENT_PATTERN__|$segment_path|g" -e "s|__PLAYLIST__|$playlist_path|g" "$template_file"

}



start_pipeline() {
  mkdir -p "$CHANNEL_DIR_LIVE"
  clean_channel_dir
  reset_freeze_state

  local cmd
  if ! cmd="$(build_command "$CMD_FILE" "${CHANNEL_DIR_LIVE}/%06d.ts" "${CHANNEL_DIR_LIVE}/index.m3u8")"; then
    log "Error al construir el comando de Streamlink"
    return 1
  fi

  log "Iniciando pipeline en carpeta Live aislada..."
  
  # Limpiar/crear archivo de log
  : > "$RUN_LOG"
  
  # Iniciar el pipeline en background
  setsid bash -c "set -o pipefail; $cmd" >>"$RUN_LOG" 2>&1 &
  PIPE_PID=$!
  
  # ✅ Inicializar START_TIME para el pipeline actual
  START_TIME=$(date +%s)
  
  log "Pipeline iniciado con PID=${PIPE_PID}, contador de uptime reiniciado"
}



initial_check() {

  log "Esperando ${STARTUP_WAIT}s para validación inicial"

  sleep "$STARTUP_WAIT"



  if ! pipeline_alive; then

    log "El proceso murió durante el arranque"

    log_tail

    return 1

  fi



  if [[ ! -f "$PLAYLIST" ]]; then

    log "No apareció la playlist"

    log_tail

    return 1

  fi



  local age

  age="$(get_playlist_age)"

  if (( age > PLAYLIST_MAX_AGE )); then

    log "La playlist está demasiado vieja al arrancar (${age}s)"

    log_tail

    return 1

  fi



  # Primera verificación de integridad

  if ! check_stream_integrity; then

    log "Fallo en verificación inicial de audio/video, rechecando en 5s..."

    sleep 5

    if ! check_stream_integrity; then

      log "Confirmado: fallo en integridad de stream inicial"

      log_tail

      return 1

    fi

  fi



  if should_restart_due_to_log; then

    log "Se detectó warning/error relevante en el log"

    log_tail

    return 1

  fi



  return 0

}



health_loop() {

  while true; do

    sleep "$CHECK_INTERVAL"



    if ! pipeline_alive; then

      log "El pipeline terminó"

      log_tail

      return 1

    fi



    if [[ ! -f "$PLAYLIST" ]]; then

      log "La playlist desapareció"

      log_tail

      return 1

    fi



    local age

    age="$(get_playlist_age)"

    if (( age > PLAYLIST_MAX_AGE )); then

      log "La playlist no se actualiza hace ${age}s"

      log_tail

      return 1

    fi



    if detect_freeze_basic; then

      log "Posible FREEZE detectado"

      log_tail

      return 1

    fi



    # Verificación periódica de integridad

    if ! check_stream_integrity; then

      log "Se perdió audio o video en ejecución; rechecando en 5s..."

      sleep 5

      if ! check_stream_integrity; then

        log "Confirmado: se perdió integridad del stream"

        return 1

      fi

    fi



    if should_restart_due_to_log; then

      log "Se detectó warning/error relevante en log"

      log_tail

      return 1

    fi



    # Forzar reinicio periódico si está configurado

    if (( FORCE_RESTART_EVERY > 0 )); then

      local uptime

      uptime=$(($(date +%s) - START_TIME))

      if (( uptime >= FORCE_RESTART_EVERY )); then

        log "Reinicio programado después de ${FORCE_RESTART_EVERY}s de uptime"

        return 1

      fi

    fi

  done

}

# =====================================================================
# PROGRAMA PRINCIPAL
# =====================================================================

log "===== watcher iniciado ====="

failure_count=0
# ❌ ELIMINADO: START_TIME=$(date +%s)  # Ya no se inicializa aquí

while true; do
  # Intentar iniciar pipeline
  if ! start_pipeline; then
    log "No se pudo iniciar el pipeline"
    failure_count=$((failure_count + 1))
    
    if (( failure_count >= MAX_FAILURES )); then
      log "Muchos fallos consecutivos (${failure_count}), emitiendo video offline..."
      if [[ -z "${FALLBACK_PID:-}" ]]; then
        fallback_offline "$CHANNEL_NAME"
      fi
      sleep 60
      failure_count=0
      continue
    fi
    
    sleep "$RESTART_DELAY"
    continue
  fi

  # Validación inicial del stream
  if ! initial_check; then
    kill_pipeline
    failure_count=$((failure_count + 1))
    
    if (( failure_count >= MAX_FAILURES )); then
      log "Fallo en validación inicial (${failure_count} intentos), emitiendo offline..."
      if [[ -z "${FALLBACK_PID:-}" ]]; then
        fallback_offline "$CHANNEL_NAME"
      fi
      sleep 60
      failure_count=0
      continue
    fi
    
    sleep "$RESTART_DELAY"
    continue
  fi

  # =================================================================
  # ¡STREAM REAL VALIDADO CON ÉXITO!
  # =================================================================
  failure_count=0
  log "Validación inicial OK. Restaurando señal en vivo..."

  # SWAP atómico: Devolvemos el enlace a los usuarios a la carpeta Live
  atomic_symlink_swap "$CHANNEL_DIR_LIVE"

  # Apagamos el fallback y limpiamos su RAM de forma segura
  if [[ -n "${FALLBACK_PID:-}" ]]; then
    kill_fallback
    clean_fallback_dir
  fi

  # Bucle de monitoreo continuo
  if ! health_loop; then
    kill_pipeline
    failure_count=$((failure_count + 1))
    
    if (( failure_count >= MAX_FAILURES )); then
      log "Pipeline caído repetidamente (${failure_count} veces), emitiendo offline..."
      if [[ -z "${FALLBACK_PID:-}" ]]; then
        fallback_offline "$CHANNEL_NAME"
      fi
      sleep 60
      failure_count=0
      continue
    fi
    
    sleep "$RESTART_DELAY"
    continue
  fi
done