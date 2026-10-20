from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from crewai import Agent, Task, Crew
from langchain_ollama import ChatOllama
from pathlib import Path

app = FastAPI()
llm = ChatOllama(model="llama3", base_url="http://host.containers.internal:11434")

class SolicitudPMF(BaseModel):
    cliente_id: str
    proyecto_id: str

def proceso_pmf(datos: SolicitudPMF):
    ruta_base = Path(f"/data/{datos.cliente_id}/{datos.proyecto_id}")
    
    manager = Agent(
        role='Director de Maquila Forestal',
        goal=f'Coordinar PMF para {datos.proyecto_id}',
        backstory="Experto en NOM-152 y gestión multicliente.",
        llm=llm
    )

    task = Task(
        description=f"Procesar inventario en {ruta_base}/inputs y generar outputs en {ruta_base}/outputs.",
        expected_output="Resumen de ejecución y dictamen técnico.",
        agent=manager
    )

    return Crew(agents=[manager], tasks=[task]).kickoff()

@app.post("/procesar")
async def iniciar(solicitud: SolicitudPMF, bt: BackgroundTasks):
    bt.add_task(proceso_pmf, solicitud)
    return {"status": "En cola", "cliente": solicitud.cliente_id}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)