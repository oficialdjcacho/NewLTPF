# gui.py
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import tkinter as tk
from typing import Any, Optional

try:
    import vlc  # type: ignore
except Exception:
    vlc = None

from playlist_updater import _format_extvdj_line, _parse_m3u_with_meta, update_playlist_logic
from indexer import cargar_indice
from matcher import (
    _artista_con_fallback,
    _es_remix,
    _filtrar_por_artista_esperado,
    _preferir_no_remix,
    _titulo_puro,
    calcular_puntaje,
    normalizar_artista,
    sim_nombre,
)

import manual_cache
import alias_suggester

CONFIG_FILE = "config.json"
DATOS_DIR = os.path.join(".", "datos")
ALIAS_FILE = os.path.join(DATOS_DIR, "aliasconfig.json")
ALIAS_SUG_FILE = os.path.join(DATOS_DIR, "alias_suggestions.json")

AUDIO_VIDEO_EXTS = [
    ("Audio/Video", "*.mp3 *.flac *.wav *.m4a *.mp4 *.webm *.avi *.mkv *.ogg *.wma *.aac"),
    ("Todos", "*.*"),
]
M3U_FILTER = [("M3U files", "*.m3u *.m3u8")]
ALLOWED_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".mp4", ".webm", ".avi", ".mkv", ".ogg", ".wma", ".aac"}

ALIAS_TEMPLATE = {"artist_alias": {}, "rules": {"normalize_diminutives": True}}
SIM_THRESHOLD = 60.0

RUIDO_TITULO = [
    r"\bofficial\s*music\s*video\b",
    r"\bofficial\s*video\b",
    r"\bofficial\b",
    r"\bvideo\b",
    r"\baudio\b",
    r"\blyrics?\b",
    r"\bcover\b",
    r"\bmv\b",
    r"\bhd\b",
    r"\b4k\b",
    r"\bremaster(?:ed)?\b",
    r"\b(clip|videoclip)\b",
    r"\bvevo\b",
    r"videodj\s*ralph",
]
RUIDO_REGEX = re.compile("|".join(RUIDO_TITULO), re.IGNORECASE)


def quitar_acentos(s: str) -> str:
    if not s:
        return s
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def limpiar_parentesis_y_corchetes(s: str) -> str:
    if not s:
        return s
    before, result = None, s
    while before != result:
        before = result
        result = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", result)
    return result


def limpiar_ruido(s: str) -> str:
    if not s:
        return s
    s2 = s.replace("_", " ")
    s2 = limpiar_parentesis_y_corchetes(s2)
    s2 = RUIDO_REGEX.sub(" ", s2)
    s2 = re.sub(r"\s+", " ", s2).strip(" .-_")
    return s2


def normalizar(s: str) -> str:
    if not s:
        return ""
    s2 = quitar_acentos(s)
    s2 = limpiar_ruido(s2)
    return s2.lower().strip()


def rough_similarity(a: str, b: str) -> float:
    na, nb = normalizar(a), normalizar(b)
    if not na or not nb:
        return 0.0
    ta = set(t for t in re.split(r"[^a-z0-9]+", na) if t)
    tb = set(t for t in re.split(r"[^a-z0-9]+", nb) if t)
    inter = len(ta & tb)
    union = len(ta | tb) or 1
    jacc = inter / union
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return (0.6 * ratio + 0.4 * jacc) * 100.0


def _is_media_line(line: str) -> bool:
    if not line:
        return False
    stripped = line.strip()
    return bool(stripped and not stripped.startswith("#"))


def _is_generated_playlist(path: str) -> bool:
    if not path:
        return False
    base = os.path.basename(path).lower()
    return bool(re.search(r"_actualizada(?:_actualizada)?\.(m3u8?|m3u)$", base))


def _base_playlist_name(path: str) -> str:
    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    while name.lower().endswith("_actualizada"):
        name = name[: -len("_actualizada")]
    return f"{name}{ext}"


def parse_m3u_meta_map(playlist_path: str) -> dict:
    meta_map, last_meta = {}, {}
    if not playlist_path or not os.path.isfile(playlist_path):
        return meta_map
    try:
        with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("#EXTVDJ:"):
                    current = {}
                    for match in re.finditer(r"<([^>]+)>(.*?)</\1>", line):
                        key = match.group(1).strip().lower()
                        value = match.group(2).strip()
                        if key == "filesize":
                            try:
                                current["filesize"] = int(float(value))
                            except Exception:
                                current["filesize"] = value
                        elif key == "lastplaytime":
                            try:
                                current["lastplaytime"] = int(float(value))
                            except Exception:
                                current["lastplaytime"] = value
                        elif key == "songlength":
                            current["duration"] = value
                        else:
                            current[key] = value
                    last_meta = current
                    continue
                if line.startswith("#"):
                    continue
                meta_map[line] = dict(last_meta) if last_meta else {}
                last_meta = {}
    except Exception:
        pass
    return meta_map


def pretty_meta(meta: dict) -> str:
    if not meta:
        return ""
    artist = (meta.get("artist") or "").strip()
    title = (meta.get("title") or "").strip()
    if artist and title:
        return f"{artist} — {title}"
    return artist or title


