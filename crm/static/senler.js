(() => {
  'use strict';
  const root = document.getElementById('senler-app');
  if (!root) return;
  const main = document.getElementById('sn-main');
  const modal = document.getElementById('sn-modal');
  const apiBase = '/reports/senler/api/';
  const tabs = ['overview', 'subscribers', 'campaigns', 'bots', 'dialogs', 'channels'];
  const state = {boot: null, tab: 'overview', channel: '', generation: 0, dirty: false, campaign: null, bot: null, wizard: 0, selected: new Set(), subscriberPage: 1, dialog: null};
  const labels = {active: 'Подписан', pending: 'Ожидает', unsubscribed: 'Отписался', blocked: 'Сообщения запрещены', configured: 'Не подключён', connected: 'Подключён', paused: 'На паузе', draft: 'Черновик', scheduled: 'По расписанию', running: 'Отправляется', completed: 'Завершено', cancelled: 'Отменено', sent: 'Отправлено', sending: 'Отправляется', error: 'Ошибка', unknown: 'Результат неизвестен'};
  const kinds = {vk: 'ВКонтакте', telegram: 'Telegram', max: 'MAX'};
  const DEFAULT_GREETING_TEXT = 'Здесь можно получать наши новости и предложения. Подписаться на рассылку? Отменить подписку можно в любой момент командой /stop.';
  const DEFAULT_STOP_TEXT = 'Жаль, что вы отписались — теперь вы не будете получать наши новости и акции. Если передумаете, можно подписаться снова.';
  const stepLabels = {message: 'Сообщение', delay: 'Задержка', condition: 'Условие', group: 'Группа подписчиков', handoff: 'Передать оператору'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
  // Shared with crm/senler_api.py's FORMAT_RE/_walk: **bold**, _italic_ (not mid-word, so
  // promo codes like USE_CODE_2026 survive), ++underline++, [label](url). Recursive so
  // combined formatting (bold containing italic, etc.) nests correctly instead of only
  // matching the outermost marker.
  function walkFormatting(text, escape, bold, italic, underline, link) {
    const re = /\[(?<linkText>[^[\]\n]+)\]\((?<linkUrl>https?:\/\/[^\s)]+)\)|\*\*(?<bold>[^\n*]+?)\*\*|\+\+(?<underline>[^\n+]+?)\+\+|(?<!\w)_(?<italic>[^\n_]+?)_(?!\w)/g;
    let out = '', pos = 0, m;
    while ((m = re.exec(text))) {
      out += escape(text.slice(pos, m.index));
      const g = m.groups;
      if (g.linkText !== undefined) out += link(walkFormatting(g.linkText, escape, bold, italic, underline, link), g.linkUrl);
      else if (g.bold !== undefined) out += bold(walkFormatting(g.bold, escape, bold, italic, underline, link));
      else if (g.underline !== undefined) out += underline(walkFormatting(g.underline, escape, bold, italic, underline, link));
      else out += italic(walkFormatting(g.italic, escape, bold, italic, underline, link));
      pos = re.lastIndex;
    }
    out += escape(text.slice(pos));
    return out;
  }
  const formatPreview = value => walkFormatting(value, esc,
    s => '<strong>' + s + '</strong>', s => '<em>' + s + '</em>', s => '<u>' + s + '</u>',
    (s, url) => '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + s + '</a>');
  const EMOJIS = ['😀','😊','😉','😍','🥳','😋','👍','🙏','🎉','🔥','⭐','❤️','✅','⏰','📢','🎁','💥','🍣','🍱','🍜','🍙','🥢','🍤','🐟','🍚','🌶️','🥤','🚀','📍','💬'];
  // The message field is a contenteditable div so the owner sees real bold/italic/underline
  // text instead of typing **/++/_ markers; editorToText serializes it back to that markup
  // (matching walkFormatting/FORMAT_RE) only when the campaign is saved or sent.
  function editorToText(root) {
    function walk(node) {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent;
      if (node.nodeType !== Node.ELEMENT_NODE) return '';
      const tag = node.tagName.toLowerCase();
      const inner = () => [...node.childNodes].map(walk).join('');
      if (tag === 'br') return '\n';
      if (tag === 'b' || tag === 'strong') return '**' + inner() + '**';
      if (tag === 'u') return '++' + inner() + '++';
      if (tag === 'i' || tag === 'em') return '_' + inner() + '_';
      if (tag === 'a') {
        const href = node.getAttribute('href') || '';
        return /^https?:\/\//i.test(href) ? '[' + inner() + '](' + href + ')' : inner();
      }
      if (tag === 'div' || tag === 'p') return inner() + '\n';
      return inner();
    }
    return [...root.childNodes].map(walk).join('').replace(/\n+$/, '');
  }
  function applyFormat(editor, command) {
    editor.focus();
    document.execCommand('styleWithCSS', false, false);
    document.execCommand(command);
    editor.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function insertEmoji(editor, emoji) {
    editor.focus();
    document.execCommand('insertText', false, emoji);
    editor.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function applyLink(editor) {
    editor.focus();
    const selection = window.getSelection();
    const hasSelection = selection && selection.rangeCount > 0 && !selection.isCollapsed && editor.contains(selection.anchorNode);
    const label = hasSelection ? selection.toString() : 'ссылка';
    const savedRange = hasSelection ? selection.getRangeAt(0).cloneRange() : null;
    const url = window.prompt('Ссылка (https://…)', 'https://');
    if (!url) return;
    const trimmed = url.trim();
    if (!/^https?:\/\/\S+$/i.test(trimmed)) { toast('Нужна полная ссылка https://…', true); return; }
    editor.focus();
    const range = savedRange || (() => { const r = document.createRange(); r.selectNodeContents(editor); r.collapse(false); return r; })();
    range.deleteContents();
    const a = document.createElement('a');
    a.href = trimmed; a.textContent = label;
    range.insertNode(a);
    range.setStartAfter(a); range.collapse(true);
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(range);
    editor.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function updateToolbarState(editor) {
    const wrap = editor.closest('[data-message-wrap]');
    if (!wrap) return;
    wrap.querySelector('[data-action=fmt-bold]')?.classList.toggle('active', document.queryCommandState('bold'));
    wrap.querySelector('[data-action=fmt-italic]')?.classList.toggle('active', document.queryCommandState('italic'));
    wrap.querySelector('[data-action=fmt-underline]')?.classList.toggle('active', document.queryCommandState('underline'));
  }
  function wireMessageEditor(container) {
    const editor = container.querySelector('[data-message-editor]');
    if (!editor) return;
    editor.addEventListener('paste', event => {
      event.preventDefault();
      const text = (event.clipboardData || window.clipboardData).getData('text/plain');
      document.execCommand('insertText', false, text);
    });
    editor.addEventListener('input', () => {
      if (!editor.textContent) editor.innerHTML = '';
      updateToolbarState(editor);
    });
  }
  document.addEventListener('selectionchange', () => {
    const editor = document.activeElement;
    if (editor && editor.matches && editor.matches('[data-message-editor]')) updateToolbarState(editor);
  });
  // Clicking a toolbar button would otherwise blur the editor first and clear the selection
  // (mousedown's default focus-shift happens before click), losing what to format/link.
  document.addEventListener('mousedown', event => { if (event.target.closest('.sn-editor-toolbar')) event.preventDefault(); });
  const num = value => Number(value || 0).toLocaleString('ru-RU');
  const date = value => value ? new Date(value * 1000).toLocaleString('ru-RU', {timeZone:'Asia/Novosibirsk', day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}) : '—';
  const badge = status => `<span class="sn-badge ${['active','connected','sent','completed'].includes(status) ? 'green' : ['running','scheduled','sending'].includes(status) ? 'blue' : ['error','unknown','blocked'].includes(status) ? 'red' : status === 'paused' ? 'orange' : ''}">${esc(labels[status] || status)}</span>`;
  const kindBadge = kind => `<span class="sn-kind ${esc(kind)}">${esc(kinds[kind] || kind)}</span>`;
  const btn = (text, action, attrs = '', primary = false) => `<button type="button" class="btn ${primary ? 'btn-primary' : 'btn-light'}" data-action="${action}" ${attrs}>${text}</button>`;
  const channelName = id => state.boot.channels.find(c => c.id === Number(id))?.name || 'Канал';
  const groupName = id => state.boot.groups.find(g => g.id === Number(id))?.name || 'Группа';
  const options = (items, selected, empty = 'Выберите…') => `<option value="">${esc(empty)}</option>` + items.map(item => `<option value="${item.id}" ${Number(selected) === item.id ? 'selected' : ''}>${esc(item.name)}</option>`).join('');
  const empty = (icon, title, text, action = '') => `<div class="sn-panel sn-empty"><i class="bi bi-${icon}"></i><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
  const head = (title, description, actions = '') => `<div class="sn-section-head"><div><h2>${title}</h2>${description ? `<p>${description}</p>` : ''}</div><div class="sn-actions">${actions}</div></div>`;
  const inputField = (label, name, value = '', extra = '', help = '') => `<div class="sn-field"><label for="${name}">${label}</label><input class="form-control${name==='ch-token'?' ym-disable-keys':''}" id="${name}" value="${esc(value)}" ${extra}>${help ? `<div class="sn-help">${help}</div>` : ''}</div>`;
  const qs = () => new URLSearchParams(location.search);
  const link = (tab, extras = {}) => { const params = new URLSearchParams({tab}); if (state.channel) params.set('channel', state.channel); for (const [key, value] of Object.entries(extras)) { if (value !== '' && value != null) params.set(key, value); else params.delete(key); } return '?' + params.toString(); };
  const navLink = (tab, text, extras = {}, css = 'sn-link') => `<a class="${css}" href="${esc(link(tab, extras))}" data-go>${text}</a>`;
  const clone = value => JSON.parse(JSON.stringify(value));
  const replyAttempts = new Map();
  let toastTimer;

  async function api(path, data, method) {
    const opts = {method: method || (data === undefined ? 'GET' : 'POST'), headers: {'X-CSRF-Token': root.dataset.csrf}};
    if (data instanceof FormData) opts.body = data;
    else if (data !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(data); }
    const response = await fetch(apiBase + path, opts);
    let result;
    try { result = await response.json(); } catch { throw new Error('Сервер не ответил. Изменения в форме сохранены на экране; попробуйте ещё раз.'); }
    if (!response.ok) throw new Error(result.error || 'Не удалось выполнить действие.');
    return result;
  }
  function toast(text, error = false) {
    const el = document.getElementById('sn-toast');
    clearTimeout(toastTimer); el.textContent = text; el.className = 'sn-toast' + (error ? ' error' : ''); el.hidden = false;
    toastTimer = setTimeout(() => el.hidden = true, error ? 9000 : 4200);
  }
  function showModal(title, html) {
    document.getElementById('sn-modal-title').textContent = title;
    document.getElementById('sn-modal-body').innerHTML = `<div id="sn-modal-error" class="sn-error" hidden></div>${html}`;
    if (!modal.open) modal.showModal();
  }
  function modalError(error) {
    const el = document.getElementById('sn-modal-error');
    if (modal.open && el) { el.textContent = error.message; el.hidden = false; el.scrollIntoView({block:'nearest'}); }
    else toast(error.message, true);
  }
  document.getElementById('sn-modal-close').onclick = () => modal.close();
  modal.addEventListener('click', e => { if (e.target === modal && e.offsetX < 0) modal.close(); });
  async function loadBoot() {
    const generation = state.generation;
    const data = await api('bootstrap' + (state.channel ? '?channel=' + state.channel : ''));
    if (generation !== state.generation) return;
    state.boot = data;
    const select = document.getElementById('sn-channel');
    select.innerHTML = options(state.boot.channels, state.channel, 'Все каналы'); select.value = state.channel;
    const unread = document.getElementById('sn-unread');
    unread.textContent = num(state.boot.stats.unread); unread.hidden = !state.boot.stats.unread;
  }
  async function navigate(url, push = true) {
    if (state.dirty && !window.confirm('В форме есть несохранённые изменения. Перейти без сохранения?')) return;
    sessionStorage.setItem('senler-scroll:'+location.search,String(window.scrollY));
    state.dirty = false; state.campaign = null; state.bot = null; state.dialog = null;
    if (push) history.pushState({}, '', url);
    await render();
  }
  window.addEventListener('beforeunload', event => { sessionStorage.setItem('senler-scroll:'+location.search,String(window.scrollY)); if (state.dirty) {event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('popstate', () => {state.dirty = false; state.campaign = null; state.bot = null; render().catch(e => toast(e.message, true));});
  document.getElementById('sn-channel').addEventListener('change', async e => {
    const previous = state.channel; state.channel = e.target.value;
    const next = link(state.tab);
    state.channel = previous;
    await navigate(next);
    e.target.value = state.channel;
  });
  document.addEventListener('click', event => {
    const target = event.target.closest('[data-go],#sn-nav a');
    if (!target || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    const url = target.closest('#sn-nav') ? link(target.dataset.tab) : target.getAttribute('href');
    navigate(url).catch(e => toast(e.message, true));
  });
  async function render() {
    const generation = ++state.generation;
    if (campaignTimer) { clearInterval(campaignTimer); campaignTimer = null; }
    const params = qs(); state.tab = tabs.includes(params.get('tab')) ? params.get('tab') : 'overview';
    state.channel = params.get('channel') || '';
    document.querySelectorAll('#sn-nav a').forEach(a => { a.classList.toggle('active', a.dataset.tab === state.tab); a.setAttribute('aria-current', a.dataset.tab === state.tab ? 'page' : 'false'); });
    main.innerHTML = '<div class="sn-spinner"><span class="spinner-border spinner-border-sm"></span> Загружаем…</div>';
    try {
      await loadBoot(); if (generation !== state.generation) return;
      if (state.tab === 'overview') renderOverview();
      if (state.tab === 'channels') renderChannels();
      if (state.tab === 'subscribers') await renderSubscribers();
      if (state.tab === 'campaigns') {
        if (params.has('edit')) await openCampaign(params.get('edit'));
        else if (params.has('id')) await renderCampaignDetail(params.get('id'));
        else await renderCampaigns();
      }
      if (state.tab === 'bots') {
        if (params.has('edit')) await openBot(params.get('edit'));
        else await renderBots();
      }
      if (state.tab === 'dialogs') await renderDialogs();
      if (generation === state.generation) window.scrollTo(0, Number(sessionStorage.getItem('senler-scroll:'+location.search)) || 0);
    } catch (error) { if (generation === state.generation) main.innerHTML = `<div class="sn-error">${esc(error.message)}<div class="mt-3">${btn('Повторить', 'refresh')}</div></div>`; }
  }
  function campaignRows(items) {
    return items.map(c => `<tr><td>${navLink('campaigns', esc(c.name), {id:c.id}, '')}<br><small>${date(c.created_at)}</small></td><td>${badge(c.status)}</td><td>${num(c.total)}<br><small>${c.audience.channels.map(id => esc(channelName(id))).join(', ')}</small></td><td>${num(c.counts.sent)} <small>из ${num(c.total)}</small><div class="sn-progress"><span style="width:${c.total ? Math.round((c.counts.sent || 0) / c.total * 100) : 0}%"></span></div></td><td>${num((c.counts.error || 0) + (c.counts.unknown || 0))}</td><td>${navLink('campaigns', '<i class="bi bi-arrow-right"></i>', {id:c.id}, 'sn-icon')}</td></tr>`).join('');
  }
  function campaignTable(items) {
    return `<div class="sn-panel flush"><div class="sn-table-scroll"><table class="sn-table"><thead><tr><th>Рассылка</th><th>Статус</th><th>Аудитория</th><th>Отправлено</th><th>Ошибки</th><th></th></tr></thead><tbody>${campaignRows(items)}</tbody></table></div></div>`;
  }
  function renderOverview() {
    const b = state.boot, stats = b.stats, subscriberStats = b.subscriber_stats || {};
    const selectedChannel = b.channels.find(channel => channel.id === Number(state.channel));
    main.innerHTML = head('Всё под контролем', 'Общайтесь с подписчиками и возвращайте их за новым заказом', navLink('campaigns','<i class="bi bi-plus-lg me-1"></i>Новая рассылка',{edit:'new'},'btn btn-primary')) +
      `<div class="sn-stats">${[['Активные подписчики',stats.active,'people','Подписки в выбранных каналах'],['Отправлено',stats.sent,'send-check','За последние 30 дней'],['Чат-боты',stats.active_bots,'diagram-3','Включённые сценарии'],['Требуют внимания',stats.errors,'exclamation-circle','Ошибки отправки за 30 дней']].map(([label,value,icon,sub]) => `<div class="sn-stat"><div class="sn-stat-label">${label}<i class="bi bi-${icon}"></i></div><strong>${num(value)}</strong><small>${sub}</small></div>`).join('')}</div>` +
      (!b.channels.length || !stats.active ? `<div class="sn-panel"><h3>От подключения до первой рассылки</h3><div class="sn-start">${navLink('channels','<span class="sn-step-num">1</span><strong>Подключите каналы</strong><p>Сообщество ВК и боты Telegram и MAX</p>',{},'')}${navLink('subscribers','<span class="sn-step-num">2</span><strong>Перенесите подписчиков</strong><p>Импортируйте базу Senler и разделите её на группы</p>',{},'')}${navLink('campaigns','<span class="sn-step-num">3</span><strong>Создайте рассылку</strong><p>Выберите аудиторию, сообщение и время отправки</p>',{edit:'new'},'')}</div></div>` : '') +
      head('Статистика подписчиков', selectedChannel ? `Канал: ${esc(selectedChannel.name)}` : 'Суммарно по всем каналам', state.channel ? navLink('overview','Показать все каналы',{channel:null}) : navLink('subscribers','Открыть базу <i class="bi bi-arrow-right ms-1"></i>')) +
      `<div class="sn-subscriber-kpis">${[['Вся база',subscriberStats.total,'Всего записей'],['Активные',subscriberStats.active,'Можно отправлять'],['Новые',subscriberStats.new_30,'За 30 дней'],['Ожидают',subscriberStats.pending,'Нужно подтверждение'],['Неактивные',(subscriberStats.unsubscribed || 0) + (subscriberStats.blocked || 0),'Отписались или запретили']].map(([label,value,sub]) => `<div class="sn-subscriber-kpi"><span>${label}</span><strong>${num(value)}</strong><small>${sub}</small></div>`).join('')}</div>` +
      `<div class="sn-subscriber-grid"><div class="sn-panel"><div class="sn-panel-title"><div><h3>Новые подписки за 30 дней</h3><p>По дате подтверждения или импорта</p></div><strong>${num(subscriberStats.new_30)}</strong></div>${subscriberActivityChart(b.subscriber_activity || [])}</div><div class="sn-panel"><div class="sn-panel-title"><div><h3>${state.channel ? 'Выбранный канал' : 'Подписчики по каналам'}</h3><p>Активные и вся база</p></div></div>${subscriberChannels(b.subscriber_channels || [])}</div></div>` +
      `<div class="sn-grid-2"><div class="sn-panel"><h3>Отправки за неделю</h3>${activityChart(b.activity)}</div><div class="sn-panel"><h3>Ваши каналы</h3>${b.channels.length ? b.channels.map(c => `<div class="d-flex justify-content-between align-items-center py-2"><div>${kindBadge(c.kind)} <span class="sn-small ms-1">${esc(c.name)}</span></div>${badge(c.status)}</div>`).join('') : '<p class="sn-muted sn-small">После подключения здесь появятся ваши сообщества и боты.</p>'}${navLink('channels','Управление каналами <i class="bi bi-arrow-right ms-1"></i>')}</div></div>` +
      head('Последние рассылки','',navLink('campaigns','Все рассылки <i class="bi bi-arrow-right ms-1"></i>')) +
      (b.recent.length ? campaignTable(b.recent) : empty('send','Здесь появится история рассылок','После первой отправки вы увидите её статус и результат.'));
  }
  function activityChart(activity) {
    const days = Array.from({length:7}, (_, i) => {
      const stamp = new Date((state.boot.server_time - (6 - i) * 86400) * 1000);
      const key = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Novosibirsk',year:'numeric',month:'2-digit',day:'2-digit'}).format(stamp);
      return {label:stamp.toLocaleDateString('ru-RU',{timeZone:'Asia/Novosibirsk',day:'numeric',month:'short'}), value:activity.find(a => a.day === key)?.n || 0};
    });
    const max = Math.max(1,...days.map(d => d.value));
    return `<div class="sn-bar-chart">${days.map(d => `<div class="sn-bar-col"><small>${num(d.value)}</small><div class="sn-bar" style="height:${Math.max(2,d.value / max * 95)}px"></div><small>${d.label}</small></div>`).join('')}</div>`;
  }
  function subscriberActivityChart(activity) {
    const formatter = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Novosibirsk',year:'numeric',month:'2-digit',day:'2-digit'});
    const days = Array.from({length:30}, (_, i) => {
      const stamp = new Date((state.boot.server_time - (29 - i) * 86400) * 1000);
      const key = formatter.format(stamp);
      return {label:stamp.toLocaleDateString('ru-RU',{timeZone:'Asia/Novosibirsk',day:'numeric',month:'short'}), value:activity.find(item => item.day === key)?.n || 0};
    });
    const max = Math.max(1,...days.map(day => day.value));
    return `<div class="sn-subscriber-chart">${days.map((day, index) => `<div class="sn-subscriber-bar-col" title="${esc(day.label)}: ${num(day.value)}"><small>${day.value ? num(day.value) : ''}</small><div class="sn-subscriber-bar" style="height:${Math.max(3,day.value / max * 116)}px"></div>${(index % 5 === 0 || index === days.length - 1) ? `<span>${day.label}</span>` : ''}</div>`).join('')}</div>`;
  }
  function subscriberChannels(channels) {
    if (!channels.length) return '<p class="sn-muted sn-small mb-0">Подключите канал, чтобы увидеть статистику.</p>';
    return `<div class="sn-channel-stats">${channels.map(channel => {
      const percent = channel.total ? Math.round(channel.active / channel.total * 100) : 0;
      return `<a href="${esc(link('overview',{channel:channel.id}))}" data-go class="sn-channel-stat"><div class="sn-channel-stat-head"><div>${kindBadge(channel.kind)} <strong>${esc(channel.name)}</strong></div><span>${num(channel.active)} <small>из ${num(channel.total)}</small></span></div><div class="sn-progress"><span style="width:${percent}%"></span></div><div class="sn-channel-stat-foot"><span>${percent}% активных</span><span>+${num(channel.new_30)} за 30 дней</span></div></a>`;
    }).join('')}</div>`;
  }
  function renderChannels() {
    main.innerHTML = head('Каналы','Подключение, состояние и ссылки для новых подписчиков',btn('<i class="bi bi-plus-lg me-1"></i>Подключить канал','channel-new','',true)) +
      (state.boot.channels.length ? `<div class="sn-channel-grid">${state.boot.channels.map(c => `<article class="sn-channel-card"><div class="d-flex justify-content-between">${kindBadge(c.kind)}${badge(c.status)}</div><h3>${esc(c.name)}</h3><div class="sn-muted sn-small">${c.username ? '@' + esc(c.username) : 'Ключ сохранён в CRM'}</div><div class="sn-stat-mini"><div><strong>${num(c.subscribers)}</strong>подписчиков</div><div><strong style="font-size:12px">${date(c.receive_mode === 'polling' ? Math.max(c.telegram_received_at || 0, c.webhook_at || 0) : c.webhook_at)}</strong>последнее событие</div></div>${telegramReceiveStatus(c)}${c.subscribe_url ? `<div class="sn-note">Ссылка для подписки<br><a href="${esc(c.subscribe_url)}" target="_blank" rel="noopener noreferrer">${esc(c.subscribe_url)}</a><div class="sn-help">Человек открывает диалог, запускает бота и подтверждает подписку.</div></div>` : ''}${c.last_error ? `<div class="sn-error">${esc(c.last_error)}</div>` : ''}<div class="sn-actions">${btn(c.status === 'connected' ? 'Проверить' : 'Подключить','channel-' + (c.status === 'connected' ? 'check' : 'connect'),`data-id="${c.id}"`,c.status !== 'connected')}${btn('Настройки','channel-edit',`data-id="${c.id}"`)}${c.status === 'connected' ? btn('Пауза','channel-pause',`data-id="${c.id}"`) : ''}</div></article>`).join('')}</div>` :
       empty('plug','Подключите первый канал','Для ВК понадобится ключ сообщества. Для Telegram и MAX — токен официального бота.',btn('Подключить канал','channel-new','',true))) +
      `<div class="sn-note mt-3">В каждом канале своя база подписок. Людей из ВК нельзя автоматически перенести в Telegram или MAX: они должны запустить соответствующего бота. Несколько каналов можно объединить в одной рассылке.</div>`;
  }
  function telegramReceiveStatus(channel) {
    if (channel.receive_mode !== 'polling' || channel.status !== 'connected') return '';
    if (channel.telegram_poll_error) return `<div class="sn-error">${esc(channel.telegram_poll_error)}</div>`;
    const current = channel.telegram_poll_at && state.boot.server_time - channel.telegram_poll_at < 90;
    // Healthy polling looks the same as VK/MAX (nothing extra); only surface this diagnostic
    // when there's actually something to check, since it has no equivalent on webhook channels.
    if (current) return '';
    return `<div class="sn-help mb-3">${channel.telegram_poll_at ? 'Приём сообщений не подтверждён. Обновите страницу через минуту.' : 'Ожидаем первое соединение с Telegram.'}${channel.telegram_poll_at ? '<br>Последняя связь: ' + date(channel.telegram_poll_at) : ''}</div>`;
  }
  function channelForm(id) {
    const channel = state.boot.channels.find(c => c.id === Number(id));
    showModal(channel ? 'Настройки канала' : 'Подключить канал', `<form id="sn-channel-form">${!channel ? `<div class="sn-field"><label for="ch-kind">Мессенджер</label><select class="form-select" id="ch-kind"><option value="vk">ВКонтакте — сообщество</option><option value="telegram">Telegram — бот</option><option value="max">MAX — бот</option></select></div>` : kindBadge(channel.kind)}${inputField('Название в CRM','ch-name',channel?.name || '', 'required maxlength="100" placeholder="Например, Папа Суши — ВК"')}<div id="ch-vk">${!channel ? inputField('Сообщество ВК','ch-id',qs().get('vk') || '', 'placeholder="https://vk.ru/papa_sushi"','Вставьте ссылку, короткое имя или числовой ID — CRM определит сообщество при проверке ключа.') : ''}</div>${inputField(channel ? 'Новый ключ (оставьте пустым, чтобы сохранить текущий)' : 'Ключ сообщества / токен бота','ch-token','','type="password" autocomplete="new-password"')}
        <div class="sn-panel mt-3"><h3>Кнопка отписки</h3>${inputField('Текст кнопки','ch-unsubscribe-label',channel?.unsubscribe_label || 'Отписаться','required maxlength="40"','До 40 символов. Нажатие отменяет подписку на рассылку.')}
        <label class="sn-check-row"><input id="ch-unsubscribe-enabled" type="checkbox" ${channel?.unsubscribe_enabled === 0 ? '' : 'checked'}><span>Показывать кнопку отписки</span></label>
        <div class="sn-help">Настройка действует для новых рассылок и сообщений бота. Сообщения, уже поставленные в очередь, сохраняют прежние настройки. Команды «Стоп» и /stop работают и без кнопки.</div></div>
        <div class="sn-panel mt-3"><h3>Приветствие новым подписчикам</h3>
        <div class="sn-field"><label for="ch-greeting-text">Текст приветствия</label><textarea class="form-control" id="ch-greeting-text" rows="3" maxlength="3500" required>${esc(channel?.greeting_text || DEFAULT_GREETING_TEXT)}</textarea></div>
        <div class="sn-field"><label for="ch-greeting-trigger">Когда отправлять</label><select class="form-select" id="ch-greeting-trigger">
          <option value="on_message" ${(channel?.greeting_trigger || 'on_message') === 'on_message' ? 'selected' : ''}>На любое входящее сообщение</option>
          <option value="off" ${channel?.greeting_trigger === 'off' ? 'selected' : ''}>Не отправлять автоматически</option>
        </select></div>
        <div class="sn-help" id="ch-greeting-help"></div></div>
        <div class="sn-panel mt-3"><h3>Сообщение при отмене подписки</h3>
        <div class="sn-field"><label for="ch-stop-text">Текст</label><textarea class="form-control" id="ch-stop-text" rows="3" maxlength="3500" required>${esc(channel?.stop_text || DEFAULT_STOP_TEXT)}</textarea></div>
        <div class="sn-help">Отправляется вместе с кнопкой «Подписаться», когда человек пишет «Стоп» или /stop, либо нажимает кнопку отписки.</div></div>
        <div id="ch-guide" class="sn-note"></div><div class="sn-modal-footer"><button class="btn btn-primary" type="submit">${channel ? 'Сохранить' : 'Сохранить и проверить'}</button></div></form>`);
    function guide() {
      const kind = channel?.kind || document.getElementById('ch-kind').value;
      document.getElementById('ch-vk').hidden = kind !== 'vk';
      document.getElementById('ch-guide').innerHTML = kind === 'vk' ? 'ВК → Управление сообществом → Работа с API → Создать ключ. Нужны права на сообщения, фотографии и управление настройками сообщества. Включите сообщения сообщества и возможности ботов. После проверки нажмите «Подключить» в карточке канала — CRM настроит приём событий.' : kind === 'telegram' ? 'Откройте <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>, создайте бота командой /newbot и скопируйте токен. Если бот уже есть, используйте его действующий токен.' : 'Создайте бота в <a href="https://business.max.ru/" target="_blank" rel="noopener">MAX для бизнеса</a> и скопируйте его токен. CRM использует официальный Bot API MAX.';
      document.getElementById('ch-greeting-help').textContent = kind === 'vk' ? 'В сообществе ВК люди часто пишут по другим вопросам, не связанным с рассылкой — по умолчанию бот не отвечает на входящие сами по себе. Подписаться можно в любой момент словом «Подписаться», даже при выключенной автоматической отправке.' : 'Показывается вместе с кнопкой «Подписаться» тому, кто ещё не подписан.';
      if (!channel) document.getElementById('ch-greeting-trigger').value = kind === 'vk' ? 'off' : 'on_message';
    }
    guide(); if (!channel) document.getElementById('ch-kind').onchange = guide;
    document.getElementById('sn-channel-form').onsubmit = async event => {
      event.preventDefault(); const button = event.submitter; button.disabled = true;
      try {
        const data = {name:document.getElementById('ch-name').value,token:document.getElementById('ch-token').value,unsubscribe_label:document.getElementById('ch-unsubscribe-label').value,unsubscribe_enabled:document.getElementById('ch-unsubscribe-enabled').checked,
          greeting_text:document.getElementById('ch-greeting-text').value,greeting_trigger:document.getElementById('ch-greeting-trigger').value,stop_text:document.getElementById('ch-stop-text').value};
        if (channel) await api(`channels/${channel.id}/edit`,data);
        else {
          const created = await api('channels',{...data,kind:document.getElementById('ch-kind').value,external_id:document.getElementById('ch-id').value});
          try { await api(`channels/${created.id}/check`,{}); toast('Ключ проверен. Теперь подключите приём сообщений.'); }
          catch(error) { toast('Канал сохранён. ' + error.message,true); }
        }
        modal.close(); await loadBoot(); renderChannels();
      } catch(error) { modalError(error); } finally { button.disabled = false; }
    };
  }
  function confirmConnect(id) {
    const channel = state.boot.channels.find(c => c.id === Number(id));
    showModal('Подключить «' + channel.name + '»',`<p class="sn-small">CRM начнёт принимать сообщения и нажатия кнопок этого ${channel.kind === 'vk' ? 'сообщества' : 'бота'}.</p><div class="sn-note">${channel.kind === 'vk' ? 'Для перехода отключите старые сценарии и рассылки в Senler, чтобы два сервиса не отвечали одновременно. CRM добавляет свой сервер событий и сохраняет остальные подключения ВК.' : 'Входящие события этого бота будут направлены в CRM. Если бот подключён к другому конструктору, завершите переход со старого сервиса перед включением новых сценариев.'}</div><div class="sn-modal-footer">${btn('Подключить приём сообщений','channel-connect-confirm',`data-id="${id}"`,true)}</div>`);
  }
  async function renderSubscribers() {
    const generation = state.generation;
    const params = qs(); const filter = new URLSearchParams();
    for (const key of ['channel','group','status','q','page']) if (params.get(key)) filter.set(key,params.get(key));
    const data = await api('subscribers?' + filter);
    if (generation !== state.generation) return;
    state.selected.clear();
    main.innerHTML = head('Подписчики',`${num(data.total)} записей · группы помогают выбирать аудиторию`,`${btn('Создать группу','group-new')}${btn('<i class="bi bi-upload me-1"></i>Импорт','import-new','',true)}`) +
      `<form id="sn-subscriber-filter" class="sn-filters"><input class="form-control" name="q" aria-label="Найти подписчика" placeholder="Имя, username или ID" value="${esc(params.get('q') || '')}"><select class="form-select" name="status" aria-label="Статус подписки">${[['','Все статусы'],['active','Подписаны'],['pending','Ждут подтверждения'],['unsubscribed','Отписались'],['blocked','Запретили сообщения']].map(([v,l]) => `<option value="${v}" ${params.get('status') === v ? 'selected' : ''}>${l}</option>`).join('')}</select><select class="form-select" name="group" aria-label="Группа">${options(state.boot.groups,params.get('group'),'Все группы')}</select><button class="btn btn-light">Найти</button><a class="btn btn-light ms-auto" href="${apiBase}subscribers/export?${esc(filter.toString())}"><i class="bi bi-download me-1"></i>Экспорт</a></form>
      <div id="sn-bulk" hidden></div>` +
      (data.items.length ? `<div class="sn-panel flush"><div class="sn-table-scroll"><table class="sn-table"><thead><tr><th><input type="checkbox" id="sn-select-all" aria-label="Выбрать страницу"></th><th>Подписчик</th><th>Канал</th><th>Группы</th><th>Статус</th><th>Добавлен</th></tr></thead><tbody>${data.items.map(s => `<tr><td><input type="checkbox" data-select="${s.id}" aria-label="Выбрать ${esc(s.name || s.external_user_id)}"></td><td><strong>${esc(s.name || 'Подписчик')}</strong><br><small>${esc(s.external_user_id)}${s.username ? ' · @' + esc(s.username) : ''}</small></td><td>${kindBadge(s.kind)}<br><small>${esc(s.channel_name)}</small></td><td>${esc(s.groups || '—')}</td><td>${badge(s.status)}</td><td>${date(s.created_at)}</td></tr>`).join('')}</tbody></table></div><div class="sn-pagination"><span>Страница ${data.page} из ${data.pages}</span><div class="sn-actions">${data.page > 1 ? navLink('subscribers','← Назад',{...Object.fromEntries(params),page:data.page-1},'btn btn-light') : ''}${data.page < data.pages ? navLink('subscribers','Далее →',{...Object.fromEntries(params),page:data.page+1},'btn btn-light') : ''}</div></div></div>` : empty('people',data.total ? 'Никого не нашли' : 'Добавьте первых подписчиков','Загрузите экспорт Senler или дайте людям ссылку на подключённого бота.',btn('Импортировать базу','import-new','',true))) +
      (state.boot.groups.length ? `<div class="sn-panel"><h3>Группы подписчиков</h3><div class="sn-actions">${state.boot.groups.map(g => `<span class="sn-group-pill">${navLink('subscribers',`${esc(g.name)} <span class="sn-muted">${num(g.members)}</span>`,{group:g.id},'btn btn-light')}<button type="button" class="sn-icon" data-action="group-delete" data-id="${g.id}" data-name="${esc(g.name)}" aria-label="Удалить группу «${esc(g.name)}»"><i class="bi bi-trash3"></i></button></span>`).join('')}</div></div>` : '');
    document.getElementById('sn-subscriber-filter').onsubmit = event => {
      event.preventDefault(); navigate(link('subscribers',Object.fromEntries(new FormData(event.target)))).catch(e => toast(e.message,true));
    };
    const all = document.getElementById('sn-select-all');
    if (all) all.onchange = () => { document.querySelectorAll('[data-select]').forEach(el => {el.checked = all.checked; all.checked ? state.selected.add(Number(el.dataset.select)) : state.selected.delete(Number(el.dataset.select));}); renderBulk(); };
    document.querySelectorAll('[data-select]').forEach(el => el.onchange = () => {el.checked ? state.selected.add(Number(el.dataset.select)) : state.selected.delete(Number(el.dataset.select)); renderBulk();});
  }
  function renderBulk() {
    const el = document.getElementById('sn-bulk'); el.hidden = !state.selected.size;
    el.innerHTML = `<div class="sn-selected"><span>Выбрано: ${state.selected.size}</span><select id="sn-bulk-group" class="form-select" style="width:210px">${options(state.boot.groups,'','Выберите группу')}</select>${btn('Добавить в группу','bulk-add_group')}${btn('Убрать из группы','bulk-remove_group')}${btn('Отписать','bulk-unsubscribe')}</div>`;
  }
  function groupForm() {
    showModal('Новая группа подписчиков',`<form id="sn-group-form">${inputField('Название','sn-group-name','','required maxlength="80" placeholder="Например, Любители сетов"')}<div class="sn-help">Один человек может состоять в нескольких группах. При рассылке он получит одно сообщение в каждом выбранном канале.</div><div class="sn-modal-footer"><button class="btn btn-primary">Создать группу</button></div></form>`);
    document.getElementById('sn-group-form').onsubmit = async e => {e.preventDefault();const b=e.submitter;b.disabled=true;try {await api('groups',{name:document.getElementById('sn-group-name').value});modal.close();await loadBoot();if(state.tab==='subscribers')await renderSubscribers();toast('Группа создана');}catch(error){modalError(error);}finally{b.disabled=false;}};
  }
  function importForm() {
    if (!state.boot.channels.length) { toast('Сначала добавьте канал, которому принадлежит база.'); navigate(link('channels')); return; }
    showModal('Перенести подписчиков',`<form id="sn-import-form"><div class="sn-field"><label for="imp-channel">В какой канал импортируем</label><select id="imp-channel" class="form-select" required>${options(state.boot.channels,state.channel)}</select></div><div class="sn-field"><label for="imp-file">Экспорт Senler или другой базы</label><input type="file" id="imp-file" class="form-control" accept=".txt,.csv" required><div class="sn-help">TXT — один ID на строку. CSV — колонка user_id, vk_user_id или ID, имя необязательно. До 50 000 записей и 4 МБ.</div></div><div class="sn-note">Выберите то же сообщество или бота, на которые подписывались люди. Отписавшихся и заблокировавших сообщения импорт не подпишет заново.</div><div class="sn-modal-footer"><button class="btn btn-primary">Посмотреть перед импортом</button></div></form>`);
    document.getElementById('sn-import-form').onsubmit = async e => {
      e.preventDefault();const button=e.submitter;button.disabled=true;
      try {const form=new FormData();form.set('channel_id',document.getElementById('imp-channel').value);form.set('file',document.getElementById('imp-file').files[0]);const data=await api('import/preview',form);importReview(data);}catch(error){modalError(error);}finally{button.disabled=false;}
    };
  }
  function importReview(data) {
    const s=data.summary;
    showModal('Проверьте импорт',`<p class="sn-small">Канал: <strong>${esc(data.channel_name)}</strong> · ${kindBadge(data.kind)}</p><div class="sn-stats" style="grid-template-columns:repeat(3,1fr)">${[['Новые',s.new],['Уже в базе',s.existing],['Ошибки ID',s.invalid]].map(([l,n])=>`<div class="sn-stat"><small>${l}</small><strong>${num(n)}</strong></div>`).join('')}</div><div class="sn-help">Повторы в файле: ${s.duplicates}. Отписавшиеся в файле: ${s.excluded}. Защищённые от повторной подписки в CRM: ${s.protected}.</div><div class="sn-table-scroll my-3"><table class="sn-table"><thead><tr><th>ID</th><th>Имя</th><th>Действие</th></tr></thead><tbody>${data.sample.map(s=>`<tr><td>${esc(s.external_user_id)}</td><td>${esc(s.name || '—')}</td><td>${s.excluded?'Исключить из рассылок':'Импортировать'}</td></tr>`).join('')}</tbody></table></div><div class="sn-field"><label for="imp-group">Сразу добавить в группу</label><select id="imp-group" class="form-select">${options(state.boot.groups,'','Без группы')}</select></div><label class="sn-check-row"><input id="imp-consent" type="checkbox"><span>Это действующие подписки на рассылку именно этого сообщества или бота. В файле учтены отписки.</span></label><div class="sn-modal-footer">${btn('Импортировать','import-confirm',`data-id="${data.id}"`,true)}</div>`);
  }
  async function renderCampaigns() {
    const generation = state.generation;
    const data=await api('campaigns'+(state.channel?'?channel='+state.channel:''));
    if (generation !== state.generation) return;
    main.innerHTML=head('Рассылки','Черновики, запланированные и отправленные сообщения',navLink('campaigns','<i class="bi bi-plus-lg me-1"></i>Новая рассылка',{edit:'new'},'btn btn-primary'))+
      (data.items.length?campaignTable(data.items):empty('send','Создайте первую рассылку','Сначала выберите аудиторию, затем составьте сообщение и назначьте время.',navLink('campaigns','Создать рассылку',{edit:'new'},'btn btn-primary')));
  }
  async function openCampaign(id) {
    const generation = state.generation;
    if (!state.boot.channels.length) {main.innerHTML=empty('plug','Сначала добавьте канал','Подключите сообщество ВК или бота, чтобы выбрать аудиторию рассылки.',navLink('channels','Подключить канал',{},'btn btn-primary'));return;}
    const campaign=id==='new'?{name:'',body:{text:'',asset_id:null,buttons:[]},audience:{channels:state.channel?[Number(state.channel)]:[],groups:[]},sendMode:'now',scheduled_at:''}:await api('campaigns/'+id);
    if (generation !== state.generation) return;
    state.campaign=campaign;
    if(state.campaign.status && state.campaign.status!=='draft'){await navigate(link('campaigns',{id}));return;}
    if(id!=='new'){state.campaign.sendMode=state.campaign.scheduled_at?'schedule':'now';state.campaign.scheduled_at=state.campaign.scheduled_at?new Date((state.campaign.scheduled_at+7*3600)*1000).toISOString().slice(0,16):'';}
    state.wizard=Math.min(3,Math.max(0,Number(qs().get('step'))||0));renderCampaignEditor();
  }
  function preview(body, channelId, savedSettings) {
    const channel=state.boot.channels.find(c=>c.id===Number(channelId));
    const name=channel?.name||'Ваше сообщество';
    const settings=savedSettings||channel;
    const unsubscribe=settings?.unsubscribe_enabled!==0;
    return `<aside class="sn-preview"><div class="sn-preview-title">Как увидит подписчик</div><div class="sn-chat-top"><i class="bi bi-chat-dots me-2"></i>${esc(name)}</div><div class="sn-bubble">${body.asset_id?`<img alt="Картинка сообщения" src="${apiBase}assets/${Number(body.asset_id)}">`:''}<span>${formatPreview((body.text||'Текст вашего сообщения появится здесь…').replaceAll('{имя}','Алексей').replaceAll('{name}','Алексей'))}</span></div>${(body.buttons||[]).map(b=>`<div class="sn-preview-button">${esc(b.label||'Подпись кнопки')}</div>`).join('')}${unsubscribe?`<div class="sn-preview-button">${esc(settings?.unsubscribe_label||'Отписаться')}</div>`:''}<div class="sn-preview-time">Пример сообщения</div></aside>`;
  }
  function messageFields(body, scope='campaign', nodes=[]) {
    return `<div class="sn-field" data-message-wrap><label>Текст сообщения</label><div class="sn-editor-toolbar" role="toolbar" aria-label="Форматирование текста"><button type="button" data-action="fmt-bold" title="Жирный" aria-label="Жирный"><i class="bi bi-type-bold"></i></button><button type="button" data-action="fmt-italic" title="Курсив" aria-label="Курсив"><i class="bi bi-type-italic"></i></button><button type="button" data-action="fmt-underline" title="Подчёркнутый" aria-label="Подчёркнутый"><i class="bi bi-type-underline"></i></button><button type="button" data-action="fmt-link" title="Ссылка" aria-label="Ссылка"><i class="bi bi-link-45deg"></i></button><button type="button" data-action="insert-name" title="Вставить имя подписчика" aria-label="Вставить имя подписчика"><i class="bi bi-person"></i></button><span class="sn-editor-toolbar-sep"></span><div class="dropdown"><button type="button" data-bs-toggle="dropdown" aria-expanded="false" title="Вставить эмодзи" aria-label="Вставить эмодзи"><i class="bi bi-emoji-smile"></i></button><div class="dropdown-menu dropdown-menu-end sn-emoji-menu">${EMOJIS.map(e=>`<button type="button" class="sn-emoji-btn" data-action="emoji-insert" data-emoji="${e}">${e}</button>`).join('')}</div></div></div><div class="form-control sn-message-editor" data-message-editor contenteditable="true" role="textbox" aria-multiline="true" aria-label="Текст сообщения" data-placeholder="Напишите предложение для подписчиков">${formatPreview(body.text)}</div><div class="d-flex justify-content-end sn-help"><span data-text-count>${body.text.length} / ${body.asset_id?900:3500}</span></div></div>
      <div class="sn-field"><label>Картинка</label>${body.asset_id?`<div><img class="sn-upload-preview" alt="Вложение" src="${apiBase}assets/${Number(body.asset_id)}"> ${btn('Убрать','asset-remove',`data-scope="${scope}"`)}</div>`:''}<input type="file" class="form-control" data-message-file data-scope="${scope}" accept="image/jpeg,image/png"><div class="sn-help">JPG или PNG, до 8 МБ. С картинкой текст короче: до 900 символов.</div></div>
      <div class="sn-field"><label>Кнопки под сообщением</label><div data-button-list>${(body.buttons||[]).map((b,i)=>`<div class="sn-button-row" data-button-index="${i}"><input class="form-control" data-button-label value="${esc(b.label)}" placeholder="Подпись кнопки" maxlength="40">${scope==='campaign'?`<input class="form-control" data-button-value value="${esc(b.value)}" placeholder="https://…">`:`<div class="d-flex gap-1"><select class="form-select" data-button-action style="max-width:100px"><option value="url" ${b.action==='url'?'selected':''}>Ссылка</option><option value="goto" ${b.action==='goto'?'selected':''}>К шагу</option></select>${b.action==='goto'?`<select class="form-select" data-button-value>${nodeOptions(nodes,b.value)}</select>`:`<input class="form-control" data-button-value value="${esc(b.value)}" placeholder="https://…">`}</div>`}<button type="button" class="sn-icon" data-action="button-remove" data-scope="${scope}" data-index="${i}" aria-label="Удалить кнопку"><i class="bi bi-x"></i></button></div>`).join('')}</div>${body.buttons.length<5?btn('<i class="bi bi-plus me-1"></i>Добавить кнопку','button-add',`data-scope="${scope}"`):''}<div class="sn-help">Текст и показ кнопки отписки задаются в настройках канала.</div></div>`;
  }
  function readMessage(container, current) {
    const editor = container.querySelector('[data-message-editor]');
    return {...current,text:editor?editorToText(editor):current.text,buttons:[...container.querySelectorAll('[data-button-index]')].map(row=>({label:row.querySelector('[data-button-label]').value,action:row.querySelector('[data-button-action]')?.value||'url',value:row.querySelector('[data-button-value]').value}))};
  }
  function collectCampaign() {
    const c=state.campaign;if(!c)return;
    if(state.wizard===0){c.name=document.getElementById('camp-name').value;c.audience.channels=[...main.querySelectorAll('[name="camp-channel"]:checked')].map(e=>Number(e.value));c.audience.groups=[...main.querySelectorAll('[name="camp-group"]:checked')].map(e=>Number(e.value));}
    if(state.wizard===1)c.body=readMessage(document.getElementById('sn-message-fields'),c.body);
    if(state.wizard===2){c.sendMode=main.querySelector('[name="send-mode"]:checked').value;c.scheduled_at=document.getElementById('camp-scheduled').value;}
  }
  function renderCampaignEditor() {
    const c=state.campaign;
    let content='';
    if(state.wizard===0)content=inputField('Внутреннее название рассылки','camp-name',c.name,'maxlength="120" placeholder="Например, Сеты на выходные"','Название видите только вы.')+`<div class="sn-field"><label>Кому отправляем</label>${state.boot.channels.map(ch=>`<label class="sn-check-row"><input type="checkbox" name="camp-channel" value="${ch.id}" ${c.audience.channels.includes(ch.id)?'checked':''}><span><strong>${esc(ch.name)}</strong><small>${kinds[ch.kind]} · ${num(ch.subscribers)} активных подписчиков</small></span>${badge(ch.status)}</label>`).join('')}</div><div class="sn-field"><label>Группы подписчиков</label><div class="sn-help mb-2">Если ничего не выбрано — все активные подписчики каналов. При выборе нескольких групп — люди из любой из них, без повторов.</div>${state.boot.groups.length?state.boot.groups.map(g=>`<label class="sn-check-row"><input type="checkbox" name="camp-group" value="${g.id}" ${c.audience.groups.includes(g.id)?'checked':''}><span>${esc(g.name)}</span><small>${num(g.members)}</small></label>`).join(''):'<p class="sn-muted sn-small">Группы можно создать в разделе «Подписчики».</p>'}</div>`;
    if(state.wizard===1)content=`<div id="sn-message-fields">${messageFields(c.body)}</div>`;
    if(state.wizard===2)content=`<h3>Когда отправить сообщение</h3><label class="sn-check-row"><input type="radio" name="send-mode" value="now" ${c.sendMode!=='schedule'?'checked':''}><span><strong>Сразу после запуска</strong><small>Сообщения будут отправляться постепенно из очереди</small></span></label><label class="sn-check-row"><input type="radio" name="send-mode" value="schedule" ${c.sendMode==='schedule'?'checked':''}><span><strong>В выбранное время</strong><small>Новосибирское время, UTC+7</small></span></label>${inputField('Дата и время','camp-scheduled',c.scheduled_at||'','type="datetime-local"')}<div class="sn-note">Перед каждой отправкой CRM проверяет статус подписки. Если человек отпишется после планирования, сообщение ему не уйдёт.</div>`;
    if(state.wizard===3)content=`<h3>${esc(c.name)}</h3><div class="sn-note"><strong>Аудитория</strong><br>${c.audience.channels.map(channelName).map(esc).join(', ')}<br>${c.audience.groups.length?c.audience.groups.map(groupName).map(esc).join(', '):'Все активные подписчики выбранных каналов'}<hr><strong id="sn-audience-count">Считаем получателей…</strong><hr><strong>Время отправки</strong><br>${c.sendMode==='schedule'?esc(c.scheduled_at.replace('T',' '))+' · UTC+7':'Сразу после нажатия «Запустить рассылку»'}</div><p class="sn-help mt-3">Сообщение и список получателей фиксируются при запуске. Можно приостановить или отменить оставшиеся отправки.</p>`;
    main.innerHTML=navLink('campaigns','<i class="bi bi-arrow-left"></i> К рассылкам',{},'sn-back')+head(c.id?'Редактирование рассылки':'Новая рассылка','Четыре понятных шага до отправки',btn('Сохранить черновик','campaign-save'))+`<div class="sn-panel"><ol class="sn-wizard">${['Аудитория','Сообщение','Время','Проверка'].map((s,i)=>`<li class="${i===state.wizard?'active':''}"><b>${i+1}</b>${s}</li>`).join('')}</ol><div class="sn-editor-layout"><div>${content}</div><div id="sn-campaign-preview">${preview(c.body,c.audience.channels[0])}</div></div><div class="sn-form-footer"><div>${state.wizard?btn('← Назад','campaign-prev'):''}</div>${state.wizard<3?btn('Далее →','campaign-next','',true):btn(c.sendMode==='schedule'?'Запланировать рассылку':'Запустить рассылку','campaign-launch','id="sn-launch" disabled',true)}</div></div>`;
    main.querySelectorAll('input,textarea,select,[contenteditable]').forEach(e=>e.addEventListener('input',()=>{state.dirty=true;collectCampaign();if(state.wizard===1){document.getElementById('sn-campaign-preview').innerHTML=preview(c.body,c.audience.channels[0]);const limit=c.body.asset_id?900:3500,count=main.querySelector('[data-text-count]');count.textContent=`${c.body.text.length} / ${limit}`;count.classList.toggle('sn-over-limit',c.body.text.length>limit);}}));
    if(state.wizard===2){const field=document.getElementById('camp-scheduled');field.disabled=c.sendMode!=='schedule';main.querySelectorAll('[name="send-mode"]').forEach(el=>el.addEventListener('change',()=>{field.disabled=el.value!=='schedule';}));}
    wireUploads();
    wireMessageEditor(main);
    history.replaceState({},'',link('campaigns',{edit:c.id||'new',step:state.wizard}));
    if(state.wizard===3)api('audience',c.audience).then(result=>{if(state.wizard!==3||state.campaign!==c)return;document.getElementById('sn-audience-count').textContent='Получателей: '+num(result.total);const button=document.getElementById('sn-launch');button.disabled=result.total===0;if(result.total)button.textContent=(c.sendMode==='schedule'?'Запланировать · ':'Отправить · ')+num(result.total);}).catch(e=>toast(e.message,true));
  }
  async function saveCampaign() {
    collectCampaign();const c=state.campaign;
    const saved=await api('campaigns',{id:c.id,name:c.name,body:c.body,audience:c.audience,scheduled_local:c.sendMode==='schedule'?c.scheduled_at:''});c.id=saved.id;state.dirty=false;
    history.replaceState({},'',link('campaigns',{edit:c.id,step:state.wizard}));return saved.id;
  }
  let campaignTimer = null;
  async function loadCampaignDetail(id, generation) {
    const c=await api('campaigns/'+id);
    if (generation !== state.generation) return null;
    const buttons=[btn('Создать копию','campaign-copy',`data-id="${id}"`)];
    if(c.status==='draft')buttons.push(navLink('campaigns','Продолжить',{edit:id},'btn btn-primary'));
    if(['running','scheduled'].includes(c.status))buttons.push(btn('Приостановить','campaign-pause',`data-id="${id}"`));
    if(c.status==='paused')buttons.push(btn('Продолжить отправку','campaign-resume',`data-id="${id}"`));
    if(['draft','paused','scheduled','running'].includes(c.status))buttons.push(btn('Отменить','campaign-cancel',`data-id="${id}"`));
    const statTiles=[['Получателей',num(c.total)],['Отправлено',num(c.counts.sent)],['В очереди',num((c.counts.pending||0)+(c.counts.sending||0))],['Ошибки / неизвестно',num((c.counts.error||0)+(c.counts.unknown||0))]];
    if(c.vk_sent)statTiles.push(['Прочитано в ВК',num(c.vk_read)+' из '+num(c.vk_sent)]);
    main.innerHTML=navLink('campaigns','<i class="bi bi-arrow-left"></i> К рассылкам',{},'sn-back')+head(esc(c.name),`Создана ${date(c.created_at)} · ${labels[c.status]}`,buttons.join(''))+
      `<div class="sn-stats">${statTiles.map(([l,v])=>`<div class="sn-stat"><div class="sn-stat-label">${l}</div><strong>${v}</strong></div>`).join('')}</div><div class="sn-editor-layout"><div><div class="sn-panel"><h3>Параметры рассылки</h3><p class="sn-small">${c.audience.channels.map(channelName).map(esc).join(', ')}</p><p class="sn-muted sn-small">${c.audience.groups.length?c.audience.groups.map(groupName).map(esc).join(', '):'Все активные подписчики'}</p><p class="sn-small">${c.scheduled_at?'Запланирована на '+date(c.scheduled_at)+' · UTC+7':'Отправка сразу после запуска'}</p>${badge(c.status)}</div>${c.counts.unknown?'<div class="sn-note mb-3">У некоторых сообщений неизвестен результат: сервис мог принять сообщение до обрыва соединения. CRM не повторяет их автоматически, чтобы не отправить дубль. Проверьте соответствующие диалоги.</div>':''}${c.deliveries.length?`<div class="sn-panel flush"><div class="sn-table-scroll"><table class="sn-table"><thead><tr><th>Подписчик</th><th>Статус</th><th>Подробности</th></tr></thead><tbody>${c.deliveries.map(d=>`<tr><td>${esc(d.name||d.external_user_id)}<br><small>${esc(d.channel_name)}</small></td><td>${badge(d.status)}</td><td>${esc(d.error||date(d.sent_at))}</td></tr>`).join('')}</tbody></table></div><div class="sn-pagination">До 200 последних результатов; ошибки показаны первыми.</div></div>`:''}</div>${preview(c.body,c.audience.channels[0],c.subscription_buttons?.[c.audience.channels[0]])}</div>`;
    return c;
  }
  async function renderCampaignDetail(id) {
    const generation = state.generation;
    if (campaignTimer) { clearInterval(campaignTimer); campaignTimer = null; }
    const c = await loadCampaignDetail(id, generation);
    if (c && ['running','scheduled'].includes(c.status)) {
      // Live progress while a broadcast is sending, without the visitor refreshing the page.
      campaignTimer = setInterval(async () => {
        if (generation !== state.generation) { clearInterval(campaignTimer); campaignTimer = null; return; }
        const next = await loadCampaignDetail(id, generation).catch(() => null);
        if (!next || !['running','scheduled'].includes(next.status)) { clearInterval(campaignTimer); campaignTimer = null; }
      }, 2500);
    }
  }
  async function renderBots() {
    const generation = state.generation;
    const data=await api('bots'+(state.channel?'?channel='+state.channel:''));
    if (generation !== state.generation) return;
    state.bots=data.items;
    main.innerHTML=head('Чат-боты','Сценарии из шагов: сообщения, кнопки, задержки и условия',navLink('bots','<i class="bi bi-plus-lg me-1"></i>Новый бот',{edit:'new'},'btn btn-primary'))+
      (data.items.length?`<div class="sn-bot-grid">${data.items.map(b=>`<article class="sn-channel-card"><div class="d-flex justify-content-between">${kindBadge(b.kind)}${b.status==='active'?'<span class="sn-badge green">Включён</span>':badge(b.status)}</div><h3>${esc(b.name)}</h3><div class="sn-muted sn-small">${esc(b.channel_name)} · ${b.trigger_type==='subscribe'?'После подписки':b.trigger_type==='keyword'?'Ключевые слова':'На входящее сообщение'}</div><div class="sn-stat-mini"><div><strong>${b.definition.nodes.length}</strong>шагов</div><div><strong>${num(b.running)}</strong>в сценарии</div><div><strong>${num(b.completed)}</strong>завершили</div></div>${b.has_changes&&b.version?'<span class="sn-badge orange">Есть неопубликованные изменения</span>':''}${b.errors?`<button type="button" class="sn-badge red ms-1" data-action="bot-errors" data-id="${b.id}" data-name="${esc(b.name)}">Ошибок: ${num(b.errors)}</button>`:''}<div class="sn-actions">${navLink('bots','Открыть редактор',{edit:b.id},'btn btn-light')}${b.status==='active'?btn('Пауза','bot-pause',`data-id="${b.id}"`):b.version?btn('Включить','bot-resume',`data-id="${b.id}"`):''}</div></article>`).join('')}</div>`:
       empty('diagram-3','Соберите первого бота','Начните с приветствия, добавьте меню с кнопками и проверьте разговор в симуляторе.',navLink('bots','Создать бота',{edit:'new'},'btn btn-primary'))) +
      '<div class="sn-note mt-3">Черновик можно менять отдельно от опубликованного сценария. Уже начавшиеся разговоры проходят ту версию, на которой были запущены. Если совпали несколько правил запуска, сработает бот с меньшим числом в поле «Приоритет».</div>';
  }
  async function showBotErrors(id, name) {
    const data = await api('bots/' + id + '/errors');
    const rows = data.items.map(it => `<div class="sn-note mb-2"><div class="d-flex justify-content-between align-items-start gap-2"><strong>${esc(it.subscriber_name || 'Подписчик ' + it.external_user_id)}</strong><span class="sn-muted sn-small text-nowrap">${date(it.error_at)}</span></div><div class="sn-small mb-1">${kindBadge(it.kind)} ${esc(it.channel_name)} · шаг «${esc(it.step_title || stepLabels[it.step_type] || 'неизвестно')}»</div><div class="sn-error mb-1">${esc(it.error || 'Причина не сохранена.')}</div><button type="button" class="sn-link" data-action="dialog-goto" data-id="${it.subscriber_id}">Открыть диалог →</button></div>`).join('');
    showModal('Ошибки бота «' + name + '»', data.items.length ? rows : '<p class="sn-muted">Ошибок не найдено — возможно, их уже исправили.</p>');
  }
  const newNode=()=>({id:'n'+secretsId().slice(0,8),type:'message',title:'',text:'',buttons:[],asset_id:null,next:''});
  function secretsId(){return crypto.randomUUID?crypto.randomUUID().replaceAll('-',''):String(Date.now())+Math.random().toString(36).slice(2);}
  async function openBot(id) {
    const generation = state.generation;
    if(!state.boot.channels.length){main.innerHTML=empty('plug','Сначала добавьте канал','Бот работает в конкретном сообществе или мессенджере.',navLink('channels','Подключить канал',{},'btn btn-primary'));return;}
    if(id==='new'){const node=newNode();node.title='Приветствие';state.bot={name:'',channel_id:Number(state.channel)||state.boot.channels[0].id,trigger_type:'subscribe',keywords:'',priority:10,definition:{entry:node.id,nodes:[node]}};}
    else {const data=await api('bots');if(generation!==state.generation)return;state.bot=data.items.find(b=>b.id===Number(id));if(!state.bot)throw new Error('Бот не найден');}
    state.node=qs().get('node')||state.bot.definition.entry;renderBotEditor();
  }
  function nodeOptions(nodes,selected,empty='Завершить сценарий') {return `<option value="">${empty}</option>`+nodes.map((n,i)=>`<option value="${n.id}" ${n.id===selected?'selected':''}>${i+1}. ${esc(n.title||stepLabels[n.type])}</option>`).join('');}
  function collectBot() {
    const b=state.bot;if(!b||!document.getElementById('bot-name'))return;
    b.name=document.getElementById('bot-name').value;b.channel_id=Number(document.getElementById('bot-channel').value);b.trigger_type=document.getElementById('bot-trigger').value;b.keywords=document.getElementById('bot-keywords').value;b.priority=Number(document.getElementById('bot-priority').value);b.definition.entry=document.getElementById('bot-entry').value;
    b.definition.nodes=b.definition.nodes.map(node=>{const el=document.querySelector(`[data-node="${node.id}"]`);if(!el)return node;const item={...node,title:el.querySelector('[data-node-title]').value,next:el.querySelector('[data-node-next]')?.value||''};if(node.type==='message')Object.assign(item,readMessage(el,node));if(node.type==='delay')item.minutes=Number(el.querySelector('[data-node-minutes]').value);if(['group','condition'].includes(node.type))item.group_id=Number(el.querySelector('[data-node-group]').value);if(node.type==='condition'){item.yes=el.querySelector('[data-node-yes]').value;item.no=el.querySelector('[data-node-no]').value;}if(node.type==='group')item.mode=el.querySelector('[data-node-mode]').value;return item;});
  }
  function botTemplateNote(b) {
    if(b.template_key!=='delivery_menu')return '';
    return '<div class="sn-note mb-3"><strong>Готовое меню доставки</strong><br>Заказ, акции, бонусная система, ссылка на оператора и адреса. Выберите шаг слева, чтобы изменить текст, ссылки или кнопки. Адреса, приложения и условия предложений заполнены по сайту Папа Суши; при их изменении обновите соответствующий шаг. Ваши правки сохраняются при обновлениях CRM.</div>';
  }
  function renderBotEditor() {
    const b=state.bot,nodes=b.definition.nodes;
    const active=nodes.find(n=>n.id===state.node)||nodes[0];state.node=active.id;
    history.replaceState({},'',link('bots',{edit:b.id||'new',node:active.id}));
    main.innerHTML=navLink('bots','<i class="bi bi-arrow-left"></i> К чат-ботам',{},'sn-back')+head(b.id?esc(b.name||'Редактор бота'):'Новый чат-бот','Соберите шаги и укажите, куда ведут кнопки',`${btn('<i class="bi bi-play-circle me-1"></i>Симулятор','bot-simulate')}${btn('Сохранить черновик','bot-save')}${btn('Опубликовать','bot-publish','',true)}`)+
      botTemplateNote(b)+
      `<div class="sn-panel"><div class="sn-step-cols">${inputField('Название бота','bot-name',b.name,'placeholder="Например, Приветствие и меню" maxlength="120"')}<div class="sn-field"><label for="bot-channel">Канал</label><select id="bot-channel" class="form-select" ${b.id?'disabled':''}>${options(state.boot.channels,b.channel_id)}</select></div><div class="sn-field"><label for="bot-trigger">Когда запускать</label><select id="bot-trigger" class="form-select"><option value="subscribe" ${b.trigger_type==='subscribe'?'selected':''}>После подписки / команды «Начать»</option><option value="keyword" ${b.trigger_type==='keyword'?'selected':''}>По ключевому слову</option><option value="any" ${b.trigger_type==='any'?'selected':''}>На любое входящее сообщение</option></select></div>${inputField('Приоритет','bot-priority',b.priority,'type="number" min="1" max="100"','При совпадении правил запускается бот с меньшим числом.')}</div><div id="bot-keywords-wrap" ${b.trigger_type!=='keyword'?'hidden':''}>${inputField('Ключевые слова через запятую','bot-keywords',b.keywords,'placeholder="меню, акция, промокод"','Сравниваем целое сообщение, без учёта регистра.')}</div><div class="sn-field mb-0"><label for="bot-entry">Первый шаг</label><select class="form-select" id="bot-entry">${nodeOptions(nodes,b.definition.entry,'Выберите шаг')}</select></div></div>
      <div class="sn-flow-bar">${Object.entries(stepLabels).map(([type,label])=>btn('<i class="bi bi-plus me-1"></i>'+label,'node-add',`data-type="${type}"`)).join('')}</div><div class="sn-bot-editor-grid"><aside class="sn-flow-outline"><div class="sn-eyebrow px-2">ШАГИ СЦЕНАРИЯ</div>${nodes.map((n,i)=>`<button type="button" class="${n.id===active.id?'active':''}" data-action="node-open" data-id="${n.id}"><span>${i+1}</span><div><strong>${esc(n.title||stepLabels[n.type])}</strong><small>${stepLabels[n.type]}${n.id===b.definition.entry?' · начало':''}</small></div></button>`).join('')}<p class="sn-help px-2 mt-3">Выберите шаг слева. Справа настройте сообщение и переходы.</p></aside><div id="sn-nodes">${renderNode(active,nodes.indexOf(active),nodes)}</div></div><div class="sn-form-footer"><span class="sn-help">Изменения начнут работать после публикации</span><div class="sn-actions">${btn('Симулятор','bot-simulate')}${btn('Сохранить черновик','bot-save','',true)}</div></div>`;
    main.querySelectorAll('input,textarea,select,[contenteditable]').forEach(el=>el.addEventListener('input',()=>{state.dirty=true;collectBot();if(el.id==='bot-trigger')document.getElementById('bot-keywords-wrap').hidden=el.value!=='keyword';}));
    main.querySelectorAll('[data-button-action]').forEach(el=>el.addEventListener('change',()=>{collectBot();renderBotEditor();}));
    wireMessageEditor(main);
    wireUploads();
  }
  function renderNode(n,index,nodes) {
    let fields='';
    if(n.type==='message')fields=messageFields(n,n.id,nodes);
    if(n.type==='delay')fields=`<div class="sn-field"><label>Подождать, минут</label><input type="number" min="1" max="43200" class="form-control" data-node-minutes value="${n.minutes||60}"><div class="sn-help">Сценарий продолжится даже после перезапуска CRM. До 30 дней.</div></div>`;
    if(['condition','group'].includes(n.type))fields=`<div class="sn-field"><label>Группа подписчиков</label><select class="form-select" data-node-group>${options(state.boot.groups,n.group_id)}</select>${!state.boot.groups.length?'<div class="sn-help">Сначала создайте группу в разделе «Подписчики».</div>':''}</div>`;
    if(n.type==='group')fields+=`<div class="sn-field"><label>Действие</label><select class="form-select" data-node-mode><option value="add" ${n.mode!=='remove'?'selected':''}>Добавить подписчика в группу</option><option value="remove" ${n.mode==='remove'?'selected':''}>Убрать из группы</option></select></div>`;
    if(n.type==='condition')fields+=`<div class="sn-step-cols"><div class="sn-field"><label>Состоит в группе →</label><select class="form-select" data-node-yes>${nodeOptions(nodes,n.yes)}</select></div><div class="sn-field"><label>Не состоит в группе →</label><select class="form-select" data-node-no>${nodeOptions(nodes,n.no)}</select></div></div>`;
    if(n.type==='handoff')fields='<div class="sn-note mb-3">Сценарий остановится, а диалог появится в начале списка у оператора. После «Вернуть бота» разговор продолжится со следующего шага.</div>';
    const hasGoto=n.type==='message'&&n.buttons.some(b=>b.action==='goto');
    if(n.type!=='condition')fields+=`<div class="sn-field mb-0"><label>${n.type==='handoff'?'После возвращения бота':'Следующий шаг'}</label><select class="form-select" data-node-next ${hasGoto?'disabled':''}>${nodeOptions(nodes,hasGoto?'':n.next)}</select>${hasGoto?'<div class="sn-help">Продолжение выбирает подписчик, нажимая кнопку.</div>':''}</div>`;
    return `<article class="sn-step-card" data-node="${n.id}"><div class="sn-step-header"><span class="sn-step-num">${index+1}</span><strong>${stepLabels[n.type]}</strong><div class="sn-actions"><button class="sn-icon" data-action="node-up" data-id="${n.id}" aria-label="Переместить выше" ${index===0?'disabled':''}><i class="bi bi-arrow-up"></i></button><button class="sn-icon" data-action="node-down" data-id="${n.id}" aria-label="Переместить ниже" ${index===nodes.length-1?'disabled':''}><i class="bi bi-arrow-down"></i></button><button class="sn-icon" data-action="node-delete" data-id="${n.id}" aria-label="Удалить шаг"><i class="bi bi-trash3"></i></button></div></div><div class="sn-field"><label>Название шага</label><input class="form-control" data-node-title value="${esc(n.title||'')}" placeholder="Для удобства в редакторе" maxlength="80"></div>${fields}</article>`;
  }
  async function saveBot() {
    collectBot();const b=state.bot;const result=await api('bots',b);b.id=result.id;state.dirty=false;history.replaceState({},'',link('bots',{edit:b.id,node:state.node}));return b.id;
  }
  function simulateBot() {
    collectBot();const definition=clone(state.bot.definition),nodes=definition.nodes;let current=definition.entry,visits=0;
    showModal('Проверка сценария',`<p class="sn-help">Локальная репетиция. «Алексей» — пример подписчика. Сообщения в мессенджеры не отправляются.</p><div class="sn-sim-log" id="sn-sim-log"></div><div class="sn-modal-footer">${btn('Начать заново','sim-restart')}</div>`);
    const log=document.getElementById('sn-sim-log');
    function line(html){const el=document.createElement('div');el.innerHTML=html;log.append(el);log.scrollTop=log.scrollHeight;return el;}
    function step(){
      if(!current){line('<div class="sn-note">Сценарий завершён.</div>');return;}
      if(++visits>80){line('<div class="sn-error">Слишком много переходов. Проверьте цикл сценария.</div>');return;}
      const node=nodes.find(n=>n.id===current);if(!node){line('<div class="sn-error">Шаг перехода не найден.</div>');return;}
      line(`<div class="sn-help">Шаг ${nodes.indexOf(node)+1} · ${esc(node.title||stepLabels[node.type])}</div>`);
      if(node.type==='message'){
        line(`<div class="sn-bubble">${node.asset_id?`<img src="${apiBase}assets/${node.asset_id}" alt="Вложение">`:''}${formatPreview((node.text||'Сообщение пока не заполнено').replaceAll('{имя}','Алексей').replaceAll('{name}','Алексей'))}</div>`);
        const buttons=line(node.buttons.map((b,i)=>`<button type="button" class="sn-preview-button" data-sim-button="${i}">${esc(b.label||'Кнопка')}</button>`).join(''));
        buttons.querySelectorAll('button').forEach(button=>button.onclick=()=>{const b=node.buttons[Number(button.dataset.simButton)];if(b.action==='url'){line(`<div class="sn-note">Открывается ссылка: ${esc(b.value)}</div>`);return;}buttons.querySelectorAll('button').forEach(el=>el.disabled=true);current=b.value;step();});
        if(node.buttons.some(b=>b.action==='goto'))return;
        current=node.next;step();
      }else if(node.type==='condition'){
        const box=line(`<div class="sn-note">Подписчик состоит в группе «${esc(groupName(node.group_id))}»?<div class="sn-actions mt-2"><button class="btn btn-light" data-answer="yes">Да</button><button class="btn btn-light" data-answer="no">Нет</button></div></div>`);
        box.querySelectorAll('button').forEach(b=>b.onclick=()=>{box.querySelectorAll('button').forEach(e=>e.disabled=true);current=node[b.dataset.answer];step();});
      }else if(node.type==='delay'||node.type==='handoff'){
        const box=line(`<div class="sn-note">${node.type==='delay'?'Пауза '+node.minutes+' мин.':'Диалог передан оператору'}<div class="mt-2"><button type="button" class="btn btn-light">${node.type==='delay'?'Пропустить ожидание':'Вернуть бота'}</button></div></div>`);
        box.querySelector('button').onclick=()=>{box.querySelector('button').disabled=true;current=node.next;step();};
      }else{line(`<div class="sn-note">${node.mode==='remove'?'Убрать из группы':'Добавить в группу'} «${esc(groupName(node.group_id))}»</div>`);current=node.next;step();}
    }
    step();
  }
  function wireUploads() {
    main.querySelectorAll('[data-message-file]').forEach(el=>el.onchange=async()=>{
      const file=el.files[0];if(!file)return;
      const scope=el.dataset.scope;
      if(scope==='campaign')collectCampaign();else collectBot();
      const form=new FormData();form.set('file',file);el.disabled=true;
      try{const asset=await api('assets',form);state.dirty=true;if(scope==='campaign'){state.campaign.body.asset_id=asset.id;renderCampaignEditor();}else{state.bot.definition.nodes.find(n=>n.id===scope).asset_id=asset.id;renderBotEditor();}}catch(error){toast(error.message,true);}finally{el.disabled=false;}
    });
  }
  async function renderDialogs() {
    const generation = state.generation;
    const data=await api('dialogs'+(state.channel?'?channel='+state.channel:''));
    if (generation !== state.generation) return;
    state.threads=data.items;
    const selected=Number(qs().get('thread'))||data.items[0]?.id;
    main.innerHTML=head('Диалоги','Вопросы подписчиков и разговоры, переданные оператору')+
      (data.items.length?`<div class="sn-dialogs"><div class="sn-thread-list">${data.items.map(s=>`<button class="sn-thread ${s.id===selected?'active':''}" data-action="dialog-open" data-id="${s.id}"><strong>${esc(s.name||'Подписчик '+s.external_user_id)} ${s.unread?`<span class="sn-badge red">${s.unread}</span>`:''}</strong><div class="mt-1">${kindBadge(s.kind)} ${s.bot_paused?'<span class="sn-badge orange">Ждёт оператора</span>':''}</div><p>${esc(s.last_message||'Новое обращение')}</p></button>`).join('')}</div><div id="sn-conversation" class="sn-conversation"></div></div>`:empty('chat-square-text','Пока нет разговоров','Диалоги появятся, когда подписчики напишут в подключённое сообщество или боту.'));
    if(selected)await openDialog(selected);
  }
  async function openDialog(id,refresh=false) {
    const generation=state.generation, request=state.dialogRequest=(state.dialogRequest||0)+1;
    const data=await api('dialogs/'+id);if(state.tab!=='dialogs'||generation!==state.generation||request!==state.dialogRequest)return;
    state.dialog=id;const sub=data.subscriber;const target=document.getElementById('sn-conversation');if(!target)return;
    const typed=refresh?target.querySelector('textarea')?.value||'':'';
    const scroll=target.querySelector('.sn-history');const wasBottom=!scroll||scroll.scrollHeight-scroll.clientHeight-scroll.scrollTop<45;const savedScroll=scroll?.scrollTop||0;
    target.innerHTML=`<div class="sn-conversation-head"><div><strong>${esc(sub.name||'Подписчик '+sub.external_user_id)}</strong><div class="mt-1">${badge(sub.status)} ${sub.bot_paused?'<span class="sn-badge orange">Оператор</span>':''}</div></div>${btn(sub.bot_paused?'Вернуть бота':'Взять диалог','dialog-'+(sub.bot_paused?'resume':'takeover'),`data-id="${id}"`)}</div><div class="sn-history">${data.messages.map(m=>`<div class="sn-message ${m.direction==='out'?'out':''}">${m.asset_id?`<img alt="Вложение" src="${apiBase}assets/${m.asset_id}">`:''}${formatPreview(m.text)}<time>${date(m.created_at)}</time></div>`).join('')}${data.pending.map(m=>`<div class="sn-message out">${formatPreview(m.text)}<div class="mt-2">${badge(m.status)}</div>${m.error?`<div class="sn-help">${esc(m.error)}</div>`:''}</div>`).join('')}</div><form class="sn-reply" id="sn-reply"><textarea class="form-control" rows="2" maxlength="3500" placeholder="Ответить подписчику…" aria-label="Ответ подписчику" ${sub.status!=='active'?'disabled':''}>${esc(typed)}</textarea><button class="btn btn-primary" ${sub.status!=='active'?'disabled':''} aria-label="Отправить ответ"><i class="bi bi-send"></i></button></form>`;
    target.querySelector('.sn-history').scrollTop=wasBottom?999999:savedScroll;
    document.querySelectorAll('.sn-thread').forEach(el=>el.classList.toggle('active',Number(el.dataset.id)===id));
    history.replaceState({},'',link('dialogs',{thread:id}));
    document.getElementById('sn-reply').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;const text=e.target.querySelector('textarea').value;let attempt=replyAttempts.get(id);if(!attempt||attempt.text!==text){attempt={text,key:secretsId()};replyAttempts.set(id,attempt);}try{await api('dialogs/'+id+'/reply',{text,request_key:attempt.key});replyAttempts.delete(id);if(state.tab==='dialogs'&&state.dialog===id)await openDialog(id);}catch(error){toast(error.message,true);}finally{button.disabled=false;}};
  }
  document.addEventListener('click', async event => {
    const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
    const action=button.dataset.action,id=button.dataset.id;button.disabled=true;
    try {
      if(action==='refresh')await render();
      else if(action==='channel-new')channelForm();
      else if(action==='channel-edit')channelForm(id);
      else if(action==='channel-connect')confirmConnect(id);
      else if(action==='channel-connect-confirm'){await api('channels/'+id+'/connect',{});modal.close();await loadBoot();renderChannels();toast('Канал подключён');}
      else if(action==='channel-check'||action==='channel-pause'){await api('channels/'+id+'/'+action.slice(8),{});await loadBoot();renderChannels();toast(action==='channel-check'?'Ключ канала работает':'Канал на паузе');}
      else if(action==='group-new')groupForm();
      else if(action==='group-delete'){if(!window.confirm('Удалить группу «'+button.dataset.name+'»? Подписчики останутся в базе, изменится только принадлежность к этой группе.'))return;await api('groups/'+id+'/delete',{});await loadBoot();await renderSubscribers();toast('Группа удалена');}
      else if(action==='import-new')importForm();
      else if(action==='import-confirm'){await api('import/confirm',{id,group_id:document.getElementById('imp-group').value,consent:document.getElementById('imp-consent').checked});modal.close();await loadBoot();await renderSubscribers();toast('База импортирована');}
      else if(action.startsWith('bulk-')){if(action==='bulk-unsubscribe'&&!window.confirm('Отписать выбранных людей от всех рассылок этого канала?'))return;await api('subscribers/bulk',{ids:[...state.selected],action:action.slice(5),group_id:document.getElementById('sn-bulk-group').value});await loadBoot();await renderSubscribers();toast('Изменения сохранены');}
      else if(action==='fmt-bold')applyFormat(button.closest('[data-message-wrap]').querySelector('[data-message-editor]'),'bold');
      else if(action==='fmt-italic')applyFormat(button.closest('[data-message-wrap]').querySelector('[data-message-editor]'),'italic');
      else if(action==='fmt-underline')applyFormat(button.closest('[data-message-wrap]').querySelector('[data-message-editor]'),'underline');
      else if(action==='fmt-link')applyLink(button.closest('[data-message-wrap]').querySelector('[data-message-editor]'));
      else if(action==='insert-name')insertEmoji(button.closest('[data-message-wrap]').querySelector('[data-message-editor]'),'{имя}');
      else if(action==='emoji-insert')insertEmoji(button.closest('[data-message-wrap]').querySelector('[data-message-editor]'),button.dataset.emoji);
      else if(action==='campaign-next'){collectCampaign();if(state.wizard===0&&(!state.campaign.name.trim()||!state.campaign.audience.channels.length))throw new Error('Введите название и выберите хотя бы один канал.');if(state.wizard===1&&!state.campaign.body.text.trim())throw new Error('Добавьте текст сообщения.');if(state.wizard===1&&state.campaign.body.text.length>(state.campaign.body.asset_id?900:3500))throw new Error('Слишком длинный текст сообщения. Сократите его.');if(state.wizard===2&&state.campaign.sendMode==='schedule'&&!state.campaign.scheduled_at)throw new Error('Выберите дату и время.');state.wizard++;renderCampaignEditor();}
      else if(action==='campaign-prev'){collectCampaign();state.wizard--;renderCampaignEditor();}
      else if(action==='campaign-save'){await saveCampaign();toast('Черновик сохранён');}
      else if(action==='campaign-launch'){const id=await saveCampaign();await api('campaigns/'+id+'/launch',{scheduled_at:state.campaign.sendMode==='schedule'?state.campaign.scheduled_at:null});state.dirty=false;await navigate(link('campaigns',{id}));toast('Рассылка поставлена в очередь');}
      else if(['campaign-copy','campaign-pause','campaign-resume','campaign-cancel'].includes(action)){if(action==='campaign-cancel'&&!window.confirm('Отменить все оставшиеся сообщения этой рассылки?'))return;const result=await api('campaigns/'+id+'/'+action.slice(9),{});await navigate(link('campaigns',action==='campaign-copy'?{edit:result.id}:{id}));}
      else if(['button-add','button-remove','asset-remove'].includes(action)){
        const scope=button.dataset.scope;if(scope==='campaign')collectCampaign();else collectBot();
        const body=scope==='campaign'?state.campaign.body:state.bot.definition.nodes.find(n=>n.id===scope);
        if(action==='button-add')body.buttons.push({label:'',action:'url',value:''});
        if(action==='button-remove')body.buttons.splice(Number(button.dataset.index),1);
        if(action==='asset-remove')body.asset_id=null;
        state.dirty=true;scope==='campaign'?renderCampaignEditor():renderBotEditor();
      }
      else if(action==='node-add'){
        collectBot();const nodes=state.bot.definition.nodes;if(nodes.length>=40)throw new Error('Можно добавить до 40 шагов.');const n={...newNode(),type:button.dataset.type,minutes:60,group_id:null,yes:'',no:'',mode:'add'};const last=nodes.at(-1);if(last&&!last.next&&last.type!=='condition'&&!last.buttons?.some(b=>b.action==='goto'))last.next=n.id;nodes.push(n);state.node=n.id;state.dirty=true;renderBotEditor();document.querySelector(`[data-node="${n.id}"]`).scrollIntoView({behavior:'smooth',block:'center'});
      }
      else if(action==='node-open'){collectBot();state.node=id;renderBotEditor();}
      else if(['node-delete','node-up','node-down'].includes(action)){
        collectBot();const nodes=state.bot.definition.nodes;const index=nodes.findIndex(n=>n.id===id);
        if(action==='node-delete'){if(nodes.length===1)throw new Error('Оставьте хотя бы один шаг.');nodes.splice(index,1);for(const node of nodes){for(const key of ['next','yes','no'])if(node[key]===id)node[key]='';for(const b of node.buttons||[])if(b.action==='goto'&&b.value===id)b.value='';}if(state.bot.definition.entry===id)state.bot.definition.entry=nodes[0].id;}
        else{const next=index+(action==='node-up'?-1:1);[nodes[index],nodes[next]]=[nodes[next],nodes[index]];}
        state.dirty=true;renderBotEditor();
      }
      else if(action==='bot-save'){await saveBot();toast('Черновик бота сохранён');}
      else if(action==='bot-publish'){const botId=await saveBot();await api('bots/'+botId+'/publish',{});await navigate(link('bots'));toast('Бот опубликован и включён');}
      else if(action==='bot-pause'||action==='bot-resume'){await api('bots/'+id+'/'+action.slice(4),{});await renderBots();}
      else if(action==='bot-simulate'||action==='sim-restart')simulateBot();
      else if(action==='bot-errors')await showBotErrors(id,button.dataset.name);
      else if(action==='dialog-goto'){modal.close();await navigate(link('dialogs',{thread:id}));}
      else if(action==='dialog-open')await openDialog(Number(id));
      else if(action==='dialog-takeover'||action==='dialog-resume'){await api('dialogs/'+id+'/'+action.slice(7),{});await openDialog(Number(id));}
    }catch(error){modalError(error);}finally{button.disabled=false;}
  });
  // Poll only the currently open conversation. Draft forms are never replaced.
  setInterval(()=>{if(state.tab==='dialogs'&&state.dialog&&!modal.open&&!document.hidden&&document.activeElement?.tagName!=='TEXTAREA')openDialog(state.dialog,true).catch(()=>{});},7000);
  render().catch(error=>toast(error.message,true));
})();
