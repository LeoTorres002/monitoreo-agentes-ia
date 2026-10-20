# SIAF - Sistema de Inteligencia Forestal

## 1. ¿De qué trata el proyecto completo?

SIAF (Sistema de Inteligencia Forestal) es una "maquiladora digital forestal" cuyo objetivo es automatizar la elaboración de Programas de Manejo Forestal (PMF), conforme a lo establecido en la Norma Oficial Mexicana **NOM-152-SEMARNAT-2023**.

La idea central del proyecto es simplificar drásticamente un proceso que tradicionalmente requiere horas de trabajo técnico: que, a partir de únicamente **tres insumos** (archivo de campo, inventario forestal y shapefile de la zona de trabajo), el sistema sea capaz de generar un **informe forestal completo, trazable y válido conforme a la NOM**.

### 1.1 Objetivo funcional

El caso de uso final buscado es el siguiente:

1. El usuario carga tres archivos:
   - **Archivo de campo** (`.csv`)
   - **Inventario forestal** (`.csv`)
   - **Shape de la zona** (`.shp` + sus archivos auxiliares `.dbf`, `.prj`, `.shx`, etc.)
2. SIAF procesa dichos insumos de manera orquestada.
3. El sistema genera un **PMF completo**, compuesto por: tablas estructuradas conforme a lo que exige la NOM-152, planos cartográficos en PDF, validaciones normativas, auditoría de calidad y un reporte final verificable.

### 1.1.1 ¿Qué son esos tres archivos?

El sistema está diseñado para trabajar directamente con la información tal como se levanta en campo, estandarizándola y transformándola hasta obtener el informe forestal. A continuación, se describe cada uno de ellos:

#### 1. Archivo de campo (.csv)

Es el registro de los datos de muestreo obtenidos durante el levantamiento de campo. Define cómo se organizó el inventario (sitios, parcelas y Unidades de Muestreo Manejables - **UMM**), permitiendo agrupar y dar trazabilidad a los datos arbóreos.

- **Función:** Describe el diseño de muestreo (qué UMM y Sitios se midieron), establece la relación entre los árboles y su ubicación de muestreo, y asegura que los cálculos dasométricos correspondan a las superficies muestreadas.
- **Qué aporta al sistema:** Proporciona el contexto de muestreo necesario para agrupar correctamente los individuos inventariados, validar la consistencia entre campo e inventario y mantener la trazabilidad de toda la información.
- **Campos típicos:** `UMM`, `Sitio`, `Parcela`, coordenadas (X,Y o UTM), fecha de levantamiento, brigada, pendiente, exposición, tipo de vegetación, cobertura, observaciones, entre otros.
- **Comportamiento en el ejemplo de referencia:** En `MEMORIA DE CALCULO_EJIDO TOPIAXLS.xls` (hoja **`ER`**), el archivo de campo **no se encuentra como un archivo `.csv` separado**, sino que **está embebido junto con el inventario forestal**. Ahí aparecen las columnas `UMM` y `Sitio`, que se repiten por cada árbol y utilizan *forward-fill* (relleno hacia abajo) cuando vienen vacías. Además, la **georreferenciación por sitio** (`UTM_X`, `UTM_Y`) se encuentra en las tablas del PMF (`ejemplo.pdf`, páginas **82–84**), correspondientes a la lista de "Sitios de muestreo".
- **Notas de procesamiento:** SIAF normaliza este tipo de archivos combinados (manejo de `skiprows`, encoding `latin-1`, *forward-fill* de `UMM` y `Sitio`), pudiendo extraer los **registros únicos agrupados por `(UMM, Sitio)`** para generar el archivo de campo independiente a partir del origen combinado.

#### 2. Inventario forestal (.csv)

Es el archivo con los datos dasométricos medidos directamente en cada individuo arbóreo dentro de los sitios de muestreo. Constituye la base numérica para todos los cálculos del PMF.

