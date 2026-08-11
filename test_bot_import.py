import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from _requests_stub import install_requests_stub_if_missing


install_requests_stub_if_missing()


class FakeTeleBot:
    def __init__(self, token):
        self.token = token

    def message_handler(self, *args, **kwargs):
        return lambda function: function

    def callback_query_handler(self, *args, **kwargs):
        return lambda function: function


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
                "MEXC_API_KEY": "",
                "MEXC_API_SECRET": "",
            },
            clear=False,
        ), patch.dict(
            sys.modules,
            {"telebot": telebot_module, "flask": flask_module},
        ):
            spec = importlib.util.spec_from_file_location("bot_smoke", "bot.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            self.assertEqual(set(module.LESSON_FILES), set(range(1, 8)))
            self.assertTrue(callable(module.start_handler))
            self.assertTrue(callable(module.referral_handler))
            self.assertTrue(callable(module.main_menu_handler))
            self.assertEqual(module.MENU_LESSON_BUTTONS["📘 Методичка №2"], 2)
            self.assertEqual(module.MENU_LESSON_BUTTONS["📘 Методичка №7"], 7)
            self.assertNotEqual(module.WEBHOOK_PATH, "123456:TEST")
            self.assertTrue(module.WEBHOOK_PATH.startswith("telegram-"))
            for lesson_number in range(2, 8):
                self.assertTrue(callable(getattr(module, f"get_lesson{lesson_number}")))


if __name__ == "__main__":
    unittest.main()
