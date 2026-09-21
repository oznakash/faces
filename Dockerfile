# Hosted profile: search only. No Playwright, no Chromium — galleries are indexed
# locally and published here with publish.sh. Models download to the /data volume
# on first start (~280 MB) and persist.
FROM python:3.12-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements-server.txt /tmp/
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements-server.txt

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
WORKDIR /app
COPY faces ./faces
COPY static ./static
COPY config ./config
ENV FACES_DATA=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "faces.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
