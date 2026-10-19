'use strict';
const APP_VERSION = '2';
const $ = id => document.getElementById(id);
const state = {overview:null, items:[], offset:0, selected:null, job:null, fitted:false, busy:false,
  uploading:false, mode:'footprint', filterGeneration:0, heatGeneration:0, trackGeneration:0,
  lastVersion:-1, lastRefresh:0, basemaps:[], defaultBasemap:'amap_road', workers:1, cores:1,
  pool:null, offline:null, sourceFolders:[]};
const number = (v, digits=0) => new Intl.NumberFormat('zh-CN',{maximumFractionDigits:digits}).format(v || 0);
const hours = v => number(v/3600,1);
const duration = v => `${Math.floor(v/3600)}小时${Math.floor(v%3600/60)}分`;
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
let toastTimer, filterTimer, trackTimer, searchTimer, trackAbort;
const loadingReasons=new Set();
function setLoading(kind,active){
  if(active)loadingReasons.add(kind);else loadingReasons.delete(kind);
  $('map-loading').hidden=!loadingReasons.size;
}

/* ------------------------------------------------------------------ heat palettes */

const PALETTES={
  cycling:[[0,'#174cd8'],[.22,'#00baca'],[.42,'#80e795'],[.62,'#f3e165'],[.8,'#ff843c'],[1,'#f44343']],
  running:[[0,'#3d2ba8'],[.24,'#8a44c9'],[.44,'#e0557f'],[.64,'#f7913d'],[.82,'#ffd35c'],[1,'#fff5c2']],
};
const PALETTE_FALLBACK=PALETTES.cycling;
const SPORT_LABELS={cycling:'骑行',e_biking:'电助力',running:'跑步',walking:'步行',hiking:'徒步',swimming:'游泳',unknown:'活动'};
const sportText = sport => SPORT_LABELS[sport] || '活动';

function paletteForSport(sport){
  return sport==='running'?PALETTES.running:PALETTE_FALLBACK;
}

/* ------------------------------------------------------------- parallel colouring */

const ColorPool=(()=>{
  let workers=null;
  let supported=typeof Worker!=='undefined';
  let nextJob=1;
  const jobs=new Map();
  const bandSize=64;             // minimum pixels per band before workers are worth it
  // One worker per core minus one (the main thread still composites), capped at 8.
  const threads=()=>Math.max(2,Math.min(8,(navigator.hardwareConcurrency||4)-1));

  function spawn(){
    if(workers||!supported)return workers;
    try{
      workers=[];
      for(let i=0;i<threads();i++){
        const worker=new Worker('/static/heat-worker.js');
        worker.addEventListener('message',event=>{
          const done=jobs.get(event.data.id);
          if(done){jobs.delete(event.data.id);done(event.data.buffer);}
        });
        worker.addEventListener('error',()=>{
          supported=false;
          for(const done of jobs.values())done(null);
          jobs.clear();
        });
        workers.push(worker);
      }
    }catch(error){supported=false;workers=null;}
    return workers;
  }

  function colorize(pixels,ramp){
    const pool=spawn();
    if(!pool||pool.length<2)return null;
    const data=pixels.data, total=data.length;
    if(total<bandSize*4*8)return null;   // Too small: the main thread is faster than posting.
    const bands=Math.min(pool.length,Math.ceil(total/4/bandSize));
    const per=Math.ceil(total/bands/4)*4;
    const pending=[];
    for(let i=0;i<bands;i++){
      const start=i*per;
      if(start>=total)break;
      const slice=data.slice(start,Math.min(total,start+per));
      const id=nextJob++;
      pending.push({start,promise:new Promise(resolve=>{
        jobs.set(id,buffer=>resolve(buffer));
        pool[i].postMessage({id,buffer:slice.buffer,ramp},[slice.buffer]);
      })});
    }
    const work=Promise.all(pending.map(item=>item.promise)).then(buffers=>{
      if(buffers.some(buffer=>!buffer))return null;
      buffers.forEach((buffer,index)=>data.set(new Uint8ClampedArray(buffer),pending[index].start));
      return pixels;
    });
    const timeout=new Promise(resolve=>setTimeout(()=>resolve(null),2500));
    return Promise.race([work,timeout]);
  }

  return {colorize,threads,spawn};
})();

function colorizeSync(pixels,ramp){
  const data=pixels.data, out=new Uint32Array(data.buffer);
  const littleEndian=new Uint8Array(new Uint32Array([1]).buffer)[0]===1;
  for(let i=0;i<out.length;i++){
    const alpha=data[i*4+3];
    if(!alpha)continue;
    let index=Math.round(alpha*1.42); if(index>255)index=255;
    const at=index*4;
    let outAlpha=Math.round(alpha*1.45); if(outAlpha>238)outAlpha=238;
    if(littleEndian)out[i]=ramp[at]|(ramp[at+1]<<8)|(ramp[at+2]<<16)|(outAlpha<<24);
    else{data[i*4]=ramp[at];data[i*4+1]=ramp[at+1];data[i*4+2]=ramp[at+2];data[i*4+3]=outAlpha;}
  }
  return pixels;
}

/* ---------------------------------------------------------------- track decoding */

// The server sends tracks as base64 int32 Web-Mercator pixels at zoom 20, delta coded:
// about a quarter of the bytes of JSON and much faster to unpack.
const TRACK_WORLD_ZOOM = 20;

