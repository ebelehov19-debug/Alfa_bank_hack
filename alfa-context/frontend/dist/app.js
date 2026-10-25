'use strict';

let profiles = Array.isArray(window.CONTEXT_DATA) ? window.CONTEXT_DATA : [];
const state = {
  companyId: profiles[0]?.company_id || '', page: 'feed', filter: 'all', query: '',
  role: 'cfo', direction: 'incoming', items: [], health: null, available: false,
  booting: true, loading: false, feedError: false, notice: '', noticeKind: '', job: null,
  localProfiles: true
};
let saved;
try {
  const stored = JSON.parse(localStorage.getItem('alfa-saved-v2') || '[]');
  saved = new Set(Array.isArray(stored) ? stored.filter(key => typeof key === 'string') : []);
} catch { saved = new Set(); }

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? 'Нет данных').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));
const fraction = value => Number.isFinite(Number(value)) ? Math.max(0, Math.min(1, Number(value))) : 0;
const pct = value => value == null ? 'Нет данных' : new Intl.NumberFormat('ru', {style: 'percent', maximumFractionDigits: 1}).format(fraction(value));
const num = value => new Intl.NumberFormat('ru').format(Number(value) || 0);
const company = () => profiles.find(c => c.company_id === state.companyId);
const companyLabel = c => c.features?.MAINOKVED_NAME || c.company_id;
const score = item => Math.round(fraction(Number(item.relevance) / 100) * 100);
const savedKey = id => JSON.stringify([state.companyId, state.role, String(id)]);
const isSaved = item => saved.has(savedKey(item.id));
const textList = value => Array.isArray(value) ? value.filter(entry => typeof entry === 'string') : [];
const budgetMessage = 'Исчерпан общий дневной бюджет. Доступ восстановится после сброса в 03:00 МСК.';

function safeUrl(value) {
  if (typeof value !== 'string') return '';
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
  } catch { return ''; }
}

