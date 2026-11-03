"""Synthetic headless map/media checks. Never connects to the user's browser or OSM.

Run from the project root:
  .venv/bin/python web/tests/browser_map_smoke.py
"""
from __future__ import annotations

import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect


WEB = Path(__file__).resolve().parents[1]
ARTIFACTS = WEB / '.local' / 'map-qa'
RECORD = dict(id='qa-post', title='A note from the same day', body='Synthetic browser-check evidence. This post has no recorded coordinates.', occurred_at='2020-03-14T15:00:00Z', source_id='qa-posts', source='QA journal', category='post', time_precision='second', media=[dict(file_id='qa-image', url='/api/files/qa-image/media', mime='image/svg+xml', kind='image', caption='Synthetic attached image')])
LOCATION = dict(id='qa-location', title='A recorded place', body='Synthetic explicit coordinate record.', occurred_at='2020-03-14T12:00:00Z', source_id='qa-locations', source='QA locations', category='location', time_precision='second')
POINT = dict(id='qa-point', event_id=LOCATION['id'], latitude=37.54, longitude=-77.44, occurred_at=LOCATION['occurred_at'], title=LOCATION['title'], source=LOCATION['source'], kind='place', time_precision='second')
IMAGE = b'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="270" viewBox="0 0 600 270"><rect fill="#dce8f6" width="600" height="270"/><circle cx="475" cy="67" r="26" fill="#e1bd78"/><path d="M0 215 140 105 265 220 370 135 600 250V270H0Z" fill="#9eb2cc"/><path d="M0 245 180 170 320 250 500 170 600 215V270H0Z" fill="#758fae"/></svg>'


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def end_headers(self):
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://tile.openstreetmap.org; font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'")
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        super().end_headers()


def run():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(WEB / 'dist')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    errors, api_requests, tile_requests = [], [], []

    def route_api(route):
        parsed = urlsplit(route.request.url)
        params = parse_qs(parsed.query)
        path = parsed.path
        api_requests.append(path + ('?' + parsed.query if parsed.query else ''))
        if path == '/api/overview':
            value = dict(total_events=2,sources_count=2,files_count=3,stored_bytes=3000,date_range=dict(start=LOCATION['occurred_at'],end=RECORD['occurred_at']),activity=[dict(month='2020-03',count=2)],categories=[dict(name='post',count=1),dict(name='location',count=1)])
        elif path == '/api/sources':
            value = dict(items=[dict(id='qa-locations',name='QA locations',status='complete',file_count=1,event_count=1,bytes=1000,notes=[]),dict(id='qa-posts',name='QA journal',status='complete',file_count=2,event_count=1,bytes=2000,notes=[])])
        elif path in ('/api/imports','/api/jobs'):
            value = dict(items=[])
        elif path == '/api/map':
            value = dict(items=[POINT] if params.get('category',[''])[0] != 'post' else [],total=1 if params.get('category',[''])[0] != 'post' else 0)
        elif path == '/api/events':
            records = [LOCATION,RECORD]
            if params.get('q',[''])[0]: records = [r for r in records if params['q'][0].lower() in (r['title'] + r['body']).lower()]
            value = dict(items=records,total=len(records),limit=40,offset=0)
        elif path.startswith('/api/events/'):
            record = RECORD if path.endswith(RECORD['id']) else LOCATION
            value = {**record,'provenance':[dict(file_id='qa-original',path='synthetic/archive.json',locator='record 1',sha256='a'*64)]}
        elif path == '/api/files/qa-image/media':
            return route.fulfill(status=200,content_type='image/svg+xml',body=IMAGE)
        else:
            return route.fulfill(status=404,content_type='application/json',body='{"detail":"Unsupported synthetic endpoint"}')
        route.fulfill(status=200,content_type='application/json',body=json.dumps(value))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport=dict(width=1440,height=1200),device_scale_factor=1,reduced_motion='reduce')
            context.route('**/api/**',route_api)
            # Tile policy forbids automated fetching. Fulfill locally, with no external connection.
            def tile(route):
                tile_requests.append(route.request.url)
                route.fulfill(status=200,content_type='image/svg+xml',body=b'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect fill="#edf2f7" width="256" height="256"/><path d="M0 80H256M0 180H256M60 0V256M180 0V256" stroke="#d7e2ef" stroke-width="2"/></svg>')
            context.route('https://tile.openstreetmap.org/**',tile)
            page = context.new_page()
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda message:errors.append(message.text) if message.type == 'error' else None)
            page.goto(origin,wait_until='networkidle')
            expect(page.get_by_role('heading',name='Places in your history')).to_be_visible()
            expect(page.get_by_role('region',name='Map of explicitly recorded locations. Use the recorded locations list to select a point.')).to_be_visible()
            expect(page.get_by_text('A note from the same day',exact=True)).to_be_visible()
            expect(page.get_by_text('This does not establish that a post was written',exact=False)).to_be_visible()
            expect(page.get_by_alt_text('Synthetic attached image')).to_be_visible()
            expect(page.locator('.same-day-event img')).to_have_js_property('naturalWidth',600)
            expect(page.locator('.leaflet-control-attribution')).to_contain_text('OpenStreetMap')
            assert tile_requests, 'Basemap did not request any locally fulfilled tiles'
            assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(245, 247, 251)'
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(ARTIFACTS/'map-desktop.png'),full_page=True)
            page.get_by_role('button',name='View location evidence').click()
            expect(page.get_by_role('dialog')).to_be_visible()
            expect(page.get_by_role('link',name='Open original file synthetic/archive.json')).to_be_visible()
            page.keyboard.press('Escape')
            page.get_by_role('checkbox',name='OpenStreetMap basemap').uncheck()
            expect(page.get_by_text('Basemap hidden.',exact=False)).to_be_visible()
            page.get_by_role('combobox',name='Filter by type').select_option('post')
            expect(page.get_by_role('heading',name='No explicit locations in this selection')).to_be_visible()
            page.get_by_role('button',name='Browse the timeline instead').click()
            expect(page.get_by_role('button',name='A recorded place',exact=False)).to_be_visible()
            expect(page.get_by_alt_text('Synthetic attached image')).to_be_visible()
            page.get_by_role('button',name='A note from the same day',exact=False).click()
            expect(page.get_by_role('dialog').get_by_alt_text('Synthetic attached image')).to_be_visible()
            page.keyboard.press('Escape')
            page.get_by_role('button',name='Clear',exact=True).click()
            page.get_by_role('button',name='Map & day context',exact=True).click()
            page.set_viewport_size(dict(width=390,height=844))
            expect(page.get_by_role('heading',name='Places in your history')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(ARTIFACTS/'map-mobile.png'),full_page=True)
            page.get_by_role('button',name='Open navigation').click()
            page.get_by_role('button',name='Source library',exact=True).click()
            expect(page.get_by_role('heading',name='Source library.')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            browser.close()
        assert not errors, errors
        report = dict(passed=True,checks=['map and explicit point rendering','same-day temporal context','local attached image decoding','evidence drawer','basemap toggle and attribution','empty map and timeline switch','inline and detail media','light palette','desktop and mobile overflow','mobile navigation','no console or page errors'], external_requests_sent=0, locally_fulfilled_tile_requests=len(tile_requests),api_requests=len(api_requests))
        (ARTIFACTS/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
    finally:
        server.shutdown()


if __name__ == '__main__':
    run()
