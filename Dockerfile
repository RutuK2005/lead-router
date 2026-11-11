FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/lead_router.db

WORKDIR /app

# Install dependencies first so this layer is cached between code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "lead_router.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