function sourceLink(title, url) {
  const href = safeUrl(url);
  return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(title || 'Первоисточник')} ↗</a>` : esc(title || 'Ссылка на публикацию недоступна');
}

function dateLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('ru', {day: 'numeric', month: 'long', year: 'numeric'}).format(date);
}

let version = 0, bootVersion = 0, feedVersion = 0, detailVersion = 0;
let contextController = new AbortController(), questionController = null, pollTimer = null;
const context = () => ({version, companyId: state.companyId, role: state.role, signal: contextController.signal});
const current = ctx => !ctx.signal.aborted && ctx.version === version && ctx.companyId === state.companyId && ctx.role === state.role;

class ApiError extends Error {
  constructor(status) { super('Запрос не выполнен'); this.status = status; }
}

async function request(path, options = {}, signal, timeoutMs = 30000) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener('abort', abort, {once: true});
  const timer = setTimeout(abort, timeoutMs);
  try {
    const response = await fetch(path, {
      ...options, signal: controller.signal, credentials: 'same-origin',
      headers: {'Accept': 'application/json', ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers}
    });
    if (!response.ok) throw new ApiError(response.status);
    return await response.json();
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

function closeDetail() {
  detailVersion++;
  questionController?.abort();
  questionController = null;
  if ($('#detail').open) $('#detail').close();
}

function stopContext() {
  version++;
  contextController.abort();
  contextController = new AbortController();
  clearTimeout(pollTimer);
  pollTimer = null;
  closeDetail();
  state.job = null;
}

function updateCompanies() {
  $('#companySelect').innerHTML = profiles.map(c => `<option value="${esc(c.company_id)}">${esc(c.company_id)} · ${esc(companyLabel(c))}</option>`).join('');
  $('#companySelect').value = state.companyId;
  $('#companySelect').disabled = profiles.length === 0;
}

async function initialize() {
  stopContext();
  const attempt = ++bootVersion;
  state.booting = true;
  state.available = false;
  state.health = null;
  state.items = [];
  state.loading = false;
  state.notice = '';
  state.feedError = false;
  render();
  if (!['http:', 'https:'].includes(window.location.protocol)) {
    state.booting = false;
    render();
    return;
  }
  const results = await Promise.allSettled([request('/api/health'), request('/api/companies')]);
  if (attempt !== bootVersion) return;
  if (results[0].status === 'fulfilled') {
    state.health = results[0].value;
    state.available = true;
  }
  if (results[1].status === 'fulfilled' && Array.isArray(results[1].value.companies) && results[1].value.companies.length) {
    profiles = results[1].value.companies;
    state.localProfiles = false;
    if (!company()) state.companyId = profiles[0].company_id;
  }
  state.booting = false;
  updateCompanies();
  render();
  if (state.available && state.companyId) await loadFeed(context());
}

async function loadFeed(ctx = context()) {
  if (!current(ctx) || !ctx.companyId || !state.available) return;
  const attempt = ++feedVersion;
  state.loading = true;
  state.feedError = false;
  render();
  try {
    const query = new URLSearchParams({company_id: ctx.companyId, role: ctx.role});
    const result = await request(`/api/feed?${query}`, {}, ctx.signal);
    if (!current(ctx) || attempt !== feedVersion) return;
    if (!Array.isArray(result.items)) throw new ApiError(502);
    state.items = result.items.filter(item => item && typeof item === 'object' && item.id != null && item.company_id === ctx.companyId && item.role === ctx.role);
  } catch (error) {
    if (!current(ctx) || attempt !== feedVersion) return;
    state.feedError = true;
  } finally {
    if (current(ctx) && attempt === feedVersion) {
      state.loading = false;
      render();
    }
  }
}

function changeSelection() {
  stopContext();
  state.items = [];
  state.notice = '';
  state.feedError = false;
  state.loading = false;
  render();
  loadFeed(context());
}

const canAnalyze = () => state.available && state.health?.configured && state.companyId && !state.job && !state.booting;
const jobStage = () => state.job?.stage || (state.job?.status === 'queued' ? 'queued' : 'analyzing');
const jobLabel = () => jobStage() === 'parsing' ? 'Собираем свежие новости…' : jobStage() === 'queued' ? 'Подготавливаем сбор…' : 'Анализируем новости…';
const jobHint = () => jobStage() === 'parsing' ? 'Загружаем публикации MOEX, ТАСС и Альфа-Банка' : jobStage() === 'queued' ? 'Ожидаем начала сбора новостей' : 'Сопоставляем публикации с контекстом компании';

async function analyze() {
  if (!canAnalyze()) return;
  const ctx = context();
  state.job = {id: '', started: Date.now(), status: 'queued', stage: 'queued'};
  state.notice = '';
  closeDetail();
  render();
  try {
    const result = await request('/api/analyze', {method: 'POST', body: JSON.stringify({company_id: ctx.companyId, role: ctx.role})}, ctx.signal);
    if (!current(ctx)) return;
    if (typeof result.job_id !== 'string' || !result.job_id) throw new ApiError(502);
    state.job.id = result.job_id;
    await pollJob(ctx, result.job_id);
  } catch (error) {
    if (!current(ctx)) return;
    state.job = null;
    state.notice = error.status === 409 ? 'Уже выполняется анализ или ответ. Дождитесь завершения и обновите ленту.' : 'Не удалось запустить анализ. Проверьте подключение и попробуйте ещё раз.';
    state.noticeKind = 'error';
    render();
  }
}

async function pollJob(ctx, id) {
  if (!current(ctx) || state.job?.id !== id) return;
  try {
    const job = await request(`/api/jobs/${encodeURIComponent(id)}`, {}, ctx.signal);
    if (!current(ctx) || state.job?.id !== id) return;
    if (job.status === 'completed') {
      state.job = null;
      if (job.result?.collection && state.health) state.health.news_available = true;
      const errors = Array.isArray(job.result?.errors) ? job.result.errors.length : Number(job.result?.counts?.errors) || 0;
      const budgetExceeded = Array.isArray(job.result?.errors) && job.result.errors.some(error => error?.code === 'budget_exceeded');
      state.notice = budgetExceeded ? `${budgetMessage} Уже полученные результаты доступны в ленте.` : errors ? 'Анализ завершён частично: некоторые публикации не удалось обработать. Доступные результаты показаны в ленте.' : 'Анализ завершён. Лента обновлена с учётом компании и вашей роли.';
      state.noticeKind = errors ? 'warning' : 'success';
      await loadFeed(ctx);
      return;
    }
    if (job.status === 'failed') {
      state.job = null;
      state.notice = typeof job.error === 'string' && job.error ? job.error : 'Не удалось завершить анализ. Попробуйте ещё раз. Ранее полученные результаты доступны в ленте.';
      state.noticeKind = 'error';
      await loadFeed(ctx);
      return;
    }
    if (!['queued', 'running'].includes(job.status)) throw new ApiError(502);
    if (Date.now() - state.job.started > 15 * 60 * 1000) {
      state.job = null;
      state.notice = 'Обработка занимает больше времени, чем ожидалось. Обновите ленту позже.';
      state.noticeKind = 'warning';
      render();
      return;
    }
    const stage = job.stage || (job.status === 'queued' ? 'queued' : 'analyzing');
    const changed = state.job.status !== job.status || state.job.stage !== stage;
    state.job.status = job.status;
    state.job.stage = stage;
    if (changed) render();
    pollTimer = setTimeout(() => pollJob(ctx, id), 1500);
  } catch (error) {
    if (!current(ctx) || state.job?.id !== id) return;
    state.job = null;
    state.notice = 'Связь с сервисом прервана. Анализ мог продолжиться; обновите ленту через некоторое время.';
    state.noticeKind = 'error';
    render();
  }
}

const pages = {feed: 'Моя лента', network: 'Связи компании', company: 'Бизнес-профиль', saved: 'Сохранённое', sources: 'Источники'};

function metric(label, value, help) {
  return `<div class="metric"><div class="metric-label">${esc(label)}</div><strong>${esc(value)}</strong><small>${esc(help)}</small></div>`;
}

function renderStatus() {
  const banner = $('#serviceStatus');
  banner.className = 'service-banner';
  if (state.booting) banner.innerHTML = '<span class="status-dot"></span><span>Подключаемся к сервису новостей…</span>';
  else if (!state.available) {
    banner.classList.add('warning');
    banner.innerHTML = '<span>Сервис новостей недоступен. Откройте приложение через запущенный сервер по адресу http://127.0.0.1:8080.</span><button class="link-btn" id="retryConnection">Повторить подключение</button>';
  } else if (!state.health?.configured) {
    banner.classList.add('warning');
    banner.innerHTML = '<span>Анализ станет доступен после настройки подключения моделей.</span><button class="link-btn" id="retryConnection">Проверить подключение</button>';
  } else if (state.job) banner.innerHTML = `<span class="status-dot"></span><span>${jobLabel()}</span>`;
  else if (!state.health.news_available) banner.innerHTML = '<span>Собранных новостей пока нет. Нажмите «Анализировать новости»: сначала сервис соберёт свежие публикации, затем подготовит разбор.</span>';
  else banner.innerHTML = '<span class="status-dot"></span><span>Выберите компанию и роль. При запуске анализа сервис сначала обновит новости.</span>';
  if ($('#retryConnection')) $('#retryConnection').onclick = initialize;
}

function card(item) {
  const type = item.type === 'opportunity' ? ['green', 'ВОЗМОЖНОСТЬ'] : item.type === 'risk' ? ['', 'РИСК'] : ['neutral', 'СОБЫТИЕ'];
  const date = dateLabel(item.published_at);
  return `<article class="card"><div class="card-top"><span class="tag ${type[0]}">${type[1]}</span><span>${esc(item.category || 'Новости')}</span><span class="score"><b>${score(item)}</b>/100</span></div><h2>${esc(item.title)}</h2><p>${esc(item.summary)}</p><div class="why"><strong>ПОЧЕМУ ЭТО МОЖЕТ БЫТЬ ВАЖНО</strong>${esc(item.connection || 'Связь с компанией не указана в разборе.')}</div><div class="article-meta">${esc(item.source || 'Источник')}${date ? ` · ${esc(date)}` : ''}</div><div class="card-bottom"><button class="link-btn" data-open="${esc(item.id)}">Разобрать влияние <span aria-hidden="true">↗</span></button><button class="bookmark ${isSaved(item) ? 'saved' : ''}" data-save="${esc(item.id)}" aria-label="${isSaved(item) ? 'Удалить из сохранённого' : 'Сохранить событие'}" aria-pressed="${isSaved(item)}">${isSaved(item) ? '★' : '☆'}</button></div></article>`;
}

function rail() {
  const c = company(), p = c.payments.incoming;
  return `<aside class="rail"><section class="panel"><div class="small-label">КОНТЕКСТ КОМПАНИИ</div><div class="company-emblem" style="margin-top:16px">▥</div><div class="company-name">${esc(companyLabel(c))}</div><div class="company-id">${esc(c.company_id)}</div><div class="key-row"><span>Регион</span><b>${esc(c.features.REGION)}</b></div><div class="key-row"><span>Основной ОКВЭД</span><b>${esc(c.features.MAINOKVED_CODE)}</b></div><div class="key-row"><span>Кредитные продукты</span><b>${c.features.CL_PRODUCT_LOA_MARK === '1.0' ? 'Есть' : 'Не отмечены'}</b></div><div class="hr"></div><button class="link-btn" data-go="company">Открыть бизнес-профиль ↗</button></section><section class="panel"><h3>Насколько полна картина?</h3><div class="key-row"><span>Отрасли плательщиков</span><b>${pct(p.sector_coverage)}</b></div><div class="progress"><div style="width:${fraction(p.sector_coverage) * 100}%"></div></div><p>Доля входящего объёма, для которой известна отрасль контрагента.</p><button class="link-btn" data-go="network">Посмотреть связи ↗</button></section><section class="panel"><h3>Основания разбора</h3><ul class="dot-list"><li>Признаки выбранной компании</li><li>Публикация первоисточника</li><li>Вывод модели с указанием неопределённости</li></ul><span class="hint">Аналитические материалы Альфа-Банка пока не загружены.</span></section></aside>`;
}

function renderFeed() {
  const p = company().payments;
  const count = state.page === 'saved' ? state.items.filter(isSaved).length : state.items.length;
  $('#view').innerHTML = `<div class="analysis-controls"><div><button class="red-button analyze-button" id="analyzeNews" ${canAnalyze() ? '' : 'disabled'}>${state.job ? jobLabel() : 'Анализировать новости'}</button><button class="link-btn refresh-button" id="refreshFeed" ${state.loading || state.booting || !state.available ? 'disabled' : ''}>Обновить ленту</button></div><span class="hint" role="status">${state.job ? jobHint() : 'Сначала собираем свежие новости, затем готовим разбор для компании и роли'}</span></div>${state.notice ? `<div class="analysis-notice ${esc(state.noticeKind)}" role="status">${esc(state.notice)}</div>` : ''}<div class="metrics">${metric(state.page === 'saved' ? 'Сохранённых событий' : 'Событий в ленте', num(count), 'Для выбранной компании и роли')}${metric('Входящих контрагентов', num(p.incoming.counterparty_count), 'По предоставленным платежам')}${metric('Концентрация топ-5', pct(p.incoming.top_5_concentration), 'Доля входящего объёма')}</div><div class="feed-layout"><section><div class="toolbar"><div class="tabs"><button data-filter="all">Все события</button><button data-filter="risk">Риски</button><button data-filter="opportunity">Возможности</button></div><input class="search" id="search" aria-label="Поиск событий" placeholder="Поиск по ленте" value="${esc(state.query)}"></div><div class="cards" id="cards" aria-live="polite" aria-busy="${state.loading}"></div></section>${rail()}</div>`;
  $('#analyzeNews').onclick = analyze;
  $('#refreshFeed').onclick = () => loadFeed(context());
  document.querySelectorAll('[data-filter]').forEach(button => {
    button.classList.toggle('active', button.dataset.filter === state.filter);
    button.onclick = () => { state.filter = button.dataset.filter; renderFeed(); };
  });
  $('#search').oninput = event => { state.query = event.target.value; renderCards(); };
  renderCards();
}

function renderCards() {
  const list = state.items.filter(item => (state.filter === 'all' || item.type === state.filter) && (state.page !== 'saved' || isSaved(item)) && `${item.title || ''} ${item.summary || ''} ${item.category || ''}`.toLowerCase().includes(state.query.toLowerCase())).sort((a, b) => score(b) - score(a));
  let message = state.page === 'saved' ? 'Для этой компании и роли пока нет сохранённых событий.' : 'Персональных разборов пока нет. Запустите анализ собранных новостей.';
  if (state.booting || state.loading) message = 'Загружаем ленту…';
  else if (!state.available) message = 'Подключитесь к сервису, чтобы загрузить новостную ленту. Профиль и связи доступны из локальных данных.';
  else if (state.feedError) message = 'Не удалось обновить ленту. Попробуйте ещё раз.';
  else if (state.items.length && !list.length) message = 'Событий с такими условиями нет. Измените фильтр или поисковый запрос.';
  else if (!state.health?.configured && !state.items.length) message = 'Анализ станет доступен после настройки подключения моделей.';
  else if (!state.health?.news_available && !state.items.length) message = 'Нажмите «Анализировать новости», чтобы собрать публикации и получить персональный разбор.';
  const warning = state.feedError && list.length ? '<div class="analysis-notice warning" role="status">Не удалось обновить ленту. Показаны ранее загруженные результаты.</div>' : '';
  $('#cards').innerHTML = warning + (list.map(card).join('') || `<div class="empty" role="status">${esc(message)}</div>`);
  bind();
}

function renderNetwork() {
  const c = company(), p = c.payments[state.direction], top = p.top_counterparties.slice(0, 6);
  const nodes = top.map((entry, i) => {
    const angle = i * 2 * Math.PI / top.length - Math.PI / 2;
    return {...entry, x: 250 + 180 * Math.cos(angle), y: 175 + 125 * Math.sin(angle)};
  });
  $('#view').innerHTML = `<div class="metrics">${metric('Контрагентов', num(p.counterparty_count), 'Самоплатежи исключены')}${metric('Известна отрасль', pct(p.sector_coverage), 'Взвешено по объёму платежей')}${metric('Концентрация топ-5', pct(p.top_5_concentration), 'Доля выбранного направления')}</div><div class="two-col"><section class="panel"><div class="section-head"><h3>Платёжное окружение</h3><select id="direction" class="direction" aria-label="Направление платежей"><option value="incoming">Входящие платежи</option><option value="outgoing">Исходящие платежи</option></select></div><svg class="network-svg" viewBox="0 0 500 350" role="img" aria-label="Шесть крупнейших контрагентов, соединённых с выбранной компанией">${nodes.map(entry => `<line x1="250" y1="175" x2="${entry.x}" y2="${entry.y}" stroke="#e4e6ec" stroke-width="${2 + fraction(entry.share) * 12}"/>`).join('')}<circle cx="250" cy="175" r="39" fill="#fff0ee"/><circle cx="250" cy="175" r="28" fill="#ec302b"/><text x="250" y="180" text-anchor="middle" style="fill:white;font-size:16px">А</text><text x="250" y="232" text-anchor="middle">${esc(c.company_id)}</text>${nodes.map(entry => `<circle cx="${entry.x}" cy="${entry.y}" r="20" fill="#f2f4f7" stroke="#dce0e8"/><text x="${entry.x}" y="${entry.y + 4}" text-anchor="middle">${Math.round(fraction(entry.share) * 100)}%</text><text x="${entry.x}" y="${entry.y + 35}" text-anchor="middle">${esc(entry.company_id)}</text>`).join('')}</svg><p>Толщина линии отражает долю платежей. Связь не доказывает отношения поставщика и покупателя.</p><table><thead><tr><th>Контрагент</th><th>Известная отрасль</th><th>Доля</th></tr></thead><tbody>${top.map(entry => `<tr><td>${esc(entry.company_id)}</td><td>${esc(entry.sector || 'Неизвестна')}</td><td>${pct(entry.share)}</td></tr>`).join('')}</tbody></table></section><section class="panel"><h3>Отрасли контрагентов</h3>${p.sector_shares.slice(0, 7).map(sector => `<div class="bar-row"><div><span>${esc(sector.sector)}</span><b>${pct(sector.share)}</b></div><div class="progress"><div style="width:${fraction(sector.share) * 100}%"></div></div></div>`).join('')}<div class="bar-row"><div><span>Отрасль неизвестна</span><b>${pct(p.sector_coverage == null ? null : 1 - fraction(p.sector_coverage))}</b></div></div><div class="hr"></div><h3>Другие типы связей</h3><div class="key-row"><span>Компании в той же группе</span><b>${num(c.group_members.length)}</b></div><div class="key-row"><span>Связаны общим физлицом</span><b>${num(c.shared_person_companies.length)}</b></div><p>Общее физлицо не означает владение или контроль. Группа взята из поля GROUP_ID.</p><p class="hint">Даты, валюта и период платежей не указаны. Доли относятся только к предоставленной выборке.</p></section></div>`;
  $('#direction').value = state.direction;
  $('#direction').onchange = event => { state.direction = event.target.value; renderNetwork(); };
}

function renderCompany() {
  const c = company(), f = c.features;
  const fields = [
    ['Идентификатор', c.company_id], ['Деятельность', f.MAINOKVED_NAME], ['ОКВЭД', f.MAINOKVED_CODE],
    ['Регион', f.REGION], ['Город', f.CITY], ['Возраст компании', f.COMPANY_AGE_YEARS_GRADATION],
    ['Стадия развития', f.ECO_MARK_LABEL], ['Группа компаний', f.GROUP_ID],
    ['Сотрудники по ФНС', f.AMOUNT_WORKERS_FNS_GRADATION], ['Выручка (единицы источника)', f.REVENUE_COMPANY],
    ['Наличие кредитных продуктов', f.CL_PRODUCT_LOA_MARK === '1.0' ? 'Да' : 'Не отмечено'], ['Страны в данных ФТС', f.FTS_COUNTRY_NAME]
  ];
  $('#view').innerHTML = `<div class="two-col"><section class="panel"><h3>Профиль ${esc(c.company_id)}</h3><div class="profile-grid">${fields.map(([key, value]) => `<div class="field"><small>${esc(key)}</small><div>${esc(value)}</div></div>`).join('')}</div></section><section class="panel"><h3>Что мы знаем и чего не знаем</h3><p>Поля рассчитаны из df_Uid_data_hack.csv.${state.localProfiles ? ' Используется сохранённый локальный срез профилей.' : ''} В списке компаний показаны виды деятельности, а не названия реальных организаций.</p><div class="source-box"><b>Можно использовать</b><br>Отрасль, регион, банковские продукты, группу и состав платёжных связей.</div><div class="source-box"><b>Нужно уточнить</b><br>Валюту и единицы сумм, период платежей, условия кредитования, назначения переводов.</div><div class="source-box"><b>Роль пользователя</b><br>Выбирается в интерфейсе. Исходный набор не содержит должности пользователя.</div><p>Конфликтующих полей у выбранной компании: ${num(Object.keys(c.conflicting_fields || {}).length)}.</p></section></div>`;
}

function renderSources() {
  const seen = new Set();
  const sources = state.items.filter(item => {
    const key = safeUrl(item.url) || String(item.source);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  $('#view').innerHTML = `<div class="two-col"><section class="panel"><h3>Данные компании</h3>${[['df_Uid_data_hack.csv', 'Профили компаний'], ['df_Uid_payment_hack.csv', 'Агрегированные платёжные связи'], ['df_Fid_Uid_link_hack (1).csv', 'Связи с физическими лицами']].map(([file, label]) => `<div class="source-box"><b>${esc(label)}</b><br>${esc(file)}<br><span class="hint">${state.localProfiles ? 'Локальный срез' : 'Профили сервиса'} · ${num(profiles.length)} компании</span></div>`).join('')}<h3>Аналитика Альфа-Банка</h3><p>Материалы ещё не загружены. Экспертная позиция банка недоступна; выводы AI не приписываются банку.</p></section><section class="panel"><h3>Первоисточники событий в ленте</h3>${sources.map(item => `<div class="source-box"><b>${esc(item.source || 'Источник публикации')}</b><br>${sourceLink(item.title, item.url)}</div>`).join('') || '<p>Ссылки на конкретные публикации появятся после анализа новостей.</p>'}<p class="hint">Факты из публикаций и выводы модели показаны отдельно в разборе события.</p></section></div>`;
}

function render() {
  document.querySelectorAll('nav button').forEach(button => button.classList.toggle('active', button.dataset.page === state.page));
  $('#feedCount').textContent = String(state.items.length);
  $('#crumb').textContent = pages[state.page];
  $('#title').textContent = state.page === 'feed' ? 'Важное для вашего бизнеса' : pages[state.page];
  $('#subtitle').textContent = state.page === 'network' ? 'Связи, концентрация и пробелы в данных.' : state.page === 'company' ? 'Подтверждённые признаки компании из исходных данных.' : 'События, связи и решения. С учётом вашей компании.';
  renderStatus();
  if (!company()) {
    $('#view').innerHTML = '<div class="empty">Профили компаний пока недоступны.</div>';
    return;
  }
  if (['feed', 'saved'].includes(state.page)) renderFeed();
  else if (state.page === 'network') renderNetwork();
  else if (state.page === 'company') renderCompany();
  else renderSources();
  bind();
}

function bind() {
  document.querySelectorAll('[data-open]').forEach(button => button.onclick = () => openDetail(button.dataset.open));
  document.querySelectorAll('[data-save]').forEach(button => button.onclick = () => {
    const key = savedKey(button.dataset.save);
    saved.has(key) ? saved.delete(key) : saved.add(key);
    try { localStorage.setItem('alfa-saved-v2', JSON.stringify([...saved])); } catch {}
    renderFeed();
  });
  document.querySelectorAll('[data-go]').forEach(button => button.onclick = () => {
    state.page = button.dataset.go;
    render();
    window.scrollTo(0, 0);
  });
}

function evidenceList(values, empty) {
  const entries = textList(values);
  return entries.length ? `<ul class="evidence-list">${entries.map(value => `<li>${esc(value)}</li>`).join('')}</ul>` : `<p class="hint">${esc(empty)}</p>`;
}

function openDetail(id) {
  const item = state.items.find(entry => String(entry.id) === id);
  if (!item) return;
  closeDetail();
  const ctx = context(), token = ++detailVersion;
  const date = dateLabel(item.published_at);
  $('#detailContent').innerHTML = `<div class="drawer"><div class="drawer-top"><span class="tag neutral">РАЗБОР СОБЫТИЯ</span><button class="close" aria-label="Закрыть">×</button></div><h2>${esc(item.title)}</h2><p>${esc(item.summary)}</p><div class="article-meta">${esc(item.source || 'Источник публикации')}${date ? ` · ${esc(date)}` : ''}</div><div class="why"><strong>КОНТЕКСТ ${esc(state.companyId)}</strong>${esc(item.connection)}</div><h3>Факты из публикации</h3>${evidenceList(item.facts, 'Перечень фактов не указан в разборе. Проверьте первоисточник.')}<h3>Возможное влияние · вывод модели</h3><p>${esc(item.impact)}</p><h3>Что можно проверить</h3><p>${esc(item.action || 'Следующее действие не указано.')}</p><h3>Неопределённость и ограничения</h3>${evidenceList(item.uncertainties, 'Дополнительные неопределённости не указаны. Вывод зависит от полноты исходных данных.')}<h3>Позиция Альфа-Банка</h3><div class="source-box">Аналитические материалы банка не загружены. Экспертная позиция банка недоступна.</div><h3>На чём основан разбор</h3><div class="source-box"><b>Профиль и платёжные связи компании</b><br>df_Uid_data_hack.csv / df_Uid_payment_hack.csv<br>Компания: ${esc(state.companyId)}</div><div class="source-box"><b>${esc(item.source || 'Публикация первоисточника')}</b><br>${sourceLink(item.title, item.url)}</div><h3>Уточнить по компании</h3><button class="question" data-question="coverage">Насколько полны данные о плательщиках?</button><button class="question" data-question="concentration">Какая концентрация входящих платежей?</button><form class="ask"><input aria-label="Вопрос по событию" placeholder="Ваш вопрос…" required maxlength="500" ${state.health?.configured && state.available ? '' : 'disabled'}><button class="red-button" type="submit" aria-label="Задать вопрос" ${state.health?.configured && state.available ? '' : 'disabled'}>↑</button></form>${!state.health?.configured ? '<p class="hint">Вопросы по разбору станут доступны после настройки подключения моделей.</p>' : ''}<div id="answer" aria-live="polite"></div></div>`;
  $('#detail .close').onclick = closeDetail;
  document.querySelectorAll('[data-question]').forEach(button => button.onclick = () => {
    questionController?.abort();
    const p = company().payments.incoming;
    const answer = button.dataset.question === 'coverage' ? `Отрасль известна для ${pct(p.sector_coverage)} входящего объёма. Остальные платежи включены в знаменатель. Неизвестные отрасли не угадываются.` : `Пять крупнейших плательщиков дают ${pct(p.top_5_concentration)} входящего объёма в выборке. Всего внешних плательщиков: ${num(p.counterparty_count)}. Это не обязательно доля выручки.`;
    $('#answer').innerHTML = `<div class="answer">${esc(answer)}</div>`;
  });
  $('#detail form').onsubmit = async event => {
    event.preventDefault();
    if (!current(ctx) || token !== detailVersion || !state.health?.configured) return;
    const input = $('#detail form input'), button = $('#detail form button'), question = input.value.trim();
    if (!question) return;
    questionController?.abort();
    const controller = new AbortController();
    questionController = controller;
    button.disabled = true;
    $('#answer').innerHTML = '<div class="answer">Готовим ответ с учётом публикации и данных компании…</div>';
    try {
      const result = await request('/api/questions', {method: 'POST', body: JSON.stringify({company_id: ctx.companyId, role: ctx.role, news_id: String(item.id), question})}, controller.signal, 120000);
      if (!current(ctx) || token !== detailVersion || controller.signal.aborted) return;
      if (result.error || typeof result.answer !== 'string' || !result.answer.trim()) {
        $('#answer').innerHTML = `<div class="answer">${esc(result.error?.code === 'budget_exceeded' ? budgetMessage : 'Не удалось подготовить ответ. Попробуйте ещё раз позже.')}</div>`;
        return;
      }
      const sources = Array.isArray(result.sources) ? result.sources.filter(source => source && typeof source === 'object') : [];
      $('#answer').innerHTML = `<div class="answer">${esc(result.answer)}${sources.length ? `<div class="answer-sources">${sources.map(source => sourceLink(source.title, source.url)).join('<br>')}</div>` : ''}</div>`;
    } catch (error) {
      if (current(ctx) && token === detailVersion && !controller.signal.aborted) $('#answer').innerHTML = '<div class="answer">Не удалось получить ответ. Попробуйте ещё раз.</div>';
    } finally {
      if (current(ctx) && token === detailVersion && questionController === controller) button.disabled = false;
    }
  };
  $('#detail').showModal();
}

updateCompanies();
$('#companySelect').onchange = event => { state.companyId = event.target.value; changeSelection(); };
$('#roleSelect').onchange = event => { state.role = event.target.value; changeSelection(); };
document.querySelectorAll('nav button').forEach(button => button.onclick = () => {
  state.page = button.dataset.page;
  closeDetail();
  render();
});
$('#detail').onclick = event => { if (event.target === $('#detail')) closeDetail(); };
$('#detail').addEventListener('cancel', event => { event.preventDefault(); closeDetail(); });
window.addEventListener('pagehide', stopContext);
initialize();
