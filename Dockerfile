# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy only what the API needs at runtime
COPY api/             ./api/
COPY rag_pipeline/    ./rag_pipeline/
COPY chatbot/         ./chatbot/
COPY cypher_query_generator.py  .
COPY extractor_classifier.py    .
COPY entity_codes.py            .
COPY embedding_config.yaml      .
COPY scripts/                   ./scripts/

# Switch to non-root user
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
  || exit 1

CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
