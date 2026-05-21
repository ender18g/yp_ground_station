FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sar_missions.py .
COPY arducopter_ws_bridge.py .

CMD ["python", "-u", "arducopter_ws_bridge.py"]
