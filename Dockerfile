FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# chdman (MAME tools) for CD/DVD -> CHD, 7-Zip for .7z sets, DejaVu for the product code on generated cart labels.
RUN apt-get update && apt-get install -y --no-install-recommends mame-tools 7zip fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /root/.cache

# nobody:users by default; override with `user:` in compose.
USER 99:100
VOLUME /config
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)"]

CMD ["python", "-m", "hakobiya"]
