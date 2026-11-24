// Демо-копия панели на сайте: ответы панели заранее сохранены в data/*.json (tools/build_static.py).
// Подменяет fetch: GET читает файл, действия ничего никуда не отправляют.
(() => {
  // в копии одна выборка на фильтр: limit, offset и порядок не различаем
  const key = u => u.replace(/[?&](limit|offset|order)=[^&]*/g, "").replace("/api/", "")
    .replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "") + ".json";
  const fnv = s => { let h = 0x811c9dc5; for (const b of new TextEncoder().encode(s)) h = Math.imul(h ^ b, 0x01000193) >>> 0;
    return h.toString(16).padStart(8, "0"); };
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
  const READONLY = window.STATIC_READONLY || "Демо-копия: только просмотр. В рабочей панели это действие уходит в UNU.";
  const realFetch = window.fetch.bind(window);

  window.STATIC_DEMO = {
    file: f => "files/" + fnv(f),
    sheet: b => "sheets/" + encodeURIComponent(b) + ".png",
  };
  window.fetch = async (url, opt = {}) => {
    if (typeof url !== "string" || !url.startsWith("/api/")) return realFetch(url, opt);
    if (/\/api\/key$/.test(url) && (opt.method || "GET") === "GET")       // в копии ключа нет и ввести его нельзя
      return json({ has_key: false, tail: "", source: "", mode: "demo", read_only: true, hosted: false });
    if ((opt.method || "GET") !== "GET") {
      // Решение по отчёту — показываем, как работает очередь, но никуда не отправляем.
      if (/\/api\/reports\/\d+\/decide$/.test(url)) return json({ ok: true });
      return json({ detail: READONLY }, 403);
    }
    if (/[?&]offset=[1-9]/.test(url)) return json({ total: 0, items: [] });   // «показать ещё» в копии пусто
    const r = await realFetch("data/" + key(url));
    return r.ok ? r : json({ detail: "В демо-копии нет этих данных" }, 404);
  };
  window.addEventListener("unhandledrejection", e => {
    const el = document.getElementById("toast");
    if (el) { el.textContent = e.reason?.message || READONLY; el.classList.add("on"); setTimeout(() => el.classList.remove("on"), 3000); }
  });
  document.addEventListener("DOMContentLoaded", () => {
    const b = document.createElement("div");
    b.style.cssText = "background:var(--warn,#f5b400);color:#000;padding:8px 16px;font-size:14px;text-align:center";
    b.textContent = window.STATIC_BANNER || "Демо-копия панели: сгенерированные данные (2 400 отчётов в сутки, 7 дней), к UNU не подключена, изменения не сохраняются.";
    document.body.prepend(b);
  });
})();
