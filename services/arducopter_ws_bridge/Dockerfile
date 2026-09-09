FROM python:3.12-slim

WORKDIR /app

COPY services/arducopter_ws_bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY yp_common ./yp_common
COPY services/arducopter_ws_bridge/sar_missions.py .
COPY services/arducopter_ws_bridge/arducopter_ws_bridge.py .

CMD ["python", "-u", "arducopter_ws_bridge.py"]
