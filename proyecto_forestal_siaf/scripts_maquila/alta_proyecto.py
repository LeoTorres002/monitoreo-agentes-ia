import os
from pathlib import Path

def preparar_maquila(cliente_id, predio_id):
    base_path = Path(f"/home/ubuntu/siaf/siaf_users/{cliente_id}/{predio_id}")
    folders = [
        base_path / "inputs/csv_inventarios",
        base_path / "inputs/cartografia_shp",
        base_path / "outputs/tablas_nom",
        base_path / "outputs/planos_pdf",
        base_path / "outputs/informe_final"
    ]
    
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    
    # Validación fail-fast
    inv = list((base_path / "inputs/csv_inventarios").glob("*.csv"))
    if not inv:
        print(f"❌ Error: Falta inventario CSV en {cliente_id}/{predio_id}")
        return False
    
    print(f"✅ Proyecto {predio_id} de {cliente_id} listo para procesar.")
    return True

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2:
        preparar_maquila(sys.argv[1], sys.argv[2])
