import os
import json
import hashlib
import logging
import sqlite3
import re
from mutagen import File
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# NUEVO: sugeridor de alias desacoplado
import alias_suggester

logging.basicConfig(level=logging.INFO)

progreso_lock = Lock()
estado_lock = Lock()

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

        # Caso 1: Sin tags
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

        # Caso 2: Tag parcial → título incluye artista
        elif artista is None and titulo and ' - ' in titulo:
            auto_tags = True
            partes = titulo.split(' - ', 1)
            posible_artista = partes[0].strip()
            posible_titulo = partes[1].strip()

            # Validar que no hay duplicación en el título
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

def procesar_archivo(args):
    ruta, total, contador, progreso_callback, estado_callback = args
    tags = extraer_tags(ruta)
    with progreso_lock:
        contador["valor"] += 1
        actual = contador["valor"]
        if progreso_callback:
            progreso_callback(round(actual / total * 100))
        if estado_callback:
            estado_callback(f"Indexando archivo {actual} de {total}")
    return {
        "path": ruta,
        "tags": tags
    }

def cargar_indice(carpeta, progreso_callback=None, estado_callback=None):
    carpeta_datos = os.path.join(".", "datos")
    os.makedirs(carpeta_datos, exist_ok=True)
    hash_nombre = generar_hash_carpeta(carpeta)
    ruta_indice = os.path.join(carpeta_datos, f"mp3_index_{hash_nombre}.json")
    ruta_bd = os.path.join(carpeta_datos, "coincidencias.db")

    if os.path.exists(ruta_indice):
        with open(ruta_indice, 'r', encoding='utf-8') as f:
            logging.info(f"Índice cargado desde: {ruta_indice}")
            if estado_callback:
                estado_callback("Índice cargado")
            return json.load(f)

    logging.info(f"No se encontró índice. Iniciando indexación en: {carpeta}")
    if estado_callback:
        estado_callback("Indexando archivos...")

    archivos = []
    for root, _, files in os.walk(carpeta):
        for file in files:
            if file.lower().endswith(('.mp3', '.flac', '.wav', '.m4a')):
                archivos.append(os.path.join(root, file))

    total = len(archivos)
    conn_mem = inicializar_bd_memoria()
    cursor = conn_mem.cursor()
    insert_values = []
    index = []
    contador = {"valor": 0}

    args = [(ruta, total, contador, progreso_callback, estado_callback) for ruta in archivos]
    num_workers = min(32, os.cpu_count() or 8)

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        for resultado in executor.map(procesar_archivo, args):
            index.append(resultado)
            if resultado["tags"]:
                insert_values.append((
                    resultado["path"],
                    resultado["tags"].get("title"),
                    resultado["tags"].get("artist"),
                    resultado["tags"].get("duration"),
                    resultado["tags"].get("bitrate"),
                    int(resultado["tags"].get("auto_tags", False))
                ))

    # Insertar en SQLite en memoria
    cursor.executemany("""
        INSERT OR REPLACE INTO indice_audio (path, title, artist, duration, bitrate, auto_tags)
        VALUES (?, ?, ?, ?, ?, ?)
    """, insert_values)
    conn_mem.commit()

    # Guardar JSON en disco
    with open(ruta_indice, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        logging.info(f"Índice guardado en: {ruta_indice}")
        if estado_callback:
            estado_callback("Índice guardado")

    # Guardar base de datos desde memoria a disco
    try:
        conn_disco = sqlite3.connect(ruta_bd)
        conn_mem.backup(conn_disco)
        conn_disco.close()
        conn_mem.close()
        logging.info(f"Base de datos guardada en: {ruta_bd}")
    except Exception as e:
        logging.warning(f"Error al guardar la base de datos desde memoria: {e}")

    # ============================================================
    # NUEVO: generar sugerencias de alias (RAM → JSON)
    # ============================================================
    try:
        sugg = alias_suggester.ArtistAliasSuggester()
        for item in index:
            tags = item.get("tags") or {}
            if not tags:
                continue
            artista = tags.get("artist")
            titulo  = tags.get("title")
            dur     = tags.get("duration")
            if artista:
                sugg.add(artista, titulo, dur, item.get("path") or "")

        suggestions = sugg.build_suggestions()
        ruta_sug = os.path.join(carpeta_datos, "alias_suggestions.json")
        sugg.save_suggestions(ruta_sug, suggestions)
        logging.info(f"Sugerencias de alias generadas: {len(suggestions)} → {ruta_sug}")
        if estado_callback:
            estado_callback("Sugerencias de alias generadas (revisar en GUI)")
    except Exception as e:
        logging.warning(f"No se pudieron generar sugerencias de alias: {e}")

    return index
