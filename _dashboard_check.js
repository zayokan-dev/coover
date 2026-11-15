
    (() => {
      "use strict";
      const $ = (selector) => document.querySelector(selector);
      const number = new Intl.NumberFormat("de-DE");
      const compact = new Intl.NumberFormat("de-DE", { notation: "compact", maximumFractionDigits: 1 });
      const dateFormat = new Intl.DateTimeFormat("de-DE", { month: "short", year: "2-digit" });
      const state = { page: 1, pageSize: 30, total: 0, topics: [], datasets: [], facets: null, analyticsRequest: null, recordsRequest: null, moreRequest: null, analyticsLoaded: false, activeDataset: "all", dateMin: null, dateMax: null, dateDataset: null, importing: false };
      const widgetDefinitions = [
        { id: "sentiment", title: "Pro und Contra" },
        { id: "metrics", title: "Kennzahlen" },
        { id: "timeline", title: "Resonanz im Zeitverlauf" },
        { id: "terms", title: "Argumente & Begriffsmuster" },
        { id: "comparison", title: "Projekt- und Städtevergleich" },
        { id: "articles", title: "Medienbeiträge" },
        { id: "import", title: "JSON- und CSV-Import" }
      ];
      const defaultWidgetOrder = widgetDefinitions.map((widget) => widget.id);
      const widgetOrderSlots = [2, 9, 10, 11, 12, 13, 14];
      const widgetStorageKey = "medienmonitor-widget-layout";
      const widgetLayout = { order: [...defaultWidgetOrder], hidden: new Set(), feedback: "", feedbackKind: "" };
      const labels = [
        { key: "positive", name: "Dafür · positiv", css: "pos", color: "#5b9c77" },
        { key: "negative", name: "Dagegen · negativ", css: "neg", color: "#d27564" },
        { key: "neutral", name: "Neutral", css: "neu", color: "#a2aaa5" },
        { key: "unknown", name: "Unentschlossen", css: "unknown", color: "#d2a44c" }
      ];

      function escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
      }

      function showMessage(text, kind) {
        const element = $("#message");
        element.textContent = text;
        element.className = `message visible ${kind || "error"}`;
      }

      function clearMessage() {
        $("#message").className = "message";
        $("#message").textContent = "";
      }

      function loadWidgetLayout() {
        try {
          const saved = localStorage.getItem(widgetStorageKey);
          if (!saved) return;
          const parsed = JSON.parse(saved);
          if (!Array.isArray(parsed.order) || !Array.isArray(parsed.hidden)) {
            throw new Error("Das gespeicherte Layout hat ein ungültiges Format.");
          }
          const validIds = new Set(defaultWidgetOrder);
          const migrateId = (id) => id === "sources" ? "import" : id;
          const order = [...new Set(parsed.order.map(migrateId).filter((id) => validIds.has(id)))];
          widgetLayout.order = [...order, ...defaultWidgetOrder.filter((id) => !order.includes(id))];
          widgetLayout.hidden = new Set(parsed.hidden.map(migrateId).filter((id) => validIds.has(id)));
          if (parsed.order.includes("sources") || parsed.hidden.includes("sources")) saveWidgetLayout();
        } catch (error) {
          widgetLayout.feedback = `Einstellungen konnten nicht geladen werden: ${error.message}`;
          widgetLayout.feedbackKind = "error";
          showMessage(`Widget-Einstellungen konnten nicht geladen werden: ${error.message}`, "error");
        }
      }

      function saveWidgetLayout() {
        try {
          localStorage.setItem(widgetStorageKey, JSON.stringify({
            order: widgetLayout.order,
            hidden: [...widgetLayout.hidden]
          }));
          widgetLayout.feedback = "Layoutänderungen werden in diesem Browser gespeichert.";
          widgetLayout.feedbackKind = "";
          $("#widget-layout-feedback").textContent = widgetLayout.feedback;
          $("#widget-layout-feedback").className = "widget-feedback";
          return true;
        } catch (error) {
          widgetLayout.feedback = `Layout konnte nicht gespeichert werden: ${error.message}`;
          widgetLayout.feedbackKind = "error";
          $("#widget-layout-feedback").textContent = widgetLayout.feedback;
          $("#widget-layout-feedback").className = "widget-feedback error";
          showMessage(`Widget-Einstellungen konnten nicht gespeichert werden: ${error.message}`, "error");
          return false;
        }
      }

      function applyWidgetLayout() {
        widgetLayout.order.forEach((id, index) => {
          const element = $(`[data-widget="${id}"]`);
          element.style.order = String(widgetOrderSlots[index]);
          element.hidden = widgetLayout.hidden.has(id);
          element.dataset.firstWidget = String(index === 0);
        });
      }

      function renderWidgetList() {
        $("#widget-layout-feedback").textContent = widgetLayout.feedback;
        $("#widget-layout-feedback").className = `widget-feedback${widgetLayout.feedbackKind ? ` ${widgetLayout.feedbackKind}` : ""}`;
        $("#widget-list").innerHTML = widgetLayout.order.map((id, index) => {
          const widget = widgetDefinitions.find((item) => item.id === id);
          return `<li class="widget-row" draggable="true" data-widget-item="${id}">
            <span class="widget-drag-handle" aria-hidden="true" title="Ziehen zum Verschieben">⋮⋮</span>
            <span class="widget-row-title">${escapeHtml(widget.title)}</span>
            <span class="widget-row-actions">
              <button type="button" data-widget-move="up" data-widget-id="${id}" aria-label="${escapeHtml(widget.title)} nach oben" title="Nach oben" ${index === 0 ? "disabled" : ""}>↑</button>
              <button type="button" data-widget-move="down" data-widget-id="${id}" aria-label="${escapeHtml(widget.title)} nach unten" title="Nach unten" ${index === widgetLayout.order.length - 1 ? "disabled" : ""}>↓</button>
              <label class="widget-visibility"><input type="checkbox" data-widget-visible="${id}" ${widgetLayout.hidden.has(id) ? "" : "checked"}><span>Anzeigen</span></label>
            </span>
          </li>`;
        }).join("");
      }

      function syncWidgetOrder() {
        widgetLayout.order = [...$("#widget-list").querySelectorAll("[data-widget-item]")]
          .map((item) => item.dataset.widgetItem);
        applyWidgetLayout();
        saveWidgetLayout();
        renderWidgetList();
        $("#widget-layout-status").textContent = "Widget-Reihenfolge gespeichert.";
      }

      function moveWidget(id, offset) {
        const index = widgetLayout.order.indexOf(id);
        const target = index + offset;
        if (index < 0 || target < 0 || target >= widgetLayout.order.length) return;
        const [widget] = widgetLayout.order.splice(index, 1);
        widgetLayout.order.splice(target, 0, widget);
        renderWidgetList();
        syncWidgetOrder();
      }

      function initializeWidgetManager() {
        loadWidgetLayout();
        applyWidgetLayout();
        const dialog = $("#widget-dialog");
        const list = $("#widget-list");
        $("#manage-widgets").addEventListener("click", () => {
          renderWidgetList();
          dialog.showModal();
        });
        $("#widget-reset").addEventListener("click", () => {
          widgetLayout.order = [...defaultWidgetOrder];
          widgetLayout.hidden.clear();
          applyWidgetLayout();
          saveWidgetLayout();
          renderWidgetList();
          $("#widget-layout-status").textContent = "Standard-Widgetlayout wiederhergestellt.";
        });
        list.addEventListener("change", (event) => {
          const checkbox = event.target.closest("[data-widget-visible]");
          if (!checkbox) return;
          if (checkbox.checked) widgetLayout.hidden.delete(checkbox.dataset.widgetVisible);
          else widgetLayout.hidden.add(checkbox.dataset.widgetVisible);
          applyWidgetLayout();
          saveWidgetLayout();
          $("#widget-layout-status").textContent = `${checkbox.closest("[data-widget-item]").querySelector(".widget-row-title").textContent} ${checkbox.checked ? "eingeblendet" : "ausgeblendet"}.`;
        });
        list.addEventListener("click", (event) => {
          const button = event.target.closest("[data-widget-move]");
          if (button) moveWidget(button.dataset.widgetId, button.dataset.widgetMove === "up" ? -1 : 1);
        });
        list.addEventListener("dragstart", (event) => {
          const item = event.target.closest("[data-widget-item]");
          if (!item || event.target.closest("button, input")) {
            event.preventDefault();
            return;
          }
          item.classList.add("dragging");
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", item.dataset.widgetItem);
        });
        list.addEventListener("dragover", (event) => {
          const target = event.target.closest("[data-widget-item]");
          const dragging = list.querySelector(".dragging");
          if (!target || !dragging || target === dragging) return;
          event.preventDefault();
          const insertAfter = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
          list.insertBefore(dragging, insertAfter ? target.nextElementSibling : target);
        });
        list.addEventListener("dragend", () => {
          const dragging = list.querySelector(".dragging");
          if (!dragging) return;
          dragging.classList.remove("dragging");
          syncWidgetOrder();
        });
      }

      async function api(url, options) {
        const response = await fetch(url, options);
        const payload = (response.headers.get("content-type") || "").includes("application/json") ? await response.json() : null;
        if (!response.ok) throw new Error(payload?.error || `Backend antwortete mit HTTP ${response.status}.`);
        return payload;
      }

      function dayNumber(value) {
        const time = Date.parse(`${String(value || "").slice(0, 10)}T00:00:00Z`);
        return Number.isNaN(time) ? null : Math.floor(time / 86400000);
      }

      function dayText(day) {
        return new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" })
          .format(new Date(day * 86400000));
      }

      function updateDateRange(reset = false, activeHandle = "") {
        const start = $("#date-start");
        const end = $("#date-end");
        const unavailable = state.dateMin === null || state.dateMax === null;
        start.disabled = unavailable || state.dateMin === state.dateMax;
        end.disabled = unavailable || state.dateMin === state.dateMax;
        if (unavailable) {
          $("#date-start-label").textContent = "Keine Datumsangaben";
          $("#date-end-label").textContent = "Keine Datumsangaben";
          start.min = start.max = start.value = "0";
          end.min = end.max = end.value = "0";
          return;
        }
        const datasetChanged = state.dateDataset !== $("#dataset-filter").value;
        start.min = end.min = String(state.dateMin);
        start.max = end.max = String(state.dateMax);
        const startValue = reset || datasetChanged ? state.dateMin : Math.max(state.dateMin, Math.min(state.dateMax, Number(start.value)));
        const endValue = reset || datasetChanged ? state.dateMax : Math.max(startValue, Math.min(state.dateMax, Number(end.value)));
        start.value = String(startValue);
        end.value = String(endValue);
        state.dateDataset = $("#dataset-filter").value;
        $("#date-start-label").textContent = dayText(startValue);
        $("#date-end-label").textContent = dayText(endValue);
        const span = Math.max(1, state.dateMax - state.dateMin);
        const left = (startValue - state.dateMin) / span * 100;
        const width = (endValue - startValue) / span * 100;
        $(".dual-range").style.setProperty("--range-left", `${left}%`);
        $(".dual-range").style.setProperty("--range-width", `${width}%`);
        if (activeHandle) {
          start.style.zIndex = activeHandle === "date-start" ? "2" : "1";
          end.style.zIndex = activeHandle === "date-end" ? "2" : "1";
        } else if (reset || datasetChanged) {
          start.style.zIndex = "1";
          end.style.zIndex = "2";
        }
      }

      function filterParams(overrides) {
        const startDay = state.dateMin === null ? null : Number($("#date-start").value);
        const endDay = state.dateMax === null ? null : Number($("#date-end").value);
        const fullRange = startDay === null || endDay === null ||
          (startDay <= state.dateMin && endDay >= state.dateMax);
        const params = new URLSearchParams();
        const values = {
          dataset_id: $("#dataset-filter").value,
          q: $("#topic-filter").value.trim(),
          city: $("#city-filter").value,
          project: $("#project-filter").value,
          sentiment: $("#sentiment-filter").value,
          date_from: fullRange ? "" : new Date(startDay * 86400000).toISOString().slice(0, 10),
          date_to: fullRange ? "" : new Date(endDay * 86400000).toISOString().slice(0, 10),
          ...overrides
        };
        for (const [key, value] of Object.entries(values)) if (value) params.set(key, value);
        const daySpan = fullRange ? state.dateMax - state.dateMin : endDay - startDay;
        params.set("bucket", daySpan <= 90 ? "day" : daySpan <= 365 ? "week" : "month");
        return params;
      }

      function recordParams() {
        const params = filterParams();
        const query = $("#record-search").value.trim();
        if (query) params.set("record_q", query);
        if ($("#record-type").value) params.set("record_media_type", $("#record-type").value);
        if ($("#record-sentiment").value) params.set("record_sentiment", $("#record-sentiment").value);
        const [sort, order] = $("#record-sort").value.split(":");
        params.set("sort", sort);
        params.set("order", order);
        return params;
      }

      function updateDatasetActions() {
        const datasetId = $("#dataset-filter").value;
        $("#delete-dataset").disabled = datasetId === "all";
      }

      async function loadDatasets(preferredId) {
        const result = await api("/api/datasets");
        state.datasets = result.items;
        const select = $("#dataset-filter");
        const current = preferredId || select.value || "all";
        select.innerHTML = '<option value="all">Alle Datensätze</option>' + result.items.map((item) =>
          `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${number.format(item.record_count)}</option>`
        ).join("");
        select.value = result.items.some((item) => item.id === current) ? current : "all";
        state.activeDataset = select.value;
        $("#tag-assignment").hidden = select.value === "all";
        updateDatasetActions();
      }

      async function loadFacets() {
        const params = new URLSearchParams();
        if ($("#dataset-filter").value !== "all") params.set("dataset_id", $("#dataset-filter").value);
        state.facets = await api(`/api/facets?${params}`);
        state.dateMin = dayNumber(state.facets.date_min);
        state.dateMax = dayNumber(state.facets.date_max);
        updateDateRange();
        const values = [
          ["city-filter", "Alle Städte", state.facets.cities],
          ["project-filter", "Alle Projekte", state.facets.projects],
          ["record-type", "Alle Medientypen", state.facets.media_types]
        ];
        for (const [id, placeholder, options] of values) {
          const select = $(`#${id}`);
          const current = select.value;
          select.innerHTML = `<option value="">${placeholder}</option>` + options.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
          select.value = options.includes(current) ? current : "";
        }
        const missing = [];
        if (!state.facets.cities.length) missing.push("Stadt");
        if (!state.facets.projects.length) missing.push("Projekt");
        $("#facet-hint").hidden = missing.length === 0;
        $("#facet-hint").textContent = missing.length
          ? `Keine ${missing.join(" und ")}-Zuordnungen in den Daten. Wähle einen einzelnen Datensatz aus, um fehlende Angaben zu ergänzen.`
          : "";
        $("#tag-assignment").hidden = $("#dataset-filter").value === "all";
        if (missing.length && !$("#tag-assignment").hidden) $("#tag-assignment").open = true;
        updateCompareOptions();
      }

      function updateCompareOptions() {
        const kind = $("#compare-kind").value;
        const options = kind === "city" ? (state.facets?.cities || []) : (state.facets?.projects || []);
        for (const id of ["compare-a", "compare-b"]) {
          const select = $(`#${id}`);
          const current = select.value;
          select.innerHTML = `<option value="">${options.length ? "Bitte auswählen" : kind === "city" ? "Keine Städte gefunden" : "Keine Projekte gefunden"}</option>` + options.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
          select.value = options.includes(current) ? current : "";
        }
      }

      function sentimentKey(value) {
        const n = Number(value);
        return n === 1 ? "positive" : n === -1 ? "negative" : n === 0 ? "neutral" : "unknown";
      }

      function sparkline(values) {
        if (!values.length) return "";
        const max = Math.max(1, ...values);
        const points = values.map((value, index) => {
          const x = values.length === 1 ? 0 : index / (values.length - 1) * 86;
          const y = 16 - value / max * 14;
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(" ");
        return `<svg viewBox="0 0 86 18" aria-hidden="true"><polyline points="${points}"></polyline></svg>`;
      }

      function renderMetrics(data) {
        const counts = data.counts;
        const classified = data.total - counts.unknown;
        const empty = data.total === 0;
        document.querySelectorAll(".metric-card").forEach((card) => { card.hidden = empty; });
        const hasData = state.datasets.some((dataset) => dataset.record_count > 0);
        $("#metrics").hidden = empty && !hasData;
        $("#metrics-empty").hidden = !empty || !hasData;
        if (empty) {
          $("#metrics-empty-title").textContent = "Keine Beiträge in dieser Auswahl";
          $("#metrics-empty-copy").textContent = "Passe die Filter an oder setze sie zurück, um wieder Ergebnisse zu sehen.";
          $("#metrics-empty-action").textContent = "Filter zurücksetzen";
          $("#sentiment-list").innerHTML = '<div class="empty-note">Noch keine Beiträge für die aktuelle Auswahl.</div>';
          return;
        }
        $("#metric-total").textContent = number.format(data.total);
        $("#metric-total-foot").textContent = `${number.format(data.sources)} Quellen · ${number.format(counts.unknown)} unklassifiziert`;
        $("#metric-positive").textContent = `${classified ? Math.round(counts.positive / classified * 100) : 0}%`;
        $("#metric-positive-foot").textContent = `${number.format(counts.positive)} positive Beiträge`;
        $("#metric-negative").textContent = `${classified ? Math.round(counts.negative / classified * 100) : 0}%`;
        $("#metric-negative-foot").textContent = `${number.format(counts.negative)} negative Beiträge`;
        $("#metric-reach").textContent = data.reach_count ? compact.format(data.reach) : "0";
        $("#metric-reach-foot").textContent = `${number.format(data.reach_count)} von ${number.format(data.total)} Beiträgen mit Reichweitenwert`;
        const dated = data.series.slice(-18);
        $("#trend-total").innerHTML = sparkline(dated.map((item) => item.positive + item.negative));
        $("#trend-positive").innerHTML = sparkline(dated.map((item) => item.positive));
        $("#trend-negative").innerHTML = sparkline(dated.map((item) => item.negative));
        $("#trend-reach").innerHTML = `<span class="coverage-track" title="Anteil der Beiträge mit Reichweitenwert"><span style="width:${Math.min(100, data.reach_count / data.total * 100)}%"></span></span>`;
        const max = Math.max(1, ...Object.values(counts));
        const sentimentValues = { positive: "1", negative: "-1", neutral: "0", unknown: "-99" };
        $("#sentiment-list").innerHTML = labels.map((item) => {
          const count = counts[item.key] || 0;
          const value = sentimentValues[item.key];
          return `<button class="sentiment-row" type="button" data-tone="${value}" aria-pressed="${$("#sentiment-filter").value === value}"><span class="sentiment-label"><i class="legend-dot" style="background:${item.color}"></i>${item.name}</span><span class="bar-track"><span class="bar-fill ${item.css}" style="display:block;width:${count ? Math.max(2, count / max * 100) : 0}%"></span></span><span class="sentiment-count">${number.format(count)}</span></button>`;
        }).join("");
        document.querySelectorAll("[data-tone]").forEach((button) => {
          button.addEventListener("click", () => {
            const filter = $("#sentiment-filter");
            filter.value = filter.value === button.dataset.tone ? "" : button.dataset.tone;
            state.page = 1;
            refresh();
          });
        });
      }

      function renderChart(series) {
        const width = 720, height = 210, left = 38, right = 10, top = 14, bottom = 27;
        const chartWidth = width - left - right, chartHeight = height - top - bottom;
        const data = series.slice(-48);
        if (!data.some((item) => item.positive || item.negative)) {
          $("#chart-wrap").innerHTML = '<div class="empty-note">Noch keine zeitlichen Daten in dieser Auswahl.</div>';
          return;
        }
        const peak = Math.max(1, ...data.flatMap((item) => [item.positive, item.negative]));
        const xStep = data.length > 1 ? chartWidth / (data.length - 1) : 0;
        const path = (key) => data.map((item, index) => {
          const x = left + index * xStep;
          const y = top + chartHeight - item[key] / peak * chartHeight;
          return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(" ");
        const grid = [0,.5,1].map((fraction) => {
          const y = top + chartHeight - chartHeight * fraction;
          return `<g><line x1="${left}" y1="${y}" x2="${width-right}" y2="${y}" stroke="#edf0ec"/><text x="${left-8}" y="${y+3}" fill="#9aa39d" font-size="9" text-anchor="end">${Math.round(peak*fraction)}</text></g>`;
        }).join("");
        const indexList = data.length > 2 ? [...new Set([0, Math.floor((data.length-1)/2), data.length-1])] : data.map((_,i)=>i);
        const xLabels = indexList.map((index) => {
          const period = data[index].month;
          const date = new Date(period.length === 7 ? `${period}-15T12:00:00Z` : `${period}T12:00:00Z`);
          const label = period.length === 7 ? dateFormat.format(date) : new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "short" }).format(date);
          return `<text x="${left+index*xStep}" y="${height-4}" fill="#929d95" font-size="9" text-anchor="${index===0?"start":index===data.length-1?"end":"middle"}">${escapeHtml(label)}</text>`;
        }).join("");
        $("#chart-wrap").innerHTML = data.length
          ? `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Positive und negative Beiträge im Zeitverlauf">${grid}${xLabels}<path d="${path("positive")}" fill="none" stroke="#5b9c77" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/><path d="${path("negative")}" fill="none" stroke="#d27564" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`
          : '<div class="empty-note">Keine datierten Beiträge für die aktuelle Auswahl.</div>';
      }

      function renderTerms(data) {
        for (const [key, element] of [["positive", "#positive-terms"], ["negative", "#negative-terms"]]) {
          const terms = data.terms[key] || [];
          $(element).innerHTML = terms.length ? terms.map((item) => `<button class="arg-chip" type="button" data-term="${escapeHtml(item.term)}">${escapeHtml(item.term)} <span>${number.format(item.count)}</span></button>`).join("") : '<span class="argument-empty">Noch keine Begriffe in dieser Auswahl.</span>';
        }
        $("#argument-topic").textContent = $("#topic-filter").value.trim() || "Alle Themen";
        document.querySelectorAll("[data-term]").forEach((button) => {
          button.addEventListener("click", () => {
            $("#topic-filter").value = button.dataset.term;
            state.page = 1;
            refresh();
          });
        });
      }

      function renderComparison(first, second, firstName, secondName) {
        const target = $("#comparison-grid");
        if (!first || !second) {
          if (!state.datasets.some((dataset) => dataset.record_count > 0)) {
            target.innerHTML = '<div class="empty-note">Vergleichsdaten erscheinen, sobald Datensätze importiert wurden.</div>';
            return;
          }
          const values = $("#compare-kind").value === "city" ? state.facets?.cities : state.facets?.projects;
          target.innerHTML = values?.length
            ? '<div class="empty-note">Wähle zwei Einträge aus, um sie zu vergleichen.</div>'
            : '<div class="empty-note">Für diesen Datensatz sind keine Stadt- oder Projektzuordnungen vorhanden. Ergänze fehlende Angaben über „Stadt oder Projekt ergänzen“.</div>';
          return;
        }
        const cards = [[first, firstName], [second, secondName]];
        target.innerHTML = cards.map(([data, name]) => {
          const counts = data.counts;
          const total = data.total || 0;
          const percentage = (key) => total ? (counts[key] / total * 100) : 0;
          return `<div><div class="compare-label"><span>${escapeHtml(name)}</span><span>${number.format(total)} Beiträge</span></div><div class="compare-bar" aria-label="${escapeHtml(name)}: Sentimentverteilung"><span class="compare-positive" style="width:${percentage("positive")}%"></span><span class="compare-negative" style="width:${percentage("negative")}%"></span><span class="compare-neutral" style="width:${percentage("neutral")}%"></span></div><div class="compare-legend"><span>Positiv ${Math.round(percentage("positive"))}%</span><span>Negativ ${Math.round(percentage("negative"))}%</span><span>Neutral ${Math.round(percentage("neutral"))}%</span><span>Nicht klassifiziert ${Math.round(percentage("unknown"))}%</span></div></div>`;
        }).join("");
      }

      function renderArticle(row) {
        const tone = sentimentKey(row.sentiment);
        const label = labels.find((item) => item.key === tone);
        const snippet = (row.content || "").replace(/\s+/g, " ").trim().slice(0, 280);
        let date = "Datum unbekannt";
        if (row.published_at) {
          const parsed = new Date(row.published_at);
          date = Number.isNaN(parsed.getTime()) ? row.published_at : new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "short", year: "numeric" }).format(parsed);
        }
        let safeLink = "";
        try {
          const url = new URL(row.url);
          if (url.protocol === "https:" || url.protocol === "http:") safeLink = url.href;
        } catch {}
        return `<article class="article"><div class="article-main"><span class="article-accent ${label.css}"></span><div class="article-content"><div class="article-meta"><span>${escapeHtml(date)}</span><span>${escapeHtml(row.source || "Quelle unbekannt")}</span><span class="article-type">${escapeHtml(row.media_type)}</span>${row.city ? `<button class="metadata-filter" type="button" data-city-filter="${escapeHtml(row.city)}">${escapeHtml(row.city)}</button>` : ""}${row.project ? `<button class="metadata-filter" type="button" data-project-filter="${escapeHtml(row.project)}">· ${escapeHtml(row.project)}</button>` : ""}</div><h3 class="article-title">${escapeHtml(row.title || "Ohne Titel")}</h3><p class="article-snippet">${escapeHtml(snippet || "Kein Beitragstext vorhanden.")}</p></div><span class="article-badge ${label.css}">${label.name}</span></div><details class="article-detail"><summary>Beitrag und Metadaten ansehen</summary><p class="article-full">${escapeHtml(row.content || "Kein Beitragstext vorhanden.")}</p>${safeLink ? `<a class="article-link" href="${escapeHtml(safeLink)}" target="_blank" rel="noopener noreferrer">Originalbeitrag öffnen ↗</a>` : ""}<p class="article-meta">Reichweite: ${row.reach ? number.format(row.reach) : "k. A."} · Interaktionen: ${number.format(row.engagements)} · Sprache: ${escapeHtml(row.language || "k. A.")} · ${escapeHtml(row.dataset_name)}</p></details></article>`;
      }

      function renderArticles(result) {
        state.total = result.total;
        $("#results-count").textContent = number.format(result.total);
        const sortLabel = $("#record-sort").selectedOptions[0].textContent;
        $("#results-caption").textContent = `${result.total ? `Seite ${result.page} · ` : ""}${sortLabel}`;
        $("#articles").innerHTML = result.items.length ? result.items.map(renderArticle).join("") : '<div class="empty">Keine Beiträge für diese Auswahl. Passe die Filter an oder setze sie zurück.</div>';
        $("#show-more").hidden = result.page * result.page_size >= result.total;
      }

      async function refreshAnalytics() {
        if (state.analyticsRequest) state.analyticsRequest.abort();
        const controller = new AbortController();
        state.analyticsRequest = controller;
        clearMessage();
        if (!state.analyticsLoaded) {
          for (const id of ["metric-total", "metric-positive", "metric-negative", "metric-reach"]) $(`#${id}`).textContent = "…";
          $("#sentiment-list").innerHTML = '<div class="empty-note">Kennzahlen werden berechnet …</div>';
          $("#chart-wrap").setAttribute("aria-busy", "true");
          $("#chart-wrap").innerHTML = '<div class="empty-note">Zeitreihe wird geladen …</div>';
          $("#positive-terms").innerHTML = '<span class="argument-empty">Begriffsmuster werden ausgewertet …</span>';
          $("#negative-terms").innerHTML = '<span class="argument-empty">Begriffsmuster werden ausgewertet …</span>';
        }
        try {
          const params = filterParams();
          const analytics = await api(`/api/analytics?${params}`, { signal: controller.signal });
          if (controller.signal.aborted) return;
          renderMetrics(analytics);
          renderChart(analytics.series);
          renderTerms(analytics);
          state.analyticsLoaded = true;
          $("#chart-wrap").removeAttribute("aria-busy");
          const termScope = analytics.terms_sampled
            ? `den bis zu ${number.format(analytics.terms_sample_size)} neuesten Beiträgen je Stimmung`
            : "allen Beiträgen";
          $("#argument-subtitle").textContent = `Häufige Begriffe aus ${termScope} · keine geprüften Argumente · Klick filtert die Liste`;
          $("#chart-subtitle").textContent = analytics.date_min && analytics.date_max
            ? `${analytics.date_min.slice(0,7)} bis ${analytics.date_max.slice(0,7)} · positive und negative Beiträge`
            : "Noch keine datierten Beiträge in dieser Auswahl";
          const topics = analytics.terms.positive.concat(analytics.terms.negative).reduce((map, item) => {
            map.set(item.term, (map.get(item.term) || 0) + item.count);
            return map;
          }, new Map());
          state.topics = [...topics.entries()].sort((a,b)=>b[1]-a[1]).slice(0,10);
          $("#topic-suggestions").innerHTML = state.topics.length ? `<span class="suggestion-label">Häufige Begriffe</span>${state.topics.map(([term])=>`<button class="topic-chip${$("#topic-filter").value.trim().toLocaleLowerCase("de")===term?" active":""}" type="button" data-topic="${escapeHtml(term)}">${escapeHtml(term)}</button>`).join("")}` : "";
          document.querySelectorAll("[data-topic]").forEach((button)=>button.addEventListener("click",()=>{$("#topic-filter").value=button.dataset.topic;state.page=1;refresh();}));
          await refreshComparison(params, controller.signal);
        } catch (error) {
          if (error.name !== "AbortError") {
            $("#chart-wrap").removeAttribute("aria-busy");
            showMessage(`Analyse konnte nicht geladen werden: ${error.message}`, "error");
          }
        }
      }

      async function refreshRecords() {
        if (state.recordsRequest) state.recordsRequest.abort();
        if (state.moreRequest) state.moreRequest.abort();
        const controller = new AbortController();
        state.recordsRequest = controller;
        clearMessage();
        try {
          const params = recordParams();
          params.set("page", String(state.page));
          params.set("page_size", String(state.pageSize));
          const records = await api(`/api/records?${params}`, { signal: controller.signal });
          if (!controller.signal.aborted) renderArticles(records);
        } catch (error) {
          if (error.name !== "AbortError") showMessage(`Medienbeiträge konnten nicht geladen werden: ${error.message}`, "error");
        }
      }

      async function refresh() {
        await Promise.all([refreshAnalytics(), refreshRecords()]);
      }

      async function refreshComparison(baseParams, signal) {
        const kind = $("#compare-kind").value;
        const field = kind === "city" ? "city" : "project";
        const firstValue = $("#compare-a").value;
        const secondValue = $("#compare-b").value;
        if (!firstValue || !secondValue) {
          renderComparison(null, null);
          return;
        }
        const firstParams = new URLSearchParams(baseParams);
        const secondParams = new URLSearchParams(baseParams);
        firstParams.delete(field);
        secondParams.delete(field);
        firstParams.set(field, firstValue);
        secondParams.set(field, secondValue);
        const [first, second] = await Promise.all([
          api(`/api/analytics?${firstParams}`, { signal }),
          api(`/api/analytics?${secondParams}`, { signal })
        ]);
        if (!signal.aborted) renderComparison(first, second, firstValue, secondValue);
      }

      function setImportBusy(busy) {
        state.importing = busy;
        $("#choose-files").disabled = busy;
        $("#dataset-files").disabled = busy;
        $("#import-dropzone").classList.toggle("busy", busy);
        $("#import-dropzone").setAttribute("aria-busy", String(busy));
      }

      function uploadDatasetFile(file, onProgress) {
        return new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", "/api/datasets");
          xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
          xhr.upload.addEventListener("progress", onProgress);
          xhr.addEventListener("load", () => {
            let result;
            try {
              result = JSON.parse(xhr.responseText);
            } catch {
              reject(new Error("Der Server hat keine gültige Antwort zurückgegeben."));
              return;
            }
            if (xhr.status < 200 || xhr.status >= 300) {
              reject(new Error(result.error || `Backend antwortete mit HTTP ${xhr.status}.`));
              return;
            }
            resolve(result);
          });
          xhr.addEventListener("error", () => reject(new Error("Upload fehlgeschlagen. Prüfe, ob das Python-Backend läuft.")));
          xhr.addEventListener("abort", () => reject(new Error("Upload abgebrochen.")));
          xhr.send(file);
        });
      }

      async function importFiles(fileList) {
        const files = Array.from(fileList || []);
        if (!files.length || state.importing) return;
        const invalidFiles = files.map((file) => {
          const extension = file.name.toLowerCase().split(".").pop();
          if (!["json", "jsonl", "ndjson", "csv"].includes(extension)) return `${file.name} (Format nicht unterstützt)`;
          if (file.size === 0) return `${file.name} (Datei ist leer)`;
          if (file.size > 300 * 1024 * 1024) return `${file.name} (größer als 300 MB)`;
          return "";
        }).filter(Boolean);
        if (invalidFiles.length) {
          $("#import-state").className = "import-state error";
          $("#import-state").textContent = `Nicht importiert: ${invalidFiles.join(", ")}. Erlaubt sind JSON/JSONL/NDJSON und CSV-Dateien bis 300 MB.`;
          return;
        }

        const totalBytes = files.reduce((total, file) => total + file.size, 0);
        const imported = [];
        const failures = [];
        let completedBytes = 0;
        let refreshError = "";
        $("#import-state").className = "import-state";
        $("#import-state").textContent = "";
        $("#import-progress").hidden = false;
        setImportBusy(true);
        try {
          for (const [index, file] of files.entries()) {
            $("#import-progress-file").textContent = `Datei ${index + 1} von ${files.length}: ${file.name}`;
            const updateProgress = (event) => {
              const loaded = completedBytes + (event.lengthComputable ? event.loaded : 0);
              const percent = Math.min(100, Math.round(loaded / totalBytes * 100));
              $("#import-progress-percent").textContent = `${percent}%`;
              $("#import-progress-bar").style.width = `${percent}%`;
              $("#import-progress .import-progress-track").setAttribute("aria-valuenow", String(percent));
            };
            try {
              imported.push(await uploadDatasetFile(file, updateProgress));
            } catch (error) {
              failures.push(`${file.name}: ${error.message}`);
            }
            completedBytes += file.size;
          }

          if (imported.length) {
            try {
              await loadDatasets(imported.length === 1 ? imported[0].id : "all");
              await loadFacets();
              state.page = 1;
              await refresh();
            } catch (error) {
              refreshError = `Import gespeichert, aber das Dashboard konnte nicht aktualisiert werden: ${error.message}`;
              showMessage(refreshError, "error");
            }
          }
        } finally {
          setImportBusy(false);
          $("#import-progress").hidden = true;
          $("#dataset-files").value = "";
        }

        const messages = [];
        if (imported.length) {
          const recordCount = imported.reduce((total, dataset) => total + dataset.record_count, 0);
          const datasetLabel = imported.length === 1 ? "Datensatz" : "Datensätze";
          const recordLabel = recordCount === 1 ? "Beitrag" : "Beiträge";
          messages.push(`${number.format(imported.length)} ${datasetLabel} mit ${number.format(recordCount)} ${recordLabel} importiert.`);
        }
        if (failures.length) messages.push(`Fehler: ${failures.join(" ")}`);
        if (refreshError) messages.push(refreshError);
        $("#import-state").className = `import-state${failures.length || refreshError ? " error" : ""}`;
        $("#import-state").textContent = messages.join(" ");
      }

      function startImport(fileList) {
        importFiles(fileList).catch((error) => {
          setImportBusy(false);
          $("#import-progress").hidden = true;
          $("#dataset-files").value = "";
          $("#import-state").className = "import-state error";
          $("#import-state").textContent = `Dateiimport fehlgeschlagen: ${error.message}`;
        });
      }

      async function start() {
        if (window.location.protocol === "file:") {
          $("#server-status").textContent = "Dashboard direkt als Datei geöffnet";
          $("#status-dot").className = "status-dot error";
          showMessage("Das Backend ist für Daten und Dateiimporte erforderlich. Starte start.bat und öffne anschließend http://127.0.0.1:8765.", "error");
          return;
        }
        try {
          const health = await api("/api/health");
          $("#server-status").textContent = `Backend verbunden · ${health.version}`;
          $("#status-dot").className = "status-dot ready";
          await loadDatasets();
          await loadFacets();
          await refresh();
        } catch (error) {
          $("#server-status").textContent = "Backend nicht erreichbar";
          $("#status-dot").className = "status-dot error";
          showMessage(`Python-Backend nicht erreichbar: ${error.message} Starte es mit start.bat.`, "error");
        }
      }

      $("#metrics-empty-action").addEventListener("click", () => {
        $("#clear-filters").click();
      });
      $("#delete-dataset").addEventListener("click", async () => {
        const datasetId = $("#dataset-filter").value;
        if (datasetId === "all") {
          showMessage("Wähle zuerst einen einzelnen Datensatz aus, den du entfernen möchtest.", "error");
          return;
        }
        const dataset = state.datasets.find((item) => item.id === datasetId);
        if (!dataset) {
          showMessage("Der Datensatz konnte nicht gefunden werden.", "error");
          return;
        }
        if (!confirm(`Datensatz „${dataset.name}“ und alle zugehörigen Beiträge wirklich entfernen?`)) return;
        try {
          await api(`/api/datasets/${encodeURIComponent(datasetId)}`, { method: "DELETE" });
          showMessage(`Datensatz „${dataset.name}“ wurde entfernt.`, "success");
          await loadDatasets("all");
          await loadFacets();
          await refresh();
        } catch (error) {
          showMessage(`Datensatz konnte nicht entfernt werden: ${error.message}`, "error");
        }
      });
      $("#dataset-filter").addEventListener("change", async () => {
        state.page = 1;
        updateDatasetActions();
        try { await loadFacets(); await refresh(); } catch (error) { showMessage(error.message); }
      });
      $("#topic-filter").addEventListener("input", () => {
        state.page = 1;
        clearTimeout(state.searchTimer);
        state.searchTimer = setTimeout(refresh, 250);
      });
      for (const id of ["city-filter", "project-filter", "sentiment-filter"]) {
        $(`#${id}`).addEventListener("change", () => { state.page = 1; refresh(); });
      }
      for (const id of ["date-start", "date-end"]) {
        $(`#${id}`).addEventListener("input", () => {
          updateDateRange(false, id);
          state.page = 1;
          clearTimeout(state.dateTimer);
          state.dateTimer = setTimeout(refresh, 100);
        });
      }
      $("#date-reset").addEventListener("click", () => {
        updateDateRange(true);
        state.page = 1;
        refresh();
      });
      $("#clear-filters").addEventListener("click", async () => {
        $("#dataset-filter").value = "all";
        $("#topic-filter").value = "";
        $("#city-filter").value = "";
        $("#project-filter").value = "";
        $("#sentiment-filter").value = "";
        $("#compare-a").value = "";
        $("#compare-b").value = "";
        $("#record-search").value = "";
        $("#record-type").value = "";
        $("#record-sentiment").value = "";
        $("#record-sort").value = "published_at:desc";
        state.page = 1;
        try {
          await loadDatasets("all");
          await loadFacets();
          updateCompareOptions();
          await refresh();
        } catch (error) { showMessage(`Filter konnten nicht zurückgesetzt werden: ${error.message}`, "error"); }
      });
      $("#record-search").addEventListener("input", () => {
        state.page = 1;
        clearTimeout(state.recordSearchTimer);
        state.recordSearchTimer = setTimeout(refreshRecords, 250);
      });
      for (const id of ["record-type", "record-sentiment", "record-sort"]) {
        $(`#${id}`).addEventListener("change", () => { state.page = 1; refreshRecords(); });
      }
      $("#record-reset").addEventListener("click", () => {
        $("#record-search").value = "";
        $("#record-type").value = "";
        $("#record-sentiment").value = "";
        $("#record-sort").value = "published_at:desc";
        state.page = 1;
        refreshRecords();
      });
      $("#articles").addEventListener("click", (event) => {
        const cityButton = event.target.closest("[data-city-filter]");
        const projectButton = event.target.closest("[data-project-filter]");
        if (cityButton) $("#city-filter").value = cityButton.dataset.cityFilter;
        if (projectButton) $("#project-filter").value = projectButton.dataset.projectFilter;
        if (cityButton || projectButton) { state.page = 1; refresh(); }
      });
      $("#show-more").addEventListener("click", async () => {
        const button = $("#show-more");
        if (button.disabled) return;
        button.disabled = true;
        state.page += 1;
        if (state.moreRequest) state.moreRequest.abort();
        const controller = new AbortController();
        state.moreRequest = controller;
        try {
          const params = recordParams();
          params.set("page", String(state.page)); params.set("page_size", String(state.pageSize));
          const result = await api(`/api/records?${params}`, { signal: controller.signal });
          if (controller.signal.aborted) return;
          const container = $("#articles");
          container.insertAdjacentHTML("beforeend", result.items.map((row) => renderArticle(row)).join(""));
          $("#show-more").hidden = state.page * state.pageSize >= result.total;
        } catch (error) {
          if (error.name !== "AbortError") showMessage(`Weitere Beiträge konnten nicht geladen werden: ${error.message}`);
        } finally {
          button.disabled = false;
        }
      });
      $("#compare-kind").addEventListener("change", () => { updateCompareOptions(); refreshAnalytics(); });
      $("#compare-a").addEventListener("change", refreshAnalytics);
      $("#compare-b").addEventListener("change", refreshAnalytics);
      $("#export").addEventListener("click", () => { window.location.href = `/api/export.csv?${recordParams()}`; });
      $("#tag-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const datasetId = $("#dataset-filter").value;
        if (datasetId === "all") {
          $("#tag-state").textContent = "Wähle zuerst einen einzelnen Datensatz aus.";
          return;
        }
        const button = $("#tag-form button[type=submit]");
        button.disabled = true;
        $("#tag-state").textContent = "Zuordnungen werden ergänzt …";
        try {
          const result = await api(`/api/datasets/${encodeURIComponent(datasetId)}/tags`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ city: $("#tag-city").value.trim(), project: $("#tag-project").value.trim() })
          });
          $("#tag-form").reset();
          $("#tag-state").textContent = `Ergänzt: ${number.format(result.city_updated)} Stadt- und ${number.format(result.project_updated)} Projektzuordnungen. Vorhandene Angaben bleiben unverändert.`;
          await loadFacets();
          state.page = 1;
          await refresh();
        } catch (error) {
          $("#tag-state").textContent = `Zuordnung fehlgeschlagen: ${error.message}`;
        } finally {
          button.disabled = false;
        }
      });
      $("#choose-files").addEventListener("click", () => {
        if (!state.importing) $("#dataset-files").click();
      });
      $("#dataset-files").addEventListener("change", (event) => {
        const files = Array.from(event.currentTarget.files || []);
        event.currentTarget.value = "";
        startImport(files);
      });
      const importDropzone = $("#import-dropzone");
      importDropzone.addEventListener("dragenter", (event) => {
        if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
        event.preventDefault();
        if (!state.importing) importDropzone.classList.add("dragover");
      });
      importDropzone.addEventListener("dragover", (event) => {
        if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
        if (!state.importing) importDropzone.classList.add("dragover");
      });
      importDropzone.addEventListener("dragleave", (event) => {
        if (!importDropzone.contains(event.relatedTarget)) importDropzone.classList.remove("dragover");
      });
      importDropzone.addEventListener("drop", (event) => {
        event.preventDefault();
        importDropzone.classList.remove("dragover");
        startImport(event.dataTransfer?.files);
      });

      initializeWidgetManager();
      start();
    })();
  