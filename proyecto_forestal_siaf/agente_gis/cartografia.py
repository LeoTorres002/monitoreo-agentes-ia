# -*- coding: utf-8 -*-
"""
Utilidades cartográficas del agente GIS de SIAF.

* Trabaja SIEMPRE en un CRS proyectado UTM (norte arriba).
* Fondo satelital y Modelo Digital de Elevación (MDE) se descargan de
  servicios públicos de teselas (configurables por variables de entorno),
  se cachean en disco y se reproyectan a UTM con GDAL.
* Render de planos con matplotlib (backend Agg, headless).
* Exportación de tabla de atributos a CSV/XLSX.
* Escritura de proyectos QGIS (.qgz) por plano.

No requiere PyQGIS para el render; sólo para escribir los .qgz.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import textwrap
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

os.environ.setdefault("MPLBACKEND", "Agg")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Circle, PathPatch, Polygon, Rectangle
from matplotlib.path import Path as MplPath
from matplotlib.patches import FancyArrow  # noqa: F401  (disponible para adornos)
from matplotlib.offsetbox import AnnotationBbox, OffsetImage

from osgeo import gdal, ogr, osr

# Mantener el estilo "None" clásico de GDAL y silenciar los FutureWarning.
try:
    gdal.DontUseExceptions()
except Exception:
    pass
try:
    ogr.DontUseExceptions()
except Exception:
    pass
try:
    osr.DontUseExceptions()
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

TILE_SATELITE_URL = os.environ.get(
    "SIAF_TILE_SATELITE",
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
)
TILE_DEM_URL = os.environ.get(
    "SIAF_TILE_DEM",
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
)
OSM_URL = os.environ.get("SIAF_OSM_URL", "https://overpass-api.de/api/interpreter")

DESCARGAR_TILES = os.environ.get("SIAF_DESCARGAR_TILES", "1").lower() not in ("0", "false", "no")
# OpenStreetMap activo por defecto: hidrografía (cauces permanentes/temporales),
# caminos (generales/principales), líneas de transmisión y cuerpos de agua son
# capas del plano. Se apaga con SIAF_OSM=0 (o en modo sin conexión).
DESCARGAR_OSM = os.environ.get("SIAF_OSM", "1").lower() not in ("0", "false", "no")
TILE_TIMEOUT = float(os.environ.get("SIAF_TILE_TIMEOUT", "20"))
TILE_WORKERS = int(os.environ.get("SIAF_TILE_WORKERS", "8"))
MAX_TILES_SATELITE = int(os.environ.get("SIAF_MAX_TILES_SATELITE", "900"))
MAX_TILES_DEM = int(os.environ.get("SIAF_MAX_TILES_DEM", "120"))
# Rango de zoom del satélite: se intenta el máximo y se baja si no hay imagen.
ZOOM_MAX_SATELITE = int(os.environ.get("SIAF_ZOOM_MAX_SATELITE", "19"))
ZOOM_MIN_SATELITE = int(os.environ.get("SIAF_ZOOM_MIN_SATELITE", "15"))

# Toponimia (OpenStreetMap): nombres de lugares, municipio, estado y colindancias.
GEO_URL_NOMINATIM = os.environ.get("SIAF_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
GEO_URL_OVERPASS = os.environ.get("SIAF_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
# Overpass es intermitente: se reintenta y se usan espejos alternos.
GEO_URLS_OVERPASS: List[str] = []
for _u in (GEO_URL_OVERPASS,
           "https://overpass.kumi.systems/api/interpreter",
           "https://overpass.private.coffee/api/interpreter"):
    if _u and _u not in GEO_URLS_OVERPASS:
        GEO_URLS_OVERPASS.append(_u)
GEO_REINTENTOS = int(os.environ.get("SIAF_GEO_REINTENTOS", "2"))
DESCARGAR_GEO = os.environ.get("SIAF_GEO", "1").lower() not in ("0", "false", "no")
GEO_RADIO_KM = float(os.environ.get("SIAF_GEO_RADIO_KM", "30"))
GEO_TIMEOUT = float(os.environ.get("SIAF_GEO_TIMEOUT", "30"))

# Afinado de la toponimia con el LLM local. DESACTIVADO por defecto: en pruebas
# llama3 tardó ~3 min y eligió lugares MÁS LEJANOS que el criterio por distancia
# (no tiene mejor información geográfica que la lista de candidatos). Activar con
# SIAF_GEO_LLM=1 solo si se quiere probar.
GEO_LLM = os.environ.get("SIAF_GEO_LLM", "0").lower() not in ("0", "false", "no")
GEO_LLM_MODEL = os.environ.get("SIAF_GEO_LLM_MODEL") or os.environ.get("RAG_LLM_MODEL") or "llama3"
GEO_LLM_TIMEOUT = float(os.environ.get("SIAF_GEO_LLM_TIMEOUT", "60"))
GEO_LLM_MAX_CAND = int(os.environ.get("SIAF_GEO_LLM_MAX_CAND", "16"))

# Resolución de salida del RÁSTER reproyectado (el PDF es vectorial encima).
ANCHO_RASTER_PX = int(os.environ.get("SIAF_ANCHO_RASTER_PX", "1600"))

# Metadatos que no vienen en el shape: se dejan como marcadores.
PLACEHOLDER = lambda campo: '[INSERTE "%s" aquí]' % campo  # noqa: E731

METADATOS_DEFECTO = {
    "nombre_lugar": PLACEHOLDER("nombre del lugar"),
    "titulo_lienzo": PLACEHOLDER("nombre del núcleo agrario / predio"),
    "municipio": PLACEHOLDER("municipio"),
    "estado": PLACEHOLDER("estado"),
    "institucion": PLACEHOLDER("unidad de conservación / prestador de servicios técnicos"),
    "contacto": PLACEHOLDER("dirección y contacto"),
    "logo": PLACEHOLDER("logotipo institucional"),
    "superficie_legal": PLACEHOLDER("superficie legal"),
    "superficie_posesion": PLACEHOLDER("superficie en posesión actual"),
    "region_hidrologica": PLACEHOLDER("región hidrológica"),
    "cuenca_hidrologica": PLACEHOLDER("cuenca y subcuenca"),
    "colindancia_n": PLACEHOLDER("colindancia norte"),
    "colindancia_s": PLACEHOLDER("colindancia sur"),
    "colindancia_e": PLACEHOLDER("colindancia oriente"),
    "colindancia_o": PLACEHOLDER("colindancia poniente"),
}

# Colores por anualidad (plan de aprovechamiento).
COLORES_ANUALIDAD = {
    2025: "#d73027",
    2026: "#fc8d59",
    2027: "#fee090",
    2028: "#91bfdb",
    2029: "#4575b4",
    None: "#bdbdbd",
}
COLORES_UM = ["#e41a1c", "#377eb8", "#4daf4a", "#984ea3",
              "#ff7f00", "#a65628", "#f781bf", "#999999",
              "#66c2a5", "#fc8d62", "#8da0cb"]

# --------------------------------------------------------------------------- #
# Catálogos temáticos (planos 2, 3 y 4)
# --------------------------------------------------------------------------- #
# El shape manda: si trae el campo temático, se usa su valor (mapeado por el
# catálogo). Si no lo trae, se DERIVA una clasificación con datos reales
# (pendiente/altitud del MDE para superficies; cobertura estimada del satélite
# para vegetación) para que el plano nunca quede vacío. La procedencia queda
# escrita en la leyenda.
TEMAS: Dict[str, Dict] = {
    "plano_02": {
        "titulo": "Clasificación de superficies",
        "campos": ("CLASS25", "CLASE", "CATEGORIA", "SUPERFICIE", "USO", "TIPO"),
        "derivar": "superficies",
        "defecto": "Sin clasificar",
        "catalogo": (
            (r"BAVC|ALTO VALOR", "Área de BAVC", "#6a1b9a"),
            (r"CUENCA", "Área de cuenca alta", "#00838f"),
            (r"AGUA|HIDRICO|HIDRICA|CUERPO", "Cuerpos de agua", "#1e88e5"),
            (r"CONSERVACION|PROTECCION|RESTRINGID|ANP",
             "Área de conservación y aprovechamiento restringido", "#1b5e20"),
            (r"RESTAURACION", "Área de restauración", "#e0a458"),
            (r"PASTOREO|OTROS USOS|AGRICULTURA|ASENTAMIENTO", "Pastoreo / otros usos", "#c9a227"),
            (r"PRODUCCION|APROVECHAMIENTO", "Área de producción", "#8bc34a"),
        ),
    },
    "plano_03": {
        "titulo": "Tratamientos complementarios",
        "campos": ("SILVO_25", "TRATAMIENTO", "TRATAMIENTOS", "SILVICULTURA", "CORTAS_25"),
        "derivar": "tratamientos",
        "defecto": "Sin acciones",
        "catalogo": (
            (r"PREV.*INCENDIO", "Prevención de incendios forestales", "#e0a458"),
            (r"LIMPIA|ACOMODO", "Limpia, acomodo y prevención de incendios", "#a7d08c"),
            (r"SIN ACCION|SIN TRATAMIENTO|NINGUN", "Sin acciones", "#cfd8dc"),
            (r"SELECCION|SELECC", "Corta de selección", "#66bb6a"),
        ),
    },
    "plano_04": {
        "titulo": "Uso de suelo y vegetación (INEGI, Serie VII)",
        "campos": ("VEGETA", "VEGETACION", "USO_SUELO", "USO_VEG", "SERIE7", "SERIE_VII",
                   "COBERTURA", "INEGI", "CLAVE_VEG"),
        "derivar": "vegetacion",
        "defecto": "Sin clasificar",
        "catalogo": (
            (r"ARBUSTIVA.*PINO[- ]?ENCINO", "Veg. sec. arbustiva bosque pino-encino", "#9ccc65"),
            (r"ARBUSTIVA.*(BOSQUE\s*)?ENCINO", "Veg. sec. arbustiva bosque encino", "#c5e1a5"),
            (r"ARBUSTIVA.*PINO", "Veg. sec. arbustiva bosque pino", "#aed581"),
            (r"ARBUSTIVA.*SELVA BAJA", "Veg. sec. arbustiva selva baja caducifolia", "#dce775"),
            (r"ARBOREA.*PINO", "Veg. sec. arbórea bosque pino", "#7cb342"),
            (r"BOSQUE\s*(DE\s*)?PINO[- ]?ENCINO", "Bosque de pino-encino", "#2e7d32"),
            (r"BOSQUE\s*(DE\s*)?PINO", "Bosque de pino", "#1b5e20"),
            (r"SELVA BAJA CADUCIFOLIA", "Selva baja caducifolia", "#827717"),
            (r"PASTIZAL", "Pastizal inducido", "#d4c76a"),
            (r"AGRICULTURA", "Agricultura de temporal", "#e0a458"),
            (r"ASENTAMIENTO", "Asentamientos humanos", "#b0bec5"),
        ),
    },
}

# Layout A3 horizontal (pulgadas).
FIG_W, FIG_H = 16.54, 11.69
L_MAPA = [0.045, 0.075, 0.630, 0.820]
L_SIDE = [0.700, 0.050, 0.285, 0.900]

# Tamaño (pt) del título de la tabla de vértices; se reutiliza para numerar
# el centro de cada geometría (a petición del cliente deben coincidir).
FS_TITULO_TABLA = 5.8


# --------------------------------------------------------------------------- #
# Matemática de teselas (XYZ / Web Mercator)
# --------------------------------------------------------------------------- #

def deg2num(lat: float, lon: float, z: int) -> Tuple[float, float]:
    lat_r = math.radians(lat)
    n = 2.0 ** z
    xtile = (lon + 180.0) / 360.0 * n
    ytile = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return xtile, ytile


def num2deg(x: float, y: float, z: int) -> Tuple[float, float]:
    n = 2.0 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def _merc_x(lon: float) -> float:
    return lon * 20037508.342789244 / 180.0


def _merc_y(lat: float) -> float:
    lat = max(min(lat, 89.999), -89.999)
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return y * 20037508.342789244 / 180.0


def _elegir_zoom(bbox_wgs: Sequence[float], z_max: int, z_min: int,
                 max_tiles: int) -> Tuple[int, int, int]:
    """Devuelve (zoom, n_tiles_x, n_tiles_y) para el bbox dado."""
    oeste, sur, este, norte = bbox_wgs
    z = z_min
    nx = ny = 1
    for zz in range(z_max, z_min - 1, -1):
        x0, y0 = deg2num(norte, oeste, zz)
        x1, y1 = deg2num(sur, este, zz)
        nx = int(math.floor(x1)) - int(math.floor(x0)) + 1
        ny = int(math.floor(y1)) - int(math.floor(y0)) + 1
        if nx * ny <= max_tiles:
            return zz, nx, ny
        z = zz
    return z, nx, ny


# --------------------------------------------------------------------------- #
# Descarga de teselas con caché y paralelismo
# --------------------------------------------------------------------------- #

def _ruta_cache_tile(cache_dir: Path, subdir: str, z: int, x: int, y: int, ext: str) -> Path:
    d = cache_dir / subdir / str(z) / str(x)
    d.mkdir(parents=True, exist_ok=True)
    return d / ("%d.%s" % (y, ext))


def _un_tile(url_template: str, z: int, x: int, y: int,
             cache_dir: Path, subdir: str, ext: str,
             timeout: float) -> Optional[Path]:
    fp = _ruta_cache_tile(cache_dir, subdir, z, x, y, ext)
    if fp.exists() and fp.stat().st_size > 0:
        return fp
    url = url_template.format(z=z, x=x, y=y)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SIAF-GIS/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data:
            return None
        fp.write_bytes(data)
        return fp
    except Exception:
        return None


def _mosaico_teselas(url_template: str, z: int, x0: int, x1: int, y0: int, y1: int,
                     cache_dir: Path, subdir: str, timeout: float) -> Optional[np.ndarray]:
    """Descarga y une las teselas. Devuelve ndarray (H, W, 3) uint8 o None."""
    try:
        from PIL import Image
    except Exception:
        return None

    ext = "jpg" if "arcgisonline" in url_template or "google" in url_template else "png"
    ancho, alto = (x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256
    mosaico = Image.new("RGB", (ancho, alto), (0, 0, 0))

    coords = [(xi, yi) for xi in range(x0, x1 + 1) for yi in range(y0, y1 + 1)]
    with ThreadPoolExecutor(max_workers=TILE_WORKERS) as ex:
        futuros = {ex.submit(_un_tile, url_template, z, xi, yi, cache_dir, subdir, ext, timeout): (xi, yi)
                   for xi, yi in coords}
        obtenidos = 0
        for fut, (xi, yi) in futuros.items():
            fp = fut.result()
            if fp is None:
                continue
            try:
                img = Image.open(fp).convert("RGB")
                mosaico.paste(img, ((xi - x0) * 256, (yi - y0) * 256))
                obtenidos += 1
            except Exception:
                continue
    if obtenidos == 0:
        return None
    return np.asarray(mosaico)


def _warp_a_utm(arr: np.ndarray, geotransform: Sequence[float],
                src_epsg: int, utm_epsg: int,
                out_bounds_utm: Sequence[float], out_size: Tuple[int, int],
                bandas: int, gdt_dtype, resample: int = gdal.GRA_Bilinear) -> Optional[np.ndarray]:
    """Reproyecta un arreglo georreferenciado a UTM. Devuelve (H, W) o (H, W, B)."""
    alto, ancho = arr.shape[0], arr.shape[1]
    drv = gdal.GetDriverByName("GTiff")
    src = drv.Create("/vsimem/_src_%d.tif" % id(arr), ancho, alto, bandas, gdt_dtype)
    if src is None:
        return None
    src.SetGeoTransform(tuple(geotransform))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(src_epsg)
    src.SetProjection(srs.ExportToWkt())
    for b in range(bandas):
        banda = src.GetRasterBand(b + 1)
        if bandas == 1:
            banda.WriteArray(arr)
        else:
            banda.WriteArray(arr[:, :, b])
        banda.FlushCache()

    opts = gdal.WarpOptions(
        dstSRS="EPSG:%d" % utm_epsg,
        outputBounds=tuple(out_bounds_utm),
        width=int(out_size[0]), height=int(out_size[1]),
        resampleAlg=resample,
    )
    out_name = "/vsimem/_warp_%d.tif" % id(arr)
    dst = gdal.Warp(out_name, src, options=opts)
    resultado = None
    if dst is not None:
        b = dst.RasterCount
        if b == 1:
            resultado = dst.ReadAsArray().astype(np.float32)
        else:
            resultado = np.transpose(dst.ReadAsArray(), (1, 2, 0))
        dst = None
    src = None
    for nombre in (out_name, "/vsimem/_src_%d.tif" % id(arr)):
        try:
            gdal.Unlink(nombre)
        except Exception:
            pass
    return resultado


def _es_imagen_real(arr: Optional[np.ndarray]) -> bool:
    """Heurística: cuando un zoom no tiene imagen, Esri sirve un gris uniforme
    (R=G=B). La fotografía real (World Imagery) tiene color. Se descartan los
    píxeles no-data (negro) que aparecen en las esquinas tras reproyectar."""
    if arr is None:
        return False
    a = np.asarray(arr).astype(np.int16)
    if a.ndim == 3 and a.shape[2] >= 3:
        rgb = a[:, :, :3]
        mx, mn = rgb.max(2), rgb.min(2)
        valido = (mx + mn) > 30            # ignora negro / no-data
        if valido.sum() < 0.05 * valido.size:
            return False
        return float((mx[valido] - mn[valido]).mean()) > 2.5
    g = a.astype(np.float32)
    m = g > 20
    return bool(m.any()) and float(g[m].std()) > 8.0


def construir_satelite(bbox_wgs: Sequence[float], utm_epsg: int,
                       out_bounds_utm: Sequence[float], out_size: Tuple[int, int],
                       cache_dir: Path) -> Optional[np.ndarray]:
    """Fondo satelital reproyectado a UTM. Devuelve (H, W, 3) uint8 o None.

    Si el zoom más alto disponible no tiene imagen en la zona (Esri devuelve
    teselas grises uniformes), se baja de zoom hasta encontrar fotografía real.
    """
    if not DESCARGAR_TILES:
        return None
    # Pedir teselas para el extent COMPLETO (bbox + relleno), no solo el bbox
    # del proyecto: así no quedan franjas negras (no-data) en los bordes.
    bbox_wgs = _bbox_wgs_desde_utm(out_bounds_utm, utm_epsg)
    oeste, sur, este, norte = bbox_wgs
    z_top = min(_elegir_zoom(bbox_wgs, ZOOM_MAX_SATELITE, 10, MAX_TILES_SATELITE)[0],
                ZOOM_MAX_SATELITE)
    z_min = min(ZOOM_MIN_SATELITE, z_top)

    resultado: Optional[np.ndarray] = None
    for z in range(z_top, z_min - 1, -1):
        x0f, y0f = deg2num(norte, oeste, z)
        x1f, y1f = deg2num(sur, este, z)
        x0, y0 = int(math.floor(x0f)), int(math.floor(y0f))
        x1, y1 = int(math.floor(x1f)), int(math.floor(y1f))

        arr = _mosaico_teselas(TILE_SATELITE_URL, z, x0, x1, y0, y1,
                               cache_dir, "satelite", TILE_TIMEOUT)
        if arr is None:
            continue

        lat_top, lon_left = num2deg(x0, y0, z)
        lat_bot, lon_right = num2deg(x1 + 1, y1 + 1, z)
        mx0, my1 = _merc_x(lon_left), _merc_y(lat_top)
        mx1, my0 = _merc_x(lon_right), _merc_y(lat_bot)
        px = (mx1 - mx0) / arr.shape[1]
        py = (my0 - my1) / arr.shape[0]   # negativo: raster norte-arriba
        gt = (mx0, px, 0.0, my1, 0.0, py)

        warped = _warp_a_utm(arr, gt, 3857, utm_epsg, out_bounds_utm, out_size,
                             bandas=3, gdt_dtype=gdal.GDT_Byte)
        if warped is None:
            continue
        if _es_imagen_real(warped):
            return warped
        resultado = warped

    return resultado


def construir_dem(bbox_wgs: Sequence[float], utm_epsg: int,
                  out_bounds_utm: Sequence[float], out_size: Tuple[int, int],
                  cache_dir: Path) -> Optional[np.ndarray]:
    """MDE (terrarium) reproyectado a UTM. Devuelve (H, W) float32 o None."""
    if not DESCARGAR_TILES:
        return None
    bbox_wgs = _bbox_wgs_desde_utm(out_bounds_utm, utm_epsg)
    oeste, sur, este, norte = bbox_wgs
    z, nx, ny = _elegir_zoom(bbox_wgs, 14, 8, MAX_TILES_DEM)
    x0f, y0f = deg2num(norte, oeste, z)
    x1f, y1f = deg2num(sur, este, z)
    x0, y0 = int(math.floor(x0f)), int(math.floor(y0f))
    x1, y1 = int(math.floor(x1f)), int(math.floor(y1f))

    arr = _mosaico_teselas(TILE_DEM_URL, z, x0, x1, y0, y1,
                           cache_dir, "dem", TILE_TIMEOUT)
    if arr is None:
        return None

    af = arr.astype(np.float32)
    dem = af[:, :, 0] * 256.0 + af[:, :, 1] + af[:, :, 2] / 256.0 - 32768.0

    lat_top, lon_left = num2deg(x0, y0, z)
    lat_bot, lon_right = num2deg(x1 + 1, y1 + 1, z)
    mx0, my1 = _merc_x(lon_left), _merc_y(lat_top)
    mx1, my0 = _merc_x(lon_right), _merc_y(lat_bot)
    px = (mx1 - mx0) / dem.shape[1]
    py = (my0 - my1) / dem.shape[0]   # negativo: raster norte-arriba
    gt = (mx0, px, 0.0, my1, 0.0, py)

    return _warp_a_utm(dem, gt, 3857, utm_epsg, out_bounds_utm, out_size,
                       bandas=1, gdt_dtype=gdal.GDT_Float32)


def calcular_hillshade(dem: np.ndarray, px: float, py: float,
                       azimut: float = 315.0, altitud: float = 45.0) -> np.ndarray:
    """Sombreado de relieve (Horn) a partir del MDE. Devuelve valores 0..1."""
    dem = np.where(np.isfinite(dem), dem, np.nanmin(dem))
    dy, dx = np.gradient(dem, py, px)
    pendiente = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspecto = np.arctan2(-dx, dy)
    az = math.radians(360.0 - azimut + 90.0)
    alt = math.radians(altitud)
    shaded = (np.sin(alt) * np.sin(pendiente) +
              np.cos(alt) * np.cos(pendiente) * np.cos(az - aspecto))
    return np.clip((shaded + 1.0) / 2.0, 0.0, 1.0)


def niveles_contorno(dem: np.ndarray) -> np.ndarray:
    """Elige un intervalo de curvas de nivel legible."""
    if dem is None or dem.size == 0:
        return np.array([])
    vmin, vmax = float(np.nanmin(dem)), float(np.nanmax(dem))
    rango = vmax - vmin
    if rango < 1.5:  # relieve despreciable: no dibujar curvas
        return np.array([])
    for intervalo in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        n = rango / intervalo
        if 4 <= n <= 25:
            base = math.floor(vmin / intervalo) * intervalo
            return np.arange(base, vmax + intervalo, intervalo)
    intervalo = max(rango / 15.0, 1.0)
    base = math.floor(vmin / intervalo) * intervalo
    return np.arange(base, vmax + intervalo, intervalo)


# --------------------------------------------------------------------------- #
# Geometría
# --------------------------------------------------------------------------- #

def get_utm_epsg(lon: float, lat: float) -> int:
    zona = int((lon + 180.0) / 6.0) + 1
    zona = max(1, min(60, zona))
    return (32700 if lat < 0 else 32600) + zona


def srs_desde_epsg(epsg: int) -> osr.SpatialReference:
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    try:
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return s


def geom_a_epsg(geom: ogr.Geometry, src_srs: osr.SpatialReference, epsg: int) -> Optional[ogr.Geometry]:
    if geom is None:
        return None
    tgt = srs_desde_epsg(epsg)
    if src_srs is None:
        return geom.Clone()
    if src_srs.IsSame(tgt):
        return geom.Clone()
    tr = osr.CoordinateTransformation(src_srs, tgt)
    g2 = geom.Clone()
    g2.Transform(tr)
    return g2


def poligonos_de_geometria(geom: ogr.Geometry) -> List[Dict]:
    """Extrae TODOS los polígonos (exterior + huecos) de cualquier geometría."""
    salida: List[Dict] = []
    if geom is None:
        return salida
    gt = geom.GetGeometryType()
    if gt in (ogr.wkbPolygon, ogr.wkbPolygon25D, ogr.wkbCurvePolygon):
        salida.append(_anillos_poligono(geom))
    elif gt in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D,
                ogr.wkbGeometryCollection, ogr.wkbGeometryCollection25D):
        for i in range(geom.GetGeometryCount()):
            salida.extend(poligonos_de_geometria(geom.GetGeometryRef(i)))
    return salida


def _anillos_poligono(poly: ogr.Geometry) -> Dict:
    ext: List[Tuple[float, float]] = []
    anillo = poly.GetGeometryRef(0)
    if anillo is not None:
        ext = [(anillo.GetX(i), anillo.GetY(i)) for i in range(anillo.GetPointCount())]
    huecos = []
    for h in range(1, poly.GetGeometryCount()):
        hr = poly.GetGeometryRef(h)
        if hr is not None:
            huecos.append([(hr.GetX(i), hr.GetY(i)) for i in range(hr.GetPointCount())])
    return {"ext": ext, "huecos": huecos}


def bbox_de_geoms(geoms: Sequence[ogr.Geometry]) -> Optional[Tuple[float, float, float, float]]:
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    for g in geoms:
        if g is None:
            continue
        env = g.GetEnvelope()
        xmin = min(xmin, env[0]); ymin = min(ymin, env[2])
        xmax = max(xmax, env[1]); ymax = max(ymax, env[3])
    if not math.isfinite(xmin):
        return None
    return (xmin, ymin, xmax, ymax)


def _bbox_wgs_desde_utm(out_bounds_utm: Sequence[float],
                        utm_epsg: int) -> Tuple[float, float, float, float]:
    """BBox WGS84 (oeste, sur, este, norte) del extent UTM solicitado.

    Las teselas se piden por su bbox geográfico: usar el bbox del proyecto (o su
    relleno) evita que el extent quede a medias y aparezcan franjas negras."""
    minx, miny, maxx, maxy = out_bounds_utm
    tr = osr.CoordinateTransformation(srs_desde_epsg(utm_epsg), srs_desde_epsg(4326))
    lons: List[float] = []
    lats: List[float] = []
    for px, py in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
        lo, la, _ = tr.TransformPoint(px, py)
        lons.append(lo)
        lats.append(la)
    return (min(lons), min(lats), max(lons), max(lats))


def vertices_de_geometrias(geoms: Sequence[ogr.Geometry], max_vertices: int = 120) -> List[Tuple[float, float]]:
    """Vértices de los anillos exteriores, sin repetir el cierre."""
    verts: List[Tuple[float, float]] = []
    for g in geoms:
        for poly in poligonos_de_geometria(g):
            ext = poly["ext"]
            if len(ext) > 1 and ext[0] == ext[-1]:
                ext = ext[:-1]
            verts.extend(ext)
            if len(verts) >= max_vertices:
                return verts[:max_vertices]
    return verts


# --------------------------------------------------------------------------- #
# OpenStreetMap (opcional, con degradación silenciosa)
# --------------------------------------------------------------------------- #

def descargar_osm(bbox_wgs: Sequence[float], cache_dir: Path,
                  utm_epsg: int) -> Dict[str, List[List[Tuple[float, float]]]]:
    """Capas de OpenStreetMap reproyectadas a UTM (con caché y espejos).

    Devuelve caminos (generales/principales), cauces (permanentes/temporales),
    líneas de transmisión y cuerpos de agua. Degrada en silencio si no hay red."""
    vacio: Dict[str, List] = {"caminos_generales": [], "caminos_principales": [],
                              "cauces_permanentes": [], "cauces_temporales": [],
                              "lineas_transmision": [], "cuerpos_agua": []}
    if not DESCARGAR_OSM:
        return vacio
    oeste, sur, este, norte = bbox_wgs
    clave = "%.4f_%.4f_%.4f_%.4f" % (sur, oeste, norte, este)
    cache_fp = cache_dir / "osm" / (clave + ".json")
    doc = None
    if cache_fp.exists():
        try:
            doc = json.loads(cache_fp.read_text("utf-8"))
        except Exception:
            doc = None
    if doc is None:
        bb = "%f,%f,%f,%f" % (sur, oeste, norte, este)
        consulta = ("[out:json][timeout:40];("
                    'way["highway"](%s);'
                    'way["waterway"](%s);'
                    'way["power"~"^(line|minor_line)$"](%s);'
                    'way["natural"="water"](%s);'
                    ");out geom;" % (bb, bb, bb, bb))
        doc = _overpass(consulta, 30.0, reintentos=1)
        if doc is None:
            return vacio
        try:
            cache_fp.parent.mkdir(parents=True, exist_ok=True)
            cache_fp.write_text(json.dumps(doc), "utf-8")
        except Exception:
            pass

    tr = osr.CoordinateTransformation(srs_desde_epsg(4326), srs_desde_epsg(utm_epsg))
    principales = ("motorway", "trunk", "primary", "secondary")
    for el in doc.get("elements", []):
        geom_pts = el.get("geometry") or []
        if len(geom_pts) < 2:
            continue
        pts = []
        for p in geom_pts:
            try:
                x, y, _ = tr.TransformPoint(float(p["lon"]), float(p["lat"]))
                pts.append((x, y))
            except Exception:
                continue
        if len(pts) < 2:
            continue
        tags = el.get("tags", {})
        if "highway" in tags:
            tipo = str(tags.get("highway", ""))
            if tipo.split("_")[0] in principales:
                vacio["caminos_principales"].append(pts)
            else:
                vacio["caminos_generales"].append(pts)
        if "waterway" in tags:
            temporal = (str(tags.get("intermittent", "")).lower() in ("yes", "true", "1")
                        or str(tags.get("waterway", "")) in ("ditch", "drain"))
            vacio["cauces_temporales" if temporal else "cauces_permanentes"].append(pts)
        if "power" in tags:
            vacio["lineas_transmision"].append(pts)
        if str(tags.get("natural", "")) == "water" and pts[0] == pts[-1]:
            vacio["cuerpos_agua"].append(pts)
    return vacio


# --------------------------------------------------------------------------- #
# Toponimia (OpenStreetMap): lugar más cercano, municipio, estado y colindancias
# --------------------------------------------------------------------------- #

# Preferencia por tipo de asentamiento (menor = más "lugar poblado").
TIPOS_LUGAR = {
    "city": 0, "town": 1, "village": 2, "hamlet": 3,
    "isolated_dwelling": 4, "locality": 5, "farm": 6, "allotments": 7,
}


def _json_http(url: str, data: Optional[bytes] = None, timeout: float = 25.0):
    cabeceras = {"User-Agent": "SIAF-GIS/1.0 (toponimia; uso local)"}
    req = urllib.request.Request(url, data=data, headers=cabeceras)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _overpass(consulta: str, timeout: float, reintentos: Optional[int] = None) -> Optional[dict]:
    """Ejecuta una consulta Overpass con reintentos y espejos alternos.

    Overpass responde 429/504 con frecuencia; sin esto la toponimia fallaría en
    silencio y los planos quedarían con marcadores."""
    data = urllib.parse.urlencode({"data": consulta}).encode("utf-8")
    n = max(1, int(reintentos if reintentos is not None else GEO_REINTENTOS))
    for url in GEO_URLS_OVERPASS:
        for _ in range(n):
            try:
                return _json_http(url, data=data, timeout=timeout)
            except Exception:
                time.sleep(1.0)  # Overpass se satura: pequeña pausa entre espejos
                continue
    return None


def _sector_cardinal(rumbo: float) -> str:
    """Sector cardinal (n/e/s/o) para un rumbo en grados (0=N, 90=E)."""
    if rumbo >= 315.0 or rumbo < 45.0:
        return "n"
    if rumbo < 135.0:
        return "e"
    if rumbo < 225.0:
        return "s"
    return "o"


def _urls_llm() -> List[str]:
    """Endpoints OpenAI-compatibles candidatos para el LLM local.

    Incluye 127.0.0.1/localhost porque `host.containers.internal` no resuelve
    cuando el agente corre directamente en el host (no en contenedor)."""
    urls: List[str] = []
    ollama = os.environ.get("OLLAMA_URL")
    candidatos = [os.environ.get("SIAF_GEO_LLM_URL"),
                  os.environ.get("RAG_LLM_URL")]
    if ollama:
        candidatos.append(ollama.rstrip("/") + "/v1")
    candidatos += ["http://127.0.0.1:11434/v1", "http://localhost:11434/v1"]
    en_contenedor = (os.path.exists("/.dockerenv") or
                     os.path.exists("/run/.containerenv"))
    for cand in candidatos:
        if not cand:
            continue
        cand = cand.rstrip("/")
        # `host.containers.internal` no resuelve en el host: apuntar a local.
        if not en_contenedor and "host.containers.internal" in cand:
            cand = cand.replace("host.containers.internal", "127.0.0.1")
        if cand not in urls:
            urls.append(cand)
    return urls


def _consulta_llm(prompt: str, timeout: float) -> Optional[str]:
    """Pide una respuesta al LLM local. Devuelve el texto o None si falla."""
    for base in _urls_llm():
        try:
            payload = json.dumps({
                "model": GEO_LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "stream": False,
            }).encode("utf-8")
            req = urllib.request.Request(
                base + "/chat/completions", data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer local"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.loads(r.read().decode("utf-8", "replace"))
            return doc["choices"][0]["message"]["content"]
        except Exception:
            continue
    return None


def _candidatos_llm(cand: List[Tuple[float, float, str, str]],
                    n_global: int = 6, n_sector: int = 4
                    ) -> List[Tuple[float, float, str, str]]:
    """Candidatos a mostrar al LLM: los más cercanos en general y por sector."""
    seleccion: List[Tuple[float, float, str, str]] = []
    for c in sorted(cand, key=lambda c: (c[0], TIPOS_LUGAR.get(c[3], 9)))[:n_global]:
        if c not in seleccion:
            seleccion.append(c)
    for sec in ("n", "e", "s", "o"):
        en_sec = sorted([c for c in cand if _sector_cardinal(c[1]) == sec],
                        key=lambda c: (c[0], TIPOS_LUGAR.get(c[3], 9)))
        for c in en_sec[:n_sector]:
            if c not in seleccion:
                seleccion.append(c)
    return seleccion[:GEO_LLM_MAX_CAND]


def _refinar_con_llm(cand: List[Tuple[float, float, str, str]]
                     ) -> Optional[Dict[str, str]]:
    """Pide al LLM local elegir la localidad de referencia y las colindancias.

    Sólo acepta nombres que estén entre los candidatos (y, en colindancias, que
    pertenezcan al sector correcto). Si algo no cuadra, devuelve None."""
    if not cand:
        return None
    cand_llm = _candidatos_llm(cand)
    if not cand_llm:
        return None
    por_sector = {sec: [c[2] for c in sorted(
        [c for c in cand_llm if _sector_cardinal(c[1]) == sec], key=lambda c: c[0])]
        for sec in ("n", "e", "s", "o")}
    renglones = "\n".join(
        "%d. %s (tipo: %s, a %.1f km del centro, rumbo %.0f grados)" % (
            i + 1, nm, tp or "sin clasificar", d / 1000.0, br)
        for i, (d, br, nm, tp) in enumerate(
            sorted(cand_llm, key=lambda c: c[0])))
    prompt = (
        "Eres un cartógrafo forestal en México. El predio está en el centro del mapa.\n"
        "Estos son los asentamientos más cercanos (nombre, tipo OpenStreetMap,\n"
        "distancia y rumbo desde el centro; 0=norte, 90=oriente, 180=sur, 270=poniente):\n\n"
        + renglones + "\n\n"
        "Elige, usando SOLO esos nombres EXACTOS:\n"
        "- nombre_lugar: la localidad de referencia más cercana y reconocible.\n"
        "- colindancia_n, colindancia_e, colindancia_s, colindancia_o: el asentamiento\n"
        "  más cercano hacia el norte, oriente, sur y poniente, respectivamente.\n"
        "Prefiere asentamientos poblados (pueblo, ranchería, caserío) sobre ranchos\n"
        "aislados cuando estén a distancia parecida.\n\n"
        "Responde ÚNICAMENTE con un objeto JSON con esas 5 claves; usa cadena vacía\n"
        "sólo si de plano no hay asentamiento en ese rumbo.\n"
        'Ejemplo: {"nombre_lugar": "X", "colindancia_n": "X", "colindancia_e": "X", '
        '"colindancia_s": "X", "colindancia_o": "X"}')
    txt = _consulta_llm(prompt, GEO_LLM_TIMEOUT)
    if not txt:
        return None
    corte = txt.find("{")
    if corte < 0:
        return None
    try:
        doc, _ = json.JSONDecoder().raw_decode(txt[corte:])
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    validos = {str(c[2]).strip().lower(): c[2] for c in cand_llm}
    salida: Dict[str, str] = {}
    v = doc.get("nombre_lugar")
    if isinstance(v, str) and v.strip().lower() in validos:
        salida["nombre_lugar"] = validos[v.strip().lower()]
    for sec in ("n", "e", "s", "o"):
        v = doc.get("colindancia_" + sec)
        permitidos = {str(n).strip().lower(): n for n in por_sector[sec]}
        if isinstance(v, str) and v.strip().lower() in permitidos:
            salida["colindancia_" + sec] = permitidos[v.strip().lower()]
    return salida or None


def resolver_toponimia(bbox_wgs: Sequence[float], cache_dir: Path,
                       utm_epsg: Optional[int] = None,
                       radio_km: Optional[float] = None) -> Optional[Dict[str, str]]:
    """Obtiene de OSM (Overpass + Nominatim) el nombre del lugar más cercano,
    el municipio/estado y las colindancias (lugar más próximo por rumbo).

    Devuelve un dict parcial (p. ej. sólo {"municipio": ...}) o None si no hay
    red/datos. Se cachea por centroide para no repetir consultas."""
    oeste, sur, este, norte = bbox_wgs
    lon_c = (oeste + este) / 2.0
    lat_c = (sur + norte) / 2.0
    clave = "%.4f_%.4f" % (round(lat_c, 4), round(abs(lon_c), 4))
    cache_fp = cache_dir / "geo" / (clave + ".json")
    if cache_fp.exists():
        try:
            return json.loads(cache_fp.read_text("utf-8"))
        except Exception:
            pass
    if not DESCARGAR_GEO:
        return None

    resultado: Dict[str, str] = {}

    # 1) Municipio y estado (geocodificación inversa).
    try:
        url = "%s/reverse?format=jsonv2&lat=%.6f&lon=%.6f&zoom=10&addressdetails=1" % (
            GEO_URL_NOMINATIM.rstrip("/"), lat_c, lon_c)
        doc = _json_http(url, timeout=GEO_TIMEOUT)
        addr = doc.get("address") or {}
        mun = addr.get("county") or addr.get("municipality") or addr.get("city")
        est = addr.get("state")
        if mun:
            resultado["municipio"] = mun
        if est:
            resultado["estado"] = est
    except Exception:
        pass

    # 2) Lugares poblados cercanos.
    lugares: List[Tuple[str, float, float, str]] = []
    try:
        radio = radio_km or GEO_RADIO_KM
        consulta = ('[out:json][timeout:%d];node["place"](around:%d,%.6f,%.6f);'
                    "out tags center 300;" % (int(GEO_TIMEOUT), int(radio * 1000.0),
                                              lat_c, lon_c))
        doc = _overpass(consulta, GEO_TIMEOUT)
        for el in (doc or {}).get("elements", []):
            tags = el.get("tags") or {}
            nombre = tags.get("name")
            la, lo = el.get("lat"), el.get("lon")
            if not nombre or la is None or lo is None:
                continue
            lugares.append((str(nombre), float(la), float(lo), tags.get("place", "")))
    except Exception:
        pass

    if lugares:
        if utm_epsg is None:
            utm_epsg = get_utm_epsg(lon_c, lat_c)
        tr = osr.CoordinateTransformation(srs_desde_epsg(4326), srs_desde_epsg(utm_epsg))
        cx, cy, _ = tr.TransformPoint(lon_c, lat_c)
        cand: List[Tuple[float, float, str, str]] = []
        for nombre, la, lo, tipo in lugares:
            x, y, _ = tr.TransformPoint(lo, la)
            dx, dy = x - cx, y - cy
            dist = math.hypot(dx, dy)
            rumbo = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
            cand.append((dist, rumbo, nombre, tipo))
        # El lugar más cercano se elige por DISTANCIA (el tipo sólo desempata).
        cand_ord = sorted(cand, key=lambda c: (c[0], TIPOS_LUGAR.get(c[3], 9)))
        if cand_ord:
            resultado["nombre_lugar"] = cand_ord[0][2]
        # Colindancias: el más cercano por distancia en cada sector cardinal.
        for sec in ("n", "e", "s", "o"):
            en_sec = [c for c in cand if _sector_cardinal(c[1]) == sec]
            if en_sec:
                en_sec.sort(key=lambda c: (c[0], TIPOS_LUGAR.get(c[3], 9)))
                resultado["colindancia_" + sec] = en_sec[0][2]
        # Afinado opcional con el LLM local: elige la localidad de referencia y
        # las colindancias más representativas entre los candidatos cercanos.
        if GEO_LLM:
            try:
                refinado = _refinar_con_llm(cand)
                if refinado:
                    resultado.update(refinado)
            except Exception:
                pass

    if not resultado:
        return None
    try:
        cache_fp.parent.mkdir(parents=True, exist_ok=True)
        cache_fp.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass
    return resultado


# --------------------------------------------------------------------------- #
# Utilidades de dibujo
# --------------------------------------------------------------------------- #

def _path_poligono(ext: Sequence[Tuple[float, float]],
                   huecos: Sequence[Sequence[Tuple[float, float]]]) -> Optional[MplPath]:
    verts: List[Tuple[float, float]] = []
    codes: List[int] = []
    for anillo in [list(ext)] + [list(h) for h in huecos]:
        if len(anillo) < 3:
            continue
        if anillo[0] != anillo[-1]:
            anillo = anillo + [anillo[0]]
        verts.extend(anillo)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(anillo) - 2) + [MplPath.CLOSEPOLY])
    if not verts:
        return None
    return MplPath(verts, codes)


def _color_um(idx: int) -> str:
    return COLORES_UM[idx % len(COLORES_UM)]


# --------------------------------------------------------------------------- #
# Clasificación temática
# --------------------------------------------------------------------------- #

def _norm(texto) -> str:
    """Mayúsculas sin acentos ni espacios repetidos (para comparar campos)."""
    s = unicodedata.normalize("NFKD", str(texto or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.upper().replace("_", " ").split())


def _categoria(catalogo: Sequence[Tuple[str, str, str]], valor) -> Optional[str]:
    v = _norm(valor)
    if not v:
        return None
    for patron, etiqueta, _color in catalogo:
        if re.search(patron, v):
            return etiqueta
    return None


def _color_catalogo(catalogo: Sequence[Tuple[str, str, str]], etiqueta: str) -> Optional[str]:
    for _p, et, color in catalogo:
        if et == etiqueta:
            return color
    return None


def _campo_tematico(polys: Sequence[Dict], candidatos: Sequence[str]) -> Optional[str]:
    """Primer campo del shape (sin distinguir acentos/mayúsculas) con datos."""
    if not polys:
        return None
    claves: Dict[str, str] = {}
    for k in (polys[0].get("attrs") or {}):
        claves[_norm(k)] = k
    for cand in candidatos:
        real = claves.get(_norm(cand))
        if not real:
            continue
        if any((p.get("attrs") or {}).get(real) not in (None, "") for p in polys):
            return real
    return None


def _mascara_poligono(poly: Dict, forma: Tuple[int, int],
                      xlim: Sequence[float], ylim: Sequence[float]):
    """Máscara booleana (subventana) de un polígono sobre la retícula del ráster.

    Devuelve (mascara, fila0, col0) o None. Sólo evalúa la ventana del bbox."""
    ext = poly.get("ext") or []
    nrow, ncol = int(forma[0]), int(forma[1])
    if len(ext) < 3 or nrow < 2 or ncol < 2:
        return None
    minx = min(p[0] for p in ext); maxx = max(p[0] for p in ext)
    miny = min(p[1] for p in ext); maxy = max(p[1] for p in ext)
    ancho = xlim[1] - xlim[0]
    alto = ylim[1] - ylim[0]
    if ancho <= 0 or alto <= 0:
        return None
    c0 = max(0, int((minx - xlim[0]) / ancho * (ncol - 1)))
    c1 = min(ncol - 1, int((maxx - xlim[0]) / ancho * (ncol - 1)) + 1)
    r0 = max(0, int((ylim[1] - maxy) / alto * (nrow - 1)))
    r1 = min(nrow - 1, int((ylim[1] - miny) / alto * (nrow - 1)) + 1)
    if c1 < c0 or r1 < r0:
        return None
    xs = np.linspace(xlim[0], xlim[1], ncol)[c0:c1 + 1]
    ys = np.linspace(ylim[1], ylim[0], nrow)[r0:r1 + 1]
    xx, yy = np.meshgrid(xs, ys)
    pts = np.column_stack([xx.ravel(), yy.ravel()])
    try:
        dentro = MplPath(ext).contains_points(pts).reshape(xx.shape)
    except Exception:
        return None
    return dentro, r0, c0


def _media_en_poligono(poly: Dict, arr: np.ndarray,
                       xlim: Sequence[float], ylim: Sequence[float]) -> Optional[float]:
    m = _mascara_poligono(poly, arr.shape[:2], xlim, ylim)
    if m is None:
        return None
    dentro, r0, c0 = m
    sub = arr[r0:r0 + dentro.shape[0], c0:c0 + dentro.shape[1]]
    if arr.ndim == 3:
        sub = sub.mean(axis=2)
    vals = sub[dentro]
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None
    return float(vals.mean())


def pendiente_de_dem(dem: np.ndarray, px: float, py: float) -> np.ndarray:
    """Pendiente en porcentaje (Horn) a partir del MDE (row 0 = norte)."""
    d = dem.astype(np.float64)
    gy, gx = np.gradient(d, float(py), float(px))
    return np.sqrt(gx * gx + gy * gy) * 100.0


def _exg(sat: np.ndarray) -> np.ndarray:
    """Índice de verdor RGB (Excess Green): 2G − R − B, en unidades de 8 bits."""
    if sat.ndim != 3 or sat.shape[2] < 3:
        return np.zeros(sat.shape[:2], dtype=np.float32)
    r = sat[:, :, 0].astype(np.float32)
    g = sat[:, :, 1].astype(np.float32)
    b = sat[:, :, 2].astype(np.float32)
    return 2.0 * g - r - b


# Clases derivadas (cuando el shape no trae el dato). Nombres explícitos para
# que la leyenda deje claro que NO son un dato de campo.
CLASES_DERIVADAS = {
    "superficies": (
        ("Área de conservación y aprovechamiento restringido", "#1b5e20"),
        ("Área de restauración", "#e0a458"),
        ("Área de producción", "#8bc34a"),
    ),
    "tratamientos": (
        ("Sin acciones", "#cfd8dc"),
    ),
    "vegetacion": (
        ("Vegetación arbórea densa", "#1b5e20"),
        ("Vegetación secundaria arbustiva", "#7cb342"),
        ("Pastizal inducido", "#d4c76a"),
        ("Suelo desnudo o agricultura", "#e0a458"),
    ),
}


def _derivar_etiquetas(ctx: Dict, modo: str) -> List[str]:
    """Clasifica cada polígono con datos reales (MDE / satélite).

    * superficies: pendiente media y altitud media (criterios NOM-152 §4.7.2).
    * vegetacion: índice de verdor (ExG) y brillo del satélite.
    * tratamientos: no hay dato fiable -> 'Sin acciones'."""
    polys = ctx.get("polys") or []
    xlim, ylim = ctx["xlim"], ctx["ylim"]
    if modo == "tratamientos" or not polys:
        return ["Sin acciones"] * len(polys)

    if modo == "superficies":
        dem = ctx.get("dem")
        if dem is None:
            return ["Sin clasificar"] * len(polys)
        ancho_px = dem.shape[1]
        px = (xlim[1] - xlim[0]) / float(ancho_px)
        py = (ylim[1] - ylim[0]) / float(dem.shape[0])
        pend = pendiente_de_dem(dem, px, py)
        salida = []
        for p in polys:
            pm = _media_en_poligono(p, pend, xlim, ylim)
            am = _media_en_poligono(p, dem, xlim, ylim)
            if pm is None:
                salida.append("Sin clasificar")
            elif (am is not None and am > 3000.0) or pm > 100.0:
                salida.append("Área de conservación y aprovechamiento restringido")
            elif pm >= 30.0:
                salida.append("Área de restauración")
            else:
                salida.append("Área de producción")
        return salida

    if modo == "vegetacion":
        sat = ctx.get("sat")
        if sat is None:
            return ["Sin clasificar"] * len(polys)
        exg = _exg(sat)
        salida = []
        for p in polys:
            v = _media_en_poligono(p, exg, xlim, ylim)
            if v is None:
                salida.append("Sin clasificar")
            elif v >= 30.0:
                salida.append("Vegetación arbórea densa")
            elif v >= 15.0:
                salida.append("Vegetación secundaria arbustiva")
            elif v >= 0.0:
                salida.append("Pastizal inducido")
            else:
                salida.append("Suelo desnudo o agricultura")
        return salida

    return ["Sin clasificar"] * len(polys)


def preparar_tema(ctx: Dict, clave: str) -> Dict:
    """Resuelve la clasificación temática de un plano y la deja en ctx['tema']."""
    cfg = TEMAS.get(clave)
    if not cfg:
        ctx["tema"] = None
        return {}
    cache = ctx.setdefault("temas", {})
    if clave in cache:
        ctx["tema"] = cache[clave]
        return cache[clave]
    polys = ctx.get("polys") or []
    campo = _campo_tematico(polys, cfg["campos"])
    origen_campo = False
    if campo:
        etiquetas = []
        for p in polys:
            et = _categoria(cfg["catalogo"], (p.get("attrs") or {}).get(campo))
            etiquetas.append(et or cfg["defecto"])
        origen_campo = True
    else:
        etiquetas = _derivar_etiquetas(ctx, cfg["derivar"])
    colores = []
    respaldo: Dict[str, str] = {}
    for et in etiquetas:
        col = _color_catalogo(cfg["catalogo"], et)
        if col is None:
            for e2, c2 in CLASES_DERIVADAS.get(cfg["derivar"], ()):
                if e2 == et:
                    col = c2
                    break
        if col is None and _norm(et).startswith("SIN "):
            col = "#bdbdbd"
        if col is None:
            if et not in respaldo:
                respaldo[et] = COLORES_UM[len(respaldo) % len(COLORES_UM)]
            col = respaldo[et]
        colores.append(col)
    leyenda: List[Tuple[str, str, float]] = []
    for et, col, p in zip(etiquetas, colores, polys):
        for i, (e2, _, _) in enumerate(leyenda):
            if e2 == et:
                leyenda[i] = (e2, leyenda[i][1], leyenda[i][2] + float(p.get("area_ha") or 0.0))
                break
        else:
            leyenda.append((et, col, float(p.get("area_ha") or 0.0)))
    ctx["tema"] = {
        "clave": clave,
        "titulo": cfg["titulo"],
        "etiquetas": etiquetas,
        "colores": colores,
        "leyenda": leyenda,
        "campo": campo,
        "origen": (("shape · %s" % campo) if origen_campo
                   else "derivada del MDE/satélite"),
    }
    cache[clave] = ctx["tema"]
    return ctx["tema"]


def simbologia_tema(ctx: Dict, clave: str) -> List[Tuple]:
    """Entradas de la leyenda temática (polígonos clasificados)."""
    tema = ctx.get("tema")
    if not tema:
        return []
    return [("poligono", col, et) for et, col, _ha in tema["leyenda"]]


def simbologia_base(ctx: Dict, con_sitios: bool = False) -> List[Tuple]:
    """Entradas de leyenda de las capas base (puntos, líneas)."""
    md = ctx.get("metadatos") or {}
    osm = ctx.get("osm") or {}
    entradas: List[Tuple] = [("punto", "o", "white", "Vértices del predio")]
    if con_sitios and ctx.get("sitios"):
        entradas.insert(0, ("punto", "o", "#e53935", "Sitios de muestreo"))
    if md.get("municipio") and not str(md["municipio"]).startswith("[INSERTE"):
        entradas.append(("punto", "^", "#333333", "Cabecera municipal"))
    if md.get("nombre_lugar") and not str(md["nombre_lugar"]).startswith("[INSERTE"):
        entradas.append(("punto", "s", "#888888", "Localidades"))
    if osm.get("caminos_principales"):
        entradas.append(("linea", "#7f1d1d", (0, (5, 2)), 1.6, "Caminos principales"))
    if osm.get("caminos_generales"):
        entradas.append(("linea", "#7f1d1d", "-", 0.9, "Caminos generales"))
    if osm.get("lineas_transmision"):
        entradas.append(("linea", "#6a1b9a", (0, (1, 1.6)), 1.2, "Líneas de transmisión"))
    if osm.get("cauces_permanentes"):
        entradas.append(("linea", "#1565c0", "-", 1.1, "Cauces permanentes"))
    if osm.get("cauces_temporales"):
        entradas.append(("linea", "#64b5f6", (0, (3, 2)), 0.9, "Cauces temporales"))
    if ctx.get("dem") is not None and ctx.get("niveles") is not None:
        entradas.append(("linea", "#7a5230", "-", 0.7, "Curvas de nivel"))
    return entradas


def _multiplos(vmin: float, vmax: float, intervalo: float) -> List[float]:
    if intervalo <= 0:
        return []
    ini = math.floor(vmin / intervalo) * intervalo
    fin = math.ceil(vmax / intervalo) * intervalo
    vals = []
    v = ini
    while v <= fin + 1e-6:
        if vmin - 1e-9 <= v <= vmax + 1e-9:
            vals.append(v)
        v += intervalo
    return vals


NICE = [10000, 5000, 4000, 3000, 2000, 1500, 1000, 500, 400, 250, 200, 100, 50, 25, 20, 10, 5, 2, 1]


def intervalo_grilla(xlim, ylim, objetivo: float = 3000.0, min_lineas: int = 3) -> float:
    minimo = min(xlim[1] - xlim[0], ylim[1] - ylim[0])
    candidatos = [n for n in NICE if n <= objetivo] or [NICE[-1]]
    for n in candidatos:
        if minimo / n >= min_lineas:
            return float(n)
    return float(candidatos[-1])


def _nice_number(x: float) -> float:
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    for m in (5, 4, 2, 1):
        if m * base <= x:
            return m * base
    return base


def dibujar_cuadricula(ax, xlim, ylim, intervalo: float, etiqueta_fs: float = 5.5) -> None:
    """Malla proyectada: cruces interiores tenues + marcas completas en bordes.

    - Interior: retícula tenue con un pequeño cruce en cada intersección.
    - Bordes: marcas (ticks) completas y coordenadas métricas en los cuatro
      costados; en los costados izquierdo y derecho la rotulación va vertical.
    """
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks([])
    ax.set_yticks([])

    xs = _multiplos(xlim[0], xlim[1], intervalo)
    ys = _multiplos(ylim[0], ylim[1], intervalo)
    ancho = xlim[1] - xlim[0]
    alto = ylim[1] - ylim[0]

    # Retícula tenue al interior.
    for xv in xs:
        ax.axvline(xv, color="0.45", lw=0.35, ls="--", alpha=0.40, zorder=4)
    for yv in ys:
        ax.axhline(yv, color="0.45", lw=0.35, ls="--", alpha=0.40, zorder=4)

    # Cruces en las intersecciones interiores (más marcados que la retícula).
    cruz_x = ancho * 0.011
    cruz_y = alto * 0.011
    for xv in xs:
        if not (xlim[0] < xv < xlim[1]):
            continue
        for yv in ys:
            if not (ylim[0] < yv < ylim[1]):
                continue
            ax.plot([xv - cruz_x, xv + cruz_x], [yv, yv], color="0.25", lw=0.5,
                    alpha=0.75, zorder=4, solid_capstyle="butt")
            ax.plot([xv, xv], [yv - cruz_y, yv + cruz_y], color="0.25", lw=0.5,
                    alpha=0.75, zorder=4, solid_capstyle="butt")

    # Marcas completas y rotulación en los cuatro bordes.
    for xv in xs:
        fx = (xv - xlim[0]) / ancho
        if not (0.03 < fx < 0.97):
            continue
        ax.text(fx, -0.016, "%.0f" % xv, transform=ax.transAxes, ha="center", va="top",
                fontsize=etiqueta_fs, color="0.15")
        ax.text(fx, 1.016, "%.0f" % xv, transform=ax.transAxes, ha="center", va="bottom",
                fontsize=etiqueta_fs, color="0.15")
        ax.plot([fx, fx], [0, 0.012], transform=ax.transAxes, color="black", lw=0.8, clip_on=False)
        ax.plot([fx, fx], [0.988, 1.0], transform=ax.transAxes, color="black", lw=0.8, clip_on=False)
    for yv in ys:
        fy = (yv - ylim[0]) / alto
        if not (0.03 < fy < 0.97):
            continue
        ax.text(-0.014, fy, "%.0f" % yv, transform=ax.transAxes, ha="right", va="center",
                fontsize=etiqueta_fs, color="0.15", rotation=90)
        ax.text(1.014, fy, "%.0f" % yv, transform=ax.transAxes, ha="left", va="center",
                fontsize=etiqueta_fs, color="0.15", rotation=90)
        ax.plot([0, 0.012], [fy, fy], transform=ax.transAxes, color="black", lw=0.8, clip_on=False)
        ax.plot([0.988, 1.0], [fy, fy], transform=ax.transAxes, color="black", lw=0.8, clip_on=False)

    for sp in ax.spines.values():
        sp.set_linewidth(0.8)
        sp.set_color("black")


_NORTE_ARR = None
_NORTE_INTENTADO = False


def _simbolo_norte_qgis():
    """Rasteriza el símbolo del norte que trae QGIS (SVG) sobre fondo transparente.

    Se usan los SVG que instala QGIS (`/usr/share/qgis/svg/arrows/`), los mismos
    del diseñador de planos, para no inventar un símbolo propio. Devuelve un
    arreglo RGBA o ``None`` si no hay PyQGIS/QtSvg disponible.
    """
    global _NORTE_ARR, _NORTE_INTENTADO
    if _NORTE_INTENTADO:
        return _NORTE_ARR
    _NORTE_INTENTADO = True
    try:
        from qgis.PyQt.QtSvg import QSvgRenderer
        from qgis.PyQt.QtGui import QImage, QPainter
        from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, Qt
    except Exception:
        return None
    for nombre in ("NorthArrow_06.svg", "NorthArrow_08.svg", "Arrow_02.svg"):
        ruta = Path("/usr/share/qgis/svg/arrows") / nombre
        try:
            if not ruta.exists():
                continue
            svg = ruta.read_text(encoding="utf-8")
            # QGIS deja marcadores param(...) que QtSvg no interpreta: se fijan a
            # negro para que el símbolo salga con su relleno y su perfil.
            for k, v in (("param(fill-opacity)", "1"),
                         ("param(outline-opacity)", "1"),
                         ("param(outline-width)", "0.6"),
                         ("param(outline)", "#000000"),
                         ("param(fill)", "#000000")):
                svg = svg.replace(k, v)
            render = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            if not render.isValid():
                continue
            img = QImage(240, 240, QImage.Format_ARGB32)
            img.fill(Qt.transparent)
            pintor = QPainter(img)
            pintor.setRenderHint(QPainter.Antialiasing, True)
            render.render(pintor)
            pintor.end()
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            img.save(buf, "PNG")
            arr = plt.imread(io.BytesIO(bytes(buf.data())), format="png")
            buf.close()
            if getattr(arr, "ndim", 0) == 3 and arr.shape[2] == 4:
                _NORTE_ARR = arr
                break
        except Exception:
            continue
    return _NORTE_ARR


def _simbolo_norte(ax, cx: float, cy: float, alto: float) -> None:
    """Dibuja el símbolo del norte centrado en ``(cx, cy)``.

    ``cx``/``cy`` y ``alto`` van en fracción de los ejes; el alto se respeta en
    puntos, así que el símbolo nunca se deforma. Si QGIS no está disponible se
    cae a una aguja partida negro/blanco.
    """
    bb = ax.get_position()
    alto_pt = alto * bb.height * FIG_H * 72.0
    arr = _simbolo_norte_qgis()
    if arr is not None and arr.shape[0] > 0:
        caja = OffsetImage(arr, zoom=alto_pt / float(arr.shape[0]))
        adorno = AnnotationBbox(caja, (cx, cy), xycoords=ax.transAxes,
                                frameon=False, box_alignment=(0.5, 0.5), pad=0.0)
        adorno.set_zorder(10)
        ax.add_artist(adorno)
        return
    # Respaldo: aguja partida (mitad blanca, mitad negra) estilo QGIS.
    k = (bb.height * FIG_H) / max(bb.width * FIG_W, 1e-9)
    r = alto / 2.0
    a = r * k * 0.42
    for signo, relleno in ((-1.0, "white"), (1.0, "black")):
        ax.add_patch(Polygon([(cx, cy + r), (cx + signo * a, cy), (cx, cy - r)],
                             closed=True, facecolor=relleno, edgecolor="black",
                             lw=0.7, transform=ax.transAxes, clip_on=False,
                             zorder=10))


def dibujar_escala_grafica(ax, xlim, ylim, n_seg: int = 4) -> float:
    ancho_m = xlim[1] - xlim[0]
    alto_m = ylim[1] - ylim[0]
    L = _nice_number(ancho_m * 0.28)
    # Esquina inferior derecha del mapa.
    x0 = xlim[1] - ancho_m * 0.055 - L
    y0 = ylim[0] + alto_m * 0.035
    h = alto_m * 0.012
    # Fondo gris tenue: sobre la imagen satelital la barra (con sus tramos
    # blancos) y los números se perdían.
    y_fondo = ylim[0] + alto_m * 0.006
    ax.add_patch(Rectangle((x0 - L * 0.06, y_fondo), L * 1.13,
                           y0 + h * 4.2 - y_fondo, facecolor="#bdbdbd",
                           edgecolor="#4d4d4d", lw=0.5, alpha=0.75, zorder=5.5))
    for i in range(n_seg):
        color = "black" if i % 2 == 0 else "white"
        ax.add_patch(Rectangle((x0 + i * L / n_seg, y0), L / n_seg, h,
                               facecolor=color, edgecolor="black", lw=0.6, zorder=6))
    ax.text(x0, y0 - h * 0.7, "0", ha="center", va="top", fontsize=6, zorder=6)
    ax.text(x0 + L, y0 - h * 0.7, "%.0f m" % L, ha="center", va="top", fontsize=6, zorder=6)
    ax.text(x0 + L / 2.0, y0 + h * 1.6, "Escala gráfica", ha="center", va="bottom", fontsize=6, zorder=6)
    return L


def escala_numerica(ancho_m: float, ancho_axes_in: float) -> str:
    if ancho_axes_in <= 0:
        return "1:?"
    escala = ancho_m / (ancho_axes_in * 0.0254)
    return "1:%.0f" % escala


def _dibujar_mapa_base(ax, ctx: Dict, colorear_por: str = "anualidad") -> None:
    xlim, ylim = ctx["xlim"], ctx["ylim"]

    ax.imshow(ctx["sat"], extent=ctx["sat_extent"], origin="upper", zorder=1,
              interpolation="bilinear")
    if ctx.get("hillshade") is not None:
        ax.imshow(ctx["hillshade"], extent=ctx["sat_extent"], origin="upper",
                  cmap="gray", alpha=0.18, zorder=2, interpolation="bilinear")
    if ctx.get("dem") is not None:
        niveles = ctx.get("niveles")
        if niveles is not None and len(niveles) and float(np.nanmax(ctx["dem"]) - np.nanmin(ctx["dem"])) >= 1.5:
            nrow, ncol = ctx["dem"].shape
            xs = np.linspace(xlim[0], xlim[1], ncol)
            ys = np.linspace(ylim[1], ylim[0], nrow)
            ax.contour(xs, ys, ctx["dem"], levels=niveles, colors="#7a5230",
                       linewidths=0.40, alpha=0.55, zorder=3)

    osm = ctx.get("osm") or {}
    for pol in osm.get("cuerpos_agua", []):
        if len(pol) >= 3:
            ax.add_patch(Polygon(pol, closed=True, facecolor="#4fc3f7",
                                 edgecolor="#1565c0", lw=0.5, alpha=0.55, zorder=3.4))
    for linea in osm.get("cauces_permanentes", []):
        ax.plot([p[0] for p in linea], [p[1] for p in linea],
                color="#1565c0", lw=1.1, alpha=0.9, zorder=5)
    for linea in osm.get("cauces_temporales", []):
        ax.plot([p[0] for p in linea], [p[1] for p in linea],
                color="#64b5f6", lw=0.9, ls=(0, (3, 2)), alpha=0.9, zorder=5)
    for linea in osm.get("caminos_generales", []):
        ax.plot([p[0] for p in linea], [p[1] for p in linea],
                color="#7f1d1d", lw=0.9, ls="-", alpha=0.8, zorder=5)
    for linea in osm.get("caminos_principales", []):
        ax.plot([p[0] for p in linea], [p[1] for p in linea],
                color="#7f1d1d", lw=1.6, ls=(0, (5, 2)), alpha=0.9, zorder=5)
    for linea in osm.get("lineas_transmision", []):
        ax.plot([p[0] for p in linea], [p[1] for p in linea],
                color="#6a1b9a", lw=1.2, ls=(0, (1, 1.6)), alpha=0.9, zorder=5)

    tema = ctx.get("tema") or {}
    colores_tema = tema.get("colores") or []
    for idx, poly in enumerate(ctx["polys"]):
        path = _path_poligono(poly["ext"], poly.get("huecos", []))
        if path is None:
            continue
        if colorear_por == "anualidad":
            color = COLORES_ANUALIDAD.get(poly["attrs"].get("AÑOCORT25"), "#bdbdbd")
            alpha = 0.30
        elif colorear_por == "tema" and idx < len(colores_tema):
            color = colores_tema[idx]
            alpha = 0.50
        elif colorear_por == "um":
            color = _color_um(idx)
            alpha = 0.30
        else:
            color = "#2c7fb8"
            alpha = 0.30
        ax.add_patch(PathPatch(path, facecolor=color, edgecolor="black",
                               lw=1.8, alpha=alpha, zorder=4.5))

    intervalo = ctx.get("intervalo_grilla") or intervalo_grilla(xlim, ylim)
    dibujar_cuadricula(ax, xlim, ylim, intervalo)
    # El símbolo del norte NO va sobre el mapa (la imagen de fondo lo tapa); va
    # en la columna derecha, dentro del bloque "norte + logotipo".
    dibujar_escala_grafica(ax, xlim, ylim)
    _etiquetas_lugares(ax, ctx)


def _guardar(fig, dest_pdf: Path, dest_png: Optional[Path]) -> None:
    dest_pdf.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(str(dest_pdf)) as pdf:
        pdf.savefig(fig, dpi=300)
    if dest_png is not None:
        try:
            fig.savefig(str(dest_png), dpi=150)
        except Exception:
            pass
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Adornos del Plano 1
# --------------------------------------------------------------------------- #

def _tabla_vertices(ax, ctx: Dict) -> None:
    verts = ctx.get("vertices") or []
    if len(verts) < 3:
        return
    bloques = max(1, min(3, int(math.ceil(len(verts) / 26.0))))
    # Tabla compacta: el ancho mínimo lo fija el título (una línea si hay 1 bloque).
    ancho = 0.200 + 0.135 * (bloques - 1)
    ins = ax.inset_axes([0.012, 0.012, ancho, 0.204])
    ins.set_xlim(0, 1)
    ins.set_ylim(0, 1)
    ins.axis("off")
    ins.add_patch(Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="black",
                            lw=0.8, zorder=1, alpha=0.95))
    # Título en dos líneas: así cabe en el recuadro angosto (columnas juntas).
    ins.text(0.5, 0.985, "COORDENADAS DE LOS VÉRTICES",
             ha="center", va="top", fontsize=FS_TITULO_TABLA, fontweight="bold", zorder=2)
    ins.text(0.5, 0.936, "DEL PREDIO",
             ha="center", va="top", fontsize=FS_TITULO_TABLA, fontweight="bold", zorder=2)
    por_bloque = int(math.ceil(len(verts) / float(bloques)))
    col_w = 1.0 / bloques
    for b in range(bloques):
        x0 = b * col_w
        # Los rótulos de columna van ARRIBA de la línea de división (sin tocarla).
        for fx, etiqueta in ((0.20, "VÉRTICE"), (0.52, "COOR. X"), (0.83, "COOR. Y")):
            ins.text(x0 + fx * col_w, 0.885, etiqueta, ha="center", va="top",
                     fontsize=4.4, fontweight="bold", zorder=2)
        ins.plot([x0 + 0.02 * col_w, x0 + 0.98 * col_w], [0.850, 0.850],
                 color="black", lw=0.5, zorder=2)
        for j in range(por_bloque):
            k = b * por_bloque + j
            if k >= len(verts):
                break
            yy = 0.833 - j * (0.816 / max(por_bloque, 1))
            x, y = verts[k]
            ins.text(x0 + 0.20 * col_w, yy, "%d" % (k + 1), ha="center", va="top",
                     fontsize=3.9, zorder=2)
            ins.text(x0 + 0.52 * col_w, yy, "%.1f" % x, ha="center", va="top",
                     fontsize=3.9, zorder=2)
            ins.text(x0 + 0.83 * col_w, yy, "%.1f" % y, ha="center", va="top",
                     fontsize=3.9, zorder=2)


def _etiquetas_lugares(ax, ctx: Dict) -> None:
    """Nombres de lugares (siempre visibles): nombre del predio, colindancias
    y un número al centro de cada geometría (mismo tamaño que el título de la
    tabla de vértices)."""
    md = ctx["metadatos"]
    # Nombre del lugar / predio: banda superior centrada.
    ax.text(0.5, 0.965, md.get("nombre_lugar", ""), transform=ax.transAxes,
            ha="center", va="top", fontsize=7.5, color="black", fontweight="bold",
            zorder=8,
            bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="black",
                      lw=0.5, alpha=0.85))
    # Colindancias (nombres de lugares vecinos) en los cuatro costados.
    for fx, fy, txt in ((0.5, 0.895, md["colindancia_n"]),
                        (0.5, 0.085, md["colindancia_s"]),
                        (0.885, 0.5, md["colindancia_e"]),
                        (0.115, 0.5, md["colindancia_o"])):
        ax.text(fx, fy, str(txt).upper(), transform=ax.transAxes, ha="center",
                va="center", fontsize=5.5, color="black", fontweight="bold",
                zorder=7,
                bbox=dict(boxstyle="round,pad=0.18", fc="white", ec="none",
                          alpha=0.65))
    # Número al centro de cada geometría.
    for idx, poly in enumerate(ctx["polys"]):
        ext = poly["ext"]
        if len(ext) < 3:
            continue
        cx = sum(p[0] for p in ext) / len(ext)
        cy = sum(p[1] for p in ext) / len(ext)
        um = poly["attrs"].get("UM")
        etiqueta = str(um) if um not in (None, "") else str(idx + 1)
        ax.text(cx, cy, etiqueta, ha="center", va="center",
                fontsize=FS_TITULO_TABLA, fontweight="bold", zorder=7,
                bbox=dict(boxstyle="circle,pad=0.20", fc="white", ec="black",
                          lw=0.5, alpha=0.85))


# --------------------------------------------------------------------------- #
# Columna modular derecha (común a los 4 planos)
# --------------------------------------------------------------------------- #

def _col_wrap(texto, fs: float, ancho_pct: float = 0.90) -> List[str]:
    """Trocea el texto para que quepa en el ancho útil de la columna."""
    ancho_pt = ancho_pct * L_SIDE[2] * FIG_W * 72.0
    por_linea = max(12, int(ancho_pt / max(fs * 0.56, 1.0)))
    salida: List[str] = []
    for parrafo in str(texto).split("\n"):
        salida.extend(textwrap.wrap(parrafo, por_linea) or [""])
    return salida


def _col_titulo_bloque(ax, y: float, h: float, texto: str, color: str) -> None:
    ax.text(0.5, y, texto, ha="center", va="top", fontsize=8.4, fontweight="bold",
            color=color, transform=ax.transAxes)
    yy = y - h * 0.56
    ax.plot([0.02, 0.98], [yy, yy], color=color, lw=0.9,
            transform=ax.transAxes, clip_on=False)


def _col_texto(ax, y: float, h: float, texto, fs: float = 6.4, bold: bool = False,
               x: float = 0.04, ha: str = "left", color: str = "black",
               wrap: bool = False, ancho_pct: float = 0.90) -> None:
    lineas = _col_wrap(texto, fs, ancho_pct) if wrap else [str(texto)]
    dy = min(h / max(len(lineas), 1), fs * 1.55 / (L_SIDE[3] * FIG_H * 72.0))
    for i, ln in enumerate(lineas):
        ax.text(x, y - i * dy, ln, ha=ha, va="top", fontsize=fs,
                fontweight="bold" if bold else "normal", color=color,
                transform=ax.transAxes)


def _col_punto(ax, y: float, h: float, marker: str, color: str, etiqueta: str) -> None:
    ax.plot([0.075], [y - h * 0.45], marker=marker, color=color,
            markeredgecolor="black", markersize=5.2, transform=ax.transAxes,
            clip_on=False)
    _col_texto(ax, y, h, etiqueta, fs=6.1, x=0.15)


def _col_linea(ax, y: float, h: float, color: str, ls, lw: float, etiqueta: str) -> None:
    ax.plot([0.04, 0.125], [y - h * 0.45, y - h * 0.45], color=color, lw=lw, ls=ls,
            transform=ax.transAxes)
    _col_texto(ax, y, h, etiqueta, fs=6.1, x=0.155)


def _col_poligono(ax, y: float, h: float, color: str, etiqueta: str) -> None:
    ax.add_patch(Rectangle((0.045, y - h * 0.62), 0.062, h * 0.44,
                           facecolor=color, edgecolor="black", lw=0.4, alpha=0.75,
                           transform=ax.transAxes))
    _col_texto(ax, y, h, etiqueta, fs=6.1, x=0.145)


def _col_norte_logo(ax, y: float, h: float, md: Dict) -> None:
    """Símbolo del norte (el que trae QGIS) con la letra N encima y el logotipo.

    La N va arriba y el símbolo debajo, con un hueco explícito, para que no se
    empalmen (defecto de la versión anterior).
    """
    cx = 0.12
    alto = h * 0.52
    fs_n = min(9.5, h * FIG_H * L_SIDE[3] * 72.0 * 0.26)
    ax.text(cx, y - h * 0.02, "N", ha="center", va="top", fontsize=fs_n,
            fontweight="bold", transform=ax.transAxes)
    _simbolo_norte(ax, cx, y - h * 0.63, alto)
    _col_texto(ax, y, h, md.get("logo", ""), fs=5.6, x=0.47, color="#333333",
               wrap=True, ancho_pct=0.50)


def columna_modular(ax, ctx: Dict, n_plano: int, titulo_mapa: str,
                    simbologia: Sequence[Tuple],
                    nota_leyenda: Optional[str] = None) -> None:
    """Columna cartográfica derecha, modular y común a los cuatro planos.

    Bloques: encabezado (PLANO N + título general), núcleo agrario / municipio,
    norte + logotipo, referencia geográfica, simbología, tenencia y superficie,
    marco hidrológico, elaboración y pie. Cada bloque recibe un alto
    proporcional a su peso, de modo que la leyenda puede crecer sin desbordar
    la hoja (el alto del texto nunca excede el renglón legible).
    """
    ax.axis("off")
    md = ctx["metadatos"]
    color = "#14532d"
    esc = ctx.get("escala_txt", "1:?")
    items: List[Tuple[float, str, Dict]] = []

    def add(peso: float, tipo: str, **kw) -> None:
        items.append((float(peso), tipo, kw))

    # ---- Encabezado ------------------------------------------------------
    add(1.7, "plano", texto="PLANO %d" % n_plano)
    add(1.4, "texto", texto=titulo_mapa, fs=8.6, bold=True, x=0.5, ha="center",
        wrap=True, ancho_pct=0.96, color=color)
    add(0.9, "texto", texto="NÚCLEO AGRARIO / PREDIO", fs=5.6, x=0.5, ha="center")
    add(1.3, "texto", texto=md["titulo_lienzo"], fs=8.8, bold=True, x=0.5,
        ha="center", wrap=True, ancho_pct=0.96)
    add(0.9, "texto", texto="MUNICIPIO / ESTADO", fs=5.6, x=0.5, ha="center")
    add(1.3, "texto", texto="%s, %s" % (md["municipio"], md["estado"]), fs=7.8,
        bold=True, x=0.5, ha="center", wrap=True, ancho_pct=0.96)
    add(3.0, "norte_logo")

    # ---- Referencia geográfica -------------------------------------------
    add(1.8, "titulo_bloque", texto="REFERENCIA GEOGRÁFICA")
    add(0.95, "texto", texto="Proyección: UTM")
    add(0.95, "texto", texto="Zona UTM: %d (%s)" % (
        ctx["zona_utm"], "Norte" if ctx["hemisferio_norte"] else "Sur"))
    add(0.95, "texto", texto="Datum / Elipsoide: %s" % ctx["datum"])
    add(0.95, "texto", texto="EPSG: %d" % ctx["utm_epsg"])
    add(0.95, "texto", texto="Escala numérica: %s" % esc, bold=True)

    # ---- Simbología -------------------------------------------------------
    add(1.8, "titulo_bloque", texto="SIMBOLOGÍA")
    if nota_leyenda:
        add(0.9, "texto", texto=nota_leyenda, fs=5.4, color="#555555", wrap=True)
    for entrada in (simbologia or []):
        tipo = entrada[0]
        if tipo == "punto" and len(entrada) >= 4:
            add(0.95, "punto", marker=entrada[1], color=entrada[2], etiqueta=entrada[3])
        elif tipo == "linea" and len(entrada) >= 5:
            add(0.95, "linea", color=entrada[1], ls=entrada[2], lw=entrada[3],
                etiqueta=entrada[4])
        elif tipo == "poligono" and len(entrada) >= 3:
            add(0.95, "poligono", color=entrada[1], etiqueta=entrada[2])
        elif tipo == "subtitulo" and len(entrada) >= 2:
            add(0.95, "texto", texto=entrada[1], fs=6.0, bold=True)
        elif tipo == "texto" and len(entrada) >= 2:
            add(0.95, "texto", texto=entrada[1], fs=6.1)

    # ---- Tenencia y superficie -------------------------------------------
    add(1.8, "titulo_bloque", texto="TENENCIA Y SUPERFICIE")
    add(0.95, "texto", texto="Superficie legal: %s" % md["superficie_legal"], wrap=True)
    add(0.95, "texto", texto="Superficie en posesión actual:", wrap=True)
    add(0.95, "texto", texto=md["superficie_posesion"], wrap=True)
    add(0.95, "texto", texto="Superficie calculada (SIG): %.4f ha" % ctx["superficie_ha"],
        bold=True)
    add(0.95, "texto", texto="Geometrías (UMM/rodales): %d" % len(ctx["polys"]))

    # ---- Marco hidrológico ------------------------------------------------
    add(1.8, "titulo_bloque", texto="MARCO HIDROLÓGICO")
    add(0.95, "texto", texto="Región hidrológica: %s" % md.get("region_hidrologica", ""),
        wrap=True)
    add(0.95, "texto", texto="Cuenca / subcuenca: %s" % md.get(
        "cuenca_hidrologica", PLACEHOLDER("cuenca y subcuenca")), wrap=True)

    # ---- Elaboración ------------------------------------------------------
    add(1.8, "titulo_bloque", texto="ELABORACIÓN")
    add(0.95, "texto", texto=md["institucion"], fs=6.1, wrap=True)
    add(0.95, "texto", texto=md["contacto"], fs=6.0, wrap=True)
    add(0.95, "texto",
        texto="Fuente de fondo: imagen satelital y MDE de servicios públicos",
        fs=5.5, wrap=True)
    add(0.95, "texto", texto="Sistema: SIAF", fs=5.5)

    # Los textos que se parten en varios renglones consumen más espacio.
    for i, (peso, tipo, kw) in enumerate(items):
        if tipo == "texto" and kw.get("wrap"):
            n = len(_col_wrap(kw["texto"], kw.get("fs", 6.4), kw.get("ancho_pct", 0.90)))
            if n > 1:
                items[i] = (peso * n, tipo, kw)

    total = sum(p for p, _, _ in items) or 1.0
    y = 0.996
    paso = 0.992 / total
    for peso, tipo, kw in items:
        h = paso * peso
        if tipo == "plano":
            ax.text(0.5, y, kw["texto"], ha="center", va="top", fontsize=14,
                    fontweight="bold", color=color, transform=ax.transAxes)
            ax.plot([0.02, 0.98], [y - h * 0.80, y - h * 0.80], color=color, lw=1.3,
                    transform=ax.transAxes, clip_on=False)
        elif tipo == "titulo_bloque":
            _col_titulo_bloque(ax, y, h, kw["texto"], color)
        elif tipo == "texto":
            _col_texto(ax, y, h, kw["texto"], fs=kw.get("fs", 6.4),
                       bold=kw.get("bold", False), x=kw.get("x", 0.04),
                       ha=kw.get("ha", "left"), color=kw.get("color", "black"),
                       wrap=kw.get("wrap", False),
                       ancho_pct=kw.get("ancho_pct", 0.90))
        elif tipo == "norte_logo":
            _col_norte_logo(ax, y, h, md)
        elif tipo == "punto":
            _col_punto(ax, y, h, kw["marker"], kw["color"], kw["etiqueta"])
        elif tipo == "linea":
            _col_linea(ax, y, h, kw["color"], kw["ls"], kw["lw"], kw["etiqueta"])
        elif tipo == "poligono":
            _col_poligono(ax, y, h, kw["color"], kw["etiqueta"])
        y -= h


def simbologia_plano1(ctx: Dict) -> List[Tuple]:
    """Leyenda del plano 1: plan de aprovechamiento por anualidad + capas base."""
    entradas: List[Tuple] = [("subtitulo", "Plan de aprovechamiento (por anualidad):")]
    anios: List = []
    hay_sin = False
    for p in ctx["polys"]:
        a = (p.get("attrs") or {}).get("AÑOCORT25")
        if a in (None, ""):
            hay_sin = True
        elif a not in anios:
            anios.append(a)
    try:
        anios.sort()
    except Exception:
        pass
    for a in anios:
        entradas.append(("poligono", COLORES_ANUALIDAD.get(a, "#bdbdbd"),
                         "Periodo %s" % a))
    if hay_sin or not anios:
        entradas.append(("poligono", COLORES_ANUALIDAD[None], "No programado"))
    entradas.append(("subtitulo", "Tratamiento silvicultural:"))
    entradas.append(("texto", "Corta de selección / Sin tratamiento"))
    entradas.extend(simbologia_base(ctx, con_sitios=False))
    return entradas


def _dibujar_sitios(ax, ctx: Dict) -> None:
    """Sitios de muestreo: círculo de 1,000 m² + punto rojo/naranja."""
    sitios = ctx.get("sitios") or []
    if not sitios:
        return
    for (x, y) in sitios:
        ax.add_patch(Circle((x, y), 17.84, facecolor="#ff7043", edgecolor="#b71c1c",
                            lw=0.5, alpha=0.45, zorder=6))
    ax.scatter([p[0] for p in sitios], [p[1] for p in sitios], s=7, c="#d32f2f",
               edgecolors="black", linewidths=0.3, zorder=7)


# --------------------------------------------------------------------------- #
# Planos
# --------------------------------------------------------------------------- #

def _encabezado(fig, ctx: Dict, titulo: str, subtitulo: str) -> None:
    fig.text(0.045, 0.965, titulo, ha="left", va="center", fontsize=17,
             fontweight="bold", color="#14532d")
    fig.text(0.045, 0.935, subtitulo, ha="left", va="center", fontsize=10.5,
             color="#333333")


def _ctx_escala(ctx: Dict) -> str:
    ancho_axes_in = L_MAPA[2] * FIG_W
    return escala_numerica(ctx["xlim"][1] - ctx["xlim"][0], ancho_axes_in)


def render_plano_01(dest_pdf: Path, dest_png: Path, ctx: Dict) -> None:
    ctx["escala_txt"] = _ctx_escala(ctx)
    fig = plt.figure(figsize=(FIG_W, FIG_H))
    ax_m = fig.add_axes(L_MAPA)
    ax_s = fig.add_axes(L_SIDE)
    _dibujar_mapa_base(ax_m, ctx, colorear_por="anualidad")
    _tabla_vertices(ax_m, ctx)
    columna_modular(ax_s, ctx, 1, "Localización y plan de aprovechamiento",
                    simbologia_plano1(ctx))
    md = ctx["metadatos"]
    _encabezado(fig, ctx, "PLANO 1",
                "Localización y plan de aprovechamiento — %s" % md["titulo_lienzo"])
    _guardar(fig, dest_pdf, dest_png)


def render_plano_02(dest_pdf: Path, dest_png: Path, ctx: Dict) -> None:
    ctx["escala_txt"] = _ctx_escala(ctx)
    tema = preparar_tema(ctx, "plano_02")
    fig = plt.figure(figsize=(FIG_W, FIG_H))
    ax_m = fig.add_axes(L_MAPA)
    ax_s = fig.add_axes(L_SIDE)
    _dibujar_mapa_base(ax_m, ctx, colorear_por="tema")
    _tabla_vertices(ax_m, ctx)
    columna_modular(ax_s, ctx, 2, "Clasificación de superficies",
                    simbologia_tema(ctx, "plano_02") + simbologia_base(ctx),
                    nota_leyenda="Origen: %s" % tema.get("origen", ""))
    md = ctx["metadatos"]
    _encabezado(fig, ctx, "PLANO 2",
                "Clasificación de superficies — %s" % md["titulo_lienzo"])
    _guardar(fig, dest_pdf, dest_png)


def render_plano_03(dest_pdf: Path, dest_png: Path, ctx: Dict) -> None:
    ctx["escala_txt"] = _ctx_escala(ctx)
    tema = preparar_tema(ctx, "plano_03")
    fig = plt.figure(figsize=(FIG_W, FIG_H))
    ax_m = fig.add_axes(L_MAPA)
    ax_s = fig.add_axes(L_SIDE)
    _dibujar_mapa_base(ax_m, ctx, colorear_por="tema")
    _dibujar_sitios(ax_m, ctx)
    _tabla_vertices(ax_m, ctx)
    n_sitios = len(ctx.get("sitios") or [])
    columna_modular(ax_s, ctx, 3, "Tratamientos complementarios y sitios de muestreo",
                    simbologia_tema(ctx, "plano_03") + simbologia_base(ctx, con_sitios=True),
                    nota_leyenda="Tratamientos: %s · Sitios de muestreo: %d (sintéticos)"
                                 % (tema.get("origen", ""), n_sitios))
    md = ctx["metadatos"]
    _encabezado(fig, ctx, "PLANO 3",
                "Tratamientos complementarios y sitios de muestreo — %s" % md["titulo_lienzo"])
    _guardar(fig, dest_pdf, dest_png)


def render_plano_04(dest_pdf: Path, dest_png: Path, ctx: Dict) -> None:
    ctx["escala_txt"] = _ctx_escala(ctx)
    tema = preparar_tema(ctx, "plano_04")
    fig = plt.figure(figsize=(FIG_W, FIG_H))
    ax_m = fig.add_axes(L_MAPA)
    ax_s = fig.add_axes(L_SIDE)
    _dibujar_mapa_base(ax_m, ctx, colorear_por="tema")
    _tabla_vertices(ax_m, ctx)
    columna_modular(ax_s, ctx, 4, "Uso de suelo y vegetación",
                    simbologia_tema(ctx, "plano_04") + simbologia_base(ctx),
                    nota_leyenda="Origen: %s" % tema.get("origen", ""))
    md = ctx["metadatos"]
    _encabezado(fig, ctx, "PLANO 4",
                "Uso de suelo y vegetación — %s" % md["titulo_lienzo"])
    _guardar(fig, dest_pdf, dest_png)


PLANOS = [
    ("plano_01", "plano_01_localizacion_aprovechamiento", render_plano_01),
    ("plano_02", "plano_02_clasificacion_superficies", render_plano_02),
    ("plano_03", "plano_03_tratamientos_sitios_muestreo", render_plano_03),
    ("plano_04", "plano_04_uso_suelo_vegetacion", render_plano_04),
]


# --------------------------------------------------------------------------- #
# Exportaciones (tabla de atributos y proyecto QGIS)
# --------------------------------------------------------------------------- #

def atributos_a_csv_y_xlsx(filas: List[Dict], campos: List[str],
                           dest_csv: Path, dest_xlsx: Optional[Path]) -> None:
    dest_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(campos)
        for fila in filas:
            w.writerow(["" if fila.get(c) is None else fila.get(c) for c in campos])
    if dest_xlsx is not None:
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "atributos"
            ws.append(campos)
            for fila in filas:
                ws.append(["" if fila.get(c) is None else fila.get(c) for c in campos])
            wb.save(str(dest_xlsx))
        except Exception:
            pass


def guardar_geotiff(arr: np.ndarray, dest: Path, bounds: Sequence[float],
                    epsg: int) -> bool:
    """Guarda un arreglo (H,W) o (H,W,B) georreferenciado (row 0 = norte)."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        xmin, ymin, xmax, ymax = bounds
        if arr.ndim == 2:
            alto, ancho = arr.shape
            bandas = 1
        else:
            alto, ancho, bandas = arr.shape
        dtype = gdal.GDT_Byte if arr.dtype == np.uint8 else gdal.GDT_Float32
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(str(dest), ancho, alto, bandas, dtype)
        if ds is None:
            return False
        px = (xmax - xmin) / ancho
        py = (ymax - ymin) / alto
        ds.SetGeoTransform((xmin, px, 0.0, ymax, 0.0, -py))
        ds.SetProjection(srs_desde_epsg(epsg).ExportToWkt())
        for b in range(bandas):
            banda = ds.GetRasterBand(b + 1)
            banda.WriteArray(arr if bandas == 1 else arr[:, :, b])
            banda.FlushCache()
        ds = None
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Capas vectoriales derivadas (para edición en QGIS)
# --------------------------------------------------------------------------- #

