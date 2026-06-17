<p align="center">
  <img src="logo.svg" alt="LTPF Logo" width="360">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows-lightgrey">
</p>

# Lost Track Playlist Finder

**Lost Track Playlist Finder (LTPF)** es una herramienta de escritorio para Windows escrita en Python que:

- detecta rutas rotas en playlists `M3U`/`M3U8`;
- busca la mejor coincidencia en una biblioteca local;
- conserva rutas válidas cuando ya cumplen el criterio de calidad;
- permite revisión manual y reproducción de candidatos;
- escribe playlists actualizadas con metadatos enriquecidos;
- mantiene cachés locales para acelerar ejecuciones futuras.

La aplicación está pensada para trabajar **100% en local**. No depende de servicios en la nube y los datos generados por el análisis se guardan en `datos/`.

---

## Estado actual

La versión activa del repositorio incluye una **nueva UI** con:

- previsualización de playlists antes de analizar;
- panel izquierdo con árbol de playlists;
- panel central con contenido completo de la playlist;
- panel derecho con detalle, candidatos y reproductor integrado;
- búsqueda asíncrona de candidatos para no bloquear la interfaz;
- selector de salida para guardar junto a la playlist original o en otra ruta;
- opción para analizar solo rutas inválidas o pistas por debajo de `320 kbps`;
- escritura de `#EXTM3U` y bloques `#EXTVDJ` en la salida.

---

## Lógica de coincidencia

La resolución no usa un único score. El flujo real prioriza:

1. **Caché manual**: si el usuario resolvió antes una entrada equivalente, esa ruta tiene prioridad.
2. **Caché aprendida**: si ya existe una decisión previa válida, se reutiliza.
3. **Tags reales**: se comparan `artist`, `title`, `duration` y `bitrate`.
4. **Nombre de archivo**: se usa como respaldo con similitud difusa.
5. **Refinamientos**: se aplican alias, filtros por artista, preferencia por no-remix y desempate por bitrate.

Regla importante: si varias opciones son válidas, la app intenta quedarse con la de **mayor calidad disponible**.

---

## Qué significa “aprende”

LTPF **no entrena un modelo de IA**. Aprende en el sentido práctico de que reutiliza resultados anteriores:

- caché manual de elecciones confirmadas;
- caché de coincidencias aprendidas;
- alias de artista guardados localmente;
- previsualización con bitrate detectado para evitar trabajo repetido;
- resultados ya resueltos en runs anteriores.

---

## Requisitos

- Windows 10/11
- Python 3.10 o superior
- Paquetes:
  - `mutagen`
  - `tqdm` (opcional para algunos flujos)
  - `python-vlc` si se quiere usar el reproductor integrado

Instalación básica:

```bash
pip install mutagen tqdm
```

Si vas a usar el reproductor:

```bash
pip install python-vlc
```

---

## Uso rápido

### Abrir la app

```bash
python main.py
```

### Flujo recomendado

1. Selecciona carpeta de música.
2. Selecciona una playlist o un lote.
3. Revisa la previsualización.
4. Ajusta la ruta de salida si hace falta.
5. Activa el análisis completo o el filtrado por bitrate/rutas inválidas.
6. Revisa candidatos, confirma manualmente si es necesario.
7. Guarda la playlist actualizada.

---

## Estructura del proyecto

```text
.
├── alias_suggester.py      # Sugerencias y aplicación de alias
├── gui.py                  # UI nueva, previsualización, revisión manual y reproductor
├── indexer.py              # Indexado local de la biblioteca
├── main.py                 # Entrada de la aplicación
├── manual_cache.py         # Caché manual de elecciones confirmadas
├── matcher.py              # Motor de búsqueda y decisión
├── playlist_updater.py     # Parseo y escritura de playlists
├── config.json             # Preferencias locales de la UI
├── README.md
├── GUIA_LTPF.md            # Guía técnica del proyecto
├── GUIA_NUEVA_VERSION.md   # Guía de la nueva UI
├── RESUMEN_CAMBIOS_Y_PLAN.md
└── datos/                  # Índices, cachés y salidas generadas
```

---

## Datos locales generados

- `datos/mp3_index_*.json`: índice de la biblioteca.
- `datos/coincidencias.db`: coincidencias aprendidas.
- `datos/manual_matches.json`: caché manual.
- `datos/aliasconfig.json`: alias aceptados.
- `datos/alias_suggestions.json`: sugerencias de alias.

Todo eso se genera en local y no debe subirse al repositorio.

---

## Rendimiento

La aplicación usa dos ideas para no recalcular todo en cada ejecución:

- **índice local** de la biblioteca;
- **cachés** de coincidencias, alias y decisiones manuales.

Además, la nueva UI reutiliza la previsualización para:

- mostrar bitrate antes del análisis;
- evitar búsquedas innecesarias en rutas ya válidas y de alta calidad;
- pasar al matcher únicamente lo que realmente necesita revisión cuando se activa el modo selectivo.

### Cache de calidad

La version nueva incluye una cache interna de calidad dentro de `matcher.py`.

Esta cache agrupa pistas por identidad musical:

- artista normalizado;
- titulo canonico;
- duracion aproximada;
- si parece remix o no-remix.

Dentro de cada grupo conserva la pista con mayor bitrate. Esto permite resolver entradas con tags fiables sin recorrer toda la biblioteca. La cache no compara audio real y no descarta pistas solo por parecerse de nombre: exige que la identidad musical encaje.

### Logs de rendimiento

Cada analisis genera un resumen de rendimiento con:

- entradas procesadas;
- encontradas y no encontradas;
- tamano de la biblioteca indexada;
- tiempo total;
- media por entrada;
- p50, p95 y maximo;
- entradas por segundo;
- porcentaje que evito escaneo completo;
- desglose por fase (`ruta_valida`, `cache_manual`, `cache_global`, `cache_calidad`, `tags_scan`, `nombre_scan`, etc.).

El resumen tambien se guarda en:

```text
datos/analisis_rendimiento_YYYYMMDD_HHMMSS.log
```

---

## Documentación útil

- `GUIA_MAESTRA_LTPF.md`: guía principal con todo lo hablado y cambiado.
- `GUIA_LTPF.md`: referencia técnica del proyecto.
- `GUIA_NUEVA_VERSION.md`: diseño y estado de la nueva UI.
- `RESUMEN_CAMBIOS_Y_PLAN.md`: resumen de cambios y plan de rendimiento.

---

## Licencia

MIT.
