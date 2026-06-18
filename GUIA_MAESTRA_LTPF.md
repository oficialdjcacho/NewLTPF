# GUIA MAESTRA DE LTPF

Este archivo reúne, en un solo lugar, lo que se ha investigado, entendido y modificado sobre **Lost Track Playlist Finder** en esta rama de trabajo.

Su objetivo es servir como guía práctica futura sin necesidad de volver a inspeccionar el código salvo para cambios puntuales.

---

## 1) Qué es LTPF

**Lost Track Playlist Finder** es una aplicación de escritorio para Windows que:

- analiza playlists `M3U` / `M3U8`;
- detecta rutas rotas o poco fiables;
- busca la mejor coincidencia en una biblioteca local;
- conserva pistas válidas cuando ya cumplen calidad suficiente;
- permite corrección manual de coincidencias;
- escribe playlists nuevas con metadatos enriquecidos;
- mantiene caches locales para acelerar ejecuciones posteriores.

La aplicación trabaja **en local**. No depende de nube ni de servicios externos para resolver rutas.

---

## 2) Qué se ha entendido del proyecto

Durante el análisis del repositorio quedó claro que LTPF no es solo un reparador de rutas. Su propósito real es:

- reconstruir playlists dañadas con la mejor pista disponible;
- priorizar calidad cuando hay varias coincidencias correctas;
- permitir revisión humana cuando el algoritmo no llega;
- “aprender” en el sentido práctico de reutilizar decisiones previas.

### Qué significa “aprender”

LTPF **no entrena IA**. Aprende porque reutiliza:

- caché manual de decisiones confirmadas;
- caché de coincidencias aprendidas;
- alias de artista aceptados;
- resultados previos persistidos;
- bitrate detectado en previsualización.

---

## 3) Cómo evalúa una canción perdida

La evaluación no usa una única puntuación. Sigue una jerarquía real de fases:

1. **Caché manual**
   - Si el usuario ya resolvió una entrada equivalente, esa ruta tiene prioridad.

2. **Caché aprendida**
   - Si ya existe una coincidencia válida previa para una entrada equivalente, se reutiliza.

3. **Tags reales**
   - Se comparan `artist`, `title`, `duration` y `bitrate`.

4. **Nombre de archivo**
   - Se usa como respaldo con similitud difusa.

5. **Refinamientos**
   - Se aplican alias, filtros por artista, preferencia por no-remix y desempate por bitrate.

### Reglas que dominan la decisión

- si varias opciones son válidas, gana la de mayor bitrate;
- si la ruta ya existe y el bitrate es alto, la app intenta no tocarla;
- si el modo selectivo está activo, solo se buscan alternativas para rutas inválidas o pistas por debajo de `320 kbps`;
- la previsualización aporta bitrate antes del análisis;
- las trazas explican por qué se elige una pista frente a otra.

---

## 4) Qué hace la nueva UI

La UI nueva dejó de ser solo un selector de carpetas. Ahora funciona como una consola visual de análisis.

### Panel superior

Debe mostrar:

- botones principales de la UI;
- rutas elegidas;
- estado global;
- carpeta de música;
- carpeta o lote de playlists;
- destino de salida;
- si se mantiene estructura o no;
- estado del reproductor.

### Panel izquierdo

Muestra el contexto de la playlist:

- árbol de playlists en análisis o ya analizadas;
- ruta del archivo `M3U/M3U8`;
- estado de la ruta;
- número total de entradas;
- entrada actual / índice actual;
- porcentaje de análisis.

### Panel central

Muestra el contenido completo de la playlist seleccionada:

- posición;
- nombre mostrado;
- ruta original;
- estado;
- validación de ruta;
- resultado elegido;
- bitrate en `kbps`.

### Panel derecho

Muestra el detalle de la coincidencia activa:

- metadatos detectados;
- pista original;
- decisión del matcher;
- candidatos disponibles;
- score;
- bitrate;
- duración;
- artista;
- botones de acción manual;
- reproductor integrado.

### Flujo ideal de trabajo

