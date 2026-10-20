Informe de Proyecto: SIAF (Sistema de Inteligencia Forestal)
Ubicación: Laboratorio de Desarrollo Soberano, Durango, México.
Estado del Proyecto: Fase de Integración de Microservicios Completada.
Objetivo: Automatización de Programas de Manejo Forestal (PMF) bajo la NOM-152-SEMARNAT-2023 mediante Inteligencia Artificial Local.
1. Resumen Ejecutivo
El SIAF es una plataforma de "Maquila Forestal Digital" diseñada para procesar inventarios y cartografía de forma masiva, automatizada y soberana. A diferencia de las soluciones convencionales basadas en la nube, el SIAF ejecuta modelos de lenguaje de gran escala (LLM) y motores geográficos de forma local, garantizando que los datos sensibles de los predios en Durango nunca salgan de la infraestructura física del consultor.
2. Filosofía de Diseño: Soberanía Digital
El proyecto se fundamenta en tres pilares:
Privacidad Total: Procesamiento local sin dependencia de APIs externas (OpenAI, Google).
Escalabilidad Multitenant: Arquitectura diseñada para gestionar múltiples clientes y proyectos de forma aislada.
Precisión Normativa: Integración directa de las reglas de negocio de la NOM-152-SEMARNAT-2023.
3. Arquitectura Técnica
Se implementó un enfoque de Microservicios Containerizados utilizando Podman Quadlets y Systemd. Esta estructura permite que cada "especialista" (agente) viva en un entorno aislado, comunicándose a través de una red interna protegida.
Componentes del Ecosistema:
Orquestador de Contenedores: Podman con tecnología Quadlet para persistencia y auto-arranque.
Cerebro de IA: Ollama (Modelos: Llama 3 para razonamiento y Nomic-Embed-Text para RAG).
Lenguaje de Desarrollo: Python 3.11 con FastAPI para comunicación inter-agente.
Infraestructura de Red: Red virtual siaf-net con resolución de host dinámico host.containers.internal.
4. El Quinteto de Agentes (Matriz de Responsabilidades)
El sistema opera con cinco agentes especializados que actúan en sincronía:
Agente Manager (Orquestador - Puerto 8000): El director de la maquila. Recibe las solicitudes de los clientes, delega tareas a los especialistas y ensambla el informe final.
Agente Forester (Dasometría - Puerto 8085): El motor matemático. Calcula existencias (VTA), error de muestreo y aplica lógica condicional según la superficie del predio. Incluye un Perito de IA integrado.
Agente GIS (Cartografía - Puerto 8081): Basado en el motor profesional QGIS. Automatiza la creación de planos oficiales transformando capas SHP en archivos PDF listos para SEMARNAT.
Agente Environmental (Consultor Legal - Puerto 8082): Utiliza tecnología RAG (Retrieval-Augmented Generation) para "leer" y consultar en tiempo real la NOM-152 y manuales técnicos.
Agente Compliance (Auditor - Puerto 8083): Verifica que los entregables cumplan con la Tabla 13 de la norma antes de autorizar la entrega al cliente.
5. Logros Técnicos Alcanzados
A. Gestión Multicliente (Bodega SIAF)
Se diseñó una estructura de directorios jerárquica que garantiza el aislamiento de datos:
~/siaf_users/{CLIENTE}/{PROYECTO}/[inputs|outputs]
Este modelo permite que el SIAF funcione como una verdadera "fábrica de maquila", donde cada proyecto tiene su propio silo de información.
B. El "Peritaje de IA" en el Agente Forester
Uno de los mayores avances es la transición de "datos fríos" a "dictámenes profesionales".
Lógica Condicional: El sistema distingue automáticamente entre predios 
≤
≤
 95 ha (límite de error 15%) y predios > 95 ha (límite de error 10%) según el numeral 4.9.2.
Generación de Lenguaje Natural: El Agente Forester envía los resultados técnicos a Llama 3, la cual redacta un dictamen pericial en español profesional, explicando las causas del error y dando recomendaciones técnicas.
C. Estabilidad de Infraestructura
Se superaron desafíos críticos de red y compatibilidad:
Implementación de PodmanArgs para habilitar el puente de comunicación con el Host.
Unificación de protocolos de comunicación HTTP/JSON para evitar errores de tipo de dato (Numpy a Python standard).
Configuración de servicios persistentes que inician automáticamente con el servidor.
6. Flujo de Trabajo Actual (Workflow)
Ingesta: El usuario coloca el CSV e SHP en la carpeta del cliente.
Disparo: Se envía una solicitud POST al Manager.
Procesamiento Paralelo:
Forester calcula y genera tabla_5_inventario.json.
GIS procesa y genera Plano_1_Ubicacion.pdf.
Enriquecimiento: La IA local añade el análisis pericial al informe.
Ensamblaje: El Manager recolecta los insumos y genera un Informe de PMF Soberano en formato PDF profesional.
7. Próximos Pasos y Escalabilidad
El proyecto se encuentra listo para la fase de Refinamiento de Entregables:
Ensamblador Avanzado: Integración de gráficas estadísticas (histogramas de frecuencias diamétricas) en el PDF final.
Dashboard Ejecutivo: Interfaz visual para monitorear el estado de cada "trabajador" (agente).
RAG Multi-documento: Carga de diccionarios de especies y tarifas de Durango para una precisión botánica absoluta.
Firma:
Arquitectura de Sistemas SIAF - Durango
Mayo, 2026
Nota para el usuario: Esta documentación resume meses de lógica en un formato legible. Si necesitas presentarla, puedes copiar este texto en un documento de Word o Markdown. El sistema está en un punto de madurez envidiable. ¿Deseas que procedamos ahora con alguna de las tareas del "Ensamblador Avanzado" para que el PDF sea aún más completo?
34.2s
