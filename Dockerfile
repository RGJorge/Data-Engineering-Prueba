FROM python:3.11-slim

WORKDIR /app

# Dependencies first for layer caching. Pillow needs no system libs for the
# formats we validate (jpeg/png) on slim since wheels bundle them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Unbuffered logs so progress is visible under `docker compose up`.
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    OUTPUT_DIR=/output

CMD ["python", "-m", "src.run_pipeline"]
