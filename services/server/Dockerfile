FROM python:3.12-slim

WORKDIR /app

COPY services/server/requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY yp_common ./yp_common
COPY services/server/sar_missions.py .
COPY services/server/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-ping-interval", "60", "--ws-ping-timeout", "30"]
