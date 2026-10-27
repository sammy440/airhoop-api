# AirHoops API: FastAPI + Uvicorn. One instance (rooms live in memory; see README "Deploy").
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8000
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install ".[monitoring]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY start.sh ./
RUN useradd --create-home api && chmod +x start.sh
USER api

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"
CMD ["./start.sh"]
