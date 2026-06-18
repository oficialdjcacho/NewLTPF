# matcher.py
import difflib
import os
import sqlite3
import logging
import hashlib
import unicodedata
import json
from datetime import datetime
from multiprocessing import Process, Manager, cpu_count
from math import ceil
import re
import time
from typing import Optional, List, Tuple

import manual_cache  # caché manual (manual_cache.py)

# ============================================================================
# ⚙️ Modos de selección
# ============================================================================
STRICT_ARTIST_MATCH = True   # exige artista compatible (tag o filename) si hay artista en la entrada

# Usar todos los núcleos disponibles del procesador
DEFAULT_MAX_WORKERS = max(1, cpu_count())

NETSEARCH_PREFIXES = ("netsearch://td",)  # Tidal ids (puedes añadir más si usas otros servicios)

# =============================================================================
# 🔧 Carga de alias desde aliasconfig.json
# =============================================================================

def _cargar_alias_desde_json():
    posibles = [
        os.path.join("datos", "aliasconfig.json"),
        os.path.join(".", "aliasconfig.json"),
    ]
    for ruta in posibles:
        try:
            if os.path.exists(ruta):
                with open(ruta, "r", encoding="utf-8") as f:
                    data = json.load(f)
                artist_alias = data.get("artist_alias", {}) or {}
                rules = data.get("rules", {}) or {}
                norm = {}
                for k, v in artist_alias.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        nk = unicodedata.normalize('NFKD', k).encode('ASCII', 'ignore').decode('ASCII').lower().strip()
                        nv = unicodedata.normalize('NFKD', v).encode('ASCII', 'ignore').decode('ASCII').lower().strip()
                        norm[nk] = nv
                return norm, (rules or {})
        except Exception as e:
            logging.warning(f"No se pudo leer {ruta}: {e}")
    return {}, {"normalize_diminutives": True}

ALIAS_ARTIST, ALIAS_RULES = _cargar_alias_desde_json()

# =============================================================================
# 🔧 Normalización y helpers
# =============================================================================

RUIDO_TITULO = [
    r'\bofficial\s*music\s*video\b', r'\bofficial\s*video\b', r'\bofficial\b',
    r'\bvideo\b', r'\baudio\b', r'\blyrics?\b', r'\bcover\b', r'\bmv\b',
    r'\bhd\b', r'\b4k\b', r'\bremaster(?:ed)?\b', r'\b(clip|videoclip)\b',
    r'\bvevo\b', r'videodj\s*ralph',
    r'\bfeat(?:\.|uring)?\b', r'\bcon\b'  # ↓ reducimos ruido de “feat.” en títulos para igualar mejor
]
RUIDO_REGEX = re.compile('|'.join(RUIDO_TITULO), re.IGNORECASE)

REMIX_REGEX = re.compile(
    r'\b(remix|re-?edit|extended(?:\s+mix)?|radio\s+mix|bootleg|rework|mashup|version|edit|intro)\b',
    re.IGNORECASE
)

def quitar_acentos(s: str) -> str:
    if not s: return s
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))

def limpiar_parentesis_y_corchetes(s: str) -> str:
    if not s: return s
    antes, res = None, s
    while antes != res:
        antes = res
        res = re.sub(r'\s*[\(\[].*?[\)\]]\s*', ' ', res)
    return res

def limpiar_ruido(s: str) -> str:
    if not s: return s
    s2 = s.replace('_', ' ')
    s2 = limpiar_parentesis_y_corchetes(s2)
    s2 = RUIDO_REGEX.sub(' ', s2)
    s2 = re.sub(r'\s+', ' ', s2).strip(' .-_')
    return s2

def normalizar(s: str) -> str:
    if not s: return ''
    s2 = quitar_acentos(s)
    s2 = limpiar_ruido(s2)
    return s2.lower().strip()

def _aplicar_reglas_genericas_artista(ns: str) -> str:
    return ns

def normalizar_artista(s: str) -> str:
    ns = normalizar(s)
    if not ns: return ns
    if ns in ALIAS_ARTIST: return ALIAS_ARTIST[ns]
    return _aplicar_reglas_genericas_artista(ns)

def tokens(s: str):
    return [t for t in re.split(r'[^a-z0-9]+', normalizar(s)) if len(t) >= 4]

def hay_solape_tokens(a: str, b: str) -> bool:
    ta, tb = set(tokens(a)), set(tokens(b))
    return len(ta.intersection(tb)) > 0

def sim_nombre(a: str, b: str) -> float:
    na, nb = normalizar(a), normalizar(b)
    if not hay_solape_tokens(a, b): return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio() * 100.0

def sim_titulo_suave(a: str, b: str) -> float:
    """Similitud de títulos ignorando tokens y sin exigir solape (para Tidal ids)."""
    na, nb = normalizar(a or ''), normalizar(b or '')
    if not na or not nb: return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio() * 100.0

def partir_artista_titulo_desde_nombre(nombre: str):
    base = limpiar_ruido(os.path.splitext(os.path.basename(nombre))[0])
    if ' - ' in base:
        artista, titulo = base.split(' - ', 1)
        return artista.strip(), titulo.strip()
    return None, base.strip()

def _titulo_puro(tags_dict: dict) -> str:
    t = (tags_dict or {}).get("title") or ""
    if not t: return ""
    _, puro = partir_artista_titulo_desde_nombre(t)
    return puro or t

