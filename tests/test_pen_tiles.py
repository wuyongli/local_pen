import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAKE = r'''
import json, os, sys, time, struct
from pathlib import Path
folder=Path(sys.argv[1]); initial=Path(sys.argv[2]); state=folder/'control'
def result(load=False):
 control=json.loads(state.read_text()) if state.exists() else {}
 if control.get('crash'): sys.exit(2)
 waste=bytearray(control.get('allocate',0)*1048576)
 if control.get('sleep'): time.sleep(control['sleep'])
 return {'layout':{'bounds':{'x':-10,'y':20,'width':2048,'height':30000},'artboards':[{'id':'a','name':'页面','x':0,'y':0}]},'rssMB':control.get('rss',32),'pid':os.getpid()}
def send(x): print('LOCAL_PEN '+json.dumps(x),flush=True)
send({'ready':True,**result()})
for line in sys.stdin:
 q=json.loads(line); answer=result()
 if q['action']=='render':
  w=round(q['region'][2]*q['scale']);h=round(q['region'][3]*q['scale'])
  Path(q['output']).write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'+struct.pack('>II',w,h)+Path(initial).read_bytes()+b'\x00\x00\x00\x00IEND\xaeB`\x82')
  answer.update(width=1088,height=1088)
 else: initial=Path(q['source'])
 send(answer)
'''


class SessionFixture(unittest.TestCase):
    def setUp(self):
        from pen_tiles import NativeSession
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root/'source.pen'
        self.source.write_text(json.dumps({'version':'2.17','children':[]}))
        script = self.root/'fake.py'
        script.write_text(FAKE)
        self.command = lambda staged, folder: [sys.executable, '-u', str(script), str(self.root), str(staged)]
        self.cls = NativeSession

    def session(self, **kwargs):
        session = self.cls(self.source, command_factory=self.command, **{'slot_directory':self.root/'slots', **kwargs})
        self.addCleanup(session.close)
        return session

    def control(self, **values):
        (self.root/'control').write_text(json.dumps(values))


class NativeSessionTest(SessionFixture):
    def test_reuses_pid_and_reloads_updated_copy(self):
        s = self.session()
        first = s.describe('one')
        _, before = s.render('one', [0,0,1024,1024], 1)
        self.source.write_text(json.dumps({'version':'2.17','children':[], 'name':'changed'}))
        second = s.describe('two')
        _, after = s.render('two', [0,0,1024,1024], 1)
        self.assertEqual(first['pid'], second['pid'])
        self.assertNotEqual(before, after)

    def test_request_budget_restarts_and_keeps_output(self):
        s = self.session(max_requests=2)
        pid = s.describe('one')['pid']
        _, a = s.render('one', [0,0,1024,1024], 1)
        _, b = s.render('one', [0,0,1024,1024], 1)
        self.assertEqual(a,b)
        self.assertNotEqual(pid, s.describe('one')['pid'])

    def test_memory_budget_recycles_after_request(self):
        s = self.session(memory_mb=64)
        pid = s.describe('one')['pid']
        self.control(rss=80)
        s.render('one', [0,0,1024,1024], 1)
        self.control()
        self.assertNotEqual(pid,s.describe('one')['pid'])

    def test_idle_worker_is_reclaimed_without_another_request(self):
        s = self.session(idle_seconds=.1)
        s.describe('one')
        process = s.process
        process.wait(timeout=3)
        self.assertIsNotNone(process.returncode)
        self.assertNotEqual(process.pid,s.describe('one')['pid'])

    def test_timeout_kills_owned_process_and_allows_retry(self):
        s = self.session(timeout=.2)
        s.describe('one')
        process = s.process
        self.control(sleep=2)
        with self.assertRaisesRegex(ValueError,'timed out'):
            s.render('one', [0,0,1024,1024], 1)
        self.assertIsNotNone(process.poll())
        self.control()
        self.assertNotEqual(process.pid,s.describe('one')['pid'])

    def test_cancelled_request_terminates_worker(self):
        s = self.session()
        s.describe('one')
        process = s.process
        event = Event(); event.set()
        with self.assertRaises(ValueError):
            s.render('one', [0,0,1024,1024], 1, request=event)
        self.assertIsNotNone(process.poll())

    def test_watchdog_terminates_memory_growth_during_render(self):
        s=self.session(memory_mb=64, timeout=5)
        s.describe('one'); process=s.process
        self.control(allocate=128, sleep=4)
        started=time.monotonic()
        with self.assertRaises(ValueError): s.render('one',[0,0,1024,1024],1)
        self.assertLess(time.monotonic()-started,3)
        self.assertIsNotNone(process.poll())

    def test_crash_is_recoverable(self):
        s=self.session(); s.describe('one'); pid=s.process.pid
        self.control(crash=True)
        with self.assertRaises(ValueError): s.render('one',[0,0,1024,1024],1)
        self.control()
        self.assertNotEqual(pid,s.describe('one')['pid'])

    def test_host_slots_limit_concurrent_processes(self):
        from pen_tiles import RendererBusy
        a=self.session(slot_directory=self.root/'slots', max_processes=1)
        b=self.session(slot_directory=self.root/'slots', max_processes=1)
        a.describe('one')
        with self.assertRaises(RendererBusy): b.describe('one')
        a.close()
        self.assertTrue(b.describe('one')['ready'])


