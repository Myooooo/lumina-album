/* ==========================================================================
   Lumina Album - iOS Photos-style Slippy Tile Map for Footprints
   Zero-dependency Web Mercator (EPSG:3857) XYZ tile map engine with:
   - Keyless default CartoDB vector-style tiles (auto light/dark per theme)
   - Full support for OSM, AMap (with automatic WGS-84 -> GCJ-02 offset fix),
     and custom XYZ tile templates configured in Settings -> 地点
   - iOS Photos-style square thumbnail pins with photo count badges
   - Dynamic screen-space clustering on zoom out, smooth split on zoom in
   - Auto fitBounds framing all places that have photos on initial open
   - Floating glass/paper/vintage photo drawer on pin click
   ========================================================================== */
"use strict";

const fpMapState = {
  mounted: false,
  places: [],
  bounds: null,
  zoom: 4,
  centerLat: 35.0,
  centerLon: 105.0,
  dragging: false,
  dragStartX: 0,
  dragStartY: 0,
  dragStartWorldX: 0,
  dragStartWorldY: 0,
  tileCache: new Map(), // key -> img element
  tileLayerKey: "",
  activeDrawerKey: null,
  resizeObserver: null,
  animToken: 0,
};

const MAP_TILE_SIZE = 256;
const MAP_MIN_ZOOM = 2;
const MAP_MAX_ZOOM = 18;
const MAP_CLUSTER_RADIUS_PX = 68;

/* --------------------------------------------------------------------------
   Coordinate math: WGS-84 <-> GCJ-02 (for AMap tiles) & Web Mercator
   -------------------------------------------------------------------------- */

function _fpOutOfChina(lat, lon) {
  return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271;
}

function _fpTransformLat(x, y) {
  let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
  ret += ((20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin((y / 3.0) * Math.PI)) * 2.0) / 3.0;
  ret += ((160.0 * Math.sin((y / 12.0) * Math.PI) + 320.0 * Math.sin((y * Math.PI) / 30.0)) * 2.0) / 3.0;
  return ret;
}

function _fpTransformLon(x, y) {
  let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0;
  ret += ((20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin((x / 3.0) * Math.PI)) * 2.0) / 3.0;
  ret += ((150.0 * Math.sin((x / 12.0) * Math.PI) + 300.0 * Math.sin((x / 30.0) * Math.PI)) * 2.0) / 3.0;
  return ret;
}

function fpWgs84ToGcj02(lat, lon) {
  if (_fpOutOfChina(lat, lon)) return [lat, lon];
  const ee = 0.00669342162296594323;
  const a = 6378245.0;
  let dLat = _fpTransformLat(lon - 105.0, lat - 35.0);
  let dLon = _fpTransformLon(lon - 105.0, lat - 35.0);
  const radLat = (lat / 180.0) * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - ee * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / (((a * (1 - ee)) / (magic * sqrtMagic)) * Math.PI);
  dLon = (dLon * 180.0) / ((a / sqrtMagic) * Math.cos(radLat) * Math.PI);
  return [lat + dLat, lon + dLon];
}

function fpDisplayCoords(lat, lon) {
  const provider = (state.mapTileProvider || "amap").toLowerCase();
  // Only AMap tiles use GCJ-02; OSM, Carto, and standard custom tiles use WGS-84.
  if (provider === "amap") {
    return fpWgs84ToGcj02(lat, lon);
  }
  return [lat, lon];
}

