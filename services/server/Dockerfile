FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/sar_missions.py .
COPY server/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-ping-interval", "60", "--ws-ping-timeout", "30"]
