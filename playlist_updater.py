# playlist_updater.py
import os
import re
import logging
import unicodedata
from typing import List, Dict, Tuple, Optional

import tkinter as tk
from tkinter import filedialog, ttk, messagebox

try:
    from mutagen import File as MutaFile
except Exception:
    MutaFile = None  # si mutagen no está, seguimos sin similitud avanzada

from matcher import buscar_mejor_coincidencia
from indexer import cargar_indice

logging.basicConfig(level=logging.INFO)

_EXTS = (".mp3", ".mp4", ".m4a", ".flac", ".wav", ".wma", ".aac", ".webm", ".ogg", ".mkv", ".avi")


def _es_ruta_de_medio(linea: str) -> bool:
    linea = (linea or "").strip()
    if not linea or linea.startswith("#"):
        return False
    if linea.lower().startswith("netsearch://"):
        return True
    low = linea.lower()
    if any(low.endswith(ext) for ext in _EXTS):
        if "<" in low or ">" in low:
            return False
        return True
    return False


# --- Limpieza de artista en metadatos de playlist ---
ARTISTAS_BASURA = {
    "videodj ralph", "video dj ralph", "unknown", "unknown artist",
    "varios", "various", "desconocido", "sin artista", "dj", "v/a"
}

CANAL_REGEX = re.compile(
    r"\b(vevo|records?|archiv|music|channel|oficial|official|videos?)\b",
    re.IGNORECASE
)


def _strip_accents(s: str) -> str:
    if not s:
        return s
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))


def _norm_min(s: str) -> str:
    s = (s or "").replace("_", " ")
    s = _strip_accents(s).lower()
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s


def _looks_like_channel(artist_norm: str) -> bool:
    return bool(artist_norm and CANAL_REGEX.search(artist_norm))


def _split_artist_from_title_if_needed(artist: Optional[str], title: Optional[str]) -> Optional[str]:
    """
    Si el artist es vacío/basura o parece 'canal' (VEVO, Records, Music...),
    o si no coincide con el prefijo del título 'Artista - Título', intenta extraer el artista real del título.
    """
    na = _norm_min(artist or "")
    base = re.sub(r"\s*\(.*?\)\s*", " ", title or "")
    if " - " in base:
        arti, _ = base.split(" - ", 1)
        arti = arti.strip()
        narti = _norm_min(arti)

        cond_basura_o_canal = (not na) or (na in ARTISTAS_BASURA) or _looks_like_channel(na)
        cond_mismatch = narti and (na != narti) and (narti not in na) and (na not in narti)

        if cond_basura_o_canal or cond_mismatch:
            return arti  # sustituimos por el artista del título
    # si no se pudo extraer, devolvemos el original
    return artist


_EXTVDJ_TAG_RE = re.compile(r"<([^>]+)>(.*?)</\1>", re.IGNORECASE)


def _parse_extvdj_line(line: str) -> Dict[str, object]:
    cur: Dict[str, object] = {}
    for match in _EXTVDJ_TAG_RE.finditer(line or ""):
        key = (match.group(1) or "").strip().lower()
        value = (match.group(2) or "").strip()
        if not key:
            continue
        if key == "filesize":
            try:
                cur["filesize"] = int(float(value))
            except Exception:
                cur["filesize"] = value
        elif key == "lastplaytime":
            try:
                cur["lastplaytime"] = int(float(value))
            except Exception:
                cur["lastplaytime"] = value
        elif key == "songlength":
            try:
                cur["duration"] = float(value)
            except Exception:
                pass
        else:
            cur[key] = value
    if "artist" in cur:
        cur["artist"] = _split_artist_from_title_if_needed(cur.get("artist"), cur.get("title"))
    return cur