1. Cargar carpeta o lote.
2. Ver la previsualización.
3. Confirmar rutas de salida.
4. Revisar playlist y entradas.
5. Escuchar candidatos si hace falta.
6. Elegir uno de la lista o buscar manualmente.
7. Guardar la salida.

---

## 5) Modificaciones realizadas en esta rama

### 5.1 Previsualización real de playlists

La app ahora puede cargar playlists o lotes en modo previsualización sin lanzar el análisis completo.

La previsualización:

- llena el árbol de playlists;
- llena la tabla central;
- enseña el detalle de la entrada;
- muestra bitrate si puede calcularlo;
- guarda overrides de bitrate para el análisis posterior.

### 5.2 Búsqueda asíncrona de candidatos

La selección de una entrada ya no dispara la búsqueda pesada en el hilo principal.

Se añadió:

- worker en segundo plano para candidatos;
- `request_id` para descartar resultados obsoletos;
- actualización segura de la UI sin bloquearla tanto como antes.

### 5.3 Filtro selectivo de análisis

Se añadió el modo:

- analizar solo rutas inválidas;
- analizar solo pistas por debajo de `320 kbps`.

Esto usa el bitrate detectado en previsualización como referencia.

### 5.4 Escritura enriquecida de playlists

La salida vuelve a incluir:

- `#EXTM3U`;
- bloques `#EXTVDJ`;
- `filesize`;
- `lastplaytime`;
- `artist`;
- `title`;
- `remix`;
- `songlength`.

### 5.5 Corrección del destino de salida

Se corrigió un fallo por el que la app seguía guardando en la carpeta original aunque el usuario hubiese elegido otra ruta.

Ahora:

- la UI calcula el destino de salida;
- lo propaga al backend;
- el escritor final usa ese `output_path`.

### 5.6 Reproductor integrado

Se añadió reproducción integrada del candidato seleccionado para poder comparar antes de aceptar.

Depende de:

- `python-vlc`;
- VLC instalado en el sistema.

### 5.7 Logs y trazas

Se añadieron trazas para entender cuellos de botella:

- carga de índice;
- selección de playlist;
- selección de entrada;
- búsqueda de candidatos;
- aplicación manual;
- escritura de salida;
- cambios de estado.

### 5.8 Cache de calidad por identidad musical

Se anadio una cache interna en `matcher.py` para acelerar bibliotecas grandes.

La cache agrupa pistas por una identidad musical prudente:

- artista normalizado;
- titulo canonico;
- bucket de duracion;
- indicador remix/no-remix.

Para cada grupo conserva la pista de mayor bitrate. El objetivo es que, si una entrada trae tags suficientemente buenos, el matcher pueda elegir directamente la mejor version disponible sin escanear toda la biblioteca.

Esta cache no compara audio real. Tampoco elimina candidatos solo por tener un nombre parecido. Solo se usa cuando la identidad musical encaja con titulo, artista y duracion aproximada.

Ademas se anadio un `path_lookup` interno para consultas rapidas de `path -> item`, evitando recorridos completos para obtener bitrate o validar existencia dentro del indice.

### 5.9 Índice auxiliar de candidatos

Para reducir el coste de `tags_scan` y `nombre_scan`, el matcher usa un índice auxiliar por tokens de título, artista y nombre de archivo.

La construcción es perezosa y limitada al bloque de cada worker:

- no se crea en bloques que se resuelven por ruta o caches;
- se construye solo cuando una entrada necesita scan;
- solo guarda referencias para tokens presentes en ese bloque;
- si no encuentra candidatos por tokens, vuelve al escaneo completo anterior.

Esto mantiene la precisión y evita pagar el coste fijo de preparar un índice auxiliar completo para todos los workers.

### 5.10 Resumen de rendimiento por analisis

Se anadio instrumentacion agregada para comparar mejoras de rendimiento.

Al terminar el matching, la app genera un resumen con:

- entradas procesadas;
- entradas encontradas y no encontradas;
- tamano de la biblioteca indexada;
- tiempo total de pared;
- media por entrada;
- percentiles p50 y p95;
- tiempo maximo por entrada;
- entradas por segundo;
- cuantas entradas evitaron escaneo completo;
- cuantas usaron escaneo completo;
- desglose por fase.
- entradas mas lentas con fase, tiempo, candidatos evaluados y resultado elegido.

Fases actuales registradas:

- `ruta_valida`;
- `cache_manual`;
- `local-exacta` / `local-aprendida`;
- `cache_global_exacta` / `cache_global_aprendida`;
- `cache_calidad`;
- `tags_scan`;
- `nombre_scan`;
- `nombre_scan_sin_coincidencia`;
- `nombre_scan_filtrado`;
- `netsearch_sin_nombre`;
- `sin_coincidencia`.

Cada resumen se guarda en:

```text
datos/analisis_rendimiento_YYYYMMDD_HHMMSS.log
```

Este archivo sirve para comparar ejecuciones antes y despues de cambios en el matcher.

El primer indexado y las cargas posteriores del indice generan un resumen separado:

```text
datos/indexado_rendimiento_YYYYMMDD_HHMMSS.log
```

Ese log registra modo (`indexado_completo` o `cache_json`), workers, archivos encontrados, entradas con tags, errores de metadata, tiempos de escaneo, extraccion con `mutagen`, escritura JSON, escritura SQLite, generacion de alias, tamano de indices y archivos por segundo.

Ademas, la GUI crea una traza persistente por sesion:

```text
datos/app_trace_YYYYMMDD_HHMMSS.log
```

Esa traza conserva lo que aparece en la pestana `Logs`, incluyendo previsualizacion, carga de biblioteca, indexado y analisis por playlist.

---

## 6) Estado de la UI y problemas corregidos o atacados

Durante la evolución de la UI se detectaron estos problemas:

- la UI se congelaba al analizar;
- la UI se quedaba en “no responde” al seleccionar entradas;
- la carga de lotes grandes era lenta;
- el bitrate se mostraba mal o como `auto/%` en vez de `kbps`;
- el análisis seguía procesando entradas válidas de calidad alta;
- el destino de salida no se respetaba;
- el usuario no tenía un flujo claro para aceptar candidatos.

### Qué se corrigió

- búsquedas de candidatos fuera del hilo principal;
- previsualización con bitrate;
- análisis selectivo por ruta/bitrate;
- persistencia del destino de salida;
- mejor actualización del estado de la playlist;
- selección manual más clara;
- botones de reproducción y acción manual.

### Qué sigue siendo costoso

- previsualizar lotes muy grandes;
- cambiar rápido entre playlists grandes;
- recalcular candidatos si la biblioteca es enorme;
- refrescar información detallada de muchas entradas seguidas.

---

## 7) Cómo trabaja la aplicación en memoria y CPU

### RAM

La app usa RAM para:

- índice de biblioteca;
- cache manual;
- cache aprendida;
- candidatos temporales;
- previsualización;
- datos de UI.

No existe un “máximo” duro en el código. El consumo depende del tamaño de la biblioteca y del número de workers.

### CPU

La CPU se usa para:

- indexado;
- extracción de tags;
- comparación de candidatos;
- similitud textual;
- escritura de playlists;
- tareas de UI asíncronas.

### Recomendación práctica

| Recurso | Mínimo usable | Recomendado | Alto volumen |
|---|---:|---:|---:|
| RAM | 8 GB | 16 GB | 32 GB o más |
| CPU para indexado | 4 hilos | 8 hilos | 16+ hilos |
| CPU para matching | 4 procesos | 6-8 procesos | 8-12 procesos |

---

## 8) Cómo la previsualización ayuda al análisis

La previsualización ya no es solo visual. También sirve como base de optimización.

### Lo que ya optimiza

- evita buscar alternativas en rutas válidas y con bitrate suficiente;
- conserva pistas que ya cumplen;
- aporta bitrate antes del análisis;
- reduce trabajo repetido en pistas que no necesitan reparación.

### Lo que todavía no hace completamente

- no es todavía una caché total del matcher;
- no guarda todos los candidatos pesados de forma persistente;
- no evita por completo recalcular la misma entrada en nuevas sesiones.

