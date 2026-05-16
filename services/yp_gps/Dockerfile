FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY yp_gps.py .

CMD ["python", "yp_gps.py"]
