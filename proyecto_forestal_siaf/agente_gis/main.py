# -*- coding: utf-8 -*-
"""
Agente GIS de SIAF (puerto 8081).

Genera los planos cartográficos (PDF), los proyectos QGIS (.qgz) y la tabla
de atributos del shape de trabajo, trabajando siempre en UTM (norte arriba)
con fondo satelital y modelo digital de elevación.

Endpoints
---------
GET  /                  -> estado del servicio
POST /generate-planos   -> genera todos los productos cartográficos
"""

import math
import os
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

# Librerías de QGIS en ARM / Ubuntu
sys.path.append("/usr/lib/python3/dist-packages")

try:
    from qgis.core import QgsApplication
    print("OK PyQGIS disponible")
except ImportError as e:  # pragma: no cover
    QgsApplication = None
    print("PyQGIS no disponible: %s" % e)

import cartografia as cart  # noqa: E402

app = FastAPI(title="SIAF GIS Agent", version="2.0")

# Inicialización headless de QGIS (para escribir proyectos .qgz)
qgs_app = None
if QgsApplication is not None:
    try:
        qgs_app = QgsApplication([], False)
        qgs_app.initQgis()
        print("QgsApplication inicializado (headless)")
    except Exception as e:  # pragma: no cover
        qgs_app = None
        print("No fue posible inicializar QgsApplication: %s" % e)


class GISJob(BaseModel):
    cliente_id: str
    proyecto_id: str
    shape: Optional[str] = None          # p.ej. "predio.shp"
    sitios: Optional[int] = 20           # sitios sintéticos (sólo presentación)
    qgis: Optional[bool] = True          # escribir proyecto QGIS por plano
    # Metadatos explícitos (futuro CSV/BD): p. ej. {"titulo_lienzo": "...",
    # "municipio": "...", "colindancia_n": "..."}. Tienen prioridad sobre la
    # toponimia derivada del mapa.
    metadatos: Optional[Dict[str, str]] = None


# --------------------------------------------------------------------------- #
# Utilidades locales
# --------------------------------------------------------------------------- #

def _base_proyecto(cliente_id: str, proyecto_id: str) -> Path:
    """Resuelve la carpeta del proyecto soportando ambos montajes:
    - <SIAF_DATA_ROOT>/siaf_users/<cliente>/<proyecto>  (desarrollo en host)
    - <SIAF_DATA_ROOT>/<cliente>/<proyecto>             (contenedor, /data)
    """
    raiz = Path(os.environ.get("SIAF_DATA_ROOT", "/data"))
    candidatos = [raiz / "siaf_users" / cliente_id / proyecto_id,
                  raiz / cliente_id / proyecto_id]
    for c in candidatos:
        if (c / "inputs").exists() or (c / "outputs").exists():
            return c
    return candidatos[0]


def _elegir_shape(shape_dir: Path, nombre: Optional[str]) -> Path:
    if nombre:
        ruta = shape_dir / nombre
        if ruta.exists():
            return ruta
        raise HTTPException(status_code=404, detail="Shape no encontrado: %s" % ruta)
    candidatos = sorted(shape_dir.glob("*.shp"))
    if not candidatos:
        raise HTTPException(status_code=404, detail="No hay archivos .shp en %s" % shape_dir)
    preferidos = ("area", "predio", "zona", "poligono", "polígono", "rodal", "um")
    for c in candidatos:
        if c.stem.lower().startswith(preferidos):
            return c
    return candidatos[0]


def _union(geoms: List[object]):
    if not geoms:
        return None
    u = geoms[0].Clone()
    for g in geoms[1:]:
        if g is None:
            continue
        try:
            u = u.Union(g)
        except Exception:
            continue
    return u


