# Local/self-hosted container. Production on Render uses the native Python runtime and
# render.yaml (build: pip install + alembic upgrade head; start: uvicorn), not this file.
# Python matches .python-version / CI / Render (3.12.15).
FROM python:3.12.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
# Same wheel-only install as Render (psycopg2-binary, lxml, rapidfuzz ship wheels)
RUN pip install --no-cache-dir --only-binary :all: -r requirements.txt

COPY . .

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

ENV PORT=8000
EXPOSE 8000

# Same sequence as Render: migrate, then serve
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