function fpLatLonToWorld(lat, lon, zoom) {
  const clampedLat = Math.max(-85.05112878, Math.min(85.05112878, lat));
  const scale = MAP_TILE_SIZE * Math.pow(2, zoom);
  const x = ((lon + 180) / 360) * scale;
  const sinLat = Math.sin((clampedLat * Math.PI) / 180);
  const y = (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * scale;
  return { x, y };
}

function fpWorldToLatLon(x, y, zoom) {
  const scale = MAP_TILE_SIZE * Math.pow(2, zoom);
  const lon = (x / scale) * 360 - 180;
  const n = Math.PI - (2 * Math.PI * y) / scale;
  const lat = (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
  return {
    lat: Math.max(-85.05112878, Math.min(85.05112878, lat)),
    lon: ((lon + 540) % 360) - 180,
  };
}

/* --------------------------------------------------------------------------
   Tile Provider Resolution (Default: AMap; OSM as the keyless international fallback)
   -------------------------------------------------------------------------- */

function fpResolveTileSpec() {
  const provider = (state.mapTileProvider || "amap").toLowerCase();
  const theme = document.body ? document.body.dataset.theme || "paper" : "paper";
  const retina = window.devicePixelRatio > 1.25 ? "@2x" : "";

  if (provider === "custom" && (state.mapTileUrl || "").trim()) {
    const rawUrl = state.mapTileUrl.trim();
    const apiKey = encodeURIComponent((state.mapApiKey || "").trim());
    return {
      key: `custom:${rawUrl}:${theme}`,
      provider: "custom",
      attribution: "自定义地图瓦片",
      needsDarkFilter: theme === "vintage",
      urlFor(z, x, y) {
        const sub = ["a", "b", "c"][Math.abs(x + y) % 3];
        return rawUrl
          .replace(/\{z\}/g, String(z))
          .replace(/\{x\}/g, String(x))
          .replace(/\{y\}/g, String(y))
          .replace(/\{s\}/g, sub)
          .replace(/\{r\}/g, retina)
          .replace(/\{apikey\}/gi, apiKey);
      },
    };
  }

  if (provider === "amap") {
    return {
      key: `amap:${theme}`,
      provider: "amap",
      attribution: "© 高德地图",
      needsDarkFilter: theme === "vintage",
      urlFor(z, x, y) {
        const sub = (Math.abs(x + y) % 4) + 1;
        return `https://webrd0${sub}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=${x}&y=${y}&z=${z}`;
      },
    };
  }

  if (provider === "osm") {
    return {
      key: `osm:${theme}`,
      provider: "osm",
      attribution: "© OpenStreetMap contributors",
      needsDarkFilter: theme === "vintage",
      urlFor(z, x, y) {
        return `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;
      },
    };
  }

  // Legacy CartoDB configs keep working; new installs use AMap above.
  const style = theme === "vintage" ? "dark_all" : "rastertiles/voyager";
  const apiKey = (state.mapApiKey || "").trim();
  return {
    key: `carto:${style}:${apiKey}`,
    provider: "carto",
    attribution: "© OpenStreetMap · © CARTO",
    needsDarkFilter: false,
    needsKey: !apiKey,
    urlFor(z, x, y) {
      if (!apiKey) return "";
      const sub = ["a", "b", "c", "d"][Math.abs(x + y) % 4];
      return `https://${sub}.basemaps.cartocdn.com/${style}/${z}/${x}/${y}${retina}.png?api_key=${encodeURIComponent(apiKey)}`;
    },
  };
}

/* --------------------------------------------------------------------------
   Auto Fit Bounds (Default view covers all places that have photos)
   -------------------------------------------------------------------------- */

function fpPlacesWithCoords(places) {
  return (places || []).filter(
    (p) => p.lat != null && p.lon != null && Number.isFinite(Number(p.lat)) && Number.isFinite(Number(p.lon))
  );
}

function fitFootprintsMapToPlaces(places) {
  const canvas = $("footprintsMapCanvas");
  const w = canvas ? canvas.clientWidth || 960 : 960;
  const h = canvas ? canvas.clientHeight || 560 : 560;
  const withCoords = fpPlacesWithCoords(places);
  const active = withCoords.filter((p) => p.count > 0);
  const targets = active.length ? active : withCoords;

  if (!targets.length) {
    fpMapState.centerLat = 35.0;
    fpMapState.centerLon = 105.0;
    fpMapState.zoom = 4;
    return;
  }

  const pts = targets.map((p) => fpDisplayCoords(Number(p.lat), Number(p.lon)));
  if (pts.length === 1) {
    fpMapState.centerLat = pts[0][0];
    fpMapState.centerLon = pts[0][1];
    fpMapState.zoom = 10;
    return;
  }

  let minLat = Infinity;
  let maxLat = -Infinity;
  let minLon = Infinity;
  let maxLon = -Infinity;
  for (const [lat, lon] of pts) {
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
  }

  const padX = Math.min(120, Math.max(72, w * 0.12));
  const padY = Math.min(120, Math.max(72, h * 0.14));
  const availW = Math.max(120, w - padX * 2);
  const availH = Math.max(120, h - padY * 2);

  let chosenZoom = MAP_MIN_ZOOM;
  for (let z = 13; z >= MAP_MIN_ZOOM; z -= 1) {
    const nw = fpLatLonToWorld(maxLat, minLon, z);
    const se = fpLatLonToWorld(minLat, maxLon, z);
    const spanX = Math.abs(se.x - nw.x);
    const spanY = Math.abs(se.y - nw.y);
    if (spanX <= availW && spanY <= availH) {
      chosenZoom = z;
      break;
    }
  }

  const nw = fpLatLonToWorld(maxLat, minLon, chosenZoom);
  const se = fpLatLonToWorld(minLat, maxLon, chosenZoom);
  const center = fpWorldToLatLon((nw.x + se.x) / 2, (nw.y + se.y) / 2, chosenZoom);
  fpMapState.centerLat = center.lat;
  fpMapState.centerLon = center.lon;
  fpMapState.zoom = chosenZoom;
}

/* --------------------------------------------------------------------------
   Map DOM Setup, Tile Rendering & iOS-style Photo Pin Clustering
   -------------------------------------------------------------------------- */

function ensureFootprintsMapMounted() {
  const canvas = $("footprintsMapCanvas");
  if (!canvas || fpMapState.mounted) return;
  fpMapState.mounted = true;

  canvas.innerHTML = `
    <div class="fp-map-tiles" id="fpMapTiles" aria-hidden="true"></div>
    <div class="fp-map-pins" id="fpMapPins"></div>
    <div class="fp-map-zoom-stack">
      <button type="button" class="fp-map-zoom-btn" id="fpMapZoomIn" title="放大" aria-label="放大">+</button>
      <button type="button" class="fp-map-zoom-btn" id="fpMapZoomOut" title="缩小" aria-label="缩小">−</button>
    </div>
    <div class="fp-map-attribution" id="fpMapAttribution"></div>
    <div class="fp-map-empty-toast hidden" id="fpMapEmptyToast"></div>
  `;

  // Zoom buttons
  const zin = $("fpMapZoomIn");
  const zout = $("fpMapZoomOut");
  if (zin) zin.addEventListener("click", () => fpMapStepZoom(1));
  if (zout) zout.addEventListener("click", () => fpMapStepZoom(-1));

  // Fit bounds button
  const fitBtn = $("footprintsMapFitBtn");
  if (fitBtn) {
    fitBtn.addEventListener("click", () => {
      const before = {
        lat: fpMapState.centerLat,
        lon: fpMapState.centerLon,
        zoom: fpMapState.zoom,
      };
      fitFootprintsMapToPlaces(fpMapState.places);
      const target = {
        lat: fpMapState.centerLat,
        lon: fpMapState.centerLon,
        zoom: fpMapState.zoom,
      };
      fpMapState.centerLat = before.lat;
      fpMapState.centerLon = before.lon;
      fpMapState.zoom = before.zoom;
      fpAnimateView(target.lat, target.lon, target.zoom, 460);
    });
  }

  // Mouse pan
  canvas.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest("button") || e.target.closest(".fp-map-pin")) return;
    fpMapState.animToken += 1;
    fpMapState.dragging = true;
    fpMapState.dragStartX = e.clientX;
    fpMapState.dragStartY = e.clientY;
    const centerW = fpLatLonToWorld(fpMapState.centerLat, fpMapState.centerLon, fpMapState.zoom);
    fpMapState.dragStartWorldX = centerW.x;
    fpMapState.dragStartWorldY = centerW.y;
    canvas.classList.add("is-dragging");
    e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (!fpMapState.dragging) return;
    const dx = e.clientX - fpMapState.dragStartX;
    const dy = e.clientY - fpMapState.dragStartY;
    const nextCenter = fpWorldToLatLon(
      fpMapState.dragStartWorldX - dx,
      fpMapState.dragStartWorldY - dy,
      fpMapState.zoom
    );
    fpMapState.centerLat = nextCenter.lat;
    fpMapState.centerLon = nextCenter.lon;
    fpMapDraw();
  });

  window.addEventListener("mouseup", () => {
    if (!fpMapState.dragging) return;
    fpMapState.dragging = false;
    canvas.classList.remove("is-dragging");
  });

  // Mouse wheel zoom centered on cursor
  canvas.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const delta = e.deltaY < 0 ? 1 : -1;
      const rect = canvas.getBoundingClientRect();
      fpMapStepZoom(delta, e.clientX - rect.left, e.clientY - rect.top);
    },
    { passive: false }
  );

  // Double-click zoom in
  canvas.addEventListener("dblclick", (e) => {
    if (e.target.closest("button") || e.target.closest(".fp-map-pin")) return;
    const rect = canvas.getBoundingClientRect();
    fpMapStepZoom(1, e.clientX - rect.left, e.clientY - rect.top);
  });

  // Touch pan
  let touchStartX = 0;
  let touchStartY = 0;
  let touchWorldX = 0;
  let touchWorldY = 0;
  canvas.addEventListener(
    "touchstart",
    (e) => {
      if (e.touches.length !== 1 || e.target.closest("button") || e.target.closest(".fp-map-pin")) return;
      fpMapState.animToken += 1;
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      const c = fpLatLonToWorld(fpMapState.centerLat, fpMapState.centerLon, fpMapState.zoom);
      touchWorldX = c.x;
      touchWorldY = c.y;
    },
    { passive: true }
  );
  canvas.addEventListener(
    "touchmove",
    (e) => {
      if (e.touches.length !== 1) return;
      e.preventDefault();
      const dx = e.touches[0].clientX - touchStartX;
      const dy = e.touches[0].clientY - touchStartY;
      const nextCenter = fpWorldToLatLon(touchWorldX - dx, touchWorldY - dy, fpMapState.zoom);
      fpMapState.centerLat = nextCenter.lat;
      fpMapState.centerLon = nextCenter.lon;
      fpMapDraw();
    },
    { passive: false }
  );

  // Resize observer so map redraws cleanly on window resize
  if (typeof ResizeObserver === "function") {
    fpMapState.resizeObserver = new ResizeObserver(() => {
      if (fpState.mode === "map" && !state.footprintPlace) fpMapDraw();
    });
    fpMapState.resizeObserver.observe(canvas);
  }
}

