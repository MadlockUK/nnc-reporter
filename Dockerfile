# NNC Reporter web app. The Playwright base image carries Chromium and its
# system libraries; the Python package is pinned to the same version.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NNC_DATA_DIR=/data \
    NNC_HOST=0.0.0.0 \
    PORT=8712 \
    NNC_SUBMIT_MODE=server

WORKDIR /app
COPY requirements.txt .
RUN pip install --break-system-packages -r requirements.txt "playwright==1.63.0"

COPY . .
RUN mkdir -p /data && chown -R pwuser:pwuser /data
USER pwuser
VOLUME ["/data"]
EXPOSE 8712
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8712/healthz', timeout=4)" || exit 1
CMD ["python", "app.py"]