// Each entry is one drawn polyline plus the activity it came from, so a click on the map
// can be traced back to a record. The axis-aligned box is what makes picking cheap.
function decodeTracks(payload) {
  if (!payload || payload.format !== 'i32z20') return [];
  const ids = payload.ids || [];
  const parts = [];
  for (let s = 0; s < payload.segments.length; s++) {
    const encoded = payload.segments[s];
    const id = ids[s];
    const binary = atob(encoded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const words = new Int32Array(bytes.buffer, 0, bytes.length >> 2);
    let index = 0;
    while (index < words.length) {
      const count = words[index++];
      const points = new Float64Array(count * 2);
      let x = 0, y = 0;
      for (let k = 0; k < count; k++) {
        if (k === 0) { x = words[index++]; y = words[index++]; }
        else { x += words[index++]; y += words[index++]; }
        points[k * 2] = x;
        points[k * 2 + 1] = y;
      }
      if (count < 2) continue;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (let i = 0; i < points.length; i += 2) {
        const px = points[i], py = points[i + 1];
        if (px < minX) minX = px;
        if (px > maxX) maxX = px;
        if (py < minY) minY = py;
        if (py > maxY) maxY = py;
      }
      parts.push({id, points, minX, minY, maxX, maxY});
    }
  }
  return parts;
}

// 世界 zoom-20 像素换算回经纬度：3D 视图要的是普通 GeoJSON 坐标。
const TRACK_WORLD_SIZE = 256 * Math.pow(2, 20);

function worldToLatLng(x, y) {
  const lon = x / TRACK_WORLD_SIZE * 360 - 180;
  const n = Math.PI * (1 - 2 * y / TRACK_WORLD_SIZE);
  return [Math.atan(Math.sinh(n)) * 180 / Math.PI, lon];
}

// 转成 3D 用的折线，每条抽稀到一百来个点，几百条线才不会把浏览器压住。
// 全部点都转过去，不再抽稀：抽稀交给 3D 里那层"按像素密度"的逻辑，
// 这样 3D 和足迹模式看到的是同一份精度。
function partsToLines(parts) {
  const lines = [];
  for (const part of parts || []) {
    const points = part.points;
    const count = points.length / 2;
    if (count < 2) continue;
    const line = [];
    for (let i = 0; i < count; i++) {
      const [lat, lon] = worldToLatLng(points[i * 2], points[i * 2 + 1]);
      line.push([lon, lat]);
    }
    if (line.length >= 2) lines.push({id: part.id, line});
  }
  return lines;
}

// Closest track to a container point, in screen pixels. Uses the axis-aligned box per
// polyline as a cheap reject, then an exact point-to-segment distance.
function pickTrack(point, tolerance = 9) {
  const parts = trackLayer.segments;
  if (!parts.length) return null;
  const frame = screenFrame(map);
  const wx = (point.x + frame.originX) / frame.factor;
  const wy = (point.y + frame.originY) / frame.factor;
  const limit = tolerance / frame.factor;
  let bestId = null, bestDistance = limit * limit;
  for (const part of parts) {
    if (wx < part.minX - limit || wx > part.maxX + limit ||
        wy < part.minY - limit || wy > part.maxY + limit) continue;
    const points = part.points;
    for (let i = 0; i < points.length - 3; i += 2) {
      const x1 = points[i], y1 = points[i + 1];
      const dx = points[i + 2] - x1, dy = points[i + 3] - y1;
      const length2 = dx * dx + dy * dy;
      let t = length2 ? ((wx - x1) * dx + (wy - y1) * dy) / length2 : 0;
      if (t < 0) t = 0; else if (t > 1) t = 1;
      const ex = x1 + t * dx - wx, ey = y1 + t * dy - wy;
      const distance2 = ex * ex + ey * ey;
      if (distance2 < bestDistance) { bestDistance = distance2; bestId = part.id; }
    }
  }
  return bestId;
}

// Screen geometry for the current view: world zoom-20 pixels scaled to this zoom, then
// shifted so that the canvas (anchored at the layer point of the container origin)
// receives plain container pixels.
function screenFrame(map) {
  const zoom = map.getZoom();
  const pane = map.containerPointToLayerPoint([0, 0]);
  const origin = map.getPixelOrigin();
  return {
    factor: Math.pow(2, zoom - TRACK_WORLD_ZOOM),
    originX: origin.x + pane.x,
    originY: origin.y + pane.y,
  };
}

/* ------------------------------------------------------------------ track layer */

// The footprint view. Leaflet polylines re-render the whole path on every pan; a plain
// canvas that pans with the map pane is both faster and simpler to keep in sync.
const RideTrackLayer = L.Layer.extend({
  initialize() {
    this.segments = [];
    this.style = {color: '#8fd7ff', weight: 1.4, opacity: .5};
    this.visible = false;
    this.dimmed = false;      // an activity is selected: fade everything else back
  },
  onAdd(map) {
    this._map = map;
    this.canvas = L.DomUtil.create('canvas', 'track-canvas');
    map.getPane('contextPane').appendChild(this.canvas);
    map.on('moveend zoomend resize', this.schedule, this);
    this.schedule();
  },
  onRemove(map) {
    map.off('moveend zoomend resize', this.schedule, this);
    cancelAnimationFrame(this.frame);
    this.canvas.remove();
  },
  schedule() {
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => this.draw());
  },
  setData(segments) { this.segments = segments || []; this.schedule(); },
  setStyle(style) { this.style = {...this.style, ...style}; this.schedule(); },
  setDimmed(dimmed) { this.dimmed = !!dimmed; this.schedule(); },
  clear() { this.segments = []; this.schedule(); },
  draw() {
    if (!this._map) return;
    const size = this._map.getSize(), dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    L.DomUtil.setPosition(this.canvas, this._map.containerPointToLayerPoint([0, 0]));
    const width = Math.max(1, Math.round(size.x * dpr)), height = Math.max(1, Math.round(size.y * dpr));
    this.canvas.style.width = size.x + 'px';
    this.canvas.style.height = size.y + 'px';
    if (this.canvas.width !== width) this.canvas.width = width;
    if (this.canvas.height !== height) this.canvas.height = height;
    const ctx = this.canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!this.visible) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const frame = screenFrame(this._map);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.globalAlpha = this.style.opacity * (this.dimmed ? 0.055 : 1);
    ctx.strokeStyle = this.style.color;
    ctx.lineWidth = Math.max(0.4, this.style.weight);
    for (const part of this.segments) {
      const points = part.points;
      ctx.beginPath();
      for (let i = 0; i < points.length; i += 2) {
        const x = points[i] * frame.factor - frame.originX;
        const y = points[i + 1] * frame.factor - frame.originY;
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
});

/* --------------------------------------------------------------------- heat layer */

// Frequency map drawn as lines: every activity is stroked once into an offscreen canvas
// with a low alpha, so a pixel where five rides overlap carries about five steps of
// alpha. The accumulated alpha is then pushed through the colour ramp (blue → red), so
// busy roads simply get warmer. No grid, no blurring brush.
const RideLineHeatLayer=L.Layer.extend({
  initialize(){this.segments=[];this.width=1.5;this.gain=1;this.visible=false;this.paintToken=0;this.stops=null;},
  onAdd(map){
    this._map=map;this.canvas=L.DomUtil.create('canvas','heat-canvas');
    map.getPane('heatPane').appendChild(this.canvas);
    map.on('moveend zoomend resize',this.schedule,this);
    this.setPalette(this.stops||PALETTE_FALLBACK);
    this.schedule();
  },
  onRemove(map){map.off('moveend zoomend resize',this.schedule,this);cancelAnimationFrame(this.frame);this.canvas.remove();},
  schedule(){cancelAnimationFrame(this.frame);this.frame=requestAnimationFrame(()=>this.draw());},
  setData(segments){this.segments=segments||[];this.schedule();},
  clear(){this.segments=[];this.schedule();},
  setPalette(stops){
    this.stops=stops;
    const gradient=document.createElement('canvas');gradient.width=1;gradient.height=256;
    const ctx=gradient.getContext('2d'),ramp=ctx.createLinearGradient(0,0,0,256);
    stops.forEach(([position,color])=>ramp.addColorStop(position,color));
    ctx.fillStyle=ramp;ctx.fillRect(0,0,1,256);this.ramp=ctx.getImageData(0,0,1,256).data;
    $('heat-legend').style.setProperty('--legend-ramp',
      `linear-gradient(90deg,${stops.map(([position,color])=>`${color} ${Math.round(position*100)}%`).join(',')})`);
    this.schedule();
  },
  paint(pixels,token){
    const done=data=>{
      if(token!==this.paintToken)return;
      if(!data)data=colorizeSync(pixels,this.ramp);
      const ctx=this.canvas.getContext('2d');
      ctx.setTransform(1,0,0,1,0,0);ctx.putImageData(data,0,0);
    };
    const job=ColorPool.colorize(pixels,this.ramp);
    if(job)job.then(result=>done(result)).catch(()=>done(null));
    else done(null);
  },
  draw(){
    if(!this._map||!this.ramp)return;
    const size=this._map.getSize(),dpr=Math.min(window.devicePixelRatio||1,1.5);
    L.DomUtil.setPosition(this.canvas,this._map.containerPointToLayerPoint([0,0]));
    const width=Math.max(1,Math.round(size.x*dpr)),height=Math.max(1,Math.round(size.y*dpr));
    this.canvas.style.width=size.x+'px';this.canvas.style.height=size.y+'px';
    if(this.canvas.width!==width)this.canvas.width=width;
    if(this.canvas.height!==height)this.canvas.height=height;
    const token=++this.paintToken;
    if(!this.visible){this.canvas.getContext('2d').clearRect(0,0,width,height);return;}
    if(!this.work)this.work=document.createElement('canvas');
    if(this.work.width!==width)this.work.width=width;
    if(this.work.height!==height)this.work.height=height;
    const ctx=this.work.getContext('2d',{willReadFrequently:true});
    ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,width,height);
    ctx.setTransform(dpr,0,0,dpr,0,0);
    const frame=screenFrame(this._map);
    const alpha=Math.min(0.4,0.085*this.gain);
    ctx.lineCap='round';ctx.lineJoin='round';
    ctx.strokeStyle=`rgba(255,255,255,${alpha})`;
    ctx.lineWidth=Math.max(0.5,this.width);
    for(const part of this.segments){
      if(!part||!part.points||part.points.length<4)continue;
      const points=part.points;
      ctx.beginPath();
      for(let i=0;i<points.length;i+=2){
        const x=points[i]*frame.factor-frame.originX;
        const y=points[i+1]*frame.factor-frame.originY;
        if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);
      }
      ctx.stroke();
    }
    this.paint(ctx.getImageData(0,0,width,height),token);
  }
});

/* ------------------------------------------------------------------------ map setup */

const map=L.map('map',{zoomControl:false,preferCanvas:true,zoomAnimation:false,minZoom:2,maxZoom:20}).setView([40.04,116.30],11);
map.createPane('heatPane').style.zIndex=430;
map.createPane('contextPane').style.zIndex=420;
map.createPane('selectedPane').style.zIndex=450;
L.control.zoom({position:'bottomright'}).addTo(map);
L.control.scale({imperial:false,position:'bottomleft',maxWidth:100}).addTo(map);
L.control.attribution({position:'bottomright',prefix:false}).addTo(map);
const heat=new RideLineHeatLayer().addTo(map);
const trackLayer=new RideTrackLayer().addTo(map);
trackLayer.setStyle(lineStyle());
let baseLayer, selectedLayer=L.layerGroup().addTo(map);
let tileErrors=0,tileSuccess=0;

function toast(message,error=false){
  $('toast').textContent=message; $('toast').classList.toggle('error',error); $('toast').hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>{$('toast').hidden=true;},error?9000:5000);
}
async function api(path,options={}){
  const response=await fetch(path,{...options,headers:{'X-Local-App':'trace-atlas',...options.headers}});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||`请求失败 (${response.status})`);
  return data;
}

function basemapSpec(){
  return state.basemaps.find(item=>item.key===$('basemap').value)||state.basemaps[0]||null;
}
function resolveCoords(){
  const chosen=$('coords').value;
  if(chosen!=='auto')return chosen;
  // Domestic street/satellite tiles are GCJ-02; the tracks must be shifted to match.
  const spec=basemapSpec();
  return spec&&spec.coords==='gcj02'?'wgs_to_gcj':'wgs84';
}
function filters(extra={}){
  const params=new URLSearchParams({sport:$('sport-filter').value,coords:resolveCoords(),...extra});
  if($('date-from').value)params.set('from',$('date-from').value);
  if($('date-to').value)params.set('to',$('date-to').value);
  if($('search').value.trim())params.set('q',$('search').value.trim());
  return params;
}

function updateCaption(){
  const spec=basemapSpec();
  const base=spec?`底图 ${spec.name.replace(/（.*?）/g,'')}`:'无底图离线';
  const engine=state.pool&&state.pool.state==='broken'
    ? '多核不可用·单进程'
    : `${state.workers} 进程 / ${ColorPool.threads()} 线程`;
  const text=`本机计算 · ${engine} · ${base}`;
  if($('map-caption').textContent!==text)$('map-caption').textContent=text;
}

/* --------------------------------------------------------- offline basemap tiles */

const formatBytes = bytes => {
  if(!bytes) return '0 MB';
  const mb = bytes / 1048576;
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
};
function renderOffline(job){
  state.offline = job || null;
  const button = $('offline-build'), status = $('offline-status');
  if(!job){ button.disabled = false; button.textContent = '下载离线地图'; status.textContent = '—'; return; }
  const state_ = job.state, total = job.total || 0, done = job.done || 0;
  if(state_ === 'planning'){
    button.disabled = true; button.textContent = '准备中…';
    status.textContent = '正在计算需要哪些瓦片';
  }else if(state_ === 'running'){
    button.disabled = false; button.textContent = '停止下载';
    const percent = total ? Math.round(done / total * 100) : 0;
    status.textContent = `${number(done)} / ${number(total)} 块 · ${percent}% · ${formatBytes(job.bytes)}`;
  }else if(state_ === 'completed'){
    const item = state.basemaps.find(entry => entry.key === job.source);
    const sourceName = item ? item.name : job.source;
    button.disabled = false; button.textContent = '更新离线地图';
    // A re-run usually finds everything cached: say so instead of "0 块".
    status.textContent = job.downloaded
      ? `已离线 ${sourceName} ${number(job.downloaded)} 块 · ${formatBytes(job.bytes)}${job.failed ? ` · ${number(job.failed)} 块失败` : ''}`
      : `已是最新 · ${sourceName} ${number(job.cached)} 块已在本机`;
  }else if(state_ === 'stopped'){
    button.disabled = false; button.textContent = '继续下载';
    status.textContent = `已停止 · ${number(job.downloaded)} / ${number(job.total)} 块`;
  }else if(state_ === 'failed'){
    button.disabled = false; button.textContent = '重试';
    status.textContent = job.error || '下载失败';
  }else{
    button.disabled = false; button.textContent = '下载离线地图';
    const cached = job.cached_tiles || 0;
    status.textContent = cached ? `本机已有 ${number(cached)} 块底图` : '还没有离线底图';
  }
}
async function toggleOffline(){
  const job = state.offline;
  try{
    if(job && job.state === 'running'){
      renderOffline(await api('/api/offline/stop', {method:'POST'}));
      return;
    }
    const spec = basemapSpec();
    $('offline-build').disabled = true;
    $('offline-status').textContent = '正在计算需要哪些瓦片…';
    renderOffline(await api('/api/offline', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({source: spec ? spec.key : 'amap'})}));
  }catch(error){
    toast(error.message, true);
    renderOffline(state.offline);
  }
}
// Prefetch the next zoom levels for the current view, so zooming in feels instant.
let prefetchTimer;
function schedulePrefetch(){
  clearTimeout(prefetchTimer);
  prefetchTimer = setTimeout(()=>{
    const spec = basemapSpec();
    if(!spec) return;
    const area = map.getBounds();
    api('/api/tiles/prefetch', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({source: spec.key, zoom: map.getZoom(),
        bbox: [area.getWest(), area.getSouth(), area.getEast(), area.getNorth()]})}).catch(()=>{});
  }, 900);
}
function changeBasemap(){
  if(baseLayer){map.removeLayer(baseLayer);baseLayer=null;}
  const spec=basemapSpec();
  $('tile-warning').hidden=true;tileErrors=tileSuccess=0;
  document.querySelector('.map-main').classList.toggle('no-tiles',!spec);
  // 3D 固定卫星，不跟着 2D 的下拉换
  if(spec){
    // Tiles are proxied by the local service: one fetch per tile, then cached on disk.
    baseLayer=L.tileLayer(`/api/tiles/${spec.key}/{z}/{x}/{y}.png`,{attribution:spec.attribution||'',
      maxZoom:20,maxNativeZoom:spec.max_native_zoom||19,minZoom:2,keepBuffer:4,
      updateWhenIdle:false,updateWhenZooming:false,detectRetina:false,
      className:spec.dark?'dark-tiles':'light-tiles'}).addTo(map);
    baseLayer.on('tileerror',()=>{tileErrors++;if(tileErrors>=6&&!tileSuccess)$('tile-warning').hidden=false;});
    baseLayer.on('tileload',()=>{tileSuccess++;$('tile-warning').hidden=true;});
  }
  updateCaption();
}
// 3D 视图固定用卫星图：起伏和阴影能让人一眼看出地形。
function threeDSpec(){
  // 3D 固定用卫星：路网图在地图上是一张密密麻麻的道路网，斜着看很乱，
  // 而且压暗后像一层网格。卫星影像更干净，也更有立体感。
  const items=state.basemaps||[];
  return items.find(item=>item.key==='amap_sat')||items[0]||{key:'amap_sat'};
}
// 3D 模式下按 MapLibre 自己的视野取周边轨迹（Leaflet 那边是隐藏的，用不了）。
let climbOthersTimer;
function scheduleClimbOthers(){clearTimeout(climbOthersTimer);climbOthersTimer=setTimeout(loadClimbOthers,320);}
async function loadClimbOthers(){
  const instance=TraceMap3D.instance();
  if(!instance||state.mode!=='climb')return;
  const bounds=instance.getBounds();
  const pad=.04;
  const range=[Math.max(-180,bounds.getWest()-pad),Math.max(-85,bounds.getSouth()-pad),
               Math.min(180,bounds.getEast()+pad),Math.min(85,bounds.getNorth()+pad)];
  // 视野还在已加载范围内就不重复请求：否则每拖一下都要重新取数，画面会闪。
  if(TraceMap3D.covers([bounds.getWest(),bounds.getSouth(),bounds.getEast(),bounds.getNorth()]))return;
  const box=range.join(',');
  try{
    const data=await api('/api/tracks?'+filters({mode:'footprint',zoom:Math.round(instance.getZoom()),bbox:box}));
    if(state.mode!=='climb')return;
    TraceMap3D.setTracks(partsToLines(decodeTracks(data)).filter(entry=>entry.id!==state.selected));
    TraceMap3D.markLoaded(range);
  }catch(error){/* 视野里没数据很正常 */}
}

