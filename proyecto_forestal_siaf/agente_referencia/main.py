import json
import os
import re
from pathlib import Path

import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException
from langchain_openai import ChatOpenAI
from langchain_community.document_loaders import PyPDFLoader
from pydantic import BaseModel
import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("SIAF_ENV_FILE", "/home/dxmdesktop/siaf/.env"))
except Exception:
    pass

app = FastAPI(title="SIAF Referencia - Destripador de Proyectos Maestros")

BONSAI_URL = os.environ.get("BONSAI_URL", "http://host.containers.internal:11435/v1")
BONSAI_API_KEY = os.environ.get("BONSAI_API_KEY", "local-dev-key")
# LLM del destripador. Default: Bonsai (calidad, lote). En CPU el 27B F16 es
# ~0.6-0.75 tok/s -> una corrida completa toma horas. Para validar el pipeline
# rapido apunta REFERENCIA_LLM_URL al Ollama OpenAI-compat:
#   REFERENCIA_LLM_URL=http://host.containers.internal:11434/v1 REFERENCIA_LLM_MODEL=llama3
REFERENCIA_LLM_URL = os.environ.get("REFERENCIA_LLM_URL", BONSAI_URL)
REFERENCIA_LLM_MODEL = os.environ.get("REFERENCIA_LLM_MODEL", "Bonsai-27B-gguf")
# Bonsai 27B razona verbosamente antes del JSON: hace falta un presupuesto
# grande para que el objeto cierre despues del razonamiento (ctx 8192 aprueba
# hasta ~3700 tokens de salida con secciones de 16k chars).
LLM_MAX_TOKENS = int(os.environ.get("REFERENCIA_MAX_TOKENS", "6400"))
llm = ChatOpenAI(model=REFERENCIA_LLM_MODEL, base_url=REFERENCIA_LLM_URL, api_key=BONSAI_API_KEY,
                 temperature=0.0, max_tokens=LLM_MAX_TOKENS)

# Salida de la referencia destilada (generica, versionar en el repo).
REFERENCIA_DIR = Path(os.environ.get(
    "REFERENCIA_DIR", "/home/dxmdesktop/siaf/proyecto_forestal_siaf/referencia_siaf"))
# Ruta del entorno para validar reglas cruzandolas con la NOM (agente environmental).
ENV_CONSULTA_URL = os.environ.get("ENV_CONSULTA_URL", "http://localhost:8082/consultar")

ARTEFACTOS = {".pdf", ".csv", ".json", ".md", ".txt", ".xls", ".xlsx"}
# Limite por llamada a Bonsai: con ctx 8192 cada seccion de ~16000 chars
# (~4200 tokens) deja margen para la respuesta generada.
SECTION_CHARS = 16000
# Tope de secciones por artefacto (0 = todas). Util en hardware lento: con
# SECCIONES_MAX>0 se procesa la primera seccion y la mas rica en dominio
# (VTA/error/muestreo/inventario), suficiente para validar el pipeline.
SECCIONES_MAX = int(os.environ.get("SECCIONES_MAX", "0"))
DOMINIO = "VTA error muestreo inventario volumen UMM Sitio fórmula intensidad".split()


class DestriparRequest(BaseModel):
    cliente_id: str = ""
    proyecto_id: str = ""
    doc_dir: str = ""


# Modos de corrida por fases (sobre CPU el 27B es lento): con EXTRAER_ONLY=1 el
# endpoint guarda agg_extraccion.json y sale; con CONSOLIDAR_ONLY=1 lee esa
# guarda y solo consolida/valida/escribe (para correr Bonsai solo en la 2a fase).
AGG_PERSIST = Path(os.environ.get(
    "AGG_PERSIST", "/home/dxmdesktop/siaf/proyecto_forestal_siaf/referencia_siaf/agg_extraccion.json"))
EXTRAER_ONLY = os.environ.get("EXTRAER_ONLY", "0") == "1"
CONSOLIDAR_ONLY = os.environ.get("CONSOLIDAR_ONLY", "0") == "1"


