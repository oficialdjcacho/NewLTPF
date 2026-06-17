# manual_cache.py
import os
import json
import unicodedata

DATOS_DIR = os.path.join(".", "datos")
CACHE_FILE = os.path.join(DATOS_DIR, "manual_matches.json")

def _ensure_dir():
    os.makedirs(DATOS_DIR, exist_ok=True)

def _strip_accents(s: str) -> str:
    if not s:
        return s
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def _normalize_key(label: str) -> str:
    """
    Clave normalizada para ser robustos con mayúsculas, acentos y espacios.
    Se usa además de la clave exacta y del basename.
    """
    if not label:
        return ""
    s = label.replace("\\", "/").strip()
    s = os.path.basename(s) or s
    s = _strip_accents(s).lower().strip()
    return "norm|" + s

def _exact_key(label: str) -> str:
    s = label.strip()
    return "exact|" + s

def _basename_key(label: str) -> str:
    s = label.replace("\\", "/").strip()
    s = os.path.basename(s) or s
    return "base|" + s

def load_cache() -> dict:
    _ensure_dir()
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}

def save_cache(data: dict) -> None:
    _ensure_dir()
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def add_mapping(original_label: str, chosen_path: str) -> None:
    data = load_cache()
    data[_exact_key(original_label)] = chosen_path
    data[_basename_key(original_label)] = chosen_path
    data[_normalize_key(original_label)] = chosen_path
    save_cache(data)

def add_mappings(mapping: dict) -> None:
    """
    mapping: {original_label -> chosen_path}
    """
    data = load_cache()
    for k, v in mapping.items():
        data[_exact_key(k)] = v
        data[_basename_key(k)] = v
        data[_normalize_key(k)] = v
    save_cache(data)

def lookup(label: str) -> str | None:
    """
    Devuelve la ruta elegida manualmente si hay entrada en la caché,
    buscando por (1) exacto, (2) basename, (3) normalizado.
    """
    data = load_cache()
    for key in (_exact_key(label), _basename_key(label), _normalize_key(label)):
        if key in data:
            return data[key]
    return None
