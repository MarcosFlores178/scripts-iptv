#!/usr/bin/env python3
"""
generate_m3u.py
Genera el M3U combinado para la app Android TV.
Fuentes:
  - channel.txt (YouTube)
  - channels.json (Gigared + capturadora + static)
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import name_to_slug

BASE_DIR = "/opt/iptv/app"
INPUT_CHANNELS = os.path.join(BASE_DIR, "channel.txt")
CHANNELS_JSON = os.path.join(BASE_DIR, "channels.json")
OUTPUT_M3U_LOCAL = "/opt/EstranetTV-Unificado/EstranetTV-backend/assets/playlists/combined_list_local.m3u"
OUTPUT_M3U_PUBLIC = "/opt/EstranetTV-Unificado/EstranetTV-backend/assets/playlists/combined_list_public.m3u"
SERVER_IP_PRIVATE = "192.168.130.22"
SERVER_IP_PUBLIC = "181.209.79.77"
SERVER_PORT = "8095"

GROUP_ORDER = [
    'Nacionales', 'Locales', 'Noticias', 'Variedades Nacionales',
    'Infantiles', 'Deportes', 'Noticias Internacionales', 'Novelas',
    'Peliculas', 'Variedades Internacionales', 'Documentales',
    'Musica', 'Religiosos', 'Nacionales Interior', 'Internacionales',
    'Radio', 'TV Aire', 'Streams'
]


def parse_youtube_channels(filepath):
    """Lee channel.txt (formato: nombre | grupo | logo | id)"""
    channels = []
    if not os.path.exists(filepath):
        print(f"  [!] No se encontró {filepath}")
        return channels
    
    with open(filepath, 'r', encoding='utf-8') as f:
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
                h['url'] = line
                channels.append(h)
                h = None
    return channels


def parse_json_channels(filepath):
    """Lee channels.json (Gigared + capturadora + static)"""
    if not os.path.exists(filepath):
        print(f"  [!] No se encontró {filepath}")
        return []
    
    with open(filepath, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    return [ch for ch in config.get("channels", []) if ch.get("type") != "youtube"]


def generate_m3u(output_file, server_ip, youtube, otros):
       
    grouped = defaultdict(list)
    
    for ch in youtube:
        slug = name_to_slug(ch['name'])
        entry = (
            ch['group'],
            f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-logo="{ch["logo"]}" group-title="{ch["group"]}",{ch["name"]}',
            f'http://{server_ip}:{SERVER_PORT}/hls/live/youtube_{slug}.m3u8'
        )
        grouped[ch['group']].append(entry)
    
    for ch in otros:
        slug = name_to_slug(ch['name'])
        entry = (
            ch.get('group', 'Otros'),
            f'#EXTINF:-1 tvg-id="{ch.get("tvg_id", ch["name"])}" tvg-logo="{ch.get("logo", "")}" group-title="{ch.get("group", "Otros")}",{ch["tvg_id"]}',
            f'http://{server_ip}:{SERVER_PORT}/hls/{slug}/index.m3u8'
        )
        grouped[ch.get('group', 'Otros')].append(entry)
    
    lines = ["#EXTM3U", ""]
    all_groups = GROUP_ORDER + [g for g in grouped if g not in GROUP_ORDER]
    
    for group in all_groups:
        if group in grouped:
            for _, extinf_line, url_line in grouped[group]:
                lines.append(extinf_line)
                lines.append(url_line)
                lines.append("")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    total = len(youtube) + len(otros)
    print(f"M3U generados: {output_file}")
    print(f"  YouTube: {len(youtube)} canales")
    print(f"  Gigared/Capturadora/Static: {len(otros)} canales")
    print(f"  Total: {total} canales")

def generate():
    youtube = parse_youtube_channels(INPUT_CHANNELS)
    otros = parse_json_channels(CHANNELS_JSON)
    generate_m3u(OUTPUT_M3U_LOCAL, SERVER_IP_PRIVATE, youtube,
        otros)
    generate_m3u(OUTPUT_M3U_PUBLIC, SERVER_IP_PUBLIC, youtube,
        otros)

    print("Listas generadas correctamente:")
    print(f"  {OUTPUT_M3U_LOCAL}")
    print(f"  {OUTPUT_M3U_PUBLIC}")

if __name__ == "__main__":
    generate()