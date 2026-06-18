# Plan de optimizacion inspirado en Everything y Mp3tag

## Objetivo

Optimizar New LTPF aprendiendo de dos ideas externas:

- Everything: indexado incremental, busqueda por tokens y evitar rehacer trabajo.
- Mp3tag: metadatos limpios, exportables y reutilizables.

La app no depende obligatoriamente de ninguna de las dos herramientas.

## Fase 1 - Indice incremental

Aplicado.

El indexador guarda por archivo:

```text
path
size
mtime
```

En cada carga:

- reutiliza entradas sin cambios;
- relee metadata solo de archivos nuevos o modificados;
- elimina entradas que ya no aparecen;
- sale sin reescribir JSON/SQLite si no hay cambios y la SQLite ya esta preparada.

Resultado medido:

```text
Modo: incremental_sin_cambios
Entradas: 127730
Reutilizadas: 127730
Nuevas/modificadas: 0
Total: 5.66 s
```

## Fase 2 - Alias fuera del flujo principal

Aplicado.

La GUI y el analisis llaman:

```python
cargar_indice(..., generar_alias=False)
```

Esto evita pagar la generacion de sugerencias de alias durante el primer uso normal. El indexador conserva `generar_alias=True` para ejecuciones explicitas.

## Fase 3 - Tokens persistentes

Aplicado.

SQLite incluye:

```text
indice_audio
track_tokens
```

`track_tokens` guarda tokens de:

- titulo;
- artista;
- nombre de archivo.

Los tokens apuntan a `track_id` entero, no a rutas completas, para reducir tamano.

Resultado medido:

```text
track_tokens: 1131582 filas
SQLite: 83.96 MB
```

## Fase 4 - Matcher usando tokens

Aplicado.

Antes de construir candidatos en memoria, el matcher consulta `track_tokens`. Si no hay resultados o la tabla no existe, usa el fallback anterior.

Resultado medido con playlist de referencia:

```text
Entradas: 105
Encontradas: 105
Tiempo analisis: 31.87 s
Escaneo completo: 0
```

Comparativa:

```text
Referencia inicial: 48.56 s
Antes de tokens persistentes: 54.58 s
Despues de tokens persistentes: 31.87 s
```

## Fase 5 - Mp3tag como fuente opcional

Pendiente.

Uso recomendado futuro:

- exportar CSV/TSV desde Mp3tag;
- importar `path`, `artist`, `title`, `duration`, `bitrate`;
- usarlo como fuente auxiliar de metadatos;
- leer con `mutagen` solo lo que falte o haya cambiado.

No debe ser dependencia obligatoria.

## Fase 6 - Everything como fuente opcional

Pendiente.

Uso recomendado futuro:

- detectar `es.exe`;
- obtener listado rapido de archivos si Everything esta disponible;
- comparar altas/bajas/cambios contra el indice propio;
- caer a `os.walk` si Everything no existe.

No sustituye a `mutagen`, porque Everything localiza archivos pero no interpreta tags musicales.

## Fase 7 - Reduccion real de candidatos en el matcher

Aplicado.

La investigacion de los logs mostro que las entradas lentas no eran un caso aislado de canciones concretas. Habia tres patrones generales:

- entradas con `tags` presentes pero sin tokens utiles de titulo/artista entraban igualmente en `tags_scan` y podian acabar puntuando contra toda la biblioteca;
- la seleccion de candidatos por tags unia tokens de titulo y artista, lo que inflaba casos con tokens comunes como `mon`/`amour`;
- algunos refinamientos posteriores (`quality_refine`, `alias_quality`, `derived_equal`) podian recorrer mas pistas de las que indicaba el contador principal.

Cambios aplicados:

- `tags_scan` se omite si no hay tokens utiles de titulo/artista;
- la busqueda por tags usa interseccion titulo+artista cuando ambas senales existen, con fallback a union si la interseccion queda vacia;
- los refinamientos trabajan sobre el subconjunto de candidatos ya filtrado;
- si SQLite ya tiene `track_tokens`, una respuesta vacia no dispara la construccion cara de la cache RAM de fallback;
- el log de entradas lentas muestra subtiempos internos: `candidate_select`, `score_loop`, `quality_refine`, `alias_quality`, `name_score_loop`, `derived_equal`, etc.

Comparativa en frio, desactivando temporalmente la cache global de coincidencias y restaurandola despues:

```text
Antes: 32.32 s | max entrada 19.500 s | tags_scan total 61.25 s | nombre_scan max 14.578 s
Despues: 12.38 s | max entrada 0.219 s | tags_scan total 0.16 s | nombre_scan max 0.219 s
Biblioteca: 127718 pistas | Playlist: 105 entradas | Encontradas: 105/105
```

El caso `Zzoilo Aitana - Mon Amour Remix` paso de 14343 candidatos a 3 candidatos. El caso de `Daniela Romo` dejo de pagar un scoring completo por tags sin tokens utiles y quedo limitado a la busqueda por nombre.
