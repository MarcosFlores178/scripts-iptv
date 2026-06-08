#!/usr/bin/env python3
"""
unified_bridge.py
Bridge unificado que maneja:
  - YouTube: lee .txt, publica a RTMP, transición suave
  - Capturadora: ffmpeg con VAAPI directo a RTMP
  - Static: ffmpeg -c copy desde URL externa a RTMP
  - Fallback: video offline cuando no hay señal
"""

import subprocess
import time
import json
import logging
import signal
import os
import re
from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass, field
from threading import Event

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN ---
STREAMS_JSON = "/opt/iptv/config/streams.json"
CHANNELS_JSON = "/home/estranet/iptv-test/channels.json"
VIDEO_OFFLINE = "http://localhost:8097/error/offline.m3u8"
NGINX_RTMP = "rtmp://localhost:1935/live"
TRANSITION_OVERLAP = 3
CHECK_INTERVAL = 5
HEALTH_INTERVAL = 30


@dataclass
class Stream:
    """Representa un stream de cualquier tipo"""
    name: str                    # Nombre del stream en RTMP
    stream_type: str             # "youtube", "capturadora", "static"
    current_url: Optional[str] = None
    current_ffmpeg: Optional[subprocess.Popen] = None
    new_ffmpeg: Optional[subprocess.Popen] = None
    transitioning: bool = False
    stop_event: Event = field(default_factory=Event)
    
    # Solo YouTube
    link_file: Optional[Path] = None
    
    # Solo capturadora
    capturadora_cmd: Optional[list] = None
    
    # Solo static
    static_url: Optional[str] = None


