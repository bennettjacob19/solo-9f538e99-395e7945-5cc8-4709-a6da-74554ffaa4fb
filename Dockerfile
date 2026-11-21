FROM python:3.11-slim

# GCC is the worker behind /compile; it depends on binutils (nm, readelf,
# ar, ld) which the /trim firmware library-trimming API parses artifacts with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev binutils \
    && rm -rf /var/lib/apt/lists/* \
    && nm --version | head -1 && readelf --version | head -1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Persistent cache directory (mounted as a volume in docker-compose).
ENV CCACHE_CACHE_DIR=/data/cache
RUN mkdir -p /data/cache

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
