#Stage 1: Builder
FROM python:3.13.1-slim AS builder
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

#Stage 2: Final Image
FROM python:3.13.1-slim
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN useradd  --uid 10001 appuser
COPY app ./app
# Create the data folder and give appuser ownership before switching users
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
