FROM python:3.11-slim

WORKDIR /app/crm

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN chmod -R 777 /app/crm

CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT"]
