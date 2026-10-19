"""Local FIT / TCX activity library. All coordinates and activity data stay on this computer."""
from __future__ import annotations

from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone, timedelta
import array
import base64
import hashlib
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ElementTree
import zlib

# The FIT decoder is bundled in vendor/, so no pip install and no network is needed.
VENDOR = Path(__file__).resolve().parent / 'vendor'
if VENDOR.is_dir() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

EARTH_R = 6378137.0
SAMPLE_M = 3.0         # spacing used when sampling geometry for the offline tile cover
LOCAL_TZ = timezone(timedelta(hours=8))
MAX_ACTIVITY_BYTES = 50 * 1024 * 1024
MAX_FIT_BYTES = MAX_ACTIVITY_BYTES  # Older name, still imported by server.py.
SUPPORTED_EXTENSIONS = ('.fit', '.tcx', '.gpx')
COORD_MODES = ('wgs84', 'wgs_to_gcj', 'gcj02')
CHINA_WEST, CHINA_SOUTH, CHINA_EAST, CHINA_NORTH = 73.66, 3.86, 135.05, 53.55
MAX_WORKERS = 12
# Spawning a pool costs a few hundred milliseconds on Windows, so small jobs stay serial:
# below these sizes the single process is measurably faster than the parallel path.
PARALLEL_MIN_BYTES = 256 * 1024
PARALLEL_MIN_TRACKS = 250
# Track transfer format: integer Web-Mercator pixels at zoom 20 (about 0.15 m), delta
# coded and base64 wrapped. Two int32 per point is roughly a quarter of the JSON text,
# and packing millions of numbers with array/struct is far cheaper than json.dumps.
TRACK_WORLD_ZOOM = 20
TRACK_WORLD_SCALE = 256 * (1 << TRACK_WORLD_ZOOM)
# Simplification tolerance (metres) per zoom tier, tuned to roughly 1.2 screen pixels at
# 40 degrees of latitude (119912 / 2**zoom metres per pixel): enough to cut the payload
# without turning roads into straight lines.
TRACK_TOLERANCE = ((16, 2.0), (14, 6.0), (12, 14.0), (10, 55.0), (8, 220.0))
TRACK_DEFAULT_TOLERANCE = 900.0
TRACK_GEOMETRY_CACHE = 4000   # activities kept in memory, per mode and tolerance tier

# Ascent is accumulated with a hysteresis anchor: a climb counts once it has risen this
# many metres above the anchor, so 1-second barometric noise does not add up to nothing
# (a per-sample ">1 m" rule collects almost no height on dense records) nor to nonsense.
ASCENT_THRESHOLD = 2.0
SKIP_DIRECTORIES = {'运动数据', 'data', '.venv', '__pycache__', 'node_modules', '.git', 'tiles'}


# --------------------------------------------------------------------------------------
# Multi-core support
#
# Parsing FIT/TCX, clipping tracks to the viewport and re-encoding them are pure Python
# loops, so they are handed to a persistent process pool. Every helper below degrades to
# the serial path, so a restricted environment stays correct, just slower.
# --------------------------------------------------------------------------------------

def available_cpus():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


_WORKER_LIMIT = 0
_POOL = None
_POOL_LOCK = threading.Lock()
_POOL_BROKEN = False


def configure_workers(value):
    """Pin the worker count; 0 (or an invalid value) means one per CPU, capped."""
    global _WORKER_LIMIT
    try:
        _WORKER_LIMIT = max(0, int(value or 0))
    except (TypeError, ValueError):
        _WORKER_LIMIT = 0


def worker_count():
    if _WORKER_LIMIT > 0:
        return max(1, min(MAX_WORKERS, _WORKER_LIMIT))
    cpus = available_cpus()
    # Leave one core for the web server and the browser instead of grabbing every core.
    usable = cpus - 1 if cpus > 2 else cpus
    return max(1, min(MAX_WORKERS, usable))


def pool_status():
    """What the multi-core path actually looks like right now, for /api/status."""
    broken = _POOL_BROKEN
    active = worker_count() if parallel_enabled() else 1
    return {'workers': active, 'configured': worker_count(), 'cores': available_cpus(),
            'state': 'serial' if active < 2 else 'broken' if broken else 'ready'}


def parallel_enabled():
    return worker_count() > 1 and not _POOL_BROKEN


def _pool():
    global _POOL, _POOL_BROKEN
    if not parallel_enabled():
        return None
    with _POOL_LOCK:
        if _POOL is None:
            try:
                _POOL = ProcessPoolExecutor(max_workers=worker_count(),
                                            mp_context=multiprocessing.get_context('spawn'))
            except Exception:
                _POOL_BROKEN = True
                return None
        return _POOL


def _break_pool():
    global _POOL, _POOL_BROKEN
    with _POOL_LOCK:
        _POOL_BROKEN = True
        pool, _POOL = _POOL, None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def parallel_block(function, items, use_pool=True):
    """Map one bounded block of independent items, keeping input order.

    Worker functions in this module never raise, so an exception here means the pool
    itself is unavailable (spawn blocked, no permission). The block is then redone in
    this process instead of failing the request.
    """
    items = list(items)
    if not use_pool or len(items) < 2:
        return [function(item) for item in items]
    pool = _pool()
    if pool is None:
        return [function(item) for item in items]
    try:
        return list(pool.map(function, items))
    except Exception:
        _break_pool()
        return [function(item) for item in items]


def pool_ready():
    """True once a pool exists, so extra parallelism no longer pays a spawn cost."""
    return _POOL is not None and not _POOL_BROKEN


# --------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def mercator(lat, lon):
    return EARTH_R * math.radians(lon), EARTH_R * math.log(math.tan(math.pi / 4 + math.radians(max(-85, min(85, lat))) / 2))


def inverse_mercator(x, y):
    return math.degrees(2 * math.atan(math.exp(y / EARTH_R)) - math.pi / 2), math.degrees(x / EARTH_R)


def distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = lat2 - lat1, math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1, math.sqrt(h)))


def sample_mercator(segments, spacing=SAMPLE_M):
    """Project geometry to Web Mercator and resample it at a uniform spacing."""
    points = []
    for segment in segments:
        projected = [mercator(*p) for p in segment]
        if not projected:
            continue
        points.append(projected[0])
        for (x1, y1), (x2, y2) in zip(projected, projected[1:]):
            steps = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / spacing))
            # Segments have already been split at GPS jumps, so no invented bridges.
            for i in range(1, steps + 1):
                t = i / steps
                points.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    return points