- **Función:** Contiene la información de cada árbol inventariado para estimar variables forestales como volumen, área basal, densidad, altura y composición de especies.
- **Qué aporta al sistema:** Alimenta al agente **Forester**, quien realiza los cálculos dasométricos requeridos para generar las tablas que exige la NOM-152.
- **Campos típicos:** `UMM`, `Sitio`, `Árbol`, `Especie`, Diámetro a la Altura del Pecho (**DAP** o `Dn (cm)`), Altura (`At (m)`), Clase de árbol, Estado fitosanitario, Volumen (`VTA (m3vta)`) u otras variables auxiliares.
- **Comportamiento en el ejemplo de referencia:** En el archivo `MEMORIA DE CALCULO_EJIDO TOPIAXLS.xls`, hoja **`ER`** ("Existencias Reales"), se encuentran combinados tanto el muestreo de campo como el inventario arbóreo. Las columnas arbóreas son: `Árbol`, `Especie`, `Dn (cm)`, `At (m)`, `Área Basal (m2)` y `VTA (m3vta)`, todas vinculadas a sus respectivas claves de agrupación `UMM`/`Sitio`. Para este ejemplo se registran **19,112 árboles**, distribuidos en **5 UMM** y **403 Sitios de muestreo**.
- **Importancia:** Es el insumo cuantificable que, junto con la superficie del polígono, permite obtener los resultados dasométricos que posteriormente se estructuran en los archivos `tablas_nom/*.json`.

#### 3. Shape de la zona (.shp + sus archivos auxiliares .dbf, .prj, .shx, etc.)

Es la capa vectorial (polígono) que delimita geográficamente la zona de trabajo, ya sea el predio, rodal, área de aprovechamiento o polígono objeto del PMF.

- **Función:** Define los límites exactos del área a analizar y representar cartográficamente.
- **Qué aporta al sistema:** Permite calcular la **superficie_ha** (área en hectáreas) a partir de su geometría, con el Sistema de Coordenadas de Referencia (**CRS**) correcto y proyectado. Este dato es **obligatorio** para el agente **Forester**. Además, sirve de base para la generación de los planos cartográficos por parte del agente **GIS**.
- **Archivos requeridos:**
  - `.shp` – Geometría (polígonos/entidades del área)
  - `.dbf` – Tabla de atributos asociados al polígono
  - `.prj` – Definición del Sistema de Coordenadas (CRS). **Es fundamental** para obtener un cálculo correcto del área
  - `.shx` – Índice espacial para lectura eficiente del shapefile
  - **Adicionales comunes:** `.cpg` (codificación de caracteres), pudiendo incluir otros auxiliares según el software con el que fue exportado. Deben cargarse **todos juntos** para que el shapefile sea válido.
- **Comportamiento en el ejemplo de referencia:** En `rag_documents/ejemplo/` **no se incluye ningún archivo vectorial** (`.shp`, `.dbf`, `.prj`, `.shx`). El límite de la zona únicamente aparece **representado gráficamente** en el mapa/croquis del `ejemplo.pdf` (con referencias al polígono, ubicación y rodales), pero **no está entregado como shapefile** dentro de la carpeta del ejemplo.
- **Notas importantes:** Para obtener el área en hectáreas correctamente, se recomienda que el shape esté en un **CRS proyectado** (no geográfico en grados). SIAF utiliza este polígono para derivar la `superficie_ha` de forma trazable, evitando que dicho dato dependa exclusivamente de un ingreso manual.

### 1.2 Principios de diseño

- **Soberanía tecnológica**: Es 100% local. Utiliza Ollama y Bonsai 27B (llama.cpp) para modelos de lenguaje, sin depender de APIs externas ni de servicios en la nube.
- **Arquitectura modular por agentes**: Cada etapa del proceso (dasometría, cartografía, normatividad, auditoría, etc.) está separada en su propio microservicio, lo que facilita su desarrollo, mantenimiento y escalado.
- **Multitenant y aislado**: Los datos de cada cliente/proyecto se organizan en `siaf_users/{CLIENTE}/{PROYECTO}/[inputs|outputs]`, manteniendo separados insumos, resultados y conocimiento sensible.
- **Basado en conocimiento normativo vivo**: Cuenta con un RAG sobre `rag_documents/normativa/` (NOM-152, manuales y actualizaciones), permitiendo que las consultas y validaciones se basen en la normativa propia, no en generalizaciones de modelos.
- **Robusto ante limitaciones de hardware**: Diseñado para funcionar con modelos locales en CPU (Bonsai 27B F16), incluyendo mecanismos de fallback, segmentación de bloques y recuperación ante salidas JSON incompletas.
- **Trazable y auditable**: Genera artefactos estructurados (`tablas_nom/*.json`, `REPORTE_CALIDAD_SIAF.json`) que permiten verificar que el resultado cumple con los requisitos de la norma.

### 1.3 Alcance

El proyecto busca cubrir todo el flujo necesario para obtener un informe forestal listo para revisión, desde la ingesta y validación de datos de campo, hasta la generación de cartografía y la auditoría de cumplimiento normativo, minimizando la intervención manual del usuario.

