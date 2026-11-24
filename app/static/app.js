const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
// На хостинге панель живёт в подпапке за CGI-скриптом: адрес API задаёт страница (window.API_BASE).
const API_BASE = window.STATIC_DEMO ? "" : (window.API_BASE || "");
const api = async (url, opt = {}) => {
  const r = await fetch(API_BASE + url, { headers: { "Content-Type": "application/json" }, ...opt,
    body: opt.body ? JSON.stringify(opt.body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
// Статичная демо-копия для сайта (tools/build_static.py) подменяет эти два адреса своими файлами.
const fileUrl = f => window.STATIC_DEMO ? window.STATIC_DEMO.file(f) : API_BASE + "/api/file?url=" + encodeURIComponent(f);
const sheetUrl = b => window.STATIC_DEMO ? window.STATIC_DEMO.sheet(b) : `${API_BASE}/api/agent/sheet?batch=${encodeURIComponent(b)}&sheet=sheet-01.png`;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = n => (n ?? 0).toLocaleString("ru-RU");
const pct = (a, b) => b ? Math.round(a * 100 / b) : 0;
const toast = t => { const el = $("#toast"); el.textContent = t; el.classList.add("on"); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove("on"), 3200); };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const VNAME = { approve: "принять", reject: "отклонить", rework: "вернуть", manual: "решает человек" };
const TSTATUS = { active: "идёт", paused: "на паузе", limit: "лимит исчерпан", moderation: "на модерации", rejected: "отклонено модератором", new: "не оплачено" };
const KIND = { order: "готовая услуга UNU", card_boost: "фото и фильтр цены → корзина, избранное", search_cart: "поиск → корзина",
  favorite: "поиск → избранное", review_like: "лайк отзыва", custom: "своё" };
const MP = { wb: "WB", ozon: "Ozon", ym: "Я.Маркет" };
const charts = {};
function chart(id, cfg) { charts[id]?.destroy(); charts[id] = new Chart($("#" + id), cfg); }
const localTime = iso => iso ? new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
const plural = (n, a, b, c) => { const m = n % 10, h = n % 100; return m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 12 || h > 14) ? b : c; };
const meter = (p, ok) => `<span class="meter"><i style="width:${Math.min(100, p)}%;background:${css(ok ? "--ok" : "--warn")}"></i></span>`;

// --- разделы ---
let tab = "promo";
$$(".tabs button").forEach(b => b.onclick = () => show(b.dataset.tab));
function show(t) {
  tab = t;
  $$(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach(s => s.classList.toggle("on", s.id === t));
  ({ promo: loadPromo, check: loadCheck, workers: loadWorkers, log: loadLog })[t]();
}
let sub = "queue";
$$(".sub button").forEach(b => b.onclick = () => showSub(b.dataset.sub));
function showSub(s) {
  sub = s;
  $$(".sub button").forEach(b => b.classList.toggle("on", b.dataset.sub === s));
  ["queue", "learn", "how"].forEach(x => $("#sub-" + x).classList.toggle("hidden", x !== s));
  loadCheck();
}

let lastSummary = null;
async function loadHeader() {
  const s = await api("/api/summary");
  lastSummary = s;
  $("#platform").textContent = s.platform === "demo" ? "демо-данные" : s.read_only ? "только чтение" : "";
  $("#qcount").textContent = s.queue || "";
  const sy = s.sync;
  // прогноз: на сколько дней хватит баланса при среднем расходе последних дней
  const spentDays = (s.days || []).filter(d => d.spent > 0), perDay = spentDays.reduce((a, d) => a + d.spent, 0) / (spentDays.length || 1);
  const left = !s.balance.error && perDay > 0 ? Math.floor(s.balance.balance / perDay) : null;
  $("#syncinfo").textContent = sy.error ? "Синхронизация не прошла: " + sy.error + (sy.ok_at ? ` (${localTime(sy.ok_at)})` : "") :
    sy.at ? (sy.running ? "Идёт синхронизация. " : "") + `Данные на ${localTime(sy.at).slice(-5)}` + (s.balance.error ? "" : `, баланс ${fmt(Math.round(s.balance.balance))} ₽`) +
      (left != null ? `, хватит на ${left} ${plural(left, "день", "дня", "дней")}` : "") : (sy.running ? "Первая синхронизация: идёт…" : "Первая синхронизация…");
  return s;
}

// ===================== ТОВАРЫ =====================
let promoData = [], openProject = null;
const delta = k => (k.now == null || k.was == null) ? '<span class="flat">—</span>' :
  k.was - k.now > 0 ? `<span class="up">▲ ${k.was - k.now}</span>` : k.was - k.now < 0 ? `<span class="down">▼ ${k.now - k.was}</span>` : '<span class="flat">без изменений</span>';
function spark(series) {
  const s = series.filter(x => x != null); if (s.length < 2) return "";
  const max = Math.max(...s), min = Math.min(...s), w = 96, h = 24;
  const pts = s.map((v, i) => `${(i * w / (s.length - 1)).toFixed(1)},${(2 + (v - min) * (h - 4) / Math.max(1, max - min)).toFixed(1)}`).join(" ");
  return `<svg width="${w}" height="${h}" class="spark" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${css("--accent")}" stroke-width="1.6"/></svg>`;
}

async function loadPosInfo() {
  const s = await api("/api/positions/status");
  const SRC = { demo: "демо-данные", mpstats: "MPStats", agent: "агент в браузере", wb_public: "поиск WB" };
  const job = s.jobs[0];
  $("#posInfo").textContent = `Места в выдаче: ${SRC[s.last_source || s.source] || s.source}` +
    (s.last_date ? `, на ${s.last_date.slice(8, 10)}.${s.last_date.slice(5, 7)}` : "") + (s.city ? `, ${s.city}` : "") + "." +
    (job && job.state !== "забрано" ? ` Задание ${job.name}: ${job.state}.` : "");
}
$("#posAgent").onclick = async () => {
  try { const r = await api("/api/positions/agent-job", { method: "POST" });
    toast(`Задание ${r.name} готово: ${r.keywords} ${plural(r.keywords, "запрос", "запроса", "запросов")}. В Claude Code: «проверь позиции ${r.name}»`); loadPosInfo(); }
  catch (e) { toast(e.message); }
};

async function loadPromo() {
  await loadHeader(); loadPosInfo();
  promoData = await api("/api/promo");
  fillTzProjects();
  if (openProject) return renderDetail(openProject);
  $("#promoTitle").textContent = "Товары";
  $("#backToList").classList.add("hidden"); $("#promoDetail").classList.add("hidden");
  $("#promoList").classList.remove("hidden"); $("#promoSummary").classList.remove("hidden");
  const done = promoData.reduce((a, d) => a + d.done24, 0), plan = promoData.reduce((a, d) => a + d.plan_day, 0);
  const spent = promoData.reduce((a, d) => a + d.week.spent, 0);
  const kws = promoData.flatMap(d => d.keywords), up = kws.filter(k => k.now != null && k.was != null && k.now < k.was).length;
  $("#promoSummary").innerHTML = promoData.length ? `
    <div class="fig"><b>${plan ? `${fmt(done)} из ${fmt(plan)}` : fmt(done)}</b><span>${plan ? "выполнено за сутки по плану" : "выполнено за сутки"}</span></div>
    <div class="fig"><b>${up} из ${kws.length}</b><span>запросов поднялись за неделю</span></div>
    <div class="fig"><b>${fmt(spent)} ₽</b><span>расход за 7 дней</span></div>` : "";
  loadUnassigned();
  renderGuide();
  if (!promoData.length) {
    $("#promoList").innerHTML = `<div class="empty">${lastSummary && lastSummary.platform !== "demo"
      ? "Идёт первая загрузка кабинета: задания и отчёты появятся через 1–3 минуты. Нажмите «Обновить», чтобы посмотреть."
      : "Товаров пока нет. Добавьте товар — потом дайте по нему ТЗ."}</div>`;
    return;
  }
  $("#promoList").innerHTML = `<div class="tablewrap"><table>
    <thead><tr><th>Товар</th><th>Запрос</th><th class="num">Место</th><th>За неделю</th><th>14 дней</th><th>Сегодня / план</th></tr></thead>
    ${promoData.map(d => {
      const p = d.project, kw = d.keywords.length ? d.keywords : [{ keyword: "", series: [] }], n = kw.length;
      const prodCell = `<td rowspan="${n}"><div class="pname">${esc(p.name)}${p.sku ? "" : ` <button class="link" data-setsku="${p.id}">указать артикул</button>`}<small>${esc(MP[p.marketplace] || p.marketplace)}${p.sku ? ", арт. " + esc(p.sku) : ""}, ${d.tasks.length} ТЗ</small></div></td>`;
      const planCell = d.plan_day ? `<td rowspan="${n}"><div class="plan">${meter(pct(d.done24, d.plan_day), pct(d.done24, d.plan_day) >= 80)}<span>${fmt(d.done24)} / ${fmt(d.plan_day)}</span></div></td>`
        : `<td rowspan="${n}"><div class="plan"><span>${fmt(d.done24)}</span><span class="muted">плана нет</span></div></td>`;
      return `<tbody class="prod" data-p="${p.id}" tabindex="0">${kw.map((k, i) => `<tr>${i ? "" : prodCell}
        <td>${k.keyword ? esc(k.keyword) : '<span class="muted">нет запросов</span>'}</td>
        <td class="num"><span class="place"${k.now == null && k.checked ? ` title="В первых ${k.depth || "страницах"} выдачи товара нет"` : ""}>${k.now ?? (k.checked ? (k.depth ? k.depth + "+" : "не найден") : "—")}</span></td><td>${k.keyword ? delta(k) : ""}</td><td>${spark(k.series || [])}</td>
        ${i ? "" : planCell}</tr>`).join("")}</tbody>`;
    }).join("")}</table></div>`;
  $$("[data-setsku]").forEach(b => b.onclick = async e => {
    e.stopPropagation();
    const sku = (prompt("Артикул товара на Wildberries (только цифры). Название панель возьмёт с маркетплейса.") || "").trim();
    if (!sku) return;
    try { const r = await api("/api/projects/" + b.dataset.setsku, { method: "PATCH", body: { sku } });
      toast(r.merged ? "Такой товар уже есть — задание перенесено к нему" : "Товар: " + r.name); loadPromo(); } catch (err) { toast(err.message); }
  });
  $$("tbody.prod").forEach(el => {
    const open = () => { openProject = +el.dataset.p; renderDetail(openProject); };
    el.onclick = open; el.onkeydown = e => { if (e.key === "Enter") open(); };
  });
}

// Подсказка «что делать дальше» на живом кабинете: без неё после ввода ключа непонятно, с чего начать.
function renderGuide() {
  const g = $("#guide"), s = lastSummary;
  if (!s || s.platform === "demo" || window.STATIC_DEMO) return g.classList.add("hidden");
  const unknown = promoData.filter(d => !d.project.sku).length, total = promoData.length;
  const sy = s.sync || {}, steps = [];
  if (!total) steps.push(["now", "Панель загружает задания и отчёты из кабинета UNU", "Обычно 1–3 минуты. Нажмите «Обновить» справа вверху."]);
  else {
    steps.push(["done", `Загружено заданий: ${promoData.reduce((a, d) => a + d.tasks.length, 0)}`, sy.running ? "Синхронизация ещё идёт — скрины докачиваются." : ""]);
    if (unknown) steps.push(["now", `Товар не определён у ${unknown} из ${total} заданий`,
      "API UNU не говорит, к какому товару относится задание. Откройте «Проверка» → «Как решается поток» → «Отдать на проверку ИИ»: ИИ проверит скрины и заодно прочитает с них артикул и запрос — товары подпишутся сами."]);
    else steps.push(["done", "Товары определены", ""]);
    steps.push([unknown ? "next" : "now", "Разберите очередь", `Вкладка «Проверка» → «Очередь»: ${fmt(s.queue || 0)} отчётов ждут вашего решения.` + (s.read_only ? " Панель в режиме «только чтение»: решения в UNU не уходят." : "")]);
  }
  g.classList.remove("hidden");
  g.innerHTML = "<h2>Что дальше</h2><ol>" + steps.map(([st, t, d]) => `<li class="${st}"><b>${esc(t)}</b>${d ? `<span>${esc(d)}</span>` : ""}</li>`).join("") + "</ol>";
}

// Задания, заведённые в кабинете площадки вручную: привязываем к товару, чтобы панель их вела.
async function loadUnassigned() {
  const box = $("#unassigned");
  let list = [];
  try { list = await api("/api/tasks/unassigned"); } catch (e) { list = []; }
  box.classList.toggle("hidden", !list.length || !!openProject);
  if (!list.length) return;
  const opts = promoData.map(d => `<option value="${d.project.id}">${esc(d.project.name)}</option>`).join("");
  box.innerHTML = `<h2>Задания из кабинета без товара: ${list.length} <span class="muted" style="font-weight:400">панель не ведёт их отчёты, пока задание не привязано к товару</span></h2>
    <div class="tablewrap"><table><thead><tr><th>Задание</th><th class="num">Цена</th><th class="num">Сделано / лимит</th><th class="num">Ждут проверки</th><th>Товар</th><th>Запрос и сценарий</th><th></th></tr></thead>
    <tbody>${list.map(t => `<tr data-t="${t.id}"><td>${esc(t.name)}<small class="muted"> № ${esc(t.ext_id)}</small></td>
      <td class="num">${fmt(t.price)} ₽</td><td class="num">${fmt(t.done_total)} / ${fmt(t.limit_total)}</td><td class="num">${fmt(t.check_total)}</td>
      <td>${opts ? `<select aria-label="Товар">${opts}</select>` : '<span class="muted">сначала добавьте товар</span>'}</td>
      <td><input aria-label="Запрос" placeholder="ключевой запрос" size="18">
        <select aria-label="Сценарий" data-sc style="margin-top:6px">
          <option value="Найти товар поиском по ключевому запросу с фильтром цены, открыть карточку, добавить в корзину и в избранное.">Сценарий: запрос + фильтр цены</option>
          <option value="Найти по ключевому запросу карточку конкурента, открыть её, в блоке «Похожие» найти наш товар, открыть его карточку, добавить в корзину и в избранное.">Сценарий: через карточку конкурента</option>
          <option value="">Сценарий неизвестен</option></select>
        <label class="muted" style="display:block;margin-top:6px;font-size:12px"><input type="checkbox" data-mv> перенести в папку товара в UNU (там действует лимит «1 человек — 1 задание»)</label></td>
      <td class="num">${opts ? '<button class="btn small">Привязать</button>' : ""}</td></tr>`).join("")}</tbody></table></div>`;
  $$("#unassigned tr[data-t] button").forEach(b => b.onclick = async () => {
    const tr = b.closest("tr");
    try {
      await api("/api/tasks/" + tr.dataset.t, { method: "PATCH",
        body: { project_id: +tr.querySelector("select").value, keyword: tr.querySelector("input").value,
          descr: tr.querySelector("[data-sc]").value, move: tr.querySelector("[data-mv]").checked } });
      toast("Задание привязано, отчёты подтянутся при синхронизации"); loadPromo();
    } catch (e) { toast(e.message); }
  });
}

function renderDetail(pid) {
  const d = promoData.find(x => x.project.id === pid); if (!d) { openProject = null; return loadPromo(); }
  const p = d.project;
  $("#promoTitle").textContent = p.name;
  $("#backToList").classList.remove("hidden"); $("#promoList").classList.add("hidden"); $("#promoSummary").classList.remove("hidden");
  $("#promoDetail").classList.remove("hidden");
  $("#promoSummary").innerHTML = d.keywords.map(k => `<div class="fig"><b>${k.now ?? (k.checked ? (k.depth ? `ниже ${k.depth}-го` : "не найден") : "—")}${k.now != null ? " место" : ""}</b><span>«${esc(k.keyword)}», за неделю ${delta(k)}</span></div>`).join("") +
    `<div class="fig"><b>${d.plan_day ? `${fmt(d.done24)} из ${fmt(d.plan_day)}` : fmt(d.done24)}</b><span>выполнено за сутки</span></div>
     <div class="fig"><b>${fmt(d.week.spent)} ₽</b><span>расход за 7 дней, ${fmt(d.week.n)} выполнений</span></div>`;
  $("#promoDetail").innerHTML = `
    <div class="panel chartbox"><h2>Место в выдаче ${MP[p.marketplace] || ""} и выполнения по дням <span class="muted" style="font-weight:400">1-е место вверху: чем выше линия, тем выше товар в выдаче</span></h2><canvas id="chPromo" height="88"></canvas></div>
    <div class="panel"><div class="tablewrap"><table>
      <thead><tr><th>ТЗ</th><th>Запрос</th><th>Сегодня / план</th><th class="num">Всего / лимит</th><th class="num">Цена</th><th>Статус</th><th></th></tr></thead>
      <tbody>${d.tasks.map(t => { const pc = pct(t.done24, t.per_day);
        return `<tr><td>${esc(t.name)}<div class="muted">${esc(KIND[t.kind] || t.kind)}</div></td><td>${esc(t.keyword || "—")}</td>
        <td><div class="plan">${meter(pc, pc >= 80)}<span>${fmt(t.done24)} / ${fmt(t.per_day)}</span></div></td>
        <td class="num">${fmt(t.done_total)} / ${fmt(t.limit_total)}</td><td class="num">${t.price} ₽</td>
        <td>${esc(t.service_id ? "услуга: " + t.order_status : (TSTATUS[t.status] || t.status))}</td>
        <td class="num"><button class="btn small" data-t="${t.id}" data-act="${t.status === "paused" ? "play" : "pause"}">${t.status === "paused" ? "Запустить" : "Пауза"}</button></td></tr>`; }).join("")
        || '<tr><td colspan="7" class="empty">По товару ещё нет ТЗ</td></tr>'}</tbody>
    </table></div></div>`;
  const palette = [css("--accent"), css("--ok"), css("--rework"), css("--agent")];
  api(`/api/projects/${pid}`).then(full => {
    const allDates = [...new Set([...full.positions.map(x => x.date), ...full.days.map(x => x.d)])].sort();
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = css("--muted");
    chart("chPromo", { data: { labels: allDates.map(x => x.slice(8, 10) + "." + x.slice(5, 7)), datasets: [
      ...d.keywords.map((k, i) => ({ type: "line", label: "«" + k.keyword + "»", borderColor: palette[i % 4], backgroundColor: palette[i % 4],
        tension: .3, yAxisID: "y", spanGaps: true, pointRadius: 0, borderWidth: 2,
        data: allDates.map(dt => full.positions.find(x => x.keyword === k.keyword && x.date === dt)?.position ?? null) })),
      { type: "bar", label: "выполнений", yAxisID: "y1", backgroundColor: css("--chip-strong"),
        data: allDates.map(dt => full.days.find(x => x.d === dt)?.n ?? 0) }] },
      options: { interaction: { mode: "index", intersect: false }, plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } }, scales: {
        y: { reverse: true, title: { display: true, text: "место" }, grid: { color: css("--line") } },
        y1: { position: "right", grid: { display: false }, title: { display: true, text: "выполнений" } },
        x: { grid: { display: false } } } } });
  });
  $$("#promoDetail [data-act]").forEach(b => b.onclick = async () => {
    try { await api(`/api/tasks/${b.dataset.t}/${b.dataset.act}`, { method: "POST" }); toast(b.dataset.act === "pause" ? "ТЗ на паузе" : "ТЗ запущено"); loadPromo(); }
    catch (e) { toast(e.message); } });
}
$("#backToList").onclick = () => { openProject = null; loadPromo(); };

// --- формы ---
$$("dialog [data-close]").forEach(b => b.onclick = () => b.closest("dialog").close());
$("#newProject").onclick = () => $("#projectDlg").showModal();

// ключ площадки: вводится здесь, а не в файле .env
async function showKey() {
  let k;
  try { k = await api("/api/key"); } catch (e) { return toast(e.message); }
  $("#keyState").textContent = (k.has_key ? `Ключ задан${k.tail ? " (…" + k.tail + ")" : ""}${k.source === "env" ? ", в файле .env" : ""}. ${k.mode === "demo" ? "Панель запущена на демо-данных." : "Панель работает с кабинетом UNU."}`
    : "Ключа нет: панель показывает демо-данные.") + (k.read_only ? " Режим «только чтение»: панель ничего не отправляет в UNU." : "");
  $("#keyDel").classList.toggle("hidden", !k.has_key || k.source === "env");
  $("#keyDlg").showModal();
}
$("#keyBtn").onclick = showKey;
$("#keyDel").onclick = async () => {
  if (!confirm("Убрать ключ? Панель вернётся к демо-данным.")) return;
  try { const r = await api("/api/key", { method: "DELETE" }); $("#keyDlg").close();
    toast(r.restart ? "Ключ убран. Перезапустите панель" : "Ключ убран"); if (!r.restart) location.reload(); } catch (e) { toast(e.message); }
};
$("#keyForm").onsubmit = async e => {
  e.preventDefault();
  const key = e.target.key.value.trim(); if (!key) return;
  try {
    const r = await api("/api/key", { method: "POST", body: { key } });
    e.target.reset(); $("#keyDlg").close();
    toast(r.restart ? `Ключ принят, баланс ${fmt(Math.round(r.balance))} ₽. Перезапустите панель: python run.py`
      : `Ключ принят, баланс ${fmt(Math.round(r.balance))} ₽. Идёт первая синхронизация`);
    if (!r.restart) setTimeout(() => location.reload(), 1500);
  } catch (err) { toast(err.message); }
};
$("#newTz").onclick = () => { fillTzProjects(); boostFields(); tzSum(); $("#tzDlg").showModal(); };
$("#projectForm").onsubmit = async e => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  f.keywords = f.keywords.split(",").map(s => s.trim()).filter(Boolean);
  try { await api("/api/projects", { method: "POST", body: f });
    e.target.reset(); $("#projectDlg").close(); toast("Товар добавлен. Теперь дайте по нему ТЗ"); loadPromo(); }
  catch (err) { toast(err.message); }
};
function fillTzProjects() {
  const sel = $("#tzProject"), cur = sel.value || openProject;
  sel.innerHTML = promoData.map(d => `<option value="${d.project.id}">${esc(d.project.name)}</option>`).join("");
  if (cur) sel.value = cur;
  fillKeywords();
}
function fillKeywords() {
  const d = promoData.find(x => x.project.id === +$("#tzProject").value);
  const kws = d?.keywords || [];
  $("#tzKeyword").innerHTML = kws.map(k => `<option>${esc(k.keyword)}</option>`).join("") +
    (kws.length > 1 ? `<option value="*">Все запросы товара — отдельное ТЗ на каждый (${kws.length})</option>` : "");
}
$("#tzProject").onchange = fillKeywords;
let services = [];
$("#tzMode").onchange = async () => {
  const svc = $("#tzMode").value === "service";
  $$("#tzForm .svc").forEach(el => el.classList.toggle("hidden", !svc));
  $$("#tzForm .own").forEach(el => el.classList.toggle("hidden", svc));
  if (svc && !services.length) {
    try { services = await api("/api/services"); } catch (e) { toast(e.message); }
    $("#tzService").innerHTML = services.map(s => `<option value="${s.id}">${esc(s.name)} — ${s.price} ₽</option>`).join("");
  }
  boostFields(); tzSum();
};
// фильтр цены нужен только ТЗ «по фото и фильтру»; контрольный ответ там по умолчанию — 3 последние цифры артикула
function boostFields() {
  const on = $("#tzKind").value === "card_boost" && $("#tzMode").value !== "service";
  $$("#tzForm .boost").forEach(el => el.classList.toggle("hidden", !on));
  $("#tzForm [name=control_expected]").placeholder = on ? "пусто — первое слово описания товара (возьмём с WB сами)" : "цена 1290 / цифры 291 / слово";
}
$("#tzKind").onchange = () => { boostFields(); tzSum(); };
function tzSum() {
  const f = Object.fromEntries(new FormData($("#tzForm")));
  const many = f.mode !== "service" && f.keyword === "*" ? tzKeywords().length : 1;
  const n = (+f.per_day || 0) * (+f.days || 0) * many;
  // своё задание: UNU берёт +1 ₽ за обязательный скрин (на тестовом ключе 5 ₽ превратились в 6 ₽)
  const price = f.mode === "service" ? (services.find(s => s.id === +f.service)?.price || 0) : (+f.price || 0) + 1;
  $("#tzSum").innerHTML = `<b>${fmt(n)}</b> выполнений, <b>${fmt(Math.round(n * price))} ₽</b>` + (f.mode === "service" ? "" : " с 1 ₽ за скрин");
}
$("#tzForm").oninput = tzSum;
$("#tzForm").onsubmit = async e => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  if (f.mode === "service") {
    try { const r = await api(`/api/projects/${f.project}/orders`, { method: "POST", body: { service: +f.service, per_day: +f.per_day, days: +f.days } });
      $("#tzDlg").close(); toast("Услуга заказана: " + r.ext_id); openProject = +f.project; loadPromo(); }
    catch (err) { toast(err.message); }
    return;
  }
  const boost = f.kind === "card_boost";
  if (!f.control_expected && !boost) return toast("Укажите контрольный ответ — по нему отчёты проверяются без картинок");
  if (boost && (!f.price_from || !f.price_to)) return toast("Укажите фильтр цены от и до — по нему исполнитель сразу находит товар");
  const keywords = f.keyword === "*" ? tzKeywords() : [f.keyword];
  const sent = [];
  for (const keyword of keywords) {
    const body = { kind: f.kind, keyword, per_day: +f.per_day, days: +f.days, price: +f.price,
      control_expected: f.control_expected.split(",").map(s => s.trim()).filter(Boolean),
      min_seconds: f.kind === "search_cart" || boost ? 300 : 90,
      ...(boost ? { price_from: +f.price_from, price_to: +f.price_to } : {}) };
    try { sent.push((await api(`/api/projects/${f.project}/tasks`, { method: "POST", body })).ext_id); }
    catch (err) { toast(`«${keyword}»: ${err.message}` + (sent.length ? `. Уже отправлено ТЗ: ${sent.length}` : "")); if (sent.length) loadPromo(); return; }
  }
  $("#tzDlg").close(); openProject = +f.project; loadPromo();
  toast(sent.length > 1 ? `Отправлено ${sent.length} ТЗ в UNU` : "ТЗ отправлено в UNU, №" + sent[0]);
};
function tzKeywords() { return (promoData.find(x => x.project.id === +$("#tzProject").value)?.keywords || []).map(k => k.keyword); }

