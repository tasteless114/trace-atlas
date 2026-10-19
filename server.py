"""Local-only HTTP application for the ride heatmap.

Deliberately built on the standard library instead of a web framework: the whole project
ships with no third-party packages, so the start script works on a plain Python 3.10+
installation with no virtual environment, no `pip install` and no network access.

The server binds to 127.0.0.1, rejects foreign Host/Origin headers, and requires a
custom header on state-changing requests, so only this local page can drive it.
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid

from heatmap import (MAX_ACTIVITY_BYTES, SUPPORTED_EXTENSIONS, Library, configure_workers,
                     pool_status, scan_folder, worker_count)
from tiles import (JPEG_SIGNATURE, SOURCES, TileCache, public_sources, tiles_for_bbox,
                   tiles_for_points)

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
STATIC_DIR = ROOT / 'static'
TEMPLATE = ROOT / 'templates' / 'index.html'
TILE_MAX_ZOOM = 19
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_HEADER_LINE = 16 * 1024
LOCAL_NAMES = ('127.0.0.1', 'localhost', '::1')
# Offline map: the zoom range stored for your own riding areas, and a safety cap so one
# click can never start an accidental mass download.
OFFLINE_ZOOMS = (int(CONFIG.get('offline_min_zoom', 8)), int(CONFIG.get('offline_max_zoom', 15)))
OFFLINE_MAX_TILES = int(CONFIG.get('offline_max_tiles', 12000))
OFFLINE_CONCURRENCY = 6
OFFLINE_DELAY = 0.02
PREFETCH_LIMIT = 60
PREFETCH_COOLDOWN = 4.0

CONTENT_TYPES = {
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
}
SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    # blob: is for the MapLibre WebGL workers used by the 3D satellite view.
    'Content-Security-Policy': ("default-src 'self'; script-src 'self'; worker-src 'self' blob:; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                                "connect-src 'self'; frame-ancestors 'none'"),
}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Response:
    def __init__(self, body=b'', status=200, content_type='application/json; charset=utf-8', headers=None):
        self.body = body if isinstance(body, bytes) else str(body).encode('utf-8')
        self.status = status
        self.content_type = content_type
        self.headers = dict(headers or {})

    @classmethod
    def json(cls, payload, status=200):
        return cls(json.dumps(payload, ensure_ascii=False).encode('utf-8'), status)


# --------------------------------------------------------------------------------------
# Request handling helpers
# --------------------------------------------------------------------------------------

class Request:
    def __init__(self, handler, method):
        parts = urllib.parse.urlsplit(handler.path)
        self.method = method
        self.path = parts.path
        self.args = {key: values[-1] for key, values in
                     urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        self.headers = {key.lower(): value for key, value in handler.headers.items()}
        self.stream = handler.rfile
        self.params = {}
        self.consumed = False

    @property
    def content_length(self):
        try:
            return max(0, int(self.headers.get('content-length') or 0))
        except (TypeError, ValueError):
            return 0

    def read_body(self):
        length = self.content_length
        if length > MAX_UPLOAD_BYTES:
            raise ValueError('请求内容超过 1 GB 上限')
        chunks, remaining = [], length
        while remaining:
            chunk = self.stream.read(min(remaining, 1 << 20))
            if not chunk:
                raise ValueError('上传数据不完整')
            chunks.append(chunk)
            remaining -= len(chunk)
        self.consumed = True
        return b''.join(chunks)

    def json_body(self):
        if self.content_length > MAX_JSON_BYTES:
            raise ValueError('请求内容过大')
        raw = self.read_body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ValueError('请求内容不是有效 JSON') from None
        if not isinstance(payload, dict):
            raise ValueError('请求内容不是有效 JSON')
        return payload


class BodyReader:
    """Bounded reader over the request body with pushback, for the multipart scanner."""

    def __init__(self, stream, limit):
        self.stream = stream
        self.limit = limit
        self.used = 0
        self.buffer = b''

    def _fill(self, size):
        if self.used >= self.limit:
            return b''
        chunk = self.stream.read(min(size, self.limit - self.used))
        if not chunk:
            return b''
        self.used += len(chunk)
        return chunk

    def read(self, size):
        if self.buffer:
            data, self.buffer = self.buffer[:size], self.buffer[size:]
            if len(data) == size:
                return data
            return data + self._fill(size - len(data))
        return self._fill(size)

    def readline(self, limit=MAX_HEADER_LINE):
        while b'\n' not in self.buffer:
            if len(self.buffer) > limit:
                raise ValueError('上传请求头过长')
            chunk = self._fill(8192)
            if not chunk:
                break
            self.buffer += chunk
        index = self.buffer.find(b'\n')
        if index < 0:
            line, self.buffer = self.buffer, b''
            return line
        line, self.buffer = self.buffer[:index + 1], self.buffer[index + 1:]
        return line

    def push(self, data):
        if data:
            self.buffer = data + self.buffer


def _write_part(out, block, limit, written):
    if not block:
        return written
    if written + len(block) > limit:
        raise ValueError(f'单个文件超过 {limit // (1024 * 1024)} MB 上限')
    out.write(block)
    return written + len(block)


def _copy_until(reader, out, delimiter, limit):
    """Copy body bytes up to the multipart delimiter without buffering the file in RAM."""
    keep = len(delimiter) - 1
    tail, written = b'', 0
    while True:
        chunk = reader.read(256 * 1024)
        if not chunk:
            raise ValueError('上传数据不完整')
        data = tail + chunk
        index = data.find(delimiter)
        if index >= 0:
            written = _write_part(out, data[:index], limit, written)
            reader.push(data[index + len(delimiter):])
            return written
        cut = len(data) - keep
        if cut > 0:
            written = _write_part(out, data[:cut], limit, written)
            tail = data[cut:]
        else:
            tail = data


def _disposition_filename(disposition):
    match = re.search(r'filename="([^"]*)"', disposition) or re.search(r'filename=([^;]+)', disposition)
    if not match:
        return ''
    return match.group(1).strip().replace('\\', '/').rsplit('/', 1)[-1]


def parse_multipart(reader, content_type, upload_dir, allowed_extensions):
    """Stream `multipart/form-data` file parts straight to disk.

    The browser uploads up to 200 files per request, so nothing is kept in memory: each
    part is written to a temporary file while it is being received.
    """
    match = re.search(r'boundary=("?)([^";]+)\1', content_type or '')
    if not match:
        raise ValueError('上传格式不正确')
    boundary = b'--' + match.group(2).strip().encode('latin-1')
    delimiter = b'\r\n' + boundary
    created, items = [], []
    try:
        if reader.readline().strip() != boundary:
            raise ValueError('上传格式不正确')
        while True:
            headers = {}
            while True:
                line = reader.readline()
                if not line:
                    raise ValueError('上传数据不完整')
                if line in (b'\r\n', b'\n'):
                    break
                text = line.decode('latin-1', 'replace')
                if ':' in text:
                    key, value = text.split(':', 1)
                    headers[key.strip().lower()] = value.strip()
            filename = _disposition_filename(headers.get('content-disposition', ''))
            if filename:
                suffix = Path(filename).suffix.lower()
                if suffix not in allowed_extensions:
                    raise ValueError('仅支持 FIT / TCX 文件')
                path = upload_dir / (uuid.uuid4().hex + suffix)
                with open(path, 'wb') as out:
                    _copy_until(reader, out, delimiter, MAX_ACTIVITY_BYTES)
                created.append(path)
                items.append((path, filename))
            else:  # A non-file form field: consume it instead of failing the upload.
                with open(os.devnull, 'wb') as sink:
                    _copy_until(reader, sink, delimiter, MAX_JSON_BYTES)
            tail = reader.readline()
            if tail.startswith(b'--'):
                break
            if tail not in (b'\r\n', b'\n'):
                raise ValueError('上传格式不正确')
        return items
    except Exception:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


# --------------------------------------------------------------------------------------
# Folders, sources and the application
# --------------------------------------------------------------------------------------

def resolve_folders(values=None):
    """Activity folders to scan, resolved against the project so it can be moved.

    One folder holds both formats. Older configs that split FIT and TCX into two folders
    are still honoured, so an existing installation keeps working after the upgrade.
    """
    if values is None:
        values = [CONFIG['folder']] if CONFIG.get('folder') else []
        if not values:
            values = [CONFIG[key] for key in ('fit_folder', 'tcx_folder') if CONFIG.get(key)]
        if not values:
            values = [CONFIG.get('source_folder', '运动数据')]
    elif isinstance(values, str):
        values = [values]
    folders = []
    for value in values:
        path = Path(str(value)).expanduser()
        folders.append((path if path.is_absolute() else ROOT / path).resolve())
    return folders


def resolve_source_folder(value=None):
    """Single-folder helper kept for scripts and older callers."""
    return resolve_folders(None if value is None else [value])[0]


def migrate_folders(folders=None):
    """Move files from the old fit/ and tcx/ folders into the single activity folder.

    Done once, at startup, with plain renames; a file that would overwrite something is
    kept under a numbered name instead, so no activity is ever lost.
    """
    target = resolve_source_folder()
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for name in ('fit', 'tcx'):
        legacy = (ROOT / name)
        if not legacy.is_dir() or legacy.resolve() == target:
            continue
        for path in sorted(legacy.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            relative = path.relative_to(legacy)
            destination = target / relative
            if destination.exists():
                stem, suffix = destination.stem, destination.suffix
                counter = 2
                while destination.exists():
                    destination = target / relative.parent / f'{stem}-{counter}{suffix}'
                    counter += 1
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.replace(destination)
                moved += 1
            except OSError:
                pass
        # Drop the now-empty legacy tree; folders that still hold other files stay.
        try:
            for child in sorted(legacy.rglob('*'), reverse=True):
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
            if not any(legacy.iterdir()):
                legacy.rmdir()
        except OSError:
            pass
    return moved


def collect_items(folders):
    """Every FIT/TCX file in these folders, counted once even if folders overlap."""
    items, seen = [], set()
    for folder in folders:
        if not folder.is_dir():
            raise ValueError(f'目录不存在：{folder}')
        for path in scan_folder(folder):
            key = str(path.resolve()).lower()
            if key not in seen:
                seen.add(key)
                items.append((path, path.name))
    return items


def basemaps():
    items = public_sources()
    items.append({'key': 'none', 'name': '无底图（离线）', 'coords': 'wgs84',
                  'max_native_zoom': TILE_MAX_ZOOM, 'dark': True, 'attribution': ''})
    return items


class OfflineJob:
    """Store the basemap tiles that cover your own activities, so the map works from disk.

    A country-sized basemap cannot be bundled (it is measured in gigabytes), but the area
    you actually ride is a few thousand tiles: one click downloads exactly those, and from
    then on panning and zooming need no network at all.
    """

    def __init__(self, library, cache):
        self.library = library
        self.cache = cache
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread = None
        self._reset()

    def _reset(self):
        # idle | planning | running | completed | stopped | failed
        self.state = 'idle'
        self.source = ''
        self.total = self.done = self.downloaded = self.failed = self.cached = 0
        self.bytes = 0
        self.error = ''
        self.started_at = self.elapsed = 0.0

    @property
    def running(self):
        return self.state in ('planning', 'running')

    def start(self, source):
        if source not in SOURCES:
            raise ValueError('未知底图')
        with self.lock:
            if self.running:
                raise ValueError('离线地图正在下载，请稍候')
            self._reset()
            self.source = source
            self.state = 'planning'
            self.started_at = time.time()
            self.stop_flag = threading.Event()
            self.thread = threading.Thread(target=self._run, daemon=True, name='offline-map')
            self.thread.start()

    def stop(self):
        self.stop_flag.set()
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {
                'state': self.state, 'source': self.source, 'zooms': list(OFFLINE_ZOOMS),
                'total': self.total, 'done': self.done, 'downloaded': self.downloaded,
                'failed': self.failed, 'cached': self.cached, 'bytes': self.bytes,
                'error': self.error, 'elapsed': round(self.elapsed, 1),
                'cached_tiles': self.cache.cached_count(),
            }

    def _run(self):
        try:
            spec = SOURCES[self.source]
            points = self.library.coverage_points(spec['coords'])
            if not points:
                raise ValueError('还没有带 GPS 的活动，先导入文件再来下载')
            tiles = set()
            for zoom in range(OFFLINE_ZOOMS[0], OFFLINE_ZOOMS[1] + 1):
                tiles |= tiles_for_points(points, zoom, margin=1)
            pending = self.cache.missing(self.source, tiles)
            with self.lock:
                self.total = len(tiles)
                self.cached = len(tiles) - len(pending)
            if len(pending) > OFFLINE_MAX_TILES:
                raise ValueError(f'需要 {len(pending)} 块瓦片，超过单次上限 {OFFLINE_MAX_TILES}；'
                                 f'可在 config.json 调小 offline_max_zoom 后重试')
            with self.lock:
                self.state = 'running'
            with ThreadPoolExecutor(max_workers=OFFLINE_CONCURRENCY) as pool:
                for start in range(0, len(pending), 120):
                    if self.stop_flag.is_set():
                        break
                    list(pool.map(self._one, pending[start:start + 120]))
        except Exception as error:
            with self.lock:
                self.state = 'failed'
                self.error = str(error)[:300]
        else:
            with self.lock:
                self.state = 'stopped' if self.stop_flag.is_set() else 'completed'
        finally:
            with self.lock:
                self.elapsed = round(time.time() - self.started_at, 1)
            self.cache._counted_at = 0.0  # let the next status call recount the cache

    def _one(self, tile):
        if self.stop_flag.is_set():
            return
        data = b''
        try:
            data = self.cache.get(self.source, *tile)
        except Exception:
            data = b''
        with self.lock:
            self.done += 1
            if data:
                self.downloaded += 1
                self.bytes += len(data)
            else:
                self.failed += 1
        time.sleep(OFFLINE_DELAY)


class RideApp:
    def __init__(self, data_folder=None):
        migrate_folders()
        for folder in resolve_folders():
            folder.mkdir(parents=True, exist_ok=True)
        configure_workers(CONFIG.get('workers', 0))
        self.library = Library(data_folder or ROOT / 'data')
        self.tiles = TileCache(self.library.folder / 'tiles')
        self.offline = OfflineJob(self.library, self.tiles)
        # Records imported before elevation was stored get it back in the background,
        # so the 3D view has heights without re-importing anything.
        threading.Thread(target=self._backfill_altitudes, daemon=True,
                         name='altitude-backfill').start()
        self._prefetch_lock = threading.Lock()
        self._prefetched_at = 0.0
        self.routes = [
            ('GET', re.compile(r'^/$'), self.index),
            ('GET', re.compile(r'^/api/status$'), self.status),
            ('GET', re.compile(r'^/api/overview$'), self.overview),
            ('GET', re.compile(r'^/api/activities$'), self.activities),
            ('GET', re.compile(r'^/api/activities/(?P<id>\d+)$'), self.activity),
            ('GET', re.compile(r'^/api/tracks$'), self.tracks),
            ('GET', re.compile(r'^/api/offline$'), self.offline_status),
            ('POST', re.compile(r'^/api/offline$'), self.offline_start),
            ('POST', re.compile(r'^/api/offline/stop$'), self.offline_stop),
            ('POST', re.compile(r'^/api/tiles/prefetch$'), self.prefetch),
            ('GET', re.compile(r'^/api/tiles/(?P<source>[a-z_]+)/(?P<z>\d{1,2})/(?P<x>\d{1,9})/(?P<y>\d{1,9})\.png$'), self.tile),
            ('POST', re.compile(r'^/api/import/folder$'), self.import_folder),
            ('POST', re.compile(r'^/api/import/upload$'), self.upload),
            ('GET', re.compile(r'^/static/(?P<path>.+)$'), self.static),
        ]

    # -- dispatch ----------------------------------------------------------------------

    def _backfill_altitudes(self):
        """Fill in per-point elevation for records imported before it was stored."""
        try:
            moved = self.library.backfill_altitudes(resolve_source_folder())
            if moved:
                with self.library.lock:
                    self.library.version += 1
                self.library.cache.clear()
        except Exception:
            pass      # the 3D view simply stays flat for those records

    def handle(self, request):
        if request.method == 'OPTIONS':
            return Response(b'', 204, 'text/plain; charset=utf-8', {'Allow': 'GET, HEAD, POST, OPTIONS'})
        self._check_local(request)
        response, path_matched = None, False
        for method, pattern, handler in self.routes:
            match = pattern.match(request.path)
            if not match:
                continue
            path_matched = True
            if method == request.method or (method == 'GET' and request.method == 'HEAD'):
                request.params = match.groupdict()
                response = handler(request)
                break
        if response is None:
            raise HttpError(405 if path_matched else 404,
                            '不支持的请求方法' if path_matched else '页面不存在')
        if (request.path.startswith('/api/') and not request.path.startswith('/api/tiles/')
                and 'Cache-Control' not in response.headers):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @staticmethod
    def _check_local(request):
        host = request.headers.get('host', '')
        name = host
        if name.startswith('['):
            name = name[1:name.find(']')] if ']' in name else name
        elif ':' in name:
            name = name.rsplit(':', 1)[0]
        if name not in LOCAL_NAMES:
            raise HttpError(403, '仅允许本机访问')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != f'http://{host}':
            raise HttpError(403, '拒绝跨站请求')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-local-app') != 'trace-atlas':
            raise HttpError(403, '请通过本机应用执行此操作')

    # -- pages and static files --------------------------------------------------------

    def index(self, request):
        return Response(TEMPLATE.read_bytes(), 200, 'text/html; charset=utf-8',
                        {'Cache-Control': 'no-store'})

    def static(self, request):
        relative = urllib.parse.unquote(request.params['path'])
        if '..' in Path(relative).parts:
            raise HttpError(404, '未找到资源')
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            raise HttpError(404, '未找到资源') from None
        if not target.is_file():
            raise HttpError(404, '未找到资源')
        stat = target.stat()
        etag = 'W/"%x-%x"' % (int(stat.st_mtime), stat.st_size)
        headers = {'ETag': etag, 'Cache-Control': 'public, max-age=0, must-revalidate'}
        if request.headers.get('if-none-match') == etag:
            return Response(b'', 304, 'text/plain; charset=utf-8', headers)
        return Response(target.read_bytes(), 200,
                        CONTENT_TYPES.get(target.suffix.lower(), 'application/octet-stream'), headers)

    # -- API ---------------------------------------------------------------------------

    def status(self, request):
        folders = resolve_folders()
        return Response.json({
            'app': 'trace-atlas', 'source_folders': [str(folder) for folder in folders],
            'source_folder': str(folders[0]) if folders else '',
            'project_folder': str(ROOT), 'coordinate_mode': CONFIG.get('coordinate_mode', 'auto'),
            'default_basemap': CONFIG.get('basemap', 'amap_road'), 'basemaps': basemaps(),
            'extensions': list(SUPPORTED_EXTENSIONS), 'workers': worker_count(),
            'pool': pool_status(), 'offline': self.offline.snapshot(),
            'job': self.library.last_job(), 'version': self.library.version,
        })

    def overview(self, request):
        return Response.json(self.library.overview(request.args))

    def activities(self, request):
        return Response.json(self.library.activities(request.args))

    def activity(self, request):
        item = self.library.activity(int(request.params['id']), request.args.get('coords'))
        if not item:
            raise HttpError(404, '未找到活动')
        return Response.json(item)

    def tracks(self, request):
        # Pre-serialised in parallel workers, so this avoids a second encode pass.
        return Response(self.library.context_tracks_json(request.args).encode('utf-8'),
                        200, 'application/json; charset=utf-8')

    def tile(self, request):
        source = request.params['source']
        z, x, y = (int(request.params[key]) for key in ('z', 'x', 'y'))
        if source not in SOURCES or not (0 <= z <= TILE_MAX_ZOOM):
            raise HttpError(404, '未找到瓦片')
        if not (0 <= x < (1 << z) and 0 <= y < (1 << z)):
            raise HttpError(404, '未找到瓦片')
        data = self.tiles.get(source, z, x, y)
        if not data:
            raise HttpError(404, '未找到瓦片')
        # Satellite imagery arrives as JPEG; the .png suffix is only a cache hint.
        mimetype = 'image/jpeg' if data[:2] == JPEG_SIGNATURE else 'image/png'
        return Response(data, 200, mimetype,
                        {'Cache-Control': 'public, max-age=2592000, immutable'})

    # -- offline basemap ---------------------------------------------------------------

    def offline_status(self, request):
        return Response.json(self.offline.snapshot())

    def offline_start(self, request):
        body = request.json_body()
        source = str(body.get('source') or CONFIG.get('basemap', 'amap_road'))
        self.offline.start(source)
        return Response.json(self.offline.snapshot(), 202)

    def offline_stop(self, request):
        return Response.json(self.offline.stop())

    def prefetch(self, request):
        """Warm the cache for the zoom levels the user is about to look at."""
        body = request.json_body()
        source = str(body.get('source') or '')
        if source not in SOURCES:
            raise HttpError(400, '未知底图')
        try:
            bbox = [float(value) for value in (body.get('bbox') or [])]
            zoom = int(body.get('zoom') or 12)
        except (TypeError, ValueError):
            raise ValueError('地图范围无效') from None
        if len(bbox) != 4 or not (0 <= zoom <= TILE_MAX_ZOOM):
            raise ValueError('地图范围无效')
        now = time.monotonic()
        with self._prefetch_lock:
            if now - self._prefetched_at < PREFETCH_COOLDOWN:
                return Response.json({'queued': 0}, 202)
            self._prefetched_at = now
        tiles = set()
        for level in (1, 2):
            if zoom + level <= TILE_MAX_ZOOM:
                tiles |= tiles_for_bbox(bbox, zoom + level)
        queued = self.tiles.prefetch(source, sorted(tiles)[:PREFETCH_LIMIT])
        return Response.json({'queued': queued}, 202)

    def import_folder(self, request):
        body = request.json_body()
        values = body.get('folders') or None
        if values is None and body.get('folder'):
            values = [body['folder']]
        folders = resolve_folders(values)
        items = collect_items(folders)
        if not items:
            raise ValueError('这些目录中没有 FIT / TCX 文件')
        job_id = self.library.begin_job(items)
        return Response.json({'job_id': job_id, 'total': len(items),
                              'folders': [str(folder) for folder in folders]}, 202)

    def upload(self, request):
        # Temporary uploads are local and removed after parsing. Original files are untouched.
        with self.library.lock:
            if any(job['state'] == 'running' for job in self.library.jobs.values()):
                raise ValueError('已有导入任务正在运行，请等当前任务完成')
        if not request.content_length:
            raise ValueError('请选择 FIT 或 TCX 文件')
        if request.content_length > MAX_UPLOAD_BYTES:
            raise ValueError('上传内容超过 1 GB 上限')
        upload_dir = self.library.folder / 'uploads'
        upload_dir.mkdir(exist_ok=True)
        reader = BodyReader(request.stream, request.content_length)
        items = parse_multipart(reader, request.headers.get('content-type', ''), upload_dir,
                               SUPPORTED_EXTENSIONS)
        request.consumed = reader.used >= request.content_length
        if not items:
            raise ValueError('请选择 FIT 或 TCX 文件')
        try:
            job_id = self.library.begin_job(items, cleanup=True)
        except Exception:
            for path, _ in items:
                path.unlink(missing_ok=True)
            raise
        return Response.json({'job_id': job_id, 'total': len(items)}, 202)


def create_app(data_folder=None):
    return RideApp(data_folder)


# --------------------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------------------

class RideHandler(BaseHTTPRequestHandler):
    server_version = 'TraceAtlas/1.3'
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    timeout = 60
    # Without this, Nagle plus delayed ACK adds ~40 ms to every tile on a kept-alive
    # connection, which is very visible when 50 tiles load at once.
    disable_nagle_algorithm = True
    app = None

    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('HEAD')

    def do_POST(self):
        self._dispatch('POST')

    def do_OPTIONS(self):
        self._dispatch('OPTIONS')

    def log_message(self, format, *args):
        """Quiet: only failures are logged, so panning across tiles does not spam the log."""
        return

    def _log(self, status, path):
        try:
            sys.stderr.write('%s %s %s\n' % (self.address_string(), status, path[:160]))
            sys.stderr.flush()
        except Exception:
            pass

    def _dispatch(self, method):
        request = Request(self, method)
        close = False
        try:
            try:
                response = self.app.handle(request)
                close = request.content_length > 0 and not request.consumed
            except HttpError as error:
                response, close = Response.json({'error': error.message}, error.status), True
            except (ValueError, FileNotFoundError, PermissionError) as error:
                response, close = Response.json({'error': str(error)}, 400), True
            except Exception:
                traceback.print_exc()
                response = Response.json({'error': '本机服务出错，请查看 data/server-error.log'}, 500)
                close = True
            if response.status >= 400:
                self._log(response.status, request.path)
            self._send(response, head_only=method == 'HEAD', close=close)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True

    def _send(self, response, head_only=False, close=False):
        self.send_response(response.status)
        self.send_header('Content-Type', response.content_type)
        self.send_header('Content-Length', str(len(response.body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in response.headers.items():
            self.send_header(key, value)
        if close:
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        if not head_only and response.body:
            self.wfile.write(response.body)


class RideServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                              socket.timeout, TimeoutError)):
            return  # A browser closing a tab or a connection is not an application error.
        super().handle_error(request, client_address)


def make_server(app, host='127.0.0.1', port=8787):
    handler = type('BoundRideHandler', (RideHandler,), {'app': app})
    return RideServer((host, port), handler)


def main():
    parser = argparse.ArgumentParser(description='行迹 · 本地 FIT / TCX 运动热图')
    parser.add_argument('--port', type=int, default=CONFIG.get('port', 8787))
    parser.add_argument('--import-folder', action='store_true', help='启动后扫描默认目录，跳过重复文件')
    args = parser.parse_args()
    app = create_app()
    try:
        server = make_server(app, '127.0.0.1', args.port)
    except OSError as error:
        print(f'端口 {args.port} 已被占用：{error}', file=sys.stderr)
        return 1
    process_file = ROOT / 'data' / 'server-process.json'
    process_file.write_text(json.dumps({'pid': os.getpid(), 'script': str(Path(__file__).resolve()),
                                        'port': args.port}), encoding='utf-8')
    if args.import_folder:
        try:
            items = collect_items(resolve_folders())
        except ValueError:
            items = []
        if items:
            app.library.begin_job(items)
    print(f'Trace Atlas ready: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        process_file.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
