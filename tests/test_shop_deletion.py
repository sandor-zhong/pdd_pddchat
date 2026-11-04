import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from gui.app.services import account_store, shop_repo
from gui.app.pages.shops_page import ShopsPage


class ShopDeletionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.profiles = self.root / 'state/profiles'
        self.profiles.mkdir(parents=True)
        self.stack.enter_context(patch.object(shop_repo, 'PROJECT_PATHS', SimpleNamespace(root=self.root)))
        for name in ('DELETED_SHOPS_PATH', 'DISABLED_SHOPS_PATH', 'IMPORTED_SHOPS_PATH'):
            self.stack.enter_context(patch.object(shop_repo, name, self.root / (name + '.json')))
        self.stack.enter_context(patch.object(account_store, 'ACCOUNT_DB_PATH', self.root / 'accounts.db'))
        self.stack.enter_context(patch.object(shop_repo.scheduler_core, 'PROFILES_DIR', self.profiles))
        row = dict(shop='店A', pending_remaining=2, submitted_today=1, failed_today=0, skipped_today=0, touched_today=3)
        self.summary = self.stack.enter_context(patch.object(shop_repo.db, 'daily_shop_summary', return_value=[row]))
        self.stack.enter_context(patch.object(shop_repo.db, 'last_activity_by_shop', return_value={}))
        self.stack.enter_context(patch.object(shop_repo.db, 'list_shop_daily_status', return_value=[dict(shop='店A', status='done')]))
        self.stack.enter_context(patch.object(shop_repo.db, 'list_shop_max_orders', return_value={}))
        self.stack.enter_context(patch.object(shop_repo.db, 'list_shop_groups', return_value={}))
        (self.profiles / '店A').mkdir()
        (self.profiles / '店A' / 'cookie').write_text('fake-session')

    def test_delete_removes_profile_and_account_without_reappearing_from_history(self):
        shop_repo.register_imported_shops(['店A'])
        shop_repo.set_shop_disabled('店A', True)
        with account_store._connect() as connection:
            connection.execute('INSERT INTO shop_accounts VALUES (?,?,?,?,?)', ('dummy', b'not-a-password', '店A', '', 'now'))
        self.assertEqual(len(shop_repo.list_shop_summaries()), 1)
        shop_repo.delete_shop_profile('店A')
        self.assertFalse((self.profiles / '店A').exists())
        self.assertNotIn('店A', shop_repo._load_imported_shops())
        self.assertNotIn('店A', shop_repo._load_disabled_shops())
        with account_store._connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM shop_accounts').fetchone()[0], 0)
        self.assertEqual(shop_repo.list_shop_summaries(), [])
        self.assertEqual(shop_repo.list_shop_summaries(), [])
        self.assertEqual(self.summary.return_value[0]['submitted_today'], 1)
        shop_repo.register_imported_shops(['店A'])
        self.assertEqual([s.shop for s in shop_repo.list_shop_summaries()], ['店A'])

    def test_profile_relogin_restores_deleted_shop(self):
        shop_repo.delete_shop_profile('店A')
        (self.profiles / '店A').mkdir()
        self.assertEqual([s.shop for s in shop_repo.list_shop_summaries()], ['店A'])

    def test_busy_profile_reports_failure_and_keeps_shop_visible(self):
        with patch('shutil.rmtree', side_effect=PermissionError('file in use')):
            with self.assertRaisesRegex(RuntimeError, '关闭店铺浏览器'):
                shop_repo.delete_shop_profile('店A')
        self.assertNotIn('店A', shop_repo._load_deleted_shops())
        self.assertEqual(len(shop_repo.list_shop_summaries()), 1)

    def test_account_store_failure_is_not_silently_ignored(self):
        with patch.object(account_store, 'delete_by_shop', side_effect=RuntimeError('account DB busy')):
            with self.assertRaisesRegex(RuntimeError, 'account DB busy'):
                shop_repo.delete_shop_profile('店A')
        self.assertNotIn('店A', shop_repo._load_deleted_shops())

    def test_path_escape_is_rejected_before_removing_files(self):
        with patch('shutil.rmtree') as remove:
            for name in ('../outside', '.', ''):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    shop_repo.delete_shop_profile(name)
            remove.assert_not_called()

    def test_page_shows_delete_error_instead_of_refreshing_as_success(self):
        page = SimpleNamespace(_refresh_now=MagicMock(), _model=SimpleNamespace(login_checks={'店A': 'old'}))
        with patch('gui.app.pages.shops_page.msgbox.confirm_yes_no', return_value=True), patch.object(shop_repo, 'delete_shop_profile', side_effect=RuntimeError('浏览器占用')), patch('gui.app.pages.shops_page.msgbox.critical') as message, patch('gui.app.pages.shops_page.logger'):
            ShopsPage._confirm_and_delete(page, '店A')
        message.assert_called_once_with(page, '删除店铺失败', '浏览器占用')
        page._refresh_now.assert_not_called()

    def test_page_refreshes_and_clears_stale_login_status_after_delete(self):
        page = SimpleNamespace(_refresh_now=MagicMock(), _model=SimpleNamespace(login_checks={'店A': 'old'}))
        with patch('gui.app.pages.shops_page.msgbox.confirm_yes_no', return_value=True), patch.object(shop_repo, 'delete_shop_profile'):
            ShopsPage._confirm_and_delete(page, '店A')
        page._refresh_now.assert_called_once()
        self.assertNotIn('店A', page._model.login_checks)


if __name__ == '__main__':
    unittest.main()
