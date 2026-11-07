"""Local HTML layout fixtures only; no system or virtual mouse events are sent."""

import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

import native_mouse
import pdd_chat
import stealth

CHROME = sorted(
    (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob(
        "chromium-*/chrome-win64/chrome.exe"
    )
)


@unittest.skipUnless(CHROME, "requires local Chromium for DOM layout fixtures")
class ChatCaptureLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(
            executable_path=str(CHROME[-1]), headless=True
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1024, "height": 641})
        self.page = self.context.new_page()
        self.addCleanup(self.context.close)

    def fixture(self, nested=False, standards=True):
        count = 15 if nested else 7
        messages = "".join(
            f'<div class="message-item"><span class="message-avatar">avatar</span><span class="message-name">sender</span><div class="message-body">fixture message {i}</div><span class="message-time">12:00</span></div>'
            for i in range(count)
        )
        style = "body{margin:0}.message-item{height:82px;width:700px;box-sizing:border-box}.new-message-list{width:1018px;height:574px;margin-left:244px}.before{height:389.09375px}"
        body = (
            f'<div class="before"></div><div class="new-message-list">{messages}</div>'
        )
        if nested:
            style = "body{margin:0}.panel{position:relative;margin:80px 40px;width:600px;height:300px;overflow-y:auto}.message-item{height:80px;width:570px;box-sizing:border-box}.new-message-list{width:580px;height:1200px}"
            body = f'<div class="panel"><div class="new-message-list">{messages}</div></div>'
        doctype = "<!doctype html>" if standards else ""
        self.page.set_content(f"{doctype}<style>{style}</style>{body}")
        return count

    def test_online_1024_layout_uses_page_scroll_and_visible_target(self):
        self.fixture()
        self.page.locator(".new-message-list").evaluate(pdd_chat._CHAT_CAPTURE_SETUP)
        data = pdd_chat._chat_capture_geometry(self.page)
        self.assertEqual(data["kind"], "page")
        self.assertAlmostEqual(data["box"]["y"], 389.09375)
        self.assertEqual(data["box"]["height"], 574)
        self.assertGreater(data["box"]["y"] + data["box"]["height"] / 2, 641)
        self.assertLess(data["target"]["y"], data["viewport"]["height"])
        self.assertLess(data["target"]["x"], data["viewport"]["width"])
        self.assertGreater(data["reveal"], 0)

    def test_nested_scroll_panel_is_selected_and_resize_is_remeasured(self):
        self.fixture(nested=True)
        self.page.locator(".new-message-list").evaluate(pdd_chat._CHAT_CAPTURE_SETUP)
        small = pdd_chat._chat_capture_geometry(self.page)
        self.assertEqual(small["kind"], "element")
        self.assertEqual(small["state"]["max"], 900)
        self.page.set_viewport_size({"width": 1920, "height": 953})
        large = pdd_chat._chat_capture_geometry(self.page)
        self.assertGreater(large["viewport"]["width"], small["viewport"]["width"])
        self.assertLess(large["target"]["y"], large["viewport"]["height"])

    def capture_fixture(self, nested, *, setup=None, quantized=False, resize=False):
        count = setup() if setup else self.fixture(nested)
        clips = []
        moves = []

        # Fixture scrolling stands in for native wheel delivery. Production never
        # writes scrollTop; no Playwright mouse APIs are exercised by this test.
        def wheel(page, delta, *_):
            if quantized:
                # Approximate browser response to integer Windows wheel units,
                # including zero response to tiny divided deltas.
                delta = int(delta * 0.8)
            page.evaluate(
                """delta => {
                    const point=window.__fixtureCursor,root=document.scrollingElement;
                    let node=document.elementFromPoint(point.x,point.y);
                    while(node && node!==root && node!==document.body) {
                        if(/^(auto|scroll|overlay)$/.test(getComputedStyle(node).overflowY) && node.scrollHeight>node.clientHeight+2) break;
                        node=node.parentElement;
                    }
                    (node && node!==document.body ? node : root).scrollTop+=delta;
                }""",
                delta,
            )

        def move(x, y):
            info = pdd_chat._chat_capture_geometry(self.page)
            self.assertGreaterEqual(x, 0)
            self.assertLess(x, info["viewport"]["width"])
            self.assertGreaterEqual(y, 0)
            self.assertLess(y, info["viewport"]["height"])
            moves.append((x, y))
            self.page.evaluate("p=>window.__fixtureCursor=p", {"x": x, "y": y})

        def screenshot(**kwargs):
            info = pdd_chat._chat_capture_geometry(self.page)
            clip = kwargs["clip"]
            self.assertGreater(clip["width"], 0)
            self.assertGreater(clip["height"], 0)
            self.assertLessEqual(clip["x"] + clip["width"], info["viewport"]["width"])
            self.assertLessEqual(clip["y"] + clip["height"], info["viewport"]["height"])
            clips.append(clip)
            if resize and len(clips) == 1:
                self.page.set_viewport_size({"width": 1024, "height": 641})

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(pdd_chat, "EVIDENCE_DIR", Path(temporary)),
            patch.object(pdd_chat, "human_scroll", side_effect=wheel),
            patch.object(pdd_chat, "human_delay"),
            patch.object(pdd_chat, "wait_for_chat_images"),
            patch.object(pdd_chat, "pointer_diagnostic"),
            patch.object(self.page.mouse, "move", side_effect=move),
            patch.object(self.page, "screenshot", side_effect=screenshot),
        ):
            captures = pdd_chat.save_page_evidence_screenshot(
                self.page, "fixture-order", 1, "fixture-run"
            )
        covered = {index for capture in captures for index in capture["full_indices"]}
        self.assertEqual(covered, set(range(1, count + 1)))
        self.assertEqual(len(captures), len(clips))
        self.assertTrue(moves)
        self.assertIsNone(self.page.evaluate("window.__pddChatCapture"))
        return captures

    def test_page_scroll_captures_all_messages_without_offscreen_coordinates(self):
        self.capture_fixture(False)

    def test_nested_scroll_captures_all_messages_across_multiple_screens(self):
        self.assertGreater(len(self.capture_fixture(True)), 1)

    def test_quirks_document_still_detects_page_scroll(self):
        self.fixture(standards=False)
        self.page.locator(".new-message-list").evaluate(pdd_chat._CHAT_CAPTURE_SETUP)
        data = pdd_chat._chat_capture_geometry(self.page)
        self.assertGreater(data["state"]["max"], 0)
        self.assertGreater(data["reveal"], 0)

    def test_stalled_page_scroll_fails_instead_of_claiming_complete_capture(self):
        self.fixture()
        with (
            patch.object(pdd_chat, "human_scroll"),
            patch.object(pdd_chat, "human_delay"),
            patch.object(pdd_chat, "pointer_diagnostic"),
            patch.object(self.page.mouse, "move"),
            self.assertRaisesRegex(RuntimeError, "连续三次"),
        ):
            pdd_chat._prepare_chat_capture(
                self.page, self.page.locator(".new-message-list"), "fixture", 1
            )

    def test_short_chat_already_visible_does_not_force_page_alignment(self):
        self.page.set_viewport_size({"width": 1920, "height": 953})
        self.fixture()
        self.page.add_style_tag(content=".before{height:345px}")
        with (
            patch.object(pdd_chat, "human_scroll") as scroll,
            patch.object(self.page.mouse, "move"),
            patch.object(pdd_chat, "pointer_diagnostic"),
        ):
            pdd_chat._prepare_chat_capture(
                self.page, self.page.locator(".new-message-list"), "fixture", 1
            )
        scroll.assert_not_called()

    def test_quantized_wheel_still_captures_page_and_internal_end(self):
        self.capture_fixture(False, quantized=True)
        self.capture_fixture(True, quantized=True)

    def test_resize_from_1920_to_1024_during_capture(self):
        self.page.set_viewport_size({"width": 1920, "height": 953})
        self.capture_fixture(True, resize=True, quantized=True)

    def test_tall_panel_clipped_by_page_does_not_require_exact_reveal(self):
        def setup():
            count = self.fixture(True)
            self.page.add_style_tag(
                content=".panel{margin-top:345px;height:679px}body{min-height:1149px}"
            )
            return count

        self.capture_fixture(True, setup=setup, quantized=True)

    def test_native_reveal_scrolls_offscreen_button_using_actual_wheel_owner(self):
        # Only the local test DOM is scrolled. No browser/native mouse event is sent.
        self.page.set_content(
            "<!doctype html><style>body{margin:0;width:1900px;height:1600px}button{position:absolute;left:1700px;top:1200px}</style><button>local target</button>"
        )
        locator = self.page.locator("button")
        driver = MagicMock()

        def wheel(page, dx, dy):
            page.evaluate(
                "p=>{document.scrollingElement.scrollLeft+=p.x;document.scrollingElement.scrollTop+=p.y}",
                {"x": dx, "y": dy},
            )

        driver.wheel.side_effect = wheel
        driver.locked.side_effect = lambda: nullcontext()
        with (
            patch.object(native_mouse, "_driver", driver),
            patch.object(native_mouse.time, "sleep"),
        ):
            native_mouse._scroll_locator_into_view(locator, 8000)
        driver.wheel.assert_called()
        self.assertTrue(locator.evaluate(native_mouse._NATIVE_REVEAL_TARGET)["ready"])

    def test_native_human_click_never_uses_cached_curve_coordinates(self):
        page = MagicMock()
        page.context._system_mouse_enabled = True
        page._pdd_mouse_position = (1705, 603)
        locator = MagicMock()
        with patch.object(stealth, "business_pause"):
            stealth.human_click(page, locator)
        locator.click.assert_called_once_with(timeout=8000)
        locator.bounding_box.assert_not_called()
        page.mouse.move.assert_not_called()


if __name__ == "__main__":
    unittest.main()
