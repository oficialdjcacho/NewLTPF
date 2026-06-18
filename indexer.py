import os
import json
import hashlib
import logging
import sqlite3
import re
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from mutagen import File

import alias_suggester

logging.basicConfig(level=logging.INFO)

AUDIO_EXTS = ('.mp3', '.flac', '.wav', '.m4a')


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
            f"Con tags validos: {stats.get('con_tags', 0)}",
            f"Sin tags legibles: {stats.get('sin_tags', 0)}",
            f"Tags autogenerados: {stats.get('auto_tags', 0)}",
            f"Con bitrate: {stats.get('con_bitrate', 0)}",
            f"Con duracion: {stats.get('con_duration', 0)}",
            f"Errores metadata: {stats.get('errores_metadata', 0)}",
            f"JSON MB: {stats.get('json_mb', 0.0):.2f}",
            f"SQLite MB: {stats.get('sqlite_mb', 0.0):.2f}",
            f"Alias sugeridos: {stats.get('alias_suggestions', 0)}",
            "",
            "Tiempos:",
            f"  - total: {_fmt_seconds(stats.get('total_elapsed', 0.0))}",
            f"  - cargar JSON existente: {_fmt_seconds(stats.get('json_load_elapsed', 0.0))}",
            f"  - escanear carpetas: {_fmt_seconds(stats.get('scan_elapsed', 0.0))}",
            f"  - extraer metadata: {_fmt_seconds(stats.get('metadata_elapsed', 0.0))}",
            f"  - preparar SQLite en memoria: {_fmt_seconds(stats.get('sqlite_insert_elapsed', 0.0))}",
            f"  - guardar JSON: {_fmt_seconds(stats.get('json_write_elapsed', 0.0))}",
            f"  - guardar SQLite: {_fmt_seconds(stats.get('sqlite_write_elapsed', 0.0))}",
            f"  - generar alias: {_fmt_seconds(stats.get('alias_elapsed', 0.0))}",
            "",
            "Rendimiento:",
            f"  - metadata archivos/s: {stats.get('metadata_files_per_sec', 0.0):.2f}",
            f"  - total archivos/s: {stats.get('total_files_per_sec', 0.0):.2f}",
            "===== FIN RESUMEN =====",
        ]
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