def canon_title(s: str) -> str:
    s2 = normalizar(s or "")
    s2 = s2.replace("-", " ")
    s2 = re.sub(r"[^a-z0-9]+", " ", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2

def titulos_equivalentes(a: str, b: str) -> bool:
    return bool(canon_title(a) and canon_title(b)) and canon_title(a) == canon_title(b)

ARTISTA_BASURA = {
    "videodj ralph", "video dj ralph", "varios", "various",
    "unknown", "unknown artist", "desconocido", "sin artista", "dj", "v/a"
}

def _artista_con_fallback(tags_dict: dict) -> str:
    a = (tags_dict or {}).get("artist") or ""
    na = normalizar_artista(a)
    if not na or na in ARTISTA_BASURA or re.search(r"\bvideodj\s*ralph\b", na):
        titulo = (tags_dict or {}).get("title") or ""
        arti_tit, _ = partir_artista_titulo_desde_nombre(titulo)
        if arti_tit: return normalizar_artista(arti_tit)
    return na

def _es_remix(titulo: str) -> bool:
    return bool(REMIX_REGEX.search(titulo or ""))

def _artista_en_filename(path: str, artist_norm: str) -> bool:
    if not path or not artist_norm: return False
    fname = normalizar(os.path.basename(path))
    for tok in tokens(artist_norm):
        if tok and tok in fname:
            return True
    return False

def _filename_es_compuesto_ajeno(path: str, expected_title: str) -> bool:
    if not path: return False
    base = os.path.splitext(os.path.basename(path))[0]
    base_clean = limpiar_ruido(base)
    parts = [p.strip() for p in base_clean.split(' - ') if p.strip()]
    if len(parts) <= 2:
        return False
    extra = " ".join(parts[2:])
    t_expected = set(tokens(expected_title))
    t_extra = set(tokens(extra))
    return bool(t_extra) and not t_extra.issubset(t_expected)

def _es_netsearch_id(ruta: str) -> bool:
    if not ruta: return False
    rl = ruta.lower()
    if rl.startswith(NETSEARCH_PREFIXES):
        return True
    base = os.path.basename(ruta)
    return bool(re.fullmatch(r"td\d{5,}", base.lower()))

def _split_artistas_compuesto(s: str) -> List[str]:
    s = s or ""
    s = re.sub(r'\bfeat(?:\.|uring)?\b', '&', s, flags=re.IGNORECASE)
    s = re.sub(r'\bcon\b', '&', s, flags=re.IGNORECASE)
    parts = re.split(r'[,&/;+]+', s)
    out = []
    for p in parts:
        p = normalizar_artista(p.strip())
        if p:
            out.append(p)
    return out

def artistas_compatibles(expected_norm: str, candidate_norm: str) -> bool:
    if not expected_norm or not candidate_norm:
        return True
    exp_parts = set(_split_artistas_compuesto(expected_norm))
    cand_parts = set(_split_artistas_compuesto(candidate_norm))
    if not exp_parts or not cand_parts:
        return expected_norm == candidate_norm
    # compatible si comparten alguna parte significativa
    return len(exp_parts.intersection(cand_parts)) > 0

# === Helpers caché manual =====================================================

def _labels_para_cache_manual(entrada: dict) -> List[str]:
    labels = []
    ruta = entrada.get("ruta") or ""
    if ruta:
        labels.append(ruta)
        base = os.path.basename(ruta)
        if base and base != ruta:
            labels.append(base)
    tags = entrada.get("tags") or {}
    a = tags.get("artist") or ""
    t = tags.get("title") or ""
    if a or t:
        at = f"{a} - {t}".strip(" -")
        if at:
            labels.append(at)
    seen, uniq = set(), []
    for lbl in labels:
        if lbl not in seen:
            uniq.append(lbl); seen.add(lbl)
    return uniq

# =============================================================================
# 💾 Índice + caché en RAM (SQLite) — lectura
# =============================================================================

def cargar_conexion_en_memoria():
    """Copia datos/coincidencias.db a :memory: para LECTURA (índice + cache previa)."""
    try:
        ruta_db = os.path.join("datos", "coincidencias.db")
        if not os.path.exists(ruta_db):
            return None
        mem_conn = sqlite3.connect(":memory:")
        disk_conn = sqlite3.connect(ruta_db)
        disk_conn.backup(mem_conn)  # solo lectura en workers
        disk_conn.close()
        return mem_conn
    except Exception as e:
        logging.warning(f"Error cargando base de datos en RAM: {e}")
        return None

def cargar_indice_desde_sqlite(mem_conn):
    try:
        cursor = mem_conn.cursor()
        cursor.execute("SELECT path, title, artist, duration, bitrate, auto_tags FROM indice_audio")
        rows = cursor.fetchall()
        indice_sqlite = []
        for path, title, artist, duration, bitrate, auto_tags in rows:
            indice_sqlite.append({
                "path": path,
                "tags": {
                    "title": title, "artist": artist, "duration": duration,
                    "bitrate": bitrate, "auto_tags": bool(auto_tags)
                }
            })
        return indice_sqlite
    except Exception:
        try:
            ruta_json = None
            for f in os.listdir("datos"):
                if f.startswith("mp3_index_") and f.endswith(".json"):
                    ruta_json = os.path.join("datos", f)
                    break
            if ruta_json:
                with open(ruta_json, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
                return data
        except Exception as e:
            logging.error(f"No se pudo cargar índice: {e}")
        return []

# =============================================================================
# 🧱 Caché (tablas) — utilidades
# =============================================================================

def _ensure_cache_tables(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cache_coincidencias (
            entrada TEXT PRIMARY KEY,
            ruta_resultado TEXT,
            bitrate INTEGER,
            timestamp TEXT
        )
    """)
    conn.commit()

def buscar_en_cache(conn, clave):
    if not conn: return None
    _ensure_cache_tables(conn)
    cur = conn.cursor()
    try:
        cur.execute("SELECT ruta_resultado FROM cache_coincidencias WHERE entrada = ?", (clave,))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None

# === Caché aprendida: claves genéricas =======================================

def _duration_bucket(d):
    if d is None: return ""
    try:
        return str(int(round(float(d) / 2.0) * 2))  # bucket de 2s
    except Exception:
        return ""

def _basename_norm(ruta):
    try:
        return normalizar(os.path.basename(ruta or "")).strip()
    except Exception:
        return ""

def _claves_aprendizaje_para_entrada(entrada: dict) -> List[str]:
    """
    Claves reutilizables:
      - base|<basename_normalizado>
      - at|<artist_norm>|<canon_title>|<bucket_duracion>
      - t|<canon_title>|<bucket_duracion>   (si el artista es dudoso)
    """
    claves = []
    ruta = entrada.get("ruta") or ""
    if ruta:
        b = _basename_norm(ruta)
        if b:
            claves.append(f"base|{b}")
    tags = entrada.get("tags") or {}
    t_puro = _titulo_puro(tags)
    a_norm = _artista_con_fallback(tags)
    a_norm = normalizar_artista(a_norm)
    ctitle = canon_title(t_puro)
    dbuck = _duration_bucket(tags.get("duration"))
    if ctitle:
        if a_norm:
            claves.append(f"at|{a_norm}|{ctitle}|{dbuck}")
        claves.append(f"t|{ctitle}|{dbuck}")
    # únicos en orden
    seen, out = set(), []
    for k in claves:
        if k not in seen:
            out.append(k); seen.add(k)
    return out

def _buscar_en_cache_aprendida(conn, entrada):
    if not conn: return None
    _ensure_cache_tables(conn)
    cur = conn.cursor()
    for k in _claves_aprendizaje_para_entrada(entrada):
        try:
            cur.execute("SELECT ruta_resultado FROM cache_coincidencias WHERE entrada = ?", (k,))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    return None

# =============================================================================
# Validación y utilidades
# =============================================================================

def validar_cache_contra_entrada(indice_sqlite, ruta_cache, entrada, umbral=80):
    try:
        ruta_cache_norm = os.path.normpath(ruta_cache)
        elem = next((x for x in indice_sqlite if os.path.normpath(x["path"]) == ruta_cache_norm), None)
        if not elem: return False
        tags_ent = entrada.get("tags")
        if tags_ent and (tags_ent.get("title") or tags_ent.get("artist")):
            t1 = normalizar(tags_ent.get("title", ""))
            a1 = normalizar_artista(tags_ent.get("artist", ""))
            t2 = normalizar(elem["tags"].get("title", ""))
            a2 = normalizar_artista(elem["tags"].get("artist", ""))
            score_t = difflib.SequenceMatcher(None, t1, t2).ratio() * 100 if t1 and t2 else 0
            # toleramos artistas compuestos
            a_ok = artistas_compatibles(a1, a2)
            score_a = 100.0 if a_ok else (difflib.SequenceMatcher(None, a1, a2).ratio() * 100 if a1 and a2 else 0)
            score = (score_t * 0.6 + score_a * 0.4)
            return score >= umbral
        else:
            nombre_in = os.path.basename(entrada["ruta"])
            nombre_cache = os.path.basename(ruta_cache_norm)
            score = sim_nombre(nombre_in, nombre_cache)
            return score >= umbral
    except Exception:
        return False

def bitrate_por_path(indice_sqlite, path):
    try:
        pnorm = os.path.normpath(path)
        elem = next((x for x in indice_sqlite if os.path.normpath(x["path"]) == pnorm), None)
        if not elem: return 0
        return int(elem.get("tags", {}).get("bitrate", 0) or 0)
    except Exception:
        return 0

def _bitrate_por_path_lookup(path_lookup, indice_sqlite, path):
    try:
        elem = path_lookup.get(os.path.normpath(path or ""))
        if elem:
            return int((elem.get("tags") or {}).get("bitrate", 0) or 0)
    except Exception:
        pass
    return bitrate_por_path(indice_sqlite, path)

def _duration_buckets_for_match(duration, tolerancia=3):
    if duration is None:
        return [""]
    buckets = []
    try:
        d = float(duration)
        step = 2
        span = int(tolerancia) + step
        for offset in range(-span, span + 1, step):
            b = _duration_bucket(d + offset)
            if b not in buckets:
                buckets.append(b)
    except Exception:
        buckets.append(_duration_bucket(duration))
    return buckets or [""]

def _quality_keys_for_tags(tags_dict, tolerancia=3):
    tags_dict = tags_dict or {}
    title = _titulo_puro(tags_dict)
    ctitle = canon_title(title)
    if not ctitle:
        return []
    artist = _artista_con_fallback(tags_dict)
    artist = normalizar_artista(artist)
    remix = bool(_es_remix(title))
    buckets = _duration_buckets_for_match(tags_dict.get("duration"), tolerancia=tolerancia)
    keys = []
    for bucket in buckets:
        if artist:
            keys.append(("artist-title-duration-remix", artist, ctitle, bucket, remix))
        keys.append(("title-duration-remix", "", ctitle, bucket, remix))
    seen, out = set(), []
    for key in keys:
        if key not in seen:
            out.append(key)
            seen.add(key)
    return out

def _build_quality_caches(indice_sqlite):
    """
    Agrupa pistas equivalentes y conserva solo la de mayor bitrate por identidad.
    La identidad incluye titulo canonico, artista normalizado, bucket de duracion y
    si parece remix, para no mezclar versiones distintas con el mismo nombre.
    """
    path_lookup = {}
    best_all = {}
    best_noauto = {}

    def _keep_best(cache, key, item):
        old = cache.get(key)
        old_b = int(((old or {}).get("tags") or {}).get("bitrate", 0) or 0)
        new_b = int(((item or {}).get("tags") or {}).get("bitrate", 0) or 0)
        if old is None or new_b > old_b:
            cache[key] = item

    for item in indice_sqlite or []:
        path = item.get("path") or ""
        if path:
            path_lookup[os.path.normpath(path)] = item
        tags = item.get("tags") or {}
        for key in _quality_keys_for_tags(tags, tolerancia=0):
            _keep_best(best_all, key, item)
            if not tags.get("auto_tags", False):
                _keep_best(best_noauto, key, item)
    return {"path": path_lookup, "best_all": best_all, "best_noauto": best_noauto}

def _best_quality_from_cache(quality_cache, base_tags, prefer_no_auto=True, tolerancia=3):
    if not quality_cache:
        return None
    keys = _quality_keys_for_tags(base_tags or {}, tolerancia=tolerancia)
    if not keys:
        return None
    caches = []
    if prefer_no_auto:
        caches.append(quality_cache.get("best_noauto") or {})
    caches.append(quality_cache.get("best_all") or {})
    for cache in caches:
        for key in keys:
            item = cache.get(key)
            if item and tags_similares(base_tags or {}, item.get("tags") or {}, tolerancia_duracion=tolerancia):
                return item
    return None

def _add_token_refs(target, token_values, item, wanted_tokens=None):
    seen = set()
    for tok in token_values or []:
        if not tok or tok in seen:
            continue
        if wanted_tokens is not None and tok not in wanted_tokens:
            continue
        seen.add(tok)
        target.setdefault(tok, []).append(item)

def _build_candidate_cache(indice_sqlite, wanted_tokens=None):
    title = {}
    artist = {}
    name = {}
    for item in indice_sqlite or []:
        tags = item.get("tags") or {}
        path = item.get("path") or ""
        _add_token_refs(title, tokens(_titulo_puro(tags) or tags.get("title") or ""), item, wanted_tokens=wanted_tokens)
        _add_token_refs(artist, tokens(normalizar_artista(tags.get("artist") or "")), item, wanted_tokens=wanted_tokens)
        _add_token_refs(name, tokens(os.path.splitext(os.path.basename(path))[0]), item, wanted_tokens=wanted_tokens)
    return {"title": title, "artist": artist, "name": name, "all": indice_sqlite or []}

def _candidate_subset_from_tokens(candidate_cache, groups, require_all_groups=False):
    if not candidate_cache:
        return candidate_cache.get("all") if candidate_cache else []
    selected = []
    seen_paths = set()
    group_sets = []
    for group_name, token_values in groups:
        group = candidate_cache.get(group_name) or {}
        group_paths = set()
        for tok in token_values or []:
            for item in group.get(tok, []):
                path = item.get("path") or id(item)
                group_paths.add(path)
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                selected.append(item)
        if token_values:
            group_sets.append(group_paths)
    if require_all_groups and len(group_sets) > 1:
        common = set.intersection(*group_sets)
        if common:
            return [item for item in selected if (item.get("path") or id(item)) in common]
    return selected

def _candidate_subset_from_sqlite(mem_conn, path_lookup, groups, require_all_groups=False):
    if mem_conn is None or not path_lookup:
        return []
    selected = []
    seen_paths = set()
    group_sets = []
    try:
        cur = mem_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='track_tokens'")
        if cur.fetchone() is None:
            return []
        for token_type, token_values in groups:
            uniq_tokens = sorted({t for t in (token_values or []) if t})
            if not uniq_tokens:
                continue
            group_paths = set()
            placeholders = ",".join("?" for _ in uniq_tokens)
            cur.execute(
                f"""
                SELECT DISTINCT ia.path
                FROM track_tokens tt
                JOIN indice_audio ia ON ia.id = tt.track_id
                WHERE tt.token_type = ? AND tt.token IN ({placeholders})
                """,
                [token_type] + uniq_tokens,
            )
            for (path,) in cur.fetchall():
                norm = os.path.normpath(path or "")
                group_paths.add(norm)
                if not norm or norm in seen_paths:
                    continue
                item = path_lookup.get(norm)
                if item:
                    seen_paths.add(norm)
                    selected.append(item)
            group_sets.append(group_paths)
    except Exception:
        return []
    if require_all_groups and len(group_sets) > 1:
        common = set.intersection(*group_sets)
        if common:
            return [item for item in selected if os.path.normpath(item.get("path") or "") in common]
    return selected

def _sqlite_token_index_ready(mem_conn) -> bool:
    try:
        cur = mem_conn.cursor()
        cur.execute("SELECT value FROM index_meta WHERE key='token_index_version'")
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return False

def _entry_perf_label(entrada):
    tags = (entrada or {}).get("tags") or {}
    artist = tags.get("artist") or ""
    title = tags.get("title") or ""
    if artist or title:
        return f"{artist} - {title}".strip(" -")
    ruta = (entrada or {}).get("ruta") or ""
    return os.path.basename(ruta) or ruta

def _wanted_tokens_for_block(bloque):
    wanted = set()
    for entrada in bloque or []:
        ruta = (entrada or {}).get("ruta") or ""
        tags = (entrada or {}).get("tags") or {}
        for tok in tokens(_titulo_puro(tags) or tags.get("title") or ""):
            wanted.add(tok)
        for tok in tokens(_artista_con_fallback(tags)):
            wanted.add(tok)
        for tok in tokens(os.path.basename(ruta)):
            wanted.add(tok)
    return wanted

# =============================================================================
# 🎯 Scoring por tags + igualdad estricta
# =============================================================================

def calcular_puntaje(tags1, tags2):
    w_title, w_artist, w_dur = 50.0, 30.0, 20.0

    t1p = _titulo_puro(tags1)
    t2  = (tags2 or {}).get("title") or ""
    a1f = _artista_con_fallback(tags1)
    a2f = normalizar_artista((tags2 or {}).get("artist") or "")
    d1  = (tags1 or {}).get("duration")
    d2  = (tags2 or {}).get("duration")

    title_overlap = hay_solape_tokens(t1p, t2)
    artist_tokens_ok = len(tokens(a1f)) > 0 and len(tokens(a2f)) > 0

    if not title_overlap and artist_tokens_ok:
        w_title, w_artist, w_dur = 5.0, 70.0, 25.0
    elif title_overlap and not artist_tokens_ok:
        w_title, w_artist, w_dur = 70.0, 5.0, 25.0

    puntaje = 0.0
    if title_overlap:
        puntaje += difflib.SequenceMatcher(None, normalizar(t1p), normalizar(t2)).ratio() * w_title
    if a1f and a2f:
        # artistas compuestos compatibles
        if artistas_compatibles(a1f, a2f):
            puntaje += w_artist
        else:
            puntaje += difflib.SequenceMatcher(None, a1f, a2f).ratio() * w_artist
    if d1 is not None and d2 is not None:
        dur_diff = abs(float(d1) - float(d2))
        puntaje += max(0.0, (20.0 - dur_diff)) * (w_dur / 20.0)
    return puntaje

def tags_similares(tags1, tags2, tolerancia_duracion=3):
    if not tags1 or not tags2: return False
    t1, a1, d1 = tags1.get("title"), tags1.get("artist"), tags1.get("duration")
    t2, a2, d2 = tags2.get("title"), tags2.get("artist"), tags2.get("duration")
    if not t1 or not t2 or d1 is None or d2 is None: return False
    artista_ok = True
    if a1 and a2:
        artista_ok = artistas_compatibles(normalizar_artista(a1), normalizar_artista(a2))
    return (titulos_equivalentes(t1, t2) and artista_ok and abs(d1 - d2) <= tolerancia_duracion)

def _preferir_no_remix(candidatos):
    if not candidatos: return candidatos
    no_remix = [c for c in candidatos if not _es_remix(c.get("tags", {}).get("title", ""))]
    return no_remix or candidatos

def _filtrar_por_artista_esperado(candidatos, artist_esperado_norm):
    if not artist_esperado_norm: return candidatos
    compatibles = []
    for c in candidatos:
        a = normalizar_artista((c.get("tags") or {}).get("artist", ""))
        ok = artistas_compatibles(artist_esperado_norm, a) or _artista_en_filename(c.get("path", ""), artist_esperado_norm)
        if ok: compatibles.append(c)
    if STRICT_ARTIST_MATCH and artist_esperado_norm:
        return compatibles
    return compatibles or candidatos

# =============================================================================
# 🔁 Upgrade explícito por alias / filename si tags son pobres
# =============================================================================

def _upgrade_por_alias(indice_sqlite, base_title: str, base_artist: str, base_duration,
                       prefer_no_auto=True, tolerancia_duracion=3, search_space=None):
    if not base_title or not base_artist: return None
    ctitle = canon_title(base_title)
    cartist = normalizar_artista(base_artist)
    if not ctitle or not cartist: return None

    candidatos = []
    for x in (search_space if search_space is not None else indice_sqlite):
        xtags = (x.get("tags") or {})
        t = xtags.get("title") or ""
        a = xtags.get("artist") or ""
        d = xtags.get("duration")
        ok_title = titulos_equivalentes(t, ctitle)
        ok_artist = artistas_compatibles(cartist, normalizar_artista(a)) if a else False
        if not ok_artist and ok_title:
            fname = os.path.basename(x.get("path") or "")
            ok_artist = cartist in normalizar(fname)
        if ok_title and ok_artist:
            if base_duration is None or d is None or abs(float(base_duration) - float(d)) <= float(tolerancia_duracion):
                candidatos.append(x)

    if not candidatos: return None
    if prefer_no_auto:
        noauto = [c for c in candidatos if not c["tags"].get("auto_tags", False)]
        candidatos = noauto or candidatos
    candidatos = _preferir_no_remix(candidatos)
    return max(candidatos, key=lambda x: x.get("tags", {}).get("bitrate", 0))

# =============================================================================
# 🧩 Procesamiento en bloques paralelos (workers SIN escrituras)
#      + Caché local por worker (opción 1)
# =============================================================================

def procesar_bloque_con_orden(
    bloque, resultados_dict, no_encontradas_dict, progreso, threshold, bloque_id,
    log_queue, seq_lock, updates_list, solo_bitrate_bajo=False, bitrate_minimo=320,
    bitrate_overrides=None, stats_list=None
):
    # --- RAM: BD en memoria + índice en memoria (LECTURA) ---
    mem_conn = cargar_conexion_en_memoria()
    indice_sqlite = cargar_indice_desde_sqlite(mem_conn)
    quality_cache = _build_quality_caches(indice_sqlite)
    path_lookup = quality_cache.get("path", {})
    sqlite_tokens_ready = _sqlite_token_index_ready(mem_conn)
    candidate_cache = None
    wanted_candidate_tokens = _wanted_tokens_for_block(bloque)
    bitrate_overrides = bitrate_overrides or {}

    # --- RAM: cache manual precargada UNA vez por proceso ---
    _manual_map = manual_cache.load_cache()

    # --- Caché local por worker (exacta y aprendida) ---
    local_cache_exact: dict[str, Tuple[str, int]] = {}
    local_cache_generic: dict[str, Tuple[str, int]] = {}

    def _get_candidate_cache():
        nonlocal candidate_cache
        if candidate_cache is None:
            candidate_cache = _build_candidate_cache(indice_sqlite, wanted_tokens=wanted_candidate_tokens)
        return candidate_cache

    def _search_space_for_tags(base_tags):
        if not base_tags:
            return indice_sqlite
        groups = [
            ("title", tokens(_titulo_puro(base_tags) or base_tags.get("title") or "")),
            ("artist", tokens(_artista_con_fallback(base_tags))),
        ]
        has_query_tokens = any(values for _, values in groups)
        candidates = _candidate_subset_from_sqlite(mem_conn, path_lookup, groups, require_all_groups=True)
        if not candidates and not sqlite_tokens_ready:
            candidates = _candidate_subset_from_tokens(_get_candidate_cache(), groups, require_all_groups=True)
        if not candidates and (not sqlite_tokens_ready or not has_query_tokens):
            return indice_sqlite
        return candidates

    def _manual_lookup_cached(label: str) -> Optional[str]:
        for k in (
            manual_cache._exact_key(label),
            manual_cache._basename_key(label),
            manual_cache._normalize_key(label),
        ):
            if k in _manual_map:
                return _manual_map[k]
        return None

    def _local_update(entrada: dict, ruta: str, bitrate: int):
        """Guarda en la caché local exacta y genérica del worker."""
        try:
            local_cache_exact[generar_clave_entrada(entrada)] = (ruta, int(bitrate or 0))
        except Exception:
            local_cache_exact[generar_clave_entrada(entrada)] = (ruta, 0)
        for k in _claves_aprendizaje_para_entrada(entrada):
            try:
                local_cache_generic[k] = (ruta, int(bitrate or 0))
            except Exception:
                local_cache_generic[k] = (ruta, 0)

    def _local_lookup(entrada: dict) -> Tuple[Optional[str], Optional[str]]:
        """Devuelve (ruta, tipo) desde la caché local del worker, si existe y es válida."""
        # exacta
        k_exact = generar_clave_entrada(entrada)
        if k_exact in local_cache_exact:
            return local_cache_exact[k_exact][0], "local-exacta"
        # genérica
        for k in _claves_aprendizaje_para_entrada(entrada):
            if k in local_cache_generic:
                return local_cache_generic[k][0], "local-aprendida"
        return None, None

    def _record_stat(seq, phase, found, elapsed, full_scan=False, entrada=None, result_path="", candidate_count=None, detail=""):
        if stats_list is None:
            return
        try:
            stats_list.append({
                "seq": int(seq),
                "phase": str(phase),
                "found": bool(found),
                "elapsed": float(elapsed),
                "full_scan": bool(full_scan),
                "index_size": len(indice_sqlite),
                "candidate_count": int(candidate_count if candidate_count is not None else len(indice_sqlite)),
                "label": _entry_perf_label(entrada) if entrada else "",
                "ruta": (entrada or {}).get("ruta", "") if entrada else "",
                "result_path": result_path or "",
                "detail": detail or "",
            })
        except Exception:
            pass

    bloque_resultados, bloque_no_encontradas = [], []

    for entrada in bloque:
        with seq_lock:
            seq = progreso.value + 1
            progreso.value = seq

        start_t = time.perf_counter()
        log_lines = [f"\n─── Entrada {seq} ─────────────────────────────────────────"]
        ruta = entrada["ruta"]
        tags = entrada.get("tags")
        clave_exacta = generar_clave_entrada(entrada)

        if tags: log_lines.append(f"     🎧 Tags: {tags}")
        else:    log_lines.append(f"     📁 Archivo: {os.path.basename(ruta)}")

        # -------- Fast path: la ruta original ya existe y no hace falta buscar alternativa ----------
        if ruta and os.path.exists(ruta) and not _es_netsearch_id(ruta):
            bsel = int(bitrate_overrides.get(os.path.normpath(ruta), 0) or _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta) or 0)
            if (not solo_bitrate_bajo) or int(bsel or 0) >= int(bitrate_minimo or 0):
                bloque_resultados.append(ruta)
                _local_update(entrada, ruta, bsel or 0)
                _acc_update(updates_list, clave_exacta, ruta, bsel or 0)
                for k in _claves_aprendizaje_para_entrada(entrada):
                    _acc_update(updates_list, k, ruta, bsel or 0)
                log_lines.append(
                    f"     ⚡ Ruta válida conservada sin búsqueda: {ruta} "
                    f"(bitrate={fmt_bitrate(bsel)})"
                )
                elapsed = time.perf_counter() - start_t
                _record_stat(seq, "ruta_valida", True, elapsed, full_scan=False)
                log_lines.append(f"     🕒 Tiempo: {elapsed:.2f} s")
                log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": True})
                continue
            log_lines.append(
                f"     🔎 Ruta válida con bitrate bajo: {ruta} "
                f"(bitrate={fmt_bitrate(bsel)}) → se buscará alternativa"
            )

        # -------- Fase 0: Caché manual (prioritaria, en RAM) ----------
        ruta_cache_manual = None
        for lbl in _labels_para_cache_manual(entrada):
            cand = _manual_lookup_cached(lbl)
            if not cand:
                continue
            cand_norm = os.path.normpath(cand)
            existe_en_indice = cand_norm in path_lookup
            if not existe_en_indice and not os.path.exists(cand_norm):
                continue

            es_valida = True
            try:
                if tags and (tags.get("title") or tags.get("artist")):
                    es_valida = validar_cache_contra_entrada(indice_sqlite, cand_norm, entrada, umbral=50)
                else:
                    base_in = os.path.basename(ruta)
                    base_c  = os.path.basename(cand_norm)
                    r = difflib.SequenceMatcher(None, normalizar(base_in), normalizar(base_c)).ratio()
                    es_valida = (r * 100) >= 50
            except Exception:
                es_valida = True

            if es_valida:
                ruta_cache_manual = cand_norm
                break
            else:
                log_lines.append(f"     ⚠️ Caché manual descartada por baja similitud: {cand_norm}")

        if ruta_cache_manual:
            bsel = _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta_cache_manual)
            base_tags = entrada.get("tags") or {}
            mejor_cache = None
            if base_tags.get("title") or base_tags.get("artist"):
                mejor_cache = _mejor_por_similares(indice_sqlite, {
                    "title": _titulo_puro(base_tags),
                    "artist": _artista_con_fallback(base_tags),
                    "duration": base_tags.get("duration")
                }, prefer_no_auto=True, quality_cache=quality_cache, search_space=_search_space_for_tags(base_tags))

            if mejor_cache:
                b_new = mejor_cache["tags"].get("bitrate", 0)
                if b_new > bsel:
                    ruta_cache_manual = mejor_cache["path"]
                    bsel = b_new
                    log_lines.append(f"     ✅ Mejorado por similares (desde caché manual): {ruta_cache_manual} (bitrate={fmt_bitrate(bsel)})")
                else:
                    log_lines.append(f"     🧠 Coincidencia por caché manual: {ruta_cache_manual} (bitrate={fmt_bitrate(bsel)})")
            else:
                log_lines.append(f"     🧠 Coincidencia por caché manual: {ruta_cache_manual} (bitrate={fmt_bitrate(bsel)})")

            # Actualizar cachés: local (worker) + acumulador global
            _local_update(entrada, ruta_cache_manual, bsel or 0)
            _acc_update(updates_list, clave_exacta, ruta_cache_manual, bsel or 0)
            for k in _claves_aprendizaje_para_entrada(entrada):
                _acc_update(updates_list, k, ruta_cache_manual, bsel or 0)

            bloque_resultados.append(ruta_cache_manual)
            elapsed = time.perf_counter() - start_t
            _record_stat(seq, "cache_manual", True, elapsed, full_scan=False)
            log_lines.append(f"     🕒 Tiempo: {elapsed:.2f} s")
            log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": True})
            continue

        # -------- Fase 1a: Caché LOCAL del worker (aprendida + exacta) ----------
        ruta_local, tipo_local = _local_lookup(entrada)
        if ruta_local:
            umbral = 80 if tipo_local == "local-exacta" else 60
            if validar_cache_contra_entrada(indice_sqlite, ruta_local, entrada, umbral=umbral):
                base_tags = entrada.get("tags") or {}
                mejor_cache = None
                if base_tags.get("title") or base_tags.get("artist"):
                    mejor_cache = _mejor_por_similares(indice_sqlite, base_tags, prefer_no_auto=True, quality_cache=quality_cache, search_space=_search_space_for_tags(base_tags))
                if mejor_cache:
                    b_old = _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta_local)
                    b_new = mejor_cache["tags"].get("bitrate", 0)
                    if b_new > b_old:
                        ruta_local = mejor_cache["path"]
                        log_lines.append(f"     ♻️ Mejorado desde caché {tipo_local} a mayor bitrate: {ruta_local} (bitrate={fmt_bitrate(b_new)})")
                else:
                    b_old = _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta_local)
                    log_lines.append(f"     ⚡ Coincidencia en caché {tipo_local}: {ruta_local} (bitrate={fmt_bitrate(b_old)})")

                # Actualizar cachés (local y acumulador global)
                bfinal = _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta_local)
                _local_update(entrada, ruta_local, bfinal or 0)
                _acc_update(updates_list, clave_exacta, ruta_local, bfinal or 0)
                for k in _claves_aprendizaje_para_entrada(entrada):
                    _acc_update(updates_list, k, ruta_local, bfinal or 0)

                bloque_resultados.append(ruta_local)
                elapsed = time.perf_counter() - start_t
                _record_stat(seq, tipo_local or "cache_local", True, elapsed, full_scan=False)
                log_lines.append(f"     🕒 Tiempo: {elapsed:.2f} s")
                log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": True})
                continue
            else:
                log_lines.append(f"     ⚠️ Caché {tipo_local} descartada por baja similitud: {ruta_local}")

        # -------- Fase 1b: Caché global (RAM :memory:) aprendida + exacta ----------
        ruta_cache = _buscar_en_cache_aprendida(mem_conn, entrada)
        cache_tipo = "aprendida"
        if not ruta_cache:
            ruta_cache = buscar_en_cache(mem_conn, clave_exacta)
            cache_tipo = "exacta" if ruta_cache else None

        if ruta_cache:
            if validar_cache_contra_entrada(indice_sqlite, ruta_cache, entrada, umbral=80 if cache_tipo=="exacta" else 60):
                base_tags = entrada.get("tags") or {}
                mejor_cache = None
                if base_tags.get("title") or base_tags.get("artist"):
                    mejor_cache = _mejor_por_similares(indice_sqlite, base_tags, prefer_no_auto=True, quality_cache=quality_cache, search_space=_search_space_for_tags(base_tags))
                if mejor_cache:
                    b_old = _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta_cache)
                    b_new = mejor_cache["tags"].get("bitrate", 0)
                    if b_new > b_old:
                        ruta_cache = mejor_cache["path"]
                        # actualizar cachés (local + acumulador global)
                        _local_update(entrada, ruta_cache, b_new or 0)
                        _acc_update(updates_list, clave_exacta, ruta_cache, b_new or 0)
                        for k in _claves_aprendizaje_para_entrada(entrada):
                            _acc_update(updates_list, k, ruta_cache, b_new or 0)
                        log_lines.append(f"     ♻️ Mejorado desde caché a mayor bitrate: {ruta_cache} (bitrate={fmt_bitrate(b_new)})")
                    else:
                        log_lines.append(f"     ⚡ Coincidencia en caché ({cache_tipo}): {ruta_cache} (bitrate={fmt_bitrate(b_old)})")
                else:
                    b_old = _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta_cache)
                    log_lines.append(f"     ⚡ Coincidencia en caché ({cache_tipo}): {ruta_cache} (bitrate={fmt_bitrate(b_old)})")

                # actualizar caché local y acumulador
                bfinal = _bitrate_por_path_lookup(path_lookup, indice_sqlite, ruta_cache)
                _local_update(entrada, ruta_cache, bfinal or 0)
                _acc_update(updates_list, clave_exacta, ruta_cache, bfinal or 0)
                for k in _claves_aprendizaje_para_entrada(entrada):
                    _acc_update(updates_list, k, ruta_cache, bfinal or 0)

                bloque_resultados.append(ruta_cache)
                elapsed = time.perf_counter() - start_t
                _record_stat(seq, f"cache_global_{cache_tipo or 'desconocida'}", True, elapsed, full_scan=False)
                log_lines.append(f"     🕒 Tiempo: {elapsed:.2f} s")
                log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": True})
                continue
            else:
                log_lines.append(f"     ⚠️ Caché {cache_tipo} descartada por baja similitud: {ruta_cache}")

        # -------- Fase 1c: Caché de calidad por identidad ----------
        # Solo descarta menor bitrate dentro de la misma identidad musical:
        # artista + titulo + duracion aproximada + remix/no-remix.
        if tags and (tags.get("title") or tags.get("artist")):
            mejor_calidad = _best_quality_from_cache(quality_cache, {
                "title": _titulo_puro(tags),
                "artist": _artista_con_fallback(tags),
                "duration": tags.get("duration"),
            }, prefer_no_auto=True, tolerancia=3)
            if mejor_calidad:
                ruta_sel = mejor_calidad["path"]
                bsel = int((mejor_calidad.get("tags") or {}).get("bitrate", 0) or 0)
                bloque_resultados.append(ruta_sel)
                _local_update(entrada, ruta_sel, bsel)
                _acc_update(updates_list, clave_exacta, ruta_sel, bsel)
                for k in _claves_aprendizaje_para_entrada(entrada):
                    _acc_update(updates_list, k, ruta_sel, bsel)
                log_lines.append(
                    f"     âš¡ Coincidencia por cachÃ© de calidad: {ruta_sel} "
                    f"(bitrate={fmt_bitrate(bsel)})"
                )
                elapsed = time.perf_counter() - start_t
                _record_stat(seq, "cache_calidad", True, elapsed, full_scan=False)
                log_lines.append(f"     Tiempo: {elapsed:.2f} s")
                log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": True})
                continue

        mejor_puntaje, candidatos = 0, []
        encontrado = False
        es_net_id = _es_netsearch_id(ruta)
        final_phase = "sin_coincidencia"
        full_scan_used = False
        scan_candidate_count = len(indice_sqlite)
        ruta_resultado_final = ""
        perf_detail = []

        # -------- Fase 2: Por tags ----------
        if tags and (tokens(_titulo_puro(tags) or tags.get("title") or "") or tokens(_artista_con_fallback(tags))):
            t_candidate = time.perf_counter()
            tag_groups = [
                ("title", tokens(_titulo_puro(tags) or tags.get("title") or "")),
                ("artist", tokens(_artista_con_fallback(tags))),
            ]
            tag_has_tokens = any(values for _, values in tag_groups)
            tag_candidates = _candidate_subset_from_sqlite(mem_conn, path_lookup, tag_groups, require_all_groups=True)
            if not tag_candidates and not sqlite_tokens_ready:
                tag_candidates = _candidate_subset_from_tokens(_get_candidate_cache(), tag_groups, require_all_groups=True)
            if not tag_candidates and (not sqlite_tokens_ready or not tag_has_tokens):
                tag_candidates = indice_sqlite
            scan_candidate_count = len(tag_candidates)
            full_scan_used = scan_candidate_count >= len(indice_sqlite)
            perf_detail.append(f"candidate_select={time.perf_counter() - t_candidate:.3f}s")
            t_score = time.perf_counter()
            for item in tag_candidates:
                puntaje = calcular_puntaje(tags, item.get("tags", {}))
                if puntaje > mejor_puntaje:
                    mejor_puntaje, candidatos = puntaje, [item]
                elif puntaje == mejor_puntaje:
                    candidatos.append(item)
            perf_detail.append(f"score_loop={time.perf_counter() - t_score:.3f}s")

            log_lines.append(
                f"     🔎 Mejor puntaje (tags): {mejor_puntaje:.2f} con {len(candidatos)} coincidencias "
                f"(candidatos evaluados={scan_candidate_count}/{len(indice_sqlite)})"
            )

            # Si es Tidal/netsearch, toleramos un poco más: intentamos “título suave”
            if es_net_id and (mejor_puntaje < 50 or not candidatos):
                t_soft = time.perf_counter()
                t_in = _titulo_puro(tags)
                d_in = tags.get("duration")
                cand_suaves = []
                for it in indice_sqlite:
                    t2 = (it.get("tags") or {}).get("title", "")
                    sim = sim_titulo_suave(t_in, t2)
                    if sim >= 60:  # umbral “suave”
                        # opcional: comprobar duración ±15s si se conoce
                        d2 = (it.get("tags") or {}).get("duration")
                        if d_in is None or d2 is None or abs(float(d_in) - float(d2)) <= 15:
                            cand_suaves.append((sim, it))
                if cand_suaves:
                    cand_suaves.sort(key=lambda x: (x[0], (x[1].get("tags") or {}).get("bitrate", 0)))
                    candidatos = [it for _, it in cand_suaves[-5:]]  # top 5
                    mejor_puntaje = 60.0  # marcamos como suficiente para continuar
                perf_detail.append(f"netsearch_soft_scan={time.perf_counter() - t_soft:.3f}s")

            if mejor_puntaje >= 50 and candidatos:
                cand_valid = [c for c in candidatos if not c["tags"].get("auto_tags", False)] or candidatos
                artist_in = _artista_con_fallback(tags)
                cand_compat = _filtrar_por_artista_esperado(cand_valid, artist_in)

                if STRICT_ARTIST_MATCH and artist_in and not cand_compat:
                    log_lines.append("     🚫 Sin candidatos con artista esperado (tags) → pasar a nombre")
                else:
                    if not _es_remix(tags.get("title", "")):
                        cand_compat = _preferir_no_remix(cand_compat)
                    mejor = max(cand_compat, key=lambda x: x.get("tags", {}).get("bitrate", 0))

                    t_refine = time.perf_counter()
                    mejor_global = _mejor_por_similares(indice_sqlite, {
                        "title": _titulo_puro(tags), "artist": artist_in, "duration": tags.get("duration"),
                    }, prefer_no_auto=True, quality_cache=quality_cache, search_space=tag_candidates)
                    perf_detail.append(f"quality_refine={time.perf_counter() - t_refine:.3f}s")
                    if mejor_global and mejor_global["tags"].get("bitrate", 0) > mejor["tags"].get("bitrate", 0):
                        mejor = mejor_global
                        log_lines.append(f"     ✅ Mejorado por similares (max bitrate): {mejor['path']} (bitrate={fmt_bitrate(mejor['tags'].get('bitrate', 0))})")
                    else:
                        bsel = mejor["tags"].get("bitrate", 0)
                        log_lines.append(
                            f"     {'⚠️' if mejor['tags'].get('auto_tags') else '✅'} "
                            f"{'Coincidencia por tags con auto_tags=True' if mejor['tags'].get('auto_tags') else 'Seleccionado por tags reales'}: "
                            f"{mejor['path']} (bitrate={fmt_bitrate(bsel)})"
                        )

                    # sustituir remix por no-remix si aplica
                    try:
                        if not _es_remix(tags.get("title", "")) and _es_remix((mejor.get("tags") or {}).get("title", "")):
                            t_alias = time.perf_counter()
                            alt = _upgrade_por_alias(indice_sqlite, _titulo_puro(tags), artist_in, tags.get("duration"),
                                                     prefer_no_auto=True, tolerancia_duracion=15, search_space=tag_candidates)
                            perf_detail.append(f"alias_no_remix={time.perf_counter() - t_alias:.3f}s")
                            if alt and not _es_remix((alt.get("tags") or {}).get("title", "")) \
                               and alt["tags"].get("bitrate", 0) >= mejor["tags"].get("bitrate", 0):
                                mejor = alt
                                log_lines.append(f"     🔄 Sustituido remix por versión no-remix: {mejor['path']} (bitrate={fmt_bitrate(mejor['tags'].get('bitrate', 0))})")
                    except Exception:
                        pass

                    # revisar alias para subir a 320 o cuadrar artista
                    try:
                        need_artist = artist_in and (normalizar_artista(mejor["tags"].get("artist", "")) != artist_in)
                        if (mejor["tags"].get("bitrate", 0) or 0) < 320 or need_artist:
                            t_alias = time.perf_counter()
                            cand_hi = _upgrade_por_alias(indice_sqlite, _titulo_puro(tags), artist_in, tags.get("duration"),
                                                         prefer_no_auto=True, tolerancia_duracion=3, search_space=tag_candidates)
                            perf_detail.append(f"alias_quality={time.perf_counter() - t_alias:.3f}s")
                            if cand_hi and (cand_hi["tags"].get("bitrate", 0) > mejor["tags"].get("bitrate", 0) or need_artist):
                                mejor = cand_hi
                                log_lines.append(f"     🔁 Revisado alias: mejor/ajustado → {mejor['path']} (bitrate={fmt_bitrate(mejor['tags'].get('bitrate', 0))})")
                    except Exception:
                        pass

                    # Acumular actualizaciones y cerrar
                    ruta_sel = mejor["path"]
                    bsel = mejor["tags"].get("bitrate", 0) or 0
                    bloque_resultados.append(ruta_sel)
                    ruta_resultado_final = ruta_sel

                    # caché local + acumulador global
                    _local_update(entrada, ruta_sel, bsel)
                    _acc_update(updates_list, clave_exacta, ruta_sel, bsel)
                    for k in _claves_aprendizaje_para_entrada(entrada):
                        _acc_update(updates_list, k, ruta_sel, bsel)

                    encontrado = True
                    final_phase = "tags_scan"
            else:
                log_lines.append("     ❌ Sin coincidencia suficiente (tags) → pasar a nombre")

        # -------- Fase 3: Por nombre ----------
        if not encontrado:
            # Si es un id netsearch (Tidal), el nombre NO sirve → evita falsa negativa.
            if es_net_id and tags and (tags.get("title") or tags.get("artist")):
                log_lines.append("     ⛔ Entrada netsearch/Tidal: se omite búsqueda por nombre (no informativa)")
                bloque_no_encontradas.append(ruta)
                elapsed = time.perf_counter() - start_t
                _record_stat(seq, "netsearch_sin_nombre", False, elapsed, full_scan=full_scan_used)
                log_lines.append(f"     🕒 Tiempo: {elapsed:.2f} s")
                log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": False})
                continue

            nombre_archivo = os.path.basename(ruta)
            mejor_puntaje, candidatos = 0, []
            t_candidate = time.perf_counter()
            name_groups = [
                ("name", tokens(nombre_archivo)),
                ("title", tokens(nombre_archivo)),
            ]
            name_has_tokens = any(values for _, values in name_groups)
            name_candidates = _candidate_subset_from_sqlite(mem_conn, path_lookup, name_groups)
            if not name_candidates and not sqlite_tokens_ready:
                name_candidates = _candidate_subset_from_tokens(_get_candidate_cache(), name_groups)
            if not name_candidates and (not sqlite_tokens_ready or not name_has_tokens):
                name_candidates = indice_sqlite
            scan_candidate_count = len(name_candidates)
            full_scan_used = scan_candidate_count >= len(indice_sqlite)
            perf_detail.append(f"name_candidate_select={time.perf_counter() - t_candidate:.3f}s")
            t_score = time.perf_counter()
            for item in name_candidates:
                cand_name = os.path.basename(item["path"]) if item.get("path") else ""
                puntaje = sim_nombre(nombre_archivo, cand_name)
                if puntaje > mejor_puntaje:
                    mejor_puntaje, candidatos = puntaje, [item]
                elif puntaje == mejor_puntaje:
                    candidatos.append(item)
            perf_detail.append(f"name_score_loop={time.perf_counter() - t_score:.3f}s")

            log_lines.append(
                f"     🔎 Mejor puntaje (nombre): {mejor_puntaje:.2f} con {len(candidatos)} coincidencias "
                f"(candidatos evaluados={scan_candidate_count}/{len(indice_sqlite)})"
            )

            if mejor_puntaje >= threshold and candidatos:
                artist_in = _artista_con_fallback(tags or {})
                cand2 = _filtrar_por_artista_esperado(candidatos, artist_in)

                titulo_in = _titulo_puro(tags or {})
                if titulo_in and not _es_remix(titulo_in):
                    cand_limpios = [c for c in cand2 if not _filename_es_compuesto_ajeno(c.get("path",""), titulo_in)]
                    if STRICT_ARTIST_MATCH and artist_in and not cand_limpios:
                        log_lines.append("     🚫 Todos los candidatos por nombre son compuestos ajenos → no encontrada")
                        bloque_no_encontradas.append(ruta)
                        elapsed = time.perf_counter() - start_t
                        _record_stat(seq, "nombre_scan_filtrado", False, elapsed, full_scan=full_scan_used, detail=" | ".join(perf_detail))
                        log_lines.append(f"     🕒 Tiempo: {elapsed:.2f} s")
                        log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": False})
                        continue
                    cand2 = cand_limpios or cand2

                if not _es_remix(nombre_archivo):
                    cand2 = _preferir_no_remix(cand2)

                if STRICT_ARTIST_MATCH and artist_in and not cand2:
                    log_lines.append("     🚫 Sin candidatos con artista esperado (nombre) → no encontrada")
                    bloque_no_encontradas.append(ruta)
                else:
                    mejor = max(cand2, key=lambda x: x.get("tags", {}).get("bitrate", 0))
                    ruta_resultado = mejor["path"]
                    bitrate_candidato = mejor.get("tags", {}).get("bitrate", 0)
                    log_lines.append(f"     📊 Candidato: {ruta_resultado} (bitrate={fmt_bitrate(bitrate_candidato)})")

                    arti_nom, tit_nom = partir_artista_titulo_desde_nombre(nombre_archivo)
                    pseudo_tags = {}
                    if tit_nom: pseudo_tags["title"] = tit_nom
                    if arti_nom: pseudo_tags["artist"] = normalizar_artista(arti_nom)
                    if entrada.get("tags", {}).get("duration") is not None:
                        pseudo_tags["duration"] = entrada["tags"]["duration"]

                    if pseudo_tags:
                        t_equal = time.perf_counter()
                        equal_space = name_candidates if name_candidates is not None else indice_sqlite
                        iguales = [
                            x for x in equal_space
                            if normalizar(x["tags"].get("title")) == normalizar(pseudo_tags.get("title", ""))
                            and (not pseudo_tags.get("artist") or normalizar_artista(x["tags"].get("artist")) == pseudo_tags.get("artist"))
                        ]
                        perf_detail.append(f"derived_equal={time.perf_counter() - t_equal:.3f}s")
                        if iguales:
                            iguales_validos = [s for s in iguales if not s["tags"].get("auto_tags", False)] or iguales
                            iguales_validos = _preferir_no_remix(iguales_validos) if not _es_remix(nombre_archivo) else iguales_validos
                            mejor_tag = max(iguales_validos, key=lambda x: x.get("tags", {}).get("bitrate", 0))
                            if mejor_tag.get("tags").get("bitrate", 0) > bitrate_candidato:
                                mejor = mejor_tag
                                log_lines.append(f"     ✅ Mejorado por igualdad de tags derivada del nombre: {mejor['path']} (bitrate={fmt_bitrate(mejor.get('tags', {}).get('bitrate', 0))})")

                    ruta_sel = mejor["path"]
                    bsel = mejor.get("tags", {}).get("bitrate", 0) or 0
                    bloque_resultados.append(ruta_sel)
                    ruta_resultado_final = ruta_sel

                    # caché local + acumulador global
                    _local_update(entrada, ruta_sel, bsel)
                    _acc_update(updates_list, clave_exacta, ruta_sel, bsel)
                    for k in _claves_aprendizaje_para_entrada(entrada):
                        _acc_update(updates_list, k, ruta_sel, bsel)

                    encontrado = True
                    final_phase = "nombre_scan"
            else:
                log_lines.append("     ❌ Sin coincidencia suficiente (nombre)")
                bloque_no_encontradas.append(ruta)
                final_phase = "nombre_scan_sin_coincidencia"

        elapsed = time.perf_counter() - start_t
        _record_stat(
            seq, final_phase, encontrado, elapsed, full_scan=full_scan_used,
            entrada=entrada, result_path=ruta_resultado_final, candidate_count=scan_candidate_count,
            detail=" | ".join(perf_detail)
        )
        log_lines.append(f"     🕒 Tiempo: {elapsed:.2f} s")
        log_queue.put({"seq": seq, "text": "\n".join(log_lines), "found": encontrado})

    # Workers: NO guardan nada a disco.
    resultados_dict[bloque_id] = bloque_resultados
    no_encontradas_dict[bloque_id] = bloque_no_encontradas

# =============================================================================
# 🧠 Orquestador + escritura única en disco
# =============================================================================

def _emit_progreso(progreso_callback, processed, total):
    if not progreso_callback or total <= 0: return
    pct = int(round(processed / total * 100))
    try:
        progreso_callback(pct)
    except TypeError:
        try: progreso_callback(processed, total)
        except Exception: pass

def _drain_logs_ordenados(log_queue, estado_callback, progreso_callback, total):
    buffer, next_seq, processed = {}, 1, 0
    while processed < total:
        item = log_queue.get()
        if item is None: break
        seq = item.get("seq")
        buffer[seq] = item
        while next_seq in buffer:
            it = buffer.pop(next_seq)
            texto = it.get("text", "")
            if estado_callback and texto:
                try: estado_callback(texto)
                except Exception: pass
            processed += 1
            _emit_progreso(progreso_callback, processed, total)
            next_seq += 1

def _percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (float(pct) / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac

def _format_perf_summary(stats, wall_elapsed, total_entries):
    stats = list(stats or [])
    elapsed_values = [float(s.get("elapsed", 0.0) or 0.0) for s in stats]
    found = sum(1 for s in stats if s.get("found"))
    full_scan = sum(1 for s in stats if s.get("full_scan"))
    avoided = len(stats) - full_scan
    index_size = max([int(s.get("index_size", 0) or 0) for s in stats] or [0])
    avg = (sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0.0
    eps = (len(stats) / wall_elapsed) if wall_elapsed > 0 else 0.0

    lines = [
        "",
        "===== ANALISIS - RESUMEN DE RENDIMIENTO =====",
        f"Entradas procesadas: {len(stats)}/{total_entries}",
        f"Encontradas: {found} | No encontradas: {len(stats) - found}",
        f"Biblioteca indexada: {index_size} pistas",
        f"Tiempo total pared: {wall_elapsed:.2f} s",
        f"Media por entrada: {avg:.3f} s | p50: {_percentile(elapsed_values, 50):.3f} s | p95: {_percentile(elapsed_values, 95):.3f} s | max: {(max(elapsed_values) if elapsed_values else 0.0):.3f} s",
        f"Rendimiento: {eps:.2f} entradas/s",
        f"Evitaron escaneo completo: {avoided} ({(avoided / len(stats) * 100.0) if stats else 0.0:.1f}%)",
        f"Usaron escaneo completo: {full_scan} ({(full_scan / len(stats) * 100.0) if stats else 0.0:.1f}%)",
        "",
        "Por fase:",
    ]

    by_phase = {}
    for item in stats:
        phase = item.get("phase") or "desconocida"
        by_phase.setdefault(phase, []).append(float(item.get("elapsed", 0.0) or 0.0))
    for phase in sorted(by_phase):
        vals = by_phase[phase]
        count = len(vals)
        total = sum(vals)
        lines.append(
            f"  - {phase}: {count} entradas | total {total:.2f} s | media {(total / count if count else 0.0):.3f} s | p95 {_percentile(vals, 95):.3f} s | max {(max(vals) if vals else 0.0):.3f} s"
        )
    slow = sorted(stats, key=lambda s: float(s.get("elapsed", 0.0) or 0.0), reverse=True)[:10]
    if slow:
        lines.extend(["", "Entradas mas lentas:"])
        for item in slow:
            label = item.get("label") or os.path.basename(item.get("ruta") or "") or "(sin etiqueta)"
            result = os.path.basename(item.get("result_path") or "")
            suffix = f" -> {result}" if result else ""
            detail = item.get("detail") or ""
            detail_suffix = f" | {detail}" if detail else ""
            lines.append(
                f"  - #{int(item.get('seq', 0) or 0)} | {item.get('phase') or 'desconocida'} | "
                f"{float(item.get('elapsed', 0.0) or 0.0):.3f} s | "
                f"candidatos {int(item.get('candidate_count', 0) or 0)}/{index_size} | {label}{suffix}{detail_suffix}"
            )
    lines.append("===== FIN RESUMEN =====")
    return "\n".join(lines)

def _save_perf_summary(summary_text):
    try:
        os.makedirs("datos", exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join("datos", f"analisis_rendimiento_{stamp}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(summary_text)
            f.write("\n")
        return path
    except Exception as e:
        logging.warning(f"No se pudo guardar resumen de rendimiento: {e}")
        return None

def fmt_bitrate(b):
    try: b = int(b or 0)
    except Exception: b = 0
    return f"{b} kbps"

def generar_clave_entrada(entrada: dict) -> str:
    base = json.dumps({
        "ruta": entrada.get("ruta"),
        "title": (entrada.get("tags") or {}).get("title"),
        "artist": (entrada.get("tags") or {}).get("artist"),
        "duration": (entrada.get("tags") or {}).get("duration"),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def _mejor_por_similares(indice_sqlite, base_tags, prefer_no_auto=True, quality_cache=None, search_space=None):
    cached = _best_quality_from_cache(quality_cache, base_tags, prefer_no_auto=prefer_no_auto, tolerancia=3)
    if cached:
        return cached
    pool = search_space if search_space is not None else indice_sqlite
    candidatos = [
        x for x in pool
        if tags_similares({
            "title": base_tags.get("title"),
            "artist": base_tags.get("artist"),
            "duration": base_tags.get("duration"),
        }, x.get("tags", {}))
    ]
    if not candidatos: return None
    if prefer_no_auto:
        cand_noauto = [c for c in candidatos if not c["tags"].get("auto_tags", False)]
        candidatos = cand_noauto or candidatos
    if not _es_remix(base_tags.get("title", "")):
        candidatos = _preferir_no_remix(candidatos)
    return max(candidatos, key=lambda x: x.get("tags", {}).get("bitrate", 0))

# --- Acumulación de updates desde workers ------------------------------------

def _acc_update(updates_list, clave: str, ruta: str, bitrate: int):
    # Guardamos como tupla; el padre deduplica
    try:
        updates_list.append((clave, ruta, int(bitrate or 0)))
    except Exception:
        updates_list.append((clave, ruta, 0))

def _aplicar_updates_en_disco(updates: List[Tuple[str, str, int]]):
    """Aplica todas las actualizaciones en una sola conexión a datos/coincidencias.db."""
    if not updates:
        return
    ruta_db = os.path.join("datos", "coincidencias.db")
    os.makedirs("datos", exist_ok=True)
    conn = sqlite3.connect(ruta_db)
    try:
        cur = conn.cursor()
        # PRAGMA rápidos: una sola escritura al final
        cur.execute("PRAGMA journal_mode=OFF")
        cur.execute("PRAGMA synchronous=OFF")
        cur.execute("PRAGMA temp_store=MEMORY")
        _ensure_cache_tables(conn)

        # Deduplicar: última tupla para cada clave
        dedup = {}
        for k, r, b in updates:
            dedup[k] = (r, b)
        rows = [(k, v[0], v[1], datetime.now().isoformat()) for k, v in dedup.items()]

        cur.executemany("""
            INSERT OR REPLACE INTO cache_coincidencias (entrada, ruta_resultado, bitrate, timestamp)
            VALUES (?, ?, ?, ?)
        """, rows)
        conn.commit()
    finally:
        conn.close()

def buscar_mejor_coincidencia(
    rutas_playlist,
    progreso_callback=None,
    estado_callback=None,
    threshold=70,
    max_workers: Optional[int] = None,
    solo_bitrate_bajo: bool = False,
    bitrate_minimo: int = 320,
    bitrate_overrides: Optional[dict] = None
):
    total = len(rutas_playlist)
    if total == 0:
        return [], []
    wall_started = time.perf_counter()

    nworkers = max(1, min(max_workers or DEFAULT_MAX_WORKERS, cpu_count()))
    tam_bloque = max(1, ceil(total / nworkers))
    bloques = [rutas_playlist[i:i+tam_bloque] for i in range(0, total, tam_bloque)]

    manager = Manager()
    resultados_dict = manager.dict()
    no_encontradas_dict = manager.dict()
    log_queue = manager.Queue()
    progreso = manager.Value('i', 0)
    seq_lock = manager.Lock()
    stats_list = manager.list()
    updates_list = manager.list()  # ← aquí acumulan los workers

    procesos = []
    for bid, bloque in enumerate(bloques):
        p = Process(target=procesar_bloque_con_orden, args=(
            bloque, resultados_dict, no_encontradas_dict, progreso, threshold,
            bid, log_queue, seq_lock, updates_list, solo_bitrate_bajo, bitrate_minimo, bitrate_overrides, stats_list
        ))
        procesos.append(p); p.start()

    try:
        _drain_logs_ordenados(log_queue, estado_callback, progreso_callback, total)
    finally:
        for p in procesos: p.join()

    # Aplicación única de cache a disco
    try:
        _aplicar_updates_en_disco(list(updates_list))
    except Exception as e:
        logging.warning(f"Error aplicando caché a disco: {e}")

    wall_elapsed = time.perf_counter() - wall_started
    summary = _format_perf_summary(list(stats_list), wall_elapsed, total)
    summary_path = _save_perf_summary(summary)
    if summary_path:
        summary = f"{summary}\nResumen guardado en: {summary_path}"

    if estado_callback:
        estado_callback("Coincidencias evaluadas")
        estado_callback(summary)

    resultados_ordenados, no_encontradas_ordenadas = [], []
    for i in range(len(bloques)):
        resultados_ordenados.extend(resultados_dict.get(i, []))
        no_encontradas_ordenadas.extend(no_encontradas_dict.get(i, []))

    return resultados_ordenados, no_encontradas_ordenadas
