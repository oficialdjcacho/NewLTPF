import os
import json
import hashlib
import logging
import sqlite3
import re
import time
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from mutagen import File

import alias_suggester

logging.basicConfig(level=logging.INFO)

AUDIO_EXTS = ('.mp3', '.flac', '.wav', '.m4a')
TOKEN_INDEX_VERSION = "2"


def _now_label():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_seconds(value):
    try:
        return f"{float(value):.2f} s"
    except Exception:
        return "0.00 s"


def _save_index_perf_log(carpeta_datos, stats):
    try:
        os.makedirs(carpeta_datos, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(carpeta_datos, f"indexado_rendimiento_{stamp}.log")
        lines = [
            "===== INDEXADO - RESUMEN DE RENDIMIENTO =====",
            f"Fecha: {stats.get('fecha', _now_label())}",
            f"Carpeta: {stats.get('carpeta', '')}",
            f"Modo: {stats.get('modo', '')}",
            f"Indice JSON: {stats.get('ruta_indice', '')}",
            f"SQLite: {stats.get('ruta_bd', '')}",
            f"Workers: {stats.get('workers', 0)}",
            f"Archivos encontrados: {stats.get('archivos_total', 0)}",
            f"Entradas indexadas: {stats.get('entradas_indexadas', 0)}",
            f"Reutilizadas sin leer tags: {stats.get('reused_entries', 0)}",
            f"Nuevas o modificadas: {stats.get('changed_entries', 0)}",
            f"Eliminadas del indice: {stats.get('deleted_entries', 0)}",
            f"Con tags validos: {stats.get('con_tags', 0)}",
            f"Sin tags legibles: {stats.get('sin_tags', 0)}",
            f"Tags autogenerados: {stats.get('auto_tags', 0)}",
            f"Con bitrate: {stats.get('con_bitrate', 0)}",
            f"Con duracion: {stats.get('con_duration', 0)}",
            f"Errores metadata: {stats.get('errores_metadata', 0)}",
            f"JSON MB: {stats.get('json_mb', 0.0):.2f}",
            f"SQLite MB: {stats.get('sqlite_mb', 0.0):.2f}",
            f"Alias sugeridos: {stats.get('alias_suggestions', 0)}",
            f"Tokens SQLite: {stats.get('token_rows', 0)}",
            "",
            "Tiempos:",
            f"  - total: {_fmt_seconds(stats.get('total_elapsed', 0.0))}",
            f"  - cargar JSON existente: {_fmt_seconds(stats.get('json_load_elapsed', 0.0))}",
            f"  - escanear carpetas: {_fmt_seconds(stats.get('scan_elapsed', 0.0))}",
            f"  - comparar incremental: {_fmt_seconds(stats.get('incremental_elapsed', 0.0))}",
            f"  - extraer metadata: {_fmt_seconds(stats.get('metadata_elapsed', 0.0))}",
            f"  - preparar SQLite en memoria: {_fmt_seconds(stats.get('sqlite_insert_elapsed', 0.0))}",
            f"  - guardar JSON: {_fmt_seconds(stats.get('json_write_elapsed', 0.0))}",
            f"  - guardar SQLite: {_fmt_seconds(stats.get('sqlite_write_elapsed', 0.0))}",
            f"  - generar alias: {_fmt_seconds(stats.get('alias_elapsed', 0.0))}",
            "",
            "Rendimiento:",
            f"  - metadata archivos/s: {stats.get('metadata_files_per_sec', 0.0):.2f}",
            f"  - total archivos/s: {stats.get('total_files_per_sec', 0.0):.2f}",
        ]
        slow_files = list(stats.get("slow_files") or [])
        if slow_files:
            lines.extend(["", "Archivos mas lentos leyendo metadata:"])
            for item in slow_files[:20]:
                lines.append(
                    f"  - {float(item.get('elapsed', 0.0) or 0.0):.3f} s | "
                    f"{item.get('ext', '')} | {item.get('path', '')}"
                )
        lines.append("===== FIN RESUMEN =====")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path
    except Exception as e:
        logging.warning(f"No se pudo guardar resumen de indexado: {e}")
        return ""


def _emit_estado(estado_callback, text):
    if estado_callback:
        estado_callback(text)


def _emit_progress(progreso_callback, percent):
    if progreso_callback:
        progreso_callback(max(0, min(100, int(percent))))


def extraer_tags(ruta):
    try:
        archivo = File(ruta, easy=True)
        if archivo is None or not hasattr(archivo, 'info'):
            return None

        duracion = archivo.info.length if archivo.info else None
        bitrate = getattr(archivo.info, 'bitrate', None)
        if bitrate:
            bitrate = round(bitrate / 1000)

        titulo = archivo.get('title', [None])[0]
        artista = archivo.get('artist', [None])[0]
        auto_tags = False

        if not archivo.tags or (not titulo and not artista):
            auto_tags = True
            nombre_archivo = os.path.splitext(os.path.basename(ruta))[0]
            nombre_archivo = re.sub(r'[_]+', ' ', nombre_archivo)
            nombre_archivo = re.sub(r'\s+', ' ', nombre_archivo).strip()
            nombre_archivo = re.sub(r'\s*[\[\(].*?[\]\)]\s*', '', nombre_archivo)

            if ' - ' in nombre_archivo:
                partes = nombre_archivo.split(' - ', 1)
                artista = partes[0].strip()
                titulo = partes[1].strip()
            else:
                artista = None
                titulo = nombre_archivo.strip()

        elif artista is None and titulo and ' - ' in titulo:
            auto_tags = True
            partes = titulo.split(' - ', 1)
            posible_artista = partes[0].strip()
            posible_titulo = partes[1].strip()

            if posible_titulo.lower().startswith(posible_artista.lower()):
                posible_titulo = posible_titulo[len(posible_artista):].lstrip("-: ").strip()

            artista = posible_artista
            titulo = posible_titulo

        return {
            "title": titulo,
            "artist": artista,
            "duration": duracion,
            "bitrate": bitrate,
            "auto_tags": auto_tags
        }

    except Exception as e:
        logging.warning(f"Error extrayendo metadata de {ruta}: {e}")
        return None


def generar_hash_carpeta(carpeta):
    return hashlib.md5(carpeta.encode()).hexdigest()


def ruta_indice_json(carpeta, carpeta_datos=None):
    carpeta_datos = carpeta_datos or os.path.join(".", "datos")
    return os.path.join(carpeta_datos, f"mp3_index_{generar_hash_carpeta(carpeta)}.json")


def indice_json_existe(carpeta, carpeta_datos=None):
    return os.path.exists(ruta_indice_json(carpeta, carpeta_datos=carpeta_datos))


def _sqlite_tokens_ready(ruta_bd):
    if not os.path.exists(ruta_bd):
        return False
    try:
        conn = sqlite3.connect(ruta_bd)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='track_tokens'")
        if cur.fetchone() is None:
            return False
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='index_meta'")
        if cur.fetchone() is None:
            return False
        cur.execute("SELECT value FROM index_meta WHERE key = 'token_index_version'")
        row = cur.fetchone()
        if not row or str(row[0]) != TOKEN_INDEX_VERSION:
            return False
        cur.execute("SELECT COUNT(*) FROM track_tokens")
        return int(cur.fetchone()[0] or 0) > 0
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _sqlite_token_count(ruta_bd):
    if not os.path.exists(ruta_bd):
        return 0
    try:
        conn = sqlite3.connect(ruta_bd)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM track_tokens")
        return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _file_stat(ruta):
    try:
        st = os.stat(ruta)
        return {"size": int(st.st_size), "mtime": float(st.st_mtime)}
    except Exception:
        return {"size": None, "mtime": None}


def _norm_path(path):
    return os.path.normcase(os.path.normpath(path or ""))


def _attach_stat(item, stat_map):
    path = item.get("path") or ""
    stat = stat_map.get(_norm_path(path)) or _file_stat(path)
    return {
        "path": path,
        "tags": item.get("tags"),
        "stat": stat,
    }


def _stat_matches(item, stat):
    old = item.get("stat") or {}
    if old.get("size") is None and old.get("mtime") is None and stat.get("size") is None and stat.get("mtime") is None:
        return True
    return (
        old.get("size") is not None
        and old.get("mtime") is not None
        and int(old.get("size")) == int(stat.get("size"))
        and abs(float(old.get("mtime")) - float(stat.get("mtime"))) < 0.0001
    )


def _tokenize_text(value):
    value = value or ""
    value = unicodedata.normalize("NFKD", value).encode("ASCII", "ignore").decode("ASCII")
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    return sorted({tok.lower() for tok in re.split(r"\s+", value) if len(tok) >= 4})


def _tokens_for_item(item):
    tags = item.get("tags") or {}
    path = item.get("path") or ""
    rows = []
    for token_type, value in (
        ("title", tags.get("title") or ""),
        ("artist", tags.get("artist") or ""),
        ("name", os.path.splitext(os.path.basename(path))[0]),
    ):
        for token in _tokenize_text(value):
            rows.append((token, token_type, path))
    return rows


def inicializar_bd_memoria():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS indice_audio (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE,
            title TEXT,
            artist TEXT,
            duration REAL,
            bitrate INTEGER,
            auto_tags INTEGER,
            size INTEGER,
            mtime REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS track_tokens (
            token TEXT NOT NULL,
            token_type TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            PRIMARY KEY (token, token_type, track_id)
        ) WITHOUT ROWID
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_track_tokens_lookup ON track_tokens(token_type, token)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        ("token_index_version", TOKEN_INDEX_VERSION),
    )
    conn.commit()
    return conn


def procesar_archivo(ruta):
    started = time.perf_counter()
    tags = extraer_tags(ruta)
    return {
        "path": ruta,
        "tags": tags,
        "stat": _file_stat(ruta),
        "elapsed": time.perf_counter() - started,
        "ok": tags is not None,
    }


def _scan_audio_files(carpeta):
    archivos = []
    for root, _, files in os.walk(carpeta):
        for file in files:
            if file.lower().endswith(AUDIO_EXTS):
                archivos.append(os.path.join(root, file))
    return archivos


def _index_stats(index):
    total = len(index)
    con_tags = sum(1 for item in index if item.get("tags"))
    auto_tags = sum(1 for item in index if (item.get("tags") or {}).get("auto_tags"))
    con_bitrate = sum(1 for item in index if (item.get("tags") or {}).get("bitrate"))
    con_duration = sum(1 for item in index if (item.get("tags") or {}).get("duration"))
    return {
        "entradas_indexadas": total,
        "con_tags": con_tags,
        "sin_tags": total - con_tags,
        "auto_tags": auto_tags,
        "con_bitrate": con_bitrate,
        "con_duration": con_duration,
    }


def _insertar_sqlite_en_memoria(index):
    conn_mem = inicializar_bd_memoria()
    cursor = conn_mem.cursor()
    insert_values = []
    token_values = []
    for track_id, resultado in enumerate(index, start=1):
        tags = resultado.get("tags") or {}
        if not tags:
            continue
        stat = resultado.get("stat") or {}
        insert_values.append((
            track_id,
            resultado["path"],
            tags.get("title"),
            tags.get("artist"),
            tags.get("duration"),
            tags.get("bitrate"),
            int(tags.get("auto_tags", False)),
            stat.get("size"),
            stat.get("mtime"),
        ))
        for token, token_type, _path in _tokens_for_item(resultado):
            token_values.append((token, token_type, track_id))

    cursor.executemany("""
        INSERT OR REPLACE INTO indice_audio (id, path, title, artist, duration, bitrate, auto_tags, size, mtime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, insert_values)
    cursor.executemany("""
        INSERT OR REPLACE INTO track_tokens (token, token_type, track_id)
        VALUES (?, ?, ?)
    """, token_values)
    conn_mem.commit()
    return conn_mem, len(token_values)


def _generar_sugerencias_alias(index, carpeta_datos):
    sugg = alias_suggester.ArtistAliasSuggester()
    for item in index:
        tags = item.get("tags") or {}
        if not tags:
            continue
        artista = tags.get("artist")
        titulo = tags.get("title")
        dur = tags.get("duration")
        if artista:
            sugg.add(artista, titulo, dur, item.get("path") or "")

    suggestions = sugg.build_suggestions()
    ruta_sug = os.path.join(carpeta_datos, "alias_suggestions.json")
    sugg.save_suggestions(ruta_sug, suggestions)
    return ruta_sug, len(suggestions)


def cargar_indice(carpeta, progreso_callback=None, estado_callback=None, generar_alias=True):
    started_total = time.perf_counter()
    carpeta_datos = os.path.join(".", "datos")
    os.makedirs(carpeta_datos, exist_ok=True)
    ruta_indice = ruta_indice_json(carpeta, carpeta_datos=carpeta_datos)
    ruta_bd = os.path.join(carpeta_datos, "coincidencias.db")

    stats = {
        "fecha": _now_label(),
        "carpeta": carpeta,
        "ruta_indice": ruta_indice,
        "ruta_bd": ruta_bd,
        "workers": 0,
    }

    existing_index = []
    if os.path.exists(ruta_indice):
        load_started = time.perf_counter()
        with open(ruta_indice, 'r', encoding='utf-8') as f:
            existing_index = json.load(f)
        stats["json_load_elapsed"] = time.perf_counter() - load_started
        logging.info(f"Índice previo cargado desde: {ruta_indice}")

    if existing_index:
        _emit_estado(estado_callback, "Comprobando cambios de biblioteca...")
    else:
        logging.info(f"No se encontró índice. Iniciando indexación en: {carpeta}")
        _emit_estado(estado_callback, "Escaneando carpetas de música...")

    _emit_estado(estado_callback, "Escaneando carpetas de música...")

    scan_started = time.perf_counter()
    archivos = _scan_audio_files(carpeta)
    scan_elapsed = time.perf_counter() - scan_started
    total = len(archivos)
    num_workers = min(32, os.cpu_count() or 8)

    stat_map = {_norm_path(path): _file_stat(path) for path in archivos}
    incremental_started = time.perf_counter()
    existing_by_path = {
        _norm_path(item.get("path")): item
        for item in existing_index
        if item.get("path")
    }
    seen_paths = set(stat_map)
    deleted_entries = max(0, len([k for k in existing_by_path if k not in seen_paths]))
    reused = []
    changed = []
    for path in archivos:
        key = _norm_path(path)
        stat = stat_map.get(key) or _file_stat(path)
        old = existing_by_path.get(key)
        if old and (_stat_matches(old, stat) or not old.get("stat")):
            reused.append(_attach_stat(old, stat_map))
        else:
            changed.append(path)

    stats.update({
        "modo": "incremental" if existing_index else "indexado_completo",
        "workers": num_workers,
        "archivos_total": total,
        "scan_elapsed": scan_elapsed,
        "incremental_elapsed": time.perf_counter() - incremental_started,
        "reused_entries": len(reused),
        "changed_entries": len(changed),
        "deleted_entries": deleted_entries,
    })

    if existing_index and not changed and deleted_entries == 0:
        index = reused
        stats.update(_index_stats(index))
        stats.update({
            "modo": "incremental_sin_cambios",
            "workers": 0,
            "errores_metadata": 0,
            "metadata_elapsed": 0.0,
            "metadata_files_per_sec": 0.0,
            "json_mb": os.path.getsize(ruta_indice) / 1024 / 1024 if os.path.exists(ruta_indice) else 0.0,
            "sqlite_mb": os.path.getsize(ruta_bd) / 1024 / 1024 if os.path.exists(ruta_bd) else 0.0,
            "token_rows": _sqlite_token_count(ruta_bd),
            "alias_suggestions": 0,
        })
        if _sqlite_tokens_ready(ruta_bd):
            stats["total_elapsed"] = time.perf_counter() - started_total
            stats["total_files_per_sec"] = (total / stats["total_elapsed"]) if stats["total_elapsed"] > 0 else 0.0
            summary_path = _save_index_perf_log(carpeta_datos, stats)
            logging.info(f"Índice incremental sin cambios: {ruta_indice}")
            _emit_estado(estado_callback, f"Índice sin cambios ({len(index)} pistas)")
            if summary_path:
                _emit_estado(estado_callback, f"Log de indexado: {summary_path}")
            _emit_progress(progreso_callback, 100)
            return index
    else:
        _emit_estado(
            estado_callback,
            f"Archivos encontrados: {total}. Reutilizados: {len(reused)}. A leer: {len(changed)}."
        )

    _emit_progress(progreso_callback, 0)

    if not (existing_index and not changed and deleted_entries == 0):
        errores_metadata = 0
        processed = 0
        slow_files = []
        last_emit_ts = 0.0
        next_emit_count = 0
        emit_every = max(100, min(1000, max(1, len(changed) // 100))) if changed else 1

        fresh = []
        metadata_started = time.perf_counter()
        if changed:
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = [executor.submit(procesar_archivo, ruta) for ruta in changed]
                for future in as_completed(futures):
                    resultado = future.result()
                    fresh.append({
                        "path": resultado.get("path"),
                        "tags": resultado.get("tags"),
                        "stat": resultado.get("stat"),
                    })
                    processed += 1
                    if not resultado.get("ok"):
                        errores_metadata += 1
                    slow_files.append({
                        "path": resultado.get("path"),
                        "elapsed": float(resultado.get("elapsed", 0.0) or 0.0),
                        "ext": os.path.splitext(resultado.get("path") or "")[1].lower(),
                    })

                    now = time.perf_counter()
                    if processed >= next_emit_count or (now - last_emit_ts) >= 1.0 or processed == len(changed):
                        pct = round(processed / len(changed) * 100) if changed else 100
                        _emit_progress(progreso_callback, pct)
                        _emit_estado(estado_callback, f"Indexando metadata {processed}/{len(changed)} ({pct}%)")
                        last_emit_ts = now
                        next_emit_count = processed + emit_every

        metadata_elapsed = time.perf_counter() - metadata_started
        index = reused + fresh
        stats.update({
            "metadata_elapsed": metadata_elapsed,
            "errores_metadata": errores_metadata,
            "metadata_files_per_sec": (len(changed) / metadata_elapsed) if metadata_elapsed > 0 else 0.0,
            "slow_files": sorted(slow_files, key=lambda x: float(x.get("elapsed", 0.0) or 0.0), reverse=True)[:20],
        })

    _emit_estado(estado_callback, "Preparando base SQLite en memoria...")
    sqlite_insert_started = time.perf_counter()
    conn_mem, token_rows = _insertar_sqlite_en_memoria(index)
    stats["sqlite_insert_elapsed"] = time.perf_counter() - sqlite_insert_started
    stats["token_rows"] = token_rows

    _emit_estado(estado_callback, "Guardando índice JSON...")
    json_started = time.perf_counter()
    with open(ruta_indice, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, separators=(',', ':'))
    stats["json_write_elapsed"] = time.perf_counter() - json_started
    stats["json_mb"] = os.path.getsize(ruta_indice) / 1024 / 1024 if os.path.exists(ruta_indice) else 0.0
    logging.info(f"Índice guardado en: {ruta_indice}")

    _emit_estado(estado_callback, "Guardando base SQLite...")
    sqlite_write_started = time.perf_counter()
    try:
        conn_disco = sqlite3.connect(ruta_bd)
        conn_mem.backup(conn_disco)
        conn_disco.close()
        conn_mem.close()
        logging.info(f"Base de datos guardada en: {ruta_bd}")
    except Exception as e:
        logging.warning(f"Error al guardar la base de datos desde memoria: {e}")
    stats["sqlite_write_elapsed"] = time.perf_counter() - sqlite_write_started
    stats["sqlite_mb"] = os.path.getsize(ruta_bd) / 1024 / 1024 if os.path.exists(ruta_bd) else 0.0

    alias_started = time.perf_counter()
    if generar_alias:
        _emit_estado(estado_callback, "Generando sugerencias de alias...")
        try:
            ruta_sug, alias_count = _generar_sugerencias_alias(index, carpeta_datos)
            stats["alias_suggestions"] = alias_count
            logging.info(f"Sugerencias de alias generadas: {alias_count} -> {ruta_sug}")
            _emit_estado(estado_callback, "Sugerencias de alias generadas (revisar en GUI)")
        except Exception as e:
            stats["alias_suggestions"] = 0
            logging.warning(f"No se pudieron generar sugerencias de alias: {e}")
    else:
        stats["alias_suggestions"] = 0
        _emit_estado(estado_callback, "Sugerencias de alias omitidas en esta carga.")
    stats["alias_elapsed"] = time.perf_counter() - alias_started

    stats.update(_index_stats(index))
    stats["total_elapsed"] = time.perf_counter() - started_total
    stats["total_files_per_sec"] = (total / stats["total_elapsed"]) if stats["total_elapsed"] > 0 else 0.0

    summary_path = _save_index_perf_log(carpeta_datos, stats)
    if summary_path:
        _emit_estado(estado_callback, f"Log de indexado: {summary_path}")
    _emit_progress(progreso_callback, 100)

    return index