def inicializar_bd_memoria():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS indice_audio (
            path TEXT PRIMARY KEY,
            title TEXT,
            artist TEXT,
            duration REAL,
            bitrate INTEGER,
            auto_tags INTEGER
        )
    """)
    conn.commit()
    return conn


def procesar_archivo(ruta):
    started = time.perf_counter()
    tags = extraer_tags(ruta)
    return {
        "path": ruta,
        "tags": tags,
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
    for resultado in index:
        tags = resultado.get("tags") or {}
        if not tags:
            continue
        insert_values.append((
            resultado["path"],
            tags.get("title"),
            tags.get("artist"),
            tags.get("duration"),
            tags.get("bitrate"),
            int(tags.get("auto_tags", False))
        ))

    cursor.executemany("""
        INSERT OR REPLACE INTO indice_audio (path, title, artist, duration, bitrate, auto_tags)
        VALUES (?, ?, ?, ?, ?, ?)
    """, insert_values)
    conn_mem.commit()
    return conn_mem


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


def cargar_indice(carpeta, progreso_callback=None, estado_callback=None):
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

    if os.path.exists(ruta_indice):
        load_started = time.perf_counter()
        with open(ruta_indice, 'r', encoding='utf-8') as f:
            index = json.load(f)
        stats.update(_index_stats(index))
        stats.update({
            "modo": "cache_json",
            "archivos_total": len(index),
            "json_load_elapsed": time.perf_counter() - load_started,
            "total_elapsed": time.perf_counter() - started_total,
            "json_mb": os.path.getsize(ruta_indice) / 1024 / 1024 if os.path.exists(ruta_indice) else 0.0,
            "sqlite_mb": os.path.getsize(ruta_bd) / 1024 / 1024 if os.path.exists(ruta_bd) else 0.0,
        })
        stats["total_files_per_sec"] = (len(index) / stats["total_elapsed"]) if stats["total_elapsed"] > 0 else 0.0
        summary_path = _save_index_perf_log(carpeta_datos, stats)
        logging.info(f"Índice cargado desde: {ruta_indice}")
        _emit_estado(estado_callback, f"Índice cargado ({len(index)} pistas)")
        if summary_path:
            _emit_estado(estado_callback, f"Log de indexado: {summary_path}")
        return index

    logging.info(f"No se encontró índice. Iniciando indexación en: {carpeta}")
    _emit_estado(estado_callback, "Escaneando carpetas de música...")

    scan_started = time.perf_counter()
    archivos = _scan_audio_files(carpeta)
    scan_elapsed = time.perf_counter() - scan_started
    total = len(archivos)
    num_workers = min(32, os.cpu_count() or 8)
    stats.update({
        "modo": "indexado_completo",
        "workers": num_workers,
        "archivos_total": total,
        "scan_elapsed": scan_elapsed,
    })

    _emit_estado(estado_callback, f"Archivos encontrados: {total}. Extrayendo metadata...")
    _emit_progress(progreso_callback, 0)

    index = []
    errores_metadata = 0
    processed = 0
    last_emit_ts = 0.0
    next_emit_count = 0
    emit_every = max(100, min(1000, max(1, total // 100))) if total else 1

    metadata_started = time.perf_counter()
    if total:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(procesar_archivo, ruta) for ruta in archivos]
            for future in as_completed(futures):
                resultado = future.result()
                index.append({
                    "path": resultado.get("path"),
                    "tags": resultado.get("tags"),
                })
                processed += 1
                if not resultado.get("ok"):
                    errores_metadata += 1

                now = time.perf_counter()
                if processed >= next_emit_count or (now - last_emit_ts) >= 1.0 or processed == total:
                    pct = round(processed / total * 100)
                    _emit_progress(progreso_callback, pct)
                    _emit_estado(estado_callback, f"Indexando metadata {processed}/{total} ({pct}%)")
                    last_emit_ts = now
                    next_emit_count = processed + emit_every

    metadata_elapsed = time.perf_counter() - metadata_started
    stats.update({
        "metadata_elapsed": metadata_elapsed,
        "errores_metadata": errores_metadata,
        "metadata_files_per_sec": (total / metadata_elapsed) if metadata_elapsed > 0 else 0.0,
    })

    _emit_estado(estado_callback, "Preparando base SQLite en memoria...")
    sqlite_insert_started = time.perf_counter()
    conn_mem = _insertar_sqlite_en_memoria(index)
    stats["sqlite_insert_elapsed"] = time.perf_counter() - sqlite_insert_started

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

    _emit_estado(estado_callback, "Generando sugerencias de alias...")
    alias_started = time.perf_counter()
    try:
        ruta_sug, alias_count = _generar_sugerencias_alias(index, carpeta_datos)
        stats["alias_suggestions"] = alias_count
        logging.info(f"Sugerencias de alias generadas: {alias_count} -> {ruta_sug}")
        _emit_estado(estado_callback, "Sugerencias de alias generadas (revisar en GUI)")
    except Exception as e:
        stats["alias_suggestions"] = 0
        logging.warning(f"No se pudieron generar sugerencias de alias: {e}")
    stats["alias_elapsed"] = time.perf_counter() - alias_started

    stats.update(_index_stats(index))
    stats["total_elapsed"] = time.perf_counter() - started_total
    stats["total_files_per_sec"] = (total / stats["total_elapsed"]) if stats["total_elapsed"] > 0 else 0.0

    summary_path = _save_index_perf_log(carpeta_datos, stats)
    if summary_path:
        _emit_estado(estado_callback, f"Log de indexado: {summary_path}")
    _emit_progress(progreso_callback, 100)

    return index
