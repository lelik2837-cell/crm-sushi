import express from 'express';
import multer from 'multer';
import qrcode from 'qrcode';
import pino from 'pino';
import fs from 'fs';
import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import { SocksProxyAgent } from 'socks-proxy-agent';

const PORT = process.env.PORT || 3000;
const AUTH_DIR = process.env.AUTH_DIR || '/data/auth';
const WEBHOOK_URL = process.env.WEBHOOK_URL || '';
const WEBHOOK_TOKEN = process.env.WEBHOOK_TOKEN || '';
const RESUME_URL = process.env.RESUME_URL || '';
const PROXY_URL = process.env.PROXY_URL || '';

const logger = pino({ level: 'warn' });
// WhatsApp/Meta недоступны напрямую с этого сервера (сеть режет соединения до
// web.whatsapp.com/static.whatsapp.net) — весь трафик Baileys идёт через локальный
// SOCKS5-туннель (см. сервис xray в docker-compose.yml).
const proxyAgent = PROXY_URL ? new SocksProxyAgent(PROXY_URL) : undefined;
if (proxyAgent) {
  console.log('[whatsapp] using proxy', PROXY_URL);
}

let sock = null;
let latestQr = null;
let connectionStatus = 'disconnected'; // disconnected | connecting | qr | connected
let connectedPhone = null;
let resumeChecked = false;
let campaign = null;

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

async function startSock() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  connectionStatus = 'connecting';
  sock = makeWASocket({
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['CRM Sushi', 'Chrome', '1.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    agent: proxyAgent,
    fetchAgent: proxyAgent,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      latestQr = qr;
      connectionStatus = 'qr';
    }

    if (connection === 'open') {
      connectionStatus = 'connected';
      latestQr = null;
      connectedPhone = sock.user?.id ? sock.user.id.split(':')[0] : null;
      console.log('[whatsapp] connected as', connectedPhone);
      if (!resumeChecked) {
        resumeChecked = true;
        checkResume().catch((err) => console.error('[whatsapp] resume check failed', err));
      }
    } else if (connection === 'close') {
      const statusCode = lastDisconnect?.error instanceof Boom
        ? lastDisconnect.error.output?.statusCode
        : null;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      connectionStatus = 'disconnected';
      connectedPhone = null;

      if (loggedOut) {
        console.log('[whatsapp] logged out, clearing session');
        fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        fs.mkdirSync(AUTH_DIR, { recursive: true });
      } else {
        console.log('[whatsapp] connection closed, reconnecting in 3s. statusCode=', statusCode,
          'error=', lastDisconnect?.error ? String(lastDisconnect.error) : null);
        setTimeout(() => {
          startSock().catch((err) => console.error('[whatsapp] reconnect failed', err));
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

async function runCampaign(cfg) {
  campaign = cfg;
  let sentSinceBatch = 0;

  for (const item of cfg.queue) {
    if (campaign?.stopFlag) break;
    const phone = item.phone;
    const text = item.message || '';

    const result = { broadcast_id: cfg.broadcastId, phone, status: 'failed', error: null };
    try {
      const checkResults = await sock.onWhatsApp(jidFor(phone)).catch(() => []);
      const check = checkResults && checkResults[0];
      if (!check?.exists) {
        result.status = 'invalid';
      } else {
        const targetJid = check.jid || jidFor(phone);
        if (cfg.image) {
          await sock.sendMessage(targetJid, {
            image: cfg.image,
            mimetype: cfg.imageMime || 'image/jpeg',
            caption: text,
          });
        } else {
          await sock.sendMessage(targetJid, { text });
        }
        result.status = 'sent';
      }
    } catch (err) {
      result.status = 'failed';
      result.error = String(err?.message || err);
    }

    await sendWebhook(result);
    if (result.status !== 'invalid') sentSinceBatch += 1;

    if (campaign?.stopFlag) break;

    if (cfg.batchSize > 0 && sentSinceBatch >= cfg.batchSize) {
      sentSinceBatch = 0;
      await sleep(cfg.batchPauseSeconds * 1000);
    } else {
      await sleep(randomDelayMs(cfg.intervalMin, cfg.intervalMax));
    }
  }

  campaign = null;
}

async function checkResume() {
  if (!RESUME_URL || !WEBHOOK_TOKEN || campaign) return;
  try {
    const resp = await fetch(`${RESUME_URL}?token=${encodeURIComponent(WEBHOOK_TOKEN)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data || data.none || !Array.isArray(data.recipients) || data.recipients.length === 0) return;

    console.log(`[whatsapp] resuming broadcast #${data.broadcast_id}, ${data.recipients.length} pending`);
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
    runCampaign(cfg).catch((err) => {
      console.error('[whatsapp] resumed campaign crashed', err);
      campaign = null;
    });
  } catch (err) {
    console.error('[whatsapp] checkResume failed', err);
  }
}

const app = express();
app.use(express.json({ limit: '2mb' }));
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 8 * 1024 * 1024 } });

app.get('/health', (req, res) => {
  res.json({ ok: true, connectionStatus });
});

app.get('/session/status', (req, res) => {
  res.json({ status: connectionStatus, phone: connectedPhone, campaignRunning: !!campaign });
});

app.get('/session/qr', async (req, res) => {
  if (!latestQr) {
    res.json({ qr: null });
    return;
  }
  try {
    const dataUrl = await qrcode.toDataURL(latestQr);
    res.json({ qr: dataUrl });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.post('/session/logout', async (req, res) => {
  try {
    if (sock) {
      await sock.logout().catch(() => {});
    }
    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    connectionStatus = 'disconnected';
    connectedPhone = null;
    latestQr = null;
    resumeChecked = false;
    setTimeout(() => {
      startSock().catch((err) => console.error('[whatsapp] restart after logout failed', err));
    }, 1000);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.post('/numbers/check', async (req, res) => {
  if (connectionStatus !== 'connected') {
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
      const checked = await sock.onWhatsApp(...chunk.map(jidFor));
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

app.post('/campaign/start', upload.single('image'), (req, res) => {
  if (connectionStatus !== 'connected') {
    res.status(409).json({ error: 'not_connected' });
    return;
  }
  if (campaign) {
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
  runCampaign(cfg).catch((err) => {
    console.error('[whatsapp] campaign crashed', err);
    campaign = null;
  });
});

app.post('/campaign/stop', (req, res) => {
  if (campaign) campaign.stopFlag = true;
  res.json({ ok: true });
});

app.get('/campaign/status', (req, res) => {
  if (!campaign) {
    res.json({ running: false });
    return;
  }
  res.json({ running: true, broadcastId: campaign.broadcastId });
});

app.listen(PORT, () => {
  console.log(`[whatsapp] listening on ${PORT}`);
});

startSock().catch((err) => console.error('[whatsapp] startSock failed', err));
