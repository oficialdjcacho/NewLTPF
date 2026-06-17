# Especificación técnica de LTPF

Este documento describe el estado actual de la versión nueva de **Lost Track Playlist Finder** dentro de `NEW LTPF/LostTrackPlaylistFinder`.

Sirve como guía operativa para entender:

- cómo decide una coincidencia;
- qué datos guarda en local;
- qué hace cada archivo principal;
- qué parte “aprende” entre ejecuciones;
- y cuáles son los límites prácticos de recursos.

---

## 0) Cómo evalúa una pista perdida

LTPF no toma decisiones con un único score. La evaluación real sigue este orden:

1. **Caché manual**: si el usuario ya confirmó una ruta equivalente, esa decisión gana prioridad.
2. **Caché aprendida**: si existe una coincidencia previa válida para una entrada equivalente, se reutiliza.
3. **Tags reales**: se comparan `artist`, `title`, `duration` y `bitrate`.
4. **Nombre de archivo**: se usa como respaldo con similitud difusa.
5. **Refinamientos**: se aplican alias, filtros por artista, preferencia por no-remix y desempate por bitrate.

Reglas prácticas que mandan en la decisión:

- si varias opciones son válidas, gana la de mayor bitrate;
- si la ruta ya existe y el bitrate es alto, la app intenta no tocarla;
- si el modo selectivo está activo, solo se buscan alternativas para rutas rotas o pistas por debajo de `320 kbps`;
- la previsualización aporta el bitrate calculado antes de analizar;
- el matcher deja trazas para explicar cada salto de fase.

---

## 1) Qué es LTPF

LTPF es una aplicación de escritorio para **Windows** que repara playlists `M3U/M3U8` cuando los archivos fueron movidos, renombrados o cuando la ruta original ya no existe.

La idea central es:

- indexar una carpeta local de música;
- leer una playlist o un lote;
- previsualizar su contenido;
- encontrar la mejor coincidencia para cada entrada rota o degradada;
- escribir una nueva playlist actualizada;
- permitir corrección manual cuando el algoritmo no alcanza suficiente confianza.

No es un reproductor general ni un servicio en nube. Todo el trabajo fuerte ocurre sobre datos locales.

---

## 2) Modelo de ejecución

La app trabaja con un modelo mixto:

- **RAM** para búsqueda intensiva, índices en memoria y cachés temporales;
- **disco** para persistir índice, cachés y resultados.

El flujo general es:

1. `main.py` arranca Tkinter.
2. `gui.py` construye la UI, carga playlists y hace la previsualización.
3. `indexer.py` crea o carga el índice de la biblioteca.
4. `matcher.py` busca coincidencias con caches, tags y nombre.
5. `playlist_updater.py` parsea y escribe playlists actualizadas.
6. `gui.py` permite revisión manual, reproducción y guardado.

### Capacidad recomendada

No existe un límite duro de RAM en el código, pero el coste real depende del tamaño de la biblioteca y del número de workers.

| Recurso | Mínimo usable | Recomendado | Alto volumen |
|---|---:|---:|---:|
| RAM | 8 GB | 16 GB | 32 GB o más |
| CPU para indexado | 4 hilos | 8 hilos | 16+ hilos |
| CPU para matching | 4 procesos | 6-8 procesos | 8-12 procesos |

Lectura práctica:

- el indexado usa hilos y escala bien;
- el matching usa procesos y consume más memoria porque cada proceso necesita su contexto;
- para bibliotecas grandes, la RAM manda más que la CPU.

---

## 3) Artefactos locales

Todo lo importante vive en el repo o en `datos/`:

- `config.json`: preferencias locales de la UI;
- `datos/mp3_index_*.json`: índice de la biblioteca;
- `datos/coincidencias.db`: coincidencias aprendidas;
- `datos/manual_matches.json`: caché manual;
- `datos/aliasconfig.json`: alias aceptados;
- `datos/alias_suggestions.json`: sugerencias de alias.
- `datos/analisis_rendimiento_*.log`: resumen de rendimiento de cada analisis.

La carpeta `datos/` está pensada para quedar fuera de Git.

---

## 4) Formatos de datos

### 4.1 Índice de biblioteca

Cada entrada del índice usa esta forma general:

```json
[
  {
    "path": "C:/Music/Artist/Track.mp3",
    "tags": {
      "title": "Track",
      "artist": "Artist",
      "duration": 215.3,
      "bitrate": 320,
      "auto_tags": false
    }
  }
]
```

