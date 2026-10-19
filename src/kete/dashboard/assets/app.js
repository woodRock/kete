/* kete dashboard. Reads the model from #kete-data (built by kete/dashboard/model.py) and renders
   everything with plain DOM + SVG. No libraries, no network. Untrusted strings (merchant and
   category names) only ever go through textContent. */
(() => {
  "use strict";

  const D = JSON.parse(document.getElementById("kete-data").textContent);
  const SVGNS = "http://www.w3.org/2000/svg";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (id) => document.getElementById(id);
  let animate = !reduced; // only the first render animates; resizes redraw instantly

  // ------------------------------------------------------------------ formatting

  const money = (v, cents) => {
    const a = Math.abs(v);
    const d = cents ?? (a < 100 && a % 1 !== 0 ? 2 : 0);
    return (v < 0 ? "-" : "") + "$" + a.toLocaleString("en-NZ", { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  const compact = (v) => {
    const a = Math.abs(v), sign = v < 0 ? "-" : "";
    if (a >= 1e6) return sign + "$" + (a / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (a >= 1e3) return sign + "$" + (a / 1e3).toFixed(a >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "K";
    return sign + "$" + Math.round(a);
  };
  const monthName = (m) => {
    const [y, mo] = m.split("-").map(Number);
    return new Date(y, mo - 1, 1).toLocaleDateString("en-NZ", { month: "long", year: "numeric" });
  };
  const dayName = (iso) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString("en-NZ", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  };

  // ------------------------------------------------------------------ DOM helpers

  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "text") el.textContent = v;
      else if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(c));
    return el;
  }
  function s(tag, attrs = {}, ...children) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null) el.append(c);
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.firstChild.remove(); return el; };

  const seriesColor = (name) => {
    const i = D.series.indexOf(name);
    return i >= 0 && i < 7 && name !== "Other" ? `var(--s${i + 1})` : "var(--other)";
  };
  const categoryColor = (cat) => seriesColor(D.series.includes(cat) ? cat : "Other");

  function niceTicks(min, max, count = 4) {
    if (max <= min) max = min + 1;
    const raw = (max - min) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => st >= raw);
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let t = lo; t <= hi + step / 2; t += step) ticks.push(Math.round(t * 100) / 100);
    return ticks;
  }
  const topRounded = (x, y, w, hgt, r = 4) => {
    r = Math.max(0, Math.min(r, w / 2, hgt));
    return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
  };
  const widthOf = (el) => Math.max(260, el.clientWidth || el.parentElement.clientWidth || 600);

  function countUp(el, to, format) {
    if (!animate || !isFinite(to)) { el.textContent = format(to); return; }
    const start = performance.now(), dur = 800;
    const step = (now) => {
      const t = Math.min(1, (now - start) / dur);
      el.textContent = format(to * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ------------------------------------------------------------------ tooltip

  const tip = $("tooltip");
  function showTip(evt, title, rows) {
    clear(tip);
    tip.append(h("div", { class: "tt-title", text: title }));
    for (const r of rows) {
      tip.append(h("div", { class: "tt-row" },
        r.color ? h("i", { class: "tt-key", style: `background:${r.color}` }) : null,
        h("b", { text: r.value }), h("span", { text: r.label || "" })));
    }
    tip.hidden = false;
    let x, y;
    if (evt.clientX != null && evt.type !== "focus") { x = evt.clientX; y = evt.clientY; }
    else { const b = evt.target.getBoundingClientRect(); x = b.left + b.width / 2; y = b.top; }
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - tw - 8, Math.max(8, x + 14)) + "px";
    tip.style.top = (y - th - 12 < 8 ? y + 16 : y - th - 12) + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // ------------------------------------------------------------------ state

  const STORE = "kete:dashboard:v1";
  const saved = (() => { try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; } })();
  const persist = () => {
    try { localStorage.setItem(STORE, JSON.stringify({ opps: Object.fromEntries(state.opps), horizon: state.horizon, rate: state.rate })); }
    catch { /* private mode: fine */ }
  };
  const lastComplete = D.monthly.map((m) => m.status).lastIndexOf("complete");
  const state = {
    hidden: new Set(),
    view: "categories",
    selected: lastComplete >= 0 ? lastComplete : D.monthly.length - 1,
    compare: true,
    opps: new Map(D.opportunities.map((o) => [o.id, { enabled: o.enabled, cut: o.cut, ...(saved.opps?.[o.id] || {}) }])),
    horizon: saved.horizon || 24,
    rate: saved.rate ?? Math.round(D.config.interest_rate * 10000) / 100,
  };
  // The breakdown's period: a month (from === to) or a run of months, as yyyy-mm keys.
  state.from = state.to = state.anchor = D.monthly[state.selected].month;
  const monthlySavings = () => D.opportunities.reduce((sum, o) => {
    const st = state.opps.get(o.id);
    return sum + (st.enabled ? o.base_monthly * st.cut : 0);
  }, 0);

  // ------------------------------------------------------------------ header + KPIs

  function renderHeader() {
    const first = D.monthly[0], last = D.monthly[D.monthly.length - 1];
    const latest = D.data_quality.accounts.map((a) => a.last).sort().pop();
    $("period").textContent = `${first.label} – ${last.label}` + (latest ? ` · data to ${dayName(latest)}` : "");
    $("generated").textContent = new Date(D.generated_at).toLocaleString("en-NZ");
    $("demo-banner").hidden = !D.demo;
  }

  function sparkline(values, w = 220, hgt = 36) {
    const max = Math.max(...values, 1), n = values.length;
    const x = (i) => (i / (n - 1)) * (w - 8) + 4, y = (v) => hgt - 4 - (v / max) * (hgt - 8);
    const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    return s("svg", { viewBox: `0 0 ${w} ${hgt}`, width: "100%", height: hgt, "aria-hidden": "true", preserveAspectRatio: "none" },
      s("path", { d, fill: "none", stroke: "var(--muted)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }),
      s("circle", { cx: x(n - 1), cy: y(values[n - 1]), r: 4, fill: "var(--accent)", stroke: "var(--surface)", "stroke-width": 2 }));
  }

  function renderKpis() {
    const k = D.kpis, root = clear($("kpis"));
    const tile = (label, value, format, delta, extra, hero) => {
      const v = h("div", { class: "value" });
      const el = h("div", { class: "kpi" + (hero ? " hero" : "") }, h("div", { class: "label", text: label }), v,
        delta ? h("div", { class: "delta" }, ...[].concat(delta)) : null, extra);
      countUp(v, value, format);
      root.append(el);
      return v;
    };
    tile("Average monthly spend", k.avg_monthly_spend, (x) => money(x, 0),
      k.last_month ? `${monthName(k.last_month).split(" ")[0]}: ${money(k.last_month_spend, 0)}` : null,
      sparkline(k.spend_trend), true);
    const paceDiff = k.avg_monthly_spend ? k.this_month_pace / k.avg_monthly_spend - 1 : 0;
    tile("This month so far", k.this_month_spend, (x) => money(x, 0), [
      h("span", { class: "badge " + (paceDiff > 0.05 ? "up" : paceDiff < -0.05 ? "down" : "") },
        h("span", { class: "arrow", text: paceDiff > 0.05 ? "▲" : paceDiff < -0.05 ? "▼" : "•" }),
        `On pace for ${money(k.this_month_pace, 0)}`),
      h("span", { text: `· day ${k.this_month_day} of ${k.this_month_days}` })]);
    tile("Savings rate", k.savings_rate ?? 0, (x) => (k.savings_rate == null ? "—" : Math.round(x) + "%"),
      k.savings_rate == null ? "Not enough history yet" : `${money(k.avg_monthly_net, 0)} a month left over`);
    const pot = tile("Potential savings", monthlySavings(), (x) => money(x, 0) + "/mo", h("span", { id: "kpi-potential-sub" }));
    pot.id = "kpi-potential";
    updatePotential();
  }

  function updatePotential() {
    const m = monthlySavings();
    const v = $("kpi-potential"), sub = $("kpi-potential-sub");
    if (v && !animate) v.textContent = money(m, 0) + "/mo";
    if (sub) sub.textContent = m ? `${money(m * 12, 0)} a year from selected changes` : "Pick changes under Where to cut";
    const pill = clear($("savings-total"));
    pill.append(h("b", { text: money(m, 0) }), " a month · ", money(m * 12, 0) + " a year");
  }

  function renderInsights() {
    const root = clear($("insights"));
    const icon = { good: "✓", warn: "!", info: "i" };
    for (const it of D.insights) {
      root.append(h("div", { class: "insight " + it.tone },
        h("span", { class: "icon", "aria-hidden": "true", text: icon[it.tone] || "i" }),
        h("div", {}, h("strong", { text: it.title }), h("p", { text: it.body }))));
    }
  }

  // ------------------------------------------------------------------ monthly chart

  function renderLegend() {
    const root = clear($("legend"));
    if (state.view === "cashflow") {
      root.append(h("span", { class: "legend-item" }, h("i", { class: "key", style: "background:var(--s1)" }), " Income"),
        h("span", { class: "legend-item" }, h("i", { class: "key", style: "background:var(--s2)" }), " Spending"));
      return;
    }
    for (const name of D.series) {
      root.append(h("button", {
        type: "button", "aria-pressed": String(!state.hidden.has(name)), title: "Show or hide",
        onclick: () => { state.hidden.has(name) ? state.hidden.delete(name) : state.hidden.add(name); renderLegend(); renderMonthly(); },
      }, h("i", { class: "key", style: `background:${seriesColor(name)}` }), name));
    }
  }

  function renderMonthly() {
    const root = clear($("monthly-chart"));
    const W = widthOf(root), H = 280, m = { t: 12, r: 8, b: 34, l: 48 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b, months = D.monthly, n = months.length, band = pw / n;
    const visible = D.series.filter((x) => !state.hidden.has(x));
    const cash = state.view === "cashflow";
    const totals = months.map((mo) => cash ? Math.max(mo.income, mo.spend) : visible.reduce((a, k) => a + (mo.by_series[k] || 0), 0));
    const ticks = niceTicks(0, Math.max(...totals, 1));
    const ymax = ticks[ticks.length - 1];
    const y = (v) => m.t + ph - (v / ymax) * ph;
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
      "aria-label": cash ? "Monthly income and spending" : "Monthly spending by category" });

    for (const t of ticks) {
      svg.append(s("line", { class: t ? "gridline" : "baseline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }),
        s("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", text: compact(t) }));
    }
    const labelEvery = band < 46 ? 2 : 1;
    months.forEach((mo, i) => {
      const cx = m.l + band * i + band / 2;
      const inPeriod = mo.month >= state.from && mo.month <= state.to;
      const g = s("g", { class: "bar-group" + (inPeriod ? " selected" : ""), tabindex: 0, role: "button",
        "aria-label": `${monthName(mo.month)}: spent ${money(mo.spend, 0)}, income ${money(mo.income, 0)}` });
      g.append(s("rect", { class: "col-bg", x: m.l + band * i + 2, y: m.t, width: band - 4, height: ph, rx: 6, fill: "transparent" }));
      const delay = animate ? `animation-delay:${i * 35}ms` : "";
      const faded = mo.status === "incomplete" ? 0.4 : mo.status === "partial" ? 0.7 : 1;
      if (cash) {
        const bw = Math.min(20, (band - 12) / 2);
        [["income", "var(--s1)", -1], ["spend", "var(--s2)", 1]].forEach(([key, col, side]) => {
          const v = mo[key]; if (!v) return;
          const x0 = side < 0 ? cx - bw - 1 : cx + 1, top = y(v);
          g.append(s("path", { class: "bar" + (animate ? " grow" : ""), style: delay, d: topRounded(x0, top, bw, y(0) - top), fill: col, opacity: faded }));
        });
        const rows = [{ value: money(mo.income, 0), label: "Income", color: "var(--s1)" },
          { value: money(mo.spend, 0), label: "Spending", color: "var(--s2)" },
          { value: money(mo.net, 0), label: "Left over" }];
        g.addEventListener("pointermove", (e) => showTip(e, monthName(mo.month) + statusNote(mo), rows));
        g.addEventListener("focus", (e) => showTip(e, monthName(mo.month) + statusNote(mo), rows));
      } else {
        const bw = Math.min(24, band - 10);
        let cum = 0;
        const stack = visible.filter((k) => mo.by_series[k] > 0);
        stack.forEach((k, j) => {
          const v = mo.by_series[k];
          const bottom = y(cum), top = y(cum + v);
          cum += v;
          const gap = j > 0 ? 2 : 0, hgt = Math.max(0, bottom - top - gap);
          if (hgt <= 0) return;
          const attrs = { class: "bar" + (animate ? " grow" : ""), style: delay, fill: seriesColor(k), opacity: faded };
          const mark = j === stack.length - 1
            ? s("path", { ...attrs, d: topRounded(cx - bw / 2, top, bw, hgt) })
            : s("rect", { ...attrs, x: cx - bw / 2, y: top, width: bw, height: hgt });
          mark.addEventListener("pointermove", (e) => {
            const rows = [{ value: money(v, 0), label: k, color: seriesColor(k) }];
            for (const other of stack) if (other !== k) rows.push({ value: money(mo.by_series[other], 0), label: other, color: seriesColor(other) });
            rows.push({ value: money(cum > 0 ? stack.reduce((a, x) => a + mo.by_series[x], 0) : 0, 0), label: "Total shown" });
            showTip(e, monthName(mo.month) + statusNote(mo), rows);
          });
          g.append(mark);
        });
        g.addEventListener("focus", (e) => showTip(e, monthName(mo.month) + statusNote(mo),
          stack.map((k) => ({ value: money(mo.by_series[k], 0), label: k, color: seriesColor(k) }))));
      }
      g.addEventListener("pointerleave", hideTip);
      g.addEventListener("blur", hideTip);
      const select = (e) => {
        state.selected = i;
        if (e && e.shiftKey) setPeriod(state.anchor < mo.month ? state.anchor : mo.month, state.anchor < mo.month ? mo.month : state.anchor, false);
        else setPeriod(mo.month, mo.month);
      };
      g.addEventListener("click", select);
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); }
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          state.selected = Math.max(0, Math.min(n - 1, i + (e.key === "ArrowRight" ? 1 : -1)));
          setPeriod(months[state.selected].month, months[state.selected].month);
          root.querySelectorAll(".bar-group")[state.selected]?.focus();
        }
      });
      svg.append(g);
      if (i % labelEvery === (n - 1) % labelEvery) {
        svg.append(s("text", { x: cx, y: H - 16, "text-anchor": "middle", text: mo.label }));
        if (mo.status === "partial") svg.append(s("text", { x: cx, y: H - 3, "text-anchor": "middle", text: "so far" }));
      }
    });
    root.append(svg);
  }
  const statusNote = (mo) => mo.status === "partial" ? " (so far)" : mo.status === "incomplete" ? " (incomplete data)" : "";

  function hbars(root, items, color, valueFmt = (v) => money(v, 0)) {
    const max = Math.max(...items.map((i) => Math.max(i.value, i.was || 0)), 1);
    items.forEach((it, idx) => {
      const fill = h("div", { class: "fill" + (animate ? " grow-x" : ""),
        style: `width:${(it.value / max) * 100}%;background:${color(it)};animation-delay:${idx * 30}ms` });
      const track = h("div", { class: "track" }, fill);
      if (it.was != null) {
        const tick = h("i", { class: "was", style: `left:${(it.was / max) * 100}%` });
        const tipRows = [{ value: money(it.value, 0), label: "This period", color: color(it) }, { value: money(it.was, 0), label: "A year earlier" }];
        track.addEventListener("pointermove", (e) => showTip(e, it.name, tipRows));
        track.addEventListener("pointerleave", hideTip);
        track.append(tick);
      }
      root.append(h("div", { class: "hbar" },
        h("div", { class: "name", title: it.name }, it.swatch ? h("i", { class: "swatch", style: `background:${color(it)}` }) : null,
          h("span", { text: it.name }), it.tag ? h("span", { class: "tag", text: it.tag }) : null),
        track,
        h("div", { class: "amt" }, valueFmt(it.value, it), it.was != null ? h("span", { class: "was-txt", text: `was ${money(it.was, 0)}` }) : null)));
    });
  }

  // ------------------------------------------------------------------ any-period breakdown

  const allMonths = D.months_all || D.monthly;
  const shiftYear = (key, by) => `${Number(key.slice(0, 4)) + by}${key.slice(4)}`;
  const periodLabel = (from, to) => from === to ? monthName(from)
    : from.slice(0, 4) === to.slice(0, 4) ? `${monthName(from).split(" ")[0]} – ${monthName(to)}` : `${monthName(from)} – ${monthName(to)}`;

  function sumMonths(from, to) {
    const ms = allMonths.filter((m) => m.month >= from && m.month <= to);
    const cats = {};
    for (const m of ms) for (const [c, v] of Object.entries(m.by_category)) cats[c] = (cats[c] || 0) + v;
    return { months: ms, cats, spend: ms.reduce((a, m) => a + m.spend, 0) };
  }

  function setPeriod(from, to, moveAnchor = true) {
    state.from = from; state.to = to;
    if (moveAnchor) state.anchor = from;
    $("period-from").value = from; $("period-to").value = to;
    renderMonthly(); renderBreakdown();
  }

  function renderBreakdown() {
    const now = sumMonths(state.from, state.to);
    const partial = now.months.some((m) => m.status === "partial") ? " (so far)" : "";
    $("breakdown-title").textContent = `${periodLabel(state.from, state.to)}${partial} · ${money(now.spend, 0)} spent`;
    const pFrom = shiftYear(state.from, -1), pTo = shiftYear(state.to, -1);
    const hasPrev = allMonths.length && allMonths[0].month <= pFrom && allMonths.some((m) => m.month >= pFrom && m.month <= pTo && m.spend > 0);
    const compare = state.compare && hasPrev;
    const prev = compare ? sumMonths(pFrom, pTo) : null;
    const note = $("breakdown-compare");
    $("period-compare").disabled = !hasPrev;
    if (compare) {
      const d = now.spend - prev.spend;
      note.textContent = `${periodLabel(pFrom, pTo)}: ${money(prev.spend, 0)} (${d >= 0 ? "+" : "−"}${money(Math.abs(d), 0)}${prev.spend ? `, ${d >= 0 ? "+" : "−"}${Math.abs(Math.round(d / prev.spend * 100))}%` : ""}). The line on each bar marks a year earlier.`
        + (partial ? " This period isn't over yet." : "");
    } else {
      note.textContent = hasPrev ? "" : "No data from a year earlier to compare with.";
    }
    const root = clear($("breakdown"));
    const names = Object.keys(now.cats);
    if (compare) for (const c of Object.keys(prev.cats)) if (!names.includes(c) && prev.cats[c] >= 1) names.push(c);
    const items = names.map((name) => ({ name, value: now.cats[name] || 0, swatch: true, was: compare ? (prev.cats[name] || 0) : null }))
      .sort((a, b) => Math.max(b.value, b.was || 0) - Math.max(a.value, a.was || 0));
    if (!items.length) { root.append(h("p", { class: "empty", text: "No spending recorded in this period." })); return; }
    hbars(root, items, (it) => categoryColor(it.name));
  }

  function renderNotes() {
    const root = clear($("month-notes"));
    const k = D.kpis, dq = D.data_quality;
    const notes = [];
    const partial = D.monthly.find((x) => x.status === "partial");
    if (partial) notes.push(`${monthName(partial.month)} is in progress (day ${k.this_month_day} of ${k.this_month_days}), so its bar is shorter.`);
    if (D.monthly.some((x) => x.status === "incomplete"))
      notes.push(`Faded months are before ${monthName(D.windows.coverage_start)}, when not every account has data, so they understate spending.`);
    if (dq.uncategorised_share >= 5) notes.push(`${dq.uncategorised_share}% of recent spending is uncategorised. Categorise it in kete tui (tab 4) for sharper suggestions.`);
    notes.push("Transfers between your own accounts are left out, so moving money to savings isn't counted as spending.");
    for (const n of notes) root.append(h("li", { text: n }));
  }

  function renderMonthlyTable() {
    const root = clear($("monthly-table"));
    const head = h("tr", {}, h("th", { text: "Month" }), D.series.map((k) => h("th", { class: "num", text: k })),
      h("th", { class: "num", text: "Spending" }), h("th", { class: "num", text: "Income" }), h("th", { class: "num", text: "Left over" }));
    const rows = D.monthly.map((mo) => h("tr", {}, h("td", { text: mo.label + statusNote(mo) }),
      D.series.map((k) => h("td", { class: "num", text: money(mo.by_series[k] || 0, 0) })),
      h("td", { class: "num", text: money(mo.spend, 0) }), h("td", { class: "num", text: money(mo.income, 0) }),
      h("td", { class: "num", text: money(mo.net, 0) })));
    root.append(h("div", { style: "overflow-x:auto" }, h("table", {}, h("thead", {}, head), h("tbody", {}, rows))));
  }

  // ------------------------------------------------------------------ trends

  function trendSpark(values, color) {
    const w = 120, hgt = 28, max = Math.max(...values, 1), n = values.length;
    const x = (i) => (i / (n - 1)) * (w - 8) + 4, y = (v) => hgt - 4 - (v / max) * (hgt - 8);
    return s("svg", { viewBox: `0 0 ${w} ${hgt}`, width: w, height: hgt, "aria-hidden": "true" },
      s("path", { d: values.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(""), fill: "none", stroke: color, "stroke-width": 1.5, "stroke-linejoin": "round" }),
      s("circle", { cx: x(n - 1), cy: y(values[n - 1]), r: 3, fill: color, stroke: "var(--surface)", "stroke-width": 1.5 }));
  }
  function changeBadge(pctv) {
    if (pctv == null) return h("span", { class: "badge", text: "new" });
    const dir = pctv >= 5 ? "up" : pctv <= -5 ? "down" : "";
    return h("span", { class: "badge " + dir },
      h("span", { class: "arrow", "aria-hidden": "true", text: dir === "up" ? "▲" : dir === "down" ? "▼" : "≈" }),
      (pctv > 0 ? "+" : "") + Math.round(pctv) + "%");
  }
  function renderTrends() {
    const r = D.windows.recent.length, b = D.windows.baseline.length;
    $("trend-hint").textContent = r
      ? `Average per month over your last ${r} complete month${r === 1 ? "" : "s"}` + (b ? `, compared with the ${b} before.` : ".")
      : "Not enough complete months yet.";
    const cats = D.categories;
    const row = (c) => h("tr", {},
      h("td", {}, h("i", { class: "swatch", style: `background:${categoryColor(c.name)}` }), c.name,
        c.discretionary ? h("span", { class: "tag", text: "want" }) : null),
      h("td", {}, trendSpark(c.monthly, categoryColor(c.name))),
      h("td", { class: "num", text: money(c.recent_avg, 0) }),
      h("td", { class: "num", text: c.baseline_avg == null ? "—" : money(c.baseline_avg, 0) }),
      h("td", { class: "num" }, changeBadge(c.change_pct)));
    const head = h("tr", {}, h("th", { text: "Category" }), h("th", { text: "Last 12 months" }),
      h("th", { class: "num", text: "Recent / mo" }), h("th", { class: "num", text: "Before / mo" }), h("th", { class: "num", text: "Change" }));
    const sorted = [...cats].sort((a, z) => z.recent_avg - a.recent_avg);
    const root = clear($("trend-table"));
    root.append(h("div", { style: "overflow-x:auto" }, h("table", {}, h("thead", {}, head), h("tbody", {}, sorted.slice(0, 10).map(row)))));
    if (sorted.length > 10) {
      root.append(h("details", {}, h("summary", { text: `Show ${sorted.length - 10} more` }),
        h("div", { style: "overflow-x:auto" }, h("table", {}, h("tbody", {}, sorted.slice(10).map(row))))));
    }
  }

  // ------------------------------------------------------------------ patterns

  function renderHeatmap() {
    const root = clear($("heatmap"));
    const days = D.daily, weeks = Math.ceil(days.length / 7);
    const W = widthOf(root), left = 30, top = 18;
    const gap = 3, cell = Math.max(8, Math.min(18, Math.floor((W - left) / weeks) - gap));
    const H = top + 7 * (cell + gap) + 26;
    const nonzero = days.map((d) => d.spend).filter((v) => v > 0).sort((a, b) => a - b);
    const q = [0.2, 0.4, 0.6, 0.8].map((p) => nonzero[Math.floor(p * (nonzero.length - 1))] || 0);
    const level = (v) => (v <= 0 ? 0 : 1 + q.filter((t) => v > t).length);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Daily spending calendar" });
    ["Mon", "", "Wed", "", "Fri", "", "Sun"].forEach((l, i) => l && svg.append(s("text", { x: 0, y: top + i * (cell + gap) + cell - 2, text: l })));
    let lastMonth = "";
    days.forEach((d, i) => {
      const w = Math.floor(i / 7), wd = i % 7;
      const x = left + w * (cell + gap), yy = top + wd * (cell + gap);
      const mo = d.date.slice(0, 7);
      if (wd === 0 && mo !== lastMonth) {
        lastMonth = mo;
        svg.append(s("text", { x, y: 11, text: new Date(d.date + "T00:00").toLocaleDateString("en-NZ", { month: "short" }) }));
      }
      if (d.date > D.today) return;
      const r = s("rect", { class: "cell" + (animate ? " fade" : ""), x, y: yy, width: cell, height: cell, rx: 3,
        fill: `var(--seq${level(d.spend)})`, style: animate ? `animation-delay:${w * 12}ms` : null, tabindex: -1 });
      r.addEventListener("pointermove", (e) => showTip(e, dayName(d.date), [{ value: money(d.spend, 0), label: d.spend ? "spent" : "no spending" }]));
      r.addEventListener("pointerleave", hideTip);
      svg.append(r);
    });
    const ly = H - 12;
    svg.append(s("text", { x: left, y: ly + 9, text: "Less" }));
    for (let i = 0; i <= 5; i++) svg.append(s("rect", { x: left + 30 + i * (cell + 2), y: ly, width: cell, height: cell > 12 ? 12 : cell, rx: 3, fill: `var(--seq${i})` }));
    svg.append(s("text", { x: left + 30 + 6 * (cell + 2) + 4, y: ly + 9, text: "More" }));
    root.append(svg);
  }

  function renderWeekday() {
    const root = clear($("weekday"));
    const W = widthOf(root), H = 220, m = { t: 22, r: 6, b: 24, l: 6 };
    const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], vals = D.weekday;
    const max = Math.max(...vals, 1), band = (W - m.l - m.r) / 7, bw = Math.min(24, band - 8), ph = H - m.t - m.b;
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Average spending by day of week" });
    svg.append(s("line", { class: "baseline", x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph }));
    const hi = vals.indexOf(Math.max(...vals));
    vals.forEach((v, i) => {
      const cx = m.l + band * i + band / 2, hgt = (v / max) * ph, top = m.t + ph - hgt;
      const g = s("g", { class: "bar-group", tabindex: 0, "aria-label": `${names[i]}: ${money(v, 0)}` });
      g.append(s("rect", { x: cx - band / 2, y: m.t, width: band, height: ph, fill: "transparent" }));
      if (hgt > 0) g.append(s("path", { class: "bar" + (animate ? " grow" : ""), style: animate ? `animation-delay:${i * 40}ms` : null,
        d: topRounded(cx - bw / 2, top, bw, hgt), fill: "var(--accent)" }));
      if (i === hi) svg.append(s("text", { x: cx, y: top - 6, "text-anchor": "middle", text: money(v, 0) }));
      const show = (e) => showTip(e, names[i], [{ value: money(v, 0), label: "average spend" }]);
      g.addEventListener("pointermove", show); g.addEventListener("focus", show);
      g.addEventListener("pointerleave", hideTip); g.addEventListener("blur", hideTip);
      svg.append(g, s("text", { x: cx, y: H - 6, "text-anchor": "middle", text: names[i] }));
    });
    root.append(svg);
  }

  function renderMerchants() {
    const n = D.windows.recent.length || 1;
    $("merchant-hint").textContent = `Average per month over the last ${n} month${n === 1 ? "" : "s"}.`;
    const root = clear($("merchants"));
    const items = D.merchants.slice(0, 10).map((x) => ({ name: x.name, value: x.monthly_avg,
      tag: x.subscription ? "subscription" : `${x.per_month}×/mo`, raw: x }));
    if (!items.length) { root.append(h("p", { class: "empty", text: "No recent spending." })); return; }
    hbars(root, items, () => "var(--accent)", (v, it) => `${money(v, 0)}/mo`);
  }

  // ------------------------------------------------------------------ opportunities

  function renderOpportunities() {
    const root = clear($("opportunities"));
    const opps = D.opportunities.filter((o) => o.group === "categories");
    if (!opps.length) root.append(h("p", { class: "empty", text: "No clear savings found in your recent spending. Nice." }));
    const grid = h("div", { class: "opps" });
    for (const o of opps) {
      const st = state.opps.get(o.id);
      const saving = h("div", { class: "saving" });
      const card = h("div", { class: "opp" + (st.enabled ? " on" : "") });
      const refresh = () => {
        card.classList.toggle("on", st.enabled);
        const mth = o.base_monthly * st.cut;
        clear(saving).append(st.enabled ? "Saves " : "Would save ", h("b", { text: money(mth, 0) }), "/month · ",
          h("b", { text: money(mth * 12, 0) }), "/year");
      };
      const box = h("input", { type: "checkbox", checked: st.enabled || null,
        onchange: (e) => { st.enabled = e.target.checked; refresh(); changed(); } });
      card.append(h("label", { class: "title" }, box, h("span", { text: o.title })),
        h("p", { text: o.detail }), o.evidence ? h("p", { class: "evidence", text: o.evidence }) : null);
      if (o.adjustable) {
        const label = h("span", { style: "min-width:92px" });
        const setLabel = () => { label.textContent = o.kind === "restore" ? `${Math.round(st.cut * 100)}% of the rise` : `Cut ${Math.round(st.cut * 100)}%`; };
        const range = h("input", { type: "range", min: 0, max: 100, step: 5, value: Math.round(st.cut * 100), "aria-label": `How much to cut ${o.title}`,
          oninput: (e) => { st.cut = e.target.value / 100; setLabel(); if (!st.enabled && st.cut > 0) { st.enabled = true; box.checked = true; } refresh(); changed(); } });
        setLabel();
        card.append(h("div", { class: "slider" }, label, range));
      }
      card.append(saving);
      refresh();
      grid.append(card);
    }
    root.append(grid);

    const subsRoot = clear($("subscriptions"));
    const subs = D.opportunities.filter((o) => o.group === "subscriptions").sort((a, z) => (a.bill ? 1 : 0) - (z.bill ? 1 : 0));
    if (!subs.length) { subsRoot.append(h("p", { class: "empty", text: "No regular subscriptions detected." })); return; }
    const list = h("div", { class: "subs" });
    for (const o of subs) {
      const st = state.opps.get(o.id);
      const item = h("label", { class: "sub-item" + (st.enabled ? " on" : "") });
      item.append(h("input", { type: "checkbox", checked: st.enabled || null, "aria-label": `Cancel ${o.title}`,
          onchange: (e) => { st.enabled = e.target.checked; item.classList.toggle("on", st.enabled); changed(); } }),
        h("div", { class: "grow1", title: o.evidence }, h("div", {}, o.title, o.bill ? h("span", { class: "tag", text: "bill" }) : null),
          h("small", { text: o.detail })),
        h("div", { class: "amt" }, h("div", { text: money(o.base_monthly) + "/mo" }), h("small", { text: money(o.base_monthly * 12, 0) + "/yr" })));
      list.append(item);
    }
    subsRoot.append(h("p", { class: "hint", text: "Tick the ones you'd cancel. Bills are listed too, in case a cheaper plan exists." }), list);
  }

  function changed() { updatePotential(); renderProjection(); persist(); }

  // ------------------------------------------------------------------ projection

  function project(extra) {
    const r = state.rate / 100 / 12, start = D.projection.start_balance ?? 0, out = [start];
    let bal = start;
    for (let i = 1; i <= state.horizon; i++) { bal = bal * (1 + r) + D.projection.monthly_net + extra; out.push(bal); }
    return out;
  }

  function renderProjection() {
    const extra = monthlySavings();
    const base = project(0), withC = project(extra);
    const root = clear($("projection-chart"));
    const W = widthOf(root), H = 300, m = { t: 16, r: 92, b: 30, l: 56 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b, n = base.length;
    const goal = D.config.savings_goal || 0;
    const all = [...base, ...withC, 0].concat(goal ? [goal] : []);
    const ticks = niceTicks(Math.min(...all), Math.max(...all));
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const x = (i) => m.l + (i / (n - 1)) * pw, y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Projected savings" });
    for (const t of ticks) svg.append(s("line", { class: t === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }),
      s("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", text: compact(t) }));
    const step = n > 37 ? 12 : n > 13 ? 6 : 3;
    for (let i = 0; i < n; i += step) svg.append(s("text", { x: x(i), y: H - 8, "text-anchor": "middle", text: i === 0 ? "Now" : (i % 12 === 0 ? `${i / 12}y` : `${i}m`) }));
    if (goal) {
      svg.append(s("line", { x1: m.l, x2: W - m.r, y1: y(goal), y2: y(goal), stroke: "var(--ink-2)", "stroke-width": 1, opacity: 0.5 }),
        s("text", { x: m.l + 4, y: y(goal) - 6, text: `Goal ${compact(goal)}` }));
    }
    const path = (vals) => vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const area = path(withC) + `L${x(n - 1)},${y(Math.max(lo, 0))}L${x(0)},${y(Math.max(lo, 0))}Z`;
    svg.append(s("path", { d: area, fill: "var(--s1)", opacity: 0.1, class: animate ? "fade" : null }));
    const line = (vals, color, label) => {
      const p = s("path", { d: path(vals), fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
      svg.append(p);
      if (animate) { const len = p.getTotalLength?.() || 2000; p.style.strokeDasharray = len; p.style.setProperty("--len", len); p.classList.add("draw"); }
      return { vals, color, label };
    };
    const series = [line(base, "var(--other)", "Current path"), line(withC, "var(--s1)", "With changes")];
    // end labels: both if they separate, otherwise only the highlighted line
    const yA = y(base[n - 1]), yB = y(withC[n - 1]);
    const labels = Math.abs(yA - yB) >= 16 ? series : [series[1]];
    for (const sr of labels) {
      const yy = y(sr.vals[n - 1]);
      svg.append(s("circle", { cx: x(n - 1), cy: yy, r: 4, fill: sr.color, stroke: "var(--surface)", "stroke-width": 2 }),
        s("text", { x: x(n - 1) + 10, y: yy + 4, text: compact(sr.vals[n - 1]), style: "fill:var(--ink);font-weight:600" }));
    }
    // crosshair
    const cross = s("line", { class: "crosshair", y1: m.t, y2: m.t + ph, visibility: "hidden" });
    const dots = series.map((sr) => s("circle", { r: 4, fill: sr.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }));
    svg.append(cross, ...dots);
    const overlay = s("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent" });
    overlay.addEventListener("pointermove", (e) => {
      const b = svg.getBoundingClientRect();
      const px = ((e.clientX - b.left) / b.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / pw) * (n - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      series.forEach((sr, k) => { dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(sr.vals[i])); dots[k].setAttribute("visibility", "visible"); });
      showTip(e, i === 0 ? "Today" : `In ${i} month${i === 1 ? "" : "s"}`, [
        { value: money(withC[i], 0), label: "With changes", color: "var(--s1)" },
        { value: money(base[i], 0), label: "Current path", color: "var(--other)" },
        { value: (withC[i] - base[i] >= 0 ? "+" : "") + money(withC[i] - base[i], 0), label: "Difference" }]);
    });
    overlay.addEventListener("pointerleave", () => { hideTip(); cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); });
    svg.append(overlay);
    root.append(svg);

    const legend = clear($("projection-legend"));
    for (const sr of [...series].reverse()) legend.append(h("span", {}, h("i", { class: "line-key", style: `background:${sr.color}` }), " " + sr.label));

    const net = D.projection.monthly_net, yrs = state.horizon / 12;
    const when = yrs === 1 ? "In a year" : `In ${yrs} years`;
    $("projection-hint").textContent = `Assumes your average monthly surplus of ${money(net, 0)} continues, with ${state.rate}% interest compounded monthly. ` +
      (D.projection.start_balance == null ? "Starts from $0 because not every account has a known balance (kete balance set)." : `Starts from your current balance of ${money(D.projection.start_balance, 0)}.`);
    const sum = clear($("projection-summary"));
    sum.append(`${when}: `, h("b", { text: money(base[n - 1], 0) }), " on your current path",
      ...(extra > 0 ? [", ", h("b", { text: money(withC[n - 1], 0) }), " with the selected changes, ",
        h("b", { text: "+" + money(withC[n - 1] - base[n - 1], 0) }), " more."] : [". Select changes above to compare."]));
    if (goal) {
      const hit = (vals) => { const i = vals.findIndex((v) => v >= goal); return i < 0 ? null : i; };
      const a = hit(base), b = hit(withC);
      sum.append(h("br"), `Goal of ${money(goal, 0)}: ` + (a == null ? "not reached on your current path" : `month ${a} on your current path`) +
        (extra > 0 ? (b == null ? ", not reached with changes." : `, month ${b} with changes.`) : "."));
    }
    if (net + extra <= 0) sum.append(h("br"), "Your balance shrinks at this rate: spending is above income.");

    const tbl = clear($("projection-table"));
    const rows = [];
    for (let i = 0; i < n; i += 3) rows.push(h("tr", {}, h("td", { text: i === 0 ? "Now" : `${i} months` }),
      h("td", { class: "num", text: money(base[i], 0) }), h("td", { class: "num", text: money(withC[i], 0) })));
    tbl.append(h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "When" }), h("th", { class: "num", text: "Current path" }), h("th", { class: "num", text: "With changes" }))), h("tbody", {}, rows)));
  }

  // ------------------------------------------------------------------ footer, theme, wiring

  function renderPatternHints() {
    const fixed = (D.config.fixed_costs || []).slice(0, 3).map((x) => x.toLowerCase()).join(", ");
    if (!fixed) return;
    $("heatmap-hint").textContent = `Day-to-day spending over the last six months (excluding ${fixed} and other fixed costs). Darker days cost more.`;
    $("weekday-hint").textContent = "Average day-to-day spend, by the date the bank posted it.";
  }

  function renderQuality() {
    const dq = D.data_quality, root = clear($("quality"));
    const items = [`${dq.months_complete} complete month${dq.months_complete === 1 ? "" : "s"} of data across ${dq.accounts.length} account${dq.accounts.length === 1 ? "" : "s"}.`];
    if (dq.stale_accounts.length) items.push(`Stale (over 30 days since the last transaction): ${dq.stale_accounts.join(", ")}. Export fresh data from Westpac One.`);
    if (dq.uncategorised_share) items.push(`${dq.uncategorised_share}% of recent spending is uncategorised.`);
    for (const t of items) root.append(h("li", { text: t }));
  }

  function setupTheme() {
    const btn = $("theme"), root = document.documentElement;
    const apply = (t) => { if (t) root.setAttribute("data-theme", t); else root.removeAttribute("data-theme"); };
    let t = null;
    try { t = localStorage.getItem("kete:theme"); } catch { /* ignore */ }
    apply(t);
    btn.addEventListener("click", () => {
      const dark = root.getAttribute("data-theme") === "dark" || (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
      t = dark ? "light" : "dark";
      apply(t);
      try { localStorage.setItem("kete:theme", t); } catch { /* ignore */ }
    });
  }

  function setupPeriod() {
    const from = $("period-from"), to = $("period-to");
    for (const sel of [from, to]) for (const m of [...allMonths].reverse()) sel.append(h("option", { value: m.month, text: monthName(m.month) }));
    from.value = state.from; to.value = state.to;
    from.addEventListener("change", () => setPeriod(from.value, to.value < from.value ? from.value : to.value));
    to.addEventListener("change", () => setPeriod(from.value > to.value ? to.value : from.value, to.value));
    $("period-compare").addEventListener("change", (e) => { state.compare = e.target.checked; renderBreakdown(); });
  }

  function setupControls() {
    setupPeriod();
    const seg = document.querySelectorAll(".seg button");
    seg.forEach((b) => b.addEventListener("click", () => {
      state.view = b.dataset.view;
      seg.forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      renderLegend(); renderMonthly();
    }));
    const hz = $("horizon"), rate = $("rate");
    hz.value = String(state.horizon);
    rate.value = String(state.rate);
    hz.addEventListener("change", () => { state.horizon = Number(hz.value); renderProjection(); persist(); });
    rate.addEventListener("input", () => { const v = parseFloat(rate.value); if (isFinite(v) && v >= 0 && v <= 20) { state.rate = v; renderProjection(); persist(); } });
  }

  function setupReveal() {
    const els = document.querySelectorAll(".reveal");
    if (reduced || !("IntersectionObserver" in window)) { els.forEach((e) => e.classList.add("in")); return; }
    const io = new IntersectionObserver((entries) => entries.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } }), { threshold: 0.08 });
    els.forEach((e) => io.observe(e));
    const links = [...document.querySelectorAll("nav a")];
    const nav = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (en.isIntersecting) links.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + en.target.id));
    }), { rootMargin: "-40% 0px -55% 0px" });
    document.querySelectorAll("main > section").forEach((sec) => nav.observe(sec));
  }

  function renderCharts() { renderMonthly(); renderBreakdown(); renderHeatmap(); renderWeekday(); renderProjection(); }

  renderHeader();
  setupTheme();
  setupControls();
  renderKpis();
  renderInsights();
  renderLegend();
  renderNotes();
  renderMonthlyTable();
  renderTrends();
  renderMerchants();
  renderOpportunities();
  renderQuality();
  renderPatternHints();
  renderCharts();
  setupReveal();
  animate = false;

  let lastW = document.querySelector("main").clientWidth, timer;
  new ResizeObserver(() => {
    const w = document.querySelector("main").clientWidth;
    if (w === lastW) return;
    lastW = w; clearTimeout(timer); timer = setTimeout(renderCharts, 120);
  }).observe(document.querySelector("main"));
})();
