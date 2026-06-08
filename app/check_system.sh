#!/bin/bash
# check_system.sh

echo "=== NGINX ==="
systemctl is-active nginx

echo ""
echo "=== BRIDGE YOUTUBE ==="
systemctl is-active iptv-bridge

echo ""
echo "=== WATCHERS ACTIVOS ==="
bash /opt/iptv/app/status_all.sh | grep "está corriendo" | wc -l
echo "watchers corriendo"

echo ""
echo "=== PRÓXIMO ESCANEO ==="
atq

echo ""
echo "=== LINKS YOUTUBE ==="
ls -lt /opt/iptv/links/*.txt 2>/dev/null | head -5
