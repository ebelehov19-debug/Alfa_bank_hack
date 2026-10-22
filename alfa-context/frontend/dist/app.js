const data = window.CONTEXT_DATA;
const names = ['Производство металлопрофиля', 'Оптовая торговля', 'Грузовые перевозки'];
const state = {
  company: 0,
  page: 'feed',
  filter: 'all',
  query: '',
  role: 'cfo',
  direction: 'incoming'
};
let saved;
try {
  saved = new Set(JSON.parse(localStorage.getItem('alfa-saved') || '[]'))
} catch {
  saved = new Set()
}
const $ = s => document.querySelector(s),
  esc = s => String(s ?? 'Нет данных').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  } [c]));
const pct = v => v == null ? 'Нет данных' : new Intl.NumberFormat('ru', {
  style: 'percent',
  maximumFractionDigits: 1
}).format(v);
const num = v => new Intl.NumberFormat('ru').format(v);
const company = () => data[state.company];
const news = [{
  id: 'rate',
  type: 'risk',
  category: 'Денежно-кредитная политика',
  title: 'Ставка остаётся высокой. Где искать влияние на ваш бизнес?',
  summary: 'Учебный сценарий: регулятор сохраняет ставку. Проверяем прямые и косвенные каналы влияния.',
  theme: 'У компании отмечено наличие кредитных продуктов. Условия и размер долга в данных отсутствуют.',
  impact: 'Высокая ставка может сохранять стоимость новых заимствований и рефинансирования. Влияние на действующие кредиты зависит от условий договоров.',
  action: 'Проверить график рефинансирования и долю кредитов с плавающей ставкой.',
  source: 'Банк России',
  url: 'https://www.cbr.ru/',
  weights: [92, 85, 79]
}, {
  id: 'logistics',
  type: 'risk',
  category: 'Логистика и контрагенты',
  title: 'Изменение стоимости перевозок: проверьте связанные отрасли',
  summary: 'Учебный сценарий: растут транспортные издержки. Оцениваем возможную связь с профилем и контрагентами.',
  theme: 'Платёжные связи позволяют выделить контрагентов, но не подтверждают назначение переводов и условия поставок.',
  impact: 'Если компания оплачивает перевозки, изменения тарифов могут затронуть расходы. Для перевозчика важно соотношение роста собственных затрат и тарифов.',
  action: 'Уточнить долю транспортных расходов и возможность пересмотра тарифов по договорам.',
  source: 'Минтранс России',
  url: 'https://mintrans.gov.ru/',
  weights: [77, 79, 95]
}, {
  id: 'support',
  type: 'opportunity',
  category: 'Развитие бизнеса',
  title: 'Новые меры поддержки: что проверить перед подачей заявки',
  summary: 'Учебный сценарий: объявлена программа поддержки. Проверяем отрасль, регион и критерии участия.',
  theme: 'ОКВЭД и регион регистрации доступны в профиле. Они помогают отобрать программы для дополнительной проверки.',
  impact: 'Потенциальная возможность зависит от опубликованных условий программы. Совпадение отрасли само по себе не гарантирует право на поддержку.',
  action: 'Сопоставить критерии конкретной программы с параметрами компании и сроками подачи.',
  source: 'Правительство России',
  url: 'https://government.ru/',
  weights: [85, 68, 70]
}, {
  id: 'demand',
  type: 'risk',
  category: 'Спрос и отраслевые связи',
  title: 'Замедление в смежной отрасли: насколько вы связаны с ней?',
  summary: 'Учебный сценарий: опубликован сигнал снижения отраслевого спроса. Ищем связи в платежах.',
  theme: 'Доли отраслей рассчитаны по входящим платежам из предоставленной выборки. Это не доли выручки компании.',
  impact: 'При подтверждении коммерческой природы платежей концентрация на одной отрасли может означать чувствительность к её спросу. Дат и назначения платежей недостаточно для оценки динамики.',
  action: 'Проверить крупнейших плательщиков и сопоставить поступления с фактическими продажами.',
  source: 'Росстат',
  url: 'https://rosstat.gov.ru/',
  weights: [81, 90, 83]
}];
const pages = {
  feed: 'Моя лента',
  network: 'Связи компании',
  company: 'Бизнес-профиль',
  saved: 'Сохранённое',
  sources: 'Источники'
};
$('#companySelect').innerHTML = data.map((c, i) => `<option value="${i}">${esc(c.company_id)} · ${names[i]}</option>`).join('');
$('#companySelect').onchange = e => {
  state.company = +e.target.value;
  render()
};
$('#roleSelect').onchange = e => {
  state.role = e.target.value;
  render()
};
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  state.page = b.dataset.page;
  render()
});
const score = n => Math.min(99, n.weights[state.company] + (state.role === 'treasury' && n.id === 'rate' ? 5 : state.role === 'ceo' && n.id === 'support' ? 10 : 0));
const connection = n => {
  const c = company();
  if (n.id === 'rate') return `Кредитные продукты: ${c.features.CL_PRODUCT_LOA_MARK==='1.0'?'есть':'не отмечены'}. ${n.theme}`;
  if (n.id === 'demand') {
    const s = c.payments.incoming.sector_shares[0];
    return s ? `Крупнейшая известная отрасль плательщиков: ${s.sector} — ${pct(s.share)} входящего объёма.` : n.theme
  }
  if (n.id === 'support') return `${c.features.REGION}. Основной ОКВЭД: ${c.features.MAINOKVED_CODE}. Условия программы требуют проверки.`;
  return n.theme
};