## 2. Arquitectura del Sistema

SIAF está compuesto por 6 agentes independientes, cada uno con su propio puerto, endpoint y responsabilidad. Todo es orquestado por el `Manager`.

| Agente | Directorio | Puerto | Endpoint Principal | Función |
|---|---|---|---|---|
| **Manager (Orquestador)** | `agente_manager/` | `8000` | `POST /procesar` | Orquesta el flujo completo con CrewAI, ejecuta tareas en segundo plano y coordina al resto de agentes. |
| **Forester (Dasometría)** | `agente_forester/` | `8085` | `POST /calculate` | Calcula variables dasométricas a partir del inventario. Requiere `superficie_ha` en el payload, normaliza CSV (encoding, skiprows, forward-fill de UMM/Sitio). |
| **GIS (Cartografía)** | `agente_gis/` | `8081` | `POST /generate-planos` | Genera los planos cartográficos del proyecto. Actualmente se encuentra como *stub* (retorna "ready" sin renderizar). |
| **Environmental (RAG Normativo)** | `agente_environmental/` | `8082` | `GET /consultar?pregunta=` | Motor de consulta normativo basado en LangChain + Chroma. Indexa todos los PDFs de `rag_documents/normativa/` con reindexado incremental por hash. |
| **Compliance (Auditor)** | `agente_compliance/` | `8083` | `POST /audit` | Audita el cumplimiento de la NOM-152. Requiere `outputs/tablas_nom/*.json` y **≥ 4 planos PDF** en `outputs/planos_pdf/*.pdf`. Genera `REPORTE_CALIDAD_SIAF.json`. |
| **Referencia (Destripador)** | `agente_referencia/` | `8084` | `POST /destripar` | Extrae, consolida y normaliza la estructura, reglas de negocio y esquema del PMF a partir de `rag_documents/ejemplo/`. Genera la `referencia_siaf/` genérica (nunca filtra datos sensibles del ejemplo). |

### 2.1 Estructura de Datos

| Ruta | Propósito |
|---|---|
| `proyecto_forestal_siaf/` | Único repositorio Git (sin commits aún). Aquí reside todo el código de los agentes. |
| `siaf_users/{CLIENTE}/{PROYECTO}/inputs/` | Insumos del proyecto (CSV de campo, inventario, shapefile + auxiliares). No versionado (gitignored). |
| `siaf_users/{CLIENTE}/{PROYECTO}/outputs/` | Resultados generados (`tablas_nom/`, `planos_pdf/`, `REPORTE_CALIDAD_SIAF.json`). No versionado. |
| `rag_documents/normativa/` | Base de conocimiento normativa (NOM-152 + manuales). Viva y actualizable. Gitignored. |
| `rag_documents/ejemplo/` | PMF completo de ejemplo para enseñar y validar al sistema. Contiene `ejemplo.pdf` y `MEMORIA DE CALCULO_EJIDO TOPIAXLS.xls` (hoja `ER`, donde campo e inventario están embebidos). **Sensible/temporal, nunca debe exponerse fuera de este equipo**. Gitignored. |
| `.env` (`/home/dxmdesktop/siaf/.env`) | Variables locales (claves, URLs de Ollama/Bonsai). No está en el repositorio. |

### 2.2 Stack de IA Local (Soberano)

| Componente | Host/Puerto | Uso |
|---|---|---|
| **Ollama** | `127.0.0.1:11434` | Embeddings (`nomic-embed-text`) y LLMs para QA/RAG (ej. `llama3`). Endpoint OpenAI-compatible `/v1`. |
| **Bonsai-27B-gguf** (llama.cpp) | `0.0.0.0:11435` | Modelo pesado para extracción/consolidación por lotes. Accesible desde contenedores vía `http://host.containers.internal:11435/v1`. Ejecutado con `CUDA_VISIBLE_DEVICES=""` cuando hay restricciones de VRAM (RTX 2070). |
| **ChromaDB** | Persistente (`.rag_index`) | Almacenamiento vectorial del RAG normativo. Se fuerza `collection_name="siaf_normativa"` para evitar colecciones nuevas por proceso. |

## 3. Estado Actual del Proyecto

El proyecto presenta una base muy sólida, con muchas piezas ya desarrolladas y pensadas para operar en un entorno con recursos limitados (CPU).

