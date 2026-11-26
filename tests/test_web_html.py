import unittest

import trackplayer_web as web

REQUIRED_IDS = (
    'adv-bd', 'adv-toggle', 'app', 'callout-root', 'chip', 'clear', 'coord', 'dclear',
    'dev-name', 'dev-sk', 'dev-udid', 'devhint', 'device', 'device-card', 'draw',
    'draw-badge', 'drawhint', 'duration', 'duse', 'empty-draw', 'empty-upload', 'file',
    'file-block', 'follow', 'font-btn', 'gcj', 'log', 'log-bd', 'log-clear', 'log-copy',
    'log-scroll', 'log-toggle', 'map', 'map-dim', 'map-empty', 'map-loading',
    'map-toolbar', 'map-wrap', 'menu-btn', 'page', 'page-sub', 'page-title', 'pause',
    'play', 'preset', 'progress', 'progress-bar', 'progress-label', 'refresh', 'repeat',
    'scale', 'scale-unit', 'scrim', 'sec-log', 'sec-look', 'sec-map', 'sec-play',
    'seg-scale', 'seg-speed', 'side', 'speed', 'speed-unit', 'sr-status', 'status',
    'stop', 'summary', 'theme-btn', 'toast-root', 'udid-copy', 'undo', 'upload-meta',
    'upload-name',
)
HTML_BUDGET = 80 * 1024


class WebRedLineTest(unittest.TestCase):
    def test_library_panel_and_navigation_are_present(self):
        for name in ('sec-lib', 'lib-refresh', 'lib-save', 'lib-update', 'lib-list'):
            self.assertIn(f'id="{name}"', web.INDEX_HTML)
        self.assertIn('data-spy="sec-lib"', web.INDEX_HTML)

    def test_all_existing_ids_preserved(self):
        for i in REQUIRED_IDS:
            self.assertIn(f'id="{i}"', web.INDEX_HTML, f'lost id {i}')

    def test_html_size_budget(self):
        self.assertLessEqual(len(web.INDEX_HTML.encode('utf-8')), HTML_BUDGET)

    def test_map_stacking_context_fix_present(self):
        self.assertIn('#map{position:relative;z-index:0', web.INDEX_HTML)

    def test_number_spinners_hidden(self):
        self.assertIn('::-webkit-inner-spin-button', web.INDEX_HTML)
        self.assertIn('-moz-appearance:textfield', web.INDEX_HTML)

    def test_amap_subdomains_not_regressed(self):
        self.assertIn("subdomains:'1234'", web.INDEX_HTML)

    def test_pos_note_stays_removed(self):
        self.assertNotIn('pos-note', web.INDEX_HTML)

    def test_windows_port_reuse_refused(self):
        self.assertFalse(web._HTTPServer.allow_reuse_address)

    def test_script_block_ids_present(self):
        for i in ('sec-script', 'script-toggle', 'script-bd', 'script-on', 'script-body',
                  'leg-move', 'leg-stay', 'leg-pick', 'script-total', 'legs', 'script-hint'):
            self.assertIn(f'id="{i}"', web.INDEX_HTML)

    def test_script_nav_link_present(self):
        self.assertIn('data-spy="sec-script"', web.INDEX_HTML)

    def test_pick_mode_wired(self):
        self.assertIn('function setPicking(', web.INDEX_HTML)
        self.assertIn('/api/script/project', web.INDEX_HTML)
        self.assertIn('if (picking) pickLeg(', web.INDEX_HTML)


if __name__ == '__main__':
    unittest.main()