function metric(label, value, help) {
  return `<div class="metric"><div class="metric-label">${label}</div><strong>${value}</strong><small>${help}</small></div>`
}

function card(n) {
  return `<article class="card"><div class="card-top"><span class="tag ${n.type==='opportunity'?'green':''}">${n.type==='opportunity'?'ВОЗМОЖНОСТЬ':'РИСК'}</span><span>${n.category}</span><span class="score"><b>${score(n)}</b>/100 · демо</span></div><h2>${n.title}</h2><p>${n.summary}</p><div class="why"><strong>ПОЧЕМУ ЭТО МОЖЕТ БЫТЬ ВАЖНО</strong>${esc(connection(n))}</div><div class="card-bottom"><button class="link-btn" data-open="${n.id}">Разобрать влияние <span aria-hidden="true">↗</span></button><button class="bookmark ${saved.has(n.id)?'saved':''}" data-save="${n.id}" aria-label="${saved.has(n.id)?'Удалить из сохранённого':'Сохранить событие'}" aria-pressed="${saved.has(n.id)}">${saved.has(n.id)?'★':'☆'}</button></div></article>`
}

function rail() {
  const c = company(),
    p = c.payments.incoming;
  return `<aside class="rail"><section class="panel"><div class="small-label">КОНТЕКСТ КОМПАНИИ</div><div class="company-emblem" style="margin-top:16px">▥</div><div class="company-name">${names[state.company]}</div><div class="company-id">${esc(c.company_id)} · название отрасли для навигации</div><div class="key-row"><span>Регион</span><b>${esc(c.features.REGION)}</b></div><div class="key-row"><span>Основной ОКВЭД</span><b>${esc(c.features.MAINOKVED_CODE)}</b></div><div class="key-row"><span>Кредитные продукты</span><b>${c.features.CL_PRODUCT_LOA_MARK==='1.0'?'Есть':'Не отмечены'}</b></div><div class="hr"></div><button class="link-btn" data-go="company">Открыть бизнес-профиль ↗</button></section><section class="panel"><h3>Насколько полна картина?</h3><div class="key-row"><span>Отрасли плательщиков</span><b>${pct(p.sector_coverage)}</b></div><div class="progress"><div style="width:${(p.sector_coverage||0)*100}%"></div></div><p>Доля входящего объёма, для которой известна отрасль контрагента.</p><button class="link-btn" data-go="network">Посмотреть связи ↗</button></section><section class="panel"><h3>За каждым выводом — основания</h3><ul class="dot-list"><li>Признаки вашей компании</li><li>Первоисточник события</li><li>Экспертная позиция банка</li></ul><span class="hint">Источники новостей и аналитика банка ожидают подключения.</span></section></aside>`
}