### 4.2 `manual_matches.json`

Guarda claves como:

- `exact|<texto exacto>`;
- `base|<basename>`;
- `norm|<basename_normalizado>`.

Cada clave apunta a la ruta elegida manualmente.

### 4.3 `aliasconfig.json`

Es un objeto JSON con dos claves:

- `artist_alias`: mapa `alias -> canonical`;
- `rules`: reglas auxiliares; hoy incluye `normalize_diminutives`.

### 4.4 `alias_suggestions.json`

Lista de sugerencias generadas por la indexación. Cada elemento incluye:

- `variant`;
- `variant_norm`;
- `canonical`;
- `canonical_norm`;
- `confidence`;
- `reasons`;
- `occurrences`;
- `examples`.

### 4.5 SQLite

Hay dos tablas relevantes:

- `indice_audio`: índice de la biblioteca
  - `path`, `title`, `artist`, `duration`, `bitrate`, `auto_tags`
- `cache_coincidencias`: coincidencias aprendidas
  - `entrada`, `ruta_resultado`, `bitrate`, `timestamp`

---

## 5) Qué hace la nueva UI

La UI actual ya no solo sirve para seleccionar carpetas. También:

- previsualiza playlists antes de analizarlas;
- muestra el árbol de playlists y el contenido completo;
- enseña el bitrate detectado en la previsualización;
- carga candidatos de forma asíncrona;
- deja elegir manualmente entre candidatos;
- permite buscar un archivo manualmente;
- reproduce el candidato activo si `python-vlc` está instalado;
- guarda salida en la ruta elegida por el usuario;
- puede limitar el análisis a rutas inválidas o pistas por debajo de `320 kbps`.

### Idea clave

La previsualización no reemplaza al matcher, pero sí evita trabajo innecesario:

- conserva rutas válidas que ya están a buena calidad;
- evita recalcular candidatos cuando el usuario solo quiere revisar;
- proporciona bitrate antes del análisis;
- sirve de base para caches futuras más agresivas.

---

## 6) Qué significa “aprende”

LTPF **no entrena un modelo de IA**. Aprende en el sentido práctico de que reutiliza resultados anteriores:

- caché manual de elecciones confirmadas;
- caché de coincidencias aprendidas;
- alias de artista guardados localmente;
- resultados ya resueltos en runs anteriores;
- bitrate detectado en previsualización para evitar búsquedas innecesarias.

---

## 7) Desglose por archivo

### `main.py`

Función detectada:

- `print_banner()`: imprime el banner de arranque.

Responsabilidad:

- crear el `Tk()`;
- instanciar `PlaylistUpdaterApp`;
- arrancar `mainloop()`.

No contiene lógica de negocio.

### `manual_cache.py`

Funciones:

- `_ensure_dir()`: crea `./datos` si no existe.
- `_strip_accents(s)`: elimina acentos.
- `_normalize_key(label)`: normaliza a una clave robusta con prefijo `norm|`.
- `_exact_key(label)`: genera clave exacta con prefijo `exact|`.
- `_basename_key(label)`: genera clave por basename con prefijo `base|`.
- `load_cache()`: carga `manual_matches.json`.
- `save_cache(data)`: persiste la caché manual en JSON.
- `add_mapping(original_label, chosen_path)`: guarda una correspondencia en las tres variantes de clave.
- `add_mappings(mapping)`: guarda varias correspondencias de una vez.
- `lookup(label)`: busca por exacto, basename y normalizado.

Qué hace:

- permite que una decisión humana se reutilice en futuras ejecuciones.

### `indexer.py`

Funciones:

- `extraer_tags(ruta)`: lee metadatos con `mutagen` y extrae `title`, `artist`, `duration`, `bitrate` y `auto_tags`.
- `generar_hash_carpeta(carpeta)`: calcula un hash del path de la carpeta para nombrar el índice.
- `inicializar_bd_memoria()`: crea una SQLite en memoria con la tabla `indice_audio`.
- `procesar_archivo(args)`: procesa un archivo concreto y devuelve `{"path", "tags"}`.
- `cargar_indice(carpeta, progreso_callback=None, estado_callback=None)`: carga un índice existente o indexa la carpeta desde cero.

Flujo de `cargar_indice`:

