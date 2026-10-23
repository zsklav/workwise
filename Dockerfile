FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --disable-pip-version-check -r requirements.txt \
    && groupadd --gid 10001 workwise \
    && useradd --uid 10001 --gid 10001 --no-create-home workwise \
    && mkdir /app/performance-results \
    && chown 10001:10001 /app/performance-results

COPY main.py ./
COPY workwise/ ./workwise/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY alembic.ini ./

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"
CMD ["python", "-m", "uvicorn", "workwise.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
