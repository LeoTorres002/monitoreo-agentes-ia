import requests

def test_maquila():
    print("--- INICIANDO TEST DE INTEGRACIÓN SIAF ---")
    payload = {"cliente_id": "CLIENTE_DURANGO", "proyecto_id": "PREDIO_EL_SALTO"}
    
    # 1. Probar Forester
    print("Enviando a Forester (Dasometría)...")
    r_for = requests.post("http://localhost:8080/calculate", json=payload)
    print(f"Respuesta Forester: {r_for.json()}")

    # 2. Probar GIS
    print("\nEnviando a GIS (Cartografía)...")
    r_gis = requests.post("http://localhost:8081/generate-planos", json=payload)
    print(f"Respuesta GIS: {r_gis.json()}")

    # 3. Probar Compliance
    print("\nEnviando a Compliance (Auditoría)...")
    r_com = requests.post("http://localhost:8083/audit", json=payload)
    print(f"Respuesta Compliance: {r_com.json()}")

if __name__ == "__main__":
    test_maquila()
