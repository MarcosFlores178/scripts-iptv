#!/usr/bin/env bash
set -u
set -o pipefail

if [[ $# -lt 1 ]]; then
  echo "Uso: $0 <nombre_canal>"
  exit 1
fi

CHANNEL_NAME="$1"

BASE_DIR="/opt/iptv/app"
CHANNELS_DIR="${BASE_DIR}/channels"
LOG_DIR="${BASE_DIR}/logs"

OUT_ROOT="/mnt/hls-ram"
CHANNEL_DIR="${OUT_ROOT}/${CHANNEL_NAME}"
PLAYLIST="${CHANNEL_DIR}/index.m3u8"
SEGMENT_PATTERN="${CHANNEL_DIR}/%06d.ts"

CMD_FILE="${CHANNELS_DIR}/${CHANNEL_NAME}.cmd"
RUN_LOG="${LOG_DIR}/${CHANNEL_NAME}-run.log"
WATCH_LOG="${LOG_DIR}/${CHANNEL_NAME}-watch.log"

# Video offline para fallback
VIDEO_OFFLINE="http://localhost:8097/error/offline.m3u8"

STARTUP_WAIT=20
CHECK_INTERVAL=10
PLAYLIST_MAX_AGE=20
RESTART_DELAY=5
FORCE_RESTART_EVERY=0
MAX_FAILURES=3  # Intentos antes de emitir offline

# Freeze detection
LAST_SEGMENT_NAME=""
LAST_SEGMENT_SIZE=""
LAST_SEGMENT_STUCK_COUNT=0
FREEZE_THRESHOLD=3

mkdir -p "$LOG_DIR"

log() {
  printf '[%s] [%s] %s\n' "$(date '+%F %T')" "$CHANNEL_NAME" "$1" | tee -a "$WATCH_LOG"
}

prepare_channel_dir() {
  mkdir -p "$CHANNEL_DIR"
}

clean_channel_dir() {
  rm -rf "$CHANNEL_DIR"
  mkdir -p "$CHANNEL_DIR"
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
    kill -- -"$PIPE_PID" 2>/dev/null || true
    sleep 2
    kill -9 -- -"$PIPE_PID" 2>/dev/null || true
    sleep 1
  fi
}

kill_fallback() {
  if [[ -n "${FALLBACK_PID:-}" ]]; then
    kill "$FALLBACK_PID" 2>/dev/null || true
    wait "$FALLBACK_PID" 2>/dev/null || true
    FALLBACK_PID=""
  fi
}

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

has_audio() {
    [[ -f "$PLAYLIST" ]] || return 1
    local probe_output
    probe_output=$(ffprobe -v error -show_entries stream=codec_type -of default=noprint_wrappers=1:nokey=0 "$PLAYLIST" 2>/dev/null)
    if [[ "$probe_output" =~ "audio" ]]; then
        return 0
    else
        return 1
    fi
}

has_video() {
    [[ -f "$PLAYLIST" ]] || return 1
    local probe_output
    probe_output=$(ffprobe -v error -show_entries stream=codec_type -of default=noprint_wrappers=1:nokey=0 "$PLAYLIST" 2>/dev/null)
    if [[ "$probe_output" =~ "video" ]]; then
        return 0
    else
        return 1
    fi
}

detect_freeze_basic() {
  local latest_segment
  latest_segment="$(ls -1t "${CHANNEL_DIR}"/*.ts 2>/dev/null | head -n 1)"

  [[ -n "$latest_segment" ]] || return 1

  local seg_name seg_size
  seg_name="$(basename "$latest_segment")"
  seg_size="$(stat -c %s "$latest_segment" 2>/dev/null || echo 0)"

  if [[ "$seg_name" == "$LAST_SEGMENT_NAME" && "$seg_size" == "$LAST_SEGMENT_SIZE" ]]; then
    LAST_SEGMENT_STUCK_COUNT=$((LAST_SEGMENT_STUCK_COUNT + 1))
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

fallback_offline() {
  local channel="$1"
  local output_dir="/mnt/hls-ram/${channel}"
  local playlist="${output_dir}/index.m3u8"
  local seg_pattern="${output_dir}/%06d.ts"

  log "Emitiendo video offline para ${channel}"

  mkdir -p "$output_dir"
  rm -f "${output_dir}"/*.ts

  ffmpeg -re -i "$VIDEO_OFFLINE" \
    -c copy \
    -f hls \
    -hls_time 2 \
    -hls_list_size 10 \
    -hls_flags delete_segments \
    -hls_segment_filename "$seg_pattern" \
    "$playlist" 2>/dev/null
}

build_command() {
  if [[ ! -f "$CMD_FILE" ]]; then
    log "No existe archivo de comando: $CMD_FILE"
    return 1
  fi

  python3 - <<'PY' "$CMD_FILE" "$SEGMENT_PATTERN" "$PLAYLIST"
import sys

cmd_file = sys.argv[1]
segment_pattern = sys.argv[2]
playlist = sys.argv[3]

with open(cmd_file, "r", encoding="utf-8") as f:
    cmd = f.read().strip()

cmd = cmd.replace("__SEGMENT_PATTERN__", segment_pattern)
cmd = cmd.replace("__PLAYLIST__", playlist)

print(cmd)
PY
}

start_pipeline() {
  prepare_channel_dir
  reset_freeze_state

  local cmd
  cmd="$(build_command)" || return 1

  log "Iniciando pipeline"
  log "Comando desde: $CMD_FILE"
  log "Salida HLS: $PLAYLIST"

  : > "$RUN_LOG"

  setsid bash -c "$cmd" >>"$RUN_LOG" 2>&1 &
  PIPE_PID=$!
  START_TIME=$(date +%s)

  log "PID/PGID pipeline: $PIPE_PID"
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

  if ! has_video; then
    log "No hay VIDEO en la salida; rechecando en 5s"
    sleep 5
    if ! has_video; then
      log "Confirmado: sigue sin VIDEO"
      log_tail
      return 1
    fi
  fi

  if ! has_audio; then
    log "No hay AUDIO en la salida; rechecando en 5s"
    sleep 5
    if ! has_audio; then
      log "Confirmado: sigue sin AUDIO"
      log_tail
      return 1
    fi
  fi

  if should_restart_due_to_log; then
    log "Se detectó warning/error relevante en el log durante arranque"
    log_tail
    return 1
  fi

  log "Validación inicial OK (audio + video detectados)"
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
      log "Posible FREEZE detectado: último segmento no avanza"
      log_tail
      return 1
    fi

    if ! has_video; then
      log "Se perdió el VIDEO en ejecución"
      sleep 5
      if ! has_video; then
        log "Confirmado: sigue sin VIDEO"
        log_tail
        return 1
      fi
    fi

    if ! has_audio; then
      log "Se perdió el AUDIO en ejecución; rechecando en 5s"
      sleep 5
      if ! has_audio; then
        log "Confirmado: sigue sin AUDIO"
        log_tail
        return 1
      fi
    fi

    if should_restart_due_to_log; then
      log "Se detectó warning/error relevante en log en ejecución"
      log_tail
      return 1
    fi

    if (( FORCE_RESTART_EVERY > 0 )); then
      local now elapsed
      now=$(date +%s)
      elapsed=$((now - START_TIME))
      if (( elapsed >= FORCE_RESTART_EVERY )); then
        log "Reinicio periódico programado tras ${elapsed}s"
        return 1
      fi
    fi
  done
}

trap 'log "Saliendo, matando pipeline y fallback"; kill_pipeline; kill_fallback; exit 0' INT TERM

log "===== watcher iniciado ====="

failure_count=0

while true; do
  kill_fallback

  clean_channel_dir

  if ! start_pipeline; then
    log "No se pudo iniciar el pipeline"
    failure_count=$((failure_count + 1))
    
    if (( failure_count >= MAX_FAILURES )); then
      log "Muchos fallos consecutivos (${failure_count}), emitiendo video offline..."
      fallback_offline "$CHANNEL_NAME" &
      FALLBACK_PID=$!
      sleep 60
      failure_count=0
      continue
    fi
    
    sleep "$RESTART_DELAY"
    continue
  fi

  if ! initial_check; then
    kill_pipeline
    failure_count=$((failure_count + 1))
    
    if (( failure_count >= MAX_FAILURES )); then
      log "Fallo en validación inicial (${failure_count} intentos), emitiendo video offline..."
      fallback_offline "$CHANNEL_NAME" &
      FALLBACK_PID=$!
      sleep 60
      failure_count=0
      continue
    fi
    
    sleep "$RESTART_DELAY"
    continue
  fi

  failure_count=0

  if ! health_loop; then
    kill_pipeline
    failure_count=$((failure_count + 1))
    
    if (( failure_count >= MAX_FAILURES )); then
      log "Pipeline caído repetidamente (${failure_count} veces), emitiendo video offline..."
      fallback_offline "$CHANNEL_NAME" &
      FALLBACK_PID=$!
      sleep 60
      failure_count=0
      continue
    fi
    
    sleep "$RESTART_DELAY"
    continue
  fi
done