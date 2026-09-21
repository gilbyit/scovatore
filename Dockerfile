FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scovatore/ ./scovatore/

# cacce/, dati/ e data/ arrivano come bind mount dal compose
ENV SCOVATORE_DATA_DIR=/app/data SCOVATORE_HUNTS_DIR=/app/cacce

CMD ["python", "-m", "scovatore", "loop", "--intervallo", "10"]
