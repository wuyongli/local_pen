"""Optional offline acceptance; always stages a copy of LOCAL_PEN_NATIVE_FIXTURE."""

import json
import os
import sys
import tempfile
import struct
import zlib
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edit_pen_text import load_pen, write_pen_copy
from pen_tiles import NativeSession


@unittest.skipUnless(os.environ.get('LOCAL_PEN_NATIVE_FIXTURE'), 'set LOCAL_PEN_NATIVE_FIXTURE to run native acceptance')
class NativeAcceptance(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='local-pen-native-acceptance-')
        self.addCleanup(self.folder.cleanup)
        original=Path(os.environ['LOCAL_PEN_NATIVE_FIXTURE']).resolve()
        self.source=Path(self.folder.name)/'copy.pen'
        write_pen_copy(load_pen(original), self.source, original)

    def session(self, **kwargs):
        s=NativeSession(self.source, **kwargs)
        self.addCleanup(s.close)
        return s

    def test_reuse_visual_identity_reload_and_request_recycle(self):
        s=self.session(max_requests=4)
        first=s.describe('one')
        b=first['layout']['bounds']; region=[b['x'],b['y'],200,200]
        _, a=s.render('one',region,2)
        _, same=s.render('one',region,2)
        self.assertEqual(a,same)
        self.assertEqual(first['pid'],s.process.pid)
        document=load_pen(self.source)
        document['children'].append({'id':'native_acceptance_overlay','type':'rectangle','x':b['x']+10,
                                     'y':b['y']+10,'width':80,'height':80,'fill':'#FF0022'})
        self.source.write_text(json.dumps(document))
        loaded=s.describe('two')
        self.assertEqual(loaded['pid'],first['pid'])
        _, updated=s.render('two',region,2)
        self.assertNotEqual(a,updated)
        restarted=s.describe('two')
        self.assertNotEqual(restarted['pid'],first['pid'])
        _, cold=s.render('two',region,2)
        self.assertEqual(cold,updated)

    def test_local_image_same_path_refresh_matches_fresh_renderer(self):
        image=self.source.parent/'asset.png'
        def png(rgb):
            def chunk(kind,data):
                return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
            data=(b'\x00'+bytes(rgb)*16)*16
            return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(data))+chunk(b'IEND',b'')
        image.write_bytes(png([255,0,0]))
        document=load_pen(self.source)
        document['children']=[{'id':'image_board','type':'frame','width':200,'height':200,
          'fill':{'type':'image','url':str(image),'mode':'stretch'}}]
        self.source.write_text(json.dumps(document))
        s=self.session();s.describe('one')
        _,red=s.render('one',[0,0,200,200],2)
        image.write_bytes(png([0,0,255]))
        s.describe('two')
        _,blue=s.render('two',[0,0,200,200],2)
        fresh=self.session();fresh.describe('two')
        _,expected=fresh.render('two',[0,0,200,200],2)
        self.assertNotEqual(red,expected,'Image fixture did not render differently')
        self.assertEqual(blue,expected,'Reused native renderer kept stale image bytes')

    def test_native_idle_reclaims_and_next_render_recovers(self):
        s=self.session(idle_seconds=.2)
        s.describe('one');process=s.process
        process.wait(timeout=4)
        next_result=s.describe('one')
        self.assertNotEqual(next_result['pid'],process.pid)

    def test_watchdog_reclaims_native_memory_and_next_render_recovers(self):
        s=self.session(memory_mb=4096)
        first=s.describe('one');process=s.process
        s.memory_mb=max(1,first['rssMB']//2)
        process.wait(timeout=4)
        s.memory_mb=4096
        second=s.describe('one')
        self.assertNotEqual(first['pid'],second['pid'])


if __name__=='__main__':unittest.main()
