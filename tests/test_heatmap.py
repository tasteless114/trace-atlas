import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import pickle
import shutil
import struct
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid
import zipfile

import heatmap
from heatmap import (Library, decode_tracks, world_to_latlng, distance, gcj_to_wgs, normalize_sport, parse_fit,
                     parse_tcx, viewport_segments, wgs_to_gcj)
import server
from server import BodyReader, create_app, make_server, parse_multipart
from tiles import SOURCES, TileCache, lonlat_to_tile, tiles_for_bbox, tiles_for_points
from build_share import build_share

PNG = b'\x89PNG\r\n\x1a\n' + b'tile-bytes'


@contextlib.contextmanager
def live_server(app):
    """Run the standard-library server on a free port for real HTTP requests."""
    server_instance = make_server(app, '127.0.0.1', 0)
    thread = threading.Thread(target=server_instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server_instance.server_address[1]}'
    finally:
        server_instance.shutdown()
        server_instance.server_close()
        thread.join(timeout=5)


def request(url, method='GET', headers=None, body=None, host=None):
    prepared = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    if host:
        prepared.add_header('Host', host)
    try:
        with urllib.request.urlopen(prepared, timeout=20) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def api_json(url, method='GET', payload=None, headers=None):
    merged = {'X-Local-App': 'trace-atlas'}
    body = None
    if payload is not None:
        merged['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode()
    merged.update(headers or {})
    status, data, response_headers = request(url, method, merged, body)
    return status, json.loads(data.decode() or 'null'), response_headers


def multipart_body(files, boundary='----RideHeatmapBoundary7d1f'):
    """Build a browser-like multipart/form-data body."""
    chunks = []
    for name, filename, content in files:
        chunks.append(f'--{boundary}\r\n'.encode())
        chunks.append((f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode())
        chunks.append(content)
        chunks.append(b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode())
    return f'multipart/form-data; boundary={boundary}', b''.join(chunks)


def wait_for_job(app, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = app.library.last_job()
        if job and job['state'] == 'completed':
            return job
        time.sleep(.02)
    return app.library.last_job()


def workspace_temp(parent):
    """A throwaway folder inside the project data directory.

    ``tempfile.mkdtemp`` would create it with an owner-only DACL, which some
    restricted/company Windows setups cannot write into; a plain mkdir keeps the
    normal inherited permissions.
    """
    path = Path(parent) / f'test-{uuid.uuid4().hex[:10]}'
    path.mkdir()
    return path


def crc16(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def fit_bytes(points, start=1162512000):
    # Minimal real FIT record definition; fixtures do not contain user locations.
    data = bytearray(struct.pack('<BBBHB', 0x40, 0, 0, 20, 3))
    data.extend(bytes([253, 4, 0x86, 0, 4, 0x85, 1, 4, 0x85]))
    for index, point in enumerate(points):
        lat, lon, seconds = point
        lat_int = round(lat * (1 << 31) / 180) if lat is not None else 0x7fffffff
        lon_int = round(lon * (1 << 31) / 180) if lon is not None else 0x7fffffff
        data.extend(struct.pack('<BIii', 0, start + seconds, lat_int, lon_int))
    header = struct.pack('<BBHI4s', 14, 0x20, 2100, len(data), b'.FIT')
    header += struct.pack('<H', crc16(header))
    contents = header + data
    return contents + struct.pack('<H', crc16(contents))


def tcx_bytes(points, start='2024-05-01T08:00:00Z', sport='Biking'):
    """Real Training Center Database structure, namespaced like Garmin exports."""
    base = datetime.fromisoformat(start.replace('Z', '+00:00'))
    track = []
    for lat, lon, seconds, altitude in points:
        stamp = (base + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')
        position = ''
        if lat is not None and lon is not None:
            position = (f'<Position><LatitudeDegrees>{lat}</LatitudeDegrees>'
                        f'<LongitudeDegrees>{lon}</LongitudeDegrees></Position>')
        height = f'<AltitudeMeters>{altitude}</AltitudeMeters>' if altitude is not None else ''
        track.append(f'<Trackpoint><Time>{stamp}</Time>{position}{height}</Trackpoint>')
    seconds = points[-1][2] - points[0][2] if len(points) > 1 else 0
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
            f'<Activities><Activity Sport="{sport}"><Id>{start}</Id>'
            f'<Lap StartTime="{start}"><TotalTimeSeconds>{seconds}</TotalTimeSeconds>'
            f'<DistanceMeters>240</DistanceMeters><Track>{"".join(track)}</Track></Lap>'
            f'</Activity></Activities></TrainingCenterDatabase>').encode()


TRACK = [(40.1, 116.2, 0), (40.1, 116.201, 30), (40.1, 116.202, 60)]
TCX_TRACK = [(40.1, 116.2, 0, 45.0), (40.1, 116.2015, 20, 47.0), (40.1, 116.203, 40, 52.0)]


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.test_parent = (Path(__file__).resolve().parents[1] / '.test-tmp').resolve()
        self.test_parent.mkdir(exist_ok=True)
        self.root = workspace_temp(self.test_parent)
        self.library = Library(self.root / 'data')
        self.sample = self.root / 'sample.fit'
        self.sample.write_bytes(fit_bytes(TRACK))
        self.tcx = self.root / 'sample.tcx'
        self.tcx.write_bytes(tcx_bytes(TCX_TRACK, sport='Running'))

    def tearDown(self):
        assert self.root.resolve().is_relative_to(self.test_parent)
        shutil.rmtree(self.root, ignore_errors=True)

    def test_real_fit_decode_and_timezone(self):
        parsed = parse_fit(self.sample.read_bytes())
        self.assertEqual(parsed['point_count'], 3)
        self.assertAlmostEqual(parsed['segments'][0][0][0], 40.1, places=5)
        self.assertAlmostEqual(parsed['segments'][0][0][1], 116.2, places=5)
        self.assertTrue(parsed['start_time'].endswith('+08:00'))
        self.assertGreater(parsed['distance_km'], .16)

    def test_tcx_decode_and_running_sport(self):
        parsed = parse_tcx(tcx_bytes(TCX_TRACK, sport='Running'))
        self.assertEqual(parsed['point_count'], 3)
        self.assertEqual(parsed['sport'], 'running')
        self.assertEqual(parsed['segment_count'], 1)
        self.assertAlmostEqual(parsed['segments'][0][1][0], 40.1, places=5)
        self.assertAlmostEqual(parsed['segments'][0][1][1], 116.2015, places=5)
        self.assertTrue(parsed['start_time'].endswith('+08:00'))
        self.assertEqual(parsed['start_time'][11:16], '16:00')  # 08:00Z in Beijing time
        self.assertGreater(parsed['distance_km'], .2)
        self.assertEqual(parsed['ascent_m'], 7)  # 45 -> 47 -> 52 metres

    def test_tcx_without_position_still_summarises(self):
        parsed = parse_tcx(tcx_bytes([(None, None, 0, None), (None, None, 60, None)]))
        self.assertEqual(parsed['point_count'], 0)
        self.assertIsNone(parsed['bounds'])
        self.assertEqual(parsed['duration_s'], 60)

    def test_ascent_accumulates_on_dense_records_but_ignores_noise(self):
        # 1-second sampling: a steady 0.3 m climb per point must still add up.
        climb = [(40.1 + i * 1e-5, 116.2 + i * 1e-5, i, 40.0 + i * 0.3) for i in range(1000)]
        parsed = parse_tcx(tcx_bytes(climb))
        self.assertGreater(parsed['ascent_m'], 250)
        self.assertLess(parsed['ascent_m'], 310)
        # A flat road with +-0.6 m of barometric noise must not invent a climb.
        noisy = [(40.1 + i * 1e-5, 116.2, i, 40.0 + (0.6 if i % 2 else 0.0)) for i in range(1000)]
        parsed = parse_tcx(tcx_bytes(noisy))
        self.assertLessEqual(parsed['ascent_m'], 3)

    def test_tcx_gps_gap_splits_segments(self):
        points = TCX_TRACK + [(40.11, 116.21, 300, 50.0), (40.11, 116.211, 320, 50.0)]
        parsed = parse_tcx(tcx_bytes(points))
        self.assertEqual(parsed['segment_count'], 2)
        self.assertLess(parsed['distance_km'], .4)

    def test_both_formats_import_into_one_library(self):
        self.assertEqual(self.library.import_file(self.sample), 'imported')
        self.assertEqual(self.library.import_file(self.tcx), 'imported')
        self.assertEqual(self.library.overview({})['count'], 2)
        self.assertEqual(self.library.overview({'sport': 'running'})['count'], 1)
        self.assertEqual(self.library.overview({'sport': 'all'})['count'], 2)
        # The minimal FIT fixture carries no session, so its type stays unknown.
        self.assertEqual({item['sport'] for item in self.library.activities({})['items']},
                         {'unknown', 'running'})

    def test_sport_filter_groups(self):
        self.library.import_file(self.tcx)
        ride = self.root / 'ride.tcx'
        ride.write_bytes(tcx_bytes(TCX_TRACK, start='2024-06-01T08:00:00Z', sport='Biking'))
        self.library.import_file(ride)
        plain = self.root / 'plain.fit'
        plain.write_bytes(fit_bytes(TRACK, start=1162598400))
        self.library.import_file(plain)
        self.assertEqual(normalize_sport('trail_running'), 'running')
        self.assertEqual(normalize_sport('e-biking'), 'e_biking')
        self.assertEqual(normalize_sport('generic'), 'unknown')
        self.assertEqual(self.library.overview({'sport': 'running'})['count'], 1)
        self.assertEqual(self.library.overview({'sport': 'cycling'})['count'], 1)
        self.assertEqual(self.library.overview({'sport': 'all'})['count'], 3)
        self.assertEqual(self.library.overview({'sport': 'gps'})['count'], 3)
        self.assertEqual(self.library.overview({})['by_sport'], {'running': 1, 'cycling': 1, 'unknown': 1})

    def test_old_sport_names_are_migrated(self):
        self.library.import_file(self.tcx)
        with self.library.connect() as db:
            db.execute("UPDATE activities SET sport='trail_running'")
        reopened = Library(self.root / 'data')
        self.assertEqual(reopened.overview({})['by_sport'], {'running': 1})
        self.assertEqual(reopened.overview({'sport': 'running'})['count'], 1)

    def test_duplicate_files_are_skipped(self):
        self.assertEqual(self.library.import_file(self.sample), 'imported')
        self.assertEqual(self.library.import_file(self.tcx), 'imported')
        self.assertEqual(self.library.overview({})['count'], 2)
        self.assertEqual(self.library.import_file(self.sample, 'renamed.fit'), 'duplicate')
        self.assertEqual(self.library.import_file(self.tcx, 'again.tcx'), 'duplicate')
        self.assertEqual(self.library.overview({})['count'], 2)
        self.assertEqual(self.library.overview({})['distance_km'] > 0, True)

    def test_gps_gap_does_not_create_bridge(self):
        parsed = parse_fit(fit_bytes(TRACK + [(40.11, 116.21, 600), (40.11, 116.211, 630)]))
        self.assertEqual(parsed['segment_count'], 2)
        self.assertLess(parsed['distance_km'], .3)

    def test_indoor_activity_retains_summary_without_heat(self):
        indoor = self.root / 'indoor.fit'
        indoor.write_bytes(fit_bytes([(None, None, 0), (None, None, 60)]))
        self.assertEqual(self.library.import_file(indoor), 'no_gps')
        self.assertEqual(self.library.overview({})['count'], 1)
        self.assertEqual(self.library.overview({'sport': 'gps'})['count'], 0)

    def test_corrupt_file_never_enters_database(self):
        bad = self.root / 'bad.fit'
        bad.write_bytes(b'not a fit')
        with self.assertRaises(Exception):
            self.library.import_file(bad)
        broken = self.root / 'broken.tcx'
        broken.write_bytes(b'<TrainingCenterDatabase><Activities')
        with self.assertRaises(Exception):
            self.library.import_file(broken)
        self.assertEqual(self.library.overview({})['count'], 0)

    def test_date_filters_and_invalid_range(self):
        self.library.import_file(self.sample)
        date = self.library.overview({})['first_date']
        self.assertEqual(self.library.overview({'from': date, 'to': date})['count'], 1)
        self.assertEqual(self.library.overview({'from': '2099-01-01'})['count'], 0)
        with self.assertRaises(ValueError):
            self.library.overview({'from': '2026-10-01', 'to': '2026-01-01'})

    def test_conversion_is_optional_and_leaves_non_china_unchanged(self):
        self.assertEqual(gcj_to_wgs(51.5, -.1), (51.5, -.1))
        self.assertEqual(wgs_to_gcj(51.5, -.1), (51.5, -.1))
        converted = gcj_to_wgs(40.1, 116.2)
        self.assertGreater(distance(converted, (40.1, 116.2)), 100)
        shifted = wgs_to_gcj(40.1, 116.2)
        self.assertGreater(distance(shifted, (40.1, 116.2)), 100)
        self.assertLess(distance(gcj_to_wgs(*shifted), (40.1, 116.2)), 1)
        self.library.import_file(self.sample)
        original = self.library.activity(1)['segments']
        corrected = self.library.activity(1, 'gcj02')['segments']
        domestic = self.library.activity(1, 'wgs_to_gcj')['segments']
        self.assertNotEqual(original, corrected)
        self.assertNotEqual(original, domestic)
        self.assertEqual(original, self.library.activity(1)['segments'])

    def test_domestic_tracks_are_shifted(self):
        self.library.import_file(self.sample)
        plain = self.library.context_tracks({'mode': 'footprint', 'zoom': 14})
        shifted = self.library.context_tracks({'mode': 'footprint', 'zoom': 14, 'coords': 'wgs_to_gcj'})
        self.assertEqual(len(plain['segments']), len(shifted['segments']))
        self.assertGreater(distance(plain['segments'][0][0], shifted['segments'][0][0]), 100)
        self.assertGreater(distance(shifted['segments'][0][0], [40.1, 116.2]), 100)

    def test_local_access_and_batch_upload(self):
        app = create_app(self.root / 'web-data')
        with live_server(app) as base:
            self.assertEqual(request(base + '/api/status', host='evil.example')[0], 403)
            self.assertEqual(request(base + '/api/import/folder', 'POST',
                                     {'Content-Type': 'application/json'},
                                     json.dumps({'folder': str(self.root)}).encode())[0], 403)
            self.assertEqual(api_json(base + '/api/import/folder', 'POST', {'folder': str(self.root)},
                                      {'Origin': 'https://evil.example'})[0], 403)
            content_type, body = multipart_body([('files', 'one.fit', fit_bytes(TRACK)),
                                                 ('files', 'two.fit', fit_bytes(TRACK))])
            status, data, _ = request(base + '/api/import/upload', 'POST',
                                      {'Content-Type': content_type, 'X-Local-App': 'trace-atlas'}, body)
            self.assertEqual(status, 202)
            job = wait_for_job(app)
            self.assertEqual(job['done'], 2)
            self.assertEqual(job['imported'], 1)
            self.assertEqual(job['duplicate'], 1)
            self.assertEqual(list((app.library.folder / 'uploads').glob('*')), [])
            self.assertEqual(request(base + '/data/rides.sqlite3')[0], 404)
            self.assertEqual(request(base + '/static/app.js')[0], 200)

    def test_tcx_upload_and_extension_rejection(self):
        app = create_app(self.root / 'web-data')
        with live_server(app) as base:
            content_type, body = multipart_body([('files', 'run.tcx', tcx_bytes(TCX_TRACK, sport='Running'))])
            status, _, _ = request(base + '/api/import/upload', 'POST',
                                   {'Content-Type': content_type, 'X-Local-App': 'trace-atlas'}, body)
            self.assertEqual(status, 202)
            self.assertEqual(wait_for_job(app)['imported'], 1)
            self.assertEqual(app.library.overview({})['by_sport'], {'running': 1})
            content_type, body = multipart_body([('files', 'notes.txt', b'plain text')])
            status, data, _ = request(base + '/api/import/upload', 'POST',
                                      {'Content-Type': content_type, 'X-Local-App': 'trace-atlas'}, body)
            self.assertEqual(status, 400)
            self.assertIn('FIT', json.loads(data)['error'])

    def test_legacy_cell_tables_are_dropped(self):
        self.library.import_file(self.sample)
        with self.library.connect() as db:
            for name in ('cells', 'cells_fine', 'cell_heat', 'meta'):
                db.execute(f'CREATE TABLE IF NOT EXISTS "{name}" (a INTEGER)')
        reopened = Library(self.root / 'data')
        with reopened.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('cells', tables)
        self.assertNotIn('cells_fine', tables)
        self.assertNotIn('cell_heat', tables)
        self.assertNotIn('meta', tables)
        self.assertEqual(reopened.overview({})['count'], 1)  # library itself is untouched

    def test_footprints_include_all_activities_and_respect_filters(self):
        self.library.import_file(self.sample)
        with self.library.connect() as db:
            for i in range(1001):
                db.execute('''INSERT INTO activities(content_hash,filename,start_time,date,sport,distance_km,
                    duration_s,ascent_m,point_count,segment_count,bounds,geometry,context_geometry,imported_at)
                    SELECT ?,filename,start_time,date,sport,distance_km,duration_s,ascent_m,point_count,
                    segment_count,bounds,geometry,context_geometry,imported_at FROM activities WHERE id=1''', (f'fixture-{i}',))
        footprints = self.library.context_tracks({'mode': 'footprint', 'zoom': 10})
        self.assertEqual(footprints['count'], 1002)
        self.assertEqual(len(footprints['segments']), 1002)
        self.assertIsNone(footprints['limit'])
        self.assertEqual(self.library.context_tracks({})['count'], 1000)
        self.assertEqual(self.library.context_tracks({'mode': 'footprint', 'from': '2099-01-01'})['count'], 0)

    def test_altitudes_are_stored_per_point(self):
        self.assertEqual(self.library.import_file(self.sample), 'imported')
        item = self.library.activity(1)
        altitudes = item['altitudes']
        self.assertEqual(len(altitudes), len(item['segments']))
        for heights, segment in zip(altitudes, item['segments']):
            self.assertEqual(len(heights), len(segment))
        flat = [value for heights in altitudes for value in heights]
        # The fixture has no altitude field, so every slot may be empty -- but it must
        # exist and line up one-to-one with the points.
        self.assertTrue(flat)
        self.assertTrue(all(value is None or isinstance(value, (int, float)) for value in flat))

    def test_altitudes_backfill_skips_a_changed_file(self):
        source = self.root / 'rides'
        source.mkdir(exist_ok=True)
        self.assertEqual(self.library.import_file(self.sample), 'imported')
        (source / self.sample.name).write_bytes(self.sample.read_bytes())
        with self.library.connect() as db:
            db.execute('UPDATE activities SET altitudes=NULL')
        moved = self.library.backfill_altitudes(source)
        self.assertEqual(moved, 1)
        self.assertTrue(self.library.activity(1)['altitudes'])
        # A file whose geometry no longer matches must not contribute heights.
        with self.library.connect() as db:
            db.execute('UPDATE activities SET altitudes=NULL, point_count=point_count+1')
        self.assertEqual(self.library.backfill_altitudes(source), 0)
        self.assertEqual(self.library.activity(1)['altitudes'], [])

    def test_gpx_track_with_extensions(self):
        gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk><type>running</type><trkseg>
    <trkpt lat="40.100" lon="116.200"><ele>50</ele><time>2026-10-20T00:00:00Z</time>
      <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>140</gpxtpx:hr>
      <gpxtpx:cad>170</gpxtpx:cad></gpxtpx:TrackPointExtension></extensions></trkpt>
    <trkpt lat="40.101" lon="116.201"><ele>56</ele><time>2026-10-20T00:00:30Z</time>
      <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr>
      <gpxtpx:cad>174</gpxtpx:cad></gpxtpx:TrackPointExtension><power>250</power></extensions></trkpt>
    <trkpt lat="40.102" lon="116.202"><ele>52</ele><time>2026-10-20T00:01:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
        gpx_path = self.root / 'ride.gpx'
        gpx_path.write_bytes(gpx)
        self.assertEqual(self.library.import_file(gpx_path), 'imported')
        item = self.library.activity(1)
        self.assertEqual(item['sport'], 'running')
        self.assertEqual(item['point_count'], 3)
        self.assertEqual(item['duration_s'], 60)
        self.assertEqual(item['metrics']['max_hr'], 150)
        self.assertEqual(item['metrics']['max_power'], 250)
        self.assertEqual(len(item['altitudes'][0]), 3)


    def test_metrics_are_read_from_the_file(self):
        self.assertEqual(self.library.import_file(self.tcx), 'imported')
        item = self.library.activity(1)
        self.assertIn('metrics', item)
        self.assertIsInstance(item['metrics'], dict)

    def test_metrics_backfill_updates_old_records(self):
        source = self.root / 'rides'
        source.mkdir(exist_ok=True)
        self.assertEqual(self.library.import_file(self.sample), 'imported')
        (source / self.sample.name).write_bytes(self.sample.read_bytes())
        with self.library.connect() as db:
            db.execute('UPDATE activities SET altitudes=NULL, metrics=NULL')
        moved = self.library.backfill_altitudes(source)
        self.assertEqual(moved, 1)
        self.assertIsInstance(self.library.activity(1)['metrics'], dict)

    def test_track_payload_ids_identify_each_activity(self):
        self.library.import_file(self.sample)
        self.library.import_file(self.tcx)
        args = {'mode': 'footprint', 'zoom': 15, 'bbox': '116.0,39.9,116.4,40.3'}
        payload = json.loads(self.library.context_tracks_json(args))
        self.assertEqual(len(payload['ids']), len(payload['segments']))
        self.assertEqual(len(payload['ids']), 2)
        for blob, activity_id in zip(payload['segments'], payload['ids']):
            packed = decode_tracks(blob)
            start = world_to_latlng(packed[0][0], packed[0][1])
            own = self.library.activity(activity_id)['segments']
            # The clipped line must start on the track it claims to belong to.
            closest = min(distance(start, segment[0]) for segment in own)
            self.assertLess(closest, 1)
        # The object API exposes the same pairing.
        plain = self.library.context_tracks(args)
        self.assertEqual(plain['ids'], payload['ids'])

    def test_tracks_json_matches_dict_payload(self):
        self.library.import_file(self.sample)
        payload = json.loads(self.library.context_tracks_json(
            {'mode': 'footprint', 'zoom': 14, 'bbox': '116.1,40.0,116.3,40.2'}))
        self.assertEqual(payload['format'], 'i32z20')
        self.assertEqual(payload['count'], 1)
        self.assertEqual(len(payload['segments']), payload['visible_count'])
        # The packed form must decode back to the same geometry the object API returns.
        packed = [part for blob in payload['segments'] for part in decode_tracks(blob)]
        plain = self.library.context_tracks({'mode': 'footprint', 'zoom': 14,
                                             'bbox': '116.1,40.0,116.3,40.2'})['segments']
        self.assertEqual(sum(len(part) // 2 for part in packed), sum(len(s) for s in plain))
        first = world_to_latlng(packed[0][0], packed[0][1])
        self.assertLess(distance(first, plain[0][0]), 1)

    def test_zoomed_footprints_keep_the_path_and_viewport_crossings(self):
        self.library.import_file(self.sample)
        detailed = self.library.context_tracks({'mode': 'footprint', 'zoom': 14})
        self.assertTrue(detailed['detailed'])
        raw = self.library.activity(1)['segments']
        self.assertEqual(len(detailed['segments']), len(raw))
        for got, want in zip(detailed['segments'], raw):
            # Simplified for the screen, but the path must start and end where it really
            # does and no point may drift more than a couple of metres off the line.
            self.assertLess(distance(got[0], want[0]), 1)    # 0.15 m integer grid
            self.assertLess(distance(got[-1], want[-1]), 1)
            for point in want:
                self.assertLess(min(distance(point, other) for other in got), 90)
        # A coarser zoom drops points aggressively instead of shipping raw geometry.
        far = self.library.context_tracks({'mode': 'footprint', 'zoom': 9})
        self.assertFalse(far['detailed'])
        self.assertLessEqual(sum(len(s) for s in far['segments']),
                             sum(len(s) for s in detailed['segments']))
        route = [[[40.1, 116], [40.1, 116.1], [40.1, 116.2], [40.1, 116.3]]]
        # The edge crosses the viewport despite both endpoints being outside it.
        self.assertEqual(viewport_segments(route, (116.12, 40.09, 116.18, 40.11)), [[[40.1, 116.1], [40.1, 116.2]]])
        self.assertEqual(viewport_segments(route, (117, 40, 118, 41)), [])

    def test_moved_project_scans_the_single_activity_folder(self):
        with patch('server.ROOT', self.root):
            app = create_app(self.root / 'moved-data')
            rides = self.root / '运动数据'
            self.assertTrue(rides.is_dir())
            self.assertEqual(list(rides.iterdir()), [])
            with live_server(app) as base:
                status, data, _ = api_json(base + '/api/status')
                self.assertEqual(status, 200)
                self.assertEqual(data['source_folders'], [str(rides.resolve())])
                self.assertEqual(data['extensions'], ['.fit', '.tcx', '.gpx'])
                # Both formats live side by side, subfolders included.
                (rides / 'friend.fit').write_bytes(fit_bytes(TRACK))
                (rides / 'sub').mkdir(exist_ok=True)
                (rides / 'sub' / 'friend.tcx').write_bytes(tcx_bytes(TCX_TRACK, sport='Running'))
                self.assertEqual(api_json(base + '/api/import/folder', 'POST', {})[0], 202)
                wait_for_job(app)
            self.assertEqual(app.library.overview({})['count'], 2)

    def test_split_fit_tcx_folders_are_merged_on_startup(self):
        with patch('server.ROOT', self.root):
            legacy_fit, legacy_tcx = self.root / 'fit', self.root / 'tcx'
            legacy_fit.mkdir(exist_ok=True)
            legacy_tcx.mkdir(exist_ok=True)
            (legacy_fit / 'a.fit').write_bytes(fit_bytes(TRACK))
            (legacy_tcx / 'sub').mkdir(exist_ok=True)
            (legacy_tcx / 'sub' / 'b.tcx').write_bytes(tcx_bytes(TCX_TRACK, sport='Running'))
            app = create_app(self.root / 'legacy-data')
            rides = self.root / '运动数据'
            self.assertTrue((rides / 'a.fit').is_file())
            self.assertTrue((rides / 'sub' / 'b.tcx').is_file())
            self.assertFalse(legacy_fit.exists())
            self.assertEqual(app.library.overview({})['count'], 0)   # moved, not imported

    def test_status_exposes_basemaps_and_worker_count(self):
        app = create_app(self.root / 'web-data')
        with live_server(app) as base:
            status, data, headers = api_json(base + '/api/status')
        self.assertEqual(status, 200)
        self.assertIn('worker-src', headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        keys = [item['key'] for item in data['basemaps']]
        self.assertEqual(keys[0], 'amap_road')
        self.assertIn('amap_sat', keys)
        self.assertIn('none', keys)
        self.assertEqual(data['basemaps'][0]['coords'], 'gcj02')
        self.assertEqual(data['default_basemap'], 'amap_road')
        self.assertGreaterEqual(data['workers'], 1)
        self.assertGreaterEqual(data['pool']['cores'], 1)
        self.assertIn(data['pool']['state'], ('ready', 'serial'))

    def test_share_zip_has_empty_activity_folders_and_excludes_local_data(self):
        archive_path = build_share(self.root / 'share.zip')
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertIn('行迹/运动数据/', names)
            # 运动数据/ 里只允许预置的合成示例轨迹，绝不能出现真实活动文件或数据库。
            activity_entries = [name for name in names if name.startswith('行迹/运动数据/')]
            demo = [name for name in activity_entries if Path(name).suffix.lower() in ('.gpx', '.tcx', '.fit')]
            self.assertTrue(all('示例' in Path(name).name for name in demo), demo)
            self.assertEqual([name for name in activity_entries if name not in demo], ['行迹/运动数据/'])
            self.assertFalse(any('/data/' in name or '/.venv/' in name or name.lower().endswith(('.sqlite3', '.jpg')) for name in names))
            self.assertFalse(any('/data/' in name or '/.venv/' in name or name.lower().endswith(('.fit', '.tcx', '.sqlite3', '.jpg')) for name in names))
            config = json.loads(archive.read('行迹/config.json'))
            self.assertEqual(config['folder'], '运动数据')
            self.assertIn('行迹/启动行迹.cmd', names)
            self.assertIn('行迹/停止行迹.cmd', names)
            self.assertIn('行迹/tiles.py', names)
            self.assertNotIn('行迹/首次安装.cmd', names)  # nothing is installed any more
            self.assertNotIn('行迹/requirements.txt', names)
            self.assertIn('行迹/static/heat-worker.js', names)
            self.assertIn('行迹/vendor/fitparse/base.py', names)
            self.assertIn('行迹/vendor/fitparse/profile.py', names)
            self.assertIn('行迹/vendor/fitparse/LICENSE.txt', names)
            self.assertIn('行迹/vendor/README.md', names)
            self.assertFalse(any(name.endswith('.pyc') for name in names))


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.test_parent = (Path(__file__).resolve().parents[1] / '.test-tmp').resolve()
        self.test_parent.mkdir(exist_ok=True)
        self.root = workspace_temp(self.test_parent)
        self.sample = self.root / 'sample.fit'
        self.sample.write_bytes(fit_bytes(TRACK))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_pool_results_keep_input_order_and_isolate_failures(self):
        class FakePool:
            def __init__(self, fail):
                self.fail = fail

            def map(self, function, items):
                if self.fail:
                    raise RuntimeError('pool unavailable')
                return [function(item) for item in items]

            def shutdown(self, **kwargs):
                pass

        paths = [str(self.sample), str(self.root / 'missing.fit'), str(self.sample)]
        with patch('heatmap._pool', lambda: FakePool(False)):
            outcomes = heatmap.parallel_block(heatmap.prepare_one, paths)
        self.assertEqual([outcome[0] for outcome in outcomes], ['ok', 'error', 'ok'])
        self.assertEqual(outcomes[0][1]['meta']['point_count'], 3)
        with patch('heatmap._pool', lambda: FakePool(True)), patch('heatmap._POOL_BROKEN', False):
            fallback = heatmap.parallel_block(heatmap.prepare_one, paths)
        self.assertEqual([outcome[0] for outcome in fallback], ['ok', 'error', 'ok'])

    def test_worker_payloads_survive_a_process_boundary(self):
        status, prepared = heatmap.prepare_one(str(self.sample))
        self.assertEqual(status, 'ok')
        restored = pickle.loads(pickle.dumps(prepared))
        self.assertEqual(restored['meta']['point_count'], 3)
        self.assertEqual(restored['digest'], prepared['digest'])
        job = (1, restored['geometry'], 'geometry', 0.5, 'wgs84')
        key, packed = heatmap.prepare_geometry(pickle.loads(pickle.dumps(job)))
        self.assertEqual(key, (1, 'geometry', 0.5, 'wgs84'))
        self.assertEqual(len(packed), 1)
        first = heatmap.world_to_latlng(packed[0][0], packed[0][1])
        self.assertLess(distance(first, [40.1, 116.2]), 1)
        _, broken = heatmap.prepare_geometry((99, b'not zlib', 'geometry', 12.0, 'wgs84'))
        self.assertEqual(broken, [])

    def test_small_workloads_skip_the_pool(self):
        class RecordingPool:
            def map(self, function, items):
                raise AssertionError('pool must not be used for a tiny job')

            def shutdown(self, **kwargs):
                pass

        with patch('heatmap._pool', lambda: RecordingPool()):
            outcomes = heatmap.parallel_block(heatmap.prepare_one, [str(self.sample)], use_pool=False)
        self.assertEqual(outcomes[0][0], 'ok')

    def test_single_worker_configuration_stays_serial(self):
        previous = heatmap.worker_count()
        try:
            heatmap.configure_workers(1)
            self.assertEqual(heatmap.worker_count(), 1)
            self.assertFalse(heatmap.parallel_enabled())
            self.assertIsNone(heatmap._pool())
            self.assertEqual(heatmap.parallel_block(heatmap.prepare_one, [str(self.sample)])[0][0], 'ok')
        finally:
            heatmap.configure_workers(1)
            heatmap._POOL_BROKEN = False
            self.assertEqual(heatmap.worker_count(), 1)


class TileTests(unittest.TestCase):
    def setUp(self):
        self.test_parent = (Path(__file__).resolve().parents[1] / '.test-tmp').resolve()
        self.test_parent.mkdir(exist_ok=True)
        self.root = workspace_temp(self.test_parent)
        self.cache = TileCache(self.root / 'tiles')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_tile_is_downloaded_once_then_served_from_disk(self):
        calls = []

        def fetch(cache, spec, z, x, y):
            calls.append((z, x, y))
            return PNG

        with patch.object(TileCache, '_fetch', fetch):
            self.assertEqual(self.cache.get('amap_road', 10, 5, 6), PNG)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.cache.get('amap_road', 10, 5, 6), PNG)  # memory hit
            self.assertEqual(len(calls), 1)
            self.cache.memory.clear()
            self.assertEqual(self.cache.get('amap_road', 10, 5, 6), PNG)  # disk hit after restart
            self.assertEqual(len(calls), 1)
        self.assertTrue(self.cache.path_for('amap_road', 10, 5, 6).is_file())

    def test_failed_tiles_are_remembered_briefly(self):
        calls = []

        def fetch(cache, spec, z, x, y):
            calls.append(1)
            return b''

        with patch.object(TileCache, '_fetch', fetch):
            self.assertEqual(self.cache.get('amap_road', 12, 1, 1), b'')
            self.assertEqual(self.cache.get('amap_road', 12, 1, 1), b'')
        self.assertEqual(len(calls), 1)

    def test_tile_urls_use_domestic_subdomains(self):
        seen = []
        real = TileCache._fetch
        try:
            def capture(cache, spec, z, x, y):
                seen.append(spec['url'].format(s='1', x=x, y=y, z=z))
                return b''
            TileCache._fetch = capture
            self.cache.get('amap_road', 9, 3, 4)
        finally:
            TileCache._fetch = real
        self.assertIn('autonavi.com', seen[0])
        self.assertIn('style=7', seen[0])
        self.assertEqual(SOURCES['amap_road']['coords'], 'gcj02')

    def test_tile_route_serves_png_with_cache_headers(self):
        app = create_app(self.root / 'data')
        with live_server(app) as base:
            with patch.object(TileCache, 'get', lambda self, source, z, x, y: PNG):
                status, data, headers = request(base + '/api/tiles/amap_road/10/5/6.png')
                self.assertEqual(status, 200)
                self.assertEqual(data, PNG)
                self.assertEqual(headers['Content-Type'], 'image/png')
                self.assertIn('max-age', headers['Cache-Control'])
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(request(base + '/api/tiles/unknown/10/5/6.png')[0], 404)
            self.assertEqual(request(base + '/api/tiles/amap_road/21/5/6.png')[0], 404)
            with patch.object(TileCache, 'get', lambda self, source, z, x, y: b''):
                self.assertEqual(request(base + '/api/tiles/amap_road/10/5/6.png')[0], 404)


class MultipartTests(unittest.TestCase):
    def setUp(self):
        self.test_parent = (Path(__file__).resolve().parents[1] / '.test-tmp').resolve()
        self.test_parent.mkdir(exist_ok=True)
        self.root = workspace_temp(self.test_parent)
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_streaming_parser_keeps_bytes_exact(self):
        # Deliberately contains CRLF sequences that look like boundaries at chunk edges.
        payload = (bytes(range(256)) * 900) + b'\r\n--not-the-boundary\r\n' + b'\x00\xff' * 5000
        content_type, body = multipart_body([('files', 'ride.fit', payload),
                                             ('files', 'run.tcx', b'<TrainingCenterDatabase/>')])
        items = parse_multipart(BodyReader(io.BytesIO(body), len(body)), content_type,
                                self.uploads, ('.fit', '.tcx'))
        self.assertEqual([name for _, name in items], ['ride.fit', 'run.tcx'])
        self.assertEqual(items[0][0].read_bytes(), payload)
        self.assertEqual(items[1][0].read_bytes(), b'<TrainingCenterDatabase/>')

    def test_parser_cleans_up_when_a_later_file_is_rejected(self):
        content_type, body = multipart_body([('files', 'fine.fit', b'first'),
                                             ('files', 'notes.txt', b'second')])
        with self.assertRaises(ValueError):
            parse_multipart(BodyReader(io.BytesIO(body), len(body)), content_type,
                            self.uploads, ('.fit', '.tcx'))
        self.assertEqual(list(self.uploads.iterdir()), [])


class TileMathTests(unittest.TestCase):
    @staticmethod
    def reference_tile(lat, lon, zoom):
        """Independent implementation of the standard slippy-map formula."""
        scale = 1 << zoom
        x = int((lon + 180.0) / 360.0 * scale)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * scale)
        return x, y

    def test_tile_index_matches_the_standard_formula(self):
        for lat, lon in [(39.9042, 116.4074), (21.0318, 110.3796), (-33.86, 151.2), (51.5, -0.12)]:
            for zoom in (5, 10, 14, 18):
                self.assertEqual(lonlat_to_tile(lat, lon, zoom), self.reference_tile(lat, lon, zoom))

    def test_bbox_cover_includes_all_corners(self):
        tiles = tiles_for_bbox((116.2, 39.8, 116.6, 40.1), 12)
        corners = {(12, *lonlat_to_tile(40.1, 116.2, 12)), (12, *lonlat_to_tile(40.1, 116.6, 12)),
                   (12, *lonlat_to_tile(39.8, 116.2, 12)), (12, *lonlat_to_tile(39.8, 116.6, 12))}
        self.assertTrue(corners <= tiles)
        self.assertGreater(len(tiles), len(corners))

    def test_points_cover_adds_a_ring_of_neighbours(self):
        point = (39.9042, 116.4074)
        single = tiles_for_points([point], 14, margin=0)
        ring = tiles_for_points([point], 14, margin=1)
        self.assertEqual(len(single), 1)
        self.assertEqual(len(ring), 9)
        self.assertTrue(single <= ring)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.test_parent = (Path(__file__).resolve().parents[1] / '.test-tmp').resolve()
        self.test_parent.mkdir(exist_ok=True)
        self.root = workspace_temp(self.test_parent)
        self.sample = self.root / 'sample.fit'
        self.sample.write_bytes(fit_bytes(TRACK))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _wait_offline(self, base):
        job = {}
        for _ in range(600):
            _, job, _ = api_json(base + '/api/offline')
            if job.get('state') in ('completed', 'failed'):
                return job
            time.sleep(.02)
        return job

    def test_offline_download_stores_the_area_and_reports_progress(self):
        app = create_app(self.root / 'data')
        app.library.import_file(self.sample)
        with patch.object(TileCache, '_fetch', lambda cache, spec, z, x, y: PNG):
            with live_server(app) as base:
                status, job, _ = api_json(base + '/api/offline', 'POST', {'source': 'amap_road'})
                self.assertEqual(status, 202)
                self.assertIn(job['state'], ('planning', 'running'))
                job = self._wait_offline(base)
        self.assertEqual(job['state'], 'completed', job.get('error'))
        self.assertGreater(job['total'], 0)
        self.assertEqual(job['failed'], 0)
        self.assertGreater(job['bytes'], 0)
        self.assertEqual(job['downloaded'] + job['cached'], job['total'])
        stored = list((app.library.folder / 'tiles').rglob('*.png'))
        self.assertEqual(len(stored), job['downloaded'])
        # A second run finds everything already on disk and downloads nothing.
        with patch.object(TileCache, '_fetch', lambda cache, spec, z, x, y: PNG):
            with live_server(app) as base:
                api_json(base + '/api/offline', 'POST', {'source': 'amap_road'})
                again = self._wait_offline(base)
        self.assertEqual(again['downloaded'], 0)
        self.assertEqual(again['cached'], again['total'])

    def test_offline_status_is_idle_before_any_download(self):
        app = create_app(self.root / 'data')
        with live_server(app) as base:
            status, job, _ = api_json(base + '/api/offline')
        self.assertEqual(status, 200)
        self.assertEqual(job['state'], 'idle')
        self.assertEqual(job['cached_tiles'], 0)
        # The zoom range comes from config.json; only its sane shape is asserted here.
        self.assertEqual(job['zooms'], list(server.OFFLINE_ZOOMS))
        self.assertGreaterEqual(job['zooms'][0], 5)
        self.assertGreaterEqual(job['zooms'][1], 13)
        self.assertLessEqual(job['zooms'][1], 18)

    def test_prefetch_queues_tiles_and_honours_the_cooldown(self):
        app = create_app(self.root / 'data')
        payload = {'source': 'amap_road', 'zoom': 12, 'bbox': [116.2, 39.8, 116.6, 40.1]}
        queued = []
        with patch.object(TileCache, 'prefetch', lambda cache, source, tiles, delay=0: queued.append(len(tiles)) or len(tiles)):
            with live_server(app) as base:
                status, first, _ = api_json(base + '/api/tiles/prefetch', 'POST', payload)
                self.assertEqual(status, 202)
                self.assertGreater(first['queued'], 0)
                _, second, _ = api_json(base + '/api/tiles/prefetch', 'POST', payload)
                self.assertEqual(second['queued'], 0)  # inside the cooldown window
                self.assertEqual(api_json(base + '/api/tiles/prefetch', 'POST',
                                          {'source': 'nope', 'zoom': 12, 'bbox': payload['bbox']})[0], 400)
        self.assertEqual(len(queued), 1)


class VendoredTests(unittest.TestCase):
    def test_fit_decoder_comes_from_the_bundled_copy(self):
        import fitparse
        self.assertIn('vendor', Path(fitparse.__file__).parts)

    def test_no_third_party_web_framework_is_needed(self):
        source = (Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8')
        for name in ('flask', 'werkzeug', 'jinja2'):
            self.assertNotIn(f'import {name}', source.lower())
            self.assertNotIn(f'from {name}', source.lower())


def setUpModule():
    # Deterministic, fast tests: the parallel plumbing is covered by ParallelTests.
    server.CONFIG['workers'] = 1
    heatmap.configure_workers(1)


def tearDownModule():
    server.CONFIG['workers'] = 0
    heatmap.configure_workers(0)


if __name__ == '__main__':
    unittest.main()
