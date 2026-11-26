import json
import shutil
import tempfile
import unittest
from pathlib import Path

import trackplayer_store as store


class LastPointTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='tp_store_')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_file_returns_none(self):
        self.assertIsNone(store.load_last_point(self.root))

    def test_roundtrip(self):
        store.save_last_point(39.99, 116.30, root=self.root)
        self.assertEqual(store.load_last_point(self.root), [39.99, 116.30])
        obj = json.loads((Path(self.root) / 'state.json').read_text(encoding='utf-8'))
        self.assertIn('saved_at', obj)

    def test_corrupt_file_returns_none(self):
        (Path(self.root) / 'state.json').write_text('{not json', encoding='utf-8')
        self.assertIsNone(store.load_last_point(self.root))

    def test_wrong_shape_returns_none(self):
        for bad in ([1.0], [1.0, 2.0, 3.0], ['a', 'b'], [91.0, 0.0], [0.0, 181.0], None):
            (Path(self.root) / 'state.json').write_text(json.dumps({'last_point': bad}), encoding='utf-8')
            self.assertIsNone(store.load_last_point(self.root), bad)

    def test_forget_removes_file(self):
        store.save_last_point(39.99, 116.30, root=self.root)
        store.forget_last_point(self.root)
        self.assertIsNone(store.load_last_point(self.root))
        store.forget_last_point(self.root)  # idempotent

    def test_write_is_atomic_no_tmp_left(self):
        store.save_last_point(39.99, 116.30, root=self.root)
        self.assertEqual([p.name for p in Path(self.root).iterdir()], ['state.json'])


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='tp_store_')
        self.src = Path(self.root) / 'in.gpx'
        self.src.write_text('<gpx/>', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_empty_library(self):
        self.assertEqual(store.list_tracks(self.root), [])

    def test_save_list_load_roundtrip(self):
        meta = store.save_track(self.src, 'loop', from_gcj02=True,
                                script=[{'type': 'stay', 'seconds': 5}], km=1.2, n=4, root=self.root)
        listed = store.list_tracks(self.root)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['id'], meta['id'])
        self.assertTrue(listed[0]['has_script'])
        self.assertTrue(listed[0]['from_gcj02'])
        got, path = store.load_track(meta['id'], self.root)
        self.assertEqual(got['name'], 'loop')
        self.assertEqual(path.read_text(encoding='utf-8'), '<gpx/>')

    def test_update_script_and_clear(self):
        meta = store.save_track(self.src, 'loop', script=[{'type': 'stay', 'seconds': 5}], root=self.root)
        store.update_track(meta['id'], script=[{'type': 'move', 'to_m': 10, 'speed': 3}], root=self.root)
        store.update_track(meta['id'], name='renamed', root=self.root)
        got, _ = store.load_track(meta['id'], self.root)
        self.assertEqual(got['script'][0]['type'], 'move')
        self.assertEqual(got['name'], 'renamed')
        store.update_track(meta['id'], script=None, root=self.root)
        got, _ = store.load_track(meta['id'], self.root)
        self.assertIsNone(got['script'])
        self.assertFalse(store.list_tracks(self.root)[0]['has_script'])

    def test_delete(self):
        meta = store.save_track(self.src, 'loop', root=self.root)
        store.delete_track(meta['id'], self.root)
        self.assertEqual(store.list_tracks(self.root), [])
        with self.assertRaises(ValueError):
            store.delete_track(meta['id'], self.root)

    def test_rejects_path_traversal_ids(self):
        for bad in ('../x', 'a/b', 'a\\b', '..', '', None, 'x' * 65):
            with self.assertRaises(ValueError):
                store.load_track(bad, self.root)

    def test_rejects_unsupported_suffix_and_empty_file(self):
        bad = Path(self.root) / 'in.txt'
        bad.write_text('x', encoding='utf-8')
        with self.assertRaises(ValueError):
            store.save_track(bad, 'x', root=self.root)
        empty = Path(self.root) / 'e.gpx'
        empty.write_text('', encoding='utf-8')
        with self.assertRaises(ValueError):
            store.save_track(empty, 'x', root=self.root)

    def test_list_skips_corrupt_entries(self):
        d = Path(self.root) / 'tracks' / 'deadbeefcafe'
        d.mkdir(parents=True)
        (d / 'meta.json').write_text('{oops', encoding='utf-8')
        meta = store.save_track(self.src, 'loop', root=self.root)
        _, path = store.load_track(meta['id'], self.root)
        (path.parent / 'meta.json').write_text(json.dumps(dict(meta, created=42)), encoding='utf-8')
        self.assertEqual(store.list_tracks(self.root), [])


if __name__ == '__main__':
    unittest.main()
