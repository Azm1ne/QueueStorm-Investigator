# Lightweight Docker fallback for the QueueStorm Investigator API.
# Image well under the 500MB recommendation; no GPU, no model weights, no runtime training.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (secrets are NEVER baked in — passed via --env-file at run time).
COPY *.py ./
COPY SUST_Preli_Sample_Cases.json ./
COPY static/ ./static/

ENV PORT=8000
EXPOSE 8000

# Bind 0.0.0.0 so the judge harness can reach it. /health responds within 60s of start.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