def _parse_m3u_with_meta(ruta_m3u: str) -> Tuple[List[Dict], Dict[str, Dict]]:
    entradas: List[Dict] = []
    meta_map: Dict[str, Dict] = {}
    last_meta: Dict[str, object] = {}

    with open(ruta_m3u, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            # eliminar BOM si lo hubiera (UTF-8 with BOM)
            line = raw.replace("\ufeff", "").strip()
            if not line:
                continue

            if line.startswith("#EXTVDJ:"):
                cur = _parse_extvdj_line(line)
                last_meta = cur
                continue

            if _es_ruta_de_medio(line):
                tag_copy = dict(last_meta) if last_meta else None
                if tag_copy:
                    tag_copy["artist"] = _split_artist_from_title_if_needed(
                        tag_copy.get("artist"), tag_copy.get("title")
                    )
                    meta_map[line] = tag_copy
                entradas.append({"ruta": line, "tags": tag_copy})
                last_meta = {}
            # otras líneas se ignoran

    return entradas, meta_map


def _format_extvdj_line(meta: Optional[Dict[str, object]] = None, path: Optional[str] = None) -> str:
    meta = dict(meta or {})
    pieces: list[str] = []

    filesize = meta.get("filesize")
    if path and os.path.exists(path):
        try:
            filesize = os.path.getsize(path)
        except Exception:
            pass
    if filesize not in (None, ""):
        try:
            pieces.append(f"<filesize>{int(float(filesize))}</filesize>")
        except Exception:
            pass

    lastplaytime = meta.get("lastplaytime")
    if lastplaytime not in (None, ""):
        try:
            pieces.append(f"<lastplaytime>{int(float(lastplaytime))}</lastplaytime>")
        except Exception:
            pass

    artist = meta.get("artist")
    if artist:
        pieces.append(f"<artist>{artist}</artist>")

    title = meta.get("title")
    if title:
        pieces.append(f"<title>{title}</title>")

    remix = meta.get("remix")
    if remix:
        pieces.append(f"<remix>{remix}</remix>")

    duration = meta.get("duration")
    if duration not in (None, ""):
        try:
            pieces.append(f"<songlength>{float(duration):.3f}</songlength>")
        except Exception:
            pass

    return "#EXTVDJ:" + "".join(pieces)


def _read_tags_from_file(path: str) -> Dict[str, object]:
    out = {"title": None, "artist": None, "duration": None}
    if not MutaFile:
        return out
    try:
        mf = MutaFile(path, easy=True)
        if mf is None:
            return out
        if "title" in mf and mf["title"]:
            out["title"] = mf["title"][0]
        if "artist" in mf and mf["artist"]:
            out["artist"] = mf["artist"][0]
        if hasattr(mf, "info") and getattr(mf.info, "length", None):
            out["duration"] = float(mf.info.length)
    except Exception:
        pass
    return out


def _score_strings(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    na, nb = _norm_min(a), _norm_min(b)
    import difflib
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _estimate_similarity(missing_meta: Optional[Dict], chosen_path: str) -> float:
    try:
        tags_chosen = _read_tags_from_file(chosen_path)
        if missing_meta:
            a = missing_meta.get("artist") or ""
            t = missing_meta.get("title") or ""
            d = missing_meta.get("duration")
        else:
            base = os.path.basename(chosen_path)
            if " - " in base:
                a, t = base.split(" - ", 1)
            else:
                a, t = "", base
            d = None
        score = _score_strings(a, tags_chosen.get("artist")) * 0.3 + _score_strings(t, tags_chosen.get("title")) * 0.5
        if d and tags_chosen.get("duration"):
            dd = abs(float(d) - float(tags_chosen["duration"]))
            score += max(0.0, 0.2 - dd / 100.0)
        return score * 100
    except Exception:
        return 0.0


class _ManualResolver(tk.Toplevel):
    """Se conserva por compatibilidad, pero NO se invoca desde update_playlist_logic."""
    def __init__(self, master, missing: List[str], meta_map: Dict[str, Dict]):
        super().__init__(master)
        self.title("Resolver manualmente")
        self.geometry("900x600")
        self.resizable(True, True)
        self.missing = []
        self.meta_map = meta_map
        self.selections = {}

        frm = ttk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tree = ttk.Treeview(frm, columns=("meta", "chosen"), show="headings")
        self.tree.heading("meta", text="Meta")
        self.tree.heading("chosen", text="Selección manual")
        self.tree.pack(fill=tk.BOTH, expand=True)

        btnfrm = ttk.Frame(self)
        btnfrm.pack(fill=tk.X, padx=10, pady=10)

        self.btn_choose = ttk.Button(btnfrm, text="Elegir archivo...", command=self._choose_file)
        self.btn_choose.pack(side=tk.LEFT)

        self.btn_done = ttk.Button(btnfrm, text="Finalizar", command=self._finish)
        self.btn_done.pack(side=tk.RIGHT)

        for it in missing:
            meta = meta_map.get(it)
            label = _format_missing_label(it, meta)
            self.missing.append({"key": it, "meta": meta, "label": label})
            self.tree.insert("", tk.END, iid=it, values=(label, ""))

    def _choose_file(self):
        cur = self.tree.focus() or ""
        if not cur:
            return
        path = filedialog.askopenfilename(title="Escoge el archivo correcto")
        if not path:
            return
        score = _estimate_similarity(self.meta_map.get(cur), path)
        self.selections[cur] = path
        self.tree.set(cur, "chosen", f"{path}  [sim={score:.1f}]")

    def _finish(self):
        self.destroy()

    def show(self):
        self.grab_set()
        self.wait_window()
        return self.selections


def _format_missing_label(texto_original: str, meta: Optional[Dict]) -> str:
    if meta and (meta.get("artist") or meta.get("title")):
        a = meta.get("artist") or ""
        t = meta.get("title") or ""
        base = f"{a} - {t}".strip(" -")
    else:
        base = os.path.basename(texto_original) or texto_original
    return base


def update_playlist_logic(carpeta_musica: str, archivo_playlist: str,
                          progreso_callback=None, estado_callback=None, threshold: int = 70,
                          solo_bitrate_bajo: bool = False, bitrate_minimo: int = 320,
                          bitrate_overrides: Optional[Dict[str, int]] = None,
                          output_path: Optional[str] = None):
    import os
    import logging
    import tkinter as tk
    from typing import Dict, Optional, List
    # usamos helpers ya definidos en el archivo:
    # _parse_m3u_with_meta, _format_missing_label, buscar_mejor_coincidencia, cargar_indice

    if not os.path.isfile(archivo_playlist):
        raise FileNotFoundError(f"No existe la playlist: {archivo_playlist}")

    if estado_callback:
        estado_callback("Iniciando actualización...")

    # 1) Parsear playlist y metadatos #EXTVDJ
    entradas, meta_map = _parse_m3u_with_meta(archivo_playlist)

    # 2) Cargar índice (usa callbacks → muestra progreso en GUI)
    indice = cargar_indice(
        carpeta_musica,
        progreso_callback=progreso_callback,
        estado_callback=estado_callback
    )
    logging.info(f"Índice cargado desde: {indice.get('fuente', 'desconocida')}" if isinstance(indice, dict) else "Índice cargado")

    if estado_callback:
        estado_callback("Índice cargado")

    # 3) Buscar coincidencias (matcher gestiona multiproceso y progresos)
    if estado_callback:
        estado_callback("Preparando búsqueda… lanzando procesos. Los primeros resultados pueden tardar unos segundos.")

    def _estado_router(texto: str):
        # Estados breves a la GUI; logs largos a consola.
        t = (texto or "")
        is_long = ("\n" in t) or t.startswith("\n") or t.startswith("─── Entrada") or any(m in t for m in ("🎧", "🔎", "✅", "❌", "📊", "🕒", "⚠️"))
        if is_long:
            print(t)
        else:
            if estado_callback:
                estado_callback(t)

    resultados, no_encontradas = buscar_mejor_coincidencia(
        entradas,
        progreso_callback=progreso_callback,
        estado_callback=_estado_router,
        threshold=threshold,
        solo_bitrate_bajo=solo_bitrate_bajo,
        bitrate_minimo=bitrate_minimo,
        bitrate_overrides=bitrate_overrides or {},
    )

    if estado_callback:
        estado_callback("Coincidencias evaluadas")

    # 4) Construir nueva playlist y guardarla
    base, _ = os.path.splitext(archivo_playlist)
    nueva_ruta = output_path or f"{base}_actualizada.m3u"
    os.makedirs(os.path.dirname(nueva_ruta) or ".", exist_ok=True)
    resolved_iter = iter(resultados)
    missing_counter = {}
    for ruta in no_encontradas:
        missing_counter[ruta] = missing_counter.get(ruta, 0) + 1
    indice_lookup = {
        os.path.normpath(item.get("path") or ""): item.get("tags") or {}
        for item in indice
        if item.get("path")
    }
    with open(nueva_ruta, "w", encoding="utf-8", errors="ignore") as out:
        out.write("#EXTM3U\n")
        for entrada in entradas:
            ruta_original = entrada.get("ruta") or ""
            if missing_counter.get(ruta_original, 0) > 0:
                missing_counter[ruta_original] -= 1
                continue
            ruta_resultado = next(resolved_iter, "")
            if not ruta_resultado:
                continue
            tags_original = dict(entrada.get("tags") or {})
            tags_resueltas = dict(indice_lookup.get(os.path.normpath(ruta_resultado), {}))
            meta = dict(tags_original)
            meta.update({k: v for k, v in tags_resueltas.items() if v not in (None, "")})
            if "lastplaytime" in tags_original:
                meta["lastplaytime"] = tags_original.get("lastplaytime")
            out.write(_format_extvdj_line(meta, ruta_resultado) + "\n")
            out.write(f"{ruta_resultado}\n")
    logging.info(f"Playlist actualizada guardada en: {nueva_ruta}")

    # 5) NO abrimos diálogo interno; devolvemos faltantes para que la GUI los resuelva.
    if no_encontradas:
        logging.info(f"No encontradas (pendientes de resolver en GUI): {len(no_encontradas)}")

    if estado_callback:
        estado_callback("¡Actualización completada!")

    return nueva_ruta, no_encontradas
