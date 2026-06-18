# LTPF — resumen de cambios y plan de rendimiento

Este documento resume el estado real de la nueva versión de LTPF y deja una hoja de ruta para usar la previsualización como base de caché de trabajo.

---

## 1) Cambios ya realizados

### 1.1 Previsualización de playlists

- La app puede cargar una playlist o un lote en modo **previsualización** sin lanzar el análisis completo.
- El panel izquierdo muestra el árbol de playlists y su estado.
- El panel central muestra el contenido completo de cada playlist.
- El panel derecho muestra detalle, candidatos y reproductor.
- La previsualización calcula y muestra el **bitrate** cuando el archivo existe en la biblioteca.

### 1.2 Evitar bloqueos de la UI

- La búsqueda de candidatos al seleccionar una entrada ya no se ejecuta en el hilo principal.
- El cálculo de candidatos se hace en un hilo de fondo y se aplica con `request_id` para descartar resultados obsoletos.
- La carga de índice está protegida para evitar carreras.
- El lote ya no reconstruye el árbol completo en cada iteración; actualiza solo la fila afectada.

### 1.3 Filtro de análisis selectivo

- Se añadió la opción de analizar solo:
  - entradas con ruta inválida;
  - entradas con bitrate inferior a `320 kbps`.
- Esta opción usa el bitrate calculado en la previsualización como referencia prioritaria.
- Si una ruta válida ya está a `320 kbps` o más, el matcher la conserva sin buscar alternativa.

### 1.4 Metadatos de playlist

- La app vuelve a escribir playlists con `#EXTM3U`.
- También conserva y regenera bloques `#EXTVDJ`.
- La salida puede incluir:
  - `filesize`
  - `lastplaytime`
  - `artist`
  - `title`
  - `remix`
  - `songlength`
- `lastplaytime` se conserva desde la playlist original.
- `filesize` se recalcula desde el archivo real guardado.
- `songlength` se regenera a partir de la pista resuelta.

### 1.5 Destino de salida

- Ya existe selector para guardar en otra ruta.
- Se corrigió un fallo por el que el análisis seguía escribiendo en la carpeta original aunque se hubiera elegido una ruta destino distinta.
- La ruta de salida ahora se recalcula de forma explícita al guardar.
- El destino se propaga desde la UI hasta el escritor real del archivo.

### 1.6 Integridad funcional

- Se mantiene la selección manual de candidatos.
- Se mantiene la reproducción de candidatos seleccionados.
- Se mantiene el caché manual y el caché de alias.
- El backend sigue funcionando con el mismo flujo base, pero con más control desde la UI.

### 1.7 Cache de calidad por identidad musical

- Se anadio en `matcher.py` una cache interna que agrupa canciones por identidad musical.
- La identidad usa:
  - artista normalizado;
  - titulo canonico;
  - duracion aproximada;
  - indicador remix/no-remix.
- Para cada identidad se conserva la pista con mayor bitrate.
- Si una entrada trae tags fiables, el matcher puede resolverla por `cache_calidad` antes de hacer un escaneo completo de la biblioteca.
- La cache no analiza audio real y no sustituye al matcher completo cuando la identidad no es suficientemente clara.

### 1.8 Resumen de rendimiento

- Se anadio un resumen final de rendimiento por analisis.
- El resumen muestra:
  - entradas procesadas;
  - encontradas y no encontradas;
  - tamano de biblioteca;
  - tiempo total;
  - media por entrada;
  - p50, p95 y maximo;
  - entradas por segundo;
  - entradas que evitaron escaneo completo;
  - entradas que usaron escaneo completo;
  - desglose por fase.
- Cada ejecucion guarda un log en:

```text
datos/analisis_rendimiento_YYYYMMDD_HHMMSS.log
```

### 1.9 Log de indexado y traza persistente de GUI

- Se anadio un log especifico para el indexado:

```text
datos/indexado_rendimiento_YYYYMMDD_HHMMSS.log
```