| Aspecto | Estado | Observación |
|---|---|---|
| **Arquitectura** | Bueno | Separación clara por agentes, alineada con las etapas de un PMF según NOM-152. |
| **Soberanía** | Excelente | Stack 100% local. Sin dependencias a APIs externas. |
| **Robustez de extracción** | Bueno | `agente_referencia` cuenta con fallbacks (`raw_decode`, `aplanar()`, agregación local `unicos`) para lidiar con salidas verbosas o no estrictamente JSON de modelos locales. |
| **RAG Normativo** | Bueno | Indexado incremental por hash, persistencia controlada y enfocado a PDFs de normativa. |
| **Orquestación** | Parcial | Manager existe (CrewAI + BackgroundTasks), pero aún no está cableado para ejecutar el flujo completo con los 3 insumos del usuario final. |
| **Cartografía (GIS)** | **Crítico (Stub)** | `agente_gis` retorna únicamente "ready" sin generar planos. Esto bloquea directamente a Compliance (requiere ≥4 PDFs). |
| **Generación de Tablas NOM** | Incompleto | No está definido explícitamente qué agente transforma los resultados dasométricos al formato de `tablas_nom/*.json` que espera Compliance. |
| **Flujo End-to-End** | Inexistente | Existen piezas aisladas, pero no existe un único flujo automatizado que reciba los 3 archivos y entregue el informe completo. |
| **Ingesta de insumos** | Pendiente | Hoy el proceso es mayormente manual (mover archivos a `inputs/`). Falta un contrato claro, validaciones y un punto de entrada único para el usuario. |

## 4. Lo que Falta por Hacer

Tomando como base el objetivo ("subes 3 archivos y obtienes el informe forestal completo"), se detectan las siguientes brechas:

| Bloque | Prioridad | Descripción | Impacto |
|---|---|---|---|
| **1. Ingesta y validación de insumos** | Alta | Crear un contrato de entrada para los 3 archivos. Definir columnas esperadas (CSV campo/inventario), manejo de `skiprows`, encoding (`latin-1`), forward-fill de `UMM`/`Sitio`, soporte para **archivos combinados** (campo+inventario en un mismo Excel/CSV, como ocurre en el ejemplo) y validación de archivos SHP completos (.shp+.dbf+.prj+.shx), con asociación automática a `{cliente}/{proyecto}`. | **Bloqueante.** No existe el punto de entrada único para el caso de uso deseado. |
| **2. Cálculo de `superficie_ha` desde el Shape** | Alta | Forester exige `superficie_ha`. Debe obtenerse del polígono (área en hectáreas con CRS proyectado correcto) y propagarse consistentemente al resto del flujo. Evitar depender de que el usuario lo proporcione manualmente. | Alto. Afecta cálculos dasométricos y trazabilidad del insumo. |
| **3. Habilitar GIS funcional** | **Crítica** | Desbloquear la generación de planos PDF. Evaluar dos caminos: **A)** PyQGIS headless (Ubuntu 24.04, entorno ARM) o **B)** Alternativa con `geopandas + matplotlib` (más portable, determinista y compatible con el enfoque 100% local). Necesario generar al menos 4 planos para cumplir con el requisito de Compliance. | **Crítico.** Sin ≥4 `planos_pdf/*.pdf`, Compliance no puede aprobar el reporte. |
| **4. Generación de Tablas NOM (.json)** | Alta | Definir y completar la generación de `outputs/tablas_nom/*.json` con la estructura exigida por NOM-152. El responsable más natural es Forester (post-cálculo dasométrico) o una etapa intermedia tras este, antes de llamar a Compliance. | Alto. Compliance depende directamente de estos JSON para generar el reporte de calidad. |
| **5. Cablear orquestación End-to-End** | Alta | Ampliar `agente_manager` para ejecutar la secuencia completa: **Ingesta → Forester (cálculo) → GIS (planos) → Environmental (soporte normativo opcional) → Compliance (auditoría)**. Gestionar errores, estados y devolver un resultado completo al usuario. | Alto. Convierte las piezas aisladas en un flujo único automatizado. |
| **6. UX para carga de archivos** | Media | Implementar un mecanismo práctico para recibir los 3 archivos (endpoint `multipart/form-data` en Manager o corregir/script `alta_proyecto.py`, evitando su hardcode a `/home/ubuntu/...`). Añadir validaciones claras con mensajes útiles al usuario. Considerar también la posibilidad de recibir el inventario en formato `.xls`/`.xlsx` (tal como viene en el ejemplo). | Medio-Alto. Facilita el uso por usuario no técnico. |
| **7. Pruebas E2E con el ejemplo** | Media | Crear/ajustar pruebas del flujo completo usando `rag_documents/ejemplo/` (respetando su carácter sensible: **solo para validación interna**, nunca exponerlo). Tener en cuenta que, en este ejemplo, campo+inventario están embebidos en `MEMORIA DE CALCULO_EJIDO TOPIAXLS.xls` (hoja ER) y que las coordenadas UTM por Sitio se encuentran en el PDF (págs. 82–84), mientras que el shapefile no está presente. Corregir `test_maquila_total.py` (apunta a puerto incorrecto 8080 vs 8085 y payload incompleto). | Medio. Reduce riesgos de regresión y valida la coherencia de tablas, planos y reporte final. |

