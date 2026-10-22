FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OPSDECK_HOST=0.0.0.0 OPSDECK_PORT=8080 OPSDECK_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY opsdeck ./opsdeck
RUN pip install . && useradd --uid 10001 --create-home opsdeck && mkdir -p /data && chown opsdeck /data

USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz'); sys.exit(0)"
CMD ["opsdeck"]