def completar_dir(req: DestriparRequest) -> Path:
    if req.doc_dir:
        path = Path(req.doc_dir)
    elif req.cliente_id and req.proyecto_id:
        path = Path("/data") / req.cliente_id / req.proyecto_id
    else:
        raise HTTPException(status_code=400, detail="Se necesita cliente_id+proyecto_id o doc_dir")
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No existe {path}")
    return path


def extraer_texto(path: Path) -> str:
    """Devuelve contexto textual de un artefacto sin exponer datos sensibles:
    PDF -> texto; CSV -> columnas/formas/valores degenerados; JSON -> estructura."""
    suf = path.suffix.lower()
    if suf == ".pdf":
        try:
            docs = PyPDFLoader(str(path)).load_and_split()
            return "\n\n".join(d.page_content for d in docs[:80])
        except Exception as e:
            return f"[error leyendo PDF: {e}]"
    if suf == ".csv":
        for enc in ("latin-1", "utf-8"):
            try:
                df = pd.read_csv(path, encoding=enc, skiprows=1)
                if df.empty or df.columns.size == 0 or "Unnamed" in str(df.columns[0]):
                    df = pd.read_csv(path, encoding=enc)
                cols = ", ".join(map(str, df.columns))
                muestra = df.head(1).to_dict(orient="records")
                nulas = {str(c): int(df[c].isna().sum()) for c in df.columns}
                return (f"CSV: columnas=[{cols}] filas={len(df)} "
                        f"nulos={json.dumps(nulas)} primera_fila={json.dumps(muestra)}")
            except Exception:
                continue
        return "[error leyendo CSV]"
    if suf == ".json":
        try:
            data = json.loads(path.read_text("utf-8"))
            return "JSON keys=" + json.dumps(list(data.keys()) if isinstance(data, dict) else data[:20], ensure_ascii=False)
        except Exception:
            try:
                return "JSON(parcial): " + path.read_text("utf-8")[:4000]
            except Exception:
                return "[error leyendo JSON]"
    if suf == ".xls" or suf == ".xlsx":
        try:
            engine = "xlrd" if suf == ".xls" else "openpyxl"
            hojas = pd.read_excel(path, sheet_name=None, header=None, engine=engine, nrows=4)
            resumen = []
            for nombre, df in hojas.items():
                filas = [" | ".join(str(c) for c in df.iloc[i].tolist() if str(c) != "nan")[:120]
                         for i in range(df.shape[0])]
                resumen.append(f"hoja='{nombre}' primeras_filas=" + " // ".join(filas))
            return "XLS hojas=" + " ; ".join(resumen)
        except Exception as e:
            return f"[error leyendo XLS: {e}]"
    try:
        return path.read_text("utf-8", errors="replace")[:8000]
    except Exception as e:
        return f"[error leyendo archivo: {e}]"


PROMPT_EXTRACCION = """Analiza el artefacto siguiente de un Programa de Manejo Forestal (PMF) terminado de ejemplo.
Tu tarea es EXTRAER PENDIENTES GENÉRICOS, NO repetir datos del predio: nada de números de inventario,
especies, volúmenes ni coordenadas reales. Solo estructura, reglas, campos y vocabulario.

Artefacto: {nombre}
Tipo detectado: {tipo}
Contenido (ya sanitizado):
{contenido}

Normas a aplicar: NOM-152-SEMARNAT-2023.

Devuelve SOLO JSON (sin markdown) con esta forma:
{{
  "clasificacion": "recurso de entrada | entregable de salida | formato/plantilla | regla de negocio | glosario",
  "esquema": ["carpeta o archivo esperado en inputs/outputs de un PMF", ...],
  "checklist": ["obligación verificable del PMF, citando numeral NOM-152 si aplica", ...],
  "reglas_negocio": ["regla calculable o condicional, e.g. límite de error 15% si <=95 ha", ...],
  "plantillas": ["descripción de plantilla reutilizable a generar", ...],
  "glosario": ["término: definición", ...]
}}
Si el contenido está vacío o es ilegible, devuelve {{"clasificacion":"ilegible","esquema":[],"checklist":[],"reglas_negocio":[],"plantillas":[],"glosario":[]}}."""