function renderFeed() {
  const p = company().payments;
  $('#view').innerHTML = `<div class="metrics">${metric('Событий в демоленте','4','Смоделированные примеры')}${metric('Входящих контрагентов',num(p.incoming.counterparty_count),'По предоставленным платежам')}${metric('Концентрация топ-5',pct(p.incoming.top_5_concentration),'Доля входящего объёма')}</div><div class="feed-layout"><section><div class="toolbar"><div class="tabs"><button data-filter="all">Все события</button><button data-filter="risk">Риски</button><button data-filter="opportunity">Возможности</button></div><input class="search" id="search" aria-label="Поиск событий" placeholder="Поиск по ленте" value="${esc(state.query)}"></div><div class="cards" id="cards"></div></section>${rail()}</div>`;
  document.querySelectorAll('[data-filter]').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === state.filter);
    b.onclick = () => {
      state.filter = b.dataset.filter;
      render()
    }
  });
  $('#search').oninput = e => {
    state.query = e.target.value;
    renderCards()
  };
  renderCards()
}

function renderCards() {
  let list = news.filter(n => (state.filter === 'all' || n.type === state.filter) && (state.page !== 'saved' || saved.has(n.id)) && (n.title + ' ' + n.summary + ' ' + n.category).toLowerCase().includes(state.query.toLowerCase()));
  list.sort((a, b) => score(b) - score(a));
  $('#cards').innerHTML = list.map(card).join('') || '<div class="empty">Здесь пока нет событий.<br>Измените фильтр или сохраните событие из ленты.</div>';
  bind()
}

function renderNetwork() {
  const c = company(),
    p = c.payments[state.direction],
    top = p.top_counterparties.slice(0, 6);
  const nodes = top.map((e, i) => {
    const angle = i * 2 * Math.PI / top.length - Math.PI / 2;
    return {
      ...e,
      x: 250 + 180 * Math.cos(angle),
      y: 175 + 125 * Math.sin(angle)
    }
  });
  $('#view').innerHTML = `<div class="metrics">${metric('Контрагентов',num(p.counterparty_count),'Самоплатежи исключены')}${metric('Известна отрасль',pct(p.sector_coverage),'Взвешено по объёму платежей')}${metric('Концентрация топ-5',pct(p.top_5_concentration),'Доля выбранного направления')}</div><div class="two-col"><section class="panel"><div class="section-head"><h3>Платёжное окружение</h3><select id="direction" class="direction" aria-label="Направление платежей"><option value="incoming">Входящие платежи</option><option value="outgoing">Исходящие платежи</option></select></div><svg class="network-svg" viewBox="0 0 500 350" role="img" aria-label="Шесть крупнейших контрагентов, соединённых с выбранной компанией">${nodes.map(e=>`<line x1="250" y1="175" x2="${e.x}" y2="${e.y}" stroke="#e4e6ec" stroke-width="${2+e.share*12}"/>`).join('')}<circle cx="250" cy="175" r="39" fill="#fff0ee"/><circle cx="250" cy="175" r="28" fill="#ec302b"/><text x="250" y="180" text-anchor="middle" style="fill:white;font-size:16px">А</text><text x="250" y="232" text-anchor="middle">${esc(c.company_id)}</text>${nodes.map(e=>`<circle cx="${e.x}" cy="${e.y}" r="20" fill="#f2f4f7" stroke="#dce0e8"/><text x="${e.x}" y="${e.y+4}" text-anchor="middle">${Math.round(e.share*100)}%</text><text x="${e.x}" y="${e.y+35}" text-anchor="middle">${esc(e.company_id)}</text>`).join('')}</svg><p>Толщина линии отражает долю платежей. Связь не доказывает отношения поставщика и покупателя.</p><table><thead><tr><th>Контрагент</th><th>Известная отрасль</th><th>Доля</th></tr></thead><tbody>${top.map(e=>`<tr><td>${esc(e.company_id)}</td><td>${esc(e.sector||'Неизвестна')}</td><td>${pct(e.share)}</td></tr>`).join('')}</tbody></table></section><section class="panel"><h3>Отрасли контрагентов</h3>${p.sector_shares.slice(0,7).map(s=>`<div class="bar-row"><div><span>${esc(s.sector)}</span><b>${pct(s.share)}</b></div><div class="progress"><div style="width:${s.share*100}%"></div></div></div>`).join('')}<div class="bar-row"><div><span>Отрасль неизвестна</span><b>${pct(p.sector_coverage==null?null:1-p.sector_coverage)}</b></div></div><div class="hr"></div><h3>Другие типы связей</h3><div class="key-row"><span>Компании в той же группе</span><b>${c.group_members.length}</b></div><div class="key-row"><span>Связаны общим физлицом</span><b>${c.shared_person_companies.length}</b></div><p>Общее физлицо не означает владение или контроль. Группа взята из поля GROUP_ID.</p><p class="hint">Даты, валюта и период платежей не указаны. Доли относятся только к предоставленной выборке.</p></section></div>`;
  $('#direction').value = state.direction;
  $('#direction').onchange = e => {
    state.direction = e.target.value;
    renderNetwork()
  }
}