function fpMapStepZoom(delta, anchorPxX, anchorPxY) {
  fpMapState.animToken += 1;
  const canvas = $("footprintsMapCanvas");
  const nextZoom = Math.max(MAP_MIN_ZOOM, Math.min(MAP_MAX_ZOOM, Math.round(fpMapState.zoom) + delta));
  if (nextZoom === fpMapState.zoom) return;
  if (canvas && anchorPxX !== undefined && anchorPxY !== undefined) {
    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    const oldCenter = fpLatLonToWorld(fpMapState.centerLat, fpMapState.centerLon, fpMapState.zoom);
    const cursorWorldX = oldCenter.x + (anchorPxX - w / 2);
    const cursorWorldY = oldCenter.y + (anchorPxY - h / 2);
    const cursorGeo = fpWorldToLatLon(cursorWorldX, cursorWorldY, fpMapState.zoom);
    const newCursorWorld = fpLatLonToWorld(cursorGeo.lat, cursorGeo.lon, nextZoom);
    const newCenterWorldX = newCursorWorld.x - (anchorPxX - w / 2);
    const newCenterWorldY = newCursorWorld.y - (anchorPxY - h / 2);
    const nextCenter = fpWorldToLatLon(newCenterWorldX, newCenterWorldY, nextZoom);
    fpMapState.centerLat = nextCenter.lat;
    fpMapState.centerLon = nextCenter.lon;
  }
  fpMapState.zoom = nextZoom;
  fpMapDraw();
}

