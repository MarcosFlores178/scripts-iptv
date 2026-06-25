#!/usr/bin/env bash
set -u
set -o pipefail

if [[ $# -lt 1 ]]; then
  echo "Uso: $0 <nombre_canal>"
  exit 1
fi

CHANNEL_NAME="$1"

BASE_DIR="/home/estranet/iptv-test"
CHANNELS_DIR="${BASE_DIR}/channels"
LOG_DIR="${BASE_DIR}/logs"

OUT_ROOT="/mnt/hls-ram"
CHANNEL_DIR="${OUT_ROOT}/${CHANNEL_NAME}"
PLAYLIST="${CHANNEL_DIR}/index.m3u8"
SEGMENT_PATTERN="${CHANNEL_DIR}/%06d.ts"

CMD_FILE="${CHANNELS_DIR}/${CHANNEL_NAME}.cmd"
RUN_LOG="${LOG_DIR}/${CHANNEL_NAME}-run.log"
WATCH_LOG="${LOG_DIR}/${CHANNEL_NAME}-watch.log"

STARTUP_WAIT=20
CHECK_INTERVAL=10
PLAYLIST_MAX_AGE=20
RESTART_DELAY=5
FORCE_RESTART_EVERY=0

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
    kill "$PIPE_PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PIPE_PID" 2>/dev/null || true
    pkill -P "$PIPE_PID" 2>/dev/null || true
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
  log_has_pattern 'Codec AVOption b .* has not been used' && return 0
  log_has_pattern 'Read timeout|Error while reading from substream|Stream ended|Broken pipe|Conversion failed|Immediate exit requested' && return 0
  log_has_pattern 'No such file or directory' && return 0
  return 1
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

  local cmd
  cmd="$(build_command)" || return 1

  log "Iniciando pipeline"
  log "Comando desde: $CMD_FILE"
  log "Carpeta canal: $CHANNEL_DIR"

  : > "$RUN_LOG"

  bash -c "$cmd" >>"$RUN_LOG" 2>&1 &
  PIPE_PID=$!
  START_TIME=$(date +%s)

  log "PID pipeline: $PIPE_PID"
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

  if should_restart_due_to_log; then
    log "Se detectó warning/error relevante en el log durante arranque"
    log_tail
    return 1
  fi

  log "Validación inicial OK"
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

trap 'log "Saliendo, matando pipeline"; kill_pipeline; exit 0' INT TERM

log "===== watcher iniciado ====="

while true; do
  clean_channel_dir

  if ! start_pipeline; then
    log "No se pudo iniciar el pipeline"
    sleep "$RESTART_DELAY"
    continue
  fi

  if ! initial_check; then
    kill_pipeline
    sleep "$RESTART_DELAY"
    continue
  fi

  if ! health_loop; then
    kill_pipeline
    sleep "$RESTART_DELAY"
    continue
  fi
done