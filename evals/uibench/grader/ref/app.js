/* 事故指挥台 —— 参考实现：5000 条离线数据、窗口化渲染、可分享视图、撤销重做。 */
(function () {
  "use strict";

  var SEVS = ["P1", "P2", "P3", "P4"];
  var SEV_RANK = { P1: 1, P2: 2, P3: 3, P4: 4 };
  var SERVICES = ["edge-gateway", "billing-api", "search-index", "auth-svc", "notify-worker", "ledger-db", "mobile-bff", "scheduler"];
  var STATUS_LABEL = { investigating: "处理中", acknowledged: "已确认", resolved: "已解决" };
  var TOTAL = 5000;
  var LS_KEY = "incident-console.view.v1";
  var XSS_TITLE = '<img src=x onerror="window.__xss=1">';

  /* ---------- 数据 ---------- */
  function mulberry32(a) {
    return function () {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  var rand = mulberry32(20261116);
  var SUBJECTS = ["连接池耗尽", "延迟毛刺", "写入放大", "证书即将过期", "磁盘水位告警", "队列积压", "鉴权超时", "心跳丢失", "分片倾斜", "重复消费", "缓存击穿", "批量任务重试"];
  var PLACES = ["华东一区", "华北二区", "华南三区", "海外节点", "灰度集群", "主备链路"];
  var records = [];
  var byId = new Map();
  var base = Date.parse("2026-11-01T00:00:00Z");
  for (var i = 0; i < TOTAL; i++) {
    var r0 = rand();
    var sev = r0 < 0.07 ? "P1" : r0 < 0.26 ? "P2" : r0 < 0.63 ? "P3" : "P4";
    var r1 = rand();
    var status = r1 < 0.46 ? "investigating" : r1 < 0.74 ? "acknowledged" : "resolved";
    var r2 = rand();
    var rec = {
      id: "INC-" + String(i + 1).padStart(6, "0"),
      severity: sev,
      service: SERVICES[Math.floor(r2 * SERVICES.length)],
      title: SUBJECTS[Math.floor(rand() * SUBJECTS.length)] + " · " + PLACES[Math.floor(rand() * PLACES.length)] + "（批次 " + ((i % 37) + 1) + "）",
      status: status,
      startedAt: new Date(base + i * 37 * 60 * 1000).toISOString(),
    };
    records.push(rec);
    byId.set(rec.id, rec);
  }
  records[1234].title = XSS_TITLE;

  /* ---------- 视图状态 ---------- */
  var view = { sev: new Set(SEVS), service: "", q: "", onlyOpen: false, from: "", to: "", sortKey: "startedAt", sortDir: "desc" };
  var selected = new Set();
  var cursorId = null;
  var undoStack = [];
  var redoStack = [];
  var filtered = [];
  var drawerId = null;
  var toastTimer = null;

  var $ = function (id) { return document.getElementById(id); };
  var tbody = $("tbody");
  var rowsEl = $("rows");
  var topSpacer = $("top-spacer");
  var bottomSpacer = $("bottom-spacer");
  var countEl = $("result-count");
  var selectAll = $("select-all");
  var drawer = $("drawer");
  var scrim = $("scrim");

  /* ---------- 视图序列化 ---------- */
  function viewToParams(v) {
    var p = new URLSearchParams();
    if (v.sev.size !== SEVS.length) p.set("sev", SEVS.filter(function (s) { return v.sev.has(s); }).join(","));
    if (v.service) p.set("svc", v.service);
    if (v.q) p.set("q", v.q);
    if (v.onlyOpen) p.set("open", "1");
    if (v.from) p.set("from", v.from);
    if (v.to) p.set("to", v.to);
    p.set("sort", v.sortKey + ":" + v.sortDir);
    return p.toString();
  }
  function paramsToView(qs) {
    var p = new URLSearchParams(qs);
    var v = { sev: new Set(SEVS), service: "", q: "", onlyOpen: false, from: "", to: "", sortKey: "startedAt", sortDir: "desc" };
    var sev = p.get("sev");
    if (sev !== null) v.sev = new Set(sev.split(",").filter(function (s) { return SEVS.indexOf(s) >= 0; }));
    if (p.get("svc")) v.service = p.get("svc");
    if (p.get("q")) v.q = p.get("q");
    if (p.get("open") === "1") v.onlyOpen = true;
    if (p.get("from")) v.from = p.get("from");
    if (p.get("to")) v.to = p.get("to");
    var s = p.get("sort");
    if (s && /^(id|severity|service|startedAt):(asc|desc)$/.test(s)) {
      v.sortKey = s.split(":")[0];
      v.sortDir = s.split(":")[1];
    }
    return v;
  }

  /* ---------- 过滤与排序 ---------- */
  function compare(a, b, key, dir) {
    var d;
    if (key === "severity") d = SEV_RANK[a.severity] - SEV_RANK[b.severity];
    else if (key === "startedAt") d = a.startedAt < b.startedAt ? -1 : a.startedAt > b.startedAt ? 1 : 0;
    else d = String(a[key]).localeCompare(String(b[key]));
    if (d === 0) d = a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    return dir === "asc" ? d : -d;
  }
  function filterAndSort(v) {
    var q = String(v.q || "").trim().toLowerCase();
    var out = [];
    for (var i = 0; i < records.length; i++) {
      var r = records[i];
      if (!v.sev.has(r.severity)) continue;
      if (v.service && r.service !== v.service) continue;
      if (v.onlyOpen && r.status === "resolved") continue;
      if (q && !(r.title.toLowerCase().indexOf(q) >= 0 || r.id.toLowerCase().indexOf(q) >= 0)) continue;
      if (v.from && r.startedAt.slice(0, 10) < v.from) continue;
      if (v.to && r.startedAt.slice(0, 10) > v.to) continue;
      out.push(r);
    }
    out.sort(function (a, b) { return compare(a, b, v.sortKey, v.sortDir); });
    return out;
  }

  /* ---------- 渲染 ---------- */
  function rowHeight() {
    var h = parseFloat(getComputedStyle(tbody).getPropertyValue("--row-h"));
    return h > 0 ? h : 46;
  }
  function fmtTime(iso) {
    var d = new Date(iso);
    var p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function span(cls, text) {
    var el = document.createElement("span");
    el.className = cls;
    el.textContent = text;
    return el;
  }
  function buildRow(r) {
    var row = document.createElement("div");
    row.className = "row";
    row.setAttribute("role", "row");
    row.setAttribute("data-row", "");
    row.dataset.id = r.id;
    row.dataset.status = r.status;
    row.tabIndex = r.id === cursorId ? 0 : -1;

    var check = document.createElement("span");
    check.className = "cell check";
    var box = document.createElement("input");
    box.type = "checkbox";
    box.setAttribute("data-pick", "");
    box.checked = selected.has(r.id);
    box.setAttribute("aria-label", "选择 " + r.id);
    check.appendChild(box);

    var sevCell = document.createElement("span");
    sevCell.className = "cell col-sev";
    var badge = document.createElement("span");
    badge.className = "sev-badge sev-" + r.severity;
    badge.textContent = r.severity;
    sevCell.appendChild(badge);

    var status = document.createElement("span");
    status.className = "cell col-status st-" + r.status;
    status.textContent = STATUS_LABEL[r.status];

    row.append(
      check,
      span("cell col-id", r.id),
      sevCell,
      span("cell col-svc", r.service),
      span("cell col-title", r.title),
      span("cell col-time", fmtTime(r.startedAt)),
      status
    );
    return row;
  }

  function renderRows() {
    var rowH = rowHeight();
    var tbodyTop = tbody.getBoundingClientRect().top + window.scrollY;
    var scrollTop = window.scrollY - tbodyTop;
    var overscan = 6;
    var start = Math.max(0, Math.floor(scrollTop / rowH) - overscan);
    var end = Math.min(filtered.length, Math.ceil((scrollTop + window.innerHeight) / rowH) + overscan);
    if (start > filtered.length) start = filtered.length;
    if (end - start > 180) end = start + 180;
    if (end < start) end = start;

    topSpacer.style.height = start * rowH + "px";
    bottomSpacer.style.height = (filtered.length - end) * rowH + "px";

    var frag = document.createDocumentFragment();
    for (var i = start; i < end; i++) frag.appendChild(buildRow(filtered[i]));
    if (!filtered.length) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "没有匹配的事故";
      empty.style.padding = "24px 12px";
      empty.style.color = "var(--muted)";
      frag.appendChild(empty);
    }
    rowsEl.replaceChildren(frag);
  }

  var rafPending = false;
  function scheduleRender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(function () {
      rafPending = false;
      renderRows();
    });
  }
  window.addEventListener("scroll", scheduleRender, { passive: true });
  window.addEventListener("resize", function () { renderRows(); });

  function updateSelectionUI() {
    var total = filtered.length;
    var selInView = 0;
    for (var i = 0; i < filtered.length; i++) if (selected.has(filtered[i].id)) selInView++;
    selectAll.checked = total > 0 && selInView === total;
    selectAll.indeterminate = selInView > 0 && selInView < total;
    $("selection-summary").textContent = selected.size ? "已选 " + selected.size + " 条" : "未选择";
    $("bulk-ack").disabled = !selected.size;
    $("bulk-resolve").disabled = !selected.size;
    $("clear-selection").disabled = !selected.size;
  }
  function updateSortMarks() {
    var arrows = document.querySelectorAll("[data-arrow]");
    for (var i = 0; i < arrows.length; i++) {
      var key = arrows[i].getAttribute("data-arrow");
      arrows[i].textContent = key === view.sortKey ? (view.sortDir === "asc" ? "↑" : "↓") : "";
    }
    var heads = document.querySelectorAll("[data-sort]");
    for (var j = 0; j < heads.length; j++) heads[j].classList.toggle("sorted", heads[j].getAttribute("data-sort") === view.sortKey);
  }

  function persist() {
    var qs = viewToParams(view);
    if (location.hash.slice(1) !== qs) history.replaceState(null, "", "#" + qs);
    try { localStorage.setItem(LS_KEY, qs); } catch (e) { /* 存储被禁用时视图照样工作 */ }
  }

  function refresh() {
    filtered = filterAndSort(view);
    var still = false;
    for (var i = 0; i < filtered.length; i++) if (filtered[i].id === cursorId) { still = true; break; }
    if (!still) cursorId = filtered.length ? filtered[0].id : null;
    countEl.textContent = filtered.length + " 条结果";
    renderRows();
    updateSelectionUI();
    updateSortMarks();
    persist();
  }

  /* ---------- 撤销 / 重做 ---------- */
  function applyStatus(ids, status) {
    var prev = new Map();
    ids.forEach(function (id) {
      var r = byId.get(id);
      if (!r || r.status === status) return;
      prev.set(id, r.status);
      r.status = status;
    });
    if (!prev.size) return false;
    undoStack.push({ status: status, prev: prev });
    if (undoStack.length > 100) undoStack.shift();
    redoStack.length = 0;
    return true;
  }
  function undo(silent) {
    var op = undoStack.pop();
    if (!op) return false;
    op.prev.forEach(function (st, id) { byId.get(id).status = st; });
    redoStack.push(op);
    if (!silent) {
      refresh();
      if (drawerId) fillDrawer(byId.get(drawerId));
      showToast("已撤销 " + op.prev.size + " 条状态变更", false);
    }
    return true;
  }
  function redo() {
    var op = redoStack.pop();
    if (!op) return false;
    op.prev.forEach(function (_st, id) { byId.get(id).status = op.status; });
    undoStack.push(op);
    refresh();
    if (drawerId) fillDrawer(byId.get(drawerId));
    showToast("已重做 " + op.prev.size + " 条状态变更", false);
    return true;
  }

  /* ---------- 提示 ---------- */
  function showToast(text, canUndo) {
    var toast = $("toast");
    $("toast-text").textContent = text;
    $("toast-undo").hidden = !canUndo;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 4000);
  }

  /* ---------- 抽屉 ---------- */
  function fillDrawer(r) {
    if (!r) return;
    var body = $("drawer-body");
    var rows = [
      ["编号", r.id],
      ["级别", r.severity],
      ["服务", r.service],
      ["标题", r.title],
      ["开始时间", fmtTime(r.startedAt)],
      ["状态", STATUS_LABEL[r.status]],
    ];
    body.replaceChildren();
    rows.forEach(function (pair) {
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      dd.textContent = pair[1];
      body.append(dt, dd);
    });
    $("act-ack").disabled = r.status === "acknowledged";
    $("act-resolve").disabled = r.status === "resolved";
  }
  function openDrawer(id) {
    var r = byId.get(id);
    if (!r) return;
    drawerId = id;
    cursorId = id;
    fillDrawer(r);
    drawer.hidden = false;
    scrim.hidden = false;
    document.body.classList.add("lock");
    requestAnimationFrame(function () {
      drawer.classList.add("open");
      $("drawer-close").focus();
    });
    renderRows();
  }
  function closeDrawer() {
    if (!drawerId) return;
    var id = drawerId;
    drawerId = null;
    drawer.classList.remove("open");
    document.body.classList.remove("lock");
    scrim.hidden = true;
    setTimeout(function () { if (!drawerId) drawer.hidden = true; }, 230);
    cursorId = id;
    var idx = -1;
    for (var i = 0; i < filtered.length; i++) if (filtered[i].id === id) { idx = i; break; }
    if (idx >= 0) ensureVisible(idx);
    renderRows();
    var el = rowsEl.querySelector('[data-id="' + id + '"]');
    if (el) el.focus();
  }
  function focusables() {
    return Array.prototype.filter.call(
      drawer.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"),
      function (el) { return !el.disabled && el.offsetParent !== null; }
    );
  }

  /* ---------- 交互绑定 ---------- */
  rowsEl.addEventListener("click", function (e) {
    var row = e.target.closest("[data-row]");
    if (!row) return;
    if (e.target.closest("[data-pick]")) return;
    openDrawer(row.dataset.id);
  });
  rowsEl.addEventListener("change", function (e) {
    if (!e.target.matches("[data-pick]")) return;
    var row = e.target.closest("[data-row]");
    if (e.target.checked) selected.add(row.dataset.id);
    else selected.delete(row.dataset.id);
    updateSelectionUI();
  });
  rowsEl.addEventListener("focusin", function (e) {
    var row = e.target.closest("[data-row]");
    if (!row || row.dataset.id === cursorId) return;
    cursorId = row.dataset.id;
    var all = rowsEl.querySelectorAll("[data-row]");
    for (var i = 0; i < all.length; i++) all[i].tabIndex = all[i] === row ? 0 : -1;
  });
  rowsEl.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      moveCursor(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter") {
      var row = e.target.closest("[data-row]");
      if (row) { e.preventDefault(); openDrawer(row.dataset.id); }
    }
  });
  function moveCursor(delta) {
    if (!filtered.length) return;
    var idx = -1;
    for (var i = 0; i < filtered.length; i++) if (filtered[i].id === cursorId) { idx = i; break; }
    if (idx < 0) idx = 0;
    idx = Math.max(0, Math.min(filtered.length - 1, idx + delta));
    cursorId = filtered[idx].id;
    ensureVisible(idx);
    renderRows();
    var el = rowsEl.querySelector('[data-id="' + cursorId + '"]');
    if (el) el.focus();
  }
  function ensureVisible(idx) {
    var rowH = rowHeight();
    var tbodyTop = tbody.getBoundingClientRect().top + window.scrollY;
    var top = tbodyTop + idx * rowH;
    if (top < window.scrollY + 90) window.scrollTo(0, Math.max(0, top - 120));
    else if (top + rowH > window.scrollY + window.innerHeight - 90) window.scrollTo(0, top - window.innerHeight + 160);
  }

  selectAll.addEventListener("change", function () {
    var all = filtered.length > 0;
    for (var i = 0; i < filtered.length && all; i++) if (!selected.has(filtered[i].id)) all = false;
    filtered.forEach(function (r) {
      if (all) selected.delete(r.id);
      else selected.add(r.id);
    });
    updateSelectionUI();
    renderRows();
  });
  $("clear-selection").addEventListener("click", function () {
    selected.clear();
    updateSelectionUI();
    renderRows();
  });
  function bulk(status) {
    var ids = Array.from(selected);
    if (!ids.length) return;
    if (!applyStatus(ids, status)) return;
    selected.clear();
    refresh();
    if (drawerId) fillDrawer(byId.get(drawerId));
    showToast("已更新 " + ids.length + " 条为「" + STATUS_LABEL[status] + "」", true);
  }
  $("bulk-ack").addEventListener("click", function () { bulk("acknowledged"); });
  $("bulk-resolve").addEventListener("click", function () { bulk("resolved"); });
  $("toast-undo").addEventListener("click", function () { undo(); });

  $("drawer-close").addEventListener("click", closeDrawer);
  scrim.addEventListener("click", closeDrawer);
  $("act-ack").addEventListener("click", function () {
    if (!drawerId) return;
    if (applyStatus([drawerId], "acknowledged")) {
      refresh();
      fillDrawer(byId.get(drawerId));
      showToast("已标记为已确认", true);
    }
  });
  $("act-resolve").addEventListener("click", function () {
    if (!drawerId) return;
    if (applyStatus([drawerId], "resolved")) {
      refresh();
      fillDrawer(byId.get(drawerId));
      showToast("已标记为已解决", true);
    }
  });
  drawer.addEventListener("keydown", function (e) {
    if (e.key !== "Tab") return;
    var list = focusables();
    if (!list.length) return;
    var first = list[0];
    var last = list[list.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && drawerId) {
      e.preventDefault();
      closeDrawer();
      return;
    }
    var mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    var k = String(e.key).toLowerCase();
    if (k === "z") {
      e.preventDefault();
      if (e.shiftKey) redo();
      else undo();
    } else if (k === "y") {
      e.preventDefault();
      redo();
    }
  });

  var searchTimer = null;
  $("search").addEventListener("input", function (e) {
    clearTimeout(searchTimer);
    var value = e.target.value;
    searchTimer = setTimeout(function () {
      view.q = value;
      refresh();
    }, 100);
  });
  document.querySelectorAll(".sev").forEach(function (box) {
    box.addEventListener("change", function () {
      document.querySelectorAll(".sev").forEach(function (b) {
        if (b.checked) view.sev.add(b.value);
        else view.sev.delete(b.value);
      });
      refresh();
    });
  });
  $("service-filter").addEventListener("change", function (e) {
    view.service = e.target.value;
    refresh();
  });
  $("only-open").addEventListener("change", function (e) {
    view.onlyOpen = e.target.checked;
    refresh();
  });
  $("adv-toggle").addEventListener("click", function () {
    var panel = $("advanced");
    panel.hidden = !panel.hidden;
    $("adv-toggle").setAttribute("aria-expanded", panel.hidden ? "false" : "true");
  });
  $("adv-from").addEventListener("change", function (e) { view.from = e.target.value; refresh(); });
  $("adv-to").addEventListener("change", function (e) { view.to = e.target.value; refresh(); });
  document.querySelectorAll("[data-sort]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = btn.getAttribute("data-sort");
      if (view.sortKey === key) view.sortDir = view.sortDir === "asc" ? "desc" : "asc";
      else {
        view.sortKey = key;
        view.sortDir = key === "startedAt" ? "desc" : "asc";
      }
      refresh();
    });
  });

  /* ---------- 自测 ---------- */
  function runSelfTest() {
    var results = [];
    var check = function (name, fn) {
      var ok = false;
      try { ok = !!fn(); } catch (err) { ok = false; }
      results.push([name, ok]);
    };
    check("排序：开始时间升序单调", function () {
      var v = Object.assign({}, view, { sev: new Set(SEVS), service: "", q: "", onlyOpen: false, from: "", to: "", sortKey: "startedAt", sortDir: "asc" });
      var list = filterAndSort(v);
      if (list.length !== TOTAL) return false;
      for (var i = 1; i < list.length; i++) if (list[i - 1].startedAt > list[i].startedAt) return false;
      return true;
    });
    check("筛选计数：仅 P1 为真子集", function () {
      var v = Object.assign({}, view, { sev: new Set(["P1"]), service: "", q: "", onlyOpen: false, from: "", to: "", sortKey: "startedAt", sortDir: "desc" });
      var n = filterAndSort(v).length;
      var manual = 0;
      for (var i = 0; i < records.length; i++) if (records[i].severity === "P1") manual++;
      return n === manual && n > 0 && n < TOTAL;
    });
    check("撤销：状态回滚", function () {
      var probe = records[records.length - 1];
      var before = probe.status;
      var target = before === "resolved" ? "acknowledged" : "resolved";
      if (!applyStatus([probe.id], target)) return false;
      var changed = probe.status === target;
      undo(true);
      redoStack.length = 0;
      return changed && probe.status === before;
    });
    check("URL 往返：视图等价", function () {
      var back = paramsToView(viewToParams(view));
      if (back.sortKey !== view.sortKey || back.sortDir !== view.sortDir) return false;
      if (back.q !== view.q || back.service !== view.service || back.onlyOpen !== view.onlyOpen) return false;
      if (back.from !== view.from || back.to !== view.to) return false;
      return SEVS.every(function (s) { return back.sev.has(s) === view.sev.has(s); });
    });
    var pass = results.every(function (r) { return r[1]; });
    document.documentElement.setAttribute("data-selftest", pass ? "pass" : "fail");
    var box = $("selftest-result");
    box.hidden = false;
    box.textContent = (pass ? "自测通过" : "自测失败") + "\n" + results.map(function (r) { return (r[1] ? "✓ " : "✗ ") + r[0]; }).join("\n");
    return pass;
  }

  /* ---------- 初始化 ---------- */
  function syncControls() {
    document.querySelectorAll(".sev").forEach(function (b) { b.checked = view.sev.has(b.value); });
    $("service-filter").value = view.service;
    $("search").value = view.q;
    $("only-open").checked = view.onlyOpen;
    $("adv-from").value = view.from;
    $("adv-to").value = view.to;
  }

  var select = $("service-filter");
  SERVICES.forEach(function (s) {
    var o = document.createElement("option");
    o.value = s;
    o.textContent = s;
    select.appendChild(o);
  });

  var boot = location.hash.slice(1);
  if (!boot) {
    try { boot = localStorage.getItem(LS_KEY) || ""; } catch (e) { boot = ""; }
  }
  if (boot) view = paramsToView(boot);
  cursorId = null;
  refresh();
  syncControls();

  if (/[?&]selftest=1(?:&|$)/.test(location.search)) {
    runSelfTest();
    refresh();
  }
})();
