"""Keep the standalone API docs usable on touch devices, without CDN access."""
from __future__ import annotations

import re
import unittest

from awbotnest.api.docs import _docs_html


class DocsMobileTests(unittest.TestCase):
    def setUp(self):
        self.page = _docs_html("test-version")
        self.styles = re.search(r"<style>(.*?)</style>", self.page, re.S).group(1)
        media = "@media (max-width:760px), (hover:none) and (pointer:coarse)"
        start = self.styles.index("{", self.styles.index(media))
        depth = 1
        end = start + 1
        while depth:
            depth += (self.styles[end] == "{") - (self.styles[end] == "}")
            end += 1
        self.mobile_styles = self.styles[start + 1:end - 1]
        self.desktop_styles = self.styles[:self.styles.index(media)]

    def test_text_controls_include_password_and_mobile_landscape(self):
        # A non-text input is excluded, rather than accidentally excluding the
        # password/API Key inputs rendered by Swagger's authorization dialog.
        selector = self.mobile_styles.split("{", 1)[0]
        self.assertIn(".swagger-ui input:not([type=checkbox])", selector)
        self.assertNotIn("[type=password]", selector)
        self.assertIn(".swagger-ui textarea", selector)
        self.assertIn(".swagger-ui select", selector)
        self.assertIn("[contenteditable]:not([contenteditable=false])", selector)
        self.assertRegex(self.mobile_styles, r"font-size:\s*16px")
        self.assertNotIn("font-size:16px", self.desktop_styles)

    def test_authorization_dialog_respects_safe_area_and_scrolls(self):
        for edge in ("top", "right", "bottom", "left"):
            self.assertIn(f"env(safe-area-inset-{edge})", self.mobile_styles)
        self.assertIn("height:var(--docs-visual-height, 100dvh)", self.mobile_styles)
        self.assertIn("top:var(--docs-visual-top, 0px)", self.mobile_styles)
        self.assertIn("transform:none", self.mobile_styles)
        self.assertIn("min-width:0; max-height:100%", self.mobile_styles)
        self.assertIn(".modal-ux-header { flex-shrink:0; }", self.mobile_styles)
        self.assertIn("min-width:44px; min-height:44px", self.mobile_styles)
        self.assertIn("min-height:0; max-height:none; overflow-y:auto", self.mobile_styles)

    def test_visible_viewport_tracks_keyboard_and_does_not_disable_zoom(self):
        self.assertIn("const docsViewport = window.visualViewport", self.page)
        self.assertIn("docsViewport?.height || window.innerHeight", self.page)
        self.assertIn("docsViewport?.offsetTop || 0", self.page)
        self.assertIn("addEventListener('resize', syncDocsViewport", self.page)
        self.assertIn("addEventListener('scroll', syncDocsViewport", self.page)
        viewport = re.search(r'<meta name="viewport" content="([^"]+)"', self.page).group(1)
        self.assertIn("viewport-fit=cover", viewport)
        self.assertNotIn("maximum-scale", viewport)
        self.assertNotIn("user-scalable", viewport)


if __name__ == "__main__":
    unittest.main()
