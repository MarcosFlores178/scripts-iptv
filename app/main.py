import yt_dlp
import os
import re
import requests
import urllib3
import time
import json
import unicodedata
import tempfile
from collections import defaultdict
import random
from requests.exceptions import Timeout, ConnectionError
from typing import Any, Optional, Tuple, Iterable, cast
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import name_to_slug as slugify_channel_name

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LINKS_DIR = '/opt/iptv/links'

# --- CONFIGURACION DE RUTAS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CHANNELS = os.path.join(BASE_DIR, 'channel.txt')
STATIC_LIST = os.path.join(BASE_DIR, 'static_list.m3u')
COOKIE_FILE = os.path.join(BASE_DIR, 'cookies.txt')
OUTPUT_FILE = os.path.join(BASE_DIR, 'combined_list.m3u')
CACHE_FILE = os.path.join(BASE_DIR, 'links_cache.json')
VIDEO_OFFLINE = "http://192.168.130.22:8097/error/offline.m3u8"

GROUP_ORDER = [
    'Nacionales', 'Locales', 'Noticias', 'Variedades Nacionales',
    'Infantiles', 'Deportes', 'Noticias Internacionales', 'Novelas',
    'Peliculas', 'Variedades Internacionales', 'Documentales',
    'Musica', 'Religiosos', 'Nacionales Interior', 'Internacionales', 'Radio'
]

YOUTUBE_BLOCKED = False

# --- LOGICA DE CACHE ---

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=4)


# --- FUNCIONES TECNICAS ---

class YtdlpLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        if 'PO Token' in msg or 'HTTP Error 403' in msg:
            print(f"      [yt-dlp warning] {msg}")

    def error(self, msg):
        # El error real se imprime clasificado desde los except.
        pass


def describe_ytdlp_error(error):
    message = str(error)
    lower = message.lower()

    if 'will begin' in lower or 'premieres in' in lower:
        return 'programado', message
    if 'not currently live' in lower or 'was live' in lower or 'is not live' in lower:
        return 'no_vivo', message
    if 'sign in to confirm' in lower or 'confirm you' in lower or 'bot' in lower:
        return 'bot_o_cookies', message
    if 'http error 429' in lower or 'too many requests' in lower or 'rate-limited' in lower or 'for up to an hour' in lower:
        return 'rate_limit_429', message
    if 'http error 403' in lower or 'po token' in lower:
        return 'po_token_o_403', message

    return 'error', message

def extract_expire_from_url(url):
    if not isinstance(url, str):
        return None
    match = re.search(r'/expire/(\d+)', url)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def save_link_for_bridge(channel_name: str, url: str, links_dir: str = LINKS_DIR):
    os.makedirs(links_dir, exist_ok=True)
    log_dir = os.path.join(links_dir, 'log')
    os.makedirs(log_dir, exist_ok=True)

    safe_name = slugify_channel_name(channel_name)
    if not safe_name:
        raise ValueError(f'Invalid channel name: {channel_name}')

    final_path = os.path.join(links_dir, f'{safe_name}.txt')
    with tempfile.NamedTemporaryFile('wb', dir=links_dir, delete=False, prefix=f'{safe_name}.', suffix='.tmp') as tmp_file:
        tmp_file.write(url.encode('utf-8'))
        temp_path = tmp_file.name

    os.replace(temp_path, final_path)

    log_path = os.path.join(log_dir, 'updates.log')
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    url_preview = url[:80]
    with open(log_path, 'a', encoding='utf-8') as log_file:
        log_file.write(f'{timestamp} {safe_name} {url_preview}\n')


def clear_link_for_bridge(channel_name: str, links_dir: str = LINKS_DIR):
    safe_name = slugify_channel_name(channel_name)
    if not safe_name:
        raise ValueError(f'Invalid channel name: {channel_name}')

    path_to_remove = os.path.join(links_dir, f'{safe_name}.txt')
    if os.path.exists(path_to_remove):
        try:
            os.remove(path_to_remove)
        except FileNotFoundError:
            pass


