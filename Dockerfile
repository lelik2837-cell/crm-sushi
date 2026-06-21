FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/crm && chmod 777 /app/crm

EXPOSE 8080

CMD gunicorn --chdir crm app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT
