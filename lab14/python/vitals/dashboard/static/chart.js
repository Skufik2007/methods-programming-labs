"use strict";
// Минимальный линейный график на <canvas>, без библиотек. Цвета берутся из CSS-переменных страницы.
window.drawChart = function (canvas, points, opts) {
  const ctx = canvas.getContext("2d");
  const css = getComputedStyle(document.documentElement);
  const color = (n) => css.getPropertyValue(n).trim() || "#888";
  const w = canvas.width, h = canvas.height;
  const pad = { l: 48, r: 12, t: 12, b: 24 };
  ctx.clearRect(0, 0, w, h);
  ctx.font = "12px system-ui, sans-serif";
  ctx.fillStyle = color("--muted");
  ctx.strokeStyle = color("--line");
  if (!points.length) {
    ctx.fillText("нет данных", pad.l, h / 2);
    return;
  }
  let lo = Infinity, hi = -Infinity;
  for (const p of points) { lo = Math.min(lo, p.v); hi = Math.max(hi, p.v); }
  if (opts && opts.thresholds) for (const t of opts.thresholds) { lo = Math.min(lo, t); hi = Math.max(hi, t); }
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const m = (hi - lo) * 0.08; lo -= m; hi += m;
  const t0 = points[0].t, t1 = points[points.length - 1].t || t0 + 1;
  const x = (t) => pad.l + ((t - t0) / Math.max(t1 - t0, 1)) * (w - pad.l - pad.r);
  const y = (v) => h - pad.b - ((v - lo) / (hi - lo)) * (h - pad.t - pad.b);
  // сетка и подписи оси Y
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
    ctx.fillText(v.toFixed(1), 4, yy + 4);
  }
  // пороги тревог пунктиром
  if (opts && opts.thresholds) {
    ctx.strokeStyle = color("--crit"); ctx.setLineDash([6, 4]);
    for (const th of opts.thresholds) { ctx.beginPath(); ctx.moveTo(pad.l, y(th)); ctx.lineTo(w - pad.r, y(th)); ctx.stroke(); }
    ctx.setLineDash([]);
  }
  ctx.strokeStyle = color("--accent"); ctx.lineWidth = 2; ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.v)) : ctx.moveTo(x(p.t), y(p.v))));
  ctx.stroke();
  ctx.fillStyle = color("--muted");
  const span = Math.round(t1 - t0);
  ctx.fillText("−" + span + " с", pad.l, h - 6);
  ctx.fillText("сейчас", w - pad.r - 40, h - 6);
};
