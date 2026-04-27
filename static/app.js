/* ============================================================
   SDR Tracker — app.js
   ============================================================ */

(function () {
  "use strict";

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  let map;
  let satMarker = null;
  let footprintCircle = null;
  let stationMarkers = {};   // id -> L.Marker
  let stationsData = [];     // full list from API
  let activeStationIds = new Set();
  let currentSat = "ISS";
  let eventSource = null;
  let filterByFreq = false;

  // ------------------------------------------------------------------
  // Map init
  // ------------------------------------------------------------------
  function initMap() {
    map = L.map("map", {
      center: [20, 10],
      zoom: 2,
      zoomControl: true,
      worldCopyJump: false,
      maxBounds: [[-90, -400], [90, 400]],
    });
    window.__MAP = map;

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
      minZoom: 1,
    }).addTo(map);
  }

  // ------------------------------------------------------------------
  // Custom icons
  // ------------------------------------------------------------------
  function makeStationIcon(isOnline, isActive, isNew) {
    const cls = [
      "sdr-marker",
      isOnline ? "online" : "offline",
      isActive ? "active-in-footprint" : "",
      isNew    ? "new-today" : "",
    ].join(" ").trim();
    return L.divIcon({
      className: "",
      html: `<div class="${cls}"></div>`,
      iconSize: [12, 12],
      iconAnchor: [6, 6],
      popupAnchor: [0, -10],
    });
  }

  function makeSatIcon() {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
      <g transform="rotate(45 16 16)">
        <!-- Body -->
        <rect x="13" y="11" width="6" height="10" rx="1.5" fill="#00d4ff" opacity="0.95"/>
        <!-- Solar panels left -->
        <rect x="2" y="14" width="9" height="4" rx="1" fill="#1a6fc4" opacity="0.9"/>
        <line x1="2" y1="15" x2="11" y2="15" stroke="#0af" stroke-width="0.5" opacity="0.5"/>
        <line x1="2" y1="17" x2="11" y2="17" stroke="#0af" stroke-width="0.5" opacity="0.5"/>
        <!-- Solar panels right -->
        <rect x="21" y="14" width="9" height="4" rx="1" fill="#1a6fc4" opacity="0.9"/>
        <line x1="21" y1="15" x2="30" y2="15" stroke="#0af" stroke-width="0.5" opacity="0.5"/>
        <line x1="21" y1="17" x2="30" y2="17" stroke="#0af" stroke-width="0.5" opacity="0.5"/>
        <!-- Antenna -->
        <line x1="16" y1="5" x2="16" y2="11" stroke="#00d4ff" stroke-width="1.2"/>
        <circle cx="16" cy="4" r="1.5" fill="#00ffff"/>
      </g>
      <!-- Glow -->
      <circle cx="16" cy="16" r="4" fill="#00d4ff" opacity="0.18"/>
    </svg>`;
    return L.divIcon({
      className: "sat-marker-icon",
      html: svg,
      iconSize: [32, 32],
      iconAnchor: [16, 16],
      popupAnchor: [0, -18],
    });
  }

  // ------------------------------------------------------------------
  // Build popup HTML for a station
  // ------------------------------------------------------------------
  function buildPopupHtml(station, isActive, downlinkFreq) {
    const status = station.online
      ? `<span class="popup-status-dot online"></span> Online`
      : `<span class="popup-status-dot offline"></span> Offline`;

    const freqRows = (station.freqs || []).map(f =>
      `<div class="popup-freq-row">
        <span>${f.low.toFixed(3)}</span>
        <span>—</span>
        <span>${f.high.toFixed(3)} MHz</span>
      </div>`
    ).join("");

    const tuneParam = downlinkFreq ? `?tune=${(downlinkFreq * 1000).toFixed(0)}` : "";
    const openUrl = station.url ? (station.url.replace(/\/$/, "") + tuneParam) : "#";

    const btnClass = isActive ? "popup-btn popup-btn-active" : "popup-btn";
    const btnLabel = isActive
      ? `Deschide WebSDR (${downlinkFreq} MHz)`
      : "Deschide WebSDR";

    const newBadge = station.is_new
      ? `<span class="popup-new-badge">★ NOU AZI</span>` : "";
    const firstSeen = station.first_seen
      ? `<div class="popup-first-seen">Văzut prima dată: ${station.first_seen}</div>` : "";

    return `<div class="popup-inner">
      <div class="popup-name">${escapeHtml(station.name)} ${newBadge}</div>
      <div class="popup-source">${escapeHtml(station.source || "")} · ${escapeHtml(station.type || "")}</div>
      <div class="popup-status">${status}</div>
      ${firstSeen}
      <div class="popup-freqs">${freqRows || '<span style="color:var(--text-dim);font-size:11px">Frecvențe necunoscute</span>'}</div>
    </div>
    <a href="${escapeHtml(openUrl)}" target="_blank" rel="noopener" class="${btnClass}">${btnLabel}</a>`;
  }

  // ------------------------------------------------------------------
  // Load SDR Stations
  // ------------------------------------------------------------------
  function loadStations() {
    fetch("/api/stations")
      .then(r => r.json())
      .then(data => {
        stationsData = data.stations || [];
        const newToday = data.new_today || 0;
        document.getElementById("station-count-label").textContent =
          `Stații SDR: ${stationsData.length}`;
        if (newToday > 0) {
          const badge = document.getElementById("new-today-badge");
          const label = document.getElementById("new-today-label");
          badge.style.display = "flex";
          label.textContent = `${newToday} ${newToday === 1 ? "nouă" : "noi"} azi`;
        }
        renderStationMarkers();
      })
      .catch(err => {
        console.warn("Station load failed:", err);
        document.getElementById("station-count-label").textContent = "Stații SDR: eroare";
      });
  }

  // ------------------------------------------------------------------
  // Render / update station markers
  // ------------------------------------------------------------------
  function renderStationMarkers() {
    stationsData.forEach(st => {
      const isActive = activeStationIds.has(st.id);
      const isNew = !!st.is_new;
      if (stationMarkers[st.id]) {
        stationMarkers[st.id].setIcon(makeStationIcon(st.online, isActive, isNew));
      } else {
        if (!isValidLatLon(st.lat, st.lon)) return;
        const marker = L.marker([st.lat, st.lon], {
          icon: makeStationIcon(st.online, isActive, isNew),
          title: st.name + (isNew ? " ★ NOU AZI" : ""),
          zIndexOffset: isNew ? 500 : (isActive ? 1000 : 0),
        });
        marker.on("click", () => onStationClick(st));
        marker.addTo(map);
        stationMarkers[st.id] = marker;
      }
    });
    applyFreqFilter();
  }

  function updateStationMarkerStates(activeIds) {
    activeStationIds = new Set(activeIds);
    window.__ACTIVE_STATION_IDS = activeStationIds;
    stationsData.forEach(st => {
      const m = stationMarkers[st.id];
      if (!m) return;
      const isActive = activeStationIds.has(st.id);
      const isNew = !!st.is_new;
      // Only update icon if marker is on the map; otherwise just store state
      if (map.hasLayer(m)) {
        m.setIcon(makeStationIcon(st.online, isActive, isNew));
        m.setZIndexOffset(isActive ? 1000 : (isNew ? 500 : 0));
      } else {
        m.options.icon = makeStationIcon(st.online, isActive, isNew);
        m.options.zIndexOffset = isActive ? 1000 : (isNew ? 500 : 0);
      }
    });
    updateActiveCount(activeIds);
  }

  function updateActiveCount(activeIds) {
    const total = (activeIds || Array.from(activeStationIds)).length;
    if (filterByFreq && total > 0) {
      const visible = (activeIds || Array.from(activeStationIds)).filter(id => {
        const m = stationMarkers[id];
        return m && map.hasLayer(m);
      }).length;
      document.getElementById("active-count").textContent =
        visible < total ? `${visible}/${total}` : total;
    } else {
      document.getElementById("active-count").textContent = total;
    }
  }

  // ------------------------------------------------------------------
  // Frequency filter
  // ------------------------------------------------------------------
  function applyFreqFilter() {
    if (!filterByFreq) {
      stationsData.forEach(st => {
        const m = stationMarkers[st.id];
        if (m && !map.hasLayer(m)) {
          m.addTo(map);
          const isActive = activeStationIds.has(st.id);
          m.setIcon(makeStationIcon(st.online, isActive, !!st.is_new));
          m.setZIndexOffset(isActive ? 1000 : (st.is_new ? 500 : 0));
        }
      });
      updateActiveCount();
      return;
    }

    const satData = window.__SAT_DB ? window.__SAT_DB[currentSat] : null;
    const downlinks = satData ? (satData.downlink || []) : [];

    if (downlinks.length === 0) {
      stationsData.forEach(st => {
        const m = stationMarkers[st.id];
        if (m && !map.hasLayer(m)) {
          m.addTo(map);
          const isActive = activeStationIds.has(st.id);
          m.setIcon(makeStationIcon(st.online, isActive, !!st.is_new));
          m.setZIndexOffset(isActive ? 1000 : (st.is_new ? 500 : 0));
        }
      });
      updateActiveCount();
      return;
    }

    stationsData.forEach(st => {
      const m = stationMarkers[st.id];
      if (!m) return;
      const stFreqs = st.freqs || [];
      const hasOverlap = downlinks.some(dl =>
        stFreqs.some(fr => fr.low <= dl.freq && dl.freq <= fr.high)
      );
      if (hasOverlap) {
        if (!map.hasLayer(m)) {
          m.addTo(map);
          // Restore icon state since setIcon is a no-op while off map
          const isActive = activeStationIds.has(st.id);
          m.setIcon(makeStationIcon(st.online, isActive, !!st.is_new));
          m.setZIndexOffset(isActive ? 1000 : (st.is_new ? 500 : 0));
        }
      } else {
        if (map.hasLayer(m)) map.removeLayer(m);
      }
    });
    updateActiveCount();
  }

  window.toggleFreqFilter = function () {
    filterByFreq = !filterByFreq;
    const btn = document.getElementById("freq-filter-btn");
    const lbl = document.getElementById("freq-filter-label");
    if (btn) btn.classList.toggle("active", filterByFreq);
    if (lbl) lbl.textContent = filterByFreq ? "Filtru: ACTIV" : "Filtru frecv.";
    applyFreqFilter();
    updateActiveCount();
  };

  // ------------------------------------------------------------------
  // Station click handler
  // ------------------------------------------------------------------
  function onStationClick(station) {
    const isActive = activeStationIds.has(station.id);
    // Find matching downlink freq
    let downlinkFreq = null;
    if (isActive && currentSat) {
      // Get sat downlinks from DOM data
      const satData = window.__SAT_DB ? window.__SAT_DB[currentSat] : null;
      if (satData) {
        for (const dl of (satData.downlink || [])) {
          for (const fr of (station.freqs || [])) {
            if (fr.low <= dl.freq && dl.freq <= fr.high) {
              downlinkFreq = dl.freq;
              break;
            }
          }
          if (downlinkFreq) break;
        }
      }
    }

    const popup = L.popup({ maxWidth: 280 })
      .setLatLng([station.lat, station.lon])
      .setContent(buildPopupHtml(station, isActive, downlinkFreq));

    popup.openOn(map);

    // If active, open URL in new tab on popup open
    if (isActive && downlinkFreq && station.url) {
      // User still needs to click the button — don't auto-navigate
    }
  }

  // ------------------------------------------------------------------
  // Satellite marker + footprint
  // ------------------------------------------------------------------
  function updateSatelliteOnMap(position) {
    const lat = position.lat;
    const lon = position.lon;
    const fpKm = position.footprint_km;

    if (!satMarker) {
      satMarker = L.marker([lat, lon], {
        icon: makeSatIcon(),
        zIndexOffset: 9999,
        title: currentSat,
      }).addTo(map);
      satMarker.bindTooltip(currentSat, {
        permanent: false,
        className: "sat-tooltip",
        direction: "top",
        offset: [0, -18],
      });
    } else {
      satMarker.setLatLng([lat, lon]);
    }

    if (!footprintCircle) {
      footprintCircle = L.circle([lat, lon], {
        radius: fpKm * 1000,
        color: "#00d4ff",
        weight: 1.5,
        opacity: 0.6,
        fillColor: "#00d4ff",
        fillOpacity: 0.04,
        dashArray: "6 4",
      }).addTo(map);
    } else {
      footprintCircle.setLatLng([lat, lon]);
      footprintCircle.setRadius(fpKm * 1000);
    }
  }

  function clearSatelliteFromMap() {
    if (satMarker) { map.removeLayer(satMarker); satMarker = null; }
    if (footprintCircle) { map.removeLayer(footprintCircle); footprintCircle = null; }
  }

  // ------------------------------------------------------------------
  // SSE Stream
  // ------------------------------------------------------------------
  function startStream(satId) {
    stopStream();
    setStreamStatus("connecting", `Conectare la ${satId}...`);

    eventSource = new EventSource(`/stream?sat=${encodeURIComponent(satId)}`);

    eventSource.onopen = () => {
      setStreamStatus("streaming", `Live: ${satId}`);
    };

    eventSource.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.error) {
          if (data.error === "no_tle") {
            setStreamStatus("error", "TLE indisponibil — reîncercare...");
          } else {
            setStreamStatus("error", `Eroare: ${data.error}`);
          }
          return;
        }
        setStreamStatus("streaming", `Live: ${satId}`);
        handleStreamUpdate(data);
      } catch (e) {
        console.warn("SSE parse error:", e);
      }
    };

    eventSource.onerror = () => {
      setStreamStatus("error", "Conexiune pierdută — reconectare...");
    };
  }

  function stopStream() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    setStreamStatus("idle", "Nicio urmărire activă");
  }

  function handleStreamUpdate(data) {
    if (!data.position) return;
    updateSatelliteOnMap(data.position);
    updatePositionPanel(data.position);
    updateStationMarkerStates(data.active_stations || []);

    // Update sidebar dot for selected satellite
    updateSatDots(data.sat_id, data.active_stations || []);

    // Observer geometry
    if (data.observer) {
      updateObserverGeo(data.observer);
    }
  }

  function updateSatDots(satId, activeStations) {
    // Highlight dot of currently tracked satellite
    document.querySelectorAll(".sat-dot").forEach(d => {
      d.classList.remove("active");
    });
    const dot = document.getElementById(`dot-${satId}`);
    if (dot) dot.classList.add("active");
  }

  // ------------------------------------------------------------------
  // Position panel
  // ------------------------------------------------------------------
  function updatePositionPanel(pos) {
    document.getElementById("pos-lat").textContent = pos.lat.toFixed(4) + "°";
    document.getElementById("pos-lon").textContent = pos.lon.toFixed(4) + "°";
    document.getElementById("pos-alt").textContent = pos.alt_km.toFixed(1) + " km";
    document.getElementById("pos-fp").textContent = pos.footprint_km.toFixed(0) + " km";
  }

  // ------------------------------------------------------------------
  // Satellite selection
  // ------------------------------------------------------------------
  window.selectSatellite = function (satId) {
    if (currentSat === satId && document.getElementById(`details-${satId}`).classList.contains("open")) {
      // Toggle off (deselect) — but keep tracking
    }

    // Close all details
    document.querySelectorAll(".sat-details").forEach(el => el.classList.remove("open"));
    document.querySelectorAll(".sat-item").forEach(el => el.classList.remove("selected"));

    // Open selected
    const details = document.getElementById(`details-${satId}`);
    const item = document.getElementById(`sat-item-${satId}`);
    if (details) details.classList.add("open");
    if (item) {
      item.classList.add("selected");
      item.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    // Update sat name in position panel
    const satNameEl = document.getElementById(`sat-item-${satId}`)
      ? document.getElementById(`sat-item-${satId}`).querySelector(".sat-name")
      : null;
    document.getElementById("pos-sat-name").textContent =
      satNameEl ? satNameEl.textContent.slice(0, 30) : satId;

    currentSat = satId;
    window.__CURRENT_SAT = satId;

    // Re-apply frequency filter for new satellite
    if (filterByFreq) applyFreqFilter();

    // Clear old satellite marker
    clearSatelliteFromMap();

    // Update ground track and pass predictions
    updateGroundTrack(satId);
    loadPasses(satId);
    document.getElementById("passes-sat-name").textContent =
      satNameEl ? satNameEl.textContent.slice(0, 20) : satId;

    // Start SSE
    startStream(satId);

    // Fetch initial position immediately
    fetch(`/api/satellite/${encodeURIComponent(satId)}/position`)
      .then(r => r.json())
      .then(data => {
        if (data.position) {
          updateSatelliteOnMap(data.position);
          updatePositionPanel(data.position);
          updateStationMarkerStates(data.active_stations || []);
          updateSatDots(satId, data.active_stations || []);
          // Pan map to satellite
          map.panTo([data.position.lat, data.position.lon], { animate: true, duration: 0.8 });
        }
      })
      .catch(err => console.warn("Initial position fetch failed:", err));
  };

  // ------------------------------------------------------------------
  // Stream status UI
  // ------------------------------------------------------------------
  function setStreamStatus(state, text) {
    const dot = document.getElementById("stream-dot");
    const txt = document.getElementById("stream-text");
    dot.className = "status-dot";
    if (state === "streaming") dot.classList.add("streaming");
    if (state === "error") dot.classList.add("error");
    txt.textContent = text;
  }

  // ------------------------------------------------------------------
  // Utils
  // ------------------------------------------------------------------
  function isValidLatLon(lat, lon) {
    return (
      typeof lat === "number" && typeof lon === "number" &&
      !isNaN(lat) && !isNaN(lon) &&
      lat >= -90 && lat <= 90 &&
      lon >= -180 && lon <= 180 &&
      !(lat === 0 && lon === 0)
    );
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ------------------------------------------------------------------
  // Load SAT_DB into window for popup use
  // ------------------------------------------------------------------
  function loadSatDB() {
    fetch("/api/satellites")
      .then(r => r.json())
      .then(data => { window.__SAT_DB = data; })
      .catch(() => {});
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    initMap();
    loadSatDB();
    loadStations();

    // Satellite search filter
    const satSearchInput = document.getElementById("sat-search");
    if (satSearchInput) {
      satSearchInput.addEventListener("input", function () {
        const q = this.value.toLowerCase().trim();
        let visible = 0;
        document.querySelectorAll(".sat-item").forEach(item => {
          const name = (item.querySelector(".sat-name") || {}).textContent || "";
          const agency = (item.querySelector(".badge-agency") || {}).textContent || "";
          const orbit = (item.querySelector(".badge-orbit") || {}).textContent || "";
          const match = !q || name.toLowerCase().includes(q) ||
                        agency.toLowerCase().includes(q) ||
                        orbit.toLowerCase().includes(q);
          item.style.display = match ? "" : "none";
          if (match) visible++;
        });
        document.getElementById("sat-count").textContent = q ? visible : document.querySelectorAll(".sat-item").length;
      });
    }

    // Default: select ISS
    setTimeout(() => {
      selectSatellite("ISS");
    }, 300);

    // Reload station markers every 5 minutes
    setInterval(loadStations, 5 * 60 * 1000);
  });

})();

/* ============================================================
   AI Assistant Widget
   ============================================================ */
(function () {
  "use strict";

  let aiOpen      = false;
  let aiStreaming  = false;
  let aiCurrentEl = null;
  let aiCurrentTxt = "";
  let aiModel     = { id: "claude-haiku-4-5-20251001", provider: "claude" };

  // ------------------------------------------------------------------
  // Boot: load models into selector
  // ------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    fetch("/api/models")
      .then(r => r.json())
      .then(data => {
        const sel = document.getElementById("ai-model-select");
        sel.innerHTML = "";

        // Claude group
        if (data.claude && data.claude.length) {
          const grp = document.createElement("optgroup");
          grp.label = "Claude (Anthropic)";
          data.claude.forEach(m => {
            const opt = document.createElement("option");
            opt.value = JSON.stringify({ id: m.id, provider: "claude" });
            opt.textContent = m.name;
            grp.appendChild(opt);
          });
          sel.appendChild(grp);
        }

        // Ollama group
        if (data.ollama && data.ollama.length) {
          const grp = document.createElement("optgroup");
          grp.label = "Ollama (local)";
          data.ollama.forEach(m => {
            const opt = document.createElement("option");
            opt.value = JSON.stringify({ id: m.id, provider: "ollama" });
            opt.textContent = m.name;
            grp.appendChild(opt);
          });
          sel.appendChild(grp);
        }

        sel.addEventListener("change", function () {
          try { aiModel = JSON.parse(this.value); } catch (_) {}
        });
      })
      .catch(() => {});
  });

  // ------------------------------------------------------------------
  // Toggle panel
  // ------------------------------------------------------------------
  window.toggleAI = function () {
    aiOpen = !aiOpen;
    const panel = document.getElementById("ai-panel");
    panel.classList.toggle("ai-hidden", !aiOpen);
    if (aiOpen) document.getElementById("ai-input").focus();
  };

  // ------------------------------------------------------------------
  // Key handler (Enter send / auto-resize)
  // ------------------------------------------------------------------
  window.handleAIKey = function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendAIMessage();
    }
    const ta = document.getElementById("ai-input");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 80) + "px";
  };

  // ------------------------------------------------------------------
  // Send message
  // ------------------------------------------------------------------
  window.sendAIMessage = function () {
    if (aiStreaming) return;
    const input = document.getElementById("ai-input");
    const msg   = input.value.trim();
    if (!msg) return;

    input.value = "";
    input.style.height = "auto";
    appendUserMsg(msg);

    const satId     = window.__CURRENT_SAT || "ISS";
    const activeIds = window.__ACTIVE_STATION_IDS ? Array.from(window.__ACTIVE_STATION_IDS) : [];

    startBotMsg();

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: msg,
        sat_id: satId,
        active_stations: activeIds,
        model: aiModel.id,
        provider: aiModel.provider,
      }),
    }).then(resp => {
      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) { finishBotMsg(); return; }
          buf += decoder.decode(value, { stream: true });
          const parts = buf.split("\n\n");
          buf = parts.pop();
          for (const part of parts) {
            const line = part.replace(/^data: /, "").trim();
            if (!line) continue;
            try {
              const ev = JSON.parse(line);
              if (ev.text)  { appendBotChunk(ev.text); }
              if (ev.done)  { finishBotMsg(); return; }
              if (ev.error) { appendBotChunk("\n\n⚠ " + ev.error); finishBotMsg(); return; }
            } catch (_) {}
          }
          pump();
        }).catch(() => finishBotMsg());
      }
      pump();
    }).catch(err => {
      appendBotChunk("⚠ Eroare conexiune: " + err.message);
      finishBotMsg();
    });
  };

  // ------------------------------------------------------------------
  // Message rendering helpers
  // ------------------------------------------------------------------
  function appendUserMsg(text) {
    const msgs = document.getElementById("ai-messages");
    const div  = document.createElement("div");
    div.className = "ai-msg ai-msg-user";
    div.innerHTML = `<div class="ai-msg-content">${escHtml(text)}</div>`;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function startBotMsg() {
    aiStreaming  = true;
    aiCurrentTxt = "";
    document.getElementById("ai-send").disabled = true;

    const msgs = document.getElementById("ai-messages");
    const div  = document.createElement("div");
    div.className = "ai-msg ai-msg-bot";
    div.innerHTML = `<div class="ai-msg-content"><span class="ai-typing"><span></span><span></span><span></span></span></div>`;
    msgs.appendChild(div);
    aiCurrentEl = div.querySelector(".ai-msg-content");
    msgs.scrollTop = msgs.scrollHeight;
  }

  function appendBotChunk(text) {
    aiCurrentTxt += text;

    let html = typeof marked !== "undefined"
      ? marked.parse(aiCurrentTxt)
      : escHtml(aiCurrentTxt).replace(/\n/g, "<br>");

    aiCurrentEl.innerHTML = html;

    // Convert plain links to station buttons; keep other links as-is
    aiCurrentEl.querySelectorAll("a[href]").forEach(a => {
      const href = a.getAttribute("href") || "";
      const isStation = /^https?:\/\/.+(:\d+|sdr|websdr|kiwi|openwebrx)/i.test(href);
      if (isStation) {
        const btn = document.createElement("a");
        btn.href      = href;
        btn.target    = "_blank";
        btn.rel       = "noopener";
        btn.className = "ai-station-btn";
        btn.innerHTML =
          `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
             <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/>
             <path d="M8 12l3 3 5-5" stroke="currentColor" stroke-width="2" fill="none"/>
           </svg> ${escHtml(a.textContent || href)}`;
        a.replaceWith(btn);
      } else {
        a.target = "_blank";
        a.rel    = "noopener";
      }
    });

    document.getElementById("ai-messages").scrollTop = 99999;
  }

  function finishBotMsg() {
    aiStreaming = false;
    document.getElementById("ai-send").disabled = false;
    if (aiCurrentEl && !aiCurrentTxt) {
      aiCurrentEl.innerHTML = '<span style="color:var(--text-dim);font-size:11px">Fără răspuns</span>';
    }
    aiCurrentEl  = null;
    aiCurrentTxt = "";
    document.getElementById("ai-input").focus();
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

})();

/* ============================================================
   Observer, Ground Track, Passes, Polar Plot
   ============================================================ */
(function () {
  "use strict";

  // ----------------------------------------------------------------
  // Observer management
  // ----------------------------------------------------------------
  window.geocodeObserver = function () {
    const q = (document.getElementById("obs-search").value || "").trim();
    if (!q) return;
    fetch("/api/geocode?q=" + encodeURIComponent(q))
      .then(r => r.json())
      .then(results => {
        if (!Array.isArray(results) || results.length === 0) {
          alert("Niciun rezultat găsit.");
          return;
        }
        const first = results[0];
        saveObserver({ lat: first.lat, lon: first.lon, name: first.name });
      })
      .catch(err => console.warn("Geocode error:", err));
  };

  window.useGPS = function () {
    if (!navigator.geolocation) { alert("GPS indisponibil."); return; }
    navigator.geolocation.getCurrentPosition(
      pos => {
        saveObserver({
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          name: "Locație GPS",
        });
      },
      err => alert("GPS eroare: " + err.message)
    );
  };

  function saveObserver(obs) {
    fetch("/api/observer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obs),
    })
      .then(r => r.json())
      .then(data => {
        updateObserverUI(data);
        // Refresh passes and ground track for current satellite
        const sat = window.__CURRENT_SAT;
        if (sat) { updateGroundTrack(sat); loadPasses(sat); }
      })
      .catch(err => console.warn("Save observer error:", err));
  }

  function updateObserverUI(obs) {
    document.getElementById("obs-lat").textContent = (obs.lat || 0).toFixed(4);
    document.getElementById("obs-lon").textContent = (obs.lon || 0).toFixed(4);
    const nameParts = (obs.name || "").split(",");
    document.getElementById("obs-name").textContent = nameParts[0].trim().slice(0, 30);
  }

  function loadObserver() {
    fetch("/api/observer")
      .then(r => r.json())
      .then(data => updateObserverUI(data))
      .catch(() => {});
  }

  // ----------------------------------------------------------------
  // Ground track
  // ----------------------------------------------------------------
  let groundTrackLayers = { past: [], future: [] };

  window.updateGroundTrack = function (satId) {
    // Remove old layers
    groundTrackLayers.past.forEach(l => { if (window.__MAP) window.__MAP.removeLayer(l); });
    groundTrackLayers.future.forEach(l => { if (window.__MAP) window.__MAP.removeLayer(l); });
    groundTrackLayers = { past: [], future: [] };

    fetch(`/api/satellite/${encodeURIComponent(satId)}/groundtrack?past=30&future=90`)
      .then(r => r.json())
      .then(data => {
        const m = window.__MAP;
        if (!m) return;
        (data.past || []).forEach(seg => {
          if (seg.length < 2) return;
          const layer = L.polyline(seg, {
            color: "#00d4ff",
            weight: 1.5,
            opacity: 0.4,
            dashArray: "4 4",
          }).addTo(m);
          groundTrackLayers.past.push(layer);
        });
        (data.future || []).forEach(seg => {
          if (seg.length < 2) return;
          const layer = L.polyline(seg, {
            color: "#22ff88",
            weight: 2,
            opacity: 0.55,
            dashArray: "6 3",
          }).addTo(m);
          groundTrackLayers.future.push(layer);
        });
      })
      .catch(err => console.warn("Ground track error:", err));
  };

  // ----------------------------------------------------------------
  // Pass predictions
  // ----------------------------------------------------------------
  window.loadPasses = function (satId) {
    fetch(`/api/satellite/${encodeURIComponent(satId)}/passes?n=5&horizon=5`)
      .then(r => r.json())
      .then(data => renderPasses(data.passes || []))
      .catch(err => console.warn("Passes error:", err));
  };

  function renderPasses(passes) {
    const list = document.getElementById("passes-list");
    if (!list) return;
    if (passes.length === 0) {
      list.innerHTML = '<div class="pass-none">Nicio trecere în 48h</div>';
      return;
    }
    list.innerHTML = passes.map((p, i) => {
      const aosTime = fmtPassTime(p.aos);
      const tcaTime = fmtPassTime(p.tca);
      const losTime = fmtPassTime(p.los);
      const durMin = Math.floor(p.duration_s / 60);
      const durSec = p.duration_s % 60;
      const elCls = p.tca_el > 45 ? "el-high" : p.tca_el > 20 ? "el-mid" : "el-low";
      return `<div class="pass-row">
        <div class="pass-num">#${i + 1}</div>
        <div class="pass-cells">
          <div class="pass-cell"><span class="pass-lbl">AOS</span><span class="pass-time">${aosTime}</span><span class="pass-az">${p.aos_az}°</span></div>
          <div class="pass-cell"><span class="pass-lbl">TCA</span><span class="pass-time">${tcaTime}</span><span class="pass-az ${elCls}">El ${p.tca_el}°</span></div>
          <div class="pass-cell"><span class="pass-lbl">LOS</span><span class="pass-time">${losTime}</span><span class="pass-az">${p.los_az}°</span></div>
        </div>
        <div class="pass-dur">${durMin}m${durSec.toString().padStart(2,"0")}s</div>
      </div>`;
    }).join("");
  }

  function fmtPassTime(isoStr) {
    if (!isoStr) return "—";
    try {
      const d = new Date(isoStr);
      // Display in local time
      return d.toLocaleTimeString("ro-RO", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
             + " " + d.toLocaleDateString("ro-RO", { day: "2-digit", month: "2-digit" });
    } catch (_) { return isoStr.slice(11, 19); }
  }

  // ----------------------------------------------------------------
  // Observer geometry display
  // ----------------------------------------------------------------
  window.updateObserverGeo = function (obs) {
    if (!obs) return;
    const azEl = document.getElementById("pos-az");
    const elEl = document.getElementById("pos-el");
    const distEl = document.getElementById("pos-dist");
    const dopEl = document.getElementById("pos-doppler");

    if (azEl) azEl.textContent = obs.az != null ? obs.az.toFixed(1) + "°" : "—";
    if (elEl) {
      elEl.textContent = obs.el != null ? obs.el.toFixed(1) + "°" : "—";
      if (elEl && obs.above_horizon !== undefined) {
        elEl.style.color = obs.above_horizon ? "var(--green)" : "var(--text-dim)";
      }
    }
    if (distEl) distEl.textContent = obs.dist_km != null ? obs.dist_km.toFixed(0) + " km" : "—";

    // Doppler based on ISS downlink 145.800 MHz
    if (dopEl && obs.range_rate_km_s != null) {
      const freq_hz = 145.8e6;
      const delta_hz = -freq_hz * obs.range_rate_km_s / 299792.458;
      const sign = delta_hz >= 0 ? "+" : "";
      dopEl.textContent = sign + delta_hz.toFixed(0) + " Hz";
      dopEl.style.color = delta_hz > 0 ? "var(--green)" : delta_hz < 0 ? "var(--orange)" : "var(--accent)";
    }

    // Draw polar plot
    drawPolarPlot(obs.az, obs.el);
  };

  // ----------------------------------------------------------------
  // Polar plot
  // ----------------------------------------------------------------
  window.drawPolarPlot = function (az, el) {
    const canvas = document.getElementById("polar-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width;
    const H = canvas.height;
    const cx = W / 2;
    const cy = H / 2;
    const R = (W / 2) - 10;

    ctx.clearRect(0, 0, W, H);

    // Background
    ctx.fillStyle = "#0a0e14";
    ctx.fillRect(0, 0, W, H);

    // Elevation circles: 0° (outer), 30°, 60°, 90° (center)
    [0, 30, 60, 90].forEach(deg => {
      const r = R * (1 - deg / 90);
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, 2 * Math.PI);
      ctx.strokeStyle = deg === 0 ? "rgba(0,212,255,0.4)" : "rgba(0,212,255,0.15)";
      ctx.lineWidth = 1;
      ctx.stroke();
      if (deg > 0 && deg < 90) {
        ctx.fillStyle = "rgba(0,212,255,0.35)";
        ctx.font = "9px monospace";
        ctx.fillText(deg + "°", cx + r + 2, cy - 2);
      }
    });

    // Crosshairs
    ctx.strokeStyle = "rgba(0,212,255,0.15)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy); ctx.stroke();

    // Cardinal labels
    ctx.fillStyle = "rgba(0,212,255,0.6)";
    ctx.font = "bold 10px monospace";
    ctx.textAlign = "center";
    ctx.fillText("N", cx, cy - R - 3);
    ctx.fillText("S", cx, cy + R + 12);
    ctx.textAlign = "left";
    ctx.fillText("E", cx + R + 3, cy + 4);
    ctx.textAlign = "right";
    ctx.fillText("V", cx - R - 3, cy + 4);
    ctx.textAlign = "center";

    // Satellite dot
    if (az != null && el != null) {
      const elClamped = Math.max(0, Math.min(90, el));
      const r_dot = R * (1 - elClamped / 90);
      const az_rad = (az - 90) * Math.PI / 180;  // convert to canvas angle (N=up, E=right)
      const sx = cx + r_dot * Math.cos(az_rad);
      const sy = cy + r_dot * Math.sin(az_rad);

      // Glow
      const grd = ctx.createRadialGradient(sx, sy, 0, sx, sy, 10);
      grd.addColorStop(0, el > 0 ? "rgba(34,255,136,0.8)" : "rgba(100,120,140,0.5)");
      grd.addColorStop(1, "rgba(0,0,0,0)");
      ctx.beginPath();
      ctx.arc(sx, sy, 10, 0, 2 * Math.PI);
      ctx.fillStyle = grd;
      ctx.fill();

      // Dot
      ctx.beginPath();
      ctx.arc(sx, sy, 4, 0, 2 * Math.PI);
      ctx.fillStyle = el > 0 ? "#22ff88" : "#4a6080";
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  };

  // ----------------------------------------------------------------
  // Expose __MAP reference for ground track layer management
  // ----------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    // Wait for map to be initialized then grab reference
    setTimeout(() => {
      // Walk leaflet instances to find our map
      const mapEl = document.getElementById("map");
      if (mapEl && mapEl._leaflet_map) {
        window.__MAP = mapEl._leaflet_map;
      } else {
        // Try getting from L.map instances
        // The main IIFE exposes 'map' locally; use a MutationObserver workaround
        // Instead, we rely on the 'map' being assigned to window by patching:
        // We'll intercept after DOMContentLoaded + 400ms
      }
    }, 400);

    loadObserver();

    // Draw empty polar plot on load
    setTimeout(() => drawPolarPlot(null, null), 500);

    // Keyboard enter on observer search
    const searchInput = document.getElementById("obs-search");
    if (searchInput) {
      searchInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") geocodeObserver();
      });
    }
  });

})();
