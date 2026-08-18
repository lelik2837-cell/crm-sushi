import express from 'express';
import multer from 'multer';
import qrcode from 'qrcode';
import pino from 'pino';
import fs from 'fs';
import path from 'path';
import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import { SocksProxyAgent } from 'socks-proxy-agent';

const PORT = process.env.PORT || 3000;
const AUTH_DIR = process.env.AUTH_DIR || '/data/auth';
const WEBHOOK_URL = process.env.WEBHOOK_URL || '';
const WEBHOOK_TOKEN = process.env.WEBHOOK_TOKEN || '';
const RESUME_URL = process.env.RESUME_URL || '';
const PROXY_URL = process.env.PROXY_URL || '';
// Для входящих сообщений (нужны "Оценке заказа" — принять ответ клиента с баллом) отдельная
// env-переменная не заводилась: путь выводится из WEBHOOK_URL, чтобы не требовать ещё одной
// правки docker-compose/env-файлов на сервере сверх уже сделанных.
const INBOUND_WEBHOOK_URL = WEBHOOK_URL.replace('/api/messenger-webhook', '/api/messenger-inbound');

const logger = pino({ level: 'warn' });
// WhatsApp/Meta недоступны напрямую с этого сервера (сеть режет соединения до
// web.whatsapp.com/static.whatsapp.net) — весь трафик Baileys идёт через локальный
// SOCKS5-туннель (см. сервис xray в docker-compose.yml).
const proxyAgent = PROXY_URL ? new SocksProxyAgent(PROXY_URL) : undefined;
if (proxyAgent) {
  console.log('[whatsapp] using proxy', PROXY_URL);
}

// Мульти-сессионность: несколько WhatsApp-аккаунтов в одном процессе, ключ — accountId,
// который генерирует Flask/SQLite при создании записи "номер-отправитель" (messenger_accounts).
// Раньше сервис держал ровно одну глобальную сессию — расширено при добавлении Max/Telegram
// и возможности подключить сразу несколько номеров с приоритетом отправки.
const accounts = new Map(); // accountId -> AccountState

function newAccountState(accountId) {
  return {
    accountId,
    sock: null,
    status: 'disconnected', // disconnected | connecting | qr | connected
    qr: null,
    phone: null,
    resumeChecked: false,
    campaign: null,
  };
}

function getAccount(accountId) {
  if (!accounts.has(accountId)) {
    accounts.set(accountId, newAccountState(accountId));
  }
  return accounts.get(accountId);
}

function authDirFor(accountId) {
  return path.join(AUTH_DIR, String(accountId));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function jidFor(phone) {
  return `${phone}@s.whatsapp.net`;
}

function randomDelayMs(min, max) {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  const sec = lo + Math.random() * (hi - lo);
  return Math.round(sec * 1000);
}

async function startAccountSock(state) {
  const dir = authDirFor(state.accountId);
  fs.mkdirSync(dir, { recursive: true });
  const { state: authState, saveCreds } = await useMultiFileAuthState(dir);
  state.status = 'connecting';
  const sock = makeWASocket({
    auth: authState,
    logger,
    printQRInTerminal: false,
    browser: ['CRM Sushi', 'Chrome', '1.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    agent: proxyAgent,
    fetchAgent: proxyAgent,
  });
  state.sock = sock;

  sock.ev.on('creds.update', saveCreds);

  // Нужно "Оценке заказа" — принять ответный текст клиента (баллом от 1 до 5) на запрос
  // об оценке заказа. Раньше сервис только отправлял, входящие никак не обрабатывались.
  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (msg.key?.fromMe) continue;
      const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text;
      const phone = msg.key?.remoteJid?.split('@')[0];
      if (!text || !phone) continue;
      sendInboundWebhook({ channel: 'whatsapp', account_id: state.accountId, phone, text })
        .catch((err) => console.error('[whatsapp] inbound handling failed', err));
    }
  });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      state.qr = qr;
      state.status = 'qr';
    }

    if (connection === 'open') {
      state.status = 'connected';
      state.qr = null;
      state.phone = sock.user?.id ? sock.user.id.split(':')[0] : null;
      console.log('[whatsapp] account', state.accountId, 'connected as', state.phone);
      if (!state.resumeChecked) {
        state.resumeChecked = true;
        checkResume(state).catch((err) => console.error('[whatsapp] resume check failed', state.accountId, err));
      }
    } else if (connection === 'close') {
      const statusCode = lastDisconnect?.error instanceof Boom
        ? lastDisconnect.error.output?.statusCode
        : null;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      state.status = 'disconnected';
      state.phone = null;

      if (loggedOut) {
        console.log('[whatsapp] account', state.accountId, 'logged out, clearing session');
        fs.rmSync(dir, { recursive: true, force: true });
        fs.mkdirSync(dir, { recursive: true });
      } else {
        console.log('[whatsapp] account', state.accountId, 'connection closed, reconnecting in 3s. statusCode=', statusCode,
          'error=', lastDisconnect?.error ? String(lastDisconnect.error) : null);
        setTimeout(() => {
          startAccountSock(state).catch((err) => console.error('[whatsapp] reconnect failed', state.accountId, err));
        }, 3000);
      }
    }
  });
}

