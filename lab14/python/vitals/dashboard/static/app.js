"use strict";
// Клиент дашборда. Все данные попадают в DOM только через textContent / createElement (без innerHTML).
(function () {
  const HISTORY = 150; // точек на метрику (при шаге 2 с ~ 5 минут)
  const ALERT_RU = { tachycardia: "тахикардия", desaturation: "десатурация", fever: "лихорадка",
    hypertension: "гипертония", sensor_fault: "сбой датчика" };
  const THRESH = { heart_rate: [120], spo2: [92], temperature: [38], systolic_bp: [160] };
  const $ = (id) => document.getElementById(id);
  const tbody = $("patients").tBodies[0];
  const rows = new Map();      // patient_id -> {tr, cells}
  const history = new Map();   // "pid|metric" -> [{t, v}]
  let selected = null, delay = 1000, lastSnap = null;

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }
  const fmt = (m, k) => (m && m[k] ? String(m[k].last) : "—");

  function ensureRow(pid) {
    let r = rows.get(pid);
    if (r) return r;
    const tr = el("tr");
    const cells = [el("td", pid), el("td"), el("td"), el("td"), el("td"), el("td")];
    cells.forEach((c) => tr.appendChild(c));
    tr.addEventListener("click", () => select(pid));
    tbody.appendChild(tr);
    r = { tr, cells };
    rows.set(pid, r);
    return r;
  }

  function applySnapshot(s) {
    if (!s || !s.patients) return;
    lastSnap = s;
    const t = s.ts ? Date.parse(s.ts) / 1000 : Date.now() / 1000;
    const seen = new Set();
    for (const p of s.patients) {
      seen.add(p.patient_id);
      const r = ensureRow(p.patient_id), m = p.metrics;
      r.cells[1].textContent = fmt(m, "heart_rate");
      r.cells[2].textContent = fmt(m, "spo2");
      r.cells[3].textContent = fmt(m, "temperature");
      r.cells[4].textContent = fmt(m, "systolic_bp") + " / " + fmt(m, "diastolic_bp");
      // статус: цвет И текст (не полагаемся только на цвет)
      const st = r.cells[5];
      st.textContent = "";
      if (p.alerts.length) st.appendChild(el("span", "ТРЕВОГА: " + p.alerts.map((a) => ALERT_RU[a] || a).join(", "), "badge crit"));
      else st.appendChild(el("span", "норма", "badge ok"));
      for (const name in m) {
        const key = p.patient_id + "|" + name;
        const h = history.get(key) || [];
        h.push({ t, v: m[name].last });
        if (h.length > HISTORY) h.shift();
        history.set(key, h);
      }
    }
    for (const [pid, r] of rows) if (!seen.has(pid)) { r.tr.remove(); rows.delete(pid); }
    $("updated").textContent = "обновлено: " + (s.ts || "");
    draw();
  }

  function addAlert(a, top) {
    const li = el("li", undefined, a.event);
    const when = (a.ts || "").slice(11, 19);
    const what = (a.event === "raised" ? "ТРЕВОГА " : "снята ") + (ALERT_RU[a.alert] || a.alert);
    li.appendChild(el("strong", what));
    li.appendChild(el("span", " — " + a.patient_id + (a.value !== undefined ? " (" + a.value + ")" : "") + ", " + when));
    const feed = $("alerts");
    if (top) feed.insertBefore(li, feed.firstChild); else feed.appendChild(li);
    while (feed.children.length > 100) feed.removeChild(feed.lastChild);
  }

  function select(pid) {
    selected = pid;
    $("sel").textContent = pid;
    for (const [id, r] of rows) r.tr.classList.toggle("selected", id === pid);
    draw();
  }

  function draw() {
    const metric = $("metric").value;
    const pts = selected ? history.get(selected + "|" + metric) || [] : [];
    window.drawChart($("chart"), pts, { thresholds: THRESH[metric] });
  }

  function init(msg) {
    $("alerts").textContent = "";
    for (const a of (msg.alerts || []).slice().reverse()) addAlert(a, false);
    applySnapshot(msg.snapshot);
  }

  function setConn(on) {
    const c = $("conn");
    c.textContent = on ? "онлайн" : "нет связи";
    c.className = "badge " + (on ? "on" : "off");
  }

  function connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => { setConn(true); delay = 1000; };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === "init") init(msg);
      else if (msg.type === "snapshot") applySnapshot(msg);
      else if (msg.type === "alert") addAlert(msg, true);
    };
    ws.onclose = () => {   // автопереподключение с нарастающей паузой до 15 с
      setConn(false);
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 15000);
    };
  }

  $("metric").addEventListener("change", draw);
  connect();
})();
