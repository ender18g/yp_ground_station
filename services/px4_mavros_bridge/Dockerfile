FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY px4_mavros_bridge.py .

CMD ["python", "px4_mavros_bridge.py"]
