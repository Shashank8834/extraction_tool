FROM python:3.12-slim

# System deps: tesseract for OCR, poppler for PDF->image conversion.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Uploads live on a mounted volume.
RUN mkdir -p /data/uploads

EXPOSE 8000

# --proxy-headers makes request.client.host reflect X-Forwarded-For from the
# reverse proxy. forwarded-allow-ips=* is safe here because only the trusted
# proxy can reach the container (bound to localhost / internal network).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