function renderCompany() {
  const c = company(),
    f = c.features;
  const fields = [
    ['Идентификатор', c.company_id],
    ['Деятельность', f.MAINOKVED_NAME],
    ['ОКВЭД', f.MAINOKVED_CODE],
    ['Регион', f.REGION],
    ['Город', f.CITY],
    ['Возраст компании', f.COMPANY_AGE_YEARS_GRADATION],
    ['Стадия развития', f.ECO_MARK_LABEL],
    ['Группа компаний', f.GROUP_ID],
    ['Сотрудники по ФНС', f.AMOUNT_WORKERS_FNS_GRADATION],
    ['Выручка (единицы источника)', f.REVENUE_COMPANY],
    ['Наличие кредитных продуктов', f.CL_PRODUCT_LOA_MARK === '1.0' ? 'Да' : 'Не отмечено'],
    ['Страны в данных ФТС', f.FTS_COUNTRY_NAME]
  ];
  $('#view').innerHTML = `<div class="two-col"><section class="panel"><h3>Профиль ${esc(c.company_id)}</h3><div class="profile-grid">${fields.map(([k,v])=>`<div class="field"><small>${k}</small><div>${esc(v)}</div></div>`).join('')}</div></section><section class="panel"><h3>Что мы знаем и чего не знаем</h3><p>Все поля взяты из df_Uid_data_hack.csv. Названия «${names[state.company]}» используются для навигации и не являются названиями реальных организаций.</p><div class="source-box"><b>Можно использовать</b><br>Отрасль, регион, банковские продукты, группу и состав платёжных связей.</div><div class="source-box"><b>Нужно уточнить</b><br>Валюту и единицы сумм, период платежей, условия кредитования, назначения переводов.</div><div class="source-box"><b>Роль пользователя</b><br>Выбирается в интерфейсе. Исходный набор не содержит должности пользователя.</div><p>Конфликтующих полей у выбранной компании: ${Object.keys(c.conflicting_fields).length}.</p></section></div>`
}

function renderSources() {
  $('#view').innerHTML = `<div class="two-col"><section class="panel"><h3>Подключённые данные</h3>${[['df_Uid_data_hack.csv','Профили компаний'],['df_Uid_payment_hack.csv','Агрегированные платёжные связи'],['df_Fid_Uid_link_hack (1).csv','Связи с физическими лицами']].map(([file,label])=>`<div class="source-box"><b>${label}</b><br>${file}<br><span style="color:var(--green)">Рассчитано локальным ядром · 3 профиля в прототипе</span></div>`).join('')}<h3>Аналитика Альфа-Банка</h3><p>Материалы ещё не загружены. Позиция банка не генерируется и не приписывается ему.</p></section><section class="panel"><h3>Планируемые источники новостей</h3>${news.map(n=>`<div class="source-box"><b>${n.source}</b> · не подключён<br><a target="_blank" rel="noopener noreferrer" href="${n.url}">Открыть официальный сайт ↗</a></div>`).join('')}<p>Это ссылки на сайты, а не подтверждения учебных событий. Автоматический сбор и ссылки на конкретные публикации появятся после подключения источников.</p></section></div>`
}

function render() {
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('active', b.dataset.page === state.page));
  $('#crumb').textContent = pages[state.page];
  $('#title').textContent = state.page === 'feed' ? 'Важное для вашего бизнеса' : pages[state.page];
  $('#subtitle').textContent = state.page === 'network' ? 'Увидеть связи. Понять концентрацию. Учесть пробелы в данных.' : state.page === 'company' ? 'Подтверждённые признаки компании из исходных данных.' : 'События, связи и решения. С учётом вашей компании.';
  if (['feed', 'saved'].includes(state.page)) renderFeed();
  else if (state.page === 'network') renderNetwork();
  else if (state.page === 'company') renderCompany();
  else renderSources();
  bind()
}

function bind() {
  document.querySelectorAll('[data-open]').forEach(b => b.onclick = () => openDetail(b.dataset.open));
  document.querySelectorAll('[data-save]').forEach(b => b.onclick = () => {
    saved.has(b.dataset.save) ? saved.delete(b.dataset.save) : saved.add(b.dataset.save);
    try {
      localStorage.setItem('alfa-saved', JSON.stringify([...saved]))
    } catch {}
    renderCards()
  });
  document.querySelectorAll('[data-go]').forEach(b => b.onclick = () => {
    state.page = b.dataset.go;
    render();
    window.scrollTo(0, 0)
  })
}

function openDetail(id) {
  const n = news.find(n => n.id === id);
  $('#detailContent').innerHTML = `<div class="drawer"><div class="drawer-top"><span class="tag">УЧЕБНЫЙ СЦЕНАРИЙ</span><button class="close" aria-label="Закрыть">×</button></div><h2>${n.title}</h2><p>${n.summary}</p><div class="why"><strong>КОНТЕКСТ ${esc(company().company_id)}</strong>${esc(connection(n))}</div><h3>Возможное влияние</h3><p>${n.impact}</p><h3>Что можно проверить</h3><p>${n.action}</p><h3>Позиция Альфа-Банка</h3><div class="source-box">Материалы банка не загружены. Экспертная оценка банка пока недоступна.</div><h3>На чём основан разбор</h3><div class="source-box"><b>Профиль и связи · реальные данные</b><br>df_Uid_data_hack.csv / df_Uid_payment_hack.csv<br>Компания: ${esc(company().company_id)}</div><div class="source-box"><b>Событие · смоделировано</b><br>Подтверждающей публикации нет.<br>Планируемый источник: <a href="${n.url}" target="_blank" rel="noopener noreferrer">${n.source} ↗</a></div><h3>Как будет работать агент</h3><ol class="steps"><li>План анализа и проверка контекста</li><li>Поиск новости и банковской аналитики</li><li>Проверка оснований и противоречий</li><li>Дополнительный поиск или отметка неопределённости</li></ol><p class="hint">Это схема будущей работы. Агентные вызовы в прототипе не выполняются.</p><h3>Уточнить по компании</h3><p>Два быстрых ответа рассчитаны из CSV. Свободные вопросы требуют подключения LLM.</p><button class="question" data-question="coverage">Насколько полны данные о плательщиках?</button><button class="question" data-question="concentration">Какая концентрация входящих платежей?</button><form class="ask"><input aria-label="Вопрос по событию" placeholder="Ваш вопрос…" required maxlength="500"><button class="red-button" type="submit">↑</button></form><div id="answer" aria-live="polite"></div></div>`;
  $('#detail .close').onclick = () => $('#detail').close();
  document.querySelectorAll('[data-question]').forEach(b => b.onclick = () => {
    const p = company().payments.incoming;
    $('#answer').innerHTML = `<div class="answer">${b.dataset.question==='coverage'?`Отрасль известна для ${pct(p.sector_coverage)} входящего объёма. Остальные платежи также включены в знаменатель. Неизвестные отрасли не угадываются.`:`Пять крупнейших плательщиков дают ${pct(p.top_5_concentration)} входящего объёма в выборке. Всего внешних плательщиков: ${p.counterparty_count}. Это не обязательно доля выручки.`}</div>`
  });
  $('#detail form').onsubmit = e => {
    e.preventDefault();
    $('#answer').innerHTML = '<div class="answer">Свободный AI-анализ ещё не подключён. Ваш вопрос не отправлен внешнему сервису. Пока доступны быстрые ответы выше.</div>'
  };
  $('#detail').showModal()
}
$('#detail').onclick = e => {
  if (e.target === $('#detail')) $('#detail').close()
};
render();