function fpAnimateView(targetLat, targetLon, targetZoom, duration = 420, done = null) {
  const startLat = fpMapState.centerLat;
  const startLon = fpMapState.centerLon;
  const startZoom = fpMapState.zoom;
  const endZoom = Math.max(MAP_MIN_ZOOM, Math.min(MAP_MAX_ZOOM, targetZoom));
  const token = ++fpMapState.animToken;
  const reduced =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || duration <= 0) {
    fpMapState.centerLat = targetLat;
    fpMapState.centerLon = targetLon;
    fpMapState.zoom = endZoom;
    fpMapDraw();
    if (done) done();
    return;
  }
  const started = performance.now();
  const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
  const frame = (now) => {
    if (token !== fpMapState.animToken) return;
    const k = Math.min(1, (now - started) / duration);
    const e = ease(k);
    fpMapState.centerLat = startLat + (targetLat - startLat) * e;
    fpMapState.centerLon = startLon + (targetLon - startLon) * e;
    fpMapState.zoom = startZoom + (endZoom - startZoom) * e;
    fpMapDraw();
    if (k < 1) requestAnimationFrame(frame);
    else if (done) done();
  };
  requestAnimationFrame(frame);
}

function fpClusterPins(projected) {
  const sorted = projected.slice().sort((a, b) => b.place.count - a.place.count);
  const clusters = [];
  for (const item of sorted) {
    let hit = null;
    for (const cl of clusters) {
      const dist = Math.hypot(item.sx - cl.sx, item.sy - cl.sy);
      if (dist <= MAP_CLUSTER_RADIUS_PX) {
        hit = cl;
        break;
      }
    }
    if (hit) {
      hit.members.push(item);
      hit.totalCount += item.place.count;
      // Weighted centroid keeps cluster marker centered nicely
      const n = hit.members.length;
      hit.sx = (hit.sx * (n - 1) + item.sx) / n;
      hit.sy = (hit.sy * (n - 1) + item.sy) / n;
    } else {
      clusters.push({
        sx: item.sx,
        sy: item.sy,
        totalCount: item.place.count,
        primary: item.place,
        primaryIndex: item.index,
        members: [item],
      });
    }
  }
  return clusters;
}

