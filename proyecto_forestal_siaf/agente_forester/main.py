import os
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from pathlib import Path
from pydantic import BaseModel
import requests
import json
from fpdf import FPDF
from scipy import stats

app = FastAPI()

OLLAMA_URL = "http://host.containers.internal:11434/api/generate"

class JobRequest(BaseModel):
    cliente_id: str
    proyecto_id: str
    superficie_ha: float

class R4_PDF(FPDF):
    def header(self):
        self.set_font("helvetica", "B", 12)
        self.cell(0, 8, "PROGRAMA DE MANEJO FORESTAL", ln=True, align="C")
        self.cell(0, 8, "REPORTE ESTADÍSTICO R4 - SIAF SOBERANO", ln=True, align="C")
        self.ln(5)

def get_t_student(n, confidence=0.95):
    if n <= 1: return 2.0 # Valor por defecto si hay pocos datos
    return float(abs(stats.t.ppf((1 - confidence) / 2, n - 1)))

@app.post("/calculate")
async def calculate_metrics(req: JobRequest):
    ruta_base = Path(f"/data/{req.cliente_id}/{req.proyecto_id}")
    csv_path = ruta_base / "inputs/csv_inventarios/inventario_campo.csv"
    
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="Archivo CSV no encontrado")

    try:
        # 1. LECTURA INTELIGENTE
        # Intentamos leer saltando la primera línea "Datos de inventario"
        # y probamos con latin-1 por si viene de Excel con eñes
        try:
            df = pd.read_csv(csv_path, skiprows=1, encoding='latin-1')
        except:
            df = pd.read_csv(csv_path, skiprows=1, encoding='utf-8')

        # Si las columnas no se llaman UMM, intentamos detectar el encabezado real
        if 'UMM' not in df.columns:
             # Fallback: leer de nuevo sin saltar nada por si el archivo no traía el título
             df = pd.read_csv(csv_path, encoding='latin-1')

        # 2. LIMPIEZA DE CELDAS COMBINADAS
        df['UMM'] = pd.to_numeric(df['UMM'], errors='coerce')
        df['Sitio'] = pd.to_numeric(df['Sitio'], errors='coerce')
        df['UMM'] = df['UMM'].ffill()
        df['Sitio'] = df['Sitio'].ffill()
        
        # Convertir datos dasométricos
        df['Dn'] = pd.to_numeric(df['Dn (cm)'], errors='coerce')
        df['At'] = pd.to_numeric(df['At (m)'], errors='coerce')
        df = df.dropna(subset=['Dn', 'At', 'UMM', 'Sitio'])

        # 3. CÁLCULO DE VOLUMEN (VTA)
        df['VTA'] = (df['Dn']**2) * df['At'] * 0.00007854 * 0.5

        # 4. AGRUPACIÓN POR SITIO
        sitios_stats = df.groupby(['UMM', 'Sitio'])['VTA'].sum().reset_index()

        # 5. PREPARAR PDF
        pdf = R4_PDF(orientation="L", unit="mm", format="A4")
        pdf.add_page()
        pdf.set_font("helvetica", "B", 8)
        headers = ["UM", "Sup. ha", "Sitios", "Sup. Muest", "Intens %", "Suma V", "Promedio", "Varianza", "Err Std", "T Student", "Err %"]
        widths = [15, 20, 15, 25, 20, 25, 25, 25, 25, 25, 20]
        
        for i in range(len(headers)):
            pdf.cell(widths[i], 10, headers[i], 1, 0, "C")
        pdf.ln()

        # 6. CÁLCULOS POR UMM
        umm_summary = []
        pdf.set_font("helvetica", "", 8)
        
        for umm_id, group in sitios_stats.groupby('UMM'):
            v = group['VTA']
            n = len(v)
            suma_v = float(v.sum())
            promedio = float(v.mean())
            varianza = float(v.var()) if n > 1 else 0.0
            desv_std = float(v.std()) if n > 1 else 0.0
            err_std = float(desv_std / np.sqrt(n)) if n > 0 else 0.0
            t_val = get_t_student(n)
            err_abs = t_val * err_std
            err_pct = float((err_abs / promedio * 100)) if promedio > 0 else 0.0
            
            row = [str(int(umm_id)), str(req.superficie_ha), str(n), f"{n*0.1:.2f}", 
                   f"{(n*0.1/req.superficie_ha)*100:.4f}", f"{suma_v:.2f}", 
                   f"{promedio:.2f}", f"{varianza:.4f}", f"{err_std:.4f}", 
                   f"{t_val:.4f}", f"{err_pct:.2f}"]
            
            for i in range(len(row)):
                pdf.cell(widths[i], 8, row[i], 1, 0, "C")
            pdf.ln()
            
            umm_summary.append({"umm": int(umm_id), "error": round(err_pct, 2)})

        # Guardar resultados
        out_pdf = ruta_base / "outputs/tablas_nom/R4_Estadistica_Oficial.pdf"
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(out_pdf))

        return {"status": "success", "umm_procesadas": len(umm_summary), "resumen": umm_summary}

    except Exception as e:
        # Si algo falla, lo capturamos para que no de 500 genérico
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8085)
