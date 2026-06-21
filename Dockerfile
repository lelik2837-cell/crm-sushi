FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONPATH=/app/crm

CMD ["python3", "-c", "import os; os.execvp('gunicorn', ['gunicorn', 'app:app', '--workers', '1', '--threads', '8', '--timeout', '120', '--bind', '0.0.0.0:' + os.environ['PORT']])"]
