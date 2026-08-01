FROM node:20-slim

ENV TZ=Asia/Novosibirsk
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json .
RUN npm install --omit=dev

COPY . /app

CMD ["node", "server.js"]
