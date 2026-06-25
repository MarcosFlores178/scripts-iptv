#!/usr/bin/env python3
"""
youtube_bridge.py (v2 robusto)
Bridge IPTV YouTube HLS → RTMP con reconexión inteligente
"""

import subprocess
import time
import json
import logging
import signal
import os
import sys
from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

logger = logging.getLogger("youtube-bridge")


# ================= CONFIG =================
STREAMS_JSON = "/opt/iptv/config/streams.json"
VIDEO_OFFLINE = "http://localhost:8097/error/offline.m3u8"
NGINX_RTMP = "rtmp://localhost:1935/live"

CHECK_INTERVAL = 5
HEALTH_INTERVAL = 20

MAX_FAILURES = 3
RETRY_AFTER_FAIL_SEC = 60  # en vez de cooldown fijo de 300


# ================= STREAM STATE =================
@dataclass
class Stream:
    name: str
    channel_name: str
    link_file: Path

    current_url: Optional[str] = None
    process: Optional[subprocess.Popen] = None

    consecutive_failures: int = 0
    last_start_time: float = 0
    last_failure_time: float = 0

    offline_mode: bool = False
    failed_url: Optional[str] = None
    last_retry: float = 0


# ================= BRIDGE =================
class YouTubeBridge:

    def __init__(self):
        self.streams: Dict[str, Stream] = {}
# Diccionario para guardar el estado: {slug: {'url': '...', 'mtime': 0}}
        self.url_cache = {}

        self.load_config()

        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    # -------- CONFIG --------
    def load_config(self):
        if not os.path.exists(STREAMS_JSON):
            logger.error(f"No existe {STREAMS_JSON}")
            sys.exit(1)

        with open(STREAMS_JSON, encoding="utf-8") as f:
            config = json.load(f)

        for item in config.get("youtube_streams", []):
            stream = Stream(
                name=item["stream_name"],
                channel_name=item["channel_name"],
                link_file=Path(item["link_file"])
            )
            self.streams[stream.name] = stream
            logger.info(f"Loaded stream: {stream.name}")

    # -------- READ URL --------
    def read_url(self, stream: Stream) -> Optional[str]:
        try:
            if not stream.link_file.exists():
                return None

            current_mtime = stream.link_file.stat().st_mtime
        
            # Verificar caché con expiración
            if stream.name in self.url_cache:
                cached = self.url_cache[stream.name]
                cache_age = time.time() - cached.get('timestamp', 0)
            
                # Si el archivo no ha cambiado y la caché es reciente (< 60 segundos)
                if cached['mtime'] == current_mtime and cache_age < 60:
                    return cached['url']

            # Leer archivo
            url = stream.link_file.read_text(encoding="utf-8").strip()
        
            if url.startswith(("http://", "https://", "rtmp://", "rtmps://")):
                self.url_cache[stream.name] = {
                    'url': url, 
                    'mtime': current_mtime,
                    'timestamp': time.time()
                }
                return url

        except Exception as e:
            logger.error(f"[{stream.name}] read error: {e}")

        return None

    # -------- FFPROBE CHECK (soft) --------
    def probe_url(self, url: str) -> bool:
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-timeout", "5000000",
                url
            ]
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return result.returncode == 0
        except Exception:
            return False

   # -------- FFMEG START --------
    def start_ffmpeg(self, stream: Stream, url: str):
        rtmp_url = f"{NGINX_RTMP}/{stream.name}"

        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_delay_max", "5",
            "-i", url,
            "-c", "copy",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            "-rtmp_live", "live",
            rtmp_url
        ]

        logger.info(f"[{stream.name}] starting ffmpeg")
        stream.last_start_time = time.time()

        # Abrimos el archivo en modo append dentro de un bloque 'with'
        # Esto garantiza que el manejador del archivo se cierre correctamente
        # después de que el proceso ha sido lanzado.
        log_path = f"/tmp/{stream.name}.log"
        with open(log_path, "a") as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True
            )
        
        # El archivo log_file ya está cerrado aquí automáticamente, 
        # pero el proceso sigue ejecutándose en segundo plano.
        return process

    # -------- STOP FFMEG --------
    def stop_ffmpeg(self, stream: Stream):
        if stream.process and stream.process.poll() is None:
            try:
                os.killpg(os.getpgid(stream.process.pid), signal.SIGTERM)
                stream.process.wait(timeout=3)
            except Exception:
                os.killpg(os.getpgid(stream.process.pid), signal.SIGKILL)

    # -------- GET TARGET URL --------
    def get_target_url(self, stream: Stream) -> str:
        url = self.read_url(stream)

        if not url:
            return VIDEO_OFFLINE

        # si está en modo offline pero cambió la URL → salir rápido
        if stream.offline_mode and url != stream.failed_url:
            logger.info(f"[{stream.name}] new URL detected → exit offline mode")
            stream.offline_mode = False
            stream.consecutive_failures = 0

        return url

    # -------- UPDATE STREAM --------
    def update_stream(self, stream: Stream):
        target_url = self.get_target_url(stream)

        # iniciar si no existe
        if not stream.process or stream.process.poll() is not None:

            # detectar muerte real
            if stream.process and stream.process.poll() is not None:

                runtime = time.time() - stream.last_start_time

                if runtime < 10:
                    stream.consecutive_failures += 1
                    logger.warning(
                        f"[{stream.name}] quick crash ({runtime:.1f}s) "
                        f"{stream.consecutive_failures}/{MAX_FAILURES}"
                    )

                stream.last_failure_time = time.time()

                if stream.consecutive_failures >= MAX_FAILURES:
                    logger.error(f"[{stream.name}] entering offline mode")
                    stream.offline_mode = True
                    stream.failed_url = target_url

            logger.info(f"[{stream.name}] starting stream")
            stream.process = self.start_ffmpeg(stream, target_url)
            stream.current_url = target_url
            return

        # cambio de URL
        if target_url != stream.current_url:
            logger.info(f"[{stream.name}] URL changed → restarting ffmpeg")
            self.stop_ffmpeg(stream)

            stream.process = self.start_ffmpeg(stream, target_url)
            stream.current_url = target_url

    # -------- HEALTH --------
    def health_check(self):
        now = time.time()

        for stream in self.streams.values():

            if stream.offline_mode:
                # reintento inteligente
                if now - stream.last_retry < RETRY_AFTER_FAIL_SEC:
                    continue

                stream.last_retry = now

                url = self.read_url(stream)
                # Usar probe_url para asegurar que el link realmente funciona
                if url and url != stream.failed_url and self.probe_url(url):
                    logger.info(f"[{stream.name}] recovery detected and validated")
                    stream.offline_mode = False
                    stream.consecutive_failures = 0

    # -------- LOOP --------
    def run(self):
        logger.info("=== YouTube Bridge v2 started ===")

        while True:
            try:
                for stream in self.streams.values():
                    self.update_stream(stream)

                self.health_check()

                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                logger.error(f"loop error: {e}")
                time.sleep(5)

    # -------- CLEANUP --------
    def cleanup(self):
        logger.info("cleaning up...")

        for stream in self.streams.values():
            self.stop_ffmpeg(stream)

        logger.info("done")

    def shutdown(self, signum, frame):
        logger.info(f"signal {signum}")
        self.cleanup()
        sys.exit(0)


# ================= MAIN =================
if __name__ == "__main__":
    bridge = YouTubeBridge()
    bridge.run()