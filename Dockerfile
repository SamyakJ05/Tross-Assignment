FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the application package, rather than relying on the working
# directory being importable at runtime.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Non-root. Nothing here needs privileges.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Render supplies $PORT; the default keeps local `docker run` working.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
