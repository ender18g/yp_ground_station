FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY sim_vehicle.py .

CMD ["python", "sim_vehicle.py"]
