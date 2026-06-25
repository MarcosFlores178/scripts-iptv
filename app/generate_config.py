"""
Genera streams.json y youtube_channels.m3u a partir de channel.txt
Ejecutar solo cuando se agreguen/quiten canales.
"""
import json
import re
import os
import sys
# Para importar utils.py desde el mismo directorio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import name_to_slug

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CHANNELS = os.path.join(BASE_DIR, 'channel.txt')
STREAMS_JSON = "/opt/iptv/config/streams.json"
OUTPUT_M3U = os.path.join(BASE_DIR, 'youtube_channels.m3u')
SERVER_IP = "192.168.130.22"  # Cambiar según tu IP
SERVER_PORT = "8095"


def parse_channels(filepath):
    channels = []
    with open(filepath, 'r') as f:
        h = None
        for line in f:
            line = line.strip()
            if not line or line.startswith('~~'):
                continue
            if not line.startswith('http'):
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 4:
                    h = {'name': parts[0], 'group': parts[1], 'logo': parts[2], 'id': parts[3]}
            elif h:
                channels.append(h)
    return channels

def generate():
    channels = parse_channels(INPUT_CHANNELS)
    streams = []
    m3u_lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""]
    
    for ch in channels:
        slug = name_to_slug(ch['name'])
        streams.append({
            "channel_name": slug,
            "stream_name": f"youtube_{slug}",
            "link_file": f"/opt/iptv/links/{slug}.txt",
            "display_name": ch['name'],
            "group": ch['group'],
            "logo": ch['logo'],
            "tvg_id": ch['id']
        })
        m3u_lines.append(f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-logo="{ch["logo"]}" group-title="{ch["group"]}",{ch["name"]}')
        m3u_lines.append(f'http://{SERVER_IP}:{SERVER_PORT}/hls/youtube_{slug}.m3u8')
        m3u_lines.append("")
    
    # Guardar JSON
    os.makedirs(os.path.dirname(STREAMS_JSON), exist_ok=True)
    with open(STREAMS_JSON, 'w') as f:
        json.dump({"youtube_streams": streams}, f, indent=2, ensure_ascii=False)
    
    # Guardar M3U
    with open(OUTPUT_M3U, 'w') as f:
        f.write("\n".join(m3u_lines))
    
    print(f"Generado: {STREAMS_JSON} ({len(streams)} streams)")
    print(f"Generado: {OUTPUT_M3U}")

if __name__ == "__main__":
    generate()