async function loadClimbRoute(id){
  const info=$('climb-info');
  try{
    const item=await api(`/api/activities/${id}?coords=${resolveCoords()}`);
    if(state.selected!==id)return;
    TraceMap3D.setRoute({id:item.id,segments:item.segments,altitudes:item.altitudes||[]});
    const altitudes=(item.altitudes||[]).flat().filter(value=>typeof value==='number');
    if(altitudes.length){
      const low=Math.min(...altitudes),high=Math.max(...altitudes);
      info.textContent=`${number(item.distance_km,1)} 公里 · 爬升 ${number(item.ascent_m)} 米 · 海拔 ${Math.round(low)}–${Math.round(high)} 米`;
    }else{
      info.textContent=`${number(item.distance_km,1)} 公里 · 这条记录没有逐点海拔，墙体贴地`;
    }
    // 视野对准这条活动自己的范围：2D 地图可能停在另一个城市。
    const lats=[],lons=[];
    for(const segment of item.segments)for(const point of segment){lats.push(point[0]);lons.push(point[1]);}
    if(!lats.length)return;
    const box=[Math.min(...lons),Math.min(...lats),Math.max(...lons),Math.max(...lats)];
    // 点视野里已有的线时不要拽镜头，只有目标在视野外才飞过去。
    if(TraceMap3D.visible()){
      const centre=[(box[0]+box[2])/2,(box[1]+box[3])/2];
      const view=TraceMap3D.instance()?.getBounds();
      if(!view||!view.contains(centre))TraceMap3D.fitBounds(box);
    }
    // 其他轨迹按这条活动的范围取一次：3D 里同一片区域的活动才叠得起来。
    const margin=(box[2]-box[0])*.15+0.01;
    const square=[box[0]-margin,box[1]-margin,box[2]+margin,box[3]+margin].join(',');
    const others=await api('/api/tracks?'+filters({mode:'footprint',zoom:13,bbox:square}));
    if(state.selected!==id)return;
    // 自己那条已经有专门的高程线了，不要在底下再画一遍。
    TraceMap3D.setTracks(partsToLines(decodeTracks(others)).filter(entry=>entry.id!==id));
  }catch(error){toast(error.message,true);}
}
function fitBounds(bounds){
  if(!bounds)return;
  let sw=[bounds[0],bounds[1]],ne=[bounds[2],bounds[3]];
  // Detail tracks provide already transformed bounds. Global bounds use original FIT data.
  map.fitBounds([sw,ne],{paddingTopLeft:[45,110],paddingBottomRight:[260,165],maxZoom:14});
}
async function fitAll(){
  if(!state.overview?.bounds){toast('当前筛选没有带 GPS 的轨迹');return;}
  if(resolveCoords()!=='wgs84'){
    const tracks=await api('/api/tracks?'+filters({mode:'footprint'}));
    const bounds=L.latLngBounds(tracks.segments.flat());
    if(bounds.isValid())map.fitBounds(bounds,{padding:[75,100],maxZoom:15});
  }else fitBounds(state.overview.bounds);
}
// One fetch feeds both views: the loaded window is padded well past the screen, so
// panning inside it never triggers another request and the previous drawing stays put
// until fresh data actually arrives.
const TRACK_PAD = .45;

function scheduleTracks(){clearTimeout(trackTimer);trackTimer=setTimeout(loadTracks,120);}
function trackWindow(){
  const bounds=map.getBounds().pad(TRACK_PAD);
  return [Math.max(-180,bounds.getWest()),bounds.getSouth(),
          Math.min(180,bounds.getEast()),bounds.getNorth()];
}
function tracksCoverView(){
  const loaded=state.trackWindow;
  if(!loaded)return false;
  if(Math.abs(state.trackZoom-map.getZoom())>0.01)return false;
  const view=map.getBounds();
  return view.getWest()>=loaded[0]&&view.getSouth()>=loaded[1]&&
         view.getEast()<=loaded[2]&&view.getNorth()<=loaded[3];
}
async function loadTracks(force=false){
  if(!force&&tracksCoverView())return;              // already loaded, nothing to do
  setLoading('tracks',true);
  const window=trackWindow();
  const generation=++state.trackGeneration;
  if(trackAbort)trackAbort.abort();trackAbort=new AbortController();
  const params=filters({mode:'footprint',zoom:map.getZoom(),bbox:window.join(',')});
  try{
    const data=await api('/api/tracks?'+params,{signal:trackAbort.signal});
    if(generation!==state.trackGeneration)return;
    const segments=decodeTracks(data);
    state.trackWindow=window;
    state.trackZoom=map.getZoom();
    state.trackCount=data.count;
    trackLayer.setData(segments);
    heat.setData(segments);
    // 3D 视图里其他活动贴地显示，需要把世界像素换算回经纬度。
    if(TraceMap3D.visible())TraceMap3D.setTracks(partsToLines(segments));
    $('heat-max').textContent=`${number(data.count)} 次活动`;
    $('footprint-count').textContent=`${number(data.count)} 次${sportNoun()}`;
    $('footprint-count').title=`当前视野中有 ${number(data.visible_count)} 次活动轨迹`;
  }catch(error){if(error.name!=='AbortError')toast(error.message,true);}
  finally{if(generation===state.trackGeneration)setLoading('tracks',false);}
}
map.on('moveend',scheduleTracks);
map.on('moveend',schedulePrefetch);
map.on('moveend',()=>{if(state.mode==='footprint')scheduleTracks();});

