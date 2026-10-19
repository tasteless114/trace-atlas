"""Domestic basemap tile proxy with a local disk cache.

Tiles are fetched once from a Chinese tile service, stored under ``data/tiles`` and then
served from disk with a long browser cache lifetime. Repeat visits and zoom changes load
without a network round trip, which is the main "the map feels slow" fix for users in
mainland China where tile.openstreetmap.org is distant and often throttled.

Only the visible area is ever requested; nothing is bulk-downloaded.
"""
from __future__ import annotations

import math
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36 RideHeatmap/1.1')
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
JPEG_SIGNATURE = b'\xff\xd8'
MAX_TILE_BYTES = 512 * 1024
CACHE_FILES_LIMIT = 24000

SOURCES = {
    'amap_road': {
        'name': '高德路网',
        'url': 'https://wprd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}',
        'subdomains': '1234',
        'coords': 'gcj02',
        'max_native_zoom': 18,
        'dark': True,
        'attribution': '© 高德地图 AutoNavi',
        'referer': 'https://www.amap.com/',
    },
    'amap_sat': {
        'name': '高德卫星',
        'url': 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
        'subdomains': '1234',
        'coords': 'gcj02',
        'max_native_zoom': 18,
        'dark': False,
        'attribution': '© 高德地图 AutoNavi',
        'referer': 'https://www.amap.com/',
    },
    # 真实地形高程（Terrarium 编码：高度 = R*256 + G + B/256 - 32768 米）。
    # 免密钥开放数据，只给 3D 视图做地形起伏，不出现在底图下拉里。
    'terrain': {
        'name': '地形高程',
        'url': 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png',
        'subdomains': '',
        'coords': 'wgs84',
        'max_native_zoom': 15,
        'dark': False,
        'hidden': True,
        'attribution': '高程 © Mapzen / AWS Terrain Tiles',
    },
}


def public_sources():
    """Basemap descriptors safe to hand to the browser (no upstream URLs)."""
    return [{
        'key': key,
        'name': spec['name'],
        'coords': spec['coords'],
        'max_native_zoom': spec['max_native_zoom'],
        'dark': spec['dark'],
        'attribution': spec['attribution'],
    } for key, spec in SOURCES.items() if not spec.get('hidden')]


# --------------------------------------------------------------------------------------
# Slippy-map tile maths, used to work out which tiles cover a route or a viewport
# --------------------------------------------------------------------------------------

MAX_LAT = 85.05112878


