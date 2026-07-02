# --- Build Stage ---
FROM python:3.12-alpine AS builder
WORKDIR /app
RUN apk add --no-cache build-base gcc musl-dev python3-dev libffi-dev
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# --- Runtime Stage ---
FROM python:3.12-alpine AS runner
WORKDIR /app

# INJECTED docker-cli-buildx SO AUTOMATED CLUSTER RUNNERS CAN PARSE CUSTOM ENGINE BLUEPRINTS NATIVELY
RUN apk add --no-cache git curl procps iproute2 docker-cli docker-cli-compose docker-cli-buildx

COPY --from=builder /root/.local /root/.local
COPY . .

ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]