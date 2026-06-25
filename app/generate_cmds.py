#!/usr/bin/env python3
"""
generate_cmds.py
Lee channels.json y genera archivos .cmd para el watcher.
Tipos: gigared, capturadora, static
"""

import json
import os
from utils import name_to_slug

BASE_DIR = "/opt/iptv/app"
CHANNELS_DIR = os.path.join(BASE_DIR, "channels")
CONFIG_FILE = os.path.join(BASE_DIR, "channels.json")

TEMPLATE_GIGARED = """/home/estranet/.local/bin/streamlink --retry-open 5 --retry-streams 5 --stream-segment-attempts 6 --stream-segment-timeout 15 --stream-timeout 60 --ffmpeg-ffmpeg /usr/bin/ffmpeg --ffmpeg-fout mpegts -O --locale es_ES {key_param} "{url}" best | mbuffer -m 4M -q | ffmpeg -nostdin -hide_banner -loglevel warning -fflags +genpts+discardcorrupt+igndts -avoid_negative_ts make_zero -i pipe:0 -max_interleave_delta 10M -c copy -f hls -hls_time 2 -hls_list_size 10 -hls_delete_threshold 3 -hls_flags delete_segments+append_list+independent_segments -hls_segment_filename __SEGMENT_PATTERN__ __PLAYLIST__"""

TEMPLATE_CAPTURADORA = """ffmpeg -hide_banner -stats -stats_period 1 \
  -vaapi_device {vaapi_device} \
  -fflags +genpts+nobuffer \
  -thread_queue_size 4096 -f v4l2 -input_format mjpeg -video_size {resolution} -framerate {fps} -i {device} \
  -thread_queue_size 4096 -f alsa -i {audio_device} \
  -vf "format=nv12,hwupload,setpts=N/FRAME_RATE/TB" \
  -c:v h264_vaapi -profile:v high -level 4.1 -qp {qp} -compression_level 1 \
  -g {gop} -keyint_min {gop} -sc_threshold 0 \
  -c:a aac -b:a {audio_bitrate} -ar 48000 \
  -af "volume={volume},aresample=async=1000:min_hard_comp=0.1" \
  -f hls \
  -hls_time 2 \
  -hls_list_size 10 \
  -hls_delete_threshold 3 \
  -hls_flags delete_segments \
  -hls_segment_filename __SEGMENT_PATTERN__ \
  __PLAYLIST__"""

TEMPLATE_STATIC = """ffmpeg -i "{url}" -c copy -f hls -hls_time 2 -hls_list_size 10 -hls_delete_threshold 3 -hls_flags delete_segments+independent_segments -hls_segment_filename __SEGMENT_PATTERN__ __PLAYLIST__"""


def generate():
    if not os.path.exists(CONFIG_FILE):
        print(f"No se encontró {CONFIG_FILE}")
        return
    
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    count = 0
    
    for ch in config.get("channels", []):
        ch_type = ch.get("type", "")
        cmd_content = None
        
        if ch_type == "gigared":
            key_param = ""
            if ch.get("decryption_key"):
                key_param = f'--decryption-key "{ch["decryption_key"]}"'
            
            cmd_content = TEMPLATE_GIGARED.format(
                url=ch["url"],
                key_param=key_param
            )
            
        elif ch_type == "capturadora":
            cmd_content = TEMPLATE_CAPTURADORA.format(
                vaapi_device=ch.get("vaapi_device", "/dev/dri/renderD128"),
                resolution=ch.get("resolution", "1280x720"),
                fps=ch.get("fps", 30),
                device=ch.get("device", "/dev/video0"),
                audio_device=ch.get("audio_device", "plughw:0,0"),
                qp=ch.get("qp", 28),
                gop=ch.get("gop", 60),
                audio_bitrate=ch.get("audio_bitrate", "128k"),
                volume=ch.get("volume", 1.5)
            )
        
        elif ch_type == "static":
            cmd_content = TEMPLATE_STATIC.format(url=ch["url"])
        
        else:
            continue
        
        cmd_file = os.path.join(CHANNELS_DIR, f"{name_to_slug(ch['name'])}.cmd")
        with open(cmd_file, 'w') as f:
            f.write(cmd_content.strip() + '\n')
        
        print(f"  [CMD] {ch_type}: {cmd_file}")
        count += 1
    
    print(f"Total: {count} archivos .cmd generados")


if __name__ == "__main__":
    generate()