### Plan de ataque recomendado

1. medir tiempos por entrada;
2. cachear la previsualización por playlist;
3. cachear top candidatos por entrada;
4. reutilizar resultados finales;
5. crear índices auxiliares en memoria;
6. persistir mejor la caché entre sesiones.

---

## 9) Qué guarda la app en local

Archivos importantes:

- `config.json`: preferencias de UI;
- `datos/mp3_index_*.json`: índice de la biblioteca;
- `datos/coincidencias.db`: coincidencias aprendidas;
- `datos/manual_matches.json`: caché manual;
- `datos/aliasconfig.json`: alias;
- `datos/alias_suggestions.json`: sugerencias de alias.

Todo eso debe considerarse local y regenerable.

---

## 10) Estructura funcional por archivo

### `main.py`

Función:

- `print_banner()`

Responsabilidad:

- arrancar la app;
- crear Tkinter;
- instanciar la UI principal.

### `manual_cache.py`

Funciones:

- `_ensure_dir()`
- `_strip_accents(s)`
- `_normalize_key(label)`
- `_exact_key(label)`
- `_basename_key(label)`
- `load_cache()`
- `save_cache(data)`
- `add_mapping(original_label, chosen_path)`
- `add_mappings(mapping)`
- `lookup(label)`

Responsabilidad:

- guardar y recuperar decisiones humanas.

### `indexer.py`

Funciones:

- `extraer_tags(ruta)`
- `generar_hash_carpeta(carpeta)`
- `inicializar_bd_memoria()`
- `procesar_archivo(args)`
- `cargar_indice(carpeta, progreso_callback=None, estado_callback=None)`

Responsabilidad:

- indexar la biblioteca;
- leer metadatos;
- construir el índice local y SQLite.

### `alias_suggester.py`

Funciones auxiliares:

- `_strip_accents(s)`
- `_normalize(s)`
- `_artist_signature(artist_norm)`
- `_tokens_jaccard(a, b)`
- `_is_diminutive(a, b)`

Clase:

- `ArtistAliasSuggester`

Funciones destacadas:

- `add(...)`
- `build_suggestions()`
- `save_suggestions(...)`
- `apply_selected(...)`

Responsabilidad:

- detectar alias plausibles y facilitar normalización.

### `playlist_updater.py`

Funciones:

- `_es_ruta_de_medio(linea)`
- `_strip_accents(s)`
- `_norm_min(s)`
- `_looks_like_channel(artist_norm)`
- `_split_artist_from_title_if_needed(artist, title)`
- `_parse_extvdj_line(line)`
- `_parse_m3u_with_meta(ruta_m3u)`
- `_format_extvdj_line(meta=None, path=None)`
- `_read_tags_from_file(path)`
- `_score_strings(a, b)`
- `_estimate_similarity(missing_meta, chosen_path)`
- `update_playlist_logic(...)`

Responsabilidad:

- leer playlists;
- conservar y generar metadatos;
- escribir salida final;
- respetar `output_path`.

### `matcher.py`

Funciones principales:

