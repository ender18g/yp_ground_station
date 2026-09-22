FROM python:3.12-slim

# libGL/libglib are required by opencv-python-headless's video decoding backend
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

# Bake the default model into the image so first boot works without outbound
# internet access; it's copied into the shared models volume at runtime.
RUN python -c "from ultralytics.utils.downloads import attempt_download_asset; attempt_download_asset('yolov8n.pt', dir='/app')"

COPY yolo_detector.py .

CMD ["python", "yolo_detector.py"]