def pedir_bonsai(prompt: str) -> dict:
    try:
        resp = llm.invoke(prompt)
        texto = resp.content if hasattr(resp, "content") else str(resp)
        # Bonsai preserva razonamiento: la respuesta final puede llegar en
        # reasoning_content; si content vino vacío, usamos ese razonamiento.
        if not texto and hasattr(resp, "additional_kwargs"):
            texto = resp.additional_kwargs.get("reasoning_content", "") or ""
        if not texto and resp and getattr(resp, "response_metadata", {}):
            texto = resp.response_metadata.get("reasoning_content", "") or ""
        texto = re.sub(r"```json|```", "", texto)
        # Bonsai mezcla razonamiento con JSON e incluso repite objetos: parseamos
        # con raw_decode el primer objeto JSON completo desde el primer "{".
        inicio = texto.find("{")
        if inicio >= 0:
            dec = json.JSONDecoder()
            try:
                obj, _ = dec.raw_decode(texto[inicio:])
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
        return {}
    except Exception as e:
        return {"error": str(e), "crudo": texto if 'texto' in dir() else "sin respuesta"}


def acumular(agg: dict, extraido: dict):
    for clave in ("esquema", "checklist", "reglas_negocio", "plantillas", "glosario"):
        items = extraido.get(clave)
        if isinstance(items, list):
            agg[clave].extend(str(x) for x in items if x)
    if extraido.get("clasificacion") and extraido["clasificacion"] not in agg["clasificaciones"]:
        agg["clasificaciones"].append(extraido["clasificacion"])
    if extraido.get("error"):
        agg["errores"].append(extraido["error"])


def aplanar(x) -> str:
    """Reduce un item a string plano: si viene {clave: valor} se queda con el
    valor de la clave de contenido (regla/obligacion/termino/item/norma)."""
    if isinstance(x, str):
        # Algunos LLM emiten un dict-literal como string p.ej. "{'regla': 'x'}"
        try:
            import ast
            xy = ast.literal_eval(x)
            if isinstance(xy, dict):
                return aplanar(xy)
        except Exception:
            pass
    if isinstance(x, dict):
        for k in ("regla", "obligacion", "obligación", "termino", "término", "definición", "item", "norma", "nombre"):
            if k in x and x[k]:
                return str(x[k])
        for v in x.values():
            if v:
                return str(v)
    return str(x)


def unicos(items: list) -> list:
    """Achata y deduplica items conservando el primer orden."""
    vistos = set()
    salida = []
    for x in items:
        plano = aplanar(x)
        clave = re.sub(r"\s+", " ", plano).strip().lower()
        if clave and clave not in vistos:
            vistos.add(clave)
            salida.append(plano)
    return salida


def consolidar(agg: dict) -> dict:
    """Consolida extracciones en referencia generica. El prompt de Bonsai cabe en
    ctx 8192, asi que con muchas secciones se hace en bloques (< 24k chars) que
    luego se fusionan por dedupe."""
    claves = ("esquema_proyecto", "checklist", "reglas_negocio", "plantillas", "glosario")
    base = {k: [] for k in claves}
    bloques = _bloques_consolidacion(agg)
    for i, bloque in enumerate(bloques, start=1):
        print(f"[consolidar] bloque {i}/{len(bloques)} -> fusionando", flush=True)
        datos = pedir_bonsai(
            f"""Consolida estas extracciones de un proyecto PMF de ejemplo en una única referencia genérica
(colapsa duplicados, ordena, generaliza; nada de datos de un predio concreto).
Devuelve SOLO JSON con CLAVES EXACTAS: esquema_proyecto, checklist, reglas_negocio, plantillas, glosario.
Todos los valores deben ser listas de strings planos, sin objetos anidados.
{json.dumps(bloque, ensure_ascii=False, indent=2)}""")
        for k in claves:
            base[k].extend(unicos(datos.get(k, [])))
    for k in claves:
        base[k] = unicos(base[k])
    # Plan B: si el LLM no devolvio JSON util en ningun bloque, agregamos local.
    if not any(base.values()):
        print("[consolidar] LLM no devolvio JSON valido -> agregacion local", flush=True)
        base["esquema_proyecto"] = unicos(agg.get("esquema", []))
        base["checklist"] = unicos(agg.get("checklist", []))
        base["reglas_negocio"] = unicos(agg.get("reglas_negocio", []))
        base["plantillas"] = unicos(agg.get("plantillas", []))
        base["glosario"] = unicos(agg.get("glosario", []))
    return base