class UnifiedBridge:
    def __init__(self):
        self.streams: Dict[str, Stream] = {}
        self.nginx_rtmp = NGINX_RTMP
        self.video_offline = VIDEO_OFFLINE
        self.transition_overlap = TRANSITION_OVERLAP
        self.check_interval = CHECK_INTERVAL
        self.health_interval = HEALTH_INTERVAL
        
        self.load_youtube_streams()
        self.load_channels_json()
        
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
    
    def load_youtube_streams(self):
        """Carga streams YouTube desde streams.json"""
        if not os.path.exists(STREAMS_JSON):
            logger.info("No se encontró streams.json, omitiendo YouTube")
            return
        
        with open(STREAMS_JSON) as f:
            config = json.load(f)
        
        for item in config.get("youtube_streams", []):
            stream = Stream(
                name=item["stream_name"],
                stream_type="youtube",
                link_file=Path(item["link_file"])
            )
            self.streams[stream.name] = stream
            logger.info(f"[YouTube] {stream.name}")
    
    def load_channels_json(self):
        """Carga capturadoras y estáticos desde channels.json"""
        if not os.path.exists(CHANNELS_JSON):
            logger.info("No se encontró channels.json, omitiendo capturadoras/estáticos")
            return
        
        with open(CHANNELS_JSON, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        for ch in config.get("channels", []):
            ch_type = ch.get("type", "")
            name = ch["name"]
            
            if ch_type == "capturadora":
                stream = Stream(
                    name=name,
                    stream_type="capturadora",
                    capturadora_cmd=self.build_capturadora_cmd(ch)
                )
                self.streams[name] = stream
                logger.info(f"[Capturadora] {name} (device: {ch.get('device')})")
            
            elif ch_type == "static":
                stream = Stream(
                    name=name,
                    stream_type="static",
                    static_url=ch["url"]
                )
                self.streams[name] = stream
                logger.info(f"[Static] {name} → {ch['url'][:60]}...")
    
    def build_capturadora_cmd(self, ch: dict) -> list:
        """Construye el comando ffmpeg para capturadora con VAAPI"""
        device = ch.get("device", "/dev/video0")
        audio = ch.get("audio_device", "plughw:0,0")
        resolution = ch.get("resolution", "1280x720")
        fps = ch.get("fps", 30)
        vaapi = ch.get("vaapi_device", "/dev/dri/renderD128")
        qp = ch.get("qp", 28)
        gop = ch.get("gop", 60)
        audio_br = ch.get("audio_bitrate", "128k")
        volume = ch.get("volume", 1.5)
        name = ch["name"]
        
        return [
            'ffmpeg', '-hide_banner',
            '-vaapi_device', vaapi,
            '-fflags', '+genpts+nobuffer',
            '-thread_queue_size', '4096',
            '-f', 'v4l2', '-input_format', 'mjpeg',
            '-video_size', resolution, '-framerate', str(fps),
            '-i', device,
            '-thread_queue_size', '4096',
            '-f', 'alsa', '-i', audio,
            '-vf', 'format=nv12,hwupload,setpts=N/FRAME_RATE/TB',
            '-c:v', 'h264_vaapi', '-profile:v', 'high', '-level', '4.1',
            '-qp', str(qp), '-compression_level', '1',
            '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', audio_br, '-ar', '48000',
            '-af', f'volume={volume},aresample=async=1000:min_hard_comp=0.1',
            '-f', 'flv', '-flvflags', 'no_duration_filesize',
            f'{self.nginx_rtmp}/{name}'
        ]
    
    # ─── YouTube ───────────────────────────────────────────
    
    def read_link(self, stream: Stream) -> Optional[str]:
        try:
            if stream.link_file and stream.link_file.exists():
                url = stream.link_file.read_text().strip()
                if url:
                    return url
        except Exception as e:
            logger.error(f"Error leyendo {stream.link_file}: {e}")
        return None
    
    def get_youtube_url(self, stream: Stream) -> str:
        url = self.read_link(stream)
        return url if url else self.video_offline
    
    # ─── Static ────────────────────────────────────────────
    
    def get_static_url(self, stream: Stream) -> str:
        return stream.static_url if stream.static_url else self.video_offline
    
    # ─── Capturadora ───────────────────────────────────────
    
    def get_capturadora_url(self, stream: Stream) -> str:
        """La capturadora no tiene URL, devuelve marcador especial"""
        return "__CAPTURADORA__"
    
    # ─── Genérico ──────────────────────────────────────────
    
    def get_target_url(self, stream: Stream) -> str:
        if stream.stream_type == "youtube":
            return self.get_youtube_url(stream)
        elif stream.stream_type == "static":
            return self.get_static_url(stream)
        elif stream.stream_type == "capturadora":
            return self.get_capturadora_url(stream)
        return self.video_offline
    
    def start_ffmpeg(self, stream: Stream, url: Optional[str] = None, label: str = "primary") -> subprocess.Popen:
        rtmp_url = f"{self.nginx_rtmp}/{stream.name}"
        
        if stream.stream_type == "capturadora" and stream.capturadora_cmd:
            # Usar el comando VAAPI preconstruido
            cmd = stream.capturadora_cmd
            logger.info(f"[{stream.name}] Iniciando capturadora VAAPI ({label})")
        elif url and url != self.video_offline:
            # YouTube o static: ffmpeg -c copy desde URL
            cmd = [
                'ffmpeg', '-re', '-i', url,
                '-c', 'copy',
                '-f', 'flv', '-flvflags', 'no_duration_filesize',
                rtmp_url
            ]
            logger.info(f"[{stream.name}] Iniciando ffmpeg ({label}): {url[:80]}...")
        else:
            # Video offline
            cmd = [
                'ffmpeg', '-re', '-i', self.video_offline,
                '-c', 'copy',
                '-f', 'flv', '-flvflags', 'no_duration_filesize',
                rtmp_url
            ]
            logger.info(f"[{stream.name}] Iniciando video offline ({label})")
        
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
    
    def kill_ffmpeg(self, stream: Stream, process: subprocess.Popen, label: str = ""):
        if process and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=3)
                logger.info(f"[{stream.name}] ffmpeg {label} detenido (PID: {process.pid})")
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                logger.warning(f"[{stream.name}] ffmpeg {label} forzado a matar")
            except ProcessLookupError:
                pass
    
    def smooth_transition(self, stream: Stream, target_url: str):
        if stream.transitioning:
            return
        
        stream.transitioning = True
        logger.info(f"[{stream.name}] === Transición suave ===")
        
        new_process = self.start_ffmpeg(stream, target_url, "new")
        stream.new_ffmpeg = new_process
        time.sleep(self.transition_overlap)
        
        if new_process.poll() is not None:
            logger.error(f"[{stream.name}] Nuevo ffmpeg murió durante transición")
            stream.new_ffmpeg = None
            stream.transitioning = False
            return
        
        if stream.current_ffmpeg:
            self.kill_ffmpeg(stream, stream.current_ffmpeg, "old")
        
        stream.current_ffmpeg = stream.new_ffmpeg
        stream.current_url = target_url
        stream.new_ffmpeg = None
        stream.transitioning = False
        logger.info(f"[{stream.name}] === Transición completa ===")
    
    def update_stream(self, stream: Stream):
        target_url = self.get_target_url(stream)
        
        # Sin stream activo: iniciar
        if not stream.current_ffmpeg or stream.current_ffmpeg.poll() is not None:
            logger.info(f"[{stream.name}] Iniciando stream ({stream.stream_type})")
            stream.current_ffmpeg = self.start_ffmpeg(stream, target_url)
            stream.current_url = target_url
            return
        
        # Para capturadora: si se cayó, reiniciar (siempre mismo comando)
        if stream.stream_type == "capturadora":
            if stream.current_ffmpeg.poll() is not None:
                stream.current_ffmpeg = self.start_ffmpeg(stream)
            return
        
        # Para YouTube/static: si URL cambió, transición
        if target_url != stream.current_url:
            logger.info(f"[{stream.name}] URL cambió → transición")
            self.smooth_transition(stream, target_url)
    
    def health_check(self):
        for stream in self.streams.values():
            if stream.transitioning:
                continue
            if stream.current_ffmpeg and stream.current_ffmpeg.poll() is not None:
                logger.warning(f"[{stream.name}] Stream muerto, recuperando...")
                target_url = self.get_target_url(stream)
                stream.current_ffmpeg = self.start_ffmpeg(stream, target_url)
                stream.current_url = target_url
    
    def run(self):
        logger.info("=" * 50)
        logger.info("Unified Bridge iniciado")
        logger.info(f"Streams: {len(self.streams)}")
        logger.info(f"YouTube: {sum(1 for s in self.streams.values() if s.stream_type == 'youtube')}")
        logger.info(f"Capturadora: {sum(1 for s in self.streams.values() if s.stream_type == 'capturadora')}")
        logger.info(f"Static: {sum(1 for s in self.streams.values() if s.stream_type == 'static')}")
        logger.info("=" * 50)
        
        for stream in self.streams.values():
            self.update_stream(stream)
        
        last_health = time.time()
        
        while True:
            try:
                for stream in self.streams.values():
                    if not stream.transitioning:
                        self.update_stream(stream)
                
                if time.time() - last_health > self.health_interval:
                    self.health_check()
                    last_health = time.time()
                
                time.sleep(self.check_interval)
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                time.sleep(5)
        
        self.cleanup()
    
    def cleanup(self):
        logger.info("Limpiando...")
        for stream in self.streams.values():
            if stream.new_ffmpeg:
                self.kill_ffmpeg(stream, stream.new_ffmpeg, "new")
            if stream.current_ffmpeg:
                self.kill_ffmpeg(stream, stream.current_ffmpeg, "current")
        logger.info("Limpieza completa")
    
    def shutdown(self, signum, frame):
        logger.info(f"Señal {signum}, cerrando...")
        self.cleanup()
        exit(0)


if __name__ == "__main__":
    bridge = UnifiedBridge()
    bridge.run()