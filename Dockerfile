# Hosted profile: search only. No Playwright, no Chromium — galleries are indexed
# locally and published here with publish.sh. Models download to the /data volume
# on first start (~280 MB) and persist.
FROM python:3.12-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements-server.txt /tmp/
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements-server.txt

FROM python:3.12-slim
# libgomp1: ONNX Runtime. The rest: insightface drags in the full opencv-python next to the
# headless build, and that cv2 links X11/GL at import time (first seen: libxcb.so.1 missing).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgomp1 libgl1 libglib2.0-0 libxcb1 libx11-6 libxext6 libsm6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
RUN python -c "import cv2, onnxruntime, insightface, fastapi, PIL, pillow_heif; print('imports ok', cv2.__version__)"
WORKDIR /app
COPY faces ./faces
COPY static ./static
COPY config ./config
ENV FACES_DATA=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "faces.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
