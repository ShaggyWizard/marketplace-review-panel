// Запускается агентом на открытой странице выдачи (WB или Ozon) в обычном браузере.
// Прокручивает страницу, чтобы подгрузились карточки, и возвращает артикулы по порядку без повторов.
// Ничего не нажимает и не отправляет; защиту сайта не обходит — читает то, что видит покупатель.
// Проверено 23.11.2026: WB — ~180 товаров за заход; Ozon (ozon.kz из Казахстана) — ~32.
(async () => {
  for (let i = 0; i < 12; i++) { window.scrollBy(0, 3500); await new Promise(r => setTimeout(r, 900)); }
  const host = location.hostname;
  let ids = [];
  if (host.includes("wildberries")) {
    ids = [...document.querySelectorAll("[data-nm-id]")].map(c => c.getAttribute("data-nm-id"));
  } else if (host.includes("ozon")) {
    // артикул Ozon — последнее число в адресе карточки: /product/nazvanie-35000-pa-1519683033/
    ids = [...document.querySelectorAll('a[href*="/product/"]')]
      .map(a => (a.getAttribute("href").match(/\/product\/[^?#]*-(\d{6,})\/?(?:[?#]|$)/) || [])[1]).filter(Boolean);
  }
  const seen = new Set(), ordered = [];
  for (const id of ids) if (!seen.has(id)) { seen.add(id); ordered.push(id); }
  const city = (document.querySelector('[data-wba-header-name="DLV_Adress"], [data-widget="addressBookBarWeb"], [class*=geo]') || {}).innerText || "";
  return { host, count: ordered.length, ids: ordered, city: city.replace(/\s+/g, " ").trim().slice(0, 80) };
})();