def simplify(segment, tolerance=25):
    """Iterative RDP for context tracks only; stored geometry stays complete."""
    if len(segment) < 3:
        return segment
    xy = [mercator(*p) for p in segment]
    keep = {0, len(segment) - 1}
    stack = [(0, len(segment) - 1)]
    while stack:
        first, last = stack.pop()
        x1, y1 = xy[first]
        dx, dy = xy[last][0] - x1, xy[last][1] - y1
        length2 = dx * dx + dy * dy
        best, best_i = tolerance * tolerance, None
        for i in range(first + 1, last):
            x, y = xy[i]
            t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / length2)) if length2 else 0
            d2 = (x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2
            if d2 > best:
                best, best_i = d2, i
        if best_i is not None:
            keep.add(best_i)
            stack.extend([(first, best_i), (best_i, last)])
    return [segment[i] for i in sorted(keep)]


def in_china(lat, lon):
    return CHINA_WEST < lon < CHINA_EAST and CHINA_SOUTH < lat < CHINA_NORTH


def _gcj_offset(lat, lon):
    """Offset between WGS84 and GCJ-02 in degrees, for mainland China."""
    x, y = lon - 105.0, lat - 35.0
    dlat = -100 + 2 * x + 3 * y + .2 * y * y + .1 * x * y + .2 * math.sqrt(abs(x))
    dlon = 300 + x + 2 * y + .1 * x * x + .1 * x * y + .1 * math.sqrt(abs(x))
    shared = (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    dlat += shared + (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3 + (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    dlon += shared + (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3 + (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    rad = math.radians(lat)
    magic = 1 - .00669342162296594323 * math.sin(rad) ** 2
    dlat *= 180 / ((6378245 * (1 - .00669342162296594323)) / (magic * math.sqrt(magic)) * math.pi)
    dlon *= 180 / (6378245 / math.sqrt(magic) * math.cos(rad) * math.pi)
    return dlat, dlon


def wgs_to_gcj(lat, lon):
    """Standard WGS84 -> GCJ-02, needed to line tracks up with Amap/Tencent tiles."""
    if not in_china(lat, lon):
        return lat, lon
    dlat, dlon = _gcj_offset(lat, lon)
    return lat + dlat, lon + dlon


def gcj_to_wgs(lat, lon):
    """Optional display conversion; never modifies input FIT or stored points."""
    if not in_china(lat, lon):
        return lat, lon
    a, b = lat, lon
    for _ in range(8):  # Converges in 2-3 rounds; the tolerance keeps it cheap.
        c, d = wgs_to_gcj(a, b)
        da, db = c - lat, d - lon
        a -= da
        b -= db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return a, b


def coords_mode(args):
    value = str((args or {}).get('coords') or '').strip().lower()
    return value if value in COORD_MODES else 'wgs84'


def transform_segments(segments, mode):
    if mode == 'gcj02':
        convert = gcj_to_wgs
    elif mode == 'wgs_to_gcj':
        convert = wgs_to_gcj
    else:
        return segments
    return [[[round(v, 6) for v in convert(*p)] for p in seg] for seg in segments]


def viewport_segments(segments, bbox):
    """Keep crossing edges and preserve breaks when a route leaves the viewport."""
    if bbox is None:
        return segments
    west, south, east, north = bbox
    result = []
    for segment in segments:
        current = []
        for a, b in zip(segment, segment[1:]):
            intersects = not (max(a[1], b[1]) < west or min(a[1], b[1]) > east or
                              max(a[0], b[0]) < south or min(a[0], b[0]) > north)
            if intersects:
                if not current:
                    current.append(a)
                current.append(b)
            elif current:
                result.append(current)
                current = []
        if current:
            result.append(current)
    return result


def parse_bbox(value):
    """Validate a west,south,east,north string and round it for stable caching."""
    if not value:
        return None
    try:
        values = [float(v) for v in str(value).split(',')]
    except (TypeError, ValueError):
        raise ValueError('地图范围无效') from None
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError('地图范围无效')
    west, south, east, north = (round(v, 4) for v in values)
    if west > east or south > north:
        raise ValueError('地图范围无效')
    return west, south, east, north


# --------------------------------------------------------------------------------------
# Activity type handling
# --------------------------------------------------------------------------------------

SPORT_ALIASES = {
    'cycling': 'cycling', 'biking': 'cycling', 'bike': 'cycling', 'road_cycling': 'cycling',
    'mountain_biking': 'cycling', 'gravel_cycling': 'cycling', 'cyclocross': 'cycling',
    'bmx': 'cycling', 'track_cycling': 'cycling', 'indoor_cycling': 'cycling',
    'e_biking': 'e_biking', 'ebike': 'e_biking', 'e-biking': 'e_biking', 'e_bike': 'e_biking',
    'running': 'running', 'run': 'running', 'trail_running': 'running',
    'treadmill_running': 'running', 'track_running': 'running', 'street_running': 'running',
    'indoor_running': 'running', 'virtual_run': 'running',
    'walking': 'walking', 'walk': 'walking', 'casual_walking': 'walking', 'speed_walking': 'walking',
    'hiking': 'hiking', 'hike': 'hiking', 'mountaineering': 'hiking',
    'swimming': 'swimming', 'swim': 'swimming', 'lap_swimming': 'swimming', 'open_water': 'swimming',
}

SPORT_KEYWORDS = (
    ('bik', 'cycling'), ('cycl', 'cycling'), ('runn', 'running'),
    ('walk', 'walking'), ('hik', 'hiking'), ('swim', 'swimming'),
)

# Sport filter values shared with the web interface.
SPORT_GROUPS = {
    'all': None,
    'cycling': ('cycling', 'e_biking'),
    'running': ('running',),
    'hiking': ('hiking', 'walking'),
    'swimming': ('swimming',),
}
SPORT_FILTERS = tuple(SPORT_GROUPS) + ('gps',)


def normalize_sport(value):
    """Collapse FIT sport names, TCX Sport attributes and old rows onto one vocabulary."""
    text = str(value or '').strip().lower().replace(' ', '_').replace('-', '_')
    if not text:
        return 'unknown'
    if text in SPORT_ALIASES:
        return SPORT_ALIASES[text]
    for keyword, sport in SPORT_KEYWORDS:
        if keyword in text:
            return sport
    return 'unknown'


# --------------------------------------------------------------------------------------
# FIT / TCX parsing
# --------------------------------------------------------------------------------------

class TrackBuilder:
    """Shared GPS stream and segmentation rules for both file formats."""

    def __init__(self):
        self.segments = []
        self.current = []
        self.current_alt = []      # one entry per point in self.current
        self.altitudes = []        # one list per segment, for the 3D elevation view
        self.previous = None
        self.previous_time = None
        self.previous_alt = None
        self.timestamps = []
        self.ascent = 0.0
        self.record_distance = 0.0
        # 逐点指标只在内存里累积，落库的是汇总值（平均/最大），不存每个点的。
        self.metrics = {'power': [], 'hr': [], 'cadence': [], 'speed': [], 'temp': []}

    def note_time(self, stamp):
        if isinstance(stamp, datetime):
            self.timestamps.append(stamp)

    def sample(self, power=None, heart_rate=None, cadence=None, speed=None, temperature=None):
        """One record's instantaneous values, kept only for the summary."""
        for key, value in (('power', power), ('hr', heart_rate), ('cadence', cadence),
                           ('speed', speed), ('temp', temperature)):
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                self.metrics[key].append(float(value))

    def summary(self):
        """Average and maximum for every metric that the file actually carried."""
        out = {}
        for key, values in self.metrics.items():
            if not values:
                continue
            out[f'avg_{key}'] = round(sum(values) / len(values), 1)
            out[f'max_{key}'] = round(max(values), 1)
        return out

    def add(self, lat, lon, stamp=None, altitude=None):
        if lat is None or lon is None:
            return
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(lat) and math.isfinite(lon)) or not (-85 < lat < 85 and -180 <= lon <= 180):
            return
        if lat == 0 and lon == 0:
            return
        point = [round(lat, 6), round(lon, 6)]
        gap = (stamp - self.previous_time).total_seconds() if isinstance(stamp, datetime) and isinstance(self.previous_time, datetime) else None
        jump = distance(self.previous, point) if self.previous else 0
        if self.previous and (jump > 2000 or (gap is not None and (gap > 180 or gap < 0 or (gap > 0 and jump / gap > 50)))):
            if len(self.current) >= 2:
                self.segments.append(self.current)
                self.altitudes.append(self.current_alt)
            self.current = []
            self.current_alt = []
            self.previous_alt = None
        if not self.current or point != self.current[-1]:
            self.current.append(point)
            self.current_alt.append(round(altitude, 1) if altitude is not None else None)
        self.previous, self.previous_time = point, stamp
        if altitude is not None:
            if self.previous_alt is None:
                self.previous_alt = altitude
            else:
                delta = altitude - self.previous_alt
                if delta >= ASCENT_THRESHOLD:      # a confirmed climb since the anchor
                    self.ascent += delta
                    self.previous_alt = altitude
                elif delta <= -ASCENT_THRESHOLD:   # a confirmed descent moves the anchor
                    self.previous_alt = altitude

    def finish(self):
        if len(self.current) >= 2:
            self.segments.append(self.current)
            self.altitudes.append(self.current_alt)
        return self.segments


def local_time(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ).isoformat(timespec='seconds')


def _summarize(segments, timestamps, sports, starts=None, session_distance=0.0,
               record_distance=0.0, duration=0.0, ascent_values=(), record_ascent=0.0,
               altitudes=None, metrics=None):
    points = [p for seg in segments for p in seg]
    base = starts or timestamps
    start = local_time(min(base)) if base else None
    geometry_distance = sum(distance(a, b) for seg in segments for a, b in zip(seg, seg[1:]))
    if not duration and len(timestamps) > 1:
        duration = (max(timestamps) - min(timestamps)).total_seconds()
    sport = next((s for s in sports if s), 'unknown')
    bounds = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)] if points else None
    return {
        'start_time': start, 'date': start[:10] if start else None, 'sport': sport,
        'distance_km': round((session_distance or record_distance or geometry_distance) / 1000, 3),
        'duration_s': round(max(0, duration)), 'ascent_m': round(sum(ascent_values) if ascent_values else record_ascent),
        'point_count': len(points), 'segment_count': len(segments), 'bounds': bounds,
        'segments': segments, 'altitudes': altitudes or [],
        'metrics': metrics or {},
    }


def parse_fit(raw):
    from fitparse import FitFile
    fit = FitFile(raw, check_crc=True)
    builder = TrackBuilder()
    sessions = []
    record_distance = 0.0
    for message in fit.get_messages():
        if message.name == 'session':
            sessions.append(message.get_values())
            continue
        if message.name != 'record':
            continue
        values = message.get_values()
        stamp = values.get('timestamp')
        builder.note_time(stamp)
        record_distance = max(record_distance, values.get('distance') or 0)
        lat_raw, lon_raw = values.get('position_lat'), values.get('position_long')
        if lat_raw is None or lon_raw is None:
            continue
        builder.sample(power=values.get('power'),
                       heart_rate=values.get('heart_rate'),
                       cadence=values.get('cadence'),
                       speed=values.get('enhanced_speed', values.get('speed')),
                       temperature=values.get('temperature'))
        builder.add(lat_raw * 180 / (1 << 31), lon_raw * 180 / (1 << 31), stamp,
                    values.get('enhanced_altitude', values.get('altitude')))
    segments = builder.finish()
    starts = [s['start_time'] for s in sessions if isinstance(s.get('start_time'), datetime)]
    sports = [normalize_sport(s.get('sport')) for s in sessions if s.get('sport')]
    # session 里的汇总比逐点平均更准（已经剔除了暂停和零值），优先用它。
    metrics = builder.summary()
    for session in sessions:
        for source, target in (('avg_power', 'avg_power'), ('max_power', 'max_power'),
                               ('avg_heart_rate', 'avg_hr'), ('max_heart_rate', 'max_hr'),
                               ('avg_cadence', 'avg_cadence'), ('max_cadence', 'max_cadence'),
                               ('enhanced_max_speed', 'max_speed'), ('max_speed', 'max_speed'),
                               ('calories', 'calories')):
            value = session.get(source)
            if isinstance(value, (int, float)) and value > 0:
                metrics[target] = round(float(value), 1)
        speed = session.get('enhanced_avg_speed') or session.get('avg_speed')
        if isinstance(speed, (int, float)) and speed > 0:
            metrics['avg_speed'] = round(float(speed), 3)
    metrics.pop('avg_temp', None)
    metrics.pop('max_temp', None)
    return _summarize(
        segments, builder.timestamps, sports, starts=starts,
        altitudes=builder.altitudes,
        session_distance=sum(s.get('total_distance') or 0 for s in sessions),
        record_distance=max(record_distance, builder.record_distance),
        duration=sum(s.get('total_timer_time') or s.get('total_elapsed_time') or 0 for s in sessions),
        ascent_values=[s['total_ascent'] for s in sessions if s.get('total_ascent') is not None],
        record_ascent=builder.ascent,
        metrics=metrics,
    )


def _local_name(tag):
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _timestamp(text):
    if not text:
        return None
    value = text.strip()
    if not value:
        return None
    if value[-1] in 'Zz':
        value = value[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _tcx_point(element, builder):
    stamp = lat = lon = altitude = None
    heart_rate = cadence = power = speed = None
    for child in element:
        name = _local_name(child.tag)
        if name == 'Time':
            stamp = _timestamp(child.text)
        elif name == 'Position':
            for field in child:
                field_name = _local_name(field.tag)
                if field_name == 'LatitudeDegrees':
                    lat = _number(field.text)
                elif field_name == 'LongitudeDegrees':
                    lon = _number(field.text)
        elif name == 'AltitudeMeters':
            altitude = _number(child.text)
        elif name == 'DistanceMeters':
            value = _number(child.text)
            if value is not None:
                builder.record_distance = max(builder.record_distance, value)
        elif name == 'HeartRateBpm':
            # <HeartRateBpm><Value>142</Value></HeartRateBpm>
            for field in child:
                if _local_name(field.tag) == 'Value':
                    heart_rate = _number(field.text)
        elif name == 'Cadence':
            cadence = _number(child.text)
        elif name == 'Extensions':
            for field in child.iter():
                field_name = _local_name(field.tag)
                if field_name in ('Speed', 'Watts', 'RunCadence'):
                    value = _number(field.text)
                    if value is None:
                        continue
                    if field_name == 'Speed':
                        speed = value
                    elif field_name == 'Watts':
                        power = value
                    else:
                        cadence = value
    if heart_rate or cadence or power or speed:
        builder.sample(power=power, heart_rate=heart_rate, cadence=cadence, speed=speed)
    builder.note_time(stamp)
    builder.add(lat, lon, stamp, altitude)


def parse_tcx(raw):
    """Stream a Training Center Database file; only trackpoints and lap totals are kept."""
    builder = TrackBuilder()
    sports, starts = [], []
    lap_distance = lap_time = 0.0
    lap_metrics = {}
    for _, element in ElementTree.iterparse(io.BytesIO(raw), events=('end',)):
        name = _local_name(element.tag)
        if name == 'Trackpoint':
            _tcx_point(element, builder)
        elif name == 'Lap':
            start = _timestamp(element.get('StartTime'))
            if start is not None:
                starts.append(start)
            for child in element:
                child_name = _local_name(child.tag)
                if child_name == 'TotalTimeSeconds':
                    lap_time += _number(child.text) or 0
                elif child_name == 'DistanceMeters':
                    lap_distance += _number(child.text) or 0
                elif child_name in ('AverageHeartRateBpm', 'MaximumHeartRateBpm'):
                    for field in child:
                        if _local_name(field.tag) == 'Value':
                            value = _number(field.text)
                            if value:
                                key = 'avg_hr' if child_name.startswith('Average') else 'max_hr'
                                lap_metrics[key] = round(value, 1)
                elif child_name == 'Cadence':
                    value = _number(child.text)
                    if value:
                        lap_metrics['avg_cadence'] = round(value, 1)
                elif child_name == 'Extensions':
                    for field in child.iter():
                        field_name = _local_name(field.tag)
                        value = _number(field.text)
                        if value is None:
                            continue
                        key = {'AvgSpeed': 'avg_speed', 'MaxSpeed': 'max_speed',
                               'AvgWatts': 'avg_power', 'MaxWatts': 'max_power',
                               'AvgRunCadence': 'avg_cadence',
                               'MaxRunCadence': 'max_cadence'}.get(field_name)
                        if key:
                            lap_metrics[key] = round(value, 1)
        elif name == 'Activity':
            sport = element.get('Sport')
            if sport:
                sports.append(normalize_sport(sport))
        if name in ('Trackpoint', 'Track', 'Lap', 'Activity'):
            # Release the parsed XML immediately; a long TCX file must not be held twice.
            element.clear()
    segments = builder.finish()
    return _summarize(
        segments, builder.timestamps, sports, starts=starts,
        altitudes=builder.altitudes,
        session_distance=lap_distance, record_distance=builder.record_distance,
        duration=lap_time, record_ascent=builder.ascent,
        metrics={**builder.summary(), **lap_metrics},
    )


def _gpx_point(element, builder):
    """One <trkpt>: coordinates as attributes, everything else as children."""
    try:
        lat = float(element.get('lat'))
        lon = float(element.get('lon'))
    except (TypeError, ValueError):
        return
    stamp = altitude = heart_rate = cadence = power = speed = None
    for child in element:
        name = _local_name(child.tag)
        if name == 'ele':
            altitude = _number(child.text)
        elif name == 'time':
            stamp = _timestamp(child.text)
        elif name == 'speed':
            speed = _number(child.text)
        elif name == 'extensions':
            # 各家写法不同：gpxtpx:hr / gpxtpx:TrackPointExtension/gpxtpx:hr / power …
            for field in child.iter():
                field_name = _local_name(field.tag).lower()
                value = _number(field.text)
                if value is None:
                    continue
                if field_name in ('hr', 'heartrate', 'heart_rate'):
                    heart_rate = value
                elif field_name in ('cad', 'cadence'):
                    cadence = value
                elif field_name in ('power', 'watts', 'pwr'):
                    power = value
                elif field_name in ('speed', 'velocity'):
                    speed = value
    if heart_rate or cadence or power or speed:
        builder.sample(power=power, heart_rate=heart_rate, cadence=cadence, speed=speed)
    builder.note_time(stamp)
    builder.add(lat, lon, stamp, altitude)


def parse_gpx(raw):
    """Stream a GPX track; every <trkpt> carries the position and the extensions."""
    builder = TrackBuilder()
    sports, starts = [], []
    for _, element in ElementTree.iterparse(io.BytesIO(raw), events=('end',)):
        name = _local_name(element.tag)
        if name == 'trkpt':
            _gpx_point(element, builder)
        elif name == 'type':
            # <trk><type>cycling</type> 是 Strava / Garmin 常见的写法
            sport = normalize_sport((element.text or '').strip())
            if sport and sport != 'unknown':
                sports.append(sport)
        if name in ('trkpt', 'trkseg', 'trk', 'gpx'):
            element.clear()
    segments = builder.finish()
    metrics = builder.summary()
    return _summarize(
        segments, builder.timestamps, sports,       # GPX 没有 session，起始时间从时间戳推断
        altitudes=builder.altitudes,
        record_distance=builder.record_distance, record_ascent=builder.ascent,
        metrics=metrics,
    )


def parse_activity(raw, suffix=''):
    """Decode one activity file, choosing the parser by extension then by content."""
    suffix = (suffix or '').lower()
    if suffix == '.tcx':
        return parse_tcx(raw)
    if suffix == '.gpx':
        return parse_gpx(raw)
    if suffix != '.fit' and raw[:64].lstrip()[:1] == b'<':
        head = raw[:4096].lstrip()
        if b'<gpx' in head or b'<trkpt' in head:
            return parse_gpx(raw)   # Renamed GPX file
        return parse_tcx(raw)       # Renamed TCX file; sniff instead of failing.
    return parse_fit(raw)


def prepare_file(path):
    """Parse one file into everything the database needs. Safe to run in a worker."""
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_ACTIVITY_BYTES:
        raise ValueError(f'{path.name} 超过单个文件 {MAX_ACTIVITY_BYTES // (1024 * 1024)} MB 上限')
    raw = path.read_bytes()
    parsed = parse_activity(raw, path.suffix)
    segments = parsed.pop('segments')
    altitudes = parsed.pop('altitudes', None)
    return {
        'digest': hashlib.sha256(raw).hexdigest(),
        'meta': parsed,
        'geometry': zlib.compress(json.dumps(segments, separators=(',', ':')).encode(), 6),
        'context': zlib.compress(json.dumps([simplify(seg) for seg in segments], separators=(',', ':')).encode(), 6),
        'altitudes': zlib.compress(json.dumps(altitudes or [], separators=(',', ':')).encode(), 6),
        'metrics': parsed.pop('metrics', {}) or {},
    }


def prepare_one(path_text):
    """Pool entry point: never raises, so one bad file cannot cancel a block."""
    try:
        return 'ok', prepare_file(path_text)
    except Exception as exc:
        return 'error', f'{type(exc).__name__}: {exc}'[:400]


def track_tolerance(zoom):
    """Simplification tolerance for a zoom level, in metres."""
    for min_zoom, tolerance in TRACK_TOLERANCE:
        if zoom >= min_zoom:
            return tolerance
    return TRACK_DEFAULT_TOLERANCE


def project_world(segment):
    """Degrees -> integer Web-Mercator pixels at TRACK_WORLD_ZOOM, as a flat x,y array."""
    out = array.array('i')
    append = out.append
    scale = TRACK_WORLD_SCALE
    for lat, lon in segment:
        append(int(round((lon + 180.0) / 360.0 * scale)))
        sin_lat = math.sin(lat * math.pi / 180.0)
        append(int(round((0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale)))
    return out


def world_to_latlng(x, y):
    """Inverse of project_world, used by the object-oriented track API."""
    lon = x / TRACK_WORLD_SCALE * 360.0 - 180.0
    n = math.pi * (1 - 2 * y / TRACK_WORLD_SCALE)
    lat = math.degrees(math.atan(math.sinh(n)))
    return [lat, lon]


def clip_world(points, box):
    """Keep the parts of a world-pixel polyline inside box=(x0,y0,x1,y1)."""
    x0, y0, x1, y1 = box
    result, current = [], None
    for i in range(0, len(points) - 3, 2):
        ax, ay = points[i], points[i + 1]
        bx, by = points[i + 2], points[i + 3]
        if not (max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1):
            if current is None:
                current = array.array('i', (ax, ay))
                result.append(current)
            current.extend((bx, by))
        else:
            current = None
    return result


def encode_tracks(segments):
    """Pack clipped world-pixel polylines: per segment a point count, then delta pairs."""
    out = array.array('i')
    append = out.append
    for segment in segments:
        count = len(segment) // 2
        if count < 2:
            continue
        append(count)
        previous_x = previous_y = 0
        for i in range(0, count * 2, 2):
            x, y = segment[i], segment[i + 1]
            if i == 0:
                append(x)
                append(y)
            else:
                append(x - previous_x)
                append(y - previous_y)
            previous_x, previous_y = x, y
    if not out:
        return ''
    return base64.b64encode(out.tobytes()).decode('ascii')


def decode_tracks(blob):
    """Inverse of encode_tracks; used by the object API and by tests."""
    if not blob:
        return []
    raw = base64.b64decode(blob)
    values = array.array('i')
    values.frombytes(raw)
    if not values:
        return []
    segments, index = [], 0
    while index < len(values):
        count = values[index]
        index += 1
        segment = array.array('i')
        x = y = 0
        for point in range(count):
            if point == 0:
                x, y = values[index], values[index + 1]
            else:
                x += values[index]
                y += values[index + 1]
            index += 2
            segment.extend((x, y))
        segments.append(segment)
    return segments


def prepare_geometry(job):
    """Pool entry point: simplify and project one activity into world pixels.

    Returns the cache key together with the packed geometry, so the request thread only
    has to clip integers once the pool has done the trigonometry.
    """
    activity_id, blob, column, tolerance, mode = job
    key = (activity_id, column, tolerance, mode)
    try:
        segments = json.loads(zlib.decompress(blob))
        segments = [simplify(segment, tolerance) for segment in segments]
        segments = transform_segments(segments, mode)
        packed = [project_world(segment) for segment in segments if len(segment) >= 2]
    except Exception:
        packed = []      # one unreadable record must not blank the whole map
    return key, packed


def read_altitudes(path_text):
    """Re-parse one file just to recover per-point elevation.

    Returns the segment/point counts as well, so the caller can refuse to attach
    altitudes to a record whose geometry no longer matches the file.
    """
    try:
        path = Path(path_text)
        parsed = parse_activity(path.read_bytes(), path.suffix)
        segments = parsed.get('segments') or []
        return 'ok', {
            'segments': len(segments),
            'points': sum(len(segment) for segment in segments),
            'altitudes': parsed.get('altitudes') or [],
            'metrics': parsed.get('metrics') or {},
        }
    except Exception:
        return 'error', None


def file_digest(path):
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def scan_folder(folder):
    """Every supported activity file below one folder, skipping local state folders."""
    folder = Path(folder)
    found = []
    for current, directories, files in os.walk(folder):
        directories[:] = [d for d in directories if d.lower() not in SKIP_DIRECTORIES]
        for name in files:
            if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(Path(current) / name)
    found.sort(key=lambda path: str(path).lower())
    return found


class ResponseCache:
    """Small ordered cache for computed map payloads; scoped to the library version."""

    def __init__(self, limit=12, max_bytes=8 << 20):
        self.limit = limit
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            value = self.entries.get(key)
            if value is not None:
                self.entries.move_to_end(key)
            return value

    def put(self, key, value, weight=0):
        if weight > self.max_bytes:
            return
        with self.lock:
            self.entries[key] = value
            self.entries.move_to_end(key)
            while len(self.entries) > self.limit:
                self.entries.popitem(last=False)

    def clear(self):
        with self.lock:
            self.entries.clear()


class Library:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'rides.sqlite3'
        self.version = 0
        self.jobs = {}
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.cache = ResponseCache()
        # Simplified, projected geometry per activity: the expensive part of a track
        # request, kept so panning only re-clips integers.
        self.geom_cache = OrderedDict()
        self.geom_lock = threading.Lock()
        # Start the workers while the browser is still downloading tiles, so the first
        # track request is already parallel instead of paying the spawn cost.
        if parallel_enabled():
            threading.Thread(target=_pool, daemon=True, name='pool-warmup').start()
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS activities (
                    id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL, start_time TEXT, date TEXT, sport TEXT NOT NULL,
                    distance_km REAL NOT NULL, duration_s INTEGER NOT NULL, ascent_m INTEGER NOT NULL,
                    point_count INTEGER NOT NULL, segment_count INTEGER NOT NULL, bounds TEXT,
                    geometry BLOB NOT NULL, context_geometry BLOB NOT NULL, imported_at TEXT NOT NULL,
                    altitudes BLOB, metrics TEXT
                );
                CREATE INDEX IF NOT EXISTS activity_date ON activities(date);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(activities)')}
            if 'altitudes' not in columns:
                # Older libraries stored no per-point elevation; the 3D view backfills it.
                db.execute('ALTER TABLE activities ADD COLUMN altitudes BLOB')
            if 'metrics' not in columns:
                # Heart rate, power, cadence… stored as a small JSON blob.
                db.execute('ALTER TABLE activities ADD COLUMN metrics TEXT')
        self._drop_legacy_grid()
        self._normalize_sport_column()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        # Anaconda's SQLite build does not ship optional SQL math functions.
        db.create_function('floor', 1, math.floor, deterministic=True)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def _drop_legacy_grid(self):
        """Drop the per-cell heat tables older versions kept.

        The heat view is drawn from the track lines themselves now, so those tables were
        pure ballast (about 100 MB on a real library) and are removed on first start.
        """
        with self.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            legacy = sorted({'cells', 'cells_fine', 'cell_heat', 'meta'} & tables)
            if not legacy:
                return
            for name in legacy:
                db.execute(f'DROP TABLE "{name}"')
            db.commit()
            try:
                db.execute('VACUUM')   # hand the freed pages back to the file system
            except sqlite3.Error:
                pass

    def _normalize_sport_column(self):
        """Map activity types stored by older versions onto the current vocabulary."""
        with self.connect() as db:
            rows = db.execute('SELECT DISTINCT sport FROM activities').fetchall()
            changes = []
            for row in rows:
                name = normalize_sport(row[0])
                if name != row[0]:
                    changes.append((name, row[0]))
            if changes:
                db.executemany('UPDATE activities SET sport=? WHERE sport=?', changes)

    def import_file(self, path, filename=None):
        status, payload = prepare_one(str(Path(path)))
        if status != 'ok':
            raise ValueError(payload)
        return self._store(payload, filename or Path(path).name)

    def _store(self, prepared, filename):
        digest = prepared['digest']
        with self.connect() as db:
            if db.execute('SELECT id FROM activities WHERE content_hash=?', (digest,)).fetchone():
                return 'duplicate'
        meta = prepared['meta']
        with self.write_lock, self.connect() as db:
            cursor = db.execute('''INSERT OR IGNORE INTO activities
                (content_hash,filename,start_time,date,sport,distance_km,duration_s,ascent_m,
                point_count,segment_count,bounds,geometry,context_geometry,imported_at,altitudes,metrics)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                digest, filename, meta['start_time'], meta['date'], meta['sport'],
                meta['distance_km'], meta['duration_s'], meta['ascent_m'], meta['point_count'],
                meta['segment_count'], json.dumps(meta['bounds']), prepared['geometry'],
                prepared['context'], datetime.now(LOCAL_TZ).isoformat(timespec='seconds'),
                prepared.get('altitudes'),
                json.dumps(prepared.get('metrics') or {}, separators=(',', ':')) if prepared.get('metrics') else None,
            ))
            if not cursor.rowcount:
                return 'duplicate'
        with self.lock:
            self.version += 1
        self.cache.clear()
        return 'imported' if meta['point_count'] else 'no_gps'

    def backfill_altitudes(self, folder):
        """Store per-point elevation for records imported before it was kept.

        The source files are still there, so they are parsed again in the worker pool;
        only the new column is written and the geometry is left untouched.
        """
        folder = Path(folder)
        if not folder.is_dir():
            return 0
        paths = {}
        for path in folder.rglob('*'):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                paths.setdefault(path.name, path)
        with self.connect() as db:
            rows = db.execute('SELECT id,filename,segment_count,point_count FROM activities '
                              'WHERE altitudes IS NULL OR metrics IS NULL').fetchall()
        jobs = [(row['id'], row['segment_count'], row['point_count'], paths[row['filename']])
                for row in rows if row['filename'] in paths]
        if not jobs:
            return 0
        updated = 0
        for start in range(0, len(jobs), 64):
            block = jobs[start:start + 64]
            results = parallel_block(read_altitudes, [str(job[3]) for job in block],
                                     use_pool=len(block) >= 8 and parallel_enabled())
            payload = []
            for (activity_id, segments, points, _), (status, value) in zip(block, results):
                if status != 'ok' or not value:
                    continue
                if value['segments'] != segments or value['points'] != points:
                    continue      # file changed since import: do not attach foreign data
                altitudes = value.get('altitudes') or []
                metrics = value.get('metrics') or {}
                heights = zlib.compress(
                    json.dumps(altitudes, separators=(',', ':')).encode(), 6) if altitudes else None
                numbers = json.dumps(metrics, separators=(',', ':')) if metrics else None
                if heights is None and numbers is None:
                    continue
                payload.append((heights, numbers, activity_id))
            if payload:
                with self.write_lock, self.connect() as db:
                    db.executemany('UPDATE activities SET altitudes=COALESCE(?,altitudes), '
                                   'metrics=COALESCE(?,metrics) WHERE id=?', payload)
                updated += len(payload)
        return updated

    def _known_hashes(self, digests):
        if not digests:
            return set()
        found = set()
        with self.connect() as db:
            for start in range(0, len(digests), 400):
                chunk = digests[start:start + 400]
                marks = ','.join('?' * len(chunk))
                found.update(row[0] for row in db.execute(
                    f'SELECT content_hash FROM activities WHERE content_hash IN ({marks})', chunk))
        return found

    def begin_job(self, items, cleanup=False):
        with self.lock:
            if any(job['state'] == 'running' for job in self.jobs.values()):
                raise ValueError('已有导入任务正在运行，请等当前任务完成')
            job_id = uuid.uuid4().hex
            job = dict(id=job_id, state='running', total=len(items), done=0, imported=0,
                       duplicate=0, no_gps=0, failed=0, current='', errors=[], started_at=time.time(),
                       workers=worker_count(), parallel=parallel_enabled())
            self.jobs[job_id] = job
            # Keep only recent job reports; the database keeps all imported activities.
            while len(self.jobs) > 20:
                del self.jobs[next(iter(self.jobs))]
        threading.Thread(target=self._import_worker, args=(job, list(items), cleanup),
                         daemon=True, name='activity-import').start()
        return job_id

    def _import_worker(self, job, items, cleanup):
        try:
            size = max(2, worker_count() * 2)
            for start in range(0, len(items), size):
                self._import_block(job, items[start:start + size], cleanup)
        finally:
            with self.lock:
                job['state'] = 'completed'
                job['current'] = ''
                job['parallel'] = parallel_enabled()
                job['workers'] = worker_count() if job['parallel'] else 1
                job['elapsed_s'] = round(time.time() - job['started_at'], 1)
            (self.folder / 'last-import.json').write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')

    def _import_block(self, job, block, cleanup):
        # Checking content hashes first turns a re-scan into a read instead of a full parse.
        digests = [file_digest(path) for path, _ in block]
        known = self._known_hashes([d for d in digests if d])
        pending, duplicates = [], 0
        for (path, filename), digest in zip(block, digests):
            if digest and digest in known:
                duplicates += 1
                if cleanup:
                    Path(path).unlink(missing_ok=True)
            else:
                pending.append((path, filename))
        if duplicates:
            with self.lock:
                job['duplicate'] += duplicates
                job['done'] += duplicates
        if not pending:
            return
        with self.lock:
            job['current'] = pending[0][1]
        volume = 0
        for path, _ in pending:
            try:
                volume += Path(path).stat().st_size
            except OSError:
                pass
        outcomes = parallel_block(prepare_one, [str(path) for path, _ in pending],
                                  use_pool=volume >= PARALLEL_MIN_BYTES)
        for (path, filename), (status, payload) in zip(pending, outcomes):
            try:
                if status == 'ok':
                    result = self._store(payload, filename)
                else:
                    raise ValueError(payload)
                with self.lock:
                    job[result] += 1
            except Exception as exc:
                with self.lock:
                    job['failed'] += 1
                    job['errors'].append({'filename': filename, 'message': str(exc)[:400]})
            finally:
                if cleanup:
                    Path(path).unlink(missing_ok=True)
                with self.lock:
                    job['done'] += 1
                    job['current'] = filename

    def last_job(self):
        with self.lock:
            if self.jobs:
                return json.loads(json.dumps(list(self.jobs.values())[-1]))
        path = self.folder / 'last-import.json'
        try:
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        except (ValueError, OSError):
            return None

    # -- querying ----------------------------------------------------------------------

    @staticmethod
    def filters(args):
        terms, params = [], []
        for key, operator in [('from', '>='), ('to', '<=')]:
            value = args.get(key, '')
            if value:
                try:
                    datetime.strptime(value, '%Y-%m-%d')
                except ValueError:
                    raise ValueError('日期格式必须为 YYYY-MM-DD') from None
                terms.append(f'a.date {operator} ?')
                params.append(value)
        if args.get('from') and args.get('to') and args['from'] > args['to']:
            raise ValueError('开始日期不能晚于结束日期')
        sport = str(args.get('sport', 'all') or 'all').strip().lower()
        if sport == 'gps':
            terms.append('a.point_count>0')
            sport = 'all'
        group = SPORT_GROUPS.get(sport)
        if group:
            terms.append('a.sport IN (%s)' % ','.join('?' * len(group)))
            params.extend(group)
        if args.get('q', '').strip():
            terms.append('a.filename LIKE ?')
            params.append('%' + args['q'].strip()[:100] + '%')
        return ' AND '.join(terms) or '1=1', params

    def overview(self, args):
        where, params = self.filters(args)
        with self.connect() as db:
            totals = dict(db.execute(f'''SELECT count(*) AS count, coalesce(sum(distance_km),0) AS distance_km,
                coalesce(sum(duration_s),0) AS duration_s, coalesce(sum(ascent_m),0) AS ascent_m,
                coalesce(sum(point_count),0) AS point_count,
                coalesce(sum(point_count>0),0) AS gps_count, min(date) AS first_date, max(date) AS last_date
                FROM activities a WHERE {where}''', params).fetchone())
            bounds = [json.loads(r[0]) for r in db.execute(f'SELECT bounds FROM activities a WHERE {where} AND point_count>0', params)]
            totals['bounds'] = [min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds)] if bounds else None
            # Start in the most-used region; distant travel remains under "view all".
            totals['focus_bounds'] = totals['bounds']
            if bounds:
                centers = [((b[0]+b[2])/2, (b[1]+b[3])/2) for b in bounds]
                region = Counter((math.floor(c[0]/2), math.floor(c[1]/2)) for c in centers).most_common(1)[0][0]
                center = next(c for c in centers if (math.floor(c[0]/2), math.floor(c[1]/2)) == region)
                # Focus tight enough to show individual roads on the first paint; the
                # "view all" button still fits every activity.
                nearby = [b for b, c in zip(bounds, centers) if distance(center, c) < 35000]
                if not nearby:
                    nearby = [b for b, c in zip(bounds, centers) if distance(center, c) < 200000]
                totals['focus_bounds'] = [min(b[0] for b in nearby), min(b[1] for b in nearby), max(b[2] for b in nearby), max(b[3] for b in nearby)]
            latest = db.execute(f'''SELECT bounds, start_time, sport FROM activities a
                WHERE {where} AND point_count>0
                ORDER BY start_time DESC, id DESC LIMIT 1''', params).fetchone()
            if latest:
                totals['latest_bounds'] = json.loads(latest['bounds'])
                totals['latest_time'] = latest['start_time']
                totals['latest_sport'] = latest['sport']
            totals['library_count'] = db.execute('SELECT count(*) FROM activities').fetchone()[0]
            totals['by_sport'] = {row['sport']: row['count'] for row in db.execute(
                f'SELECT a.sport AS sport, count(*) AS count FROM activities a WHERE {where} GROUP BY a.sport', params)}
        totals['version'] = self.version
        return totals

    def activities(self, args):
        where, params = self.filters(args)
        limit = min(200, max(1, int(args.get('limit', 80))))
        offset = max(0, int(args.get('offset', 0)))
        with self.connect() as db:
            rows = db.execute(f'''SELECT id,filename,start_time,date,sport,distance_km,duration_s,ascent_m,
                point_count,segment_count FROM activities a WHERE {where}
                ORDER BY start_time DESC,id DESC LIMIT ? OFFSET ?''', params + [limit + 1, offset]).fetchall()
        return {'items': [dict(r) for r in rows[:limit]], 'has_more': len(rows) > limit, 'offset': offset}

    def activity(self, activity_id, mode='wgs84'):
        with self.connect() as db:
            row = db.execute('SELECT * FROM activities WHERE id=?', (activity_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        segments = json.loads(zlib.decompress(result.pop('geometry')))
        stored_altitudes = result.pop('altitudes', None)
        result.pop('context_geometry')
        result.pop('content_hash')
        result['altitudes'] = json.loads(zlib.decompress(stored_altitudes)) if stored_altitudes else []
        result['metrics'] = json.loads(result.get('metrics') or '{}')
        result['segments'] = transform_segments(segments, coords_mode({'coords': mode}))
        result['bounds'] = json.loads(result['bounds'])
        return result

    def _tracks(self, args):
        """Clip, convert and pack the visible tracks; returns encoded blobs and counters."""
        where, params = self.filters(args)
        mode = coords_mode(args)
        footprint = args.get('mode') == 'footprint'
        zoom = float(args.get('zoom', 12))
        # Detail geometry is only worth sending when the screen can resolve it.
        detailed = footprint and zoom >= 13
        column = 'geometry' if detailed else 'context_geometry'
        tolerance = track_tolerance(zoom)
        bbox = None
        if footprint:
            view = parse_bbox(args.get('bbox'))
            if view:
                # Include a small halo and the optional GCJ display shift.
                padding = .015 if mode != 'wgs84' else .001
                bbox = (view[0]-padding, view[1]-padding, view[2]+padding, view[3]+padding)
        limit = None if footprint else 1000
        key = ('tracks', self.version, where, tuple(params), mode, footprint, detailed,
               tolerance, bbox)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        # Only the bounding boxes are read first: pulling the geometry of every activity
        # on each request was the other half of the original cost (~130 MB here).
        with self.connect() as db:
            rows = db.execute(f'''SELECT id,bounds FROM activities a
                WHERE {where} AND point_count>0
                ORDER BY start_time DESC {'LIMIT 1000' if limit else ''}''', params).fetchall()
        if bbox:
            kept = []
            for row in rows:
                bounds = json.loads(row['bounds'])  # [min lat, min lon, max lat, max lon]
                if bounds[3] < bbox[0] or bounds[1] > bbox[2] or bounds[2] < bbox[1] or bounds[0] > bbox[3]:
                    continue
                kept.append(row)
            rows = kept
        with self.geom_lock:
            missing = [row['id'] for row in rows
                       if (row['id'], column, tolerance, mode) not in self.geom_cache]
        if missing:
            markers = ','.join('?' * len(missing))
            with self.connect() as db:
                blobs = {row['id']: row['blob'] for row in db.execute(
                    f'SELECT id,{column} AS blob FROM activities WHERE id IN ({markers})', missing)}
            jobs = [(activity_id, blobs[activity_id], column, tolerance, mode)
                    for activity_id in missing if activity_id in blobs]
            prepared = parallel_block(prepare_geometry, jobs,
                                     use_pool=len(jobs) >= 40 and parallel_enabled())
            with self.geom_lock:
                for key_, packed in prepared:
                    self.geom_cache[key_] = packed
                while len(self.geom_cache) > TRACK_GEOMETRY_CACHE:
                    self.geom_cache.popitem(last=False)
        # Clip in integer world pixels, so the viewport test needs no trigonometry.
        box = None
        if bbox:
            west, south, east, north = bbox
            left, top = project_world([[north, west]])[:2]
            right, bottom = project_world([[south, east]])[:2]
            box = (left, top, right, bottom)
        blobs, ids = [], []
        for row in rows:
            with self.geom_lock:
                segments = self.geom_cache.get((row['id'], column, tolerance, mode), [])
            if box is not None:
                clipped = []
                for segment in segments:
                    clipped.extend(clip_world(segment, box))
                segments = clipped
            blob = encode_tracks(segments)
            if blob:
                blobs.append(blob)
                # The browser needs to know which activity a line belongs to, so that a
                # click on the map can open the matching record.
                ids.append(row['id'])
        payload = (blobs, ids, {'count': len(rows), 'visible_count': len(blobs),
                                'limit': limit, 'detailed': detailed, 'tolerance': tolerance})
        self.cache.put(key, payload, sum(len(blob) for blob in blobs) * 2)
        return payload

    def coverage_points(self, mode='wgs84', per_segment=1200):
        """Decimated GPS points covering every activity, in the requested display frame.

        Used to work out which basemap tiles to store for offline use. One tile is at
        least a few hundred metres, so every ~1200th point of a segment is plenty.
        """
        with self.connect() as db:
            rows = db.execute('SELECT geometry FROM activities WHERE point_count>0').fetchall()
        points = []
        for row in rows:
            for segment in json.loads(zlib.decompress(row['geometry'])):
                step = max(1, len(segment) // per_segment)
                points.extend(segment[index] for index in range(0, len(segment), step))
                points.append(segment[-1])
        if mode == 'wgs_to_gcj':
            return [wgs_to_gcj(*point) for point in points]
        if mode == 'gcj02':
            return [gcj_to_wgs(*point) for point in points]
        return points

    def context_tracks(self, args):
        """Visible tracks as plain [lat, lon] lists (object API and tests)."""
        blobs, ids, meta = self._tracks(args)
        segments, segment_ids = [], []
        for blob, activity_id in zip(blobs, ids):
            for packed in decode_tracks(blob):
                segments.append([world_to_latlng(packed[i], packed[i + 1])
                                 for i in range(0, len(packed), 2)])
                segment_ids.append(activity_id)
        return dict(segments=segments, ids=segment_ids, **meta)

    def context_tracks_json(self, args):
        """Visible tracks packed for the browser: base64 int32 deltas at zoom 20."""
        blobs, ids, meta = self._tracks(args)
        return ('{"format":"i32z20","ids":[%s],"segments":[%s],"count":%d,"visible_count":%d,'
                '"limit":%s,"detailed":%s,"tolerance":%g}' % (
                    ','.join(str(value) for value in ids),
                    ','.join('"%s"' % blob for blob in blobs), meta['count'],
                    meta['visible_count'], 'null' if meta['limit'] is None else meta['limit'],
                    'true' if meta['detailed'] else 'false', meta['tolerance']))