// 选中活动的详细数据：心率、功率、踏频、配速这些只有文件里带得动才有。
function renderDetail(item){
  const grid=$('detail-grid');
  const metrics=item.metrics||{};
  const running=/^(running|walking|hiking)$/.test(item.sport||'');
  const km=Number(item.distance_km)||0, seconds=Number(item.duration_s)||0;
  const rows=[];
  // 字符串（用时、配速）直接放行，只有数字才需要排除 0 和 NaN。
  const add=(label,value,unit)=>{
    if(value===undefined||value===null||value==='')return;
    if(typeof value==='number'&&(!isFinite(value)||value<=0))return;
    rows.push([label,value,unit||'']);
  };

  add('距离', km?km.toFixed(2):null, 'km');
  if(seconds)add('用时', duration(seconds));
  if(item.ascent_m)add('爬升', number(item.ascent_m), 'm');

  if(running){
    if(km&&seconds)add('平均配速', pace(seconds/km), '/km');
    const fastest=metrics.max_speed;
    if(fastest)add('最快配速', pace(1000/fastest), '/km');
  }else{
    if(km&&seconds)add('平均速度', (km/(seconds/3600)).toFixed(1), 'km/h');
    if(metrics.max_speed)add('最高速度', (metrics.max_speed*3.6).toFixed(1), 'km/h');
  }

  add('平均功率', metrics.avg_power, 'W');
  add('最大功率', metrics.max_power, 'W');
  add('平均心率', metrics.avg_hr, 'bpm');
  add('最大心率', metrics.max_hr, 'bpm');
  add(running?'平均步频':'平均踏频', metrics.avg_cadence, running?'spm':'rpm');
  add(running?'最大步频':'最大踏频', metrics.max_cadence, running?'spm':'rpm');
  add('卡路里', metrics.calories, 'kcal');

  grid.replaceChildren();
  for(const [label,value,unit] of rows){
    const cell=document.createElement('div');
    const dt=document.createElement('dt');dt.textContent=label;
    const dd=document.createElement('dd');dd.textContent=value;
    if(unit){const small=document.createElement('small');small.textContent=unit;dd.append(small);}
    cell.append(dt,dd);grid.append(cell);
  }
  // 没有任何可展示的指标时，按钮直接禁用。
  const hasAny=rows.length>3;
  $('toggle-detail').hidden=!hasAny;
  $('toggle-detail').textContent='查看更多数据';
  $('toggle-detail').setAttribute('aria-expanded','false');
  grid.hidden=true;
}

// 秒/公里 → 5'32"
function pace(secondsPerKm){
  if(!isFinite(secondsPerKm)||secondsPerKm<=0)return '';
  const total=Math.round(secondsPerKm);
  return `${Math.floor(total/60)}'${String(total%60).padStart(2,'0')}"`;
}

function clearSelection(){
  selectedLayer.clearLayers();state.selected=null;$('selected-card').hidden=true;
  document.querySelectorAll('.activity-item.active').forEach(el=>el.classList.remove('active'));
  trackLayer.setDimmed(false);
  heat.canvas.style.opacity=1;
  TraceMap3D.setDimOthers(false);
  TraceMap3D.clearRoute();
  if(state.mode==='climb')loadClimbOthers();
  $('climb-info').textContent='点地图上的轨迹，或从左侧列表选一次活动，查看它的爬升';
}
async function selectActivity(id,fit=true){
  state.selected=id;
  try{
    const item=await api(`/api/activities/${id}?coords=${resolveCoords()}`);
    if(state.selected!==id)return;
    selectedLayer.clearLayers();
    // A dark halo under the bright line keeps it readable on top of five hundred others.
    L.polyline(item.segments,{pane:'selectedPane',color:'#07100e',weight:9,opacity:.85,
      lineCap:'round',lineJoin:'round',interactive:false}).addTo(selectedLayer);
    const line=L.polyline(item.segments,{pane:'selectedPane',color:'#e9ffa8',weight:3.4,
      opacity:1,lineCap:'round',lineJoin:'round',interactive:false}).addTo(selectedLayer);
    if(fit&&line.getBounds().isValid())map.fitBounds(line.getBounds(),{paddingTopLeft:[45,230],paddingBottomRight:[260,130],maxZoom:16});
    document.querySelectorAll('.activity-item.active').forEach(el=>el.classList.remove('active'));
    const row=document.querySelector(`.activity-item[data-id="${id}"]`);
    if(row)row.classList.add('active');
    trackLayer.setDimmed(true);
    heat.canvas.style.opacity=.15;
    TraceMap3D.setDimOthers(true);
    if(state.mode==='climb')loadClimbRoute(id);
    else if(TraceMap3D.visible())loadClimbRoute(id);
    $('selected-card').hidden=false;
    $('selected-title').textContent=item.start_time?`${sportText(item.sport)} · ${item.date} ${item.start_time.slice(11,16)}`:`${sportText(item.sport)} · 未标记日期`;
    $('selected-stats').textContent=`${number(item.distance_km,1)} 公里  ·  ${duration(item.duration_s)}  ·  ↑ ${number(item.ascent_m)} 米`;
    renderDetail(item);
    $('selected-file').textContent=`${item.filename} · ${number(item.point_count)} 个 GPS 点 · ${item.segment_count} 段`;
    document.querySelectorAll('.activity-item').forEach(el=>el.classList.toggle('active',Number(el.dataset.id)===id));
    if(window.innerWidth<=900)document.querySelector('.sidebar').classList.remove('open');
  }catch(error){toast(error.message,true);}
}
function lineStyle(){
  return {color:$('line-color').value,weight:Number($('line-width').value),
          opacity:Number($('line-opacity').value)/100};
}
function updateLineStyle(){
  $('line-width-value').textContent=Number($('line-width').value);
  $('line-opacity-value').textContent=$('line-opacity').value;
  document.documentElement.style.setProperty('--track-color',$('line-color').value);
  document.documentElement.style.setProperty('--track-width',$('line-width').value+'px');
  if(trackLayer)trackLayer.setStyle(lineStyle());
}
function sportNoun(){
  const value=$('sport-filter').value;
  return value==='running'?'跑步':value==='cycling'?'骑行':value==='hiking'?'步行':'运动';
}
function updateTitles(){
  const isHeat=state.mode==='heat',isClimb=state.mode==='climb',noun=sportNoun();
  const title=isClimb?`${noun}爬升 · 3D 卫星`:isHeat?`个人${noun}热图`:`我的${noun}足迹`;
  $('map-title').textContent=title;
  document.title=`行迹 · ${title}`;
  $('map').setAttribute('aria-label',isHeat?`${noun}热图地图`:`${noun}足迹地图`);
  $('summary-label').textContent=`我的${noun}足迹`;
  $('total-hours-label').textContent=`小时${noun==='运动'?'运动':noun}`;
}
function applySportTheme(){
  const palette=paletteForSport($('sport-filter').value);
  heat.setPalette(palette);
  updateTitles();
}
function setMapMode(mode,initial=false){
  const previous=state.mode;
  state.mode=mode==='heat'?'heat':mode==='climb'?'climb':'footprint';
  const isHeat=state.mode==='heat',isClimb=state.mode==='climb';
  $('heat-controls').hidden=!isHeat;
  $('heat-legend').hidden=!isHeat;
  $('climb-controls').hidden=!isClimb;
  $('footprint-controls').hidden=isHeat||isClimb;
  $('footprint-legend').hidden=isHeat||isClimb;
  document.body.classList.toggle('climb-mode',isClimb);
  document.querySelectorAll('[data-mode]').forEach(button=>{
    const active=button.dataset.mode===state.mode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));
  });
  updateTitles();
  heat.visible=isHeat&&$('heat-visible').checked;
  heat.canvas.style.display=heat.visible?'':'none';
  trackLayer.visible=state.mode==='footprint';
  trackLayer.canvas.style.display=trackLayer.visible?'':'none';
  heat.schedule();trackLayer.schedule();
  if(!isHeat)++state.heatGeneration;
  // 2D 用 Leaflet，3D 用 MapLibre，两个容器切换，视野互相传递。
  $('map').hidden=isClimb;
  $('map3d').hidden=!isClimb;
  if(isClimb){
    TraceMap3D.show({center:map.getCenter(),zoom:Math.max(10,map.getZoom()),spec:threeDSpec()});
    if(state.selected)loadClimbRoute(state.selected);
    else{scheduleTracks();loadClimbOthers();$('climb-info').textContent='点地图上的轨迹，或从左侧列表选一次活动，查看它的爬升';}
  }else{
    TraceMap3D.hide();
    if(previous==='climb'){
      map.setView(map.getCenter(),map.getZoom(),{animate:false});
      setTimeout(()=>map.invalidateSize({pan:false}),60);
    }
  }
  updateLineStyle();
  if(!initial){
    clearSelection();localStorage.setItem('trace-display-mode',state.mode);scheduleTracks();
  }
}
function renderActivities(items,append=false){
  const list=$('activity-list');if(!append)list.replaceChildren();
  for(const item of items){
    const button=document.createElement('button');button.className='activity-item';button.dataset.id=item.id;button.title=item.filename;
    button.classList.toggle('active',state.selected===item.id);button.disabled=!item.point_count;
    const date=document.createElement('div');date.className='activity-date';
    const day=document.createElement('strong');day.textContent=item.date?item.date.slice(5).replace('-','/'):'—';
    const year=document.createElement('small');year.textContent=item.date?item.date.slice(0,4):'无日期';date.append(day,year);
    const info=document.createElement('div');info.className='activity-info';
    const title=document.createElement('strong');title.textContent=sportText(item.sport)+(item.start_time?` · ${item.start_time.slice(11,16)}`:'');
    const subtitle=document.createElement('p');subtitle.textContent=item.point_count?`${duration(item.duration_s)} · ↑ ${number(item.ascent_m)} m`:'无 GPS · 已保留活动汇总';info.append(title,subtitle);
    const km=document.createElement('div');km.className='activity-km';km.textContent=number(item.distance_km,1);const unit=document.createElement('small');unit.textContent='公里';km.append(unit);
    button.append(date,info,km);button.addEventListener('click',()=>selectActivity(item.id));list.append(button);
  }
  if(!list.children.length){const empty=document.createElement('div');empty.className='activity-empty';empty.textContent=state.busy?'正在解析 FIT / TCX，活动会陆续出现…':'没有符合筛选条件的活动。\n可调整筛选或导入 FIT / TCX 文件。';list.append(empty);}
}
function updateOverview(data){
  state.overview=data;
  $('total-distance').textContent=number(data.distance_km,1);$('total-count').textContent=number(data.count);
  $('total-hours').textContent=hours(data.duration_s);$('total-ascent').textContent=number(data.ascent_m);
  const sports=data.by_sport||{};
  const rides=(sports.cycling||0)+(sports.e_biking||0),runs=sports.running||0;
  $('list-count').textContent=`${number(data.count)} 次 · 骑行 ${number(rides)} · 跑步 ${number(runs)}`;
  $('library-count').textContent=`库中 ${number(data.library_count)} 个文件`;
  $('date-range').textContent=data.first_date?`${data.first_date.replaceAll('-','.')} — ${data.last_date.replaceAll('-','.')}`:'尚未导入带日期的记录';
  $('map-empty').hidden=!!data.gps_count||state.busy;
}
async function refresh({refit=false,clear=false}={}){
  if(clear)clearSelection();
  const generation=++state.filterGeneration;
  try{
    const [overview,activities]=await Promise.all([api('/api/overview?'+filters()),api('/api/activities?'+filters({offset:0}))]);
    if(generation!==state.filterGeneration)return;
    updateOverview(overview);state.items=activities.items;state.offset=activities.items.length;
    renderActivities(activities.items);$('load-more').hidden=!activities.has_more;
    if(overview.bounds&&(!state.fitted||refit)){
      state.fitted=true;
      // Open on the most-ridden region (not the whole country); the GCJ display shift is
      // a few hundred metres, which is invisible at this zoom, so bounds stay usable.
      // 打开时定位到最近一次活动；没有 GPS 记录时退回"最常去的区域"。
  fitBounds(overview.latest_bounds||overview.focus_bounds||overview.bounds);
  if(state.mode==='climb')TraceMap3D.jumpTo(map.getCenter(),map.getZoom());
    }
    state.trackWindow=null;scheduleTracks();state.lastRefresh=Date.now();
  }catch(error){toast(error.message,true);}
}
function filterChange(){
  applySportTheme();
  state.trackWindow=null;                 // new filter, new tracks
  clearTimeout(filterTimer);filterTimer=setTimeout(()=>refresh({refit:true,clear:true}),150);
}