def _normalize_key(s: str) -> str:
    s = quitar_acentos(s or "")
    s = re.sub(r"[^\w\s&\+]", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()


def _existing_alias_pairs_norm() -> set:
    try:
        with open(ALIAS_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        amap = cfg.get("artist_alias", {}) or {}
    except Exception:
        amap = {}
    return {(_normalize_key(v), _normalize_key(c)) for v, c in amap.items()}


@dataclass
class EntryRecord:
    index: int
    original_path: str
    tags: dict | None
    route_valid: bool = False
    resolved_path: str = ""
    status: str = "pendiente"
    origin: str = "auto"
    score: float = 0.0
    bitrate: int = 0
    manual: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlaylistRecord:
    path: str
    entries: list[EntryRecord]
    output_path: str = ""
    state: str = "pendiente"
    progress: int = 0
    total: int = 0
    found: int = 0
    manual: int = 0
    missing: int = 0
    invalid: int = 0
    error: str = ""


class PlaybackController:
    def __init__(self, on_status=None):
        self.on_status = on_status
        self.available = False
        self._vlc = None
        self.instance = None
        self.player = None
        self.current_path = ""
        try:
            if vlc is None:
                raise RuntimeError("python-vlc no disponible")
            self._vlc = vlc
            self.instance = vlc.Instance()
            self.player = self.instance.media_player_new()
            self.available = True
        except Exception:
            self.available = False

    def _emit(self, text: str):
        if self.on_status:
            self.on_status(text)

    def load(self, path: str) -> bool:
        if not self.available or not path or not os.path.exists(path):
            return False
        try:
            media = self.instance.media_new(path)
            self.player.set_media(media)
            self.current_path = path
            return True
        except Exception as exc:
            self._emit(f"Reproductor: error cargando archivo: {exc}")
            return False

    def play(self) -> bool:
        if not self.available:
            return False
        try:
            self.player.play()
            return True
        except Exception:
            return False

    def pause(self) -> bool:
        if not self.available:
            return False
        try:
            self.player.pause()
            return True
        except Exception:
            return False

    def stop(self) -> bool:
        if not self.available:
            return False
        try:
            self.player.stop()
            return True
        except Exception:
            return False

    def set_volume(self, value: int) -> None:
        if self.available:
            try:
                self.player.audio_set_volume(int(value))
            except Exception:
                pass

    def get_time_ms(self) -> int:
        if not self.available:
            return 0
        try:
            return int(self.player.get_time() or 0)
        except Exception:
            return 0

    def get_duration_ms(self) -> int:
        if not self.available:
            return 0
        try:
            return int(self.player.get_length() or 0)
        except Exception:
            return 0

    def is_playing(self) -> bool:
        if not self.available:
            return False
        try:
            return bool(self.player.is_playing())
        except Exception:
            return False


class PlaylistUpdaterApp:
    def __init__(self, master):
        self.master = master
        self.master.title("LTPF — Nueva UI")
        self.master.geometry("1460x900")
        self.master.minsize(1280, 760)

        self.folder_path = tk.StringVar()
        self.playlist_path = tk.StringVar()
        self.threshold = tk.IntVar(value=70)
        self.playlist_paths: list[str] = []
        self.batch_playlists_root = tk.StringVar()
        self.include_subdirs = tk.BooleanVar(value=True)
        self.dest_mode = tk.StringVar(value="original")
        self.dest_folder = tk.StringVar()
        self.keep_structure = tk.BooleanVar(value=True)
        self.open_manual_during_batch = tk.BooleanVar(value=False)
        self.selective_low_bitrate = tk.BooleanVar(value=True)

        self.status_text = tk.StringVar(value="Listo")
        self.current_playlist_text = tk.StringVar(value="Playlist activa: —")
        self.current_entry_text = tk.StringVar(value="Entrada activa: —")
        self.current_progress_text = tk.StringVar(value="0%")
        self.player_state_text = tk.StringVar(value="Reproductor: detenido")
        self.player_track_text = tk.StringVar(value="Pista: —")
        self.player_time_text = tk.StringVar(value="00:00 / 00:00")
        self.library_path_text = tk.StringVar(value="Biblioteca: —")
        self.playlist_path_text = tk.StringVar(value="Playlist: —")
        self.batch_root_text = tk.StringVar(value="Lote: —")
        self.dest_path_text = tk.StringVar(value="Destino: —")

        self._post_batch_queue: list[tuple[str, str, list[str]]] = []
        self._post_batch_summary_lines = None
        self._playlist_records: dict[str, PlaylistRecord] = {}
        self._playlist_order: list[str] = []
        self._preview_bitrate_overrides: dict[str, dict[str, int]] = {}
        self._selected_playlist_path: str | None = None
        self._selected_entry_index: int | None = None
        self._analysis_running = False
        self._analysis_stop = False
        self._preview_running = False
        self._preview_stop = False
        self._library_index: list[dict[str, Any]] | None = None
        self._library_lookup: dict[str, dict[str, Any]] = {}
        self._library_index_lock = threading.Lock()
        self._current_candidates: list[dict[str, Any]] = []
        self._current_candidate_index: int | None = None
        self._candidate_request_id = 0
        self._current_manual_override: dict[int, str] = {}
        self._progress_refresh_state: dict[str, dict[str, float | int]] = {}
        self._last_route_status: tuple[str, float] = ("", 0.0)

        self.playback = PlaybackController(on_status=self._set_player_state)

        self._build_ui()
        self.load_config()
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_player_state()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self.master, padding=(8, 8, 8, 4))
        top.pack(fill="x")

        actions = ttk.Frame(top)
        actions.pack(fill="x")
        ttk.Button(actions, text="Carpeta música…", command=self.select_folder).pack(side="left", padx=3)
        ttk.Button(actions, text="Playlists…", command=self.select_playlists_multi).pack(side="left", padx=3)
        ttk.Button(actions, text="Carpeta lote…", command=self.select_batch_folder).pack(side="left", padx=3)
        ttk.Button(actions, text="Analizar", command=self.run_update).pack(side="left", padx=3)
        ttk.Button(actions, text="Guardar salida", command=self.save_current_playlist_output).pack(side="left", padx=3)
        ttk.Button(actions, text="Alias…", command=self.open_alias_editor).pack(side="left", padx=3)
        ttk.Button(actions, text="Sugerencias…", command=self.open_alias_suggestions_dialog).pack(side="left", padx=3)
        ttk.Button(actions, text="Limpiar caché", command=self.clear_cache_db).pack(side="left", padx=3)
        ttk.Button(actions, text="Salir", command=self.on_close).pack(side="right", padx=3)

        settings = ttk.LabelFrame(top, text="Salida", padding=(8, 6))
        settings.pack(fill="x", pady=(8, 0))
        mode_row = ttk.Frame(settings)
        mode_row.pack(fill="x")
        ttk.Radiobutton(mode_row, text="Guardar junto a la playlist original", variable=self.dest_mode, value="original", command=self._on_dest_mode_changed).pack(side="left")
        ttk.Radiobutton(mode_row, text="Guardar en otra ruta", variable=self.dest_mode, value="custom", command=self._on_dest_mode_changed).pack(side="left", padx=(12, 0))
        browse_row = ttk.Frame(settings)
        browse_row.pack(fill="x", pady=(6, 0))
        self.dest_entry = ttk.Entry(browse_row, textvariable=self.dest_folder)
        self.dest_entry.pack(side="left", fill="x", expand=True)
        self.dest_btn = ttk.Button(browse_row, text="Elegir ruta…", command=self.select_dest_folder)
        self.dest_btn.pack(side="left", padx=(6, 0))
        self.keep_struct_chk = ttk.Checkbutton(
            browse_row,
            text="Mantener estructura",
            variable=self.keep_structure,
            command=self._refresh_route_labels,
        )
        self.keep_struct_chk.pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            browse_row,
            text="Abrir revisión manual durante lote",
            variable=self.open_manual_during_batch,
            command=self._refresh_route_labels,
        ).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            browse_row,
            text="Analizar solo rutas inválidas o < 320 kbps",
            variable=self.selective_low_bitrate,
            command=self._refresh_route_labels,
        ).pack(side="left", padx=(10, 0))

        routes = ttk.LabelFrame(top, text="Rutas activas", padding=(8, 6))
        routes.pack(fill="x", pady=(8, 0))
        ttk.Label(routes, textvariable=self.library_path_text).pack(anchor="w")
        ttk.Label(routes, textvariable=self.playlist_path_text).pack(anchor="w")
        ttk.Label(routes, textvariable=self.batch_root_text).pack(anchor="w")
        ttk.Label(routes, textvariable=self.dest_path_text).pack(anchor="w")

        status = ttk.Frame(top)
        status.pack(fill="x", pady=(8, 0))
        ttk.Label(status, textvariable=self.status_text).pack(side="left", padx=(0, 20))
        ttk.Label(status, textvariable=self.current_playlist_text).pack(side="left", padx=(0, 20))
        ttk.Label(status, textvariable=self.current_entry_text).pack(side="left", padx=(0, 20))
        ttk.Label(status, text="Progreso:").pack(side="left")
        ttk.Label(status, textvariable=self.current_progress_text).pack(side="left", padx=(4, 20))
        ttk.Label(status, textvariable=self.player_state_text).pack(side="left", padx=(0, 20))
        ttk.Label(status, textvariable=self.library_path_text).pack(side="left")

        self.progress = ttk.Progressbar(top, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 0))

        paned = ttk.PanedWindow(self.master, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        self.left_frame = ttk.Frame(paned, width=320)
        self.center_frame = ttk.Frame(paned, width=540)
        self.right_frame = ttk.Frame(paned, width=600)
        paned.add(self.left_frame, weight=1)
        paned.add(self.center_frame, weight=2)
        paned.add(self.right_frame, weight=2)

        self._build_left_panel()
        self._build_center_panel()
        self._build_right_panel()

    def _build_left_panel(self):
        ttk.Label(self.left_frame, text="Playlists", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.playlist_tree = ttk.Treeview(self.left_frame, columns=("estado", "total", "resueltas", "manual", "faltantes", "progreso"), show="tree headings", height=20)
        self.playlist_tree.heading("#0", text="Playlist")
        self.playlist_tree.heading("estado", text="Estado")
        self.playlist_tree.heading("total", text="Total")
        self.playlist_tree.heading("resueltas", text="Resueltas")
        self.playlist_tree.heading("manual", text="Manual")
        self.playlist_tree.heading("faltantes", text="Faltantes")
        self.playlist_tree.heading("progreso", text="%")
        self.playlist_tree.column("#0", width=220, anchor="w")
        for col, w in [("estado", 90), ("total", 55), ("resueltas", 70), ("manual", 60), ("faltantes", 70), ("progreso", 55)]:
            self.playlist_tree.column(col, width=w, anchor="center")
        ysb = ttk.Scrollbar(self.left_frame, orient="vertical", command=self.playlist_tree.yview)
        self.playlist_tree.configure(yscrollcommand=ysb.set)
        self.playlist_tree.pack(side="left", fill="both", expand=True, pady=(6, 0))
        ysb.pack(side="right", fill="y", pady=(6, 0))
        self.playlist_tree.bind("<<TreeviewSelect>>", self._on_playlist_selected)

    def _build_center_panel(self):
        header = ttk.Frame(self.center_frame)
        header.pack(fill="x")
        ttk.Label(header, text="Contenido de la playlist", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Label(header, text="Selecciona una playlist para ver sus entradas").pack(side="right")
        self.entries_tree = ttk.Treeview(
            self.center_frame,
            columns=("entrada", "ruta", "estado", "validez", "resultado", "origen", "score", "bitrate"),
            show="headings",
            height=22,
        )
        headings = [
            ("entrada", "#"),
            ("ruta", "Entrada / Ruta original"),
            ("estado", "Estado"),
            ("validez", "Ruta válida"),
            ("resultado", "Resultado"),
            ("origen", "Origen"),
            ("score", "Score"),
            ("bitrate", "Bitrate (kbps)"),
        ]
        for col, title in headings:
            self.entries_tree.heading(col, text=title)
        self.entries_tree.column("entrada", width=55, anchor="center")
        self.entries_tree.column("ruta", width=340, anchor="w")
        self.entries_tree.column("estado", width=110, anchor="center")
        self.entries_tree.column("validez", width=80, anchor="center")
        self.entries_tree.column("resultado", width=220, anchor="w")
        self.entries_tree.column("origen", width=70, anchor="center")
        self.entries_tree.column("score", width=70, anchor="center")
        self.entries_tree.column("bitrate", width=110, anchor="center")
        ysb = ttk.Scrollbar(self.center_frame, orient="vertical", command=self.entries_tree.yview)
        xsb = ttk.Scrollbar(self.center_frame, orient="horizontal", command=self.entries_tree.xview)
        self.entries_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.entries_tree.pack(fill="both", expand=True, pady=(6, 0))
        ysb.place(in_=self.entries_tree, relx=1.0, rely=0.0, relheight=1.0, x=0, y=0, anchor="ne")
        xsb.pack(fill="x")
        self.entries_tree.bind("<<TreeviewSelect>>", self._on_entry_selected)
        self.entries_tree.tag_configure("ok", background="#eaffea")
        self.entries_tree.tag_configure("warn", background="#fff7df")
        self.entries_tree.tag_configure("bad", background="#ffe3e3")
        self.entries_tree.tag_configure("manual", background="#e6f2ff")

    def _build_right_panel(self):
        self.right_notebook = ttk.Notebook(self.right_frame)
        self.right_notebook.pack(fill="both", expand=True)

        self.detail_tab = ttk.Frame(self.right_notebook, padding=8)
        self.candidates_tab = ttk.Frame(self.right_notebook, padding=8)
        self.player_tab = ttk.Frame(self.right_notebook, padding=8)
        self.logs_tab = ttk.Frame(self.right_notebook, padding=8)

        self.right_notebook.add(self.detail_tab, text="Detalle")
        self.right_notebook.add(self.candidates_tab, text="Candidatos")
        self.right_notebook.add(self.player_tab, text="Reproductor")
        self.right_notebook.add(self.logs_tab, text="Logs")

        # Detail
        detail_top = ttk.Frame(self.detail_tab)
        detail_top.pack(fill="x")
        self.detail_title = ttk.Label(detail_top, text="Entrada: —", font=("TkDefaultFont", 10, "bold"))
        self.detail_title.pack(anchor="w")
        self.detail_meta = ttk.Label(detail_top, text="Metadatos: —", wraplength=430, justify="left")
        self.detail_meta.pack(anchor="w", pady=(4, 0))
        self.detail_path = ttk.Label(detail_top, text="Ruta original: —", wraplength=430, justify="left")
        self.detail_path.pack(anchor="w", pady=(4, 0))
        self.detail_state = ttk.Label(detail_top, text="Estado: —")
        self.detail_state.pack(anchor="w", pady=(4, 0))
        self.detail_decision = ttk.Label(detail_top, text="Decisión: —", wraplength=430, justify="left")
        self.detail_decision.pack(anchor="w", pady=(4, 0))
        self.detail_route = ttk.Label(detail_top, text="Ruta válida: —")
        self.detail_route.pack(anchor="w", pady=(4, 0))
        self.detail_score = ttk.Label(detail_top, text="Score: —")
        self.detail_score.pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(self.detail_tab)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Seleccionar marcada", command=self.accept_selected_candidate).pack(side="left", padx=2)
        ttk.Button(actions, text="Buscar manualmente", command=self.manual_search_current_entry).pack(side="left", padx=2)
        ttk.Button(actions, text="Marcar como válido", command=self.mark_current_entry_valid).pack(side="left", padx=2)
        ttk.Button(actions, text="Saltar", command=self.skip_current_entry).pack(side="left", padx=2)

        # Candidates
        cand_top = ttk.Frame(self.candidates_tab)
        cand_top.pack(fill="x")
        ttk.Label(cand_top, text="Coincidencias disponibles", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.candidates_tree = ttk.Treeview(self.candidates_tab, columns=("score", "bitrate", "dur", "artist", "path"), show="headings", height=16)
        for col, title, width in [
            ("score", "Score", 70),
            ("bitrate", "Bitrate", 75),
            ("dur", "Duración", 80),
            ("artist", "Artista", 180),
            ("path", "Ruta", 350),
        ]:
            self.candidates_tree.heading(col, text=title)
            self.candidates_tree.column(col, width=width, anchor="w")
        self.candidates_tree.column("score", anchor="center")
        self.candidates_tree.column("bitrate", anchor="center")
        self.candidates_tree.pack(fill="both", expand=True, pady=(6, 0))
        self.candidates_tree.bind("<<TreeviewSelect>>", self._on_candidate_selected)
        cand_buttons = ttk.Frame(self.candidates_tab)
        cand_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(cand_buttons, text="Reproducir", command=self.play_selected_candidate).pack(side="left", padx=2)
        ttk.Button(cand_buttons, text="Pausa", command=self.pause_player).pack(side="left", padx=2)
        ttk.Button(cand_buttons, text="Detener", command=self.stop_player).pack(side="left", padx=2)
        ttk.Button(cand_buttons, text="Seleccionar marcada", command=self.accept_selected_candidate).pack(side="right", padx=2)
        ttk.Button(cand_buttons, text="Refrescar lista", command=self.refresh_current_candidates).pack(side="right", padx=2)

        # Player
        player_info = ttk.Frame(self.player_tab)
        player_info.pack(fill="x")
        ttk.Label(player_info, text="Reproductor integrado", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.player_track_label = ttk.Label(player_info, textvariable=self.player_track_text, wraplength=430, justify="left")
        self.player_track_label.pack(anchor="w", pady=(4, 0))
        self.player_time_label = ttk.Label(player_info, textvariable=self.player_time_text)
        self.player_time_label.pack(anchor="w", pady=(4, 0))
        vol_row = ttk.Frame(self.player_tab)
        vol_row.pack(fill="x", pady=(10, 0))
        ttk.Label(vol_row, text="Volumen").pack(side="left")
        self.volume_var = tk.IntVar(value=80)
        self.volume_scale = ttk.Scale(vol_row, from_=0, to=100, orient="horizontal", command=self._on_volume_change)
        self.volume_scale.set(80)
        self.volume_scale.pack(side="left", fill="x", expand=True, padx=(8, 0))
        btns = ttk.Frame(self.player_tab)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Play", command=self.play_selected_candidate).pack(side="left", padx=2)
        ttk.Button(btns, text="Pause/Resume", command=self.toggle_pause).pack(side="left", padx=2)
        ttk.Button(btns, text="Stop", command=self.stop_player).pack(side="left", padx=2)
        ttk.Label(self.player_tab, text="Si no hay python-vlc, el reproductor quedará desactivado.").pack(anchor="w", pady=(12, 0))

        # Logs
        self.log_text = ScrolledText(self.logs_tab, height=20, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self._log("UI inicializada.")
        if not self.playback.available:
            self._log("Reproductor no disponible: python-vlc no está instalado o VLC no está accesible.")

    # ----------------------------------------------------------------- Utils
    def _log(self, message: str):
        if threading.current_thread() is threading.main_thread():
            self._append_log(message)
        else:
            self.master.after(0, self._append_log, message)

    def _append_log(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{stamp}] {message}\n"
        try:
            self.log_text.insert("end", text)
            self.log_text.see("end")
        except Exception:
            pass
        print(message)

    def _trace(self, step: str, **fields):
        parts = [f"{k}={v}" for k, v in fields.items() if v is not None and v != ""]
        suffix = f" | {' | '.join(parts)}" if parts else ""
        self._log(f"[TRACE] {step}{suffix}")

    def _ui(self, callback, *args, **kwargs):
        self.master.after(0, lambda: callback(*args, **kwargs))

    def _set_status(self, text: str):
        if threading.current_thread() is threading.main_thread():
            self.status_text.set(text)
        else:
            self.master.after(0, self.status_text.set, text)

    def _set_player_state(self, text: str):
        if threading.current_thread() is threading.main_thread():
            self.player_state_text.set(text)
        else:
            self.master.after(0, self.player_state_text.set, text)

    def _format_mmss(self, ms: int) -> str:
        if not ms or ms < 0:
            return "00:00"
        seconds = ms // 1000
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _poll_player_state(self):
        try:
            if self.playback.available:
                current = self._format_mmss(self.playback.get_time_ms())
                total = self._format_mmss(self.playback.get_duration_ms())
                self.player_time_text.set(f"{current} / {total}")
        finally:
            self.master.after(500, self._poll_player_state)

    def _load_library_index(self):
        folder = self.folder_path.get().strip()
        if not folder or not os.path.isdir(folder):
            raise FileNotFoundError("Selecciona una carpeta de música válida.")
        loaded_from_cache = self._library_index is not None
        if not loaded_from_cache:
            with self._library_index_lock:
                if self._library_index is None:
                    started = time.perf_counter()
                    self._set_status("Cargando índice de biblioteca…")
                    self._trace("library_index_begin", folder=folder)
                    self._library_index = cargar_indice(folder, progreso_callback=None, estado_callback=self._log)
                    self._library_lookup = {}
                    for item in self._library_index or []:
                        path = os.path.normpath(item.get("path") or "")
                        if path:
                            self._library_lookup[path] = item
                    self.library_path_text.set(f"Biblioteca: {folder}")
                    self._trace("library_index_end", items=len(self._library_index or []), elapsed=f"{time.perf_counter() - started:.2f}s")
                else:
                    loaded_from_cache = True
        if loaded_from_cache and self._library_index is not None:
            self._trace("library_index_cache_hit", items=len(self._library_index or []))
        return self._library_index or []

    def _playlist_files_from_current_selection(self) -> list[str]:
        if self.playlist_paths:
            return [p for p in self.playlist_paths if os.path.isfile(p) and not _is_generated_playlist(p)]
        batch_root = self.batch_playlists_root.get().strip()
        if batch_root and os.path.isdir(batch_root):
            return self._list_playlists_in_folder(batch_root, self.include_subdirs.get())
        single = self.playlist_path.get().strip()
        if single and os.path.isfile(single) and not _is_generated_playlist(single):
            return [single]
        return []

    def _list_playlists_in_folder(self, root_dir: str, recursive: bool) -> list[str]:
        patterns = ["*.m3u", "*.m3u8"]
        files: list[str] = []
        if recursive:
            for base, _, _ in os.walk(root_dir):
                for pat in patterns:
                    files.extend(
                        [
                            os.path.abspath(p)
                            for p in __import__("glob").glob(os.path.join(base, pat))
                            if not _is_generated_playlist(p)
                        ]
                    )
        else:
            for pat in patterns:
                files.extend(
                    [
                        os.path.abspath(p)
                        for p in __import__("glob").glob(os.path.join(root_dir, pat))
                        if not _is_generated_playlist(p)
                    ]
                )
        return sorted(set(files))

    def _output_path_for_playlist(self, playlist_path: str) -> str:
        cleaned_name = _base_playlist_name(playlist_path)
        base_dir = os.path.dirname(playlist_path)
        base, _ = os.path.splitext(cleaned_name)
        if self.dest_mode.get() == "original":
            return os.path.join(base_dir, f"{base}_actualizada.m3u")
        target_root = self.dest_folder.get().strip()
        if not target_root:
            return os.path.join(base_dir, f"{base}_actualizada.m3u")
        playlist_dir = os.path.dirname(playlist_path)
        playlist_rel = cleaned_name
        batch_root = self.batch_playlists_root.get().strip()
        if self.keep_structure.get() and batch_root:
            try:
                rel_dir = os.path.relpath(playlist_dir, batch_root)
                if rel_dir and rel_dir != ".":
                    target_root = os.path.join(target_root, rel_dir)
            except Exception:
                pass
        os.makedirs(target_root, exist_ok=True)
        return os.path.join(target_root, f"{os.path.splitext(playlist_rel)[0]}_actualizada.m3u")

    def _read_media_lines(self, path: str) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return [ln.rstrip("\r\n") for ln in f if _is_media_line(ln)]
        except Exception:
            return []

    def _write_output_playlist(self, record: PlaylistRecord):
        record.output_path = self._output_path_for_playlist(record.path)
        try:
            os.makedirs(os.path.dirname(record.output_path) or ".", exist_ok=True)
            with open(record.output_path, "w", encoding="utf-8", errors="ignore") as f:
                f.write("#EXTM3U\n")
                for entry in record.entries:
                    if entry.resolved_path:
                        meta = self._output_meta_for_entry(entry)
                        f.write(_format_extvdj_line(meta, entry.resolved_path) + "\n")
                        f.write(f"{entry.resolved_path}\n")
            self._log(f"Playlist escrita: {record.output_path}")
        except Exception as exc:
            self._log(f"Error escribiendo salida: {exc}")

    def _refresh_record_counts(self, record: PlaylistRecord):
        record.total = len(record.entries)
        record.found = sum(1 for e in record.entries if e.status == "encontrada")
        record.manual = sum(1 for e in record.entries if e.status == "manual")
        record.missing = sum(1 for e in record.entries if e.status == "no encontrada")
        record.invalid = sum(1 for e in record.entries if e.status == "ruta inválida")
        done = record.found + record.manual + record.missing + record.invalid
        record.progress = int((done / record.total) * 100) if record.total else 0
        record.state = "completada" if record.total and done == record.total else record.state

    def _candidate_info(self, item: dict) -> dict:
        tags = item.get("tags") or {}
        return {
            "path": item.get("path") or "",
            "score": 0.0,
            "bitrate": int(tags.get("bitrate") or 0),
            "duration": float(tags.get("duration") or 0.0),
            "artist": tags.get("artist") or "",
            "title": tags.get("title") or "",
            "auto_tags": bool(tags.get("auto_tags", False)),
            "origin": "auto",
        }

    def _compute_candidates_for_entry(self, entry: EntryRecord, limit: int = 12, request_id: int | None = None) -> list[dict[str, Any]]:
        started = time.perf_counter()
        self._trace(
            "compute_candidates_begin",
            entry=entry.index,
            analysis_running=self._analysis_running,
            status=entry.status,
            manual=entry.manual,
        )
        index = self._load_library_index()
        if not index:
            self._trace("compute_candidates_end", entry=entry.index, total=0, elapsed=f"{time.perf_counter() - started:.2f}s", reason="no_index")
            return []
        base_tags = entry.tags or {}
        base_name = os.path.basename(entry.original_path) or entry.original_path
        artist_in = _artista_con_fallback(base_tags)
        candidates: list[dict[str, Any]] = []
        for idx, item in enumerate(index):
            if request_id is not None and request_id != self._candidate_request_id:
                self._trace(
                    "compute_candidates_aborted",
                    entry=entry.index,
                    request_id=request_id,
                    active_request_id=self._candidate_request_id,
                    processed=idx,
                    elapsed=f"{time.perf_counter() - started:.2f}s",
                )
                return []
            tags = item.get("tags") or {}
            path = item.get("path") or ""
            if not path:
                continue
            if base_tags and (base_tags.get("title") or base_tags.get("artist")):
                score = calcular_puntaje(base_tags, tags)
                if artist_in:
                    cand_artist = normalizar_artista(tags.get("artist") or "")
                    if not _filtrar_por_artista_esperado([item], artist_in):
                        continue
            else:
                score = sim_nombre(base_name, os.path.basename(path))
            if score <= 0:
                continue
            cand = self._candidate_info(item)
            cand["score"] = round(score, 2)
            candidates.append(cand)
        if not candidates:
            self._trace("compute_candidates_end", entry=entry.index, total=0, elapsed=f"{time.perf_counter() - started:.2f}s", reason="no_candidates")
            return []
        if base_tags.get("title") and not _es_remix(base_tags.get("title", "")):
            candidates.sort(key=lambda c: (c["score"], c["bitrate"]), reverse=True)
            candidates = _preferir_no_remix([{ "path": c["path"], "tags": {
                "title": c["title"], "artist": c["artist"], "bitrate": c["bitrate"], "duration": c["duration"], "auto_tags": c["auto_tags"]
            }} for c in candidates]) or candidates
            if candidates and isinstance(candidates[0], dict) and "tags" in candidates[0]:
                # reconvert helper format
                converted = []
                for item in candidates[:limit]:
                    tags = item.get("tags") or {}
                    converted.append({
                        "path": item.get("path") or "",
                        "score": 0.0,
                        "bitrate": int(tags.get("bitrate") or 0),
                        "duration": float(tags.get("duration") or 0.0),
                        "artist": tags.get("artist") or "",
                        "title": tags.get("title") or "",
                        "auto_tags": bool(tags.get("auto_tags", False)),
                        "origin": "auto",
                    })
                candidates = converted
        candidates.sort(key=lambda c: (c["score"], c["bitrate"]), reverse=True)
        self._trace(
            "compute_candidates_end",
            entry=entry.index,
            total=min(len(candidates), limit),
            elapsed=f"{time.perf_counter() - started:.2f}s",
            index_size=len(index),
        )
        return candidates[:limit]

    def _select_playlist_record(self, playlist_path: str, refresh_detail: Optional[bool] = None):
        self._selected_playlist_path = playlist_path
        record = self._playlist_records.get(playlist_path)
        if not record:
            self.entries_tree.delete(*self.entries_tree.get_children())
            self.current_playlist_text.set("Playlist activa: —")
            return
        if refresh_detail is None:
            refresh_detail = not self._analysis_running and not self._preview_running
        self._trace("select_playlist_record", playlist=os.path.basename(playlist_path), refresh_detail=refresh_detail)
        self.current_playlist_text.set(f"Playlist activa: {os.path.basename(record.path)}")
        self._populate_entries(record, refresh_detail=refresh_detail)
        if refresh_detail:
            self._select_first_entry_if_any(refresh_detail=True)

    def _populate_entries(self, record: PlaylistRecord, refresh_detail: bool = True):
        started = time.perf_counter()
        self.entries_tree.delete(*self.entries_tree.get_children())
        for entry in record.entries:
            label = pretty_meta(entry.tags or {}) or os.path.basename(entry.original_path) or entry.original_path
            route_valid = "Sí" if entry.route_valid else "No"
            result = os.path.basename(entry.resolved_path) if entry.resolved_path else ""
            score = f"{entry.score:.0f}" if entry.score else ""
            bitrate = f"{entry.bitrate} kbps" if entry.bitrate else ""
            tags = ()
            if entry.manual:
                tags = ("manual",)
            elif entry.status == "no encontrada" or entry.status == "ruta inválida":
                tags = ("bad",)
            elif entry.status in {"pendiente", "analizando"}:
                tags = ("warn",)
            else:
                tags = ("ok",)
            self.entries_tree.insert(
                "",
                "end",
                iid=str(entry.index),
                values=(entry.index + 1, label, entry.status, route_valid, result, entry.origin, score, bitrate),
                tags=tags,
            )
        self._trace("populate_entries", playlist=os.path.basename(record.path), entries=len(record.entries), refresh_detail=refresh_detail, elapsed=f"{time.perf_counter() - started:.2f}s")
        if refresh_detail:
            self._update_detail_from_selection()
        else:
            self._fill_candidates([])
            self.detail_title.config(text="Entrada: â€”")
            self.detail_meta.config(text="Metadatos: â€”")
            self.detail_path.config(text="Ruta original: â€”")
            self.detail_state.config(text="Estado: â€”")
            self.detail_decision.config(text="Decisión: â€”")
            self.detail_route.config(text="Ruta válida: â€”")
            self.detail_score.config(text="Score: â€”")

    def _sync_playlist_tree_item(self, playlist_path: str):
        record = self._playlist_records.get(playlist_path)
        if not record:
            return
        values = (record.state, record.total, record.found + record.manual, record.manual, record.missing, f"{record.progress}%")
        if self.playlist_tree.exists(playlist_path):
            self.playlist_tree.item(playlist_path, text=os.path.basename(playlist_path), values=values)
            self.playlist_tree.delete(*self.playlist_tree.get_children(playlist_path))
        else:
            self.playlist_tree.insert("", "end", iid=playlist_path, text=os.path.basename(playlist_path), values=values)
        self.playlist_tree.insert(playlist_path, "end", text=f"Ruta: {playlist_path}", values=("", "", "", "", "", ""))
        self.playlist_tree.insert(playlist_path, "end", text=f"Estado: {record.state}", values=("", "", "", "", "", ""))
        self.playlist_tree.insert(playlist_path, "end", text=f"Salida: {record.output_path or '—'}", values=("", "", "", "", "", ""))

    def _update_playlist_tree(self):
        self.playlist_tree.delete(*self.playlist_tree.get_children())
        for path in self._playlist_order:
            self._sync_playlist_tree_item(path)

    def _select_first_entry_if_any(self, refresh_detail: bool = True):
        ids = self.entries_tree.get_children()
        if ids:
            self.entries_tree.selection_set(ids[0])
            self.entries_tree.focus(ids[0])
            self.entries_tree.see(ids[0])
            if refresh_detail:
                self._on_entry_selected(None)

    def _entry_from_selection(self) -> Optional[EntryRecord]:
        if self._selected_playlist_path is None:
            return None
        record = self._playlist_records.get(self._selected_playlist_path)
        if not record:
            return None
        selection = self.entries_tree.selection()
        if not selection:
            return None
        try:
            idx = int(selection[0])
            self._selected_entry_index = idx
            return record.entries[idx]
        except Exception:
            return None

    def _update_detail_from_selection(self, refresh_candidates: bool = True):
        started = time.perf_counter()
        entry = self._entry_from_selection()
        if not entry:
            self.detail_title.config(text="Entrada: —")
            self.detail_meta.config(text="Metadatos: —")
            self.detail_path.config(text="Ruta original: —")
            self.detail_state.config(text="Estado: —")
            self.detail_decision.config(text="Decisión: —")
            self.detail_route.config(text="Ruta válida: —")
            self.detail_score.config(text="Score: —")
            self._fill_candidates([])
            return
        self._trace(
            "entry_selected",
            index=entry.index,
            status=entry.status,
            manual=entry.manual,
            route_valid=entry.route_valid,
            analysis_running=self._analysis_running,
            playlist=os.path.basename(self._selected_playlist_path or ""),
        )
        self.detail_title.config(text=f"Entrada: {entry.index + 1}")
        self.detail_meta.config(text=f"Metadatos: {pretty_meta(entry.tags or {}) or '(sin metadatos)'}")
        self.detail_path.config(text=f"Ruta original: {entry.original_path}")
        self.detail_state.config(text=f"Estado: {entry.status}")
        decision = entry.resolved_path or "(sin resolver)"
        if entry.manual:
            decision = f"Manual: {decision}"
        self.detail_decision.config(text=f"Decisión: {decision}")
        self.detail_route.config(text=f"Ruta válida: {'Sí' if entry.route_valid else 'No'}")
        bitrate = f"{entry.bitrate} kbps" if entry.bitrate else "—"
        self.detail_score.config(text=f"Score: {entry.score:.2f} | Bitrate: {bitrate}")
        if refresh_candidates:
            self._refresh_candidates_async(entry, reason="detail_selection")
            self._trace("detail_updated_requested", entry=entry.index, elapsed=f"{time.perf_counter() - started:.2f}s")
        else:
            self._fill_candidates([])
            self._trace("detail_updated_deferred", entry=entry.index, elapsed=f"{time.perf_counter() - started:.2f}s")

    def _refresh_candidates_async(self, entry: EntryRecord, reason: str = "manual"):
        request_id = self._candidate_request_id + 1
        self._candidate_request_id = request_id
        entry_snapshot = EntryRecord(
            index=entry.index,
            original_path=entry.original_path,
            tags=dict(entry.tags or {}),
            route_valid=entry.route_valid,
            resolved_path=entry.resolved_path,
            status=entry.status,
            origin=entry.origin,
            score=entry.score,
            bitrate=entry.bitrate,
            manual=entry.manual,
            candidates=list(entry.candidates or []),
        )
        self._trace(
            "candidate_refresh_requested",
            request_id=request_id,
            entry=entry_snapshot.index,
            reason=reason,
            playlist=os.path.basename(self._selected_playlist_path or ""),
        )
        self._ui(self._set_status, f"Calculando candidatos para la entrada {entry_snapshot.index + 1}…")
        self._ui(self._fill_candidates, [])
        worker = threading.Thread(
            target=self._candidate_worker,
            args=(request_id, entry_snapshot, reason),
            daemon=True,
        )
        worker.start()

    def _candidate_worker(self, request_id: int, entry_snapshot: EntryRecord, reason: str):
        started = time.perf_counter()
        try:
            candidates = self._compute_candidates_for_entry(entry_snapshot, request_id=request_id)
            error = None
        except Exception as exc:
            candidates = []
            error = exc
            self._trace(
                "candidate_refresh_worker_error",
                request_id=request_id,
                entry=entry_snapshot.index,
                reason=reason,
                error=exc,
            )

        def apply_results():
            if request_id != self._candidate_request_id:
                self._trace(
                    "candidate_refresh_stale",
                    request_id=request_id,
                    active_request_id=self._candidate_request_id,
                    entry=entry_snapshot.index,
                    reason=reason,
                )
                return
            if error is not None:
                self._fill_candidates([])
                self._set_status("Error calculando candidatos.")
                self._trace(
                    "candidate_refresh_failed",
                    request_id=request_id,
                    entry=entry_snapshot.index,
                    reason=reason,
                    elapsed=f"{time.perf_counter() - started:.2f}s",
                )
                return
            self._fill_candidates(candidates)
            self._set_status("Listo")
            self._trace(
                "candidate_refresh_applied",
                request_id=request_id,
                entry=entry_snapshot.index,
                reason=reason,
                candidates=len(candidates),
                elapsed=f"{time.perf_counter() - started:.2f}s",
            )

        self._ui(apply_results)

    def _fill_candidates(self, candidates: list[dict[str, Any]]):
        self._current_candidates = candidates
        self._current_candidate_index = None
        self.candidates_tree.delete(*self.candidates_tree.get_children())
        self._trace("candidates_refreshed", total=len(candidates))
        for i, cand in enumerate(candidates):
            self.candidates_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    f"{cand['score']:.2f}",
                    f"{cand['bitrate']} kbps" if cand["bitrate"] else "—",
                    self._format_mmss(int((cand["duration"] or 0) * 1000)),
                    cand["artist"],
                    cand["path"],
                ),
            )
        if candidates:
            self.candidates_tree.selection_set("0")
            self.candidates_tree.focus("0")
            self.candidates_tree.see("0")
            self._on_candidate_selected(None)
        else:
            self.player_track_text.set("Pista: —")
            self.player_time_text.set("00:00 / 00:00")

    def _on_playlist_selected(self, _event):
        selection = self.playlist_tree.selection()
        if not selection:
            return
        playlist_path = selection[0]
        if playlist_path in self._playlist_records:
            self._select_playlist_record(playlist_path)

    def _on_entry_selected(self, _event):
        started = time.perf_counter()
        entry = self._entry_from_selection()
        if not entry:
            return
        total = len(self._playlist_records.get(self._selected_playlist_path or "", PlaylistRecord("", [])).entries) or 0
        self._trace(
            "entry_select_event",
            entry=entry.index,
            total=total,
            analysis_running=self._analysis_running,
            playlist=os.path.basename(self._selected_playlist_path or ""),
            status=entry.status,
        )
        self.current_entry_text.set(f"Entrada activa: {entry.index + 1}/{total}")
        if self._analysis_running or self._preview_running:
            self._trace(
                "entry_select_deferred",
                entry=entry.index,
                reason="analysis_or_preview_running",
            )
            self._update_detail_from_selection(refresh_candidates=False)
        else:
            self._update_detail_from_selection(refresh_candidates=True)
        self._trace("entry_select_event_end", entry=entry.index, elapsed=f"{time.perf_counter() - started:.2f}s")

    def _on_candidate_selected(self, _event):
        selection = self.candidates_tree.selection()
        if not selection:
            return
        try:
            idx = int(selection[0])
            self._current_candidate_index = idx
            cand = self._current_candidates[idx]
        except Exception:
            return
        self._trace("candidate_selected", index=idx, path=cand["path"], score=cand["score"], bitrate=cand["bitrate"])
        self.player_track_text.set(f"Pista: {cand['path']}")
        self.detail_decision.config(text=f"Decisión: candidato seleccionado -> {os.path.basename(cand['path'])}")

    # -------------------------------------------------------------- Actions
    def select_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.folder_path.set(path)
            self._library_index = None
            self._library_lookup = {}
            self._refresh_route_labels()

    def select_playlists_multi(self):
        paths = filedialog.askopenfilenames(filetypes=M3U_FILTER)
        if not paths:
            return
        self.playlist_paths = list(paths)
        if len(self.playlist_paths) == 1:
            self.playlist_path.set(self.playlist_paths[0])
        else:
            first = os.path.basename(self.playlist_paths[0])
            self.playlist_path.set(f"{first} (+{len(self.playlist_paths) - 1} más)")
        self.batch_playlists_root.set("")
        self._refresh_route_labels()
        self._start_preview_load()

    def select_batch_folder(self):
        path = filedialog.askdirectory()
        if not path:
            return
        self.batch_playlists_root.set(path)
        self.playlist_paths = []
        self.playlist_path.set("")
        self._refresh_route_labels()
        self._start_preview_load()

    def select_dest_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.dest_folder.set(path)
            self._refresh_route_labels()
            self._refresh_all_output_paths()

    def _toggle_dest_controls(self):
        state = "normal" if self.dest_mode.get() == "custom" else "disabled"
        for widget in (self.dest_entry, self.dest_btn, self.keep_struct_chk):
            widget.config(state=state)

    def _route_estado(self, message: str):
        if not message:
            return
        long_text = ("\n" in message) or any(m in message for m in ("🎧", "🔎", "✅", "❌", "📊", "⏲", "⚠️"))
        if long_text:
            self._log(message)
        else:
            now = time.perf_counter()
            last_message, last_time = self._last_route_status
            if message != last_message or (now - last_time) >= 0.75:
                self._last_route_status = (message, now)
                self._set_status(message)

    def _refresh_route_labels(self):
        folder = self.folder_path.get().strip()
        playlist = self.playlist_path.get().strip()
        batch_root = self.batch_playlists_root.get().strip()
        dest = self.dest_folder.get().strip()
        self.library_path_text.set(f"Biblioteca: {folder if folder else '—'}")
        self.playlist_path_text.set(f"Playlist: {playlist if playlist else '—'}")
        self.batch_root_text.set(f"Lote: {batch_root if batch_root else '—'}")
        self.dest_path_text.set(f"Destino: {dest if dest else '—'}")

    def _start_preview_load(self):
        if self._analysis_running:
            self._trace("preview_skip", reason="analysis_running")
            return
        playlists = self._playlist_files_from_current_selection()
        self._preview_stop = False
        self._preview_running = True
        self._trace(
            "preview_start",
            playlists=len(playlists),
            playlist_mode=bool(self.playlist_paths),
            batch_mode=bool(self.batch_playlists_root.get().strip()),
        )
        threading.Thread(target=self._preview_worker, args=(playlists,), daemon=True).start()

    def _preview_worker(self, playlists: list[str]):
        started = time.perf_counter()
        if not playlists:
            self._preview_running = False
            self._trace("preview_end", playlists=0, elapsed=f"{time.perf_counter() - started:.2f}s", reason="no_playlists")
            return
        self._playlist_records.clear()
        self._playlist_order = []
        self._preview_bitrate_overrides = {}
        self._ui(self._update_playlist_tree)
        self._ui(self.progress.configure, value=0)
        self._ui(self.current_progress_text.set, "0%")
        total = len(playlists)
        for index, playlist in enumerate(playlists, start=1):
            if self._preview_stop:
                self._trace("preview_stop_requested", playlist=playlist, index=index)
                break
            item_started = time.perf_counter()
            self._ui(self._set_status, f"Previsualizando {index}/{total}: {os.path.basename(playlist)}")
            self._trace("preview_playlist_begin", playlist=playlist, index=index, total=total)
            record = PlaylistRecord(path=playlist, entries=[], state="previsualizando")
            if not os.path.isfile(playlist):
                record.state = "ruta inválida"
                record.error = "La playlist no existe"
                self._playlist_records[playlist] = record
                self._playlist_order.append(playlist)
                self._ui(self._sync_playlist_tree_item, playlist)
                self._trace("preview_playlist_missing", playlist=playlist)
                continue
            try:
                raw_entries, meta_map = _parse_m3u_with_meta(playlist)
                record.entries = [
                    EntryRecord(
                        index=i,
                        original_path=item.get("ruta") or "",
                        tags=item.get("tags") or {},
                        route_valid=os.path.exists(item.get("ruta") or ""),
                        bitrate=0,
                        status="ruta inválida" if not os.path.exists(item.get("ruta") or "") else "pendiente",
                    )
                    for i, item in enumerate(raw_entries)
                ]
                record.total = len(record.entries)
                record.invalid = sum(1 for e in record.entries if e.status == "ruta inválida")
                record.state = "previsualizada"
                record.output_path = self._output_path_for_playlist(playlist)
                self._preview_bitrate_overrides[playlist] = {
                    os.path.normpath(entry.original_path): int(entry.bitrate or 0)
                    for entry in record.entries
                    if entry.original_path
                }
                self._playlist_records[playlist] = record
                self._playlist_order.append(playlist)
                self._ui(self._sync_playlist_tree_item, playlist)
                if index == 1:
                    self._ui(self._select_playlist_record, playlist, False)
                self._trace(
                    "preview_playlist_done",
                    playlist=playlist,
                    entries=record.total,
                    invalid=record.invalid,
                    meta_entries=len(meta_map),
                    elapsed=f"{time.perf_counter() - item_started:.2f}s",
                )
            except Exception as exc:
                record.state = "error"
                record.error = str(exc)
                self._playlist_records[playlist] = record
                self._playlist_order.append(playlist)
                self._ui(self._sync_playlist_tree_item, playlist)
                self._trace("preview_playlist_error", playlist=playlist, error=exc)
            finally:
                self._ui(self.progress.configure, value=int(index / total * 100))
                self._ui(self.current_progress_text.set, f"{int(index / total * 100)}%")
        self._preview_running = False
        self._ui(self._set_status, "Previsualización cargada.")
        self._trace("preview_end", playlists=len(playlists), elapsed=f"{time.perf_counter() - started:.2f}s")

        threading.Thread(target=self._preview_bitrate_worker, args=(list(self._playlist_order),), daemon=True).start()

    def _preview_bitrate_worker(self, playlists: list[str]):
        if not playlists or self._analysis_running:
            return
        started = time.perf_counter()
        self._trace("preview_bitrate_begin", playlists=len(playlists))
        try:
            self._ui(self._set_status, "Calculando bitrates de previsualizacion en segundo plano...")
            self._load_library_index()
            library_lookup = dict(self._library_lookup)
        except Exception as exc:
            self._trace("preview_bitrate_index_unavailable", error=exc)
            return

        for playlist in playlists:
            if self._preview_stop or self._analysis_running:
                break
            record = self._playlist_records.get(playlist)
            if not record:
                continue
            overrides: dict[str, int] = {}
            for entry in record.entries:
                if not entry.original_path:
                    continue
                bitrate = self._bitrate_for_path(entry.original_path, lookup=library_lookup)
                entry.bitrate = bitrate
                overrides[os.path.normpath(entry.original_path)] = int(bitrate or 0)
            self._preview_bitrate_overrides[playlist] = overrides
            if playlist == self._selected_playlist_path:
                self._ui(self._populate_entries, record, False)
            self._ui(self._sync_playlist_tree_item, playlist)

        self._trace("preview_bitrate_end", playlists=len(playlists), elapsed=f"{time.perf_counter() - started:.2f}s")
        if not self._analysis_running:
            self._ui(self._set_status, "Bitrates de previsualizacion actualizados.")

    def run_update(self):
        if self._analysis_running:
            messagebox.showinfo("En curso", "Ya hay un análisis en curso.")
            return
        self._preview_stop = True
        self._analysis_running = True
        self._analysis_stop = False
        self._progress_refresh_state.clear()
        self._last_route_status = ("", 0.0)
        self._trace(
            "run_update",
            playlists=len(self._playlist_files_from_current_selection()),
            music_folder=self.folder_path.get().strip() or "—",
            dest_mode=self.dest_mode.get(),
            dest=self.dest_folder.get().strip() or "—",
            selective_low_bitrate=self.selective_low_bitrate.get(),
        )
        self._set_status("Preparando análisis…")
        threading.Thread(target=self._analysis_worker, daemon=True).start()

    def _analysis_worker(self):
        worker_started = time.perf_counter()
        playlists = self._playlist_files_from_current_selection()
        if not playlists:
            self._analysis_running = False
            self._ui(messagebox.showwarning, "Playlist inválida", "Selecciona una playlist o una carpeta de playlists válida.")
            return
        music_folder = self.folder_path.get().strip()
        if not music_folder or not os.path.isdir(music_folder):
            self._analysis_running = False
            self._ui(messagebox.showwarning, "Carpeta inválida", "Selecciona una carpeta de música válida.")
            return

        self._trace("worker_start", playlists=len(playlists), music_folder=music_folder)
        preview_bitrate_overrides = dict(self._preview_bitrate_overrides)
        self._playlist_records.clear()
        self._playlist_order = []
        self._ui(self._update_playlist_tree)
        self._ui(self.progress.configure, value=0)
        self._ui(self.current_progress_text.set, "0%")
        self._ui(self._set_status, f"Iniciando análisis… {len(playlists)} playlist(s)")
        self._log(f"Iniciando análisis de {len(playlists)} playlist(s).")

        total = len(playlists)

        for index, playlist in enumerate(playlists, start=1):
            if self._analysis_stop:
                self._trace("worker_stop_requested", playlist=playlist, index=index)
                break
            playlist_started = time.perf_counter()
            self._ui(self._set_status, f"Procesando {index}/{total}: {os.path.basename(playlist)}")
            self._ui(self.current_playlist_text.set, f"Playlist activa: {os.path.basename(playlist)}")
            self._ui(self.current_entry_text.set, "Entrada activa: —")
            self._ui(self.current_progress_text.set, f"{int(((index - 1) / total) * 100)}%")
            self._trace("playlist_begin", playlist=playlist, index=index, total=total)

            record = PlaylistRecord(path=playlist, entries=[], state="analizando")
            try:
                parse_started = time.perf_counter()
                raw_entries, meta_map = _parse_m3u_with_meta(playlist)
                self._trace("playlist_parsed", playlist=playlist, entries=len(raw_entries), meta_entries=len(meta_map), elapsed=f"{time.perf_counter() - parse_started:.2f}s")
                record.entries = [
                    EntryRecord(
                        index=i,
                        original_path=item.get("ruta") or "",
                        tags=item.get("tags") or {},
                        route_valid=os.path.exists(item.get("ruta") or ""),
                        status="ruta inválida" if not os.path.exists(item.get("ruta") or "") else "pendiente",
                    )
                    for i, item in enumerate(raw_entries)
                ]
                record.total = len(record.entries)
                record.invalid = sum(1 for e in record.entries if e.status == "ruta inválida")
                self._trace("playlist_records_ready", playlist=playlist, total=record.total, invalid=record.invalid)
                self._playlist_records[playlist] = record
                if playlist not in self._playlist_order:
                    self._playlist_order.append(playlist)
                self._ui(self._update_playlist_tree)

                update_started = time.perf_counter()
                self._trace("update_playlist_logic_begin", playlist=playlist)
                output_path = self._output_path_for_playlist(playlist)
                updated_path, missing_list = update_playlist_logic(
                    music_folder,
                    playlist,
                    progreso_callback=lambda pct, p=playlist: self._set_playlist_progress(p, pct),
                    estado_callback=self._route_estado,
                    threshold=self.threshold.get(),
                    solo_bitrate_bajo=self.selective_low_bitrate.get(),
                    bitrate_minimo=320,
                    bitrate_overrides=preview_bitrate_overrides.get(playlist, {}),
                    output_path=output_path,
                )
                self._trace(
                    "update_playlist_logic_end",
                    playlist=playlist,
                    missing=len(missing_list),
                    elapsed=f"{time.perf_counter() - update_started:.2f}s",
                )
                record.output_path = updated_path
                read_started = time.perf_counter()
                missing_counter = Counter(missing_list)
                result_paths = self._read_media_lines(updated_path)
                result_iter = iter(result_paths)
                self._trace(
                    "output_read",
                    playlist=playlist,
                    result_lines=len(result_paths),
                    elapsed=f"{time.perf_counter() - read_started:.2f}s",
                )

                map_started = time.perf_counter()
                for entry in record.entries:
                    if missing_counter.get(entry.original_path, 0) > 0:
                        missing_counter[entry.original_path] -= 1
                        entry.status = "no encontrada"
                        entry.resolved_path = ""
                        entry.origin = "auto"
                        entry.score = 0.0
                        entry.bitrate = 0
                    else:
                        entry.resolved_path = next(result_iter, "")
                        entry.status = "encontrada"
                        entry.origin = "auto"
                        if entry.resolved_path:
                            info = self._lookup_index_item(entry.resolved_path)
                            if info:
                                tags = info.get("tags") or {}
                                entry.bitrate = int(tags.get("bitrate") or 0)
                                entry.score = self._score_entry_against_path(entry, entry.resolved_path)
                    if entry.manual:
                        entry.status = "manual"
                self._trace(
                    "entry_map_end",
                    playlist=playlist,
                    elapsed=f"{time.perf_counter() - map_started:.2f}s",
                )

                self._refresh_record_counts(record)
                self._ui(self._update_playlist_tree)
                self._ui(self._select_playlist_record, playlist, False)
                self._trace(
                    "playlist_done",
                    playlist=playlist,
                    elapsed=f"{time.perf_counter() - playlist_started:.2f}s",
                    state=record.state,
                    found=record.found + record.manual,
                    missing=record.missing,
                )
            except Exception as exc:
                record.state = "error"
                record.error = str(exc)
                self._playlist_records[playlist] = record
                if playlist not in self._playlist_order:
                    self._playlist_order.append(playlist)
                self._ui(self._update_playlist_tree)
                self._trace("playlist_error", playlist=playlist, error=exc)
            finally:
                self._ui(self._update_playlist_tree)
                self._ui(self.progress.configure, value=int(index / total * 100))
                self._ui(self.current_progress_text.set, f"{int(index / total * 100)}%")

        self._analysis_running = False
        self._trace("worker_end", elapsed=f"{time.perf_counter() - worker_started:.2f}s")
        self._ui(self._set_status, "Análisis completado.")
        self._ui(self.current_playlist_text.set, "Playlist activa: —")
        self._ui(self.current_entry_text.set, "Entrada activa: —")

    def _set_playlist_progress(self, playlist_path: str, pct: int):
        record = self._playlist_records.get(playlist_path)
        if record:
            record.progress = int(pct)
            state = self._progress_refresh_state.get(playlist_path, {"pct": -1, "ts": 0.0})
            now = time.perf_counter()
            prev_pct = int(state.get("pct", -1))
            prev_ts = float(state.get("ts", 0.0))
            should_refresh = pct in (0, 100) or abs(int(pct) - prev_pct) >= 5 or (now - prev_ts) >= 0.5
            if should_refresh:
                self._progress_refresh_state[playlist_path] = {"pct": int(pct), "ts": now}
                self._ui(self._update_playlist_tree)
                self._trace("playlist_progress", playlist=os.path.basename(playlist_path), pct=int(pct))
        self._ui(self.current_progress_text.set, f"{int(pct)}%")

    def _lookup_index_item(self, path: str) -> Optional[dict]:
        if self._library_index is None:
            try:
                self._load_library_index()
            except Exception:
                return None
        item = self._library_lookup.get(os.path.normpath(path))
        if item:
            return item
        for candidate in self._library_index or []:
            if os.path.normpath(candidate.get("path") or "") == os.path.normpath(path):
                return candidate
        return None

    def _bitrate_for_path(self, path: str, lookup: Optional[dict[str, dict[str, Any]]] = None) -> int:
        item = None
        if lookup is not None:
            item = lookup.get(os.path.normpath(path))
        if item is None:
            item = self._lookup_index_item(path)
        if not item:
            return 0
        tags = item.get("tags") or {}
        try:
            return int(tags.get("bitrate") or 0)
        except Exception:
            return 0

    def _score_entry_against_path(self, entry: EntryRecord, path: str) -> float:
        item = self._lookup_index_item(path)
        if not item:
            return 0.0
        tags = item.get("tags") or {}
        if entry.tags and (entry.tags.get("title") or entry.tags.get("artist")):
            return round(float(calcular_puntaje(entry.tags, tags)), 2)
        return round(float(sim_nombre(os.path.basename(entry.original_path), os.path.basename(path))), 2)

    # ------------------------------------------------------- Selection flow
    def accept_selected_candidate(self):
        entry = self._entry_from_selection()
        cand = self._selected_candidate()
        if not entry or not cand:
            return
        self._trace("manual_accept_candidate", entry=entry.index, path=cand["path"], manual=True)
        self._apply_manual_choice(entry, cand["path"], manual=True)

    def manual_search_current_entry(self):
        entry = self._entry_from_selection()
        if not entry:
            messagebox.showinfo("Selecciona una entrada", "Elige primero una entrada en la tabla.")
            return
        initialdir = self.folder_path.get() if os.path.isdir(self.folder_path.get()) else None
        path = filedialog.askopenfilename(
            title=f"Localizar: {pretty_meta(entry.tags or {}) or os.path.basename(entry.original_path)}",
            filetypes=AUDIO_VIDEO_EXTS,
            initialdir=initialdir,
        )
        if not path:
            return
        self._trace("manual_search_path", entry=entry.index, path=path)
        score = rough_similarity(os.path.basename(entry.original_path), os.path.basename(path))
        if score < SIM_THRESHOLD:
            if not messagebox.askyesno(
                "Confirmar selección dudosa",
                f"La similitud es baja ({score:.1f}%).\n\n¿Quieres añadir igualmente este archivo?",
            ):
                return
        self._apply_manual_choice(entry, path, manual=True)

    def mark_current_entry_valid(self):
        entry = self._entry_from_selection()
        cand = self._selected_candidate()
        if not entry:
            return
        if cand:
            self._trace("mark_valid_candidate", entry=entry.index, path=cand["path"])
            self._apply_manual_choice(entry, cand["path"], manual=False)
        elif entry.resolved_path:
            self._trace("mark_valid_current", entry=entry.index, path=entry.resolved_path)
            self._apply_manual_choice(entry, entry.resolved_path, manual=False)

    def skip_current_entry(self):
        entry = self._entry_from_selection()
        if not entry:
            return
        self._trace("skip_entry", entry=entry.index, original=entry.original_path)
        entry.resolved_path = ""
        entry.status = "no encontrada"
        entry.manual = False
        entry.origin = "manual"
        entry.score = 0.0
        entry.bitrate = 0
        record = self._playlist_records.get(self._selected_playlist_path or "")
        if record:
            self._refresh_record_counts(record)
        self._rewrite_playlist_output()
        self._populate_entries(self._playlist_records[self._selected_playlist_path])
        self._update_playlist_tree()
        self._log(f"Entrada omitida: {entry.original_path}")

    def _selected_candidate(self) -> Optional[dict]:
        if self._current_candidate_index is None:
            return None
        if 0 <= self._current_candidate_index < len(self._current_candidates):
            return self._current_candidates[self._current_candidate_index]
        return None

    def _apply_manual_choice(self, entry: EntryRecord, chosen_path: str, manual: bool):
        if not chosen_path:
            return
        entry.resolved_path = chosen_path
        entry.manual = manual
        entry.status = "manual" if manual else "encontrada"
        entry.origin = "manual" if manual else "auto"
        entry.score = self._score_entry_against_path(entry, chosen_path)
        info = self._lookup_index_item(chosen_path)
        if info:
            tags = info.get("tags") or {}
            entry.bitrate = int(tags.get("bitrate") or 0)
        if manual:
            original_label = entry.original_path
            manual_cache.add_mapping(original_label, chosen_path)
        record = self._playlist_records.get(self._selected_playlist_path or "")
        if record:
            self._refresh_record_counts(record)
        self._rewrite_playlist_output()
        self._populate_entries(self._playlist_records[self._selected_playlist_path])
        self._update_playlist_tree()
        self._log(f"Selección aplicada: {chosen_path}")
        self._load_candidate_to_player(chosen_path, autoplay=False)

    def _output_meta_for_entry(self, entry: EntryRecord) -> dict[str, Any]:
        meta = dict(entry.tags or {})
        if entry.resolved_path:
            info = self._lookup_index_item(entry.resolved_path)
            if info:
                tags = info.get("tags") or {}
                for key in ("artist", "title", "duration", "remix"):
                    if tags.get(key) not in (None, ""):
                        meta[key] = tags.get(key)
        if entry.tags and entry.tags.get("lastplaytime") not in (None, ""):
            meta["lastplaytime"] = entry.tags.get("lastplaytime")
        return meta

    def _rewrite_playlist_output(self):
        if not self._selected_playlist_path:
            return
        record = self._playlist_records.get(self._selected_playlist_path)
        if not record:
            return
        record.output_path = self._output_path_for_playlist(record.path)
        try:
            with open(record.output_path, "w", encoding="utf-8", errors="ignore") as f:
                f.write("#EXTM3U\n")
                for entry in record.entries:
                    if entry.resolved_path:
                        meta = self._output_meta_for_entry(entry)
                        f.write(_format_extvdj_line(meta, entry.resolved_path) + "\n")
                        f.write(entry.resolved_path + "\n")
        except Exception as exc:
            self._log(f"No se pudo reescribir salida: {exc}")

    def refresh_current_candidates(self):
        entry = self._entry_from_selection()
        if not entry:
            return
        self._refresh_candidates_async(entry, reason="manual_refresh")

    def _on_dest_mode_changed(self):
        self._toggle_dest_controls()
        self._refresh_route_labels()
        self._refresh_all_output_paths()

    def _refresh_all_output_paths(self):
        for record in self._playlist_records.values():
            record.output_path = self._output_path_for_playlist(record.path)
        self._update_playlist_tree()

    # ------------------------------------------------------------- Player
    def _load_candidate_to_player(self, path: str, autoplay: bool = False):
        if not path:
            return
        if self.playback.load(path):
            self.player_track_text.set(f"Pista: {path}")
            if autoplay:
                self.playback.play()
        else:
            self.player_track_text.set(f"Pista: {path} (sin soporte de reproducción)")

    def play_selected_candidate(self):
        cand = self._selected_candidate()
        if cand:
            if self.playback.load(cand["path"]):
                self.playback.play()
                self.player_track_text.set(f"Pista: {cand['path']}")
        else:
            entry = self._entry_from_selection()
            if entry and entry.resolved_path:
                self._load_candidate_to_player(entry.resolved_path, autoplay=True)

    def pause_player(self):
        self.playback.pause()

    def stop_player(self):
        self.playback.stop()

    def toggle_pause(self):
        self.playback.pause()

    def _on_volume_change(self, value):
        try:
            self.playback.set_volume(int(float(value)))
        except Exception:
            pass

    def _selected_entry(self) -> Optional[EntryRecord]:
        return self._entry_from_selection()

    # -------------------------------------------------------- Alias dialog
    def open_alias_editor(self):
        os.makedirs(DATOS_DIR, exist_ok=True)
        if not os.path.exists(ALIAS_FILE):
            try:
                with open(ALIAS_FILE, "w", encoding="utf-8") as f:
                    json.dump(ALIAS_TEMPLATE, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                messagebox.showerror("Error", f"No se pudo crear {ALIAS_FILE}:\n\n{exc}")
                return
        try:
            with open(ALIAS_FILE, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:
            messagebox.showerror("Error", f"No se pudo abrir {ALIAS_FILE}:\n\n{exc}")
            return

        win = tk.Toplevel(self.master)
        win.title("Editor de alias")
        win.geometry("800x520")
        ttk.Label(win, text="Edita el JSON de alias. Se validará antes de guardar.").pack(anchor="w", padx=10, pady=6)
        txt = ScrolledText(win, wrap="none")
        txt.pack(fill="both", expand=True, padx=10, pady=6)
        txt.insert("1.0", content)
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=6)

        def pretty_format():
            try:
                data = json.loads(txt.get("1.0", "end").strip() or "{}")
                txt.delete("1.0", "end")
                txt.insert("1.0", json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as exc:
                messagebox.showerror("JSON inválido", f"Error al formatear:\n\n{exc}")

        def save_alias():
            raw = txt.get("1.0", "end").strip()
            try:
                data = json.loads(raw or "{}")
            except Exception as exc:
                messagebox.showerror("JSON inválido", f"No es un JSON válido:\n\n{exc}")
                return
            if not isinstance(data, dict):
                messagebox.showerror("JSON inválido", "La raíz del JSON debe ser un objeto {}.")
                return
            if "artist_alias" in data and not isinstance(data["artist_alias"], dict):
                messagebox.showerror("JSON inválido", '"artist_alias" debe ser un objeto.')
                return
            if "rules" in data and not isinstance(data["rules"], dict):
                messagebox.showerror("JSON inválido", '"rules" debe ser un objeto.')
                return
            try:
                with open(ALIAS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                messagebox.showinfo("Guardado", "Alias guardados correctamente.")
                self._set_status("Alias actualizados")
            except Exception as exc:
                messagebox.showerror("Error", f"No se pudo guardar:\n\n{exc}")

        ttk.Button(btns, text="Formatear JSON", command=pretty_format).pack(side="left")
        ttk.Button(btns, text="Guardar", command=save_alias).pack(side="left", padx=8)
        ttk.Button(btns, text="Cerrar", command=win.destroy).pack(side="right")

    def open_alias_suggestions_dialog(self):
        if not os.path.exists(ALIAS_SUG_FILE):
            messagebox.showinfo("Sin sugerencias", "Aún no hay sugerencias. Se generan al indexar.")
            return
        try:
            with open(ALIAS_SUG_FILE, "r", encoding="utf-8") as f:
                all_suggestions = json.load(f) or []
            if not isinstance(all_suggestions, list):
                raise ValueError("alias_suggestions.json inválido")
        except Exception as exc:
            messagebox.showerror("Error", f"No se pudieron cargar las sugerencias:\n\n{exc}")
            return

        win = tk.Toplevel(self.master)
        win.title("Sugerencias de alias")
        win.geometry("920x640")
        top = ttk.Frame(win, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Revisa y marca los alias que quieras aplicar al aliasconfig.json").pack(anchor="w")
        filter_frame = ttk.Frame(win, padding=(10, 0, 10, 6))
        filter_frame.pack(fill="x")
        ttk.Label(filter_frame, text="Confianza mínima:").pack(side="left")
        min_conf_var = tk.DoubleVar(value=0.90)
        ttk.Scale(filter_frame, from_=0.0, to=1.0, resolution=0.01, orient="horizontal", variable=min_conf_var, length=180).pack(side="left", padx=6)
        search_var = tk.StringVar(value="")
        ttk.Label(filter_frame, text="Buscar:").pack(side="left", padx=(10, 0))
        ttk.Entry(filter_frame, textvariable=search_var, width=28).pack(side="left", padx=6)

        canvas = tk.Canvas(win, borderwidth=0)
        frame_checks = ttk.Frame(canvas)
        vsb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.create_window((0, 0), window=frame_checks, anchor="nw")

        def on_frame_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        frame_checks.bind("<Configure>", on_frame_configure)

        accepted_pairs_norm = set()
        rows_vars = []

        def _norm(s: str) -> str:
            return _normalize_key(s)

        def rebuild_list():
            for child in list(frame_checks.children.values()):
                child.destroy()
            rows_vars.clear()
            used = _existing_alias_pairs_norm()
            thr = float(min_conf_var.get() or 0.0)
            q = (search_var.get() or "").strip().lower()
            for suggestion in all_suggestions or []:
                conf = float(suggestion.get("confidence", suggestion.get("score", 0.0)) or 0.0)
                if conf < thr:
                    continue
                var = suggestion.get("variant") or suggestion.get("variant_norm") or ""
                can = suggestion.get("canonical") or suggestion.get("canonical_norm") or ""
                var_n, can_n = _norm(var), _norm(can)
                if (var_n, can_n) in used or (var_n, can_n) in accepted_pairs_norm:
                    continue
                if q and q not in (var.lower() + " " + can.lower()):
                    continue
                row = ttk.Frame(frame_checks)
                row.pack(fill="x", padx=10, pady=3)
                var_sel = tk.BooleanVar(value=(conf >= 0.97))
                ttk.Checkbutton(row, variable=var_sel).pack(side="left")
                ttk.Label(row, text=f"{var}  →  {can}").pack(side="left", padx=6)
                ttk.Label(row, text=f"{conf:.2f}", foreground="#555").pack(side="right", padx=8)
                occ = int(suggestion.get("occurrences", 0))
                ttk.Label(row, text=f"{occ}×", foreground="#777").pack(side="right")
                rows_vars.append((var_sel, suggestion, row))
            on_frame_configure()

        bottom = ttk.Frame(win, padding=10)
        bottom.pack(fill="x", side="bottom")

        def select_all():
            for v, _, _ in rows_vars:
                v.set(True)

        def clear_all():
            for v, _, _ in rows_vars:
                v.set(False)

        ttk.Button(bottom, text="Seleccionar todo", command=select_all).pack(side="left")
        ttk.Button(bottom, text="Limpiar selección", command=clear_all).pack(side="left", padx=6)
        ttk.Button(bottom, text="Cerrar", command=win.destroy).pack(side="right", padx=8)

        def apply_selected():
            accepted = []
            for v, suggestion, _ in rows_vars:
                if v.get():
                    a = suggestion.get("variant") or suggestion.get("variant_norm") or ""
                    b = suggestion.get("canonical") or suggestion.get("canonical_norm") or ""
                    if a and b:
                        accepted.append((a, b))
            if not accepted:
                messagebox.showinfo("Nada que aplicar", "No hay alias seleccionados.")
                return
            try:
                alias_suggester.ArtistAliasSuggester.apply_selected(ALIAS_FILE, accepted)
                alias_suggester.prune_applied_suggestions(ALIAS_SUG_FILE, accepted)
                for a, b in accepted:
                    accepted_pairs_norm.add((_norm(a), _norm(b)))
                with open(ALIAS_SUG_FILE, "r", encoding="utf-8") as f:
                    nonlocal all_suggestions
                    all_suggestions = json.load(f) or []
                rebuild_list()
                self._set_status(f"Alias aplicados: {len(accepted)}")
                messagebox.showinfo("Listo", f"Se aplicaron {len(accepted)} alias.")
            except Exception as exc:
                messagebox.showerror("Error", f"No se pudieron aplicar alias:\n\n{exc}")

        ttk.Button(bottom, text="Aplicar seleccionados", command=apply_selected).pack(side="right")

        min_conf_var.trace_add("write", lambda *_: rebuild_list())
        search_var.trace_add("write", lambda *_: rebuild_list())
        rebuild_list()

    def clear_cache_db(self):
        if not os.path.isdir(DATOS_DIR):
            messagebox.showinfo("Nada que borrar", "La carpeta './datos' no existe.")
            return
        db_path = os.path.join(DATOS_DIR, "coincidencias.db")
        json_indexes = [os.path.join(DATOS_DIR, f) for f in os.listdir(DATOS_DIR)] if os.path.isdir(DATOS_DIR) else []
        json_indexes = [p for p in json_indexes if os.path.basename(p).startswith("mp3_index_") and p.endswith(".json")]
        manual_json = manual_cache.CACHE_FILE
        alias_sug_json = ALIAS_SUG_FILE

        if not any(os.path.exists(p) for p in [db_path, manual_json, alias_sug_json]) and not json_indexes:
            messagebox.showinfo("Nada que borrar", "No se encontraron archivos de caché/BD en './datos'.")
            return

        details = []
        if os.path.exists(db_path):
            details.append(f" - {db_path}")
        for j in json_indexes:
            details.append(f" - {j}")
        if os.path.exists(manual_json):
            details.append(f" - {manual_json} (caché manual)")
        if os.path.exists(alias_sug_json):
            details.append(f" - {alias_sug_json} (sugerencias de alias)")

        if not messagebox.askyesno("Confirmar", "Se eliminarán estos archivos:\n\n" + "\n".join(details) + "\n\n¿Continuar?"):
            return

        removed = 0
        errors = []
        for path in [db_path, manual_json, alias_sug_json, *json_indexes]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                    removed += 1
                except Exception as exc:
                    errors.append(f"No se pudo borrar {path}: {exc}")
        if errors:
            messagebox.showwarning("Completado con errores", f"Archivos eliminados: {removed}\n\nErrores:\n" + "\n".join(errors))
        else:
            messagebox.showinfo("Completado", f"Archivos eliminados: {removed}")
        self._set_status("Caché/BD limpiadas. Reindexa en el próximo run.")

    # --------------------------------------------------------- Config
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.folder_path.set(data.get("folder_path", ""))
                self.playlist_path.set(data.get("playlist_path", ""))
                self.threshold.set(data.get("threshold", 70))
                self.batch_playlists_root.set(data.get("batch_playlists_root", ""))
                self.include_subdirs.set(data.get("include_subdirs", True))
                self.dest_mode.set(data.get("dest_mode", "original"))
                self.dest_folder.set(data.get("dest_folder", ""))
                self.keep_structure.set(data.get("keep_structure", True))
                self.open_manual_during_batch.set(data.get("open_manual_during_batch", False))
                self._toggle_dest_controls()
                self._refresh_route_labels()
            except Exception:
                pass

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "folder_path": self.folder_path.get(),
                        "playlist_path": self.playlist_path.get(),
                        "threshold": self.threshold.get(),
                        "batch_playlists_root": self.batch_playlists_root.get(),
                        "include_subdirs": self.include_subdirs.get(),
                        "dest_mode": self.dest_mode.get(),
                        "dest_folder": self.dest_folder.get(),
                        "keep_structure": self.keep_structure.get(),
                        "open_manual_during_batch": self.open_manual_during_batch.get(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            self._refresh_route_labels()
        except Exception:
            pass

    def on_close(self):
        self.save_config()
        try:
            self.playback.stop()
        except Exception:
            pass
        self.master.destroy()

    # --------------------------------------------------------- helpers
    def save_current_playlist_output(self):
        if not self._selected_playlist_path:
            messagebox.showinfo("Nada que guardar", "Selecciona una playlist primero.")
            return
        record = self._playlist_records.get(self._selected_playlist_path)
        if not record:
            return
        self._rewrite_playlist_output()
        messagebox.showinfo("Guardado", f"Salida actualizada:\n\n{record.output_path}")

    def _on_volume_change(self, value):
        try:
            self.playback.set_volume(int(float(value)))
        except Exception:
            pass


if __name__ == "__main__":
    root = tk.Tk()
    app = PlaylistUpdaterApp(root)
    root.mainloop()