// ===================== ПРОВЕРКА =====================
let queue = [], qi = 0, vf = "manual", qtotal = 0;
async function loadCheck() {
  const s = await loadHeader();
  $("#mode").value = s.mode;
  loadLearnCount();
  if (sub === "queue") return loadQueue();
  if (sub === "learn") return loadLearn();
  renderHow(s);
}

function renderHow(s) {
  const f = s.funnel, total = f.total || 0, human = f.human || 0, agentN = f.to_agent || 0, rules = f.rules || 0;
  const parts = [[rules, "--ok", "решили правила", "контрольный ответ, копии скринов, IP, ссылки — без просмотра картинок"],
    [agentN, "--agent", "проверил агент", `новички, 3% выборка и спорные, где решает картинка; ${Math.ceil(agentN / 12)} листов по 12 скринов`],
    [human, "--warn", "решает человек", `${total ? (human * 100 / total).toFixed(1) : 0}% потока`]];
  $("#funnel").innerHTML = `<div class="muted">Пришло ${fmt(total)} ${plural(total, "отчёт", "отчёта", "отчётов")}</div>
    <div class="flow">${parts.map(([n, c]) => `<i style="width:${total ? n * 100 / total : 0}%;background:${css(c)}"></i>`).join("")}</div>
    <div class="flow-legend">${parts.map(([n, c, t, sub]) => `<div style="--c:${css(c)}"><b>${fmt(n)}</b><span>${t}. ${sub}</span></div>`).join("")}</div>
    <details class="how"><summary>Почему агент смотрит не всё</summary>
      <p>Агенту уходит не каждая картинка, а листы: 12 скриншотов на одном изображении и описание каждого. Это в ~6 раз дешевле.
      Одинаковые скрины уже отсеяны правилами по отпечаткам. Скорость, ферму на IP и копию скрина картинка не опровергает — такие отчёты смотрит человек,
      пока разбор не покажет, что агент с ними справляется (вкладка «Обучение»).</p></details>`;
  const b = s.agent_batches, spot = s.spot;
  $("#agentBox").innerHTML = `
    <div class="bar-tools"><button class="btn primary" id="mkBatch">${s.agent?.online ? "Отдать на проверку ИИ" : "Собрать пачку"}</button><button class="btn" id="impBatch">Забрать ответы</button>
      <span class="pill ${s.agent?.online ? "ok" : "warn"}">${s.agent?.online ? "ИИ на связи: Claude на компьютере оператора" : "ИИ не на связи"}</span></div>
    <p class="muted">${s.agent?.online ? "Пачку заберёт Claude на компьютере оператора, проверит скрины и вернёт вердикты сюда — обычно за 1–2 минуты. Обновите страницу, чтобы увидеть ответ."
      : "Проверку скринов делает Claude Code на компьютере оператора. Сейчас он не опрашивает панель: запустите <code>python tools/remote_agent.py watch &lt;адрес панели&gt;</code> или проверьте пачку в Claude Code вручную."}</p>
    ${spot.checked ? `<p class="lead">Выборочный контроль: среди ${fmt(spot.checked)} отчётов, принятых правилами, агент нашёл брак в ${spot.bad} (${(spot.bad * 100 / spot.checked).toFixed(1)}%).</p>` : ""}
    ${b.length ? `<table><thead><tr><th>Пачка</th><th class="num">Отчётов</th><th class="num">Листов</th><th>Состояние</th></tr></thead><tbody>${b.map(x =>
      `<tr><td><a href="${sheetUrl(x.name)}" target="_blank" rel="noopener">${esc(x.name)}</a></td><td class="num">${x.reports}</td><td class="num">${x.sheets}</td><td>${esc(x.state)}</td></tr>`).join("")}</tbody></table>
      <p class="muted">В Claude Code: «проверь пачку ${esc(b[0].name)}». Ответ агента панель заберёт сама.</p>` : '<p class="muted">Пачек ещё не было.</p>'}`;
  $("#mkBatch").onclick = async () => { try { const r = await api("/api/agent/batch?limit=" + (s.hosted ? 24 : 240), { method: "POST" });
    toast(`Пачка ${r.name}: ${r.reports} отчётов на ${r.sheets} листах`); loadCheck(); } catch (e) { toast(e.message); } };
  $("#impBatch").onclick = async () => { try { const r = await api("/api/agent/import", { method: "POST" });
    toast(r.batches ? `Забрано: ок ${r.ok}, брак ${r.bad}, не уверен ${r.unsure}` : "Новых ответов агента нет"); loadCheck(); } catch (e) { toast(e.message); } };
  const maxR = Math.max(1, ...s.reasons.map(r => r[1]));
  $("#reasons").innerHTML = s.reasons.length ? s.reasons.slice(0, 8).map(([n, c]) =>
    `<div class="rsn"><span>${esc(n)}</span>${meter(100 * c / maxR, false)}<span class="num">${fmt(c)}</span></div>`).join("")
    : '<p class="muted">Пока нечего показать</p>';
}