def _poligono_ogr(poly: Dict) -> Optional[ogr.Geometry]:
    ext = poly.get("ext") or []
    if len(ext) < 3:
        return None
    anillo = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ext:
        anillo.AddPoint_2D(float(x), float(y))
    anillo.CloseRings()
    g = ogr.Geometry(ogr.wkbPolygon)
    g.AddGeometry(anillo)
    for hueco in (poly.get("huecos") or []):
        if len(hueco) < 3:
            continue
        r = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in hueco:
            r.AddPoint_2D(float(x), float(y))
        r.CloseRings()
        g.AddGeometry(r)
    return g


def _linea_ogr(pts: Sequence[Tuple[float, float]]) -> Optional[ogr.Geometry]:
    if len(pts) < 2:
        return None
    g = ogr.Geometry(ogr.wkbLineString)
    for x, y in pts:
        g.AddPoint_2D(float(x), float(y))
    return g


def _crear_capa_geojson(dest: Path, nombre: str, geom_tipo: int,
                        campos: Sequence[Tuple[str, int]], epsg: int):
    dest.parent.mkdir(parents=True, exist_ok=True)
    drv = ogr.GetDriverByName("GeoJSON")
    if drv is None:
        return None, None
    if dest.exists():
        drv.DeleteDataSource(str(dest))
    ds = drv.CreateDataSource(str(dest))
    if ds is None:
        return None, None
    ly = ds.CreateLayer(nombre, srs_desde_epsg(epsg), geom_tipo)
    for campo, tipo in campos:
        ly.CreateField(ogr.FieldDefn(campo, tipo))
    return ds, ly


