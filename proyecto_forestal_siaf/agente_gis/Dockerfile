FROM docker.io/library/ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV QT_QPA_PLATFORM=offscreen

WORKDIR /app

# Instalamos librerías actualizadas para Ubuntu 24.04 ARM
# python3-qgis aporta GDAL/OGR/PyQGIS; numpy/matplotlib/pil para el render
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-qgis \
    python3-numpy \
    python3-matplotlib \
    python3-pil \
    libgl1 \
    libglib2.0-0 \
    libxrender1 \
    libxext6 \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# Configurar librerías de Python para el microservicio
RUN pip install --no-cache-dir --break-system-packages fastapi uvicorn

COPY main.py cartografia.py ./

# Variables de entorno críticas para la arquitectura ARM64 de Oracle
ENV PYTHONPATH=/usr/lib/python3/dist-packages
ENV LD_LIBRARY_PATH=/usr/lib
ENV QT_QPA_PLATFORM=offscreen

EXPOSE 8081
CMD ["python3", "main.py"]
