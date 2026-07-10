FROM node:22-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY alembic.ini ./
COPY --from=frontend-builder /frontend/dist ./frontend/dist
RUN mkdir -p /app/data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8080 --workers 1"]