def escribir_capas_geo(ctx: Dict, dest_dir: Path, utm_epsg: int) -> List[str]:
    """Escribe las capas derivadas (temática, OSM y sitios) como GeoJSON.

    Son las capas que se cargan en el proyecto QGIS de cada plano para poder
    revisarlas y editarlas (no se guardan datos sensibles: sólo geometrías
    generadas o clasificaciones del propio shape / MDE / satélite).
    """
    escritas: List[str] = []

    # Temática de cada plano (polígonos clasificados).
    for clave in TEMAS:
        tema = (ctx.get("temas") or {}).get(clave)
        if not tema:
            continue
        dest = dest_dir / ("tematica_%s.geojson" % clave)
        ds, ly = _crear_capa_geojson(
            dest, clave, ogr.wkbPolygon,
            [("UM", ogr.OFTString), ("CATEGORIA", ogr.OFTString),
             ("COLOR", ogr.OFTString), ("AREA_HA", ogr.OFTReal)], utm_epsg)
        if ds is None:
            continue
        for i, poly in enumerate(ctx.get("polys") or []):
            g = _poligono_ogr(poly)
            if g is None:
                continue
            um = (poly.get("attrs") or {}).get("UM")
            f = ogr.Feature(ly.GetLayerDefn())
            f.SetField("UM", str(um if um not in (None, "") else i + 1))
            f.SetField("CATEGORIA", str(tema["etiquetas"][i])
                       if i < len(tema["etiquetas"]) else "")
            f.SetField("COLOR", str(tema["colores"][i])
                       if i < len(tema["colores"]) else "")
            f.SetField("AREA_HA", float(poly.get("area_ha") or 0.0))
            f.SetGeometry(g)
            ly.CreateFeature(f)
            f = None
        ds = None
        escritas.append(str(dest))

    # Capas de OpenStreetMap.
    osm = ctx.get("osm") or {}
    for nombre, clave in (("osm_caminos_principales", "caminos_principales"),
                          ("osm_caminos_generales", "caminos_generales"),
                          ("osm_cauces_permanentes", "cauces_permanentes"),
                          ("osm_cauces_temporales", "cauces_temporales"),
                          ("osm_lineas_transmision", "lineas_transmision"),
                          ("osm_cuerpos_agua", "cuerpos_agua")):
        trazos = osm.get(clave) or []
        if not trazos:
            continue
        es_pol = (clave == "cuerpos_agua")
        ds, ly = _crear_capa_geojson(dest_dir / ("%s.geojson" % nombre), nombre,
                                     ogr.wkbPolygon if es_pol else ogr.wkbLineString,
                                     [("TIPO", ogr.OFTString)], utm_epsg)
        if ds is None:
            continue
        for pts in trazos:
            if es_pol:
                anillo = ogr.Geometry(ogr.wkbLinearRing)
                for x, y in pts:
                    anillo.AddPoint_2D(float(x), float(y))
                anillo.CloseRings()
                g = ogr.Geometry(ogr.wkbPolygon)
                g.AddGeometry(anillo)
            else:
                g = _linea_ogr(pts)
            if g is None:
                continue
            f = ogr.Feature(ly.GetLayerDefn())
            f.SetField("TIPO", clave)
            f.SetGeometry(g)
            ly.CreateFeature(f)
            f = None
        ds = None
        escritas.append(str(dest_dir / ("%s.geojson" % nombre)))

    # Sitios de muestreo (sintéticos mientras no llegue el CSV real).
    sitios = ctx.get("sitios") or []
    if sitios:
        dest = dest_dir / "sitios_muestreo.geojson"
        ds, ly = _crear_capa_geojson(dest, "sitios_muestreo", ogr.wkbPoint,
                                     [("ID", ogr.OFTInteger),
                                      ("AREA_M2", ogr.OFTReal)], utm_epsg)
        if ds is not None:
            for i, (x, y) in enumerate(sitios):
                g = ogr.Geometry(ogr.wkbPoint)
                g.AddPoint_2D(float(x), float(y))
                f = ogr.Feature(ly.GetLayerDefn())
                f.SetField("ID", i + 1)
                f.SetField("AREA_M2", 1000.0)
                f.SetGeometry(g)
                ly.CreateFeature(f)
                f = None
            ds = None
            escritas.append(str(dest))

    return escritas


def escribir_proyecto_qgis(dest_qgz: Path, capas: Sequence[str],
                           crs_epsg: int) -> bool:
    """Escribe un proyecto QGIS (.qgz) con las capas indicadas."""
    try:
        from qgis.core import (QgsProject, QgsVectorLayer, QgsRasterLayer,
                               QgsCoordinateReferenceSystem)
    except Exception:
        return False
    try:
        proyecto = QgsProject.instance()
        proyecto.clear()
        for ruta in capas:
            ruta_s = str(ruta)
            if not Path(ruta_s).exists():
                continue
            nombre = Path(ruta_s).stem
            if ruta_s.lower().endswith((".shp", ".gpkg", ".geojson", ".json")):
                capa = QgsVectorLayer(ruta_s, nombre, "ogr")
            else:
                capa = QgsRasterLayer(ruta_s, nombre, "gdal")
            if capa.isValid():
                proyecto.addMapLayer(capa)
        proyecto.setCrs(QgsCoordinateReferenceSystem.fromEpsgId(int(crs_epsg)))
        dest_qgz.parent.mkdir(parents=True, exist_ok=True)
        return bool(proyecto.write(str(dest_qgz)))
    except Exception:
        return False