function fpMapDraw() {
  const canvas = $("footprintsMapCanvas");
  const tilesEl = $("fpMapTiles");
  const pinsEl = $("fpMapPins");
  const attrEl = $("fpMapAttribution");
  const emptyEl = $("fpMapEmptyToast");
  if (!canvas || !tilesEl || !pinsEl) return;

  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;

  const spec = fpResolveTileSpec();
  const keyHint = $("footprintsMapKeyHint");
  if (keyHint) keyHint.classList.toggle("hidden", !spec.needsKey);
  if (fpMapState.tileLayerKey !== spec.key) {
    fpMapState.tileLayerKey = spec.key;
    fpMapState.tileCache.clear();
    tilesEl.innerHTML = "";
  }
  tilesEl.classList.toggle("is-dark-filter", !!spec.needsDarkFilter);
  if (attrEl) attrEl.textContent = spec.attribution;

  const z = fpMapState.zoom;
  // Always request integer XYZ tile zoom while smoothly scaling during fractional fly-to animation.
  const tileZ = Math.max(MAP_MIN_ZOOM, Math.min(MAP_MAX_ZOOM, Math.floor(z)));
  const tileScale = Math.pow(2, z - tileZ);
  const tilePx = MAP_TILE_SIZE * tileScale;
  const numTiles = Math.pow(2, tileZ);
  const centerW = fpLatLonToWorld(fpMapState.centerLat, fpMapState.centerLon, z);
  const topLeftX = centerW.x - w / 2;
  const topLeftY = centerW.y - h / 2;

  const minTileX = Math.floor(topLeftX / tilePx) - 1;
  const maxTileX = Math.floor((topLeftX + w) / tilePx) + 1;
  const minTileY = Math.max(0, Math.floor(topLeftY / tilePx) - 1);
  const maxTileY = Math.min(numTiles - 1, Math.floor((topLeftY + h) / tilePx) + 1);

  const needed = new Set();
  for (let ty = minTileY; ty <= maxTileY; ty += 1) {
    for (let tx = minTileX; tx <= maxTileX; tx += 1) {
      const wrappedX = ((tx % numTiles) + numTiles) % numTiles;
      const key = `${tileZ}/${tx}/${ty}`;
      needed.add(key);
      let img = fpMapState.tileCache.get(key);
      if (!img) {
        img = document.createElement("img");
        img.className = "fp-map-tile";
        img.alt = "";
        img.decoding = "async";
        img.draggable = false;
        const tileUrl = spec.urlFor(tileZ, wrappedX, ty);
        if (!tileUrl) continue;
        img.src = tileUrl;
        img.addEventListener("load", () => img.classList.add("is-loaded"), { once: true });
        fpMapState.tileCache.set(key, img);
        tilesEl.appendChild(img);
      }
      const px = tx * tilePx - topLeftX;
      const py = ty * tilePx - topLeftY;
      img.style.transformOrigin = "0 0";
      img.style.transform = tileScale === 1
        ? `translate3d(${Math.round(px)}px, ${Math.round(py)}px, 0)`
        : `translate3d(${px.toFixed(1)}px, ${py.toFixed(1)}px, 0) scale(${tileScale.toFixed(4)})`;
    }
  }

  for (const [key, img] of fpMapState.tileCache.entries()) {
    if (!needed.has(key)) {
      img.remove();
      fpMapState.tileCache.delete(key);
    }
  }

  // Project and cluster photo pins
  const projected = [];
  (fpMapState.places || []).forEach((place, index) => {
    if (place.lat == null || place.lon == null) return;
    const [dLat, dLon] = fpDisplayCoords(Number(place.lat), Number(place.lon));
    const wp = fpLatLonToWorld(dLat, dLon, z);
    const sx = wp.x - topLeftX;
    const sy = wp.y - topLeftY;
    if (sx < -80 || sx > w + 80 || sy < -80 || sy > h + 80) return;
    projected.push({ place, index, sx, sy, lat: dLat, lon: dLon });
  });

  if (emptyEl) {
    const totalWithCoords = fpPlacesWithCoords(fpMapState.places).length;
    if (!totalWithCoords) {
      emptyEl.textContent = "当前还没有带坐标的足迹，点右上角「新建地点」即可反查坐标并标注到地图上。";
      emptyEl.classList.remove("hidden");
    } else {
      emptyEl.classList.add("hidden");
    }
  }

  const clusters = fpClusterPins(projected);
  pinsEl.innerHTML = clusters
    .map((cl, cIdx) => {
      const p = cl.primary;
      const cover = p.cover || {};
      const imgHtml = cover.id
        ? `<img src="/api/thumbnail/${cover.id}?v=${encodeURIComponent(cover.thumb_version || "")}" alt="" loading="lazy" decoding="async" />`
        : `<span class="fp-pin-fallback">${ICONS.pin}</span>`;
      const isCluster = cl.members.length > 1;
      const label = isCluster
        ? `${placeDisplayName(p)} 等 ${cl.members.length} 处`
        : placeDisplayName(p);
      const isSelected = !isCluster && fpMapState.activeDrawerKey === p.key;
      return `
        <button type="button"
                class="fp-map-pin${isCluster ? " is-cluster" : ""}${isSelected ? " is-selected" : ""}${p.source === "manual" && !isCluster ? " is-manual" : ""}"
                data-cluster-idx="${cIdx}"
                style="left:${cl.sx.toFixed(1)}px;top:${cl.sy.toFixed(1)}px"
                title="${escapeHtml(label)} · ${cl.totalCount} 张">
          <span class="fp-pin-frame">
            ${imgHtml}
            <span class="fp-pin-badge">${cl.totalCount}</span>
          </span>
          <span class="fp-pin-label">${escapeHtml(label)}</span>
          <span class="fp-pin-tail" aria-hidden="true"></span>
        </button>`;
    })
    .join("");

  pinsEl.querySelectorAll(".fp-map-pin").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const cl = clusters[Number(btn.dataset.clusterIdx)];
      if (!cl) return;
      if (cl.members.length > 1 && fpMapState.zoom < MAP_MAX_ZOOM) {
        // Fly to the cluster instead of hard-cutting to a new zoom level.
        let sumLat = 0;
        let sumLon = 0;
        for (const m of cl.members) {
          sumLat += m.lat;
          sumLon += m.lon;
        }
        fpAnimateView(
          sumLat / cl.members.length,
          sumLon / cl.members.length,
          Math.min(MAP_MAX_ZOOM, fpMapState.zoom + 2)
        );
        return;
      }
      const p = cl.primary;
      if (p.lat != null && p.lon != null) {
        const [dLat, dLon] = fpDisplayCoords(Number(p.lat), Number(p.lon));
        fpAnimateView(dLat, dLon, Math.max(fpMapState.zoom, 14), 420, () =>
          openFootprintsMapDrawer(cl.primary, cl.primaryIndex)
        );
      } else {
        openFootprintsMapDrawer(cl.primary, cl.primaryIndex);
      }
    });
  });
}