## 5. Ruta Recomendada para Alcanzar el Objetivo

Se propone seguir un enfoque incremental, priorizando lo que desbloquea la generación del informe completo desde el primer momento.

| Fase | Objetivo | Acciones Clave | Criterio de Éxito |
|---|---|---|---|
| **Fase 1. Definir contrato de insumos** | Alinear esquemas y evitar ambigüedad | Documentar columnas esperadas (campo + inventario), contemplar el **caso combinado** (campo e inventario embebidos, como en hoja `ER` del `.xls`), reglas de normalización (skiprows, encoding, forward-fill UMM/Sitio/VTA), cómo extraer sitios únicos `(UMM,Sitio)` para generar el CSV de campo, cómo obtener coordenadas UTM por sitio desde el PDF si no vienen en el CSV, requisitos del SHP (CRS, atributos mínimos) y cómo derivar `superficie_ha` desde el polígono. Validar contra `rag_documents/ejemplo/`. | Especificación clara + insumos validados y normalizables sin intervención manual. |
| **Fase 2. Implementar ingesta** | Poder recibir los 3 archivos | Añadir soporte `multipart` en Manager (`POST /procesar`) para recibirlos, con capacidad de leer `.xls/.xlsx` (hoja ER). Implementar lógica para **detectar y separar** datos de campo e inventario cuando vengan combinados (extraer `UMM,Sitio` únicos para el archivo de campo y conservar filas arbóreas para el inventario). Validar estructura, copiar a `siaf_users/{CLIENTE}/{PROYECTO}/inputs/` y crear la jerarquía de outputs. Alternativa: corregir `scripts_maquila/alta_proyecto.py` (quitar hardcode de path a `/home/ubuntu/...`). | Al subir los 3 archivos (o sus equivalentes combinados), estos quedan correctamente ubicados en `inputs/`, con validaciones básicas pasando. |
| **Fase 3. Desbloquear GIS (Cartografía)** | Cumplir requisito ≥4 planos PDF | **Recomendación:** Evaluar primero **opción B (geopandas + matplotlib)** por portabilidad, determinismo y menor fricción en Ubuntu 24.04. Si no cubre necesidades cartográficas, migrar a **PyQGIS headless**. Generar plantillas mínimas (localización, zona de trabajo/rodal, base cartográfica y complementarios) y escribir PDFs a `outputs/planos_pdf/`. | Compliance detecta ≥4 PDFs y avanza sin error por este requisito. |
| **Fase 4. Completar Tablas NOM** | Generar JSON requeridos por Compliance | Implementar la transformación de resultados dasométricos al formato de `tablas_nom/*.json` (alineado a lo que espera `agente_compliance`). Sugerido hacerlo posterior a Forester (responsable de cálculo) para mantener cohesión de responsabilidades. | Archivos `.json` generados en `outputs/tablas_nom/` y `REPORTE_CALIDAD_SIAF.json` se crea exitosamente. |
| **Fase 5. Cablear orquestación End-to-End** | Flujo único automatizado | Extender Manager para ejecutar la secuencia completa con manejo de errores, estados por etapa y ejecución en BackgroundTasks. Asegurar paso correcto de parámetros (especialmente `superficie_ha`) entre agentes. | `POST /procesar` con los 3 insumos ejecuta todo el pipeline y retorna/produce el informe completo. |
| **Fase 6. Validación integral con el Ejemplo** | Validación real del PMF | Ejecutar el flujo completo contra `rag_documents/ejemplo/`, respetando estrictamente su carácter sensible (uso exclusivo para validación interna). Tomar como insumos base: `MEMORIA DE CALCULO_EJIDO TOPIAXLS.xls` (hoja `ER`) para obtener campo+inventario (con forward-fill de UMM/Sitio), extraer coordenadas UTM por Sitio desde `ejemplo.pdf` (págs. 82–84) para enriquecer el archivo de campo, y considerar que el shapefile no está presente en el ejemplo (deberá evaluarse su tratamiento o generación para validación). Ajustar con base en `REPORTE_CALIDAD_SIAF.json`, tablas y planos hasta obtener un flujo verde y coherente. | Pipeline validado end-to-end, resultados consistentes y auditables. |

