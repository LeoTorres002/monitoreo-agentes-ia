import os
import hashlib
import json
from pathlib import Path
from fastapi import FastAPI
from langchain_community.document_loaders import PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings
from langchain_openai import ChatOpenAI
from langchain.chains.retrieval_qa.base import RetrievalQA

try:
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("SIAF_ENV_FILE", "/home/dxmdesktop/siaf/.env"))
except Exception:
    pass

app = FastAPI(title="SIAF Environmental - Consultor Normativa")

# Q&A con LLM local. Por defecto Bonsai via llama-server (OpenAI-compatible).
# En maquinas CPU el 27B F16 razona a ~0.75 tok/s -> para respuestas
# interactivas apunta RAG_LLM_URL a un LLM pequeno de Ollama (default en .env:
# http://host.containers.internal:11434/v1 + llama3). Los embeddings SIEMPRE
# quedan en Ollama.
BONSAI_URL = os.environ.get("BONSAI_URL", "http://host.containers.internal:11435/v1")
BONSAI_API_KEY = os.environ.get("BONSAI_API_KEY", "local-dev-key")
LLM_URL = os.environ.get("RAG_LLM_URL", BONSAI_URL)
LLM_MODEL = os.environ.get("RAG_LLM_MODEL", "Bonsai-27B-gguf")
# Bonsai preserva razonamiento antes de la respuesta final: mantener max_tokens
# holgado (2048) para que nunca devuelva content vacio. RAG_MAX_TOKENS permite
# tope menor para pruebas rapidas o despliegues limitados de RAM.
RAG_MAX_TOKENS = int(os.environ.get("RAG_MAX_TOKENS", "2048"))
llm = ChatOpenAI(
    model=LLM_MODEL,
    base_url=LLM_URL,
    api_key=BONSAI_API_KEY,
    temperature=0.0,
    max_tokens=RAG_MAX_TOKENS,
)
embeddings = OllamaEmbeddings(
    model="nomic-embed-text",
    base_url=os.environ.get("OLLAMA_URL", "http://host.containers.internal:11434"),
)

# Directorio de normativa (montado en /normativa en produccion via Quadlet).
# En dev apuntalo a la copia local con RAG_DOCS_DIR.
DOCS_DIR = Path(os.environ.get("RAG_DOCS_DIR", "/normativa"))
CHROMA_DIR = Path(os.environ.get("RAG_CHROMA_DIR", "/rag_index"))
MANIFEST_FILE = CHROMA_DIR / "manifest.json"
EXT = ".pdf"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def purgar_fuente(vs: Chroma, fuente: str):
    """Elimina todos los chunks del vectorstore cuya metadata 'fuente' coincida."""
    try:
        ids = vs.get(where={"fuente": fuente}).get("ids", [])
        if ids:
            vs.delete(ids=ids)
    except Exception as e:
        print(f"[RAG] no se pudo purgar '{fuente}': {e}")


def indexar_directorio():
    """Escaneo recursivo de PDFs bajo DOCS_DIR con reindexado incremental
    por hash: solo se procesa lo nuevo/modificado; lo eliminado se purga."""
    if not DOCS_DIR.exists():
        print(f"[RAG] {DOCS_DIR} no existe (degradando)")
        return None

    manifest = {}
    if MANIFEST_FILE.exists():
        try:
            manifest = json.loads(MANIFEST_FILE.read_text("utf-8"))
        except Exception:
            manifest = {}
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(p for p in DOCS_DIR.rglob(f"*{EXT}") if p.is_file())
    nuevos = {str(p) for p in pdfs}

    # Mover de nombre -> fuente
    por_reindexar = [p for p in pdfs if manifest.get(str(p)) != sha256_file(p)]
    borrados = [name for name, _ in manifest.items() if name not in nuevos]

    if not por_reindexar and not borrados:
        try:
            return Chroma(
                collection_name="siaf_normativa",
                embedding_function=embeddings,
                persist_directory=str(CHROMA_DIR),
            )
        except Exception:
            pass

    splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=120)
    documentos = []
    for pdf in por_reindexar:
        rel = Path(pdf).name
        try:
            loader = PyPDFLoader(str(pdf))
            for chunk in splitter.split_documents(loader.load_and_split()):
                chunk.metadata["fuente"] = rel
                documentos.append(chunk)
        except Exception as e:
            print(f"[RAG] fallo al leer {rel}: {e}")

    vs = Chroma(
        collection_name="siaf_normativa",
        embedding_function=embeddings,
        persist_directory=str(CHROMA_DIR),
    )

    # Reindexado de un archivo ya indexado: purgar instancias viejas antes de agregar
    for p in por_reindexar:
        purgar_fuente(vs, Path(p).name)
    for name in borrados:
        try:
            purgar_fuente(vs, Path(name).name)
        except Exception:
            pass

    if documentos:
        vs.add_documents(documentos)
        try:
            vs.persist()
        except Exception:
            pass

    for p in por_reindexar:
        manifest[str(p)] = sha256_file(p)
    for name in borrados:
        manifest.pop(name, None)
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
    return vs


vectorstore = indexar_directorio()


def construir_rag():
    if vectorstore is None:
        return None
    return RetrievalQA.from_chain_type(
        llm, retriever=vectorstore.as_retriever(search_kwargs={"k": 6}),
        return_source_documents=True,
    )


rag_chain = construir_rag()


@app.get("/consultar")
async def consultar(pregunta: str):
    if not rag_chain:
        return {"error": f"No hay documentos en {DOCS_DIR}. Revisa RAG_DOCS_DIR / montura."}
    try:
        resp = rag_chain.invoke({"query": pregunta})
    except Exception as e:
        return {"error": f"Fallo al generar con Bonsai: {e}"}
    fuente = [d.metadata.get("fuente", "?") for d in resp.get("source_documents", [])]
    return {"respuesta": resp["result"], "fuentes": sorted(set(fuente))}


@app.get("/indexado")
async def indexado():
    if not DOCS_DIR.exists():
        return {"error": f"{DOCS_DIR} no existe"}
    return {
        "directorio": str(DOCS_DIR),
        "documentos": [str(p) for p in DOCS_DIR.rglob(f"*{EXT}")],
        "vectorstore": str(CHROMA_DIR),
        "manifest": len(MANIFEST_FILE.read_text("utf-8").splitlines()) if MANIFEST_FILE.exists() else 0,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)