# Built with the repo root as context (see docker-compose.yml) so the image
# carries shared/semantic_contract.py — the query/document text contract.
FROM python:3.12-slim

WORKDIR /app

# CPU-only torch, same rationale as embedding/Dockerfile.
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY api /app

ENV HF_HOME=/hf-cache

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
