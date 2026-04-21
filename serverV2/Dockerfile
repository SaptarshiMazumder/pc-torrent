FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./serverV2/

EXPOSE 8080

CMD ["sh", "-c", "uvicorn serverV2.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