def _puntos_en_poligono(geom, num: int) -> List[Tuple[float, float]]:
    """Sitios de muestreo sintéticos con distribución sistemática (rejilla con
    jitter) dentro del polígono. Se reemplazan por el CSV real de sitios cuando
    exista; mientras tanto son reproducibles (semilla fija)."""
    pts: List[Tuple[float, float]] = []
    if geom is None or num <= 0:
        return pts
    rnd = random.Random(20260101)
    env = geom.GetEnvelope()
    minx, maxx, miny, maxy = env[0], env[1], env[2], env[3]
    dx, dy = maxx - minx, maxy - miny
    # Rejilla sistemática con jitter.
    n = int(math.ceil(math.sqrt(float(num))))
    paso = float(n) + 1.0
    for i in range(n + 2):
        for j in range(n + 2):
            if len(pts) >= num:
                break
            rx = minx + dx * (i + 0.5 + rnd.uniform(-0.30, 0.30)) / paso
            ry = miny + dy * (j + 0.5 + rnd.uniform(-0.30, 0.30)) / paso
            pt = cart.ogr.Geometry(cart.ogr.wkbPoint)
            pt.AddPoint(rx, ry)
            if geom.Contains(pt):
                pts.append((rx, ry))
    # Relleno aleatorio si la rejilla no alcanzó el número pedido.
    intentos = 0
    max_intentos = max(num * 5000, 10000)
    while len(pts) < num and intentos < max_intentos:
        rx = rnd.uniform(minx, maxx)
        ry = rnd.uniform(miny, maxy)
        pt = cart.ogr.Geometry(cart.ogr.wkbPoint)
        pt.AddPoint(rx, ry)
        if geom.Contains(pt):
            pts.append((rx, ry))
        intentos += 1
    return pts


def _zona_y_hemisferio(utm_epsg: int) -> Tuple[int, bool]:
    if utm_epsg >= 32700:
        return utm_epsg - 32700, False
    return utm_epsg - 32600, True


@app.get("/")
async def root():
    return {"status": "GIS Agent Online", "version": "2.0"}


