FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN chmod -R 777 /app/crm

WORKDIR /app/crm

CMD ["sh", "-c", "python3 app.py"]