def _bloques_consolidacion(agg: dict) -> list:
    """Trocea los items extraidos en bloques que serializados queden <= LIMITE_CHARS
    y no rebasen el ctx de Bonsai. Cada item se etiqueta con su lista de origen."""
    LIMITE_CHARS = 24000
    claves = ("esquema", "checklist", "reglas_negocio", "plantillas", "glosario")
    items = [({clave: it}, clave, it)
             for clave in claves for it in agg.get(clave, [])]
    bloques, actual = [], []
    for etiquetado, clave, it in items:
        candidato = actual + [etiquetado]
        if len(json.dumps({"bloque": candidato}, ensure_ascii=False)) > LIMITE_CHARS and actual:
            bloques.append({"bloque": actual})
            actual = [etiquetado]
        else:
            actual = candidato
    if actual:
        bloques.append({"bloque": actual})
    return bloques or [{"bloque": []}]


def validar_con_nom(reglas: list) -> list:
    """Cruce automatico de cada regla contra el agente environmental (RAG NOM-152+Bonsai).
    Clasifica: confirmado / sin_evidencia / manual (ambiguo o respuesta sospechosa)."""
    resultado = []
    for regla in reglas:
        estado = "sin_evidencia"
        evidencia = ""
        try:
            r = requests.get(ENV_CONSULTA_URL, params={"pregunta": f"¿Confirma la NOM-152 que {regla}?"}, timeout=90)
            if r.status_code == 200:
                data = r.json()
                evidencia = str(data.get("respuesta", ""))[:300]
                marca = {"no", "incorrecto", "falso", "no es correcto", "sin fundamento"}
                si = {"sí", "si", "correcto", "corresponde", "confirma"}
                if any(m in evidencia.lower() for m in marca):
                    estado = "manual"
                elif any(m in evidencia.lower() for m in si):
                    estado = "confirmado"
        except Exception as e:
            evidencia = f"<no se pudo consultar NOM: {e}>"
            estado = "sin_evidencia"
        resultado.append({"regla": regla, "estado": estado, "evidencia": evidencia})
    return resultado


def escribir_referencia(base: dict, validado: list):
    REFERENCIA_DIR.mkdir(parents=True, exist_ok=True)
    (REFERENCIA_DIR / "plantillas").mkdir(exist_ok=True)

    (REFERENCIA_DIR / "esquema_proyecto.json").write_text(
        json.dumps({"carpetas": ["inputs/csv_inventarios", "inputs/cartografia_shp",
                                 "outputs/tablas_nom", "outputs/planos_pdf", "outputs/informe_final"],
                    "artefactos": base.get("esquema_proyecto", [])}, ensure_ascii=False, indent=2), "utf-8")

    (REFERENCIA_DIR / "reglas_negocio.yaml").write_text(
        yaml.safe_dump({"reglas": base.get("reglas_negocio", []),
                        "validacion_nom": validado}, allow_unicode=True, sort_keys=False), "utf-8")

    (REFERENCIA_DIR / "checklist_pmf.yaml").write_text(
        yaml.safe_dump({"checklist": base.get("checklist", [])}, allow_unicode=True, sort_keys=False), "utf-8")

    glosario = base.get("glosario", [])
    (REFERENCIA_DIR / "glosario_pmf.md").write_text(
        "# Glosario PMF (SIAF)\n\n" + "\n".join(f"- {g}" for g in glosario) + "\n", "utf-8")

    plantillas = base.get("plantillas", [])
    (REFERENCIA_DIR / "plantillas/zonas.md").write_text(
        "# Plantillas reutilizables\n\n" + "\n".join(f"## {p}" for p in plantillas) + "\n", "utf-8")

    informe = {"predio": "GENERICO", "origen": "destripado desde ejemplo (datos no copiados)"}
    (REFERENCIA_DIR / "informe_final_generico.md").write_text(
        "# Informe PMF Soberano\n\nPlantilla base generada por el destripador.\n", "utf-8")

    (REFERENCIA_DIR / "validacion.json").write_text(
        json.dumps({"total_reglas": len(validado),
                    "por_estado": {e: sum(1 for v in validado if v["estado"] == e) for e in ("confirmado", "sin_evidencia", "manual")},
                    "detalle": validado}, ensure_ascii=False, indent=2), "utf-8")