- `_cargar_alias_desde_json()`
- `normalizar_artista(s)`
- `sim_nombre(a, b)`
- `sim_titulo_suave(a, b)`
- `partir_artista_titulo_desde_nombre(nombre)`
- `_titulo_puro(tags_dict)`
- `canon_title(s)`
- `titulos_equivalentes(a, b)`
- `_artista_con_fallback(tags_dict)`
- `_es_remix(titulo)`
- `_artista_en_filename(path, artist_norm)`
- `_filename_es_compuesto_ajeno(path, expected_title)`
- `_es_netsearch_id(ruta)`
- `artistas_compatibles(expected_norm, candidate_norm)`
- `_labels_para_cache_manual(entrada)`
- `cargar_conexion_en_memoria()`
- `cargar_indice_desde_sqlite(mem_conn)`
- `_ensure_cache_tables(conn)`
- `buscar_en_cache(conn, clave)`
- `_duration_bucket(d)`
- `_basename_norm(ruta)`
- `_claves_aprendizaje_para_entrada(entrada)`
- `_buscar_en_cache_aprendida(conn, entrada)`
- `validar_cache_contra_entrada(indice_sqlite, ruta_cache, entrada, umbral=80)`
- `bitrate_por_path(indice_sqlite, path)`
- `calcular_puntaje(tags1, tags2)`
- `tags_similares(tags1, tags2, tolerancia_duracion=3)`
- `_preferir_no_remix(candidatos)`
- `_filtrar_por_artista_esperado(candidatos, artist_esperado_norm)`
- `_upgrade_por_alias(...)`
- `procesar_bloque_con_orden(...)`
- `_emit_progreso(...)`
- `_drain_logs_ordenados(...)`
- `fmt_bitrate(b)`
- `generar_clave_entrada(entrada)`
- `_mejor_por_similares(...)`
- `_acc_update(...)`
- `_aplicar_updates_en_disco(...)`
- `buscar_mejor_coincidencia(...)`

Responsabilidad:

- aplicar la jerarquía de resolución;
- reutilizar caches;
- seleccionar la mejor coincidencia;
- desempatar por bitrate;
- generar trazas claras.

### `gui.py`

Componentes destacados:

- `PlayerBackend`
- `EntryRecord`
- `PlaylistRecord`
- `PlaylistUpdaterApp`

Responsabilidad:

- construir la UI;
- hacer previsualización;
- mostrar contenido y detalle;
- gestionar candidatos;
- permitir revisión manual;
- controlar reproducción;
- lanzar análisis;
- escribir salida;
- guardar configuración;
- mantener trazas.

---

## 11) Cambios de UX que se buscaron

La UI se rediseñó con la intención de que el usuario pueda:

- ver qué playlist está analizando;
- ver el contenido completo sin abrir otras ventanas;
- ver si la ruta está bien o mal;
- revisar cada coincidencia;
- comparar score, bitrate y duración;
- elegir manualmente un candidato;
- buscar un archivo a mano;
- reproducir un candidato antes de aceptarlo;
- guardar en una ruta distinta;
- mantener estructura de directorios si hace falta.

---

## 12) Problemas observados y conclusiones

### Sobre la congelación de UI

La congelación venía de:

- trabajo pesado en el hilo principal;
- refreshs demasiado costosos;
- recalcular candidatos al seleccionar entradas;
- listas grandes y cambios de selección demasiado frecuentes.

### Sobre la eficiencia

Se confirmó que:

- las rutas válidas y de alta calidad no deberían hacer perder tiempo;
- el bitrate de previsualización es útil para evitar búsquedas;
- el lote grande necesita caché más agresiva;
- la UI nueva ya es funcional, pero todavía no es la versión más rápida posible.

### Sobre el aprendizaje

LTPF aprende por reutilización local, no por entrenamiento.

### Sobre `#EXTM3U`

La playlist actualizada sí debe llevar `#EXTM3U`. Eso no rompe nada; al contrario, la salida queda más estándar.

### Sobre `#EXTVDJ`

La nueva escritura conserva y regenera esos metadatos para mantener más contexto de la pista.

---

## 13) Guía operativa recomendada

Uso recomendado:

1. cargar biblioteca;
2. previsualizar playlist o lote;
3. revisar rutas y bitrate;
4. usar análisis selectivo si la colección ya está saneada;
5. revisar candidatos solo en incidencias;
6. usar reproducción integrada para comparar;
7. confirmar manualmente lo necesario;
8. guardar la salida donde el usuario quiera.

---

## 14) Resumen final

Lo que ya se puede afirmar con confianza sobre LTPF:

- es un reparador local de playlists;
- usa caches y coincidencias aprendidas;
- da prioridad a la calidad al resolver;
- permite revisión manual y reproducción;
- guarda metadatos enriquecidos;
- tiene una nueva UI con previsualización y paneles de contexto;
- puede optimizarse más convirtiendo la previsualización en caché de trabajo.

Este documento debe usarse como referencia principal de la rama nueva.
