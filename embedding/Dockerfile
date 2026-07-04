FROM python:3.12-slim

WORKDIR /app

# CPU-only torch installed first, from PyTorch's own index: the PyPI default
# build drags in ~2.5GB of CUDA libraries this pipeline never uses (SPEC C3
# budgets for single-machine CPU encoding; the deduped workload is ~337
# texts, so GPU would be pointless anyway).
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Model weights (~130MB) download on first run and persist in the HF_HOME
# volume mounted by compose, so subsequent runs are offline-capable.
ENV HF_HOME=/hf-cache

CMD ["python", "embed.py"]
