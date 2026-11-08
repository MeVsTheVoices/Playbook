FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CALLBACK_APPDATA_DIR=/data \
    ANSIBLE_CONFIG=/app/ansible.cfg \
    PYTHONPATH=/app/callback_site \
    HOST=0.0.0.0 \
    PORT=8088

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        iputils-ping \
        openssh-client \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-ansible.txt /tmp/requirements-ansible.txt
COPY callback_site/requirements.txt /tmp/requirements-callback.txt
RUN python -m pip install \
        -r /tmp/requirements-ansible.txt \
        -r /tmp/requirements-callback.txt \
        gunicorn==23.0.0

COPY ansible.cfg inventory.yml /app/
COPY collections/requirements.yml /tmp/collections-requirements.yml
COPY callback_site/ /app/callback_site/
COPY scripts/ /app/scripts/
COPY roles/ /app/roles/
COPY library/ /app/library/
COPY group_vars/ /app/group_vars/
COPY host_vars/ /app/host_vars/

RUN mkdir -p /app/collections /data \
    && ansible-galaxy collection install \
        -r /tmp/collections-requirements.yml \
        -p /app/collections \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && chown -R app:app /data /home/app

USER app

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8088/health', timeout=3)"

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8088", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
