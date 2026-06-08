#!/bin/bash
# smart_scan.sh

set -u

APP_DIR="/opt/iptv/app"
PYTHON_BIN="$APP_DIR/venv/bin/python"
SCRIPT_PATH="$APP_DIR/smart_scan.sh"
LOG_FILE="$APP_DIR/smart_scan.log"
CACHE_FILE="$APP_DIR/links_cache.json"

cd "$APP_DIR" || exit 1

LOCKFILE="/tmp/iptv_scan.lock"

exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "[$(date)] En proceso: Ya hay una instancia ejecutándose." | tee -a "$LOG_FILE"
    exit 1
fi

log() {
    echo "[$(date)] $1" | tee -a "$LOG_FILE"
}

log ">>> Iniciando actualización de canales..."

###############################################################################
# main.py
###############################################################################

"$PYTHON_BIN" "$APP_DIR/main.py" 2>&1 | tee -a "$LOG_FILE"
MAIN_STATUS=${PIPESTATUS[0]}

if [ "$MAIN_STATUS" -ne 0 ]; then
    log "ERROR: main.py terminó con código $MAIN_STATUS"
    exit 1
fi

###############################################################################
# generate_m3u.py
###############################################################################

"$PYTHON_BIN" "$APP_DIR/generate_m3u.py" 2>&1 | tee -a "$LOG_FILE"
GEN_STATUS=${PIPESTATUS[0]}

if [ "$GEN_STATUS" -ne 0 ]; then
    log "ERROR: generate_m3u.py terminó con código $GEN_STATUS"
    exit 1
fi

###############################################################################
# Re-agendado inteligente
###############################################################################

if [ -f "$CACHE_FILE" ]; then

    if ! jq empty "$CACHE_FILE" >/dev/null 2>&1; then
        log "ERROR: links_cache.json contiene JSON inválido."
        exit 1
    fi

    AHORA=$(date +%s)

    PROXIMO_REFRESH=$(
        jq -r '.[] | .next_refresh? // empty' "$CACHE_FILE" |
        sort -n |
        awk -v ahora="$AHORA" '$1 > ahora {print $1; exit}'
    )

    PROXIMO_EVENTO=$(
        jq -r '.[] | .next_event? // empty' "$CACHE_FILE" |
        sort -n |
        awk -v ahora="$AHORA" '$1 > ahora {print $1; exit}'
    )

    PROXIMO=""

    if [ -n "$PROXIMO_REFRESH" ] && [ -n "$PROXIMO_EVENTO" ]; then
        if [ "$PROXIMO_REFRESH" -lt "$PROXIMO_EVENTO" ]; then
            PROXIMO="$PROXIMO_REFRESH"
        else
            PROXIMO="$PROXIMO_EVENTO"
        fi
    elif [ -n "$PROXIMO_REFRESH" ]; then
        PROXIMO="$PROXIMO_REFRESH"
    elif [ -n "$PROXIMO_EVENTO" ]; then
        PROXIMO="$PROXIMO_EVENTO"
    fi

    if [ -n "$PROXIMO" ]; then

        DELTA=$((PROXIMO - AHORA))

        # Redondear hacia arriba
        ESPERA=$(((DELTA + 59) / 60))
        ESPERA=$((ESPERA - 3))

        if [ "$ESPERA" -gt 0 ] && [ "$ESPERA" -le 365 ]; then

            FECHA_PROXIMA=$(date -d "@$PROXIMO" "+%Y-%m-%d %H:%M:%S")

            log "Próximo timestamp detectado: $PROXIMO"
            log "Próxima ejecución estimada: $FECHA_PROXIMA"
            log "Faltan $DELTA segundos (~$ESPERA minutos)"

            while read -r job; do

                if at -c "$job" 2>/dev/null | grep -q "$SCRIPT_PATH"; then
                    atrm "$job" >/dev/null 2>&1
                fi

            done < <(atq | awk '{print $1}')

            echo "bash $SCRIPT_PATH >> $LOG_FILE 2>&1" | at now + "$ESPERA" minutes -M >/dev/null 2>&1

            log "Éxito: Próximo reescaneo agendado en $ESPERA minutos."

        else
            log "Alerta: Tiempo calculado fuera de rango ($ESPERA minutos)."
        fi

    else
        log "No se encontraron timestamps futuros en links_cache.json."
    fi

else
    log "No existe links_cache.json."
fi

###############################################################################
# Finalización
###############################################################################

log "<<< Proceso finalizado correctamente."