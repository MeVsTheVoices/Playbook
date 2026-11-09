FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/callback_site \
    CALLBACK_APPDATA_DIR=/data \
    FLASH_WORK_DIR=/work \
    FLASH_BIND=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates cloud-init curl mount openssl parted udev util-linux xz-utils xorriso \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY callback_site/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY callback_site/ /app/callback_site/
COPY scripts/ /app/scripts/
COPY flash_helper/ /app/flash_helper/
COPY inventory.yml /app/inventory.yml
RUN mkdir -p /data /work && chmod 700 /data /work
WORKDIR /work

EXPOSE 8090
STOPSIGNAL SIGTERM
CMD ["python", "-m", "flash_helper.server"]
