# Guía de trabajo — nueva versión de LTPF

Este documento describe la UI nueva que vive en `NEW LTPF/LostTrackPlaylistFinder`.

Su objetivo es dejar claro:

- qué ya está implementado;
- qué flujo sigue la interfaz;
- cómo interactúa con el backend;
- y qué queda pendiente de optimización.

---

## 1) Estado actual

La base funcional heredada se ha mantenido, pero la interfaz ha cambiado de un formulario simple a una consola visual de análisis.

### Ya está implementado

- previsualización de playlists antes del análisis;
- árbol de playlists en el panel izquierdo;
- contenido completo de la playlist en el panel central;
- detalle de entrada, candidatos y reproductor en el panel derecho;
- selección manual de candidatos;
- búsqueda manual de archivo;
- botón para marcar una entrada como válida;
- botón para saltar incidencias;
- reproducción del candidato seleccionado;
- trazas y logs para diagnosticar cuellos de botella;
- selector de destino de salida;
- opción de guardar junto a la playlist original o en otra ruta;
- opción de mantener la estructura de carpetas;
- opción selectiva para analizar solo rutas inválidas o pistas por debajo de `320 kbps`.

### Sigue en evolución

- optimización de carga de lotes grandes;
- caché más profunda de la previsualización;
- reducción de trabajo repetido al cambiar entre playlists;
- mejora de tiempos cuando se recalculan candidatos.

---

## 2) Qué debe mostrar la UI

### Panel superior

Debe mostrar de golpe:

- botones principales de la UI antigua;
- rutas elegidas;
- estado global;
- ruta de música;
- ruta de playlists;
- carpeta de lote;
- destino de salida;
- si se mantiene estructura o no;
- estado del reproductor.

### Panel izquierdo

Contexto de la playlist:

- árbol de playlists que se están analizando o ya se analizaron;
- ruta del archivo `M3U/M3U8`;
- estado de la ruta;
- número total de entradas;
- entrada actual / índice actual;
- porcentaje de análisis.

### Panel central

Contenido de la playlist seleccionada:

- posición;
- nombre mostrado;
- ruta original;
- estado;
- validación de ruta;
- resultado elegido;
- bitrate en `kbps`.

### Panel derecho

Detalle de la coincidencia:

- metadatos detectados;
- pista original;
- decisión del matcher;
- candidatos disponibles;
- score;
- bitrate;
- duración;
- artista;
- botón para aceptar un candidato;
- botón para buscar manualmente;
- botón para marcar como válido;
- botón para saltar;
- botón para reproducir el candidato.

---

## 3) Flujo ideal de trabajo

1. Cargar carpeta o lote.
2. Ver la previsualización sin entrar todavía en análisis completo.
3. Confirmar que la ruta de salida es correcta.
4. Navegar por playlists y entradas.
5. Revisar solo las incidencias o pistas de baja calidad si está activo el filtro selectivo.
6. Escuchar candidatos cuando haga falta.
7. Elegir manualmente o buscar a mano.
8. Guardar la salida.

Este flujo busca evitar cambios de ventana y reducir revisiones innecesarias.

---

## 4) Comportamiento actual importante

### La UI no debe bloquearse

La búsqueda de candidatos se ejecuta en segundo plano y los resultados obsoletos se descartan con `request_id`.

### La previsualización importa

La app ya usa la previsualización para:

- mostrar el bitrate;
- detectar si una ruta existe;
- conservar entradas válidas de alta calidad;
- evitar búsquedas innecesarias.

### El análisis selectivo es real

Si activas el filtro:

- no se buscan candidatos para rutas válidas y con bitrate suficiente;
- sí se buscan para rutas inválidas o pistas por debajo del umbral.

---

## 5) Reproductor integrado

El reproductor no es una ventana aparte. Está embebido en la UI y sirve para escuchar:

- el candidato seleccionado;
- una selección manual;
- una pista antes de confirmarla.

Depende de `python-vlc` y de VLC instalado en el sistema.

---

## 6) Relación con el backend

La UI no reemplaza el backend. Lo orquesta.

### Entrada

- carpeta de música;
- playlist individual;
- lote de playlists;
- destino de salida;
- modo selectivo.

### Procesamiento

- carga índice si existe;
- crea previsualización;
- calcula candidatos;
- resuelve manualmente si hace falta;
- escribe playlist final.

### Salida

- playlist actualizada;
- trazas;
- cachés y confirmaciones manuales;
- metadatos enriquecidos.

---

## 7) Ruta de salida

La nueva UI permite:

- guardar junto a la playlist original;
- guardar en otra ruta;
- mantener la estructura de carpetas;
- abrir revisión manual durante lote si se desea.

El destino elegido se propaga hasta el escritor final de playlists.

---

## 8) Rendimiento visible desde la UI

El analisis usa el matcher del backend, pero la UI recibe el estado y los logs del proceso.

Al terminar el matching, el backend genera un resumen de rendimiento con:

- tiempo total;
- media por entrada;
- p50, p95 y maximo;
- entradas por segundo;
- entradas que evitaron escaneo completo;
- entradas que usaron escaneo completo;
- desglose por fase.

El resumen se muestra por la ruta de logs del analisis y tambien se guarda en:

```text
datos/analisis_rendimiento_YYYYMMDD_HHMMSS.log
```

La fase `cache_calidad` indica entradas resueltas por la cache interna de mayor bitrate por identidad musical.

---

## 9) Qué sigue por hacer

Prioridades razonables:

1. acelerar el lote grande;
2. cachear mejor la previsualización;
3. reutilizar más el trabajo ya hecho en la sesión;
4. reducir la latencia al cambiar de playlist o entrada;
5. mejorar la escritura incremental de resultados.

---

## 10) Conclusión

La nueva UI ya cumple su función principal: permite ver, revisar y corregir sin perder contexto.

El siguiente trabajo no es “hacer más pantallas”, sino reducir el coste de cada interacción y convertir más datos de previsualización en caché útil.
