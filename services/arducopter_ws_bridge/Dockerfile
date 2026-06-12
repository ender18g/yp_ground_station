FROM python:3.12-slim

WORKDIR /app

COPY arducopter_ws_bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/sar_missions.py .
COPY arducopter_ws_bridge/arducopter_ws_bridge.py .

CMD ["python", "-u", "arducopter_ws_bridge.py"]