@app.post("/destripar")
async def destripar(req: DestriparRequest):
    agg = {"esquema": [], "checklist": [], "reglas_negocio": [],
           "plantillas": [], "glosario": [], "clasificaciones": [], "errores": []}

    if CONSOLIDAR_ONLY:
        if not AGG_PERSIST.exists():
            raise HTTPException(status_code=404, detail=f"No hay {AGG_PERSIST}")
        agg = json.loads(AGG_PERSIST.read_text("utf-8"))
        print(f"[destripar] fase 2: leidos {sum(len(v) for v in agg.values())} registros de {AGG_PERSIST}", flush=True)
    else:
        path = completar_dir(req)
        artefactos = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in ARTEFACTOS]
        for p in artefactos:
            contenido = extraer_texto(p)
            tipo = p.suffix.lower().lstrip(".")
            # Particionar documentos grandes: Bonsai cabe en ctx 8192 (~4200 tokens).
            secciones = [contenido[i:i + SECTION_CHARS]
                         for i in range(0, len(contenido), SECTION_CHARS)] or [contenido]
            if SECCIONES_MAX > 0 and len(secciones) > 2:
                # Seccion 1 + la de mayor vocabulario de dominio (indice = num seccion)
                mejor = max(range(1, len(secciones)), key=lambda i:
                            sum(secciones[i].count(p) for p in DOMINIO)) if len(secciones) > 2 else 1
                secciones = [secciones[0], secciones[mejor]]
                print(f"[destripar] tope: {len(secciones)} secciones ({p.name})")
            for num, sec in enumerate(secciones, start=1):
                nombre_sec = f"{p.name} (seccion {num}/{len(secciones)})"
                print(f"[destripar] extrayendo {nombre_sec} chars={len(sec)} ...", flush=True)
                extraido = pedir_bonsai(PROMPT_EXTRACCION.format(nombre=nombre_sec, tipo=tipo, contenido=sec))
                acumular(agg, extraido)
                print(f"[destripar] listo {nombre_sec}: {extraido.get('clasificacion', extraido.get('error','?'))}", flush=True)
        if EXTRAER_ONLY:
            AGG_PERSIST.parent.mkdir(parents=True, exist_ok=True)
            AGG_PERSIST.write_text(json.dumps(agg, ensure_ascii=False, indent=2), "utf-8")
            print(f"[destripar] fase 1: guardado {AGG_PERSIST}", flush=True)
            return {"fase": "extraccion", "artefactos_procesados": len(artefactos),
                    "registros": sum(len(v) for v in agg.values()),
                    "clasificaciones": agg["clasificaciones"], "errores": agg["errores"],
                    "agg_persist": str(AGG_PERSIST)}

    base = consolidar(agg)
    reglas = unicos(base.get("reglas_negocio", []))
    print(f"[destripar] consolidado: {sum(len(v) for v in base.values())} registros, {len(reglas)} reglas", flush=True)
    validado = validar_con_nom(reglas)
    print(f"[destripar] validacion NOM: {len(validado)} reglas", flush=True)
    escribir_referencia(base, validado)

    return {
        "artefactos_procesados": len(artefactos),
        "clasificaciones": agg["clasificaciones"],
        "errores_extraccion": agg["errores"],
        "referencia": str(REFERENCIA_DIR),
        "reglas_extraidas": len(base.get("reglas_negocio", [])),
        "reglas_pendientes_revision": [v["regla"] for v in validado if v["estado"] == "manual"],
        "validacion_nom": validado,
    }


@app.get("/referencia")
async def referencia():
    if not REFERENCIA_DIR.exists():
        raise HTTPException(status_code=404, detail=f"No hay referencia en {REFERENCIA_DIR}")
    return {"referencia": str(REFERENCIA_DIR),
            "archivos": sorted(str(p.relative_to(REFERENCIA_DIR)) for p in REFERENCIA_DIR.rglob("*") if p.is_file())}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8084)