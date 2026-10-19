'use strict';
/*
 * 3D 视图：MapLibre GL 渲染（内置在 static/vendor，无密钥、无 CDN）。
 *
 * 轨迹用普通线绘制：连续、干净、随地图倾斜。颜色按 GPS 海拔从青到红。
 * 可选叠加 DEM 地形（AWS Terrain Tiles）让地面真的起伏 —— 4.x 在栅格底图上开地形会把
 * 瓦片重复错位绘制，已升级到 MapLibre 5。
 */
const TraceMap3D = (() => {
  const MAX_SEGMENTS = 1400;      // 一条长轨迹按海拔分段着色，最多这么多段

  let map = null;
  let route = null;               // 选中的一次活动 {id, segments, altitudes}
  let others = [];                // 其他活动的折线 [{id, line}]
  let options = {
    pitch: 55,
    bearing: 0,
    dimOthers: false,
    precisionPx: 1.2,
    solidColor: null,      // 设了就按这个单色画，不用海拔渐变             // 其他轨迹的抽稀粒度（屏幕像素）
    terrain: false,
    terrainExaggeration: 1.6,     // 地形强度固定，不再开放给界面
  };
  let onMove = null;
  let onPick = null;
  let visible = false;
  let basemapKey = null;
  let loadedBox = null;           // 已加载数据的经纬度范围，避免平移时重复请求

  function styleFor(spec) {
    return {
      version: 8,
      sources: {
        base: {
          type: 'raster',
          tiles: [`/api/tiles/${spec.key}/{z}/{x}/{y}.png`],
          tileSize: 256,
          maxzoom: spec.max_native_zoom || 19,
          attribution: spec.attribution || '',
        },
        terrain: {
          type: 'raster-dem',
          tiles: ['/api/tiles/terrain/{z}/{x}/{y}.png'],
          tileSize: 256,
          encoding: 'terrarium',
          maxzoom: 15,
          attribution: '高程 © Mapzen / AWS Terrain Tiles',
        },
      },
      layers: [
        {id: 'backdrop', type: 'background', paint: {'background-color': '#070808'}},
        {
          id: 'base', type: 'raster', source: 'base',
          // 路网图是浅色的，压暗到和深色界面一致；卫星图只轻微压一点。
          // 路网图压暗：亮度 + 半透明叠在近乎黑的背景上，两条一起用力才稳定变黑。
          paint: spec.dark
            ? {'raster-brightness-max': 0.16, 'raster-contrast': 0.5, 'raster-saturation': -0.7,
               'raster-opacity': 0.55}
            : {'raster-brightness-max': 0.82},
        },
      ],
    };
  }

  // 选中那条轨迹：按海拔分段上色。相邻段共用端点，配圆头连接看不出接头。
  function buildTrail(data) {
    const features = [];
    if (!data || !data.segments) return {type: 'FeatureCollection', features};
    const altitudes = data.altitudes || [];
    const values = [];
    for (const alts of altitudes) {
      for (const value of alts || []) if (typeof value === 'number' && isFinite(value)) values.push(value);
    }
    let floor = Infinity, ceiling = -Infinity;
    for (const value of values) {
      if (value < floor) floor = value;
      if (value > ceiling) ceiling = value;
    }
    const hasHeight = values.length > 0 && isFinite(floor) && isFinite(ceiling);
    if (!hasHeight) floor = 0;
    const span = hasHeight ? Math.max(1, ceiling - floor) : 1;
    const total = data.segments.reduce((n, segment) => n + segment.length, 0);
    const step = Math.max(1, Math.ceil(total / MAX_SEGMENTS));

    for (let s = 0; s < data.segments.length; s++) {
      const raw = data.segments[s];
      const alts = altitudes[s] || [];
      if (!raw || raw.length < 2) continue;
      for (let i = 0; i + 1 < raw.length; i += step) {
        const j = Math.min(i + step, raw.length - 1);
        const heightA = typeof alts[i] === 'number' ? alts[i] : null;
        const heightB = typeof alts[j] === 'number' ? alts[j] : null;
        const average = ((heightA ?? heightB ?? floor) + (heightB ?? heightA ?? floor)) / 2;
        const ratio = hasHeight ? Math.max(0, Math.min(1, (average - floor) / span)) : 0;
        features.push({
          type: 'Feature',
          properties: {t: Math.sqrt(ratio), id: data.id != null ? data.id : null},
          geometry: {
            type: 'LineString',
            // 服务端给的是 [纬度, 经度]，GeoJSON 要 [经度, 纬度]。
            coordinates: raw.slice(i, j + 1).map(point => [point[1], point[0]]),
          },
        });
      }
    }
    return {type: 'FeatureCollection', features};
  }

  // 其他活动：淡蓝色细线。抽稀按"一像素"算，远景少存点、近景几乎保留全部，
  // 这样 3D 里的精度和足迹模式一致。
  function buildOthers(lines) {
    const features = [];
    const zoom = map ? map.getZoom() : 12;
    for (const entry of lines || []) {
      const line = entry && entry.line ? entry.line : entry;
      if (!line || line.length < 2) continue;
      const metreLat = 111320;
      const metreLon = 111320 * Math.cos(line[0][1] * Math.PI / 180) || 1;
      const minDistance = 156543.03392 * Math.cos(line[0][1] * Math.PI / 180)
        / Math.pow(2, zoom) * options.precisionPx;
      const points = [line[0]];
      let last = line[0];
      for (let i = 1; i < line.length; i++) {
        const point = line[i];
        const dx = (point[0] - last[0]) * metreLon;
        const dy = (point[1] - last[1]) * metreLat;
        if (Math.hypot(dx, dy) >= minDistance) {
          points.push(point);
          last = point;
        }
      }
      if (points[points.length - 1] !== line[line.length - 1]) points.push(line[line.length - 1]);
      if (points.length < 2) continue;
      features.push({
        type: 'Feature',
        properties: {id: entry && entry.id != null ? entry.id : null},
        geometry: {type: 'LineString', coordinates: points},
      });
    }
    return {type: 'FeatureCollection', features};
  }

  function trailPaint() {
    return {
      'line-color': options.solidColor || ['interpolate', ['linear'], ['coalesce', ['get', 't'], 0],
        0, '#4fd2f0', 0.25, '#6fd394', 0.5, '#e8d071', 0.75, '#e88f4f', 1, '#e05555'],
      'line-width': 3,
      'line-opacity': 0.95,
    };
  }

  // 选中某条轨迹时，其他轨迹退到背景里去。
  function applyOthers() {
    if (!map || !map.getLayer('others')) return;
    map.setPaintProperty('others', 'line-opacity', options.dimOthers ? 0.16 : 0.55);
    map.setPaintProperty('others', 'line-color', options.dimOthers ? '#4a5a5e' : '#7fb6c9');
  }

  function applyTerrain() {
    if (!map) return;
    try {
      if (options.terrain) map.setTerrain({source: 'terrain', exaggeration: options.terrainExaggeration});
      else map.setTerrain(null);
    } catch (error) { /* 源不可用时保持平面 */ }
  }

  function refresh() {
    if (!map || !map.isStyleLoaded()) return;
    if (map.getSource('trail')) map.getSource('trail').setData(buildTrail(route));
    if (map.getSource('others')) map.getSource('others').setData(buildOthers(others));
    if (map.getLayer('trail')) {
      for (const [key, value] of Object.entries(trailPaint())) {
        try { map.setPaintProperty('trail', key, value); } catch (error) { /* 老版本忽略 */ }
      }
    }
  }

  function addLayers() {
    if (!map || map.getSource('trail')) return;
    // 先加背景轨迹，再加选中那条，后者压在上面。
    map.addSource('others', {type: 'geojson', data: buildOthers(others)});
    map.addLayer({
      id: 'others', type: 'line', source: 'others',
      layout: {'line-join': 'round', 'line-cap': 'round'},
      paint: {'line-color': '#7fb6c9', 'line-width': 1.1, 'line-opacity': 0.55},
    });
    map.addSource('trail', {type: 'geojson', data: buildTrail(route)});
    map.addLayer({
      id: 'trail', type: 'line', source: 'trail',
      layout: {'line-join': 'round', 'line-cap': 'round'},
      paint: trailPaint(),
    });
    applyOthers();
    refresh();
  }

  // 屏幕上离点击点最近的轨迹（10 像素以内）。返回活动 id。
  function pickLine(point) {
    const pad = 10;
    const px = point.x, py = point.y;
    let bestId = null, bestDistance = pad * pad;
    const lines = others.slice();
    if (route && route.segments) {
      for (const segment of route.segments) {
        if (segment && segment.length > 1) {
          lines.push({id: route.id, line: segment.map(coord => [coord[1], coord[0]])});
        }
      }
    }
    for (const entry of lines) {
      if (!entry || entry.id == null || !entry.line) continue;
      let previous = null;
      for (const coord of entry.line) {
        const current = map.project(coord);
        if (previous) {
          const dx = current.x - previous.x, dy = current.y - previous.y;
          const length2 = dx * dx + dy * dy;
          let t = length2 ? ((px - previous.x) * dx + (py - previous.y) * dy) / length2 : 0;
          if (t < 0) t = 0; else if (t > 1) t = 1;
          const ex = previous.x + t * dx - px, ey = previous.y + t * dy - py;
          const distance = ex * ex + ey * ey;
          if (distance < bestDistance) { bestDistance = distance; bestId = entry.id; }
        }
        previous = current;
      }
    }
    return bestId;
  }

  function ensure(view) {
    if (map) return map;
    basemapKey = (view.spec || {}).key || null;
    map = new maplibregl.Map({
      container: 'map3d',
      style: styleFor(view.spec || {key: 'amap_sat'}),
      center: view.center,
      zoom: view.zoom,
      pitch: options.pitch,
      bearing: options.bearing,
      attributionControl: false,
      maxPitch: 85,
      maxZoom: 20,
      minZoom: 2,
      dragRotate: true,
      pitchWithRotate: true,
      fadeDuration: 0,          // 瓦片淡入淡出在缩放时像闪屏，关掉更干脆
    });
    map.addControl(new maplibregl.AttributionControl({compact: true}), 'bottom-left');
    map.addControl(new maplibregl.ScaleControl({maxWidth: 100, unit: 'metric'}), 'bottom-left');
    map.addControl(new maplibregl.NavigationControl({visualizePitch: true, showCompass: true}), 'bottom-right');
    map.on('load', () => { addLayers(); applyTerrain(); });
    map.on('styledata', () => { addLayers(); applyTerrain(); applyOthers(); });
    map.on('moveend', () => { if (onMove) onMove(); });
    // 缩小时不重建几何：重建一次要重新解析几百条线，缩放会明显卡顿、画面还会闪。
    map.on('click', event => {
      if (!onPick) return;
      const id = pickLine(event.point);
      if (id !== null) onPick(id);
    });
    return map;
  }

  return {
    setMoveHandler(handler) { onMove = handler; },
    setPickHandler(handler) { onPick = handler; },
    instance() { return map; },
    visible() { return visible; },
    show(view) {
      visible = true;
      const instance = ensure(view);
      requestAnimationFrame(() => instance.resize());
      if (instance.isStyleLoaded()) {
        instance.jumpTo({center: [view.center.lng, view.center.lat], zoom: view.zoom});
      }
    },
    hide() { visible = false; },
    resize() { if (map) map.resize(); },
    // 3D 里换底图：重建样式，轨迹图层会在 styledata 里自动补回来。
    setBasemap(spec) {
      if (!spec || !map || spec.key === basemapKey) return;
      basemapKey = spec.key;
      map.setStyle(styleFor(spec));
    },
    setRoute(data) { route = data; refresh(); },
    setTracks(lines) { others = lines || []; loadedBox = null; refresh(); },
    markLoaded(box) { loadedBox = box; },
    covers(box) {
      if (!loadedBox || !box) return false;
      return box[0] >= loadedBox[0] && box[1] >= loadedBox[1]
          && box[2] <= loadedBox[2] && box[3] <= loadedBox[3];
    },
    setDimOthers(dim) { options.dimOthers = !!dim; applyOthers(); },
    // null = 按海拔渐变；传颜色值 = 整条线用这个颜色
    setSolidColor(color) {
      options.solidColor = color || null;
      if (map && map.getLayer('trail')) {
        map.setPaintProperty('trail', 'line-color', trailPaint()['line-color']);
      }
    },
    setPrecision(pixels) { options.precisionPx = Math.max(0.2, Math.min(8, pixels)); refresh(); },
    setTerrain(on) { options.terrain = !!on; applyTerrain(); },
    pickCount() { return others.length; },
    pickAt(x, y) { return pickLine({x, y}); },   // 调试与自动化用
    clearRoute() { route = null; refresh(); },
    setPitch(value) { options.pitch = Math.max(0, Math.min(85, value)); if (map) map.setPitch(options.pitch); },
    setBearing(value) { options.bearing = value; if (map) map.setBearing(value); },
    jumpTo(center, zoom) {
      if (map) map.jumpTo({center: [center.lng, center.lat], zoom});
    },
    fitBounds(bounds) {
      if (!map || !bounds) return;
      map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]],
                    {padding: 90, maxZoom: 15, duration: 600});
    },
  };
})();