function closeFootprintsMapDrawer() {
  fpMapState.activeDrawerKey = null;
  const drawer = $("footprintsMapDrawer");
  if (drawer) {
    drawer.classList.add("hidden");
    drawer.innerHTML = "";
  }
  fpMapDraw();
}

function openFootprintsMapDrawer(place, placeIndex) {
  const drawer = $("footprintsMapDrawer");
  if (!drawer || !place) return;
  fpMapState.activeDrawerKey = place.key;
  const photos = place.photos || [];
  adoptPhotos(photos);
  const label = placeDisplayName(place);
  const coords =
    place.lat != null && place.lon != null
      ? `${Number(place.lat).toFixed(4)}, ${Number(place.lon).toFixed(4)}`
      : "";
  const previewPhotos = photos.slice(0, 12);

  drawer.innerHTML = `
    <div class="fp-drawer-head">
      <div class="fp-drawer-title-wrap">
        <h3>${escapeHtml(label)}${place.source === "manual" ? ' <span class="fp-drawer-tag">自建</span>' : ""}</h3>
        <p>${place.count} 张照片${coords ? ` · ${coords}` : ""}</p>
      </div>
      <button type="button" class="btn icon-only small" id="fpDrawerCloseBtn" title="关闭">${ICONS.close}</button>
    </div>
    <div class="fp-drawer-grid">
      ${
        previewPhotos.length
          ? previewPhotos
              .map(
                (p) => `
            <button type="button" class="fp-drawer-thumb" data-drawer-photo="${p.id}" title="${escapeHtml(p.filename || "")}">
              <img src="${thumbnailUrl(p)}" alt="" loading="lazy" decoding="async" />
            </button>`
              )
              .join("")
          : '<p class="fp-drawer-empty">该地点暂无照片</p>'
      }
    </div>
    <div class="fp-drawer-actions">
      <button type="button" class="btn small primary" id="fpDrawerOpenPlaceBtn">进入地点${place.source === "manual" ? "管理" : "查看全部"}</button>
      ${place.source === "manual" ? `<button type="button" class="btn small" id="fpDrawerAddPhotosBtn">${ICONS.image}<span>添加照片</span></button>` : ""}
    </div>
  `;
  drawer.classList.remove("hidden");

  const closeBtn = $("fpDrawerCloseBtn");
  if (closeBtn) closeBtn.addEventListener("click", closeFootprintsMapDrawer);
  const openBtn = $("fpDrawerOpenPlaceBtn");
  if (openBtn) {
    openBtn.addEventListener("click", () => {
      closeFootprintsMapDrawer();
      openPlace(placeIndex);
    });
  }
  const addBtn = $("fpDrawerAddPhotosBtn");
  if (addBtn) {
    addBtn.addEventListener("click", () => {
      state.footprintPlace = place;
      openPlacePhotoPicker(place);
    });
  }
  drawer.querySelectorAll("[data-drawer-photo]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const pid = Number(btn.dataset.drawerPhoto);
      if (!Number.isFinite(pid)) return;
      openPreview(pid, {
        ids: photos.map((p) => p.id),
        title: label,
      });
    });
  });

  fpMapDraw();
}

function renderFootprintsMap(places, bounds) {
  fpMapState.places = places || [];
  fpMapState.bounds = bounds || null;
  fpMapState.activeDrawerKey = null;
  const drawer = $("footprintsMapDrawer");
  if (drawer) drawer.classList.add("hidden");
  ensureFootprintsMapMounted();
  // Defer fitBounds one frame so container dimensions are laid out after unhiding.
  requestAnimationFrame(() => {
    fitFootprintsMapToPlaces(fpMapState.places);
    fpMapDraw();
  });
}

function refreshFootprintsMapLayer() {
  if (!fpMapState.mounted) return;
  if (state.view === "footprints" && fpState.mode === "map" && !state.footprintPlace) {
    fpMapDraw();
  }
}
