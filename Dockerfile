FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QM_CONTAINER=1 QM_SETTINGS_FILE=/data/local-settings.ps1 QM_WEB_PORT=8787
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 qm && useradd --uid 10001 --gid 10001 --no-create-home qm \
    && mkdir -p /data && chown qm:qm /data && chmod 700 /data
COPY --chown=qm:qm *.py ./
COPY --chown=qm:qm static/ ./static/
USER qm
EXPOSE 8787
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8787/api/health',timeout=2)); assert d.get('auth_configured')"
ENTRYPOINT ["python", "start.py"]