- Este log separa:
  - carga de JSON existente;
  - escaneo de carpetas;
  - extraccion de metadata;
  - preparacion SQLite;
  - escritura JSON;
  - escritura SQLite;
  - generacion de sugerencias de alias.
- Tambien registra workers, archivos por segundo, tamano de indices y cantidad de archivos sin metadata legible.
- La GUI guarda una traza por sesion en:

```text
datos/app_trace_YYYYMMDD_HHMMSS.log
```

- El JSON de indice nuevo se guarda en formato compacto para reducir tamano y tiempo de escritura.

### 1.10 Índice auxiliar perezoso para scans

- `tags_scan` y `nombre_scan` usan un índice auxiliar por tokens para reducir candidatos antes de puntuar.
- El índice auxiliar se construye solo si el bloque del worker llega a una fase de scan.
- El índice auxiliar se limita a tokens presentes en ese bloque, en vez de construir un mapa global completo por worker.
- Si no hay candidatos por tokens, el matcher vuelve al escaneo completo anterior.
- El log de analisis lista las entradas mas lentas con:
  - fase;
  - tiempo;
  - candidatos evaluados;
  - etiqueta de entrada;
  - resultado elegido.

### 1.11 Índice incremental estilo Everything

- El índice guarda `path + size + mtime`.
- En reindexados reutiliza entradas sin cambios.
- Solo lee metadata con `mutagen` en archivos nuevos o modificados.
- Elimina entradas que ya no aparecen en la biblioteca.
- Si no hay cambios y SQLite ya tiene tokens, sale sin reescribir JSON ni SQLite.
- El log de indexado muestra reutilizadas, nuevas/modificadas y eliminadas.

### 1.12 Tokens persistentes en SQLite

- Se añadió `track_tokens`.
- Guarda tokens de:
  - título;
  - artista;
  - nombre de archivo.
- Usa `track_id` entero para no repetir rutas completas.
- El matcher consulta `track_tokens` antes de crear candidatos en memoria.
- Si no hay tabla o candidatos, mantiene el fallback anterior.

### 1.13 Alias fuera del flujo normal

- La GUI y el análisis llaman a `cargar_indice(..., generar_alias=False)`.
- Esto evita pagar el coste de alias durante carga/análisis normal.
- El indexador conserva `generar_alias=True` para usos explícitos.

### 1.14 Tests de referencia

- `compileall`: correcto.
- imports principales: correctos.
- indexado incremental sin cambios:
  - `5.66 s`;
  - `127730` entradas reutilizadas;
  - `0` archivos releídos;
  - SQLite con tokens: `83.96 MB`.
- análisis playlist de referencia:
  - `105/105` encontradas;
  - `31.87 s`;
  - `0` escaneos completos.

---

## 2) Estado actual de la arquitectura

### UI

- `gui.py` concentra:
  - carga de playlists;
  - previsualización;
  - selección de entradas;
  - reproducción;
  - guardado;
  - configuración de salida;
  - trazas.

### Motor de coincidencia

- `matcher.py` sigue resolviendo coincidencias por:
  - tags;
  - nombre;
  - caché manual;
  - caché aprendida;
  - alias;
  - desempate por bitrate.

### Escritura de playlist

- `playlist_updater.py`:
  - parsea `#EXTVDJ`;
  - llama al matcher;
  - escribe la playlist final;
  - admite `output_path` externo;
  - conserva metadatos enriquecidos.

---

## 3) Qué está optimizando ya la previsualización

- Reutiliza el bitrate detectado para decidir si una pista válida se conserva o no.
- Evita búsquedas innecesarias para pistas ya válidas y de calidad suficiente.
- Sirve como base para construir una caché de análisis más agresiva.

---

## 4) Qué falta para acelerar de verdad el análisis

La previsualización todavía **no** es una caché completa del matcher. Hoy evita trabajo en algunos casos, pero no guarda el resultado pesado de la búsqueda.

