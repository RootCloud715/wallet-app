FROM python:3.13.1-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd  --uid 10001 appuser
COPY app ./app

# Create the data folder and give appuser ownership before switching users
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
