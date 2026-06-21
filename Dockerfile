FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN mkdir -p /app/crm && chmod 777 /app/crm

WORKDIR /app/crm

CMD ["sh", "-c", "export DATABASE_PATH=/app/crm/crm.db && python3 app.py"]
