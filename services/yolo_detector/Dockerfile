FROM python:3.12-slim

# libGL/libglib are required by opencv-python-headless's video decoding backend
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY yolo_detector.py .

CMD ["python", "yolo_detector.py"]
