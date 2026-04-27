FROM python:3.11-slim

WORKDIR /app

# Cloud Run health probe expects something on $PORT for HTTP services, but
# this is a worker (no inbound traffic).  We DON'T expose a port — Cloud Run
# detects this via the deployment config (use --no-cpu-throttling / worker
# template) and skips the HTTP startup probe.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

CMD ["python", "-u", "main.py"]