Cuellos de botella que siguen existiendo:

- escaneo de la biblioteca completa para candidatos;
- evaluación repetida de la misma entrada en distintos runs;
- lectura repetida de metadatos de archivos candidatos;
- selección repetida de mejores coincidencias por tags/nombre;
- reapertura de rutas ya resueltas sin reutilizar el resultado exacto anterior.

---

## 5) Plan de ataque para convertir la previsualización en caché útil

### Fase 1 — Instrumentación fina

Objetivo: medir con precisión dónde se va el tiempo.

- Registrar tiempos por entrada:
  - parseo de playlist;
  - cálculo de bitrate en previsualización;
  - validación de ruta;
  - búsqueda de candidatos;
  - selección final;
  - escritura de salida.
- Registrar el tamaño de la biblioteca usada en cada run.
- Medir cuántas entradas se:
  - conservan sin búsqueda;
  - omiten por filtro;
  - resuelven por caché;
  - resuelven por búsqueda completa.

### Fase 2 — Caché de previsualización por playlist

Objetivo: no volver a recalcular datos ya conocidos.

Guardar por playlist y por índice de entrada:

- ruta original;
- ruta normalizada;
- tags originales;
- bitrate previsualizado;
- estado de ruta;
- `lastplaytime`;
- `filesize`;
- resultado elegido si ya existe.

Esto permitiría reusar la previsualización en:

- reanálisis de la misma playlist;
- revisiones manuales posteriores;
- comparación de lotes.

### Fase 3 — Caché de candidatos por entrada

Objetivo: evitar volver a recorrer toda la biblioteca.

Para cada entrada, guardar:

- clave de entrada estable;
- hash de la biblioteca o índice;
- top N candidatos;
- score;
- bitrate;
- ruta elegida;
- motivo de la elección.

Invalidar el caché si cambia:

- la biblioteca;
- el índice;
- la playlist original;
- el algoritmo de scoring.

### Fase 4 — Reutilización del resultado final

Objetivo: que la siguiente ejecución sea casi incremental.

- Si una entrada ya fue resuelta correctamente, reusar esa resolución antes de buscar.
- Si una entrada ya fue revisada manualmente, priorizar el caché manual.
- Si el bitrate previsualizado ya supera el umbral y la ruta existe, conservarla sin cálculo adicional.

### Fase 5 — Índices auxiliares

Objetivo: reducir el coste del matcher.

- Crear índices en memoria por:
  - artista normalizado;
  - título normalizado;
  - bitrate;
  - duración por buckets.
- Usar esos índices para reducir el conjunto de candidatos antes de puntuar.
- Evitar recorrer la biblioteca entera cuando la entrada ya trae tags útiles.

### Fase 6 — Persistencia del caché

Objetivo: que el rendimiento mejore entre sesiones.

- Guardar el caché en disco.
- Mantener una versión de caché por:
  - biblioteca;
  - playlist;
  - fecha de modificación.
- Invalidar automáticamente cuando cambie la fuente.

---

## 6) Orden recomendado de implementación

1. Medir.
2. Cachear previsualización por entrada.
3. Cachear candidatos por clave de entrada.
4. Cachear resultados finales y manuales.
5. Añadir índices auxiliares para reducir búsquedas.
6. Persistir el caché entre sesiones.

---

## 7) Riesgos a controlar

- No confundir una ruta válida con una ruta “buena”.
- No fiarse de bitrate sin validar que corresponde al mismo archivo.
- No reutilizar caché si cambió la biblioteca.
- No guardar resultados obsoletos cuando el usuario cambia de playlist o de selección.

---

## 8) Conclusión

La versión nueva ya mejora bastante la experiencia:

- previsualiza sin analizar;
- evita congelaciones;
- conserva metadatos;
- respeta el destino de salida;
- permite filtrar análisis innecesarios.

El siguiente salto real de rendimiento no vendrá de la UI, sino de convertir la previsualización en una caché de trabajo del matcher.
