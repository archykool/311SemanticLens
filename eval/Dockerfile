# Evaluation-only image. FAISS lives here and ONLY here — it must never
# appear in the serving path (SPEC global boundary 5); keeping it in a
# separate image makes that boundary physical.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

COPY eval/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY eval /app

ENV HF_HOME=/hf-cache

CMD ["python", "evaluate.py"]
