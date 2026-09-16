/**
 * Relay for CRM Senler's Telegram bot API calls (see crm/SENLER.md, crm/senler_api.py
 * telegram_api_base()). Not a webhook receiver: Telegram never calls this Worker.
 * The CRM server calls it instead of api.telegram.org directly, on networks where
 * reaching Cloudflare's edge is reliable but reaching Telegram directly is not.
 *
 * Deploy: paste into a Cloudflare Worker (dashboard editor, no build step needed),
 * then set a "RELAY_SECRET" secret variable on the Worker to the same value as
 * SENLER_TELEGRAM_RELAY_SECRET on the CRM server. Point SENLER_TELEGRAM_API_BASE
 * at this Worker's *.workers.dev URL.
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/bot([^/]+)\/([A-Za-z]+)$/);
    if (!match) {
      return new Response('not found', { status: 404 });
    }
    if (env.RELAY_SECRET && request.headers.get('X-Relay-Secret') !== env.RELAY_SECRET) {
      return new Response('forbidden', { status: 403 });
    }
    const headers = new Headers(request.headers);
    headers.delete('x-relay-secret');
    headers.delete('host');
    const upstream = new Request('https://api.telegram.org' + url.pathname + url.search, {
      method: request.method,
      headers,
      body: request.body,
      redirect: 'follow',
    });
    return fetch(upstream);
  },
};