1. calcula `datos/mp3_index_<hash>.json`;
2. si existe, lo carga;
3. si no existe, recorre la carpeta;
4. filtra extensiones de audio/vídeo soportadas;
5. procesa archivos con `ThreadPoolExecutor`;
6. construye la lista de entradas y la tabla SQLite;
7. guarda el JSON;
8. vuelca la BD SQLite a `datos/coincidencias.db`;
9. genera sugerencias de alias.

### `alias_suggester.py`

Funciones auxiliares:

- `_strip_accents(s)`: normaliza a ASCII.
- `_normalize(s)`: limpia acentos, caracteres raros y espacios.
- `_artist_signature(artist_norm)`: crea una firma ordenada del artista sin tokens de ruido.
- `_tokens_jaccard(a, b)`: calcula similitud Jaccard por tokens.
- `_is_diminutive(a, b)`: detecta pares tipo diminutivo o variante común.

Clase `ArtistAliasSuggester`:

- `__init__()`: inicializa contadores y estructuras de coocurrencia.
- `add(artist, title, duration, path)`: ingesta una observación desde el índice.
- `_best_display_form(variant_norm)`: elige la forma más presentable del alias.
- `_pick_canonical(variants_counter)`: elige el canonical entre variantes equivalentes.
- `_confidence_and_reasons(a_norm, b_norm)`: calcula confianza y explica por qué dos variantes se relacionan.
- `build_suggestions()`: produce la lista final de sugerencias.
- `save_suggestions(path_json, suggestions=None)`: guarda sugerencias en JSON.
- `_load_aliasconfig(path_cfg)`: carga o crea la config de alias.
- `_backup(path_cfg)`: crea copia `.bakN` antes de escribir.
- `apply_selected(path_cfg, accepted_pairs)`: aplica alias aceptados por el usuario.

Función auxiliar:

- `prune_applied_suggestions(path_json, accepted_pairs)`: elimina de las sugerencias ya aplicadas los pares aceptados.

Qué hace:

- detecta alias plausibles entre artistas y facilita normalización futura.

### `playlist_updater.py`

Funciones auxiliares:

- `_es_ruta_de_medio(linea)`: detecta líneas que parecen rutas.
- `_strip_accents(s)`: elimina acentos.
- `_norm_min(s)`: normalización ligera.
- `_looks_like_channel(artist_norm)`: heurística de canal o contenido especial.
- `_split_artist_from_title_if_needed(artist, title)`: corrige casos donde artista y título vienen mezclados.
- `_parse_extvdj_line(line)`: parsea bloques `#EXTVDJ`.
- `_parse_m3u_with_meta(ruta_m3u)`: lee la playlist y recupera metadatos.
- `_format_extvdj_line(meta=None, path=None)`: serializa un bloque `#EXTVDJ`.
- `_read_tags_from_file(path)`: extrae tags del archivo real.
- `_score_strings(a, b)`: similitud textual.
- `_estimate_similarity(missing_meta, chosen_path)`: aproxima similitud con la pista elegida.

Clase interna:

- `_ManualResolver`: ventana para resolver manualmente entradas faltantes.

Función principal:

- `update_playlist_logic(carpeta_musica, archivo_playlist, ..., solo_bitrate_bajo=False, bitrate_minimo=320, bitrate_overrides=None, output_path=None)`

Qué hace `update_playlist_logic`:

- parsea la playlist;
- llama al matcher;
- mantiene `#EXTM3U`;
- escribe `#EXTVDJ`;
- conserva `lastplaytime`;
- añade `filesize`, `artist`, `title`, `remix` y `songlength` cuando corresponde;
- escribe en `output_path` si se le pasa uno;
- si no, genera la playlist actualizada con el sufijo estándar.

### `matcher.py`

Funciones principales:

