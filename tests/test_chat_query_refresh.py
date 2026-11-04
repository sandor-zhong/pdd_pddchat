import unittest
from playwright.sync_api import sync_playwright
from pdd_chat import start_result_observer, wait_for_query_result, _query_network_probe
from unittest.mock import MagicMock, patch
import io
from contextlib import redirect_stdout


class ChatQueryRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content('<button>查询</button><div class="search-order-sn-msg-table"><div class="new-message-list"><div class="message-item">old order</div></div></div>')
        self.addCleanup(self.page.close)
        start_result_observer(self.page)

    def test_old_records_do_not_count_as_new_result(self):
        self.page.get_by_role('button').click()
        with self.assertRaises(RuntimeError):
            wait_for_query_result(self.page, 'old order', timeout=200)

    def test_hover_only_never_counts_as_query_click(self):
        self.page.get_by_role('button').hover()
        self.assertFalse(self.page.evaluate('Boolean(window.__pddChatQueryClicked)'))

    def test_synthetic_click_is_recorded_but_not_accepted_as_physical_click(self):
        self.page.get_by_role('button').evaluate('el => el.click()')
        self.assertFalse(self.page.evaluate('Boolean(window.__pddChatQueryClicked)'))
        self.assertFalse(self.page.evaluate('window.__pddChatClickDetails.trusted'))

    def test_real_click_without_query_handler_can_still_be_true(self):
        self.page.get_by_role('button').click()
        self.assertTrue(self.page.evaluate('window.__pddChatQueryClicked'))
        with redirect_stdout(io.StringIO()) as log:
            with self.assertRaisesRegex(RuntimeError, '含本订单号请求=0'):
                wait_for_query_result(self.page, 'old order', timeout=200, network_records=[])
        self.assertIn('"trusted": true', log.getvalue())

    def test_matching_request_diagnostics_do_not_save_body_or_credentials(self):
        page = MagicMock()
        records, stop = _query_network_probe(page, '260926-196220096880481')
        handlers = {c.args[0]: c.args[1] for c in page.on.call_args_list}
        request = MagicMock(resource_type='xhr', url='https://example.com/query?token=SECRET', method='POST', post_data='{"order":"260926-196220096880481","password":"SECRET"}')
        handlers['request'](request)
        handlers['response'](MagicMock(request=request, status=429))
        self.assertEqual(records, [{'path':'/query','method':'POST','status':429,'failed':False}])
        self.assertNotIn('SECRET', repr(records))
        unrelated = MagicMock(resource_type='xhr', url='https://example.com/query', method='POST', post_data='{"order":"260926-196220096880482"}')
        handlers['request'](unrelated)
        self.assertEqual(len(records), 1)
        handlers['requestfailed'](request)
        self.assertTrue(records[0]['failed'])
        stop()
        self.assertEqual(page.remove_listener.call_count, 3)

    def test_style_and_scroll_do_not_count_as_result_refresh(self):
        self.page.get_by_role('button').click()
        self.page.locator('.message-item').evaluate("el => el.style.color = 'red'")
        with self.assertRaises(RuntimeError):
            wait_for_query_result(self.page, 'old order', timeout=200)

    def test_offscreen_button_waits_for_delayed_new_result(self):
        self.page.get_by_role('button').evaluate("el => { el.style.marginTop = '2000px'; el.onclick = () => setTimeout(() => document.querySelector('.message-item').textContent = 'new order', 200); }")
        self.page.get_by_role('button').scroll_into_view_if_needed()
        self.page.get_by_role('button').click()
        wait_for_query_result(self.page, 'old order', timeout=3000)
        self.assertEqual(self.page.locator('.message-item').inner_text(), 'new order')

    def test_identical_result_replaced_after_query_is_allowed(self):
        self.page.get_by_role('button').evaluate("el => el.onclick = () => document.querySelector('.new-message-list').innerHTML = '<div class=message-item>old order</div>'")
        self.page.get_by_role('button').click()
        wait_for_query_result(self.page, 'old order', timeout=3000)

    def test_empty_result_must_also_be_refreshed(self):
        self.page.get_by_role('button').evaluate("el => el.onclick = () => document.querySelector('.search-order-sn-msg-table').innerHTML = '<div class=table-blank-text>暂无聊天记录</div>'")
        self.page.get_by_role('button').click()
        wait_for_query_result(self.page, 'old order', timeout=3000)
        self.assertEqual(self.page.locator('.table-blank-text').inner_text(), '暂无聊天记录')
