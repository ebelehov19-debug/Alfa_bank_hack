// Local model UI. Model data is escaped before insertion into HTML.
const assessments = new Map();
let aiBusy = false;
let aiHealth = null;
let customTitle = '';
let customText = '';
let customResult = '';
const assessmentKey = (n, c = company(), role = state.role) =>
  JSON.stringify([c.company_id, role, n.title, n.summary]);
const assessment = n => assessments.get(assessmentKey(n));

function scoreBadge(n) {
  const r = assessment(n);
  if (!r) return '<span class="score">Не оценено</span>';
  if (r.error) return '<span class="score">Оценка недоступна</span>';
  return '<span class="score" title="Оценка модели, не вероятность">' +
    'Релевантность: <b>' + r.score + '%</b></span>';
}
function resultHTML(r) {
  if (r.error) return '<p class="ai-error">' + esc(r.error) + '</p>';
  const verdict = r.status === 'insufficient_data' ? 'Недостаточно данных — не отобрана' :
    r.relevant ? 'Релевантна' : 'Не релевантна';
  return '<div class="ai-result"><strong>' + verdict + ' · ' + r.score + '%</strong>' +
    '<p>' + esc(r.reason) + '</p>' +
    (r.evidence_verified ? '<small>Признак компании: ' + esc(r.company_quote) +
      '<br>Фрагмент новости: ' + esc(r.news_quote) + '</small>' : '') +
    '<small>' + esc(r.model) + ' · ' + r.elapsed_seconds + ' с' +
    (r.cached ? ' · из кеша' : '') + '</small></div>';
}
function assessmentHTML(n) {
  const r = assessment(n);
  return r ? resultHTML(r) : '';
}
function localPanel() {
  const status = aiHealth === null ? 'Проверка модели…' :
    aiHealth ? 'Модель готова · Qwen2.5 1.5B · CPU' :
    'Модель недоступна. Запустите bash run-local.sh и откройте http://localhost:8080';
  return '<section class="panel ai-panel"><div class="section-head"><h3>Локальная оценка релевантности</h3>' +
    '<button class="link-btn" id="ai-health">Проверить подключение</button></div>' +
    '<p id="ai-status" role="status">' + status + '</p>' +
    '<p>Процент — оценка значимости для выбранной компании, не вероятность и не проверка свежести. ' +
    'Строгий отбор: от 80% с подтверждёнными цитатами. Обработка по одной новости.</p>' +
    '<button class="red-button" id="ai-feed"' + (aiBusy ? ' disabled' : '') + '>' +
    (aiBusy ? 'Модель обрабатывает запрос…' : 'Оценить новости ленты') + '</button>' +
    '<details><summary>Проверить свою новость</summary><form id="ai-custom">' +
    '<label>Заголовок<input id="ai-title" maxlength="200" required value="' + esc(customTitle) + '"></label>' +
    '<label>Текст новости<textarea id="ai-text" maxlength="2500" rows="4" required>' + esc(customText) + '</textarea></label>' +
    '<button class="red-button" type="submit"' + (aiBusy ? ' disabled' : '') + '>Оценить для выбранной компании</button>' +
    '<div id="ai-custom-result" aria-live="polite">' + customResult + '</div></form></details></section>';
}
async function checkModel() {
  try {
    const r = await fetch('/api/health', {signal: AbortSignal.timeout(5000)});
    aiHealth = r.ok && (await r.json()).ready;
  } catch { aiHealth = false; }
  const target = document.querySelector('#ai-status');
  if (target) target.textContent = aiHealth ? 'Модель готова · Qwen2.5 1.5B · CPU' :
    'Модель недоступна. Запустите bash run-local.sh и откройте http://localhost:8080';
}
async function requestAssessment(n, c, role) {
  const key = assessmentKey(n, c, role);
  try {
    const response = await fetch('/api/relevance', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({company_id: c.company_id, role, title: n.title, text: n.summary}),
      signal: AbortSignal.timeout(190000)
    });
    const r = await response.json();
    if (!response.ok) throw new Error(r.error || 'Ошибка сервера');
    assessments.set(key, r);
    return r;
  } catch (error) {
    const r = {error: error.message === 'Failed to fetch' ?
      'Нет соединения с API. Запустите bash run-local.sh.' : error.message};
    assessments.set(key, r);
    return r;
  }
}
function setBusy(value) {
  aiBusy = value;
  document.querySelectorAll('[data-evaluate], #ai-feed, #ai-custom button[type="submit"]').forEach(b => b.disabled = value);
  const b = document.querySelector('#ai-feed');
  if (b) b.textContent = value ? 'Модель обрабатывает запрос…' : 'Оценить новости ленты';
}
async function evaluateNews(items) {
  if (aiBusy) return;
  const c = company(), role = state.role;
  setBusy(true);
  try {
    for (const n of items) {
      const r = await requestAssessment(n, c, role);
      if (document.querySelector('#cards')) renderCards();
      if (r.error) break;
    }
  } finally { setBusy(false); }
}
function bindLocalPanel() {
  const health = document.querySelector('#ai-health');
  if (!health) return;
  health.onclick = checkModel;
  document.querySelector('#ai-feed').onclick = () => evaluateNews(news);
  document.querySelector('#ai-title').oninput = e => { customTitle = e.target.value; customResult = ''; document.querySelector('#ai-custom-result').textContent = ''; };
  document.querySelector('#ai-text').oninput = e => { customText = e.target.value; customResult = ''; document.querySelector('#ai-custom-result').textContent = ''; };
  document.querySelector('#ai-custom').onsubmit = async e => {
    e.preventDefault();
    if (aiBusy) return;
    const c = company(), role = state.role;
    const n = {title: customTitle, summary: customText};
    setBusy(true);
    document.querySelector('#ai-custom-result').textContent = 'Оценка на CPU…';
    try {
      const r = await requestAssessment(n, c, role);
      if (company().company_id === c.company_id && state.role === role && customTitle === n.title && customText === n.summary) {
        customResult = '<p>Компания: ' + esc(c.company_id) + '</p>' + resultHTML(r);
        const target = document.querySelector('#ai-custom-result');
        if (target) target.innerHTML = customResult;
      }
    } finally { setBusy(false); }
  };
}
window.addEventListener('DOMContentLoaded', checkModel);
