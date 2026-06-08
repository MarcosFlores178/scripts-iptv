# /home/estranet/iptv-test/utils.py
import unicodedata
import re

def name_to_slug(name: str) -> str:
    """Convierte cualquier nombre a slug ASCII seguro"""
    normalized = unicodedata.normalize('NFKD', name)
    ascii_text = normalized.encode('ascii', 'ignore').decode('ascii')
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', ascii_text.strip().lower())
    slug = re.sub(r'__+', '_', slug)
    return slug.strip('_')