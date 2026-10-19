/* kete report. Reads the model from #kete-data (built by kete/report/__init__.py) and renders it
   with plain DOM + SVG. No libraries, no network. Untrusted strings only go through textContent. */
(() => {
  "use strict";

  const R = JSON.parse(document.getElementById("kete-data").textContent);
  const SVGNS = "http://www.w3.org/2000/svg";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (id) => document.getElementById(id);
  let animate = !reduced;

  const money = (v, cents) => {
    const a = Math.abs(v);
    const d = cents ?? (a < 100 && a % 1 !== 0 ? 2 : 0);
    return (v < 0 ? "-" : "") + "$" + a.toLocaleString("en-NZ", { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + money(Math.abs(v), 0);
  const compact = (v) => {
    const a = Math.abs(v), sign = v < 0 ? "-" : "";
    if (a >= 1e3) return sign + "$" + (a / 1e3).toFixed(a >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "K";
    return sign + "$" + Math.round(a);
  };
  const longDate = (iso) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString("en-NZ", { day: "numeric", month: "short", year: "numeric" });
  };

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
  function s(tag, attrs = {}) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) k === "text" ? (el.textContent = v) : el.setAttribute(k, v);
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.firstChild.remove(); return el; };

  const tip = $("tooltip");
  function showTip(evt, title, rows) {
    clear(tip).append(h("div", { class: "tt-title", text: title }));
    for (const r of rows) tip.append(h("div", { class: "tt-row" },
      r.color ? h("i", { class: "tt-key", style: `background:${r.color}` }) : null, h("b", { text: r.value }), h("span", { text: r.label || "" })));
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = Math.min(innerWidth - tw - 8, Math.max(8, evt.clientX + 14)) + "px";
    tip.style.top = (evt.clientY - th - 12 < 8 ? evt.clientY + 16 : evt.clientY - th - 12) + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // Dismissals made in this page are remembered in the browser; `kete report dismiss` makes them
  // permanent in the database, so future reports hide them too.
  const STORE = "kete:report:dismissed";
  const local = (() => { try { return new Set(JSON.parse(localStorage.getItem(STORE)) || []); } catch { return new Set(); } })();
  const saveLocal = () => { try { localStorage.setItem(STORE, JSON.stringify([...local])); } catch { /* fine */ } };
  const isDismissed = (f) => f.dismissed || local.has(f.id);
  const state = { filter: "all", showDismissed: false };

  // ------------------------------------------------------------------ header + summary

  function renderHeader() {
    const p = R.period;
    $("title").textContent = p.label;
    document.title = `kete report · ${p.label}`;
    $("period").textContent = `${longDate(p.start)} – ${longDate(p.end)}`;
    $("generated").textContent = new Date(R.generated_at).toLocaleString("en-NZ");
    $("demo-banner").hidden = !R.demo;
    if (!p.complete) {
      const b = $("partial-banner");
      b.hidden = false;
      b.textContent = `This period isn't over yet: it covers ${p.days} days so far. Some checks only run on complete periods.`;
    }
    $("detectors").textContent = `Checks run: ${R.detectors.map((d) => d.replace(/_/g, " ")).join(", ")}.`;
  }

  function renderKpis() {
    const sm = R.summary, pv = R.previous, root = clear($("kpis"));
    const tile = (label, value, prevValue, upIsGood, fmt = (v) => money(v, 0)) => {
      let delta = null;
      if (pv.has_data && prevValue != null && value != null) {
        const diff = value - prevValue;
        const dir = Math.abs(diff) < 0.005 ? "" : diff > 0 ? "up" : "down";
        const good = dir && ((dir === "up") === upIsGood);
        delta = h("div", { class: "delta" }, h("span", { class: "badge " + (dir ? (good ? "down" : "up") : "") },
          h("span", { class: "arrow", "aria-hidden": "true", text: dir === "up" ? "▲" : dir === "down" ? "▼" : "•" }),
          label === "Savings rate" ? `${diff > 0 ? "+" : ""}${Math.round(diff)} pts` : signed(diff)),
          h("span", { text: `vs ${pv.label}${pv.partial ? " (partial data)" : ""}` }));
      }
      root.append(h("div", { class: "kpi" }, h("div", { class: "label", text: label }),
        h("div", { class: "value", text: value == null ? "—" : fmt(value) }), delta));
    };
    tile("Spent", sm.spend, pv.spend, false);
    tile("Came in", sm.income, pv.income, true);
    tile("Left over", sm.net, pv.net, true);
    tile("Savings rate", sm.savings_rate, pv.savings_rate, true, (v) => Math.round(v) + "%");
  }

  // ------------------------------------------------------------------ findings

  const SEV = { high: ["!", "Important"], medium: ["!", "Worth a look"], low: ["i", "For your information"], good: ["✓", "Good news"] };

  function evidenceTable(rows) {
    return h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Date" }), h("th", { text: "Account" }),
      h("th", { text: "Payee" }), h("th", { text: "Category" }), h("th", { class: "num", text: "Amount" }))),
      h("tbody", {}, rows.map((t) => h("tr", {}, h("td", { text: longDate(t.date) }), h("td", { text: t.account }),
        h("td", { text: t.payee }), h("td", { class: "muted", text: t.category }), h("td", { class: "num", text: money(t.amount) })))));
  }

  function renderFindings() {
    const list = clear($("finding-list"));
    const all = R.findings;
    const nDismissed = all.filter(isDismissed).length;
    $("dismissed-label").textContent = nDismissed ? `Show ${nDismissed} dismissed` : "Show dismissed";
    $("show-dismissed").parentElement.hidden = !nDismissed;
    const attention = all.filter((f) => !isDismissed(f) && (f.severity === "high" || f.severity === "medium")).length;
    $("findings-hint").textContent = all.length
      ? `${attention} need${attention === 1 ? "s" : ""} a look. Each finding lists the transactions behind it.`
      : "Nothing stood out.";
    const shown = all.filter((f) => (state.showDismissed || !isDismissed(f)) &&
      (state.filter === "all" || (state.filter === "good" ? f.severity === "good" : f.severity === "high" || f.severity === "medium")));
    if (!shown.length) {
      list.append(h("div", { class: "empty-state" }, h("div", { class: "big", text: "✓" }),
        h("p", { text: state.filter === "good" ? "No good-news items this period." : "Nothing needs your attention for this period." })));
      return;
    }
    for (const f of shown) {
      const [icon, label] = SEV[f.severity];
      const dismissed = isDismissed(f);
      const cmd = h("span", { class: "cmd", hidden: !local.has(f.id) || f.dismissed },
        "Hidden here. To hide it in future reports too: ", h("code", { text: `kete report dismiss ${f.id}` }));
      const card = h("article", { class: `finding ${f.severity}${dismissed ? " dismissed" : ""}`, "aria-label": f.title },
        h("span", { class: "sev", "aria-hidden": "true", text: icon }),
        h("div", { class: "head" }, h("strong", { text: f.title }), h("span", { class: "sev-label", text: label })),
        f.impact_yearly ? h("span", { class: "impact", text: `≈ ${money(f.impact_yearly, 0)}/yr` }) : h("span"),
        h("p", { class: "body", text: f.body }),
        f.suggestion ? h("p", { class: "tip", text: f.suggestion }) : null,
        f.evidence.length ? h("details", {}, h("summary", { text: `Transactions (${f.evidence.length})` }),
          h("div", { style: "overflow-x:auto" }, evidenceTable(f.evidence))) : null,
        h("div", { class: "actions" },
          f.dismissed ? h("span", { class: "cmd" }, "Dismissed. To bring it back: ", h("code", { text: `kete report restore ${f.id}` }))
            : h("button", { class: "btn", type: "button", text: local.has(f.id) ? "Undo dismiss" : "Dismiss",
              onclick: () => { local.has(f.id) ? local.delete(f.id) : local.add(f.id); saveLocal(); renderFindings(); } }),
          cmd));
      list.append(card);
    }
  }

  // ------------------------------------------------------------------ categories (diverging bars)

  function renderCategories() {
    const pv = R.previous, root = clear($("category-chart"));
    const cats = R.categories.filter((c) => c.spend || c.previous);
    $("categories-hint").textContent = pv.has_data
      ? `Spending by category compared with ${pv.label}. Red bars are more than last time, blue bars less.` +
        (pv.partial ? ` Your data starts partway through ${pv.label}, so increases look bigger than they are.` : "")
      : "Spending by category. No earlier period to compare with yet.";
    const legend = clear($("category-legend"));
    if (pv.has_data) legend.append(h("span", {}, h("i", { class: "key", style: "background:var(--div-more)" }), " More than " + pv.label),
      h("span", {}, h("i", { class: "key", style: "background:var(--div-less)" }), " Less"));
    if (!cats.length) { root.append(h("p", { class: "empty", text: "No spending in this period." })); return; }
    if (pv.has_data) {
      const rows = [...cats].sort((a, b) => Math.abs(b.change) - Math.abs(a.change));
      const max = Math.max(...rows.map((c) => Math.abs(c.change)), 1);
      rows.forEach((c, i) => {
        const w = (Math.abs(c.change) / max) * 50;
        const bar = c.change ? h("div", { class: `bar ${c.change > 0 ? "more" : "less"}${animate ? (c.change > 0 ? " grow-x" : " grow-l") : ""}`,
          style: `width:${w}%;animation-delay:${i * 30}ms` }) : null;
        const row = h("div", { class: "div-row", tabindex: 0 },
          h("div", { class: "name", title: c.name, text: c.name }),
          h("div", { class: "div-track" }, h("i", { class: "mid" }), bar),
          h("div", { class: "vals" }, money(c.spend, 0), h("span", { text: `${signed(c.change)}` })));
        const show = (e) => showTip(e.clientX != null ? e : { clientX: row.getBoundingClientRect().left + 200, clientY: row.getBoundingClientRect().top }, c.name, [
          { value: money(c.spend, 0), label: R.period.label }, { value: money(c.previous, 0), label: pv.label },
          { value: signed(c.change), label: "Change" }]);
        row.addEventListener("pointermove", show); row.addEventListener("focus", show);
        row.addEventListener("pointerleave", hideTip); row.addEventListener("blur", hideTip);
        root.append(row);
      });
    } else {
      const max = Math.max(...cats.map((c) => c.spend), 1);
      for (const c of cats) root.append(h("div", { class: "hbar" }, h("div", { class: "name", text: c.name }),
        h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(c.spend / max) * 100}%;background:var(--accent)` })),
        h("div", { class: "amt", text: money(c.spend, 0) })));
    }
    const tbl = clear($("category-table"));
    tbl.append(h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Category" }), h("th", { class: "num", text: R.period.label }),
      h("th", { class: "num", text: pv.has_data ? pv.label : "" }), h("th", { class: "num", text: pv.has_data ? "Change" : "" }))),
      h("tbody", {}, cats.map((c) => h("tr", {}, h("td", { text: c.name }), h("td", { class: "num", text: money(c.spend, 0) }),
        h("td", { class: "num", text: c.previous == null ? "" : money(c.previous, 0) }),
        h("td", { class: "num", text: c.change == null ? "" : signed(c.change) }))))));
  }

  // ------------------------------------------------------------------ months (year reports)

  function renderMonths() {
    if (!R.monthly.length) return;
    $("months").hidden = false; $("nav-months").hidden = false;
    const root = clear($("month-chart"));
    const W = Math.max(280, root.clientWidth || 800), H = 260, m = { t: 12, r: 8, b: 26, l: 48 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b, n = R.monthly.length, band = pw / n;
    const max = Math.max(...R.monthly.flatMap((x) => [x.income, x.spend]), 1);
    const step = Math.pow(10, Math.floor(Math.log10(max / 4)));
    const tickStep = [1, 2, 2.5, 5, 10].map((k) => k * step).find((t) => t >= max / 4);
    const top = Math.ceil(max / tickStep) * tickStep;
    const y = (v) => m.t + ph - (v / top) * ph;
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Monthly income and spending" });
    for (let t = 0; t <= top + 0.01; t += tickStep) {
      svg.append(s("line", { class: t ? "gridline" : "baseline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }),
        s("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", text: compact(t) }));
    }
    const bw = Math.min(20, (band - 10) / 2);
    R.monthly.forEach((mo, i) => {
      const cx = m.l + band * i + band / 2;
      const g = s("g", { class: "bar-group" });
      g.append(s("rect", { x: cx - band / 2, y: m.t, width: band, height: ph, fill: "transparent" }));
      [["income", "var(--s1)", -1], ["spend", "var(--s2)", 1]].forEach(([k, col, side]) => {
        const v = mo[k]; if (!v) return;
        const x0 = side < 0 ? cx - bw - 1 : cx + 1, t = y(v), r = Math.min(4, bw / 2, y(0) - t);
        g.append(s("path", { class: "bar" + (animate ? " grow" : ""), style: animate ? `animation-delay:${i * 35}ms` : null, fill: col,
          d: `M${x0},${y(0)}V${t + r}Q${x0},${t} ${x0 + r},${t}H${x0 + bw - r}Q${x0 + bw},${t} ${x0 + bw},${t + r}V${y(0)}Z` }));
      });
      g.addEventListener("pointermove", (e) => showTip(e, mo.label, [
        { value: money(mo.income, 0), label: "Income", color: "var(--s1)" }, { value: money(mo.spend, 0), label: "Spending", color: "var(--s2)" },
        { value: money(mo.net, 0), label: "Left over" }]));
      g.addEventListener("pointerleave", hideTip);
      svg.append(g, s("text", { x: cx, y: H - 8, "text-anchor": "middle", text: mo.label }));
    });
    root.append(svg);
    clear($("month-table")).append(h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Month" }),
      h("th", { class: "num", text: "Income" }), h("th", { class: "num", text: "Spending" }), h("th", { class: "num", text: "Left over" }))),
      h("tbody", {}, R.monthly.map((mo) => h("tr", {}, h("td", { text: mo.label }), h("td", { class: "num", text: money(mo.income, 0) }),
        h("td", { class: "num", text: money(mo.spend, 0) }), h("td", { class: "num", text: money(mo.net, 0) }))))));
  }

  function renderTax() {
    if (!R.tax) return;
    $("tax").hidden = false; $("nav-tax").hidden = false;
    const t = R.tax, root = clear($("tax-grid"));
    const tile = (label, v) => root.append(h("div", { class: "kpi" }, h("div", { class: "label", text: label }), h("div", { class: "value", text: v })));
    tile("Gross interest", money(t.gross, 2));
    tile("Tax withheld (RWT)", money(t.rwt, 2));
    tile("Paid into your accounts", money(t.net, 2));
    tile("Interest payments", String(t.payments));
  }

  function renderLargest() {
    const root = clear($("largest-table"));
    if (!R.largest.length) { root.append(h("p", { class: "empty", text: "No spending in this period." })); return; }
    root.append(h("div", { style: "overflow-x:auto" }, evidenceTable(R.largest)));
  }

  // ------------------------------------------------------------------ wiring

  function setup() {
    const root = document.documentElement;
    try { const t = localStorage.getItem("kete:theme"); if (t) root.setAttribute("data-theme", t); } catch { /* ignore */ }
    $("theme").addEventListener("click", () => {
      const dark = root.getAttribute("data-theme") === "dark" || (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
      root.setAttribute("data-theme", dark ? "light" : "dark");
      try { localStorage.setItem("kete:theme", dark ? "light" : "dark"); } catch { /* ignore */ }
    });
    $("print").addEventListener("click", () => window.print());
    const tabs = document.querySelectorAll("#finding-filter button");
    tabs.forEach((b) => b.addEventListener("click", () => {
      state.filter = b.dataset.filter;
      tabs.forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      renderFindings();
    }));
    $("show-dismissed").addEventListener("change", (e) => { state.showDismissed = e.target.checked; renderFindings(); });
  }

  renderHeader();
  setup();
  renderKpis();
  renderFindings();
  renderCategories();
  renderMonths();
  renderTax();
  renderLargest();
  animate = false;
})();
