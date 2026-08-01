FROM node:20-slim

ENV TZ=Asia/Novosibirsk
RUN apt-get update && apt-get install -y --no-install-recommends tzdata git ca-certificates && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

# Некоторые транзитивные зависимости Baileys (libsignal, eslint-config) указаны в package.json
# как git+ssh github-ссылки — анонимно (без ключа) по ssh их не получить, npm/pacote при этом
# не откатывается на https сам. Переписываем ssh-ссылки на https глобально для git внутри образа.
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \
    git config --global url."https://github.com/".insteadOf "git@github.com:"

WORKDIR /app

COPY package.json .
RUN npm install --omit=dev

COPY . /app

CMD ["node", "server.js"]