async function sendWebhook(payload) {
  if (!WEBHOOK_URL) return;
  try {
    await fetch(`${WEBHOOK_URL}/${WEBHOOK_TOKEN}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    console.error('[whatsapp] webhook call failed', err);
  }
}

async function sendInboundWebhook(payload) {
  if (!WEBHOOK_URL) return;
  try {
    await fetch(`${INBOUND_WEBHOOK_URL}/${WEBHOOK_TOKEN}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    console.error('[whatsapp] inbound webhook call failed', err);
  }
}

async function runCampaign(state, cfg) {
  state.campaign = cfg;
  let sentSinceBatch = 0;

  for (const item of cfg.queue) {
    if (state.campaign?.stopFlag) break;
    const phone = item.phone;
    const text = item.message || '';

    const result = {
      broadcast_id: cfg.broadcastId, account_id: state.accountId, phone, status: 'failed', error: null,
    };
    try {
      const checkResults = await state.sock.onWhatsApp(jidFor(phone)).catch(() => []);
      const check = checkResults && checkResults[0];
      if (!check?.exists) {
        result.status = 'invalid';
      } else {
        const targetJid = check.jid || jidFor(phone);
        if (cfg.image) {
          await state.sock.sendMessage(targetJid, {
            image: cfg.image,
            mimetype: cfg.imageMime || 'image/jpeg',
            caption: text,
          });
        } else {
          await state.sock.sendMessage(targetJid, { text });
        }
        result.status = 'sent';
      }
    } catch (err) {
      result.status = 'failed';
      result.error = String(err?.message || err);
    }

    await sendWebhook(result);
    if (result.status !== 'invalid') sentSinceBatch += 1;

    if (state.campaign?.stopFlag) break;

    if (cfg.batchSize > 0 && sentSinceBatch >= cfg.batchSize) {
      sentSinceBatch = 0;
      await sleep(cfg.batchPauseSeconds * 1000);
    } else {
      await sleep(randomDelayMs(cfg.intervalMin, cfg.intervalMax));
    }
  }

  state.campaign = null;
}

async function checkResume(state) {
  if (!RESUME_URL || !WEBHOOK_TOKEN || state.campaign) return;
  try {
    const url = `${RESUME_URL}?token=${encodeURIComponent(WEBHOOK_TOKEN)}&account_id=${encodeURIComponent(state.accountId)}`;
    const resp = await fetch(url);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data || data.none || !Array.isArray(data.recipients) || data.recipients.length === 0) return;

    console.log(`[whatsapp] account ${state.accountId} resuming broadcast #${data.broadcast_id}, ${data.recipients.length} pending`);
    const cfg = {
      broadcastId: data.broadcast_id,
      image: data.image_base64 ? Buffer.from(data.image_base64, 'base64') : null,
      imageMime: data.image_mime || null,
      queue: data.recipients,
      intervalMin: data.interval_min_seconds || 5,
      intervalMax: data.interval_max_seconds || 5,
      batchSize: data.batch_size || 0,
      batchPauseSeconds: data.batch_pause_seconds || 0,
      stopFlag: false,
    };
    runCampaign(state, cfg).catch((err) => {
      console.error('[whatsapp] resumed campaign crashed', state.accountId, err);
      state.campaign = null;
    });
  } catch (err) {
    console.error('[whatsapp] checkResume failed', state.accountId, err);
  }
}

const app = express();
app.use(express.json({ limit: '2mb' }));
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 8 * 1024 * 1024 } });

app.get('/health', (req, res) => {
  res.json({ ok: true, accounts: accounts.size });
});

app.post('/accounts/:id/session/start', async (req, res) => {
  const state = getAccount(req.params.id);
  if (state.status === 'connecting' || state.status === 'qr' || state.status === 'connected') {
    // Идемпотентно — не поднимаем вторую сессию поверх той же auth-директории.
    res.json({ ok: true, status: state.status });
    return;
  }
  try {
    await startAccountSock(state);
    res.json({ ok: true, status: state.status });
  } catch (err) {
    res.status(500).json({ error: String(err?.message || err) });
  }
});

app.get('/accounts/:id/session/status', (req, res) => {
  const state = getAccount(req.params.id);
  res.json({ status: state.status, phone: state.phone, campaignRunning: !!state.campaign });
});

