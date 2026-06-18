# Investigacion de entradas lentas

## Objetivo

No hacer ajustes exclusivos para canciones concretas. El objetivo fue entender por que algunas entradas tardaban demasiado y corregir patrones generales del matcher.

## Logs revisados

Referencia anterior:

```text
Log: datos/analisis_rendimiento_20260618_030311.log
Entradas: 105/105
Biblioteca: 127718 pistas
Tiempo total pared: 32.32 s
Maximo por entrada: 19.500 s
tags_scan: 4 entradas | total 61.25 s | max 19.500 s
nombre_scan: 1 entrada | total 14.58 s
```

Entradas que revelaron el problema:

```text
Zzoilo Aitana - Mon Amour Remix: 14343 candidatos, 19.500 s
VIDA DE RICO: 793 candidatos, 17.747 s
Daniela Romo - Yo No Te Pido La Luna: 663 candidatos, 14.578 s
BIZARRAP FT. QUEVEDO: 1138 candidatos, 13.516 s
Lele Pons & Guaynaa: 632 candidatos, 10.486 s
```

## Causas reales encontradas

1. Algunas entradas tenian diccionario `tags`, pero sin tokens utiles de titulo/artista. El matcher entraba en `tags_scan` igualmente y podia puntuar contra toda la biblioteca.

2. La seleccion de candidatos por tags usaba union de tokens de titulo y artista. En titulos con tokens comunes, esto podia inflar mucho el conjunto antes de calcular similitud.

3. Los refinamientos posteriores (`quality_refine`, `alias_quality`, `derived_equal`) podian recorrer mas pistas de las que indicaba el contador principal de candidatos.

4. El fallback de cache RAM podia construirse aunque SQLite ya tuviera `track_tokens`. Si SQLite moderna responde sin candidatos, no hace falta construir otro indice en memoria.

## Cambios aplicados

- `tags_scan` se salta si no hay tokens utiles de titulo/artista.
- La busqueda por tags usa interseccion titulo+artista cuando ambas senales existen.
- Si la interseccion queda vacia, se conserva un fallback a union para no perder compatibilidad.
- `quality_refine`, `alias_quality` y `derived_equal` trabajan sobre el subconjunto filtrado.
- Si SQLite tiene `track_tokens`, no se construye la cache RAM de fallback ante una respuesta vacia.
- El resumen de rendimiento muestra subtiempos por entrada lenta:
  - `candidate_select`
  - `score_loop`
  - `quality_refine`
  - `alias_quality`
  - `name_candidate_select`
  - `name_score_loop`
  - `derived_equal`

## Resultados medidos

Pruebas en frio, desactivando temporalmente `datos/coincidencias.db` y restaurandolo despues:

```text
Referencia anterior: 32.32 s
Tras limitar refinamientos: 21.58 s
Tras evitar fallback RAM innecesario: 18.26 s
Tras interseccion titulo+artista y skip de tags sin tokens: 12.38 s
```

Resultado final:

```text
Log: datos/analisis_rendimiento_20260618_032904.log
Entradas procesadas: 105/105
Encontradas: 105
Tiempo total pared: 12.38 s
Media por entrada: 0.007 s
p95: 0.008 s
Maximo: 0.219 s
Rendimiento: 8.48 entradas/s
Escaneo completo: 0
```

Ejemplos representativos tras la mejora:

```text
Zzoilo Aitana - Mon Amour Remix: 14343 candidatos -> 3 candidatos
Daniela Romo - Yo No Te Pido La Luna: 14.578 s -> 0.219 s
tags_scan total: 61.25 s -> 0.16 s
```

## Conclusion

El problema no eran canciones sueltas. Eran rutas generales del algoritmo que ampliaban demasiado candidatos o hacian trabajo auxiliar fuera del subconjunto filtrado. La solucion aplicada reduce el trabajo antes de puntuar y deja instrumentacion suficiente para detectar el siguiente cuello de botella con logs.