class ViewportTest(SessionFixture):
    def viewport(self, **kwargs):
        from pen_tiles import ViewportRenderer
        v=ViewportRenderer(self.source, session=self.session(), **kwargs)
        self.addCleanup(v.close)
        return v

    def test_manifest_cache_and_stale_tile_rejection(self):
        from pen_tiles import StaleRevision
        v=self.viewport(); m=v.manifest()
        first=v.tile(m['etag'],0,0,0)
        self.control(crash=True)
        self.assertEqual(first,v.tile(m['etag'],0,0,0))
        self.source.write_text(json.dumps({'version':'2.17','children':[], 'name':'changed'}))
        with self.assertRaises(StaleRevision): v.tile(m['etag'],0,0,0)
        self.control()
        self.assertNotEqual(m['etag'],v.manifest()['etag'])

    def test_superseded_render_is_discarded_without_throwing_away_process(self):
        from pen_tiles import StaleRevision
        v=self.viewport(); m=v.manifest(); pid=v.session.process.pid
        self.control(sleep=.2)
        def change():
            time.sleep(.05)
            self.source.write_text('{"version":"2.17","children":[],"name":"new"}')
        thread=Thread(target=change);thread.start()
        with self.assertRaises(StaleRevision): v.tile(m['etag'],0,0,0)
        thread.join();self.control()
        v.manifest()
        self.assertEqual(pid,v.session.process.pid)

    def test_invalid_and_outside_tiles_are_rejected_before_native_render(self):
        v=self.viewport(); m=v.manifest()
        for args in [(20,0,0),(True,0,0),(0,-1,0),(0,2,0),(0,0,30)]:
            with self.subTest(args=args), self.assertRaises(ValueError): v.tile(m['etag'],*args)

    def test_cache_has_byte_budget_and_asset_change_invalidates(self):
        v=self.viewport(cache_bytes=1)
        m=v.manifest();v.tile(m['etag'],0,0,0)
        self.assertLessEqual(v.cache_size,1)
        image=self.root/'image.png'; image.write_bytes(b'one')
        self.source.write_text(json.dumps({'version':'2.17','children':[{'id':'i','type':'rectangle','fill':{'type':'image','url':'image.png'}}]}))
        m=v.manifest();image.write_bytes(b'different')
        from pen_tiles import StaleRevision
        with self.assertRaises(StaleRevision):v.tile(m['etag'],0,0,0)

    def test_cli_guard_rejects_other_versions_and_unknown_source_shape(self):
        from pen_tiles import validate_cli
        folder=self.root/'cli';folder.mkdir(); entry=folder/'index.mjs'
        package=folder/'package.json';package.write_text('{"name":"@pen.dev/cli","version":"0.3.17"}')
        entry.write_text('not-supported')
        with self.assertRaisesRegex(ValueError,'0.3.16'):validate_cli(entry)
        package.write_text('{"name":"@pen.dev/cli","version":"0.3.16"}')
        with self.assertRaisesRegex(ValueError,'structure'):validate_cli(entry)


class TileHTTPTest(SessionFixture):
    def window(self):
        from pen_live_preview import PreviewWindow
        from pen_tiles import ViewportRenderer
        window=PreviewWindow(self.source.resolve(),1)
        window.tiles.session.close()
        window.tiles=ViewportRenderer(self.source, session=self.session(), cancelled=window.cancelled)
        self.addCleanup(window.close)
        return window

    def test_manifest_tile_routes_and_stale_revision(self):
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        window=self.window();url=window.status()['url']
        with urlopen(url+'manifest.json') as response:
            manifest=json.load(response);etag=response.headers['ETag']
        self.assertEqual(manifest['bounds']['height'],30000)
        self.assertEqual(manifest['etag'],etag)
        tile=Request(url+'tile.png?level=0&col=0&row=0',headers={'If-Match':etag})
        with urlopen(tile) as response:self.assertEqual(response.status,200)
        self.source.write_text('{"version":"2.17","children":[],"name":"changed"}')
        with self.assertRaises(HTTPError) as stale:urlopen(tile)
        self.assertEqual(stale.exception.code,412)
        for query in ['level=99&col=0&row=0','level=0&col=-1&row=0','level=0&col=0&row=0&col=1']:
            with self.assertRaises(HTTPError) as error:urlopen(Request(url+'tile.png?'+query,headers={'If-Match':etag}))
            self.assertEqual(error.exception.code,400)

    def test_compatibility_failure_is_distinct_from_temporary_failure(self):
        from urllib.request import urlopen
        from urllib.error import HTTPError
        from unittest.mock import patch
        from pen_tiles import NativeUnsupported
        window=self.window();url=window.status()['url']
        with patch.object(window.tiles,'manifest',side_effect=NativeUnsupported('unsupported')):
            with self.assertRaises(HTTPError) as error:urlopen(url+'manifest.json')
            self.assertEqual(error.exception.code,501)
        with patch.object(window.tiles,'manifest',side_effect=ValueError('temporary')):
            with self.assertRaises(HTTPError) as error:urlopen(url+'manifest.json')
            self.assertEqual(error.exception.code,503)

    def test_script_route_and_not_modified_manifest(self):
        from urllib.request import urlopen,Request
        from urllib.error import HTTPError
        window=self.window();url=window.status()['url']
        with urlopen(url) as response:self.assertIn(b'preview.js',response.read())
        with urlopen(url+'preview.js') as response:
            self.assertIn(b'manifest.json',response.read())
            self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
        with urlopen(url+'manifest.json') as response:tag=response.headers['ETag']
        with self.assertRaises(HTTPError) as unchanged:urlopen(Request(url+'manifest.json',headers={'If-None-Match':tag}))
        self.assertEqual(unchanged.exception.code,304)


if __name__=='__main__': unittest.main()