def get_youtube_data(channel_url, cache, original_name):
    # 1. Reutilizar cache si es valida.
    if channel_url in cache:
        data = cache[channel_url]
        vivos_cache = data.get('vivos') if isinstance(data, dict) else None
        next_refresh = data.get('next_refresh') if isinstance(data, dict) else None
        timestamp_cache = data.get('timestamp', 0) if isinstance(data, dict) else 0

        if vivos_cache:
            ahora = time.time()

            if next_refresh:
                if ahora < next_refresh:
                    resultado_cache = is_link_online_pro(
                        vivos_cache[0]['link'], original_name, tipo_canal="youtube"
                    )
                    if resultado_cache is True:
                        return vivos_cache, data.get('next_event'), False
                print(f"Link expirado o inválido para {original_name}, regenerando...")
                del cache[channel_url]
            elif timestamp_cache and (ahora - timestamp_cache < 1800):
                resultado_cache = is_link_online_pro(
                    vivos_cache[0]['link'], original_name, tipo_canal="youtube"
                )
                if resultado_cache is True:
                    return vivos_cache, data.get('next_event'), False
            else:
                print(f"Cache antigua o caducada para {original_name}, regenerando...")
                del cache[channel_url]
    clean_url = channel_url.split('/live')[0].split('/streams')[0].rstrip('/')
    search_url = f"{clean_url}/streams"

    ydl_opts_list: dict[str, Any] = {
        'quiet': True,
        'no_warnings': False,
        'extract_flat': True,
        'playlist_items': '1-20',
        'cookiefile': COOKIE_FILE,
        'logger': YtdlpLogger(),
        'extractor_args': {
            'youtube': {
                'player_client': ['android'],
                'player_skip': ['web', 'tv']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts_list) as ydl:  # type: ignore[arg-type]
            print(f" (Escaneando {original_name} con modo Android...) ", end="")
            channel_info = ydl.extract_info(search_url, download=False)

            vivos_temporales = []
            proximos_ts = []
            estados_detectados = defaultdict(int)
            descartados_por_estado = 0
            descartados_programados = 0
            probados_sin_estado = 0
            fallos_extraccion = 0
            sin_id = 0

            entries = list(cast(Iterable, channel_info.get('entries') or []))
            print(f"entradas={len(entries)}")

            backoff_delay = 0  # Backoff exponencial para rate limiting
            rate_limit_detected = False
            
            for idx, entry in enumerate(entries):
                title = entry.get('title', '')
                title_upper = title.upper()
                estado = (entry.get('live_status') or 'sin_estado').lower()
                estados_detectados[estado] += 1

                if estado == 'was_live' or estado == 'waslive':
                    print(f"      [youtube] 'was_live' detectado en entry: corte temprano en {original_name}")
                    break

                if estado == 'is_upcoming' or any(
                    word in title_upper for word in ["PROGRAMADO", "ESPERA", "PROXIMAMENTE", "PRÓXIMAMENTE"]
                ):
                    descartados_programados += 1
                    ts = entry.get('release_timestamp')
                    if ts:
                        proximos_ts.append(ts)
                    continue

                if estado not in ['is_live', 'sin_estado']:
                    descartados_por_estado += 1
                    continue

                if estado == 'sin_estado':
                    probados_sin_estado += 1

                v_id = entry.get('id') or entry.get('url')
                if not v_id:
                    sin_id += 1
                    print(f"      [youtube] Entrada sin id: {title[:80]}")
                    continue

                if str(v_id).startswith('http'):
                    video_url = str(v_id)
                else:
                    video_url = f"https://www.youtube.com/watch?v={v_id}"

                # Delay adaptativo: aumenta si se detecta rate limiting
                if rate_limit_detected:
                    backoff_delay = min(backoff_delay * 1.5, 30)  # Cap a 30 segundos
                else:
                    backoff_delay = 1.5 + (idx * 0.3)  # Incremento gradual: 1.5s, 1.8s, 2.1s...
                
                link_m3u8, video_status = extract_m3u8(video_url, original_name, delay=backoff_delay)

                if video_status == 'was_live' or video_status == 'waslive':
                    print(f"      [youtube] 'was_live' detectado tras extract_m3u8: corte temprano en {original_name}")
                    break

                if link_m3u8:
                    if "yt_live_broadcast" not in link_m3u8:
                        print("      [youtube] Link vivo sin marcador yt_live_broadcast; se acepta igual.")
                    vivos_temporales.append(link_m3u8)
                    rate_limit_detected = False  # Reset si fue exitoso
                else:
                    fallos_extraccion += 1

            if not vivos_temporales:
                if channel_url in cache:
                    del cache[channel_url]
                res_event = min(proximos_ts) if proximos_ts else None
                resumen_estados = ', '.join(f"{k}={v}" for k, v in sorted(estados_detectados.items())) or 'sin entradas'
                print(
                    "      [youtube] Sin vivos utiles. "
                    f"estados: {resumen_estados}; "
                    f"programados={descartados_programados}; "
                    f"probados_sin_estado={probados_sin_estado}; "
                    f"descartados_por_estado={descartados_por_estado}; "
                    f"sin_id={sin_id}; "
                    f"fallos_extraccion={fallos_extraccion}"
                )
                if res_event:
                    prox = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(res_event))
                    print(f"      [youtube] Proximo evento detectado: {prox}")
                return [], res_event, False

            vivos_finales = []
            total = len(vivos_temporales)
            for i, link in enumerate(vivos_temporales):
                nombre = original_name if total == 1 else f"{original_name} - Señal {i + 1}"
                vivos_finales.append({'name': nombre, 'link': link})

            res_event = min(proximos_ts) if proximos_ts else None
            expire = extract_expire_from_url(vivos_finales[0]['link']) if vivos_finales else None
            next_refresh = expire - 300 if expire else None

            cache[channel_url] = {
                'vivos': vivos_finales,
                'timestamp': time.time(),
                'expire': expire,
                'next_refresh': next_refresh,
                'next_event': res_event
            }

            return vivos_finales, res_event, False
    except Exception as e:
        tipo_error, detalle = describe_ytdlp_error(e)
        print(f" Error escaneando {original_name}: {tipo_error}: {detalle}")

        if tipo_error == 'rate_limit_429':
            global YOUTUBE_BLOCKED
            YOUTUBE_BLOCKED = True
            print(f"      [!] RATE LIMIT DETECTADO: Se marca YOUTUBE_BLOCKED y se detienen nuevos intentos YouTube")
            return [], None, True

        return [], None, False


def extract_m3u8(video_url, original_name=None, delay=0.0) -> Tuple[Optional[str], Optional[str]]:
    """Extrae el link directo .m3u8 si el video realmente esta en vivo."""
    # Aplicar delay antes de la solicitud para evitar rate limiting
    if delay > 0:
        time.sleep(delay)
    
    opts: dict[str, Any] = {
        'quiet': True,
        'no_warnings': False,
        'format': 'best[height=720]/best[height<=720]',
        'cookiefile': COOKIE_FILE,
        'logger': YtdlpLogger(),
        'extractor_args': {
            'youtube': {
                'player_client': ['android'],
                'player_skip': ['web', 'tv']
            }
        }
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(video_url, download=False)
            estado = info.get('live_status') or 'sin_estado'
            is_live = info.get('is_live') is True
            title = info.get('title') or video_url

            if estado != 'is_live' and not is_live:
                print(f"      [youtube] No es vivo: estado={estado}, is_live={info.get('is_live')}, titulo={title[:80]}")
                return None, estado

            direct_url = info.get('url')
            if not direct_url:
                formatos = len(info.get('formats') or [])
                canal = f" para {original_name}" if original_name else ""
                print(f"      [youtube] Sin URL directa{canal}: formatos={formatos}, keys={sorted(info.keys())[:12]}")
            return direct_url, estado
    except Exception as e:
        canal = f" para {original_name}" if original_name else ""
        tipo_error, detalle = describe_ytdlp_error(e)
        print(f"      [youtube] Extraccion omitida{canal}: {tipo_error}: {detalle}")
        return None, None


def is_link_online_pro(url, nombre_canal, tipo_canal="estatico", max_intentos=3, timeout=12):
    """
    Valida un stream.

    Returns:
        True si el stream esta online y activo, o la URL offline personalizada si falla.
    """
    BASE_ERROR_URL = "http://192.168.130.22:8097/error"

    nombre_limpio = re.sub(r'[^a-zA-Z0-9]', '_', nombre_canal).lower()
    url_error_personalizada = f"{BASE_ERROR_URL}/{nombre_limpio}/offline.m3u8"

    if not url or BASE_ERROR_URL in url:
        return url_error_personalizada

    if tipo_canal == "youtube":
        max_intentos = 2
        timeout = 15
        delays = [2 + random.random(), 4 + random.random()]
    else:
        delays = [random.uniform(1.5, 3.0) for _ in range(max_intentos)]

    headers = {
        'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 11; Pixel 5) VLC/3.3.4',
        'Range': 'bytes=0-1024',
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
        'Connection': 'keep-alive'
    }

    if tipo_canal == "youtube":
        headers['Accept-Language'] = 'es-AR,es;q=0.9,en;q=0.8'

    with requests.Session() as session:
        for intento in range(1, max_intentos + 1):
            try:
                r = session.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    stream=True,
                    verify=False,
                    allow_redirects=True
                )

                if r.status_code not in [200, 206]:
                    print(f"      [!] Intento {intento}: HTTP {r.status_code} en {tipo_canal}")
                    if intento < max_intentos:
                        delay = delays[intento - 1] if intento - 1 < len(delays) else random.uniform(1, 3)
                        time.sleep(delay)
                        continue
                    return url_error_personalizada

                es_finalizado = False
                if ".m3u8" in url.lower() or "manifest" in url.lower():
                    line_count = 0
                    try:
                        for line in r.iter_lines():
                            if line:
                                line_str = line.decode('utf-8', errors='ignore').upper()
                                if "#EXT-X-ENDLIST" in line_str:
                                    es_finalizado = True
                                    break
                            line_count += 1
                            if line_count > 60:
                                break
                    except Exception:
                        pass

                if es_finalizado:
                    print(f"      [!] Intento {intento}: Stream finalizado detectado")
                    if intento < max_intentos:
                        delay = delays[intento - 1] if intento - 1 < len(delays) else random.uniform(1, 3)
                        time.sleep(delay)
                        continue
                    return url_error_personalizada

                return True

            except (Timeout, ConnectionError) as e:
                error_type = "Timeout" if isinstance(e, Timeout) else "Conexion"
                print(f"      [!] Intento {intento}: Error de {error_type}")
                if intento < max_intentos:
                    delay = delays[intento - 1] if intento - 1 < len(delays) else random.uniform(1, 3)
                    time.sleep(delay)
                    continue
            except Exception as e:
                print(f"      [!] Intento {intento}: Error inesperado {str(e)[:40]}")
                if intento < max_intentos:
                    delay = delays[intento - 1] if intento - 1 < len(delays) else random.uniform(1, 3)
                    time.sleep(delay)
                    continue

    return url_error_personalizada


def parse_static_m3u(file_path):
    channels = []
    if not os.path.exists(file_path):
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        current = {}
        options = []
        for line in f:
            line = line.strip()
            if not line or line.startswith('#EXTM3U'):
                continue
            if line.startswith('#EXTINF'):
                get_val = lambda p, t: (re.search(p, t).group(1) if re.search(p, t) else "")
                current['tvg-id'] = get_val(r'tvg-id="([^"]+)"', line)
                current['tvg-logo'] = get_val(r'tvg-logo="([^"]+)"', line)
                current['group-title'] = get_val(r'group-title="([^"]+)"', line) or "Otros"
                current['name'] = line.split(',')[-1].strip()
                options = []
            elif line.startswith('#EXTVLCOPT'):
                options.append(line)
            elif line.startswith('http') and current:
                current['url'] = line
                current['options'] = options
                channels.append(current.copy())
                current = {}
                options = []
    return channels


# --- PROCESO PRINCIPAL ---

def main():
    combined_data = defaultdict(list)
    cache = load_cache()

    # 1. CANALES DINAMICOS
    if os.path.exists(INPUT_CHANNELS):
        print("Procesando canales de YouTube...")
        with open(INPUT_CHANNELS, 'r', encoding='utf-8') as f:
            h = None
            for line in f:
                line = line.strip()
                if not line or line.startswith('~~'):
                    continue

                if not line.startswith('http'):
                    p = [parts.strip() for parts in line.split('|')]
                    if len(p) >= 4:
                        h = {'name': p[0], 'group': p[1], 'logo': p[2], 'id': p[3]}
                    continue

                if h is None:
                    print(f"   [!] URL ignorada sin metadata previa: {line}")
                    continue

                wait_time = random.uniform(3, 6)  # Aumentado de 2-5 a 3-6 segundos
                print(f"   -> {h['name']} (esperando {wait_time:.1f}s...)", end=" ", flush=True)
                time.sleep(wait_time)

                if "youtube" in line:
                    if YOUTUBE_BLOCKED:
                        print("SKIP: YOUTUBE_BLOCKED activado, no se procesan más canales de YouTube")
                        url_error = is_link_online_pro(None, h['name'])
                        combined_data[h['group']].append({
                            'group-title': h['group'],
                            'tvg-id': h['id'],
                            'tvg-logo': h['logo'],
                            'name': h['name'],
                            'url': url_error,
                            'options': []
                        })
                        continue

                    vivos, next_ev, yt_rate_limited = get_youtube_data(line, cache, h['name'])
                    if yt_rate_limited:
                        print(f"   [!] YouTube rate limit detectado, se omitiran los siguientes canales YouTube")

                    if vivos:
                        total_vivos = len(vivos)
                        for idx, v in enumerate(vivos):
                            id_final = h['id'] if total_vivos == 1 else f"{h['id']} {idx + 1}"
                            resultado = is_link_online_pro(v['link'], v['name'], tipo_canal="youtube")
                            url_final = v['link'] if resultado is True else resultado

                            if resultado is True:
                                try:
                                    save_link_for_bridge(h['name'], v['link'])
                                except Exception as e:
                                    print(f"      [bridge] No se pudo guardar link para {h['name']}: {e}")

                            combined_data[h['group']].append({
                                'group-title': h['group'],
                                'tvg-id': id_final,
                                'tvg-logo': h['logo'],
                                'name': v['name'],
                                'url': url_final,
                                'options': []
                            })
                        print(f"OK {total_vivos} señales reales")
                    else:
                        try:
                            clear_link_for_bridge(h['name'])
                        except Exception as e:
                            print(f"      [bridge] No se pudo borrar link para {h['name']}: {e}")
                        url_error = is_link_online_pro(None, h['name'])
                        combined_data[h['group']].append({
                            'group-title': h['group'],
                            'tvg-id': h['id'],
                            'tvg-logo': h['logo'],
                            'name': h['name'],
                            'url': url_error,
                            'options': []
                        })
                        print("Offline / Proximamente")
                else:
                    resultado = is_link_online_pro(line, h['name'], tipo_canal="directo")
                    url_final = line if resultado is True else resultado
                    combined_data[h['group']].append({
                        'group-title': h['group'],
                        'tvg-id': h['id'],
                        'tvg-logo': h['logo'],
                        'name': h['name'],
                        'url': url_final,
                        'options': []
                    })
                    print("OK" if resultado is True else "Offline")
    else:
        print(f"No se encontro {INPUT_CHANNELS}; se procesaran solo canales estaticos.")

    # 2. CANALES ESTATICOS
    print("\nVerificando canales estaticos...")
    for chan in parse_static_m3u(STATIC_LIST):
        resultado = is_link_online_pro(chan['url'], chan['name'], tipo_canal="directo")

        if resultado is True:
            print(f"   -> {chan['name']}... OK")
        else:
            chan['url'] = resultado
            print(f"   -> {chan['name']}... Offline (Respaldo)")

        combined_data[chan['group-title']].append(chan)

    # 3. GUARDAR CACHE Y GENERAR M3U
    save_cache(cache)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U x-tvg-url="https://github.com/botallen/epg/releases/download/latest/epg.xml"\n')
        all_groups = GROUP_ORDER + [g for g in combined_data if g not in GROUP_ORDER]
        for group in all_groups:
            for c in combined_data[group]:
                f.write(f'#EXTINF:-1 group-title="{c["group-title"]}" tvg-id="{c["tvg-id"]}" tvg-logo="{c["tvg-logo"]}", {c["name"]}\n')
                for opt in c.get('options', []):
                    f.write(f'{opt}\n')
                f.write(f'{c["url"]}\n')

    end_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"\nProceso finalizado a las {end_time}.")


if __name__ == "__main__":
    main()