app.get('/accounts/:id/session/qr', async (req, res) => {
  const state = getAccount(req.params.id);
  if (!state.qr) {
    res.json({ qr: null });
    return;
  }
  try {
    const dataUrl = await qrcode.toDataURL(state.qr);
    res.json({ qr: dataUrl });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.post('/accounts/:id/session/logout', async (req, res) => {
  const state = getAccount(req.params.id);
  try {
    if (state.sock) {
      await state.sock.logout().catch(() => {});
    }
    fs.rmSync(authDirFor(state.accountId), { recursive: true, force: true });
    fs.mkdirSync(authDirFor(state.accountId), { recursive: true });
    state.status = 'disconnected';
    state.phone = null;
    state.qr = null;
    state.resumeChecked = false;
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.post('/accounts/:id/numbers/check', async (req, res) => {
  const state = getAccount(req.params.id);
  if (state.status !== 'connected') {
    res.status(409).json({ error: 'not_connected' });
    return;
  }
  const phones = Array.isArray(req.body.phones) ? req.body.phones : [];
  if (phones.length === 0) {
    res.status(400).json({ error: 'empty' });
    return;
  }
  const results = {};
  const CHUNK = 30;
  try {
    for (let i = 0; i < phones.length; i += CHUNK) {
      const chunk = phones.slice(i, i + CHUNK);
      const checked = await state.sock.onWhatsApp(...chunk.map(jidFor));
      const existsByPhone = new Map();
      for (const c of checked || []) {
        if (c?.jid) existsByPhone.set(c.jid.split('@')[0], !!c.exists);
      }
      for (const phone of chunk) {
        results[phone] = existsByPhone.get(phone) ?? false;
      }
      if (i + CHUNK < phones.length) await sleep(300);
    }
    res.json({ ok: true, results });
  } catch (err) {
    res.status(500).json({ error: String(err?.message || err) });
  }
});

// Одно сообщение сразу, без очереди/пауз — в отличие от campaign/start (та под пачку
// с рандомными интервалами). Нужно для событийных одиночных отправок ("Оценка заказа").
app.post('/accounts/:id/message/send', async (req, res) => {
  const state = getAccount(req.params.id);
  if (state.status !== 'connected') {
    res.status(409).json({ error: 'not_connected' });
    return;
  }
  const { phone, text } = req.body || {};
  if (!phone || !text) {
    res.status(400).json({ error: 'bad_request' });
    return;
  }
  try {
    await state.sock.sendMessage(jidFor(phone), { text });
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: String(err?.message || err) });
  }
});

app.post('/accounts/:id/campaign/start', upload.single('image'), (req, res) => {
  const state = getAccount(req.params.id);
  if (state.status !== 'connected') {
    res.status(409).json({ error: 'not_connected' });
    return;
  }
  if (state.campaign) {
    res.status(409).json({ error: 'already_running' });
    return;
  }

  let recipients;
  try {
    recipients = JSON.parse(req.body.recipients || '[]');
  } catch {
    res.status(400).json({ error: 'bad_recipients' });
    return;
  }
  if (!Array.isArray(recipients) || recipients.length === 0 || !recipients[0]?.phone) {
    res.status(400).json({ error: 'empty_recipients' });
    return;
  }

  const cfg = {
    broadcastId: req.body.broadcastId,
    image: req.file ? req.file.buffer : null,
    imageMime: req.file ? req.file.mimetype : null,
    queue: recipients,
    intervalMin: Number(req.body.intervalMin) || 5,
    intervalMax: Number(req.body.intervalMax) || 5,
    batchSize: Number(req.body.batchSize) || 0,
    batchPauseSeconds: Number(req.body.batchPauseSeconds) || 0,
    stopFlag: false,
  };

  res.status(202).json({ ok: true });
  runCampaign(state, cfg).catch((err) => {
    console.error('[whatsapp] campaign crashed', state.accountId, err);
    state.campaign = null;
  });
});

app.post('/accounts/:id/campaign/stop', (req, res) => {
  const state = getAccount(req.params.id);
  if (state.campaign) state.campaign.stopFlag = true;
  res.json({ ok: true });
});

app.get('/accounts/:id/campaign/status', (req, res) => {
  const state = getAccount(req.params.id);
  if (!state.campaign) {
    res.json({ running: false });
    return;
  }
  res.json({ running: true, broadcastId: state.campaign.broadcastId });
});

app.listen(PORT, () => {
  console.log(`[whatsapp] listening on ${PORT}`);
});

// Переживаем рестарт контейнера: поднимаем заново все сессии, для которых на диске уже есть
// auth-директория (Flask могла и не успеть/не быть обязана заново дёргать session/start для
// каждого известного аккаунта) — так подключённые номера не отваливаются молча после деплоя.
fs.mkdirSync(AUTH_DIR, { recursive: true });
for (const entry of fs.readdirSync(AUTH_DIR, { withFileTypes: true })) {
  if (!entry.isDirectory()) continue;
  const state = getAccount(entry.name);
  startAccountSock(state).catch((err) => console.error('[whatsapp] startup restore failed', entry.name, err));
}
