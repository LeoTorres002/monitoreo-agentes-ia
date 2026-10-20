import os
from fastapi import FastAPI
from pathlib import Path
from pydantic import BaseModel

app = FastAPI(title="SIAF Compliance - Auditor NOM-152")

class AuditRequest(BaseModel):
    cliente_id: str
    proyecto_id: str

@app.post("/audit")
async def perform_audit(req: AuditRequest):
    raiz = Path(os.environ.get("SIAF_DATA_ROOT", "/data"))
    base = next((c for c in (raiz / "siaf_users" / req.cliente_id / req.proyecto_id,
                             raiz / req.cliente_id / req.proyecto_id) if c.exists()),
                raiz / "siaf_users" / req.cliente_id / req.proyecto_id)
    ruta_outputs = base / "outputs"

    # Verificación de Tablas (Numeral 4.10)
    tablas_generadas = list((ruta_outputs / "tablas_nom").glob("*.json"))

    # Verificación de Planos (Numeral 4.19) — considera subcarpetas por plano
    planos_generados = list((ruta_outputs / "planos_pdf").rglob("*.pdf"))

    # Lógica de Auditoría (Tabla 13 de la Norma)
    check_inventario = len(tablas_generadas) > 0
    check_cartografia = len(planos_generados) >= 4 # La norma exige 4 planos base

    status = "APROBADO PARA ENTREGA" if (check_inventario and check_cartografia) else "PENDIENTE / RECHAZADO"

    reporte_calidad = {
        "predio": req.proyecto_id,
        "auditoria_inventario": "OK" if check_inventario else "FALTANTE",
        "auditoria_planos": f"DETECTADOS {len(planos_generados)} de 4" if check_cartografia else "INCOMPLETO",
        "dictamen_final": status
    }

    # Guardar reporte de auditoría
    with open(ruta_outputs / "REPORTE_CALIDAD_SIAF.json", 'w') as f:
        import json
        json.dump(reporte_calidad, f, indent=4)

    return reporte_calidad

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8083)