- `_cargar_alias_desde_json()`: carga alias desde disco.
- `_strip_accents(s)`, `_normalize(s)`: normalización básica.
- `_artista_con_fallback(tags_dict)`: obtiene artista con fallback.
- `_es_remix(titulo)`: detecta remix.
- `normalizar_artista(s)`: normaliza nombres de artista.
- `sim_nombre(a, b)`: similitud por nombre.
- `sim_titulo_suave(a, b)`: similitud suave por título.
- `partir_artista_titulo_desde_nombre(nombre)`: separa artista y título si vienen juntos.
- `_titulo_puro(tags_dict)`: obtiene título depurado.
- `canon_title(s)`: título canónico.
- `titulos_equivalentes(a, b)`: equivalencia de títulos.
- `_artista_en_filename(path, artist_norm)`: comprueba si el artista aparece en el nombre.
- `_filename_es_compuesto_ajeno(path, expected_title)`: detecta falsos positivos por nombre.
- `_es_netsearch_id(ruta)`: detecta IDs o entradas tipo netsearch.
- `artistas_compatibles(expected_norm, candidate_norm)`: compatibilidad entre artistas.
- `_labels_para_cache_manual(entrada)`: claves para caché manual.
- `cargar_conexion_en_memoria()`: copia `coincidencias.db` a memoria.
- `cargar_indice_desde_sqlite(mem_conn)`: carga el índice desde SQLite.
- `_ensure_cache_tables(conn)`: crea tablas si faltan.
- `buscar_en_cache(conn, clave)`: busca coincidencia exacta.
- `_duration_bucket(d)`: bucket de duración.
- `_basename_norm(ruta)`: basename normalizado.
- `_claves_aprendizaje_para_entrada(entrada)`: claves equivalentes para aprendizaje.
- `_buscar_en_cache_aprendida(conn, entrada)`: busca coincidencia aprendida.
- `validar_cache_contra_entrada(indice_sqlite, ruta_cache, entrada, umbral=80)`: valida una caché contra la entrada.
- `bitrate_por_path(indice_sqlite, path)`: recupera bitrate de una ruta.
- `_bitrate_por_path_lookup(path_lookup, indice_sqlite, path)`: recupera bitrate usando primero un diccionario `path -> item`.
- `_build_quality_caches(indice_sqlite)`: crea caches en memoria por identidad musical y conserva la mejor calidad.
- `_best_quality_from_cache(quality_cache, base_tags, prefer_no_auto=True, tolerancia=3)`: busca la mejor pista compatible en la cache de calidad.
- `calcular_puntaje(tags1, tags2)`: score por tags.
- `tags_similares(tags1, tags2, tolerancia_duracion=3)`: chequeo de similitud entre tags.
- `_preferir_no_remix(candidatos)`: prioriza no-remix.
- `_filtrar_por_artista_esperado(candidatos, artist_esperado_norm)`: filtra por artista esperado.
- `_upgrade_por_alias(...)`: mejora usando alias detectados.
- `procesar_bloque_con_orden(...)`: resuelve un bloque de entradas y mantiene trazas.
- `_emit_progreso(...)`: emite progreso.
- `_drain_logs_ordenados(...)`: vacía logs en orden.
- `_format_perf_summary(stats, wall_elapsed, total_entries)`: genera el resumen de rendimiento.
- `_save_perf_summary(summary_text)`: guarda el resumen en `datos/analisis_rendimiento_*.log`.
- `fmt_bitrate(b)`: formatea bitrate.
- `generar_clave_entrada(entrada)`: genera clave estable.
- `_mejor_por_similares(indice_sqlite, base_tags, prefer_no_auto=True)`: elige el mejor candidato por similitud.
- `_acc_update(updates_list, clave, ruta, bitrate)`: acumula actualizaciones.
- `_aplicar_updates_en_disco(updates)`: persiste cache en SQLite.
- `buscar_mejor_coincidencia(...)`: entry-point de búsqueda para una entrada.

Qué hace el matcher en la práctica:

- aplica caché manual;
- aplica caché aprendida;
- evalúa tags;
- evalúa nombre;
- descarta falsos positivos;
- prefiere mayor bitrate;
- registra por qué gana una pista y por qué pierde otra.

### `gui.py`

La nueva UI concentra casi toda la interacción de usuario. Sus métodos principales son:

#### Reproductor

- `PlayerBackend.__init__(...)`
- `load(path)`
- `play()`
- `pause()`
- `stop()`
- `set_volume(value)`
- `get_time_ms()`
- `get_duration_ms()`
- `is_playing()`

#### Construcción de interfaz

- `PlaylistUpdaterApp.__init__(master)`
- `_build_ui()`
- `_build_left_panel()`
- `_build_center_panel()`
- `_build_right_panel()`

#### Estado, trazas y refresco

- `_log(message)`
- `_append_log(message)`
- `_trace(step, **fields)`
- `_ui(callback, *args, **kwargs)`
- `_set_status(text)`
- `_set_player_state(text)`
- `_format_mmss(ms)`
- `_poll_player_state()`

#### Índice y previsualización

- `_load_library_index()`
- `_playlist_files_from_current_selection()`
- `_list_playlists_in_folder(root_dir, recursive)`
- `_output_path_for_playlist(playlist_path)`
- `_read_media_lines(path)`
- `_write_output_playlist(record)`
- `_refresh_record_counts(record)`
- `_candidate_info(item)`
- `_compute_candidates_for_entry(entry, limit=12, request_id=None)`
- `_select_playlist_record(playlist_path, refresh_detail=None)`
- `_populate_entries(record, refresh_detail=True)`
- `_sync_playlist_tree_item(playlist_path)`
- `_update_playlist_tree()`
- `_select_first_entry_if_any(refresh_detail=True)`
- `_entry_from_selection()`
- `_update_detail_from_selection(refresh_candidates=True)`

#### Candidatos asíncronos

- `_refresh_candidates_async(entry, reason="manual")`
- `_candidate_worker(request_id, entry_snapshot, reason)`
- `_fill_candidates(candidates)`
- `_on_playlist_selected(_event)`
- `_on_entry_selected(_event)`
- `_on_candidate_selected(_event)`

#### Selección y carga de rutas

- `select_folder()`
- `select_playlists_multi()`
- `select_batch_folder()`
- `select_dest_folder()`
- `_toggle_dest_controls()`
- `_route_estado(message)`
- `_refresh_route_labels()`
- `_start_preview_load()`
- `_preview_worker(playlists)`

#### Análisis y escritura

- `run_update()`
- `_analysis_worker()`
- `_set_playlist_progress(playlist_path, pct)`
- `_lookup_index_item(path)`
- `_bitrate_for_path(path, lookup=None)`
- `_score_entry_against_path(entry, path)`
- `_rewrite_playlist_output()`
- `_refresh_all_output_paths()`

#### Resolución manual

- `accept_selected_candidate()`
- `manual_search_current_entry()`
- `mark_current_entry_valid()`
- `skip_current_entry()`
- `_selected_candidate()`
- `_apply_manual_choice(entry, chosen_path, manual)`
- `refresh_current_candidates()`

#### Reproductor integrado

- `_load_candidate_to_player(path, autoplay=False)`
- `play_selected_candidate()`
- `pause_player()`
- `stop_player()`
- `toggle_pause()`
- `_on_volume_change(value)`

#### Alias y limpieza

- `_selected_entry()`
- `open_alias_editor()`
- `open_alias_suggestions_dialog()`
- `clear_cache_db()`

#### Configuración

- `load_config()`
- `save_config()`
- `on_close()`
- `save_current_playlist_output()`

Qué hace `gui.py` en la práctica:

- carga playlists y lotes sin bloquear la interfaz tanto como antes;
- enseña el bitrate en la previsualización;
- permite revisar candidatos y reproducirlos;
- respeta la ruta de salida elegida;
- marca entradas manuales y actualiza contadores de estado;
- usa trazas para diagnosticar cuellos de botella.

---

## 8) Qué decide exactamente el matcher

### A. Caché manual

Si el usuario ya confirmó una decisión equivalente, esa ruta gana antes de volver a calcular nada.

### B. Caché aprendida

Si la app ya resolvió algo equivalente en una ejecución anterior, se reutiliza si la similitud sigue siendo aceptable.

### C. Cache de calidad

Antes del escaneo completo por tags, el matcher puede usar una cache de calidad en memoria.

Esta cache se construye por proceso a partir del indice cargado y guarda la mejor pista por identidad musical:

- artista normalizado;
- titulo canonico;
- bucket de duracion;
- indicador remix/no-remix.

Si una entrada trae tags claros, la fase `cache_calidad` puede resolverla directamente con la pista de mayor bitrate del grupo. Esto evita comparar la entrada contra toda la biblioteca.

La cache de calidad no analiza audio real. Usa metadatos y duracion, y solo descarta menor bitrate dentro de una identidad suficientemente compatible.

### D. Tags reales

Se comparan `artist`, `title`, `duration` y `bitrate`. Aquí el nombre del archivo no es la fuente principal.

### E. Nombre de archivo

Se usa como respaldo cuando los tags no bastan o la pista llega con nombres poco fiables.

### F. Refinamientos

Se aplican alias, preferencia por no-remix y filtros por artista esperado.

---

## 9) Qué significa “mejor opción”

La mejor opción no es simplemente la más parecida en texto. Es la que:

- pasa el filtro de similitud;
- encaja mejor con artista/título/duración;
- no es un falso positivo obvio;
- y, entre candidatas válidas, ofrece mayor bitrate.

---

## 10) Resolución manual: criterio real

