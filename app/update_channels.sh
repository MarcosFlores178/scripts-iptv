#!/bin/bash
# update_channels.sh
# Ejecutar después de modificar channel.txt o channels.json

set -u

BASE_DIR="/opt/iptv/app"
cd "$BASE_DIR"

echo "============================================"
echo "  Actualizando canales IPTV"
echo "  $(date '+%F %T')"
echo "============================================"
echo ""

# 1. YouTube: generar streams.json para el bridge
echo "[1/4] Generando configuración YouTube..."
python3 generate_config.py
echo ""

# 2. Generar archivos .cmd para watchers
echo "[2/4] Generando archivos .cmd..."
python3 generate_cmds.py
echo ""

# 3. Reiniciar watchers (Gigared, capturadora, static)
echo "[3/4] Reiniciando watchers..."
bash stop_all.sh 2>/dev/null
sleep 2
bash start_all.sh
echo ""

# 4. Reiniciar bridge YouTube y forzar escaneo
echo "[4/4] Reiniciando bridge y escaneando links..."
sudo systemctl restart iptv-bridge 2>/dev/null || echo "  [!] No se pudo reiniciar iptv-bridge"
bash smart_scan.sh

echo ""
echo "============================================"
echo "  Actualización completada"
echo "============================================"