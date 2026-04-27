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
  }

  function updateStationMarkerStates(activeIds) {
    activeStationIds = new Set(activeIds);
    window.__ACTIVE_STATION_IDS = activeStationIds;
    stationsData.forEach(st => {
      const m = stationMarkers[st.id];
      if (!m) return;
      const isActive = activeStationIds.has(st.id);
      const isNew = !!st.is_new;
      m.setIcon(makeStationIcon(st.online, isActive, isNew));
      m.setZIndexOffset(isActive ? 1000 : (isNew ? 500 : 0));
    });
    document.getElementById("active-count").textContent = activeIds.length;
  }

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

    // Clear old satellite marker
    clearSatelliteFromMap();

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