$$("#vfilter button").forEach(b => b.onclick = () => { vf = b.dataset.v; $$("#vfilter button").forEach(x => x.classList.toggle("on", x === b)); qi = 0; loadQueue(); });
$("#pfilter").onchange = () => { qi = 0; loadQueue(); };
$("#qorder").onchange = () => { qi = 0; loadQueue(); };
const qurl = off => `/api/reports?verdict=${vf}&limit=200&offset=${off}&order=${$("#qorder").value}${$("#pfilter").value ? "&project_id=" + $("#pfilter").value : ""}`;
const ago = iso => { const h = Math.max(0, Math.round((Date.now() - new Date(iso)) / 36e5)); return h < 1 ? "только что" : `${h} ч назад`; };
$("#mode").onchange = async e => { try { await api("/api/settings", { method: "POST", body: { decision_mode: e.target.value } }); toast("Режим сохранён"); } catch (err) { toast(err.message); } };
$("#applyAll").onclick = async () => {
  if (!confirm("Отправить в UNU все уверенные решения панели: принять, вернуть, отклонить? Отчёты, которые ждут человека, не трогаем.")) return;
  try { const r = await api("/api/reports/apply-confident", { method: "POST" }); toast(`Отправлено решений: ${r.applied}`); loadCheck(); }
  catch (e) { toast(e.message); }
};
async function loadQueue() {
  const sel = $("#pfilter"), cur = sel.value;
  sel.innerHTML = '<option value="">Все товары</option>' + (await api("/api/projects")).map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  sel.value = cur;
  const r = await api(qurl(0));
  queue = r.items; qtotal = r.total; qi = Math.min(qi, Math.max(0, queue.length - 1));
  renderList(); renderCard();
}
function renderList() {
  $("#rlist").innerHTML = `<div class="total">${fmt(qtotal)} ${plural(qtotal, "отчёт", "отчёта", "отчётов")}${qtotal > queue.length ? `, загружено ${queue.length}` : ""}</div>` +
    queue.map((r, i) => `<div class="ritem ${i === qi ? "on" : ""}" data-i="${i}"><span class="s v-${r.verdict}">${r.score}</span>
      <span><span class="t">${esc(r.reasons.filter(f => f.weight > 0).map(f => f.name || f.text).join(", ") || "замечаний нет")}</span><span class="w">${esc(r.project_name)}, ${ago(r.submitted_at)}</span></span></div>`).join("") +
    (qtotal > queue.length ? '<div class="total"><button class="link" id="qmore">Показать ещё</button></div>' : "");
  $$(".ritem").forEach(el => el.onclick = () => { qi = +el.dataset.i; renderCard(); markList(); });
  $("#qmore")?.addEventListener("click", async () => {
    try { const r = await api(qurl(queue.length)); const ids = new Set(queue.map(x => x.id));
      queue.push(...r.items.filter(x => !ids.has(x.id))); qtotal = r.items.length ? r.total : queue.length; renderList(); } catch (e) { toast(e.message); } });
}
function markList() { $$(".ritem").forEach(el => el.classList.toggle("on", +el.dataset.i === qi)); $(".ritem.on")?.scrollIntoView({ block: "nearest" }); }
function renderCard() {
  const r = queue[qi];
  if (!r) { $("#rcard").innerHTML = `<div class="empty" style="grid-column:1/-1">${vf === "manual" ? "Очередь разобрана. Новые отчёты придут со следующей синхронизацией." : "Здесь пусто."}</div>`; return; }
  const ws = r.worker_stats, files = r.files;
  const cls = f => f.code.startsWith("agent_") ? "agent" : f.weight < 0 ? "good" : f.kind;
  const ref = r.reasons.find(f => f.ref?.files?.length)?.ref;
  const shot = (f, alt) => `<figure><img src="${fileUrl(f)}" alt="${alt}"><figcaption>${alt}</figcaption></figure>`;
  $("#rcard").innerHTML = `
    <div class="shots ${files.length > 1 || ref ? "many" : ""}">${files.length ? files.map((f, i) => shot(f, files.length > 1 ? `Скрин ${i + 1} из ${files.length}` : "Скрин исполнителя")).join("") : '<div class="empty">Скриншота нет</div>'}
      ${ref ? shot(ref.files[0], `Похожий: отчёт #${+ref.id}, исполнитель ${esc(ref.worker)}`) : ""}</div>
    <div class="info">
      <div class="who"><div><b>${esc(r.project_name)}</b><div class="muted">${esc(r.task_name)}</div></div>
        <div class="verdict v-${r.verdict}"><b>${r.score}</b>${VNAME[r.verdict]}</div></div>
      <dl class="facts">
        <dt>Нужно в отчёте</dt><dd>${esc(r.need_for_report)}</dd>
        <dt>Правильный ответ</dt><dd><b>${esc(r.control_expected.join(", ") || "не задан")}</b>, артикул ${esc(r.sku)}</dd>
        <dt>Сдан</dt><dd>${localTime(r.submitted_at)}, отчёт №${esc(r.ext_id)}, ${fmt(r.price)} ₽</dd>
        <dt>Исполнитель</dt><dd>${esc(r.worker_id)}, IP ${esc(r.ip)}. Отчётов ${fmt(ws.n)}, в браке ${fmt(ws.bad)}</dd>
      </dl>
      <div class="rtext">${esc(r.text) || '<span class="muted">Текста нет</span>'}</div>
      <ul class="reasons">${r.reasons.map(f => `<li class="${cls(f)}">${esc(f.text)}</li>`).join("") || '<li class="good">Замечаний нет</li>'}</ul>
      <div class="decide">
        <button class="btn ok" data-a="approve">Принять <kbd>A</kbd></button>
        <button class="btn rework" data-a="rework">Вернуть <kbd>R</kbd></button>
        <button class="btn bad" data-a="reject">Отклонить <kbd>X</kbd></button>
        <span class="grow"></span>
        <button class="btn" id="bl">В чёрный список</button>
      </div>
      <div class="muted" style="font-size:12px">Следующий и предыдущий: <kbd>J</kbd> <kbd>K</kbd>. Каждое ваше решение учит проверку — вкладка «Обучение».</div>
    </div>`;
  $$("#rcard [data-a]").forEach(b => b.onclick = () => decideCur(b.dataset.a));
  $$("#rcard img").forEach(img => img.onclick = () => { const z = document.createElement("div"); z.id = "zoom"; z.innerHTML = `<img src="${img.src}" alt="">`; z.onclick = () => z.remove(); document.body.append(z); });
  $("#bl").onclick = async () => { if (!confirm(`Добавить ${r.worker_id} в чёрный список UNU? Его непроверенные отчёты будут отклонены.`)) return;
    try { await api(`/api/workers/${encodeURIComponent(r.worker_id)}/blacklist`, { method: "POST" }); toast(`${r.worker_id} в чёрном списке`); } catch (e) { toast(e.message); } };
}
let deciding = false;
async function decideCur(action) {
  const r = queue[qi]; if (!r || deciding) return;      // повторное нажатие не отправит второе решение
  let comment = null;
  if (action !== "approve") { comment = prompt("Комментарий исполнителю:", defaultComment(r)); if (comment === null) return; }
  deciding = true; $$("#rcard [data-a]").forEach(b => b.disabled = true);
  try {
    await api(`/api/reports/${r.id}/decide`, { method: "POST", body: { action, comment } });
    toast({ approve: "Принято", rework: "Возвращено на доработку", reject: "Отклонено" }[action]);
    const i = queue.findIndex(x => x.id === r.id);       // удаляем именно этот отчёт, а не текущую позицию
    if (i >= 0) { queue.splice(i, 1); qtotal--; if (i < qi || qi >= queue.length) qi = Math.max(0, Math.min(qi - (i < qi), queue.length - 1)); }
    renderList(); renderCard();
    if (!queue.length && qtotal > 0) loadQueue();
  } catch (e) { toast(e.message); $$("#rcard [data-a]").forEach(b => b.disabled = false); }
  finally { deciding = false; }
}
function defaultComment(r) {
  const c = r.reasons.map(f => f.code);
  if (c.includes("no_screen")) return "Приложите скриншот, на котором видно выполненное действие.";
  if (c.includes("few_stages")) return "Приложите скрины всех этапов: поиск с фильтром, карточка, корзина, избранное.";
  if (c.includes("link_wrong")) return "В отчёте ссылка на другой товар. Найдите товар с фото из задания.";
  if (c.includes("control_wrong") || c.includes("control_missing")) return "Ответ на контрольный вопрос неверный или отсутствует. Откройте товар и проверьте.";
  if (c.includes("dup_exact") || c.includes("dup_near")) return "Скриншот совпадает с чужим отчётом.";
  if (c.includes("agent_bad")) return "На скриншоте не видно выполненного действия. Пришлите скриншот карточки товара с результатом.";
  return "Отчёт не соответствует заданию.";
}
document.addEventListener("keydown", e => {
  if (tab !== "check" || sub !== "queue" || e.ctrlKey || e.metaKey || e.altKey || document.querySelector("dialog[open]") ||
      ["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
  const k = e.key.toLowerCase();
  if (k === "a" || k === "ф") decideCur("approve");
  else if (k === "r" || k === "к") decideCur("rework");
  else if (k === "x" || k === "ч") decideCur("reject");
  else if (k === "j" || k === "о") { qi = Math.min(queue.length - 1, qi + 1); renderCard(); markList(); }
  else if (k === "k" || k === "л") { qi = Math.max(0, qi - 1); renderCard(); markList(); }
});

// --- обучение ---
async function loadLearnCount() {
  try { const l = await api("/api/learning"); $("#lcount").textContent = l.suggestions.length || ""; return l; } catch { return null; }
}
async function loadLearn() {
  const l = await api("/api/learning");
  const q = l.queue, a = l.agent;
  const CH = c => c.agent ? '<span class="pill">отдано агенту</span>' : c.hard ? `<span class="pill bad">${c.hard === "reject" ? "отклонять" : "возвращать"} сразу</span>`
    : "weight" in c ? `<span class="pill warn">вес ${c.weight}</span>` : "";
  $("#learnBox").innerHTML = `
    <div class="learn-top">
      <div class="fig"><b>${fmt(l.reviews)}</b><span>решений человека разобрано</span></div>
      <div class="fig"><b>${a.n ? `${a.agree} из ${a.n}` : "—"}</b><span>агент в тени совпал с человеком</span></div>
      <div class="fig"><b>${fmt(q.now)} → ${fmt(q.human)}</b><span>отчётов человеку, если принять все предложения${q.agent ? ` (${fmt(q.agent)} уйдут агенту)` : ""}</span></div>
    </div>
    <div class="panel" style="margin-bottom:16px">
      <div style="padding:14px 18px 0"><h2>Предложения</h2>
        <p class="lead">Выводы делаются по старым решениям, а проверяются на свежих. Предложение, из-за которого хоть один брак ушёл бы в оплату, не показывается. Применяете вы — сама проверка правила не меняет: исполнители подстраиваются, и самообучение можно было бы приучить пропускать брак.</p></div>
      ${l.suggestions.length ? l.suggestions.map((g, i) => `<div class="sugg"><p>${esc(g.text)}</p>
        <span class="proof">Проверено на ${fmt(g.of)} свежих решениях, на которых правило не подбиралось: ещё ${fmt(g.gained)} ${plural(g.gained, "отчёт", "отчёта", "отчётов")} без человека, брака в оплату — ${g.new_pay}, лишних отказов честным — ${g.new_reject}.</span>
        <button class="btn primary" data-apply="${i}">Применить</button></div>`).join("")
        : `<p class="empty">Пока нечего предложить. Выводы появляются после ${l.min_n} решений по одной причине.</p>`}
    </div>
    ${l.overrides.length ? `<div class="panel" style="margin-bottom:16px"><div style="padding:14px 18px 0"><h2>Применено</h2></div><table><tbody>${l.overrides.map(o =>
      `<tr><td>${esc(o.name)}</td><td>${CH(o)}</td><td class="num"><button class="btn small" data-rollback="${esc(o.code)}">Откатить</button></td></tr>`).join("")}</tbody></table></div>` : ""}
    <div class="panel"><div class="tablewrap"><table>
      <thead><tr><th>Причина сомнения</th><th class="num" title="Не больше 2 решений по одному исполнителю — чтобы ферма не «научила» проверку">Разобрано</th><th class="num">Человек принял</th><th class="num">Забраковал</th><th class="num">Агент совпал</th><th>Сейчас</th></tr></thead>
      <tbody>${l.rules.map(x => `<tr><td>${esc(x.name)}</td><td class="num">${fmt(x.n)}</td>
        <td class="num">${fmt(x.approve)} <span class="muted">(${pct(x.approve, x.n)}%)</span></td><td class="num">${fmt(x.rework + x.reject)}</td>
        <td class="num">${x.agent_n ? `${x.agent_agree} из ${x.agent_n}` : "—"}</td><td>${x.override ? CH(x.override) : '<span class="muted">как было</span>'}</td></tr>`).join("")
        || '<tr><td colspan="6" class="empty">Решений человека ещё не было</td></tr>'}</tbody></table></div></div>
    <details class="how"><summary>Как работает обучение</summary>
      <p>Каждое решение в очереди сохраняется вместе с тем, что сказали правила и агент. Часть спорных отчётов агент смотрит «в тени»: решаете всё равно вы, а его ответ только сравнивается.
      Если по причине человек почти всегда принимает — у неё лишний вес; почти всегда бракует — её можно решать без человека; агент совпадает с человеком — её можно отдать агенту.
      Случаи, где агент ошибся, попадают в его примеры: он читает их перед каждой пачкой. Копии скринов агенту не отдаются никогда — оригинал ему не виден.</p></details>`;
  $$("[data-apply]").forEach(b => b.onclick = async () => {
    const g = l.suggestions[+b.dataset.apply];
    try { await api("/api/learning/apply", { method: "POST", body: { code: g.code, change: g.change } }); toast("Применено. Очередь перепроверится при синхронизации"); loadCheck(); }
    catch (e) { toast(e.message); } });
  $$("[data-rollback]").forEach(b => b.onclick = async () => {
    try { await api("/api/learning/rollback", { method: "POST", body: { code: b.dataset.rollback } }); toast("Откатили, правило как раньше"); loadCheck(); }
    catch (e) { toast(e.message); } });
}

// ===================== ИСПОЛНИТЕЛИ =====================
let wsort = "risk";
$$("#wsort button").forEach(b => b.onclick = () => { wsort = b.dataset.s; $$("#wsort button").forEach(x => x.classList.toggle("on", x === b)); loadWorkers(); });
async function loadWorkers() {
  const s = await loadHeader();
  $("#alerts").innerHTML = s.alerts.map(a => `<div class="alert ${a.kind === "farm" ? "" : "grow-a"}">${esc(a.text)}</div>`).join("");
  const r = await api("/api/workers?sort=" + wsort);
  const g = $("#geo"), cs = r.countries || [];
  g.classList.toggle("hidden", !cs.length);
  if (cs.length) {
    const tot = r.geo_known || 1, home = cs.filter(c => c.home).reduce((a, c) => a + c.n, 0);
    const top = cs.slice(0, 5), rest = cs.slice(5).reduce((a, c) => a + c.n, 0);
    const row = (nm, n, ok) => `<div class="georow"><span>${esc(nm)}</span><span class="meter"><i style="width:${n * 100 / tot}%;background:${css(ok ? "--ok" : "--warn")}"></i></span><b>${Math.round(n * 100 / tot)}%</b><span class="muted">${fmt(n)}</span></div>`;
    g.innerHTML = `<h2>Откуда приходят отчёты <span class="muted" style="font-weight:400">по IP исполнителя; из России, Беларуси и Казахстана — ${Math.round(home * 100 / tot)}%</span></h2>
      <div class="geo">${top.map(c => row(c.name, c.n, c.home)).join("")}${rest ? row("Остальные страны", rest, false) : ""}</div>`;
  }
  const bar = parts => { const sum = parts.reduce((x, [n]) => x + (n || 0), 0) || 1;
    return `<span class="meter" style="width:120px">${parts.map(([n, c]) => `<i style="width:${(n || 0) * 100 / sum}%;background:${css(c)}"></i>`).join("")}</span>`; };
  $("#wtable").innerHTML = `<div class="tablewrap"><table>
    <thead><tr><th>Исполнитель</th><th>Страна</th><th class="num">Риск</th><th class="num">Заданий</th><th class="num">За сутки</th><th class="num">Всего</th><th>Принято, брак, вручную</th><th class="num">Чужих скринов</th><th class="num">Аккаунтов на IP</th><th></th></tr></thead>
    <tbody>${r.items.map(w => `<tr><td>${esc(w.worker_id)}${w.blacklisted ? ' <span class="pill bad">в ЧС</span>' : ""}</td>
      <td>${w.country ? (w.cc && !["RU", "BY", "KZ"].includes(w.cc) ? `<span class="pill warn">${esc(w.country)}</span>` : esc(w.country)) : ""}</td>
      <td class="num"><b class="${w.risk >= 60 ? "v-reject" : w.risk >= 30 ? "v-manual" : "v-approve"}">${w.risk}</b></td>
      <td class="num">${fmt(w.tasks)}</td><td class="num">${fmt(w.n24)}</td><td class="num">${fmt(w.n)}</td>
      <td><div class="plan">${bar([[w.ok, "--ok"], [w.bad, "--bad"], [w.manual, "--warn"]])}<span class="muted">${fmt(w.ok)}, ${fmt(w.bad)}, ${fmt(w.manual)}</span></div></td>
      <td class="num">${w.stolen || ""}</td><td class="num">${w.ip_mates || ""}</td>
      <td class="num">${w.blacklisted ? "" : `<button class="btn small" data-w="${esc(w.worker_id)}">В ЧС</button>`}</td></tr>`).join("")}</tbody></table></div>
    <p class="muted" style="padding:0 14px 12px">Исполнителей: ${fmt(r.total)}. Риск складывается из доли брака, чужих скриншотов, соседей по IP и объёма за сутки.</p>`;
  $$("#wtable [data-w]").forEach(b => b.onclick = async () => {
    if (!confirm(`Добавить ${b.dataset.w} в чёрный список UNU?`)) return;
    try { await api(`/api/workers/${encodeURIComponent(b.dataset.w)}/blacklist`, { method: "POST" }); toast(`${b.dataset.w} в чёрном списке`); loadWorkers(); }
    catch (e) { toast(e.message); } });
}

// ===================== ЖУРНАЛ =====================
async function loadLog() {
  loadHeader();
  const a = await api("/api/actions");
  const NAMES = { approve: "Принят отчёт", rework: "Возвращён на доработку", reject: "Отклонён отчёт", blacklist: "Чёрный список", task_create: "ТЗ отправлено",
    project_create: "Добавлен товар", task_attach: "Задание привязано к товару", task_move: "Задание перенесено в папку товара", auto_attach: "Задания кабинета подхвачены",
    product_found: "Товар определён по скринам", product_set: "Артикул указан вручную", agent_remote: "Вердикты ИИ с компьютера", key_set: "Ключ сохранён", key_unset: "Ключ убран", settings: "Настройки", task_pause: "Пауза ТЗ", task_play: "Запуск ТЗ", agent_batch: "Пачка агенту",
    agent_import: "Ответ агента", rule_change: "Правило проверки изменено", rule_rollback: "Правило откатили", positions_job: "Задание на позиции" };
  $("#actions").innerHTML = a.length ? `<div class="tablewrap"><table><thead><tr><th>Когда</th><th>Что</th><th>Объект</th><th>Подробности</th></tr></thead><tbody>` +
    a.map(x => `<tr><td>${localTime(x.ts)}</td><td>${x.ok ? "" : '<span class="pill bad">ошибка</span> '}${esc(NAMES[x.kind] || x.kind)}</td><td>${esc(x.ref)}</td><td class="muted">${esc(x.detail).slice(0, 160)}</td></tr>`).join("") + "</tbody></table></div>"
    : '<div class="empty">Панель ещё ничего не отправляла</div>';
}

$("#syncnow").onclick = async () => { $("#syncinfo").textContent = "Синхронизация…"; try { await api("/api/sync", { method: "POST" }); } catch (e) { toast(e.message); } show(tab); };
loadPromo();
setInterval(() => { if (tab === "promo" && !openProject && !document.querySelector("dialog[open]")) loadPromo(); }, 60000);