function renderJob(job){
  if(!job)return;
  $('job-panel').hidden=false;
  const running=job.state==='running';state.busy=running;
  $('scan-folder').disabled=running||state.uploading;
  $('job-title').textContent=running?'正在解析 FIT / TCX…':job.failed?'导入完成，部分文件需检查':'导入完成';
  $('job-count').textContent=`${job.done} / ${job.total}`;
  $('job-progress').max=Math.max(1,job.total);$('job-progress').value=job.done;
  const engine=job.parallel?` · ${job.workers} 进程并行`:'';
  $('job-detail').textContent=(running?job.current:`新增 ${job.imported+job.no_gps} · 重复 ${job.duplicate} · 无 GPS ${job.no_gps} · 失败 ${job.failed}`)+engine;
  $('job-detail').title=$('job-detail').textContent;
  $('job-errors').hidden=!job.errors.length;
  const errorList=$('error-list');errorList.replaceChildren();
  for(const err of job.errors){const p=document.createElement('p');p.textContent=`${err.filename}: ${err.message}`;errorList.append(p);}
}
function fillBasemaps(){
  const select=$('basemap');
  if(!state.basemaps.length||select.options.length)return;
  for(const item of state.basemaps){
    const option=document.createElement('option');option.value=item.key;option.textContent=item.name;select.append(option);
  }
  const legacy={dark:'amap',light:'amap',osm:'osm',none:'none'};
  const saved=localStorage.getItem('trace-basemap')||state.defaultBasemap||'amap_road';
  const wanted=legacy[saved]||saved;
  select.value=Array.from(select.options).some(option=>option.value===wanted)?wanted:select.options[0].value;
}
function fillFolders(){
  const list=$('folder-list');
  const key=state.sourceFolders.join('|');
  if(list.dataset.key===key)return;   // Polling runs often; only rebuild when it changes.
  list.dataset.key=key;list.replaceChildren();
  for(const folder of state.sourceFolders){
    const name=folder.replace(/[\\/]+$/,'').split(/[\\/]/).pop()||folder;
    const chip=document.createElement('span');chip.className='folder-chip';chip.textContent=name;chip.title=folder;list.append(chip);
  }
}
let polling=false;
async function pollStatus(){
  if(polling)return;polling=true;
  try{
    const data=await api('/api/status');
    $('server-status').textContent='本机服务正常';
    state.basemaps=data.basemaps||[];state.defaultBasemap=data.default_basemap||'amap_road';
    state.workers=data.workers||1;state.cores=(data.pool&&data.pool.cores)||data.cores||1;
    state.pool=data.pool||null;state.sourceFolders=data.source_folders||[];
    fillBasemaps();fillFolders();
    renderOffline(data.offline);
    if(!localStorage.getItem('trace-coords'))$('coords').value=data.coordinate_mode||'auto';
    updateCaption();
    const old=state.job;state.job=data.job;renderJob(data.job);
    if(data.job?.state==='running'){
      $('import-section').hidden=false;
      if(data.version!==state.lastVersion&&Date.now()-state.lastRefresh>4500){state.lastVersion=data.version;refresh();}
    }
    if(old?.state==='running'&&data.job?.state==='completed'&&old.id===data.job.id){
      await refresh({refit:!state.selected});
      if(!state.uploading)toast(`导入完成：新增 ${data.job.imported+data.job.no_gps} 个，跳过 ${data.job.duplicate} 个重复文件${data.job.failed?`，${data.job.failed} 个失败`:''}。`,!!data.job.failed);
    }
  }catch(error){$('server-status').textContent='服务连接中断';}
  finally{polling=false;}
}
async function importFolders(){
  $('scan-folder').disabled=true;
  try{
    const extra=$('extra-folder').value.split(';').map(value=>value.trim()).filter(Boolean);
    const body=extra.length?{folders:[...state.sourceFolders,...extra]}:{folders:state.sourceFolders};
    await api('/api/import/folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    await pollStatus();
  }catch(error){toast(error.message,true);$('scan-folder').disabled=state.busy;}
}
async function importFiles(files){
  if(state.busy||state.uploading){toast('当前导入尚未完成，请稍后再添加文件。');return;}
  const selected=Array.from(files).filter(file=>/\.(fit|tcx)$/i.test(file.name));
  if(!selected.length){toast('请选择 .fit 或 .tcx 文件',true);return;}
  if(selected.some(file=>file.size>50*1024*1024)){toast('单个文件不能超过 50 MB',true);return;}
  state.uploading=true;$('scan-folder').disabled=true;$('import-section').hidden=false;
  const totals={added:0,duplicates:0,failed:0};
  try{
    // Bounded requests keep large selections below multipart parser and memory limits.
    for(let offset=0;offset<selected.length;offset+=200){
      const batch=selected.slice(offset,offset+200),form=new FormData();batch.forEach(file=>form.append('files',file,file.name));
      $('job-panel').hidden=false;$('job-title').textContent=`正在传输 ${offset+1}–${offset+batch.length} / ${selected.length} 个文件…`;
      const result=await api('/api/import/upload',{method:'POST',body:form});
      let job;
      do{await sleep(850);const status=await api('/api/status');job=status.job;state.job=job;renderJob(job);}while(job?.id===result.job_id&&job.state==='running');
      if(job?.id!==result.job_id)throw new Error('导入任务已变化，请检查本地服务');
      totals.added+=job.imported+job.no_gps;totals.duplicates+=job.duplicate;totals.failed+=job.failed;
      await refresh();
    }
    await refresh({refit:!state.selected});toast(`批量导入完成：新增 ${totals.added} 个，重复 ${totals.duplicates} 个，失败 ${totals.failed} 个。`,!!totals.failed);
  }catch(error){toast(error.message,true);}
  finally{state.uploading=false;$('scan-folder').disabled=state.busy;$('file-input').value='';}
}

$('scan-folder').addEventListener('click',importFolders);
$('toggle-import').addEventListener('click',()=>{$('import-section').hidden=!$('import-section').hidden;});
$('dropzone').addEventListener('click',()=>{if(!state.busy&&!state.uploading)$('file-input').click();else toast('请等当前导入完成');});
$('dropzone').addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('dropzone').click();}});
$('file-input').addEventListener('change',event=>importFiles(event.target.files));
for(const name of ['dragenter','dragover'])$('dropzone').addEventListener(name,event=>{event.preventDefault();$('dropzone').classList.add('dragover');});
for(const name of ['dragleave','drop'])$('dropzone').addEventListener(name,event=>{event.preventDefault();$('dropzone').classList.remove('dragover');});
$('dropzone').addEventListener('drop',event=>importFiles(event.dataTransfer.files));
window.addEventListener('dragover',event=>event.preventDefault());window.addEventListener('drop',event=>event.preventDefault());
function quickDate(period){
  const today=new Date(),localDate=date=>`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
  $('date-to').value=period==='all'?'':localDate(today);
  if(period==='all')$('date-from').value='';
  else if(period==='year')$('date-from').value=`${today.getFullYear()}-01-01`;
  else{today.setDate(today.getDate()-89);$('date-from').value=localDate(today);}
  document.querySelectorAll('[data-period]').forEach(button=>button.classList.toggle('active',button.dataset.period===period));
  filterChange();
}
document.querySelectorAll('[data-period]').forEach(button=>button.addEventListener('click',()=>quickDate(button.dataset.period)));
for(const id of ['date-from','date-to'])$(id).addEventListener('change',()=>{document.querySelectorAll('[data-period]').forEach(button=>button.classList.remove('active'));filterChange();});
$('sport-filter').addEventListener('change',filterChange);
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(filterChange,250);});
$('reset-filters').addEventListener('click',()=>{$('search').value='';$('sport-filter').value='all';quickDate('all');});
$('load-more').addEventListener('click',async()=>{
  $('load-more').disabled=true;const generation=state.filterGeneration;
  try{const data=await api('/api/activities?'+filters({offset:state.offset}));if(generation!==state.filterGeneration)return;state.items.push(...data.items);state.offset+=data.items.length;renderActivities(data.items,true);$('load-more').hidden=!data.has_more;}
  catch(error){toast(error.message,true);}finally{$('load-more').disabled=false;}
});
$('fit-all').addEventListener('click',()=>{clearSelection();fitAll().catch(error=>toast(error.message,true));});
$('clear-selection').addEventListener('click',clearSelection);
$('heat-width').addEventListener('input',()=>{heat.width=Number($('heat-width').value)/10;$('width-value').textContent=$('heat-width').value;heat.schedule();localStorage.setItem('trace-heat-width',$('heat-width').value);});
$('heat-intensity').addEventListener('input',()=>{heat.gain=Number($('heat-intensity').value)/100;$('intensity-value').textContent=$('heat-intensity').value;heat.schedule();localStorage.setItem('trace-heat-intensity',$('heat-intensity').value);});
// 3D 默认按海拔上色；关掉就用下面选的单色，和 2D 的线条颜色对应。
function applyClimbColor(){
  const altitude=$('climb-altitude').checked;
  TraceMap3D.setSolidColor(altitude?null:$('climb-color').value);
  $('climb-color').disabled=altitude;
  $('climb-color').style.opacity=altitude?.4:1;
}
$('climb-altitude').addEventListener('change',()=>{
  applyClimbColor();
  localStorage.setItem('trace-climb-altitude',$('climb-altitude').checked?'1':'0');
});
$('climb-color').addEventListener('change',()=>{
  if(!$('climb-altitude').checked)TraceMap3D.setSolidColor($('climb-color').value);
  localStorage.setItem('trace-climb-color',$('climb-color').value);
});
$('toggle-detail').addEventListener('click',()=>{
  const grid=$('detail-grid'), open=grid.hidden;
  grid.hidden=!open;
  $('toggle-detail').textContent=open?'收起详细数据':'查看更多数据';
  $('toggle-detail').setAttribute('aria-expanded',String(open));
});
$('climb-terrain').addEventListener('change',()=>{
  TraceMap3D.setTerrain($('climb-terrain').checked);
  const savedAltitude=localStorage.getItem('trace-climb-altitude');
  if(savedAltitude==='0')$('climb-altitude').checked=false;
  const savedClimbColor=localStorage.getItem('trace-climb-color');
  if(savedClimbColor)$('climb-color').value=savedClimbColor;
  applyClimbColor();
  localStorage.setItem('trace-climb-terrain',$('climb-terrain').checked?'1':'0');
});
$('climb-precision').addEventListener('input',()=>{
  const pixels=Number($('climb-precision').value)/10;
  TraceMap3D.setPrecision(pixels);
  $('precision-value').textContent=pixels<=0.8?'极细':pixels<=1.5?'细':pixels<=2.5?'中':'粗';
  localStorage.setItem('trace-climb-precision',$('climb-precision').value);
});
$('climb-pitch').addEventListener('input',()=>{
  const value=Number($('climb-pitch').value);
  TraceMap3D.setPitch(value);
  $('pitch-value').textContent=String(value);
  localStorage.setItem('trace-climb-pitch',String(value));
});
$('climb-bearing').addEventListener('input',()=>{
  const value=Number($('climb-bearing').value);
  TraceMap3D.setBearing(value);
  $('bearing-value').textContent=String(value);
  localStorage.setItem('trace-climb-bearing',String(value));
});
$('heat-visible').addEventListener('change',()=>{heat.visible=state.mode==='heat'&&$('heat-visible').checked;heat.canvas.style.display=heat.visible?'':'none';heat.schedule();if(heat.visible)scheduleHeat();else setLoading('heat',false);});
function togglePanel(force){
  const hidden=force===undefined?!document.body.classList.contains('panel-hidden'):force;
  document.body.classList.toggle('panel-hidden',hidden);
  localStorage.setItem('trace-panel-hidden',hidden?'1':'');
  const button=$('toggle-panel');
  button.textContent=hidden?'显示面板':'隐藏面板';
  button.setAttribute('aria-pressed',String(hidden));
  // Leaflet has to be told the container changed size, or tiles are cut off.
  setTimeout(()=>{map.invalidateSize({pan:false});TraceMap3D.resize();},80);
}
$('toggle-panel').addEventListener('click',()=>togglePanel());
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&state.selected){clearSelection();return;}
  if(event.key!=='h'&&event.key!=='H')return;
  if(event.target instanceof HTMLInputElement||event.target instanceof HTMLSelectElement)return;
  if(event.ctrlKey||event.metaKey||event.altKey)return;
  togglePanel();
});
document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>setMapMode(button.dataset.mode)));
for(const [id,key] of [['line-width','trace-line-width'],['line-opacity','trace-line-opacity'],['line-color','trace-line-color']]){
  $(id).addEventListener(id==='line-color'?'change':'input',()=>{updateLineStyle();localStorage.setItem(key,$(id).value);});
}
$('offline-build').addEventListener('click',toggleOffline);
$('basemap').addEventListener('change',()=>{
  localStorage.setItem('trace-basemap',$('basemap').value);
  changeBasemap();
  scheduleTracks();
  if(state.selected)selectActivity(state.selected,false);
});
$('coords').addEventListener('change',()=>{localStorage.setItem('trace-coords',$('coords').value);refresh({refit:!state.selected});if(state.selected)selectActivity(state.selected);});
$('mobile-sidebar').addEventListener('click',()=>document.querySelector('.sidebar').classList.toggle('open'));
// Click a line on the map to open the activity it came from. The geometry needed for
// that is already in memory (both views share it), so this costs a few milliseconds.
map.on('click',event=>{
  if(window.innerWidth<=900)document.querySelector('.sidebar').classList.remove('open');
  const id=pickTrack(event.containerPoint);
  if(id!==null&&id!==undefined){
    selectActivity(id,false);
    revealActivity(id);
  }
});
// Show a pointer cursor when a line is under the pointer, throttled to one check per frame.
let hoverFrame=0,hoverLast=0;
map.on('mousemove',event=>{
  const now=performance.now();
  if(now-hoverLast<60)return;
  hoverLast=now;
  if(hoverFrame)return;
  hoverFrame=requestAnimationFrame(()=>{
    hoverFrame=0;
    const id=pickTrack(event.containerPoint);
    map.getContainer().style.cursor=id!==null&&id!==undefined?'pointer':'';
  });
});
function revealActivity(id){
  const item=document.querySelector(`.activity-item[data-id="${id}"]`);
  if(!item)return;
  item.scrollIntoView({block:'nearest'});
}

// The app used to be called 骑迹 / ride-heatmap; carry saved settings over once.
function migrateSettings(){
  for(const name of ['coords','display-mode','basemap','heat-width','heat-intensity',
                     'climb-pitch','climb-bearing',
                     'line-width','line-opacity','line-color','app-version']){
    const previous=localStorage.getItem('ride-'+name);
    if(previous===null)continue;
    if(localStorage.getItem('trace-'+name)===null)localStorage.setItem('trace-'+name,previous);
    localStorage.removeItem('ride-'+name);
  }
}

async function boot(){
  migrateSettings();
  if(localStorage.getItem('trace-app-version')!==APP_VERSION){
    // The default basemap became a GCJ-02 domestic provider; old coordinate choices
    // would show a ~500 m offset, so start clean once.
    localStorage.removeItem('trace-coords');
    localStorage.setItem('trace-app-version',APP_VERSION);
  }
  for(const [id,key] of [['coords','trace-coords'],['heat-width','trace-heat-width'],['heat-intensity','trace-heat-intensity'],['line-width','trace-line-width'],['line-opacity','trace-line-opacity'],['line-color','trace-line-color']]){
    const saved=localStorage.getItem(key);if(saved)$(id).value=saved;
  }
  for(const [id,key] of [['climb-pitch','trace-climb-pitch'],['climb-bearing','trace-climb-bearing']]){
    const saved=localStorage.getItem(key);if(saved)$(id).value=saved;
  }
  // 地形默认关；开了就记住，下次打开还是开着的。
  $('climb-terrain').checked = localStorage.getItem('trace-climb-terrain') === '1';
  TraceMap3D.setTerrain($('climb-terrain').checked);
  const savedAltitude=localStorage.getItem('trace-climb-altitude');
  if(savedAltitude==='0')$('climb-altitude').checked=false;
  const savedClimbColor=localStorage.getItem('trace-climb-color');
  if(savedClimbColor)$('climb-color').value=savedClimbColor;
  applyClimbColor();
  const savedPrecision=localStorage.getItem('trace-climb-precision');
  if(savedPrecision)$('climb-precision').value=savedPrecision;
  const pixels=Number($('climb-precision').value)/10;
  TraceMap3D.setPrecision(pixels);
  $('precision-value').textContent=pixels<=0.8?'极细':pixels<=1.5?'细':pixels<=2.5?'中':'粗';
  TraceMap3D.setPitch(Number($('climb-pitch').value));
  TraceMap3D.setBearing(Number($('climb-bearing').value));
  TraceMap3D.setMoveHandler(()=>{if(state.mode==='climb')scheduleClimbOthers();});
  // 3D 视图里点中哪条轨迹，就打开哪次活动。
  TraceMap3D.setPickHandler(id=>{selectActivity(id,false);revealActivity(id);});
  $('pitch-value').textContent=$('climb-pitch').value;
  $('bearing-value').textContent=$('climb-bearing').value;
  heat.width=Number($('heat-width').value)/10;heat.gain=Number($('heat-intensity').value)/100;
  $('width-value').textContent=$('heat-width').value;$('intensity-value').textContent=$('heat-intensity').value;
  if(localStorage.getItem('trace-panel-hidden'))togglePanel(true);
  setMapMode(localStorage.getItem('trace-display-mode')||'footprint',true);
  applySportTheme();
  ColorPool.spawn();
  await pollStatus();
  changeBasemap();
  await refresh();
  if(state.overview?.library_count&&!state.busy)$('import-section').hidden=true;
  setInterval(pollStatus,1500);
}
boot().catch(error=>{
  // 初始化失败不能悄悄吞掉：直接写在列表区，否则页面看起来只是"没数据"。
  console.error('启动失败', error);
  const list=$('activity-list');
  if(list)list.textContent=`启动失败：${error && error.message ? error.message : error}`;
  toast('启动失败：'+(error && error.message ? error.message : error),true);
});
