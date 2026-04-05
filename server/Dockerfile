FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY infrastructure/db.py ./db.py
COPY infrastructure/storage.py ./storage.py
COPY services/blend_parser.py ./blend_parser.py
COPY services/runpod_dispatch.py ./runpod_dispatch.py

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
