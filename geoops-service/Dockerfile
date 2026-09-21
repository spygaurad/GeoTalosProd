# geoops-service — GPU model-training/serving microservice.
#
# Plain Python base for this scaffolding step (no GPU-capable base needed
# yet — the NVIDIA Container Toolkit isn't configured on the host, and
# nothing here does GPU work yet). Swap to a CUDA-enabled base (e.g.
# pytorch/pytorch:*-cuda*-cudnn*-runtime) in the increment that adds real
# backbone loading — a one-line change, not a design commitment made now.
FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