@app.post("/generate-planos")
async def generate_planos(req: GISJob):
    ruta_base = _base_proyecto(req.cliente_id, req.proyecto_id)
    shape_dir = ruta_base / "inputs" / "shape"
    output_dir = ruta_base / "outputs" / "planos_pdf"
    cache_dir = output_dir / "_cache"
    datos_dir = output_dir / "_datos"
    output_dir.mkdir(parents=True, exist_ok=True)

    shape_path = _elegir_shape(shape_dir, req.shape)

    try:
        ds = cart.ogr.Open(str(shape_path))
        if ds is None:
            raise HTTPException(status_code=500, detail="No se pudo abrir: %s" % shape_path)
        lyr = ds.GetLayer()
        if lyr.GetFeatureCount() == 0:
            raise HTTPException(status_code=400, detail="El shapefile no tiene geometrías")

        src_srs = lyr.GetSpatialRef()
        if src_srs is None:
            src_srs = cart.srs_desde_epsg(4326)

        defn = lyr.GetLayerDefn()
        campos = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]

        geoms_src: List[object] = []
        registros: List[Dict] = []
        for feat in lyr:
            g = feat.GetGeometryRef()
            if g is None:
                continue
            geoms_src.append(g.Clone())
            fila = {c: feat.GetField(c) for c in campos}
            registros.append(fila)

        if not geoms_src:
            raise HTTPException(status_code=400, detail="No hay geometrías válidas en el shapefile")

        # BBox en WGS84 para tiles, y elección de zona UTM
        geoms_wgs = [cart.geom_a_epsg(g, src_srs, 4326) for g in geoms_src]
        bbox_wgs = cart.bbox_de_geoms(geoms_wgs)
        union_wgs = _union(geoms_wgs)
        if union_wgs is not None:
            c = union_wgs.Centroid()
            utm_epsg = cart.get_utm_epsg(c.GetX(), c.GetY())
        else:
            utm_epsg = 32613

        geoms_utm = [cart.geom_a_epsg(g, src_srs, utm_epsg) for g in geoms_src]
        union_utm = _union(geoms_utm)
        superficie_ha = (union_utm.GetArea() / 10000.0) if union_utm is not None else 0.0

        # Atributos + área por registro y lista de polígonos para dibujar
        polys: List[Dict] = []
        for idx, (gt, fila) in enumerate(zip(geoms_utm, registros)):
            area_reg = (gt.GetArea() / 10000.0) if gt is not None else 0.0
            fila["AREA_HA"] = round(area_reg, 4)
            fila["ID_SIG"] = idx + 1
            for poly in cart.poligonos_de_geometria(gt):
                poly["attrs"] = fila
                poly["area_ha"] = area_reg
                polys.append(poly)

        bbox_utm = cart.bbox_de_geoms(geoms_utm)
        if bbox_utm is None:
            raise HTTPException(status_code=400, detail="No se pudo calcular el extent en UTM")

        ancho_m = bbox_utm[2] - bbox_utm[0]
        alto_m = bbox_utm[3] - bbox_utm[1]
        pad = max(ancho_m * 0.08, alto_m * 0.08, 20.0)
        xlim = (bbox_utm[0] - pad, bbox_utm[2] + pad)
        ylim = (bbox_utm[1] - pad, bbox_utm[3] + pad)
        ancho_plot = xlim[1] - xlim[0]
        alto_plot = ylim[1] - ylim[0]
        out_bounds = [xlim[0], ylim[0], xlim[1], ylim[1]]  # orden GDAL
        # imshow de matplotlib usa (left, right, bottom, top).
        img_extent = (xlim[0], xlim[1], ylim[0], ylim[1])

        ancho_px = cart.ANCHO_RASTER_PX
        alto_px = max(int(ancho_px * alto_plot / ancho_plot), 200)
        out_size = (ancho_px, alto_px)

        # Fondos (satélite + MDE)
        sat = cart.construir_satelite(bbox_wgs, utm_epsg, out_bounds, out_size, cache_dir)
        dem = cart.construir_dem(bbox_wgs, utm_epsg, out_bounds, out_size, cache_dir)
        if sat is None:
            sat = np.full((alto_px, ancho_px, 3), 236, dtype=np.uint8)
        else:
            # Realce suave de brillo/contraste: la foto satelital debe leerse
            # por debajo del hillshade y de las geometrías semitransparentes.
            sat = np.clip(sat.astype(np.float32) * 1.12 + 8.0, 0, 255).astype(np.uint8)
        if dem is not None:
            px = ancho_plot / ancho_px
            py = alto_plot / alto_px
            hillshade = cart.calcular_hillshade(dem, px, py)
            niveles = cart.niveles_contorno(dem)
        else:
            hillshade = None
            niveles = None

        osm = cart.descargar_osm(bbox_wgs, cache_dir, utm_epsg)
        if osm and not any(osm.values()):
            # Overpass no respondió o el área no tiene elementos mapeados: los
            # planos siguen siendo válidos, pero hay que decirlo en la bitácora.
            print("[GIS] OSM sin elementos para el área (Overpass saturado o "
                  "zona sin datos mapeados); la capa base queda vacía.")

        sitios = _puntos_en_poligono(union_utm, int(req.sitios or 0)) if union_utm is not None else []

        zona, hemisferio_norte = _zona_y_hemisferio(utm_epsg)

        # Toponimia: primero se deriva del mapa (lugar más cercano, municipio,
        # estado y colindancias por rumbo); luego manda el campo del shape y,
        # por último, los metadatos explícitos del request (futuro CSV/BD).
        metadatos = dict(cart.METADATOS_DEFECTO)
        topon = cart.resolver_toponimia(bbox_wgs, cache_dir, utm_epsg)
        if topon:
            for clave, valor in topon.items():
                if valor not in (None, ""):
                    metadatos[clave] = str(valor)
            # El núcleo agrario / predio toma provisionalmente el nombre del
            # lugar más cercano (a futuro vendrá de un CSV/BD).
            if topon.get("nombre_lugar"):
                metadatos["titulo_lienzo"] = str(topon["nombre_lugar"])
        for c in campos:
            if str(c).upper() in ("NOMBRE", "LUGAR", "PREDIO", "TOPONIMO", "TOPÓNIMO",
                                  "NOM_PREDIO", "NOMBRE_LUGAR", "NOMBRE_PREDIO", "EJIDO"):
                valor = registros[0].get(c) if registros else None
                if valor not in (None, ""):
                    metadatos["nombre_lugar"] = str(valor)
                    break
        for clave, valor in (req.metadatos or {}).items():
            if valor not in (None, ""):
                metadatos[clave] = str(valor)

        # La toponimia (lugar, municipio, estado y colindancias) también se
        # escribe en la tabla de atributos para poder revisarla/editarse en
        # QGIS o en un CSV/BD, y alimentar el núcleo agrario a futuro.
        campos_toponimia = ("nombre_lugar", "municipio", "estado",
                            "colindancia_n", "colindancia_s",
                            "colindancia_e", "colindancia_o")
        for fila in registros:
            for clave in campos_toponimia:
                fila[clave.upper()] = metadatos.get(clave, "")

        ctx = {
            "polys": polys,
            "xlim": xlim,
            "ylim": ylim,
            "sat": sat,
            "sat_extent": img_extent,
            "dem": dem,
            "hillshade": hillshade,
            "niveles": niveles,
            "osm": osm,
            "sitios": sitios,
            "vertices": cart.vertices_de_geometrias([union_utm] if union_utm is not None else geoms_utm),
            "utm_epsg": utm_epsg,
            "zona_utm": zona,
            "hemisferio_norte": hemisferio_norte,
            "datum": "WGS84",
            "superficie_ha": superficie_ha,
            "metadatos": metadatos,
        }

        # Clasificación temática de los planos 2, 3 y 4 (usa el campo del shape
        # si existe; si no, se deriva del MDE / satélite).
        for clave in ("plano_02", "plano_03", "plano_04"):
            cart.preparar_tema(ctx, clave)
        # Escribe en _datos/ las capas derivadas (temática, OSM, sitios) para
        # poder revisarlas y editarlas en el proyecto QGIS de cada plano.
        cart.escribir_capas_geo(ctx, datos_dir, utm_epsg)

        # Rásters para los proyectos QGIS
        sat_tif = datos_dir / "satelite_utm.tif"
        dem_tif = datos_dir / "dem_utm.tif"
        hill_tif = datos_dir / "hillshade_utm.tif"
        cart.guardar_geotiff(sat, sat_tif, out_bounds, utm_epsg)
        if dem is not None:
            cart.guardar_geotiff(dem, dem_tif, out_bounds, utm_epsg)
        if hillshade is not None:
            cart.guardar_geotiff((hillshade * 255.0).astype(np.uint8), hill_tif, out_bounds, utm_epsg)

        # Render de cada plano en su propia subcarpeta
        capas_base = [str(shape_path)]
        for tif in (sat_tif, dem_tif, hill_tif):
            if tif.exists():
                capas_base.append(str(tif))
        # La base cartográfica (OSM) se agrega a todos los planos.
        capas_base.extend(str(p) for p in sorted(datos_dir.glob("osm_*.geojson")))
        capas_tema = {
            "plano_02": [datos_dir / "tematica_plano_02.geojson"],
            "plano_03": [datos_dir / "tematica_plano_03.geojson",
                         datos_dir / "sitios_muestreo.geojson"],
            "plano_04": [datos_dir / "tematica_plano_04.geojson"],
        }

        planos_generados: List[str] = []
        qgis_generados: List[str] = []
        for carpeta, base, render in cart.PLANOS:
            sub = output_dir / carpeta
            sub.mkdir(parents=True, exist_ok=True)
            pdf_path = sub / ("%s.pdf" % base)
            png_path = sub / ("%s.png" % base)
            render(pdf_path, png_path, ctx)
            planos_generados.append(str(pdf_path))
            if req.qgis:
                qgz = sub / ("%s.qgz" % base)
                capas = capas_base + [str(p) for p in capas_tema.get(carpeta, [])
                                      if p.exists()]
                if cart.escribir_proyecto_qgis(qgz, capas, utm_epsg):
                    qgis_generados.append(str(qgz))

        # Tabla de atributos (extra)
        campos_salida = (["ID_SIG"] + campos +
                         [c.upper() for c in campos_toponimia] + ["AREA_HA"])
        tabla_csv = output_dir / "tabla_atributos.csv"
        tabla_xlsx = output_dir / "tabla_atributos.xlsx"
        cart.atributos_a_csv_y_xlsx(registros, campos_salida, tabla_csv, tabla_xlsx)

        ds = None
        return {
            "status": "success",
            "msg": "Planos generados correctamente",
            "cliente_id": req.cliente_id,
            "proyecto_id": req.proyecto_id,
            "shape_path": str(shape_path),
            "output_dir": str(output_dir),
            "planos_pdf": [Path(p).name for p in planos_generados],
            "planos_paths": planos_generados,
            "qgis_proyectos": [Path(p).name for p in qgis_generados],
            "count": len(planos_generados),
            "superficie_ha": round(superficie_ha, 4),
            "utm_epsg": utm_epsg,
            "geometrias": len(polys),
            "sitios_sinteticos": len(sitios),
            "tabla_atributos": str(tabla_csv),
            "satelite_descargado": bool(cart.DESCARGAR_TILES),
            "dem_disponible": dem is not None,
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error generando planos: %s" % e)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
