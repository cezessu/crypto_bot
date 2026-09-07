import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from _requests_stub import install_requests_stub_if_missing
except ModuleNotFoundError:
    from tests._requests_stub import install_requests_stub_if_missing


install_requests_stub_if_missing()


class FakeTeleBot:
    def __init__(self, token, **kwargs):
        self.token = token
        self.threaded = kwargs.get("threaded", True)
        self.exception_handler = kwargs.get("exception_handler")
        self.webhook_config = None

    def message_handler(self, *args, **kwargs):
        return lambda function: function

    def callback_query_handler(self, *args, **kwargs):
        return lambda function: function

    def set_webhook(self, *, url, allowed_updates, drop_pending_updates):
        self.webhook_config = (url, allowed_updates, drop_pending_updates)


class FakeFlask:
    def __init__(self, name):
        self.name = name

    def route(self, *args, **kwargs):
        return lambda function: function


class BotImportTests(unittest.TestCase):
    def test_bot_imports_and_registers_lesson_scenarios(self):
        telebot_module = types.ModuleType("telebot")
        telebot_module.TeleBot = FakeTeleBot
        telebot_module.types = types.SimpleNamespace()

        flask_module = types.ModuleType("flask")
        flask_module.Flask = FakeFlask
        flask_module.request = types.SimpleNamespace()

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "123456:TEST",
                "DATABASE_PATH": str(Path(temp_dir) / "bot.sqlite3"),
                "SUPABASE_DATABASE_URL": "",
                "MEXC_API_KEY": "  test-key\n",
                "MEXC_API_SECRET": "\ttest-secret \n",
                "RENDER_EXTERNAL_HOSTNAME": "crypto-bot.example",
            },
            clear=False,
        ), patch.dict(
            sys.modules,
            {"telebot": telebot_module, "flask": flask_module},
        ):
            spec = importlib.util.spec_from_file_location("bot_smoke", "bot.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            self.assertIsNotNone(module.exchange_clients["mexc"])
            self.assertEqual(module.exchange_clients["mexc"].api_key, "test-key")
            self.assertEqual(module.exchange_clients["mexc"].api_secret, "test-secret")
            self.assertEqual(set(module.LESSON_FILES), set(range(1, 8)))
            lesson_one_text = module.get_after_lesson_text(1)
            self.assertIn("MEXC или Bitunix", lesson_one_text)
            self.assertIn("первая сделка", module.get_lesson_requirement_text(2))
            for lesson_number in range(1, 8):
                self.assertIn(f"Методичка №{lesson_number}", module.get_lesson_requirement_text(lesson_number))
            self.assertTrue(callable(module.exchange_callback))
            self.assertTrue(callable(module.start_handler))
            self.assertTrue(callable(module.referral_handler))
            self.assertIsNotNone(module.bot.exception_handler)
            self.assertFalse(module.bot.threaded)
            self.assertTrue(module.IS_RENDER)
            self.assertEqual(
                module.bot.webhook_config,
                (
                    f"https://crypto-bot.example/{module.WEBHOOK_PATH}",
                    ["message", "callback_query", "my_chat_member"],
                    False,
                ),
            )
            for lesson_number in range(2, 8):
                self.assertTrue(callable(getattr(module, f"get_lesson{lesson_number}")))

            with patch.dict(
                os.environ,
                {"ADMIN_TELEGRAM_IDS": "invalid-admin"},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "non-numeric"):
                    module.load_admin_telegram_ids()


if __name__ == "__main__":
    unittest.main()
