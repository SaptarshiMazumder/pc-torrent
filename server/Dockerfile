FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY firebase_auth.py ./
COPY infrastructure/ ./infrastructure/
COPY services/ ./services/
COPY routers/ ./routers/
COPY schemas/ ./schemas/
COPY utils/ ./utils/

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
