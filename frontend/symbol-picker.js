// Symbol combobox: shows the user's watchlist on focus, switches to a
// Binance-backed typeahead as the user types, lets the user add/remove
// from the watchlist, and falls back to free-text submission for any
// symbol Binance knows about (or even those it doesn't — the form still
// posts the raw text).
(function () {
  const input = document.getElementById("symbol-input");
  const popover = document.getElementById("symbol-popover");
  const watchRows = document.getElementById("watchlist-rows");
  const searchRows = document.getElementById("search-rows");
  const marketSel = document.querySelector('select[name="market"]');
  if (!input || !popover || !marketSel) return;

  const debounce = (fn, ms) => {
    let t = null;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  };

  const api = async (path, opts = {}) => {
    const r = await fetch(path, {
      headers: { "content-type": "application/json" },
      credentials: "include",
      ...opts,
    });
    if (r.status === 401) { window.location.href = "/login"; throw new Error("unauth"); }
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  };

  let watchlist = [];
  async function loadWatchlist() {
    try {
      const r = await api("/api/watchlist");
      watchlist = r.items || [];
      renderWatchlist();
    } catch (_) { /* unauth handled above */ }
  }

  function renderWatchlist() {
    watchRows.textContent = "";
    if (!watchlist.length) {
      const empty = document.createElement("div");
      empty.className = "symbol-empty";
      empty.textContent = "No saved symbols yet — search and tap “+” to add.";
      watchRows.appendChild(empty);
      return;
    }
    for (const w of watchlist) {
      const row = document.createElement("div");
      row.className = "symbol-row";
      row.dataset.symbol = w.symbol;
      row.dataset.market = w.market;
      const main = document.createElement("button");
      main.type = "button";
      main.className = "symbol-pick";
      main.textContent = `${w.symbol} · ${w.market}`;
      main.addEventListener("click", () => pickSymbol(w.symbol, w.market));
      const del = document.createElement("button");
      del.type = "button";
      del.className = "symbol-del";
      del.textContent = "×";
      del.title = "Remove from watchlist";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await api(`/api/watchlist/${w.id}`, { method: "DELETE" });
          watchlist = watchlist.filter((x) => x.id !== w.id);
          renderWatchlist();
        } catch (_) {}
      });
      row.appendChild(main);
      row.appendChild(del);
      watchRows.appendChild(row);
    }
  }

  function pickSymbol(symbol, market) {
    input.value = symbol;
    if (market && marketSel.value !== market) marketSel.value = market;
    closePopover();
    const form = document.getElementById("setup-form");
    if (form && typeof form.requestSubmit === "function") form.requestSubmit();
    else if (form) form.submit();
  }

  async function inWatchlist(symbol, market) {
    return watchlist.some((w) => w.symbol === symbol && w.market === market);
  }

  function renderSearch(results, q) {
    searchRows.textContent = "";
    if (!results.length) {
      const empty = document.createElement("div");
      empty.className = "symbol-empty";
      empty.textContent = q ? `No matches for “${q}”.` : "Type to search Binance…";
      searchRows.appendChild(empty);
      return;
    }
    for (const s of results) {
      const row = document.createElement("div");
      row.className = "symbol-row";
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "symbol-pick";
      const base = s.baseAsset || "";
      const quote = s.quoteAsset || "";
      pick.textContent = base && quote ? `${s.symbol}  (${base}/${quote})` : s.symbol;
      pick.addEventListener("click", () => pickSymbol(s.symbol, marketSel.value));
      const add = document.createElement("button");
      add.type = "button";
      add.className = "symbol-add";
      add.textContent = "+";
      add.title = "Add to watchlist";
      add.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          const r = await api("/api/watchlist", {
            method: "POST",
            body: JSON.stringify({ symbol: s.symbol, market: marketSel.value }),
          });
          if (!watchlist.some((x) => x.id === r.id)) watchlist.push(r);
          renderWatchlist();
          add.disabled = true;
          add.textContent = "✓";
        } catch (_) {}
      });
      inWatchlist(s.symbol, marketSel.value).then((already) => {
        if (already) { add.disabled = true; add.textContent = "✓"; }
      });
      row.appendChild(pick);
      row.appendChild(add);
      searchRows.appendChild(row);
    }
  }

  const runSearch = debounce(async (q) => {
    if (!q) { renderSearch([], ""); return; }
    try {
      const r = await api(`/api/symbols/search?q=${encodeURIComponent(q)}&market=${encodeURIComponent(marketSel.value)}`);
      renderSearch(r.results || [], q);
    } catch (_) {
      renderSearch([], q);
    }
  }, 150);

  function openPopover() { popover.hidden = false; }
  function closePopover() { popover.hidden = true; }

  input.addEventListener("focus", () => {
    loadWatchlist();
    renderSearch([], "");
    openPopover();
  });
  input.addEventListener("input", () => runSearch(input.value.trim().toUpperCase()));
  marketSel.addEventListener("change", () => {
    if (!popover.hidden) runSearch(input.value.trim().toUpperCase());
  });
  document.addEventListener("click", (e) => {
    if (!popover.hidden && !popover.contains(e.target) && e.target !== input) closePopover();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !popover.hidden) closePopover();
  });
})();
