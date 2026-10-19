# syntax=docker/dockerfile:1

FROM python:3.13-slim

WORKDIR /app

ENV ORDER_API_HOST=0.0.0.0
ENV ORDER_API_PORT=10000
ENV ORDER_API_DB_PATH=/var/data/su-order-api/orders.db
ENV ORDER_API_SLIPS_DIR=/var/data/su-order-api/slips
ENV ORDER_API_SLIDES_DIR=/var/data/su-order-api/slides

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-tha tesseract-ocr-eng \
    libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytesseract==0.3.13 Pillow==10.4.0 opencv-python-headless numpy

COPY server/order_api.py ./order_api.py
COPY server/ocr.py ./ocr.py
COPY server/fp28/ ./fp28/
COPY server/store/ ./store/
COPY server/templates/ ./templates/

RUN mkdir -p /var/data/su-order-api/slips /var/data/easyocr-models

EXPOSE 10000

CMD ["python", "/app/order_api.py"]
