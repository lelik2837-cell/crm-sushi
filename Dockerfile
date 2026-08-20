FROM python:3.11-slim

ENV TZ=Asia/Novosibirsk
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# --preload убран намеренно: APScheduler стартует при импорте app.py и заводит фоновый
# поток — с --preload это происходит в master-процессе ДО fork() воркеров, а fork()
# копирует только текущий поток ОС; если этот фоновый поток в момент форка держал
# внутренний lock (urllib3/SQLite/ssl), лок остаётся навсегда захваченным в дочернем
# воркере, а поток, который мог бы его освободить, в этом процессе уже не существует —
# воркер зависает без потребления CPU. Без --preload каждый worker импортирует
# приложение и заводит свой поток уже после собственного fork, до какого-либо
# дальнейшего форка — race исчезает.
CMD ["python3", "-c", "import os; os.execvp('gunicorn', ['gunicorn', 'wsgi:app', '--workers', '2', '--threads', '8', '--timeout', '120', '--bind', '0.0.0.0:' + os.environ['PORT']])"]
