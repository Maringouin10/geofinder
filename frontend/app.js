/* GeoFinder — front minimal : indexation d'une ville, puis recherche d'une photo. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    cityInput: $("city-input"), indexBtn: $("index-btn"),
    optPanos: $("opt-panos"), optHeadings: $("opt-headings"),
    optRadius: $("opt-radius"), optSpacing: $("opt-spacing"),
    providerSelect: $("provider-select"), providerNote: $("provider-note"),
    attribution: $("attribution"),
    costEstimate: $("cost-estimate"),
    job: $("job"), jobStep: $("job-step"), jobBar: $("job-bar"), jobCancel: $("job-cancel"),
    jobError: $("job-error"),
    cities: $("cities"), citySelect: $("city-select"),
    drop: $("drop"), photo: $("photo"), preview: $("preview"), dropText: $("drop-text"),
    findBtn: $("find-btn"), optRerank: $("opt-rerank"),
    resultsCard: $("results-card"), results: $("results"), verdict: $("verdict"),
    uncertainNote: $("uncertain-note"),
    demoBanner: $("demo-banner"), toast: $("toast"),
  };

  let map, markers = [], pollTimer = null, currentJob = null, selectedFile = null;
  let providers = [], activeProvider = "demo";

  const ATTRIBUTION = {
    mapillary: "Images © contributeurs Mapillary (CC BY-SA)",
    kartaview: "Images © contributeurs KartaView (CC BY-SA)",
    google: "Panoramas © Google Street View",
    demo: "Images synthétiques — aucune valeur géographique",
  };

  /* ---------------------------------------------------------------- carte */
  function initMap() {
    map = L.map("map", { zoomControl: true }).setView([48.8566, 2.3522], 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "© OpenStreetMap",
    }).addTo(map);
  }

  function clearMarkers() {
    markers.forEach((m) => map.removeLayer(m));
    markers = [];
  }

  function plot(matches) {
    clearMarkers();
    if (!matches.length) return;

    matches.forEach((m, i) => {
      const best = i === 0;
      const marker = L.circleMarker([m.lat, m.lng], {
        radius: best ? 12 : 7,
        color: best ? "#3fb950" : "#2f81f7",
        fillColor: best ? "#3fb950" : "#2f81f7",
        fillOpacity: best ? 0.85 : 0.45,
        weight: best ? 3 : 1.5,
      }).addTo(map);

      marker.bindPopup(
        `<img src="${m.thumb}" alt="" />` +
        `<b>#${i + 1} — score ${(m.score * 100).toFixed(0)}%</b><br />` +
        `${m.lat.toFixed(6)}, ${m.lng.toFixed(6)}<br />` +
        `<span style="opacity:.7">similarité ${(m.similarity * 100).toFixed(1)}% · ` +
        `${m.inliers} points vérifiés</span><br />` +
        `<a href="${m.source_url}" target="_blank" rel="noopener">Voir sur place →</a>`
      );
      marker.on("click", () => highlight(i));
      markers.push(marker);
    });

    const group = L.featureGroup(markers);
    map.fitBounds(group.getBounds().pad(0.35), { maxZoom: 17 });
    markers[0].openPopup();
  }

  function highlight(i) {
    document.querySelectorAll(".result").forEach((el, k) => el.classList.toggle("active", k === i));
    if (markers[i]) {
      map.setView(markers[i].getLatLng(), Math.max(map.getZoom(), 16));
      markers[i].openPopup();
    }
  }

  /* --------------------------------------------------------------- utils */
  function toast(msg, ok = false) {
    els.toast.textContent = msg;
    els.toast.classList.toggle("ok", ok);
    els.toast.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => els.toast.classList.add("hidden"), 5200);
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, options);
    const text = await resp.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
    if (!resp.ok) throw new Error(data.detail || `Erreur ${resp.status}`);
    return data;
  }

  function renderProviders() {
    const auto = providers.find((p) => p.name === activeProvider);
    els.providerSelect.innerHTML =
      `<option value="auto">Automatique — ${escapeHtml(auto ? auto.label : activeProvider)}</option>` +
      providers
        .map((p) => `<option value="${p.name}">${escapeHtml(p.label)}${p.ready ? "" : " (non configurée)"}</option>`)
        .join("");
    els.attribution.textContent = ATTRIBUTION[activeProvider] || ATTRIBUTION.demo;
    describeProvider();
  }

  function describeProvider() {
    const chosen = els.providerSelect.value === "auto" ? activeProvider : els.providerSelect.value;
    const info = providers.find((p) => p.name === chosen);
    if (!info) return;
    els.providerNote.textContent = info.ready
      ? ATTRIBUTION[info.name] || ""
      : info.reason;
    els.providerNote.classList.toggle("provider-ko", !info.ready);
    // Seul Google rend des vues à la demande ; ailleurs le cap vient de la photo.
    els.optSpacing.disabled = chosen !== "google" && chosen !== "demo";
  }

  function updateCost() {
    const n = (+els.optPanos.value || 0) * (+els.optHeadings.value || 0);
    els.costEstimate.textContent = n.toLocaleString("fr-FR");
  }

  /* ------------------------------------------------------------- villes */
  async function refreshCities() {
    const { cities } = await api("/api/cities");
    const previous = els.citySelect.value;

    els.cities.innerHTML = "";
    cities.forEach((c) => {
      const row = document.createElement("div");
      row.className = "city-item";
      row.innerHTML =
        `<span class="name">${escapeHtml(c.display_name.split(",")[0])}` +
        (c.demo
          ? ' <span class="tag-demo">DEMO</span>'
          : ` <span class="tag-src">${escapeHtml(c.provider || "?")}</span>`) +
        (c.stale ? ' <span class="tag-stale" title="Construit avec un autre modèle — à réindexer">PÉRIMÉ</span>' : "") +
        "</span>" +
        `<span class="count">${c.panos} pano · ${c.views} vues</span>` +
        `<button class="del" title="Supprimer">✕</button>`;
      row.querySelector(".del").onclick = async () => {
        if (!confirm(`Supprimer l'index de ${c.display_name.split(",")[0]} ?`)) return;
        await api(`/api/cities/${c.slug}`, { method: "DELETE" });
        refreshCities();
      };
      row.querySelector(".name").onclick = () => map.setView([c.lat, c.lng], 13);
      els.cities.appendChild(row);
    });

    els.citySelect.innerHTML = '<option value="all">Toutes les villes indexées</option>' +
      cities.map((c) => `<option value="${c.slug}">${escapeHtml(c.display_name.split(",")[0])}</option>`).join("");
    if ([...els.citySelect.options].some((o) => o.value === previous)) els.citySelect.value = previous;

    els.findBtn.disabled = !selectedFile || cities.length === 0;
    if (!cities.length) els.citySelect.innerHTML = '<option value="all">— aucune ville indexée —</option>';
    return cities;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* --------------------------------------------------------- indexation */
  async function startIndex() {
    const city = els.cityInput.value.trim();
    if (city.length < 2) return toast("Entre un nom de ville.");

    els.indexBtn.disabled = true;
    try {
      const job = await api("/api/cities", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          city,
          provider: els.providerSelect.value || "auto",
          max_panos: +els.optPanos.value,
          headings: +els.optHeadings.value,
          radius_km: +els.optRadius.value,
          spacing_m: +els.optSpacing.value,
        }),
      });
      currentJob = job.id;
      els.job.classList.remove("hidden");
      els.jobError.classList.add("hidden");
      els.jobError.textContent = "";
      poll();
    } catch (e) {
      toast(e.message);
      els.indexBtn.disabled = false;
    }
  }

  function poll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!currentJob) return;
      try {
        const job = await api(`/api/jobs/${currentJob}`);
        els.jobStep.textContent = job.step;
        els.jobBar.style.width = `${Math.round(job.progress * 100)}%`;

        if (job.state === "done") {
          finishJob();
          toast(job.step, true);
          await refreshCities();
        } else if (job.state === "error") {
          finishJob({ keepOpen: true });
          // Un diagnostic de source fait plusieurs lignes : il doit rester
          // affiché, pas disparaître avec un toast au bout de 5 secondes.
          els.jobError.textContent = job.error || "Échec de l'indexation.";
          els.jobError.classList.remove("hidden");
          toast("Échec de l'indexation — détail ci-dessous.");
        } else if (job.state === "cancelled") {
          finishJob();
          toast("Indexation annulée.");
          await refreshCities();
        } else {
          poll();
        }
      } catch (e) {
        finishJob();
        toast(e.message);
      }
    }, 900);
  }

  function finishJob({ keepOpen = false } = {}) {
    currentJob = null;
    els.indexBtn.disabled = false;
    if (!keepOpen) setTimeout(() => els.job.classList.add("hidden"), 2500);
  }

  async function cancelJob() {
    if (!currentJob) return;
    try { await api(`/api/jobs/${currentJob}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
  }

  /* -------------------------------------------------------------- photo */
  function setFile(file) {
    if (!file || !file.type.startsWith("image/")) return toast("Ce fichier n'est pas une image.");
    selectedFile = file;
    const url = URL.createObjectURL(file);
    els.preview.src = url;
    els.preview.classList.remove("hidden");
    els.dropText.classList.add("hidden");
    els.findBtn.disabled = els.citySelect.options.length === 0 ||
      els.citySelect.value === "" || els.citySelect.innerHTML.includes("aucune ville");
  }

  function wireDropzone() {
    els.photo.addEventListener("change", (e) => e.target.files[0] && setFile(e.target.files[0]));
    ["dragenter", "dragover"].forEach((ev) =>
      els.drop.addEventListener(ev, (e) => { e.preventDefault(); els.drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) =>
      els.drop.addEventListener(ev, (e) => { e.preventDefault(); els.drop.classList.remove("over"); }));
    els.drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && setFile(e.dataTransfer.files[0]));
    window.addEventListener("paste", (e) => {
      const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
      if (item) setFile(item.getAsFile());
    });
  }

  /* --------------------------------------------------------------- find */
  async function find() {
    if (!selectedFile) return toast("Choisis d'abord une photo.");
    const form = new FormData();
    form.append("file", selectedFile);
    form.append("city", els.citySelect.value || "all");
    form.append("top_k", "6");
    form.append("rerank", els.optRerank.checked ? "true" : "false");

    els.findBtn.disabled = true;
    els.findBtn.textContent = "…";
    try {
      const data = await api("/api/find", { method: "POST", body: form });
      render(data);
    } catch (e) {
      toast(e.message);
    } finally {
      els.findBtn.disabled = false;
      els.findBtn.textContent = "FIND";
    }
  }

  function render(data) {
    els.resultsCard.classList.remove("hidden");
    els.results.innerHTML = "";

    const matches = data.matches || [];
    if (!matches.length) {
      els.verdict.innerHTML = '<div class="verdict-box none">Aucune correspondance trouvée.</div>';
      clearMarkers();
      return;
    }

    const best = matches[0];
    const conf = data.diagnostic?.confidence ?? 0;
    const uncertain = data.diagnostic?.uncertain === true;
    const level = uncertain ? "uncertain-box" : conf >= 0.6 ? "" : "low";

    // Mieux vaut annoncer l'incertitude que désigner un lieu avec aplomb :
    // une réponse fausse mais assurée est pire qu'un « je ne sais pas ».
    els.uncertainNote.classList.toggle("hidden", !uncertain);
    if (uncertain) {
      els.uncertainNote.innerHTML =
        "<strong>Localisation incertaine.</strong> Aucune vue indexée ne " +
        "correspond vraiment à cette photo. Les propositions ci-dessous sont " +
        "les moins mauvaises, pas des réponses. Le lieu n'est probablement pas " +
        "couvert : réindexe la zone avec un rayon plus petit et plus de lieux.";
    }

    els.verdict.innerHTML =
      `<div class="verdict-box ${level}">` +
      `<div class="verdict-coords">${best.lat.toFixed(6)}, ${best.lng.toFixed(6)}</div>` +
      `<div class="verdict-meta">${escapeHtml(best.city)}</div>` +
      `<div class="verdict-meta">Confiance ${(conf * 100).toFixed(0)}% · ` +
      `similarité ${(best.similarity * 100).toFixed(1)}% · ` +
      `${best.inliers} points vérifiés · ` +
      `${best.support} vues voisines concordantes · ` +
      `${(data.diagnostic?.views_searched || 0).toLocaleString("fr-FR")} vues comparées</div>` +
      `</div>`;

    matches.forEach((m, i) => {
      const li = document.createElement("li");
      li.className = "result";
      li.innerHTML =
        `<span class="rank">${i + 1}</span>` +
        `<img src="${m.thumb}" alt="" loading="lazy" />` +
        `<div class="meta">` +
        `<div class="coords">${m.lat.toFixed(5)}, ${m.lng.toFixed(5)}</div>` +
        `<div class="sub">score ${(m.score * 100).toFixed(0)}% · sim ${(m.similarity * 100).toFixed(1)}% · ` +
        `${m.inliers} pts · ${m.support} voisins · cap ${m.heading}°</div>` +
        `</div>`;
      li.onclick = () => highlight(i);
      els.results.appendChild(li);
    });

    plot(matches);
    highlight(0);
  }

  /* --------------------------------------------------------------- init */
  async function init() {
    initMap();
    wireDropzone();
    els.indexBtn.onclick = startIndex;
    els.jobCancel.onclick = cancelJob;
    els.findBtn.onclick = find;
    els.cityInput.addEventListener("keydown", (e) => e.key === "Enter" && startIndex());
    els.providerSelect.addEventListener("change", describeProvider);
    [els.optPanos, els.optHeadings].forEach((el) => el.addEventListener("input", updateCost));
    els.citySelect.addEventListener("change", () => {
      els.findBtn.disabled = !selectedFile;
    });
    updateCost();

    try {
      const health = await api("/api/health");
      providers = health.providers || [];
      activeProvider = health.provider || "demo";
      renderProviders();
      els.demoBanner.classList.toggle("hidden", !health.demo_mode);
    } catch { /* l'API répondra plus tard */ }

    const cities = await refreshCities();
    if (cities.length) map.setView([cities[0].lat, cities[0].lng], 13);
  }

  init();
})();