Cuando el usuario elige una pista manualmente:

- esa ruta se guarda en caché manual;
- la entrada se marca como resuelta;
- la playlist se actualiza;
- el contador de manuales cambia;
- y la decisión puede reutilizarse en futuros runs.

---

## 11) Alias: criterio real

Los alias sirven para normalizar casos como variantes de artista, diminutivos o nombres que cambian ligeramente.

No son una sustitución de tags reales. Son una capa de ayuda para mejorar el matcher.

---

## 12) Limitaciones conocidas

- La previsualización mejora mucho la UX, pero no convierte todavía toda la entrada en una caché de candidatos completa.
- El matching sigue siendo costoso en bibliotecas muy grandes.
- Si la biblioteca cambia, los índices y caches locales deben regenerarse.
- El reproductor depende de que `python-vlc` y VLC estén disponibles.

### Logs de rendimiento

Al terminar el analisis, el matcher genera un resumen con tiempos agregados.

Campos importantes:

- `Tiempo total pared`: tiempo real transcurrido desde que empieza el matching hasta que termina.
- `Media por entrada`: coste medio por entrada procesada.
- `p50`: mediana; ayuda a ver el comportamiento normal.
- `p95`: coste de las entradas lentas sin fijarse solo en el maximo.
- `Rendimiento`: entradas procesadas por segundo.
- `Evitaron escaneo completo`: entradas resueltas por ruta valida o caches.
- `Usaron escaneo completo`: entradas que tuvieron que recorrer la biblioteca por tags o nombre.

Fases utiles para comparar mejoras:

- `cache_calidad`: entradas resueltas por la nueva cache de mayor bitrate.
- `tags_scan`: entradas que recorrieron la biblioteca comparando tags.
- `nombre_scan`: entradas que recurrieron al nombre de archivo.
- `ruta_valida`: entradas conservadas sin buscar alternativa.
- `cache_manual` y `cache_global_*`: entradas resueltas por decisiones previas.

Cada resumen queda guardado en:

```text
datos/analisis_rendimiento_YYYYMMDD_HHMMSS.log
```

El indexador genera un resumen independiente:

```text
datos/indexado_rendimiento_YYYYMMDD_HHMMSS.log
```

Ese fichero mide:

- si el indice se cargo desde JSON o se genero desde cero;
- tiempo de carga de JSON existente;
- tiempo de escaneo de carpetas;
- tiempo de extraccion de metadata con `mutagen`;
- workers usados;
- archivos encontrados;
- entradas con tags validos, sin tags, con bitrate y con duracion;
- errores de metadata;
- tiempo de preparacion de SQLite en memoria;
- tiempo de escritura del JSON;
- tiempo de escritura de SQLite;
- tiempo de generacion de sugerencias de alias;
- tamano final de JSON y SQLite;
- archivos por segundo.

La GUI tambien guarda una traza completa por sesion:

```text
datos/app_trace_YYYYMMDD_HHMMSS.log
```

Esa traza conserva los mensajes que se ven en la pestana `Logs`: previsualizacion, carga de biblioteca, indexado, analisis, lectura de salida, seleccion manual y errores.

Para comprobar si una optimizacion ayuda, compara dos logs y mira especialmente:

- si sube el porcentaje de `Evitaron escaneo completo`;
- si baja `Media por entrada`;
- si baja `p95`;
- si baja el total de `tags_scan` y `nombre_scan`;
- si aumenta `cache_calidad` sin aumentar errores manuales.

Para comparar mejoras de primer indexado, compara `indexado_rendimiento_*.log` y mira especialmente:

- `extraer metadata`;
- `guardar JSON`;
- `guardar SQLite`;
- `generar alias`;
- `metadata archivos/s`;
- `total archivos/s`.

---

## 13) Regla de uso recomendada

Para la operación normal:

1. cargar biblioteca;
2. previsualizar playlists;
3. revisar bitrate y rutas válidas;
4. activar análisis selectivo si la colección ya está saneada;
5. revisar manualmente solo las incidencias;
6. guardar en la ruta deseada.

---

## 14) Resumen ejecutivo

LTPF ya funciona como una herramienta local de reparación de playlists con:

- previsualización;
- cachés locales;
- matcher por fases;
- revisión manual;
- reproductor integrado;
- y escritura enriquecida de playlists.

El próximo salto de rendimiento no vendrá de cambiar la lógica de decisión, sino de convertir la previsualización en una caché más profunda del matcher.
