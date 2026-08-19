FROM python:3.11-slim

ENV TZ=Asia/Novosibirsk
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

CMD ["python3", "-c", "import os; os.execvp('gunicorn', ['gunicorn', 'wsgi:app', '--workers', '2', '--threads', '8', '--timeout', '120', '--preload', '--bind', '0.0.0.0:' + os.environ['PORT']])"]
