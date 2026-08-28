# Stage 1: build the Alpaca CLI (Go). It is the execution boundary for every
# order, so it ships in the image rather than being fetched at runtime.
FROM golang:1.24-bookworm AS cli
RUN go install github.com/alpacahq/cli/cmd/alpaca@latest

# Stage 2: runtime. One image serves two roles:
#   Cloud Run service -> the dashboard (default CMD)
#   Cloud Run job     -> one trading cycle (command overridden at deploy)
FROM python:3.12-slim-bookworm
COPY --from=cli /go/bin/alpaca /usr/local/bin/alpaca

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/ ./agent/
COPY dashboard/ ./dashboard/

ENV PYTHONUNBUFFERED=1 JOURNAL_BACKEND=firestore
EXPOSE 8080
CMD exec uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8080}