def lonlat_to_tile(lat, lon, zoom):
    """Tile column/row holding a coordinate, Web Mercator (the scheme every tile server uses)."""
    scale = 1 << zoom
    x = int((lon + 180.0) / 360.0 * scale)
    clamped = max(-MAX_LAT, min(MAX_LAT, lat))
    sin_lat = math.sin(math.radians(clamped))
    y = int((0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale)
    return max(0, min(scale - 1, x)), max(0, min(scale - 1, y))


def tiles_for_points(points, zoom, margin=1):
    """Every tile touched by these coordinates, plus a ring of `margin` neighbours."""
    scale = 1 << zoom
    seen = set()
    for lat, lon in points:
        x, y = lonlat_to_tile(lat, lon, zoom)
        for dx in range(-margin, margin + 1):
            for dy in range(-margin, margin + 1):
                tile_x, tile_y = x + dx, y + dy
                if 0 <= tile_x < scale and 0 <= tile_y < scale:
                    seen.add((zoom, tile_x, tile_y))
    return seen


def tiles_for_bbox(bbox, zoom, margin=0):
    """Every tile inside a west,south,east,north box."""
    west, south, east, north = (float(value) for value in bbox)
    x1, y1 = lonlat_to_tile(north, west, zoom)
    x2, y2 = lonlat_to_tile(south, east, zoom)
    scale = 1 << zoom
    tiles = set()
    for x in range(max(0, min(x1, x2) - margin), min(scale - 1, max(x1, x2) + margin) + 1):
        for y in range(max(0, min(y1, y2) - margin), min(scale - 1, max(y1, y2) + margin) + 1):
            tiles.add((zoom, x, y))
    return tiles


class _Slot:
    """Single-flight marker: one request fetches a tile, the others wait for it."""
    __slots__ = ('event', 'data')

    def __init__(self):
        self.event = threading.Event()
        self.data = b''


class TileCache:
    def __init__(self, folder, memory_limit=800, negative_ttl=45, concurrency=8):
        self.folder = Path(folder)
        self.memory = OrderedDict()
        self.memory_limit = memory_limit
        self.negative_ttl = negative_ttl
        self.negative = {}
        self.lock = threading.Lock()
        self.inflight = {}
        self.semaphore = threading.BoundedSemaphore(concurrency)
        self.writes = 0
        self.pruning = False
        self._count_value = 0
        self._counted_at = 0.0
        self.folder.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------------------

    def path_for(self, source, z, x, y):
        # One directory per tile column: keeps folders small and, unlike a "y.png" name,
        # cannot collide with the neighbouring column.
        return self.folder / source / str(z) / str(x) / f'{y}.png'

    def has(self, source, z, x, y):
        return self.path_for(source, z, x, y).is_file()

    def missing(self, source, tiles):
        return [tile for tile in tiles if not self.has(source, *tile)]

    def cached_count(self, ttl=60.0):
        """Number of cached tiles; memoised because walking the folder is not cheap."""
        now = time.monotonic()
        with self.lock:
            if self._counted_at and now - self._counted_at < ttl:
                return self._count_value
        total = 0
        try:
            for _, _, files in os.walk(self.folder):
                total += len(files)
        except OSError:
            total = 0
        with self.lock:
            self._count_value, self._counted_at = total, now
        return total

    # -- background prefetch -----------------------------------------------------------

    def prefetch(self, source, tiles, delay=0.03):
        """Fetch tiles for nearby zoom levels in the background; returns how many were queued."""
        pending = self.missing(source, tiles)
        if not pending:
            return 0
        threading.Thread(target=self._prefetch_worker, args=(source, pending, delay),
                         daemon=True, name='tile-prefetch').start()
        return len(pending)

    def _prefetch_worker(self, source, tiles, delay):
        for tile in tiles:
            try:
                self.get(source, *tile)
            except Exception:
                pass
            time.sleep(delay)

    # -- public API --------------------------------------------------------------------

    def get(self, source, z, x, y):
        """Return PNG/JPEG bytes, or b'' when the tile cannot be served."""
        key = (source, z, x, y)
        with self.lock:
            cached = self.memory.get(key)
            if cached is not None:
                self.memory.move_to_end(key)
                return cached
            if self._recently_failed(key):
                return b''
        path = self.path_for(*key)
        try:
            data = path.read_bytes()
        except OSError:
            data = b''
        if data:
            self._remember(key, data)
            return data
        return self._download(key, SOURCES[source], z, x, y, path)

    # -- internals ---------------------------------------------------------------------

    def _recently_failed(self, key):
        stamp = self.negative.get(key)
        if stamp is None:
            return False
        if time.monotonic() - stamp < self.negative_ttl:
            return True
        self.negative.pop(key, None)
        return False

    def _remember(self, key, data):
        with self.lock:
            self.memory[key] = data
            self.memory.move_to_end(key)
            while len(self.memory) > self.memory_limit:
                self.memory.popitem(last=False)

    def _download(self, key, spec, z, x, y, path):
        with self.lock:
            slot = self.inflight.get(key)
            owner = slot is None
            if owner:
                slot = _Slot()
                self.inflight[key] = slot
        if not owner:
            slot.event.wait(20)
            return slot.data
        data = b''
        try:
            data = self._fetch(spec, z, x, y)
            if data:
                self._write(path, data)
                self._remember(key, data)
            else:
                with self.lock:
                    self.negative[key] = time.monotonic()
        finally:
            with self.lock:
                self.inflight.pop(key, None)
            slot.data = data
            slot.event.set()
        return data

    def _fetch(self, spec, z, x, y):
        hosts = spec.get('subdomains') or ''
        order = list(hosts) or ['']
        # Spread neighbouring tiles over the provider's hosts, like a real browser does.
        start = (x + y) % len(order)
        for attempt in range(len(order)):
            host = order[(start + attempt) % len(order)]
            url = spec['url'].format(s=host, x=x, y=y, z=z)
            request = urllib.request.Request(url, headers={
                'User-Agent': USER_AGENT,
                'Referer': spec.get('referer', ''),
                'Accept': 'image/png,image/*;q=0.8,*/*;q=0.5',
            })
            try:
                with self.semaphore:
                    with urllib.request.urlopen(request, timeout=8) as response:
                        if getattr(response, 'status', 200) != 200:
                            continue
                        data = response.read(MAX_TILE_BYTES)
            except Exception:
                continue
            if data[:8] == PNG_SIGNATURE or data[:2] == JPEG_SIGNATURE:
                return data
        return b''

    def _write(self, path, data):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.part')
            temporary.write_bytes(data)
            temporary.replace(path)
        except OSError:
            return
        with self.lock:
            self.writes += 1
            should_prune = self.writes % 400 == 0
        if should_prune:
            self._prune_soon()

    def _prune_soon(self):
        with self.lock:
            if self.pruning:
                return
            self.pruning = True
        threading.Thread(target=self._prune, daemon=True, name='tile-prune').start()

    def _prune(self):
        """Keep the tile cache bounded by deleting the least recently used files."""
        try:
            entries = []
            for current, _, files in os.walk(self.folder):
                for name in files:
                    path = Path(current) / name
                    try:
                        entries.append((path.stat().st_mtime, path))
                    except OSError:
                        continue
            if len(entries) <= CACHE_FILES_LIMIT:
                return
            entries.sort()
            for _, path in entries[:len(entries) - CACHE_FILES_LIMIT + 2000]:
                try:
                    path.unlink()
                except OSError:
                    continue
        finally:
            with self.lock:
                self.pruning = False