## 6. Notas Prácticas y Consideraciones Técnicas

| Tema | Recomendación |
|---|---|
| **Uso de modelos (CPU)** | Reservar **Bonsai 27B** únicamente para trabajo por lotes (extracción/consolidación en Referencia). Para QA/RAG interactivo (Environmental), usar un modelo ligero de Ollama (ej. `llama3`) para tiempos de respuesta razonables. |
| **Desarrollo local vs Contenedores** | Al ejecutar agentes directamente en el host (sin Docker/Podman), sobreescribir URLs a `127.0.0.1:PORT` (`RAG_LLM_URL`, `REFERENCIA_LLM_URL`, `BONSAI_URL`, `ENV_CONSULTA_URL`). En contenedores usar `http://host.containers.internal:PORT`. |
| **Rutas para RAG (dev)** | En desarrollo local, establecer `RAG_DOCS_DIR=/home/dxmdesktop/siaf/rag_documents/normativa` y `RAG_CHROMA_DIR` a un directorio escribible local (evitar paths de contenedor `/normativa` o `/rag_index`). |
| **ChromaDB** | Mantener `collection_name="siaf_normativa"` fijado. Si aparecen 0 resultados tras reinicio, suele indicar que se creó una colección nueva por proceso. |
| **Bonsai + VRAM (RTX 2070)** | Con GGUF F16 27B puede fallar `cudaMalloc` (GGML_ASSERT). En ese caso forzar CPU con `CUDA_VISIBLE_DEVICES=""` (~0.75 tok/s). Es viable para lotes, no para interactivo. |
| **Tratamiento del Ejemplo** | `agente_referencia` **sanitiza** los artefactos del ejemplo (CSV → solo nombres de columnas/formas/recuentos de nulos, JSON → solo llaves) antes de enviar prompts a Bonsai. El ejemplo crudo permanece únicamente en `rag_documents/ejemplo/` (gitignored) y, tras validación, puede purgarse. **Nunca debe filtrarse o exponerse fuera del equipo.** |
| **Robustez JSON (Bonsai)** | Aprovechar los mecanismos existentes: decodificación por `raw_decode` (captura primer `{` válido ante razonamiento previo), `aplanar()` (desenvuelve strings dict-literal) y fallback de consolidación local. Estos hacen viable el uso con modelos locales. |
| **Evitación de paths frágiles** | Leer scripts antes de ejecutarlos (`scripts_maquila/alta_proyecto.py` tiene paths hardcodeados a producción). Corregir hacia rutas relativas/parametrizables para desarrollo. |
| **Archivos combinados (campo+inventario)** | El ejemplo demuestra que **campo e inventario pueden venir embebidos en un mismo archivo** (`.xls`, hoja `ER`). El pipeline debe ser flexible: aceptar archivos separados (ideal) o **detectar, separar y normalizar** el caso combinado, aplicando *forward-fill* a `UMM`/`Sitio` y extrayendo sitios únicos para reconstruir el archivo de campo. Además, las coordenadas UTM por sitio pueden estar presentes en el PDF y deben poder mapearse por `(UMM, Sitio)`. |

## 7. Conclusión

El proyecto SIAF está **muy bien planteado y con una base técnica sólida**. La arquitectura por agentes, el enfoque 100% soberano y las defensas para trabajar con LLMs locales demuestran que está pensado para un entorno real y con restricciones.

El principal cuello de botella para alcanzar el objetivo de "recibir 3 archivos y generar un informe forestal completo conforme a NOM-152" es **desbloquear GIS (generación funcional de planos)**, seguido de cerrar la **ingesta de insumos**, completar las **tablas NOM** y **cablear la orquestación end-to-end** en el Manager.

**Recomendación estratégica:** Iniciar en **paralelo** las **Fase 1 (Contrato de Insumos)** y **Fase 3 (GIS)**. GIS es el bloque crítico que impide que Compliance complete su validación, mientras que el contrato de insumos permite cablear y estandarizar todo el resto del pipeline de manera ordenada y trazable, contemplando desde ya el **caso real del ejemplo** (campo+inventario embebidos en Excel + coordenadas UTM en PDF).