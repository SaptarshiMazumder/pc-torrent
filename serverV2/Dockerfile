# Stage 1 — build the /home dashboard SPA.  Source lives in serverV2/home
# so the existing build context (serverV2/) stays sufficient.
FROM node:22-alpine AS home_build

WORKDIR /build
COPY home/package.json home/package-lock.json ./
RUN npm ci
COPY home/ ./
RUN npm run build

# Stage 2 — the server image (unchanged apart from the dist copy).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./serverV2/
COPY --from=home_build /build/dist ./serverV2/home/dist

EXPOSE 8080

CMD ["sh", "-c", "uvicorn serverV2.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
