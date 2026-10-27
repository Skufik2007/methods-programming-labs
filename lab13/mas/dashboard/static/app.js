// Автообновление таблиц: раз в 2,5 с запрашивает JSON и перерисовывает строки.
// Данные выводятся только через textContent/createElement — innerHTML не используется (CSP + защита от XSS).
// Без JS страницы работают как обычные серверные (обновление вручную).
(function () {
  "use strict";

  var INTERVAL_MS = 2500;
  var statusEl = document.getElementById("live-status");

  function setStatus(text, bad) {
    if (!statusEl) { return; }
    statusEl.textContent = text;
    statusEl.className = "live" + (bad ? " bad" : "");
  }

  function pathGet(obj, path) {
    var cur = obj;
    var parts = path.split(".");
    for (var i = 0; i < parts.length; i++) {
      if (cur === null || cur === undefined) { return undefined; }
      cur = cur[parts[i]];
    }
    return cur;
  }

  function display(value) {
    if (value === null || value === undefined || value === "") { return "—"; }
    return String(value);
  }

  function columnsOf(table) {
    var heads = table.querySelectorAll("thead th");
    var cols = [];
    for (var i = 0; i < heads.length; i++) {
      var d = heads[i].dataset;
      cols.push({
        field: d.field,
        badge: d.badge || "",
        linkPrefix: d.linkPrefix || "",
        linkField: d.linkField || "",
        pre: d.pre === "1",
        num: d.num === "1"
      });
    }
    return cols;
  }

  function buildCell(col, row) {
    var td = document.createElement("td");
    if (col.num) { td.className = "num"; }
    var value = row[col.field];
    if (col.badge) {
      var span = document.createElement("span");
      span.className = "badge " + display(row[col.badge]).replace(/[^a-z]/g, "");
      span.textContent = display(value);
      td.appendChild(span);
    } else if (col.linkPrefix) {
      var a = document.createElement("a");
      a.setAttribute("href", col.linkPrefix + encodeURIComponent(display(row[col.linkField])));
      a.textContent = display(value);
      td.appendChild(a);
    } else if (col.pre && value) {
      var pre = document.createElement("pre");
      pre.className = "out";
      pre.textContent = String(value);
      td.appendChild(pre);
    } else {
      td.textContent = display(value);
    }
    return td;
  }

  function renderTable(table, rows) {
    var cols = columnsOf(table);
    var body = table.querySelector("tbody");
    var fresh = document.createDocumentFragment();
    if (!rows.length) {
      var tr = document.createElement("tr");
      tr.className = "empty";
      var td = document.createElement("td");
      td.colSpan = cols.length;
      td.textContent = table.dataset.empty || "Нет данных";
      tr.appendChild(td);
      fresh.appendChild(tr);
    }
    rows.forEach(function (row) {
      var tr2 = document.createElement("tr");
      cols.forEach(function (col) { tr2.appendChild(buildCell(col, row)); });
      fresh.appendChild(tr2);
    });
    while (body.firstChild) { body.removeChild(body.firstChild); }
    body.appendChild(fresh);
  }

  // data-bind-url: элемент берёт значение только из ответа этого адреса (на странице их несколько).
  function applyBindings(data, url) {
    var nodes = document.querySelectorAll("[data-bind]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.dataset.bindUrl && node.dataset.bindUrl !== url) { continue; }
      var value = pathGet(data, node.dataset.bind);
      if (value !== undefined) {
        node.textContent = (value === "" && node.dataset.bindBlank) ? "" : display(value);
      }
      if (node.dataset.bindClass) {
        var css = pathGet(data, node.dataset.bindClass);
        if (css !== undefined) {
          node.className = (node.dataset.baseClass || "") + " " + String(css).replace(/[^a-z]/g, "");
        }
      }
    }
  }

  function fetchJson(url) {
    return fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  }

  function refresh() {
    if (document.hidden) { return; }
    var tables = document.querySelectorAll("table[data-live-url]");
    var cache = {};
    var jobs = [];
    var urls = [];
    var failed = false;
    tables.forEach(function (table) {
      var url = table.dataset.liveUrl;
      if (!cache[url]) {
        cache[url] = fetchJson(url);
        urls.push(url);
        jobs.push(cache[url]);
      }
      cache[url].then(function (data) {
        var rows = data[table.dataset.liveKey];
        if (Array.isArray(rows)) { renderTable(table, rows); }
      }).catch(function () { /* учтено ниже */ });
    });
    Promise.all(jobs.map(function (j) { return j.catch(function () { failed = true; return null; }); }))
      .then(function (all) {
        all.forEach(function (data, i) { if (data) { applyBindings(data, urls[i]); } });
        if (failed) {
          setStatus("нет связи с панелью", true);
        } else {
          setStatus("обновлено " + new Date().toLocaleTimeString("ru-RU"), false);
        }
      });
  }

  if (document.querySelector("table[data-live-url]")) {
    setStatus("автообновление включено", false);
    window.setInterval(refresh, INTERVAL_MS);
  }
})();
