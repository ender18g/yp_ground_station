FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sim_vehicle.py .

CMD ["python", "sim_vehicle.py"]
