import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

try:
    from _requests_stub import install_requests_stub_if_missing
except ModuleNotFoundError:
    from tests._requests_stub import install_requests_stub_if_missing


install_requests_stub_if_missing()

from mexc_client import ReferralData
from storage import BotStorage, LessonDeliveryClaimStatus, LessonReviewStatus


ADMIN_ID = 7_000_000_005


class FakeButton:
    def __init__(self, text, **kwargs):
        self.text = text
        self.callback_data = kwargs.get("callback_data")
        self.url = kwargs.get("url")


class FakeMarkup:
    def __init__(self, **kwargs):
        self.rows = []

    def add(self, *buttons):
        self.rows.append(list(buttons))

    def row(self, *buttons):
        self.rows.append(list(buttons))


class FakeInputFile:
    def __init__(self, file_object, file_name=None):
        self.file_object = file_object
        self.file_name = file_name


class FakeTeleBot:
    def __init__(self, token, **kwargs):
        self.token = token
        self.messages = []
        self.documents = []
        self.edits = []
        self.callback_answers = []
        self.next_steps = {}
        self.fail_message_chat_ids = set()
        self.fail_next_document = False
        self.fail_document_attempts = set()
        self.document_attempts = 0
        self._next_message_id = 1

    def message_handler(self, *args, **kwargs):
        return lambda function: function

    def callback_query_handler(self, *args, **kwargs):
        return lambda function: function

    def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_message_chat_ids:
            raise RuntimeError("simulated Telegram message failure")
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=chat_id),
            message_id=self._next_message_id,
            text=text,
        )
        self._next_message_id += 1
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": kwargs.get("reply_markup"),
                "parse_mode": kwargs.get("parse_mode"),
                "message": message,
            }
        )
        return message

    def send_document(self, chat_id, document, **kwargs):
        self.document_attempts += 1
        if (
            self.fail_next_document
            or self.document_attempts in self.fail_document_attempts
        ):
            self.fail_next_document = False
            raise RuntimeError("simulated Telegram document failure")
        self.documents.append(
            {
                "chat_id": chat_id,
                "filename": Path(document.name).name,
                "caption": kwargs.get("caption"),
            }
        )

    def send_chat_action(self, *args, **kwargs):
        return None

    def register_next_step_handler(self, message, callback, *args):
        self.next_steps.setdefault(message.chat.id, []).append((callback, args))

    def clear_step_handler_by_chat_id(self, chat_id):
        self.next_steps.pop(chat_id, None)

    def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        for message in self.messages:
            if message["chat_id"] == kwargs["chat_id"] and message["message"].message_id == kwargs["message_id"]:
                message["text"] = kwargs["text"]
                message["reply_markup"] = kwargs.get("reply_markup")

    def answer_callback_query(self, callback_id, text, **kwargs):
        self.callback_answers.append(
            {
                "id": callback_id,
                "text": text,
                "show_alert": kwargs.get("show_alert", False),
            }
        )

    def set_webhook(self, **kwargs):
        return True


class FakeFlask:
    def __init__(self, name):
        self.name = name

    def route(self, *args, **kwargs):
        return lambda function: function


def load_bot_module(database_path):
    telebot_module = types.ModuleType("telebot")
    telebot_module.TeleBot = FakeTeleBot
    telebot_module.types = types.SimpleNamespace(
        InlineKeyboardMarkup=FakeMarkup,
        InlineKeyboardButton=FakeButton,
        ReplyKeyboardMarkup=FakeMarkup,
        KeyboardButton=FakeButton,
        InputFile=FakeInputFile,
        Update=types.SimpleNamespace(de_json=lambda payload: payload),
    )

    flask_module = types.ModuleType("flask")
    flask_module.Flask = FakeFlask
    flask_module.request = types.SimpleNamespace()

    module_name = f"bot_manual_review_{uuid.uuid4().hex}"
    with patch.dict(
        os.environ,
        {
            "BOT_TOKEN": "123456:TEST",
            "DATABASE_PATH": str(database_path),
            "SUPABASE_DATABASE_URL": "",
            "MEXC_API_KEY": "", "BITUNIX_API_KEY": "", "BITUNIX_API_SECRET": "",
            "MEXC_API_SECRET": "",
            "RENDER_EXTERNAL_HOSTNAME": "",
            "ADMIN_TELEGRAM_IDS": str(ADMIN_ID),
        },
        clear=False,
    ), patch.dict(
        sys.modules,
        {"telebot": telebot_module, "flask": flask_module},
    ):
        spec = importlib.util.spec_from_file_location(module_name, "bot.py")
        module = importlib.util.module_from_spec(spec)
        with patch("storage.create_storage_from_env", return_value=BotStorage(str(database_path))):
            spec.loader.exec_module(module)
    return module


def unavailable_volume_referral(uid):
    return ReferralData(
        uid=uid,
        deposit_amount=None,
        trading_amount=None,
        first_trade_time=1_700_000_000_000,
        last_trade_time=1_700_000_000_000,
    )


def callback(data, *, from_user_id=ADMIN_ID, chat_id=None, message_id=1):
    return types.SimpleNamespace(
        id=f"callback-{data}",
        data=data,
        from_user=types.SimpleNamespace(id=from_user_id),
        message=types.SimpleNamespace(
            chat=types.SimpleNamespace(id=from_user_id if chat_id is None else chat_id),
            message_id=message_id,
        ),
    )


class ManualLessonReviewBotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module = load_bot_module(Path(self.temp_dir.name) / "bot.sqlite3")
        self.user_id = 123_456_789
        self.uid = "54458789"
        self.module.storage.set_exchange(self.user_id, "mexc")
        self.module.exchange_clients["mexc"] = object()
        self.module.get_referral_cached = lambda uid, **kwargs: (
            unavailable_volume_referral(uid)
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def submit(self, lesson_number=3):
        self.module.storage.set_exchange(self.user_id, "mexc")
        for n in range(1, lesson_number):
            self.module.storage.claim_lesson(self.user_id, n)
        self.module.check_lesson_with_uid(
            self.user_id,
            lesson_number,
            self.uid,
        )
        return self.module.storage.request_lesson_review(
            self.user_id,
            lesson_number,
        ).request

    def admin_messages(self):
        return [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == ADMIN_ID
        ]

    @staticmethod
    def markup_callbacks(markup):
        if markup is None:
            return set()
        return {
            button.callback_data
            for row in markup.rows
            for button in row
            if button.callback_data is not None
        }

    def assert_personal_invitation(self, message, user_id):
        expected = f"https://t.me/growthtradebot?start=ref_{user_id}"
        self.assertIn(expected, message["text"])
        buttons = [button for row in message["reply_markup"].rows for button in row]
        share = next(button for button in buttons if button.text == "👥 Пригласить друга")
        self.assertEqual(parse_qs(urlparse(share.url).query)["url"], [expected])
        self.assertEqual(urlparse(share.url).netloc, "t.me")
        self.assertEqual(urlparse(share.url).path, "/share/url")

    def test_repeated_lessons_show_recipient_specific_invitation_for_next_step(self):
        self.module._bot_username = "growthtradebot"
        for user_id in (self.user_id, self.user_id + 1):
            for previous_lesson in (3, 5, 6):
                self.module.send_already_issued_guidance(user_id, previous_lesson)
                message = self.module.bot.messages[-1]
                self.assert_personal_invitation(message, user_id)
                self.assertIn(self.module.LESSON_VIDEO_URLS[previous_lesson], message["text"])

    def test_delivered_lessons_include_invitation_with_next_requirement(self):
        self.module._bot_username = "growthtradebot"
        with patch.object(self.module, "send_lesson_part", return_value=True):
            for previous_lesson in (3, 5, 6):
                self.assertTrue(self.module.send_lesson(self.user_id, previous_lesson, "test-delivery"))
                self.assert_personal_invitation(self.module.bot.messages[-1], self.user_id)

    def test_friend_conditions_show_invitation_without_menu_detour(self):
        self.module._bot_username = "growthtradebot"
        self.module.storage.bind_exchange_uid(self.user_id, self.uid)
        for lesson in (4, 6, 7):
            for previous in range(1, lesson):
                self.module.storage.claim_lesson(self.user_id, previous)
            self.module.bot.messages.clear()
            with patch.object(self.module, "check_lesson_with_uid"):
                self.module.process_lesson_for_user(self.user_id, lesson)
            self.assert_personal_invitation(self.module.bot.messages[0], self.user_id)
            self.assertFalse(self.module.storage.is_lesson_issued(self.user_id, lesson))

    def test_returning_user_start_resumes_without_captcha(self):
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=self.user_id),
            text="/start",
        )

        with patch.object(
            self.module.storage,
            "is_lesson_issued",
            return_value=True,
        ), patch.object(self.module, "send_captcha") as send_captcha:
            self.module.start_handler(message)

        send_captcha.assert_not_called()
        resume = self.module.bot.messages[-1]
        self.assertEqual(resume["chat_id"], self.user_id)
        self.assertIn("С возвращением", resume["text"])
        self.assertIsNotNone(resume["reply_markup"])

    def test_lesson2_explains_requirement_before_requesting_uid(self):
        self.module.exchange_clients["mexc"] = object()
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=self.user_id),
            text="📘 Методичка №2",
        )

        with patch.object(
            self.module.storage,
            "is_lesson_issued",
            side_effect=lambda _user_id, lesson_number: lesson_number == 1,
        ):
            self.module.process_lesson_request(message, 2)

        user_messages = [
            item for item in self.module.bot.messages
            if item["chat_id"] == self.user_id
        ]
        self.assertEqual(len(user_messages), 1)
        self.assertNotIn("Выберите биржу", user_messages[0]["text"])
        self.assertIn("первая сделка", user_messages[0]["text"])
        self.assertIn("числовой UID", user_messages[0]["text"])

    def test_exchange_selection_continues_to_uid_without_second_lesson_click(self):
        user_id = 445566
        self.module.storage.claim_lesson(user_id, 1)
        self.module.exchange_clients["bitunix"] = object()
        self.module.process_lesson_for_user(user_id, 2)
        self.assertEqual(len(self.module.bot.messages), 1)
        self.assertIn("exchange:bitunix:2", self.markup_callbacks(self.module.bot.messages[-1]["reply_markup"]))
        self.assertEqual(self.module.bot.messages[-1]["parse_mode"], "HTML")
        for url in self.module.EXCHANGE_REFERRAL_URLS.values():
            self.assertIn(f'href="{url}"', self.module.bot.messages[-1]["text"])
        self.module.exchange_callback(callback("exchange:bitunix:2", from_user_id=user_id, chat_id=user_id))
        self.assertEqual(len(self.module.bot.messages), 1)
        self.assertIn("числовой UID", self.module.bot.messages[-1]["text"])
        self.assertIn("Bitunix", self.module.bot.messages[-1]["text"])
        self.assertEqual(self.module.bot.next_steps[user_id], [(self.module.process_uid, (2,))])
        self.assertIn("Методичка №2 · Bitunix", self.module.bot.edits[-1]["text"])

    def test_switch_exchange_replaces_pending_uid_handler(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        self.module.exchange_clients["bitunix"] = object()
        self.module.process_lesson_for_user(self.user_id, 2)
        self.module.choose_exchange_callback(callback("choose_exchange:2", from_user_id=self.user_id))
        self.assertNotIn(self.user_id, self.module.bot.next_steps)
        self.module.exchange_callback(callback("exchange:bitunix:2", from_user_id=self.user_id))
        self.assertEqual(len(self.module.bot.next_steps[self.user_id]), 1)
        self.assertEqual(self.module.storage.get_user(self.user_id).exchange, "bitunix")

    def test_switching_exchange_keeps_one_message_and_preserves_lesson_progress(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        self.module.exchange_clients["bitunix"] = object()
        self.module.process_lesson_for_user(self.user_id, 2)
        message_id = self.module.bot.messages[-1]["message"].message_id
        self.assertIn("MEXC", self.module.bot.messages[-1]["text"])
        for exchange in ("bitunix", "mexc", "bitunix"):
            self.module.choose_exchange_callback(callback("choose_exchange:2", from_user_id=self.user_id, chat_id=self.user_id, message_id=message_id))
            self.assertEqual(self.module.bot.edits[-1]["parse_mode"], "HTML")
            for url in self.module.EXCHANGE_REFERRAL_URLS.values():
                self.assertIn(f'href="{url}"', self.module.bot.messages[0]["text"])
            self.module.exchange_callback(callback(f"exchange:{exchange}:2", from_user_id=self.user_id, chat_id=self.user_id, message_id=message_id))
            self.assertEqual(len(self.module.bot.messages), 1)
            self.assertIn(self.module.EXCHANGE_NAMES[exchange], self.module.bot.messages[0]["text"])
            other_name = "MEXC" if exchange == "bitunix" else "Bitunix"
            self.assertNotIn(other_name, self.module.bot.messages[0]["text"])
            self.assertEqual(self.module.bot.next_steps[self.user_id], [(self.module.process_uid, (2,))])
        self.assertTrue(self.module.storage.is_lesson_issued(self.user_id, 1))
        self.assertIsNone(self.module.storage.get_user(self.user_id).exchange_uid)

    def test_exchange_edit_failure_still_allows_uid_input(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        self.module.exchange_clients["bitunix"] = object()
        with patch.object(self.module.bot, "edit_message_text", side_effect=RuntimeError("message cannot be edited")):
            self.module.exchange_callback(callback("exchange:bitunix:2", from_user_id=self.user_id, chat_id=self.user_id))
        self.assertIn("Bitunix", self.module.bot.messages[-1]["text"])
        self.assertIn("числовой UID", self.module.bot.messages[-1]["text"])
        self.assertEqual(self.module.bot.next_steps[self.user_id], [(self.module.process_uid, (2,))])

    def test_bound_mexc_user_goes_directly_to_check(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        self.module.storage.bind_exchange_uid(self.user_id, self.uid)
        with patch.object(self.module, "check_lesson_with_uid") as check:
            self.module.process_lesson_for_user(self.user_id, 2)
        check.assert_called_once_with(self.user_id, 2, self.uid, force_refresh=False)
        self.assertEqual(self.module.bot.messages, [])

    def test_stale_selection_cannot_skip_prerequisites_or_change_bound_exchange(self):
        self.module.exchange_callback(callback("exchange:mexc:3", from_user_id=self.user_id))
        self.assertNotIn(self.user_id, self.module.bot.next_steps)
        self.assertIn("не получили методичку №1", self.module.bot.messages[-1]["text"])
        self.module.storage.bind_exchange_uid(self.user_id, self.uid)
        self.module.exchange_callback(callback("exchange:bitunix:2", from_user_id=self.user_id))
        self.assertEqual(self.module.storage.get_user(self.user_id).exchange, "mexc")
        self.assertTrue(self.module.bot.callback_answers[-1]["show_alert"])

    def test_invalid_uid_can_be_reentered_immediately(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        self.module.process_uid(types.SimpleNamespace(from_user=types.SimpleNamespace(id=self.user_id), text="not a uid"), 2)
        self.assertIn("Попробуйте ещё раз", self.module.bot.messages[-1]["text"])
        self.assertEqual(self.module.bot.next_steps[self.user_id], [(self.module.process_uid, (2,))])
        with patch.object(self.module, "check_lesson_with_uid") as check:
            self.module.process_uid(types.SimpleNamespace(from_user=types.SimpleNamespace(id=self.user_id), text=self.uid), 2)
        check.assert_called_once_with(self.user_id, 2, self.uid, force_refresh=False)

    def test_uid_prompt_is_for_existing_referrals_on_both_exchanges(self):
        for exchange in ("mexc", "bitunix"):
            with self.subTest(exchange=exchange):
                self.module.storage.set_exchange(self.user_id, exchange)
                self.module.request_exchange_uid(self.user_id, 2)
                prompt = self.module.bot.messages[-1]
                self.assertIn("числовой UID", prompt["text"])
                self.assertEqual(self.markup_callbacks(prompt["reply_markup"]), {"choose_exchange:2"})
                self.assertTrue(all(button.url is None for row in prompt["reply_markup"].rows for button in row))
                self.assertIn(f'href="{self.module.EXCHANGE_REFERRAL_URLS[exchange]}"', prompt["text"])
                self.assertEqual(prompt["parse_mode"], "HTML")

    def test_old_exchange_button_continues_next_unissued_lesson(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        self.module.exchange_callback(callback("exchange:mexc", from_user_id=self.user_id))
        self.assertIn("числовой UID", self.module.bot.edits[-1]["text"])
        self.assertEqual(self.module.bot.next_steps[self.user_id], [(self.module.process_uid, (2,))])

    def test_unrecognized_uid_keeps_input_open(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        with patch.object(self.module, "get_referral_cached", return_value=None):
            self.module.check_lesson_with_uid(self.user_id, 2, self.uid)
        self.assertEqual(self.module.bot.next_steps[self.user_id], [(self.module.process_uid, (2,))])
        self.assertIsNone(self.module.storage.get_user(self.user_id).exchange_uid)

    def test_exchange_choice_does_not_award_lesson_when_client_unavailable(self):
        self.module.storage.claim_lesson(self.user_id, 1)
        self.module.exchange_clients["bitunix"] = None
        self.module.exchange_callback(callback("exchange:bitunix:2", from_user_id=self.user_id))
        self.assertFalse(self.module.storage.is_lesson_issued(self.user_id, 2))
        self.assertNotIn(self.user_id, self.module.bot.next_steps)
        self.assertIn("временно недоступна", self.module.bot.messages[-1]["text"])

    def test_future_lesson_redirects_to_first_missing_lesson(self):
        self.module.exchange_clients["mexc"] = object()
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=self.user_id),
            text="📘 Методичка №6",
        )

        with patch.object(
            self.module.storage,
            "is_lesson_issued",
            side_effect=lambda _user_id, lesson_number: lesson_number == 1,
        ):
            self.module.process_lesson_request(message, 6)

        user_messages = [
            item for item in self.module.bot.messages
            if item["chat_id"] == self.user_id
        ]
        self.assertEqual(len(user_messages), 1)
        self.assertIn("Методичка №6 пока недоступна", user_messages[0]["text"])
        self.assertIn("не получили методичку №2", user_messages[0]["text"])
        self.assertNotIn("Как получить методичку №2", user_messages[0]["text"])

    def test_lesson_button_is_not_misread_as_uid(self):
        self.module.exchange_clients["mexc"] = object()
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=self.user_id),
            text="📘 Методичка №6",
        )

        with patch.object(
            self.module.storage,
            "is_lesson_issued",
            side_effect=lambda _user_id, lesson_number: lesson_number == 1,
        ):
            self.module.process_uid(message, 2)

        user_texts = [
            item["text"] for item in self.module.bot.messages
            if item["chat_id"] == self.user_id
        ]
        self.assertFalse(any("UID должен содержать" in text for text in user_texts))
        self.assertTrue(any("не получили методичку №2" in text for text in user_texts))
        self.assertFalse(any("Где найти свой UID" in text for text in user_texts))

    def test_lesson_skip_from_new_user_redirects_to_lesson1(self):
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=self.user_id),
            text="📘 Методичка №4",
        )

        self.module.process_lesson_request(message, 4)

        user_messages = [
            item for item in self.module.bot.messages
            if item["chat_id"] == self.user_id
        ]
        self.assertEqual(len(user_messages), 1)
        self.assertIn("не получили методичку №1", user_messages[0]["text"])
        self.assertNotIn("Как получить методичку №1", user_messages[0]["text"])
        self.assertIsNotNone(user_messages[0]["reply_markup"])

    def test_already_issued_lesson_restores_next_step_guidance(self):
        with patch.object(
            self.module.storage,
            "claim_lesson_delivery",
            return_value=LessonDeliveryClaimStatus.ALREADY_ISSUED,
        ):
            delivered = self.module.issue_lesson_once(self.user_id, 1)

        self.assertFalse(delivered)
        guidance = self.module.bot.messages[-1]
        self.assertIn("уже была вам выдана", guidance["text"])
        self.assertIn("Методичка №2", guidance["text"])
        self.assertIn('<a href="https://promote.mexc.com/r/RV1DdMzE">MEXC</a>', guidance["text"])
        self.assertIn('<a href="https://www.bitunix.com/register?vipCode=GT777">Bitunix</a>', guidance["text"])
        self.assertIsNotNone(guidance["reply_markup"])

    def test_all_seven_lessons_deliver_all_eleven_pdfs_once(self):
        for lesson_number in range(1, 8):
            self.assertTrue(
                self.module.issue_lesson_once(self.user_id, lesson_number)
            )

        expected_files = {
            file_name
            for lesson_files in self.module.LESSON_FILES.values()
            for file_name in lesson_files.values()
            if file_name
        }
        delivered_files = {
            document["filename"] for document in self.module.bot.documents
        }
        self.assertEqual(delivered_files, expected_files)
        self.assertEqual(len(self.module.bot.documents), 11)
        self.assertEqual(
            self.module.storage.issued_lessons(self.user_id),
            tuple(range(1, 8)),
        )

        for lesson_number in range(1, 8):
            self.assertFalse(
                self.module.issue_lesson_once(self.user_id, lesson_number)
            )
        self.assertEqual(len(self.module.bot.documents), 11)

    def test_redownload_buttons_keep_video_and_invitation(self):
        self.module._bot_username = "growthtradebot"
        for lesson in range(1, 8):
            self.module.send_already_issued_guidance(self.user_id, lesson)
            message = self.module.bot.messages[-1]
            self.assertIn(f"lesson_download:{lesson}", self.markup_callbacks(message["reply_markup"]))
            self.assertIn(self.module.LESSON_VIDEO_URLS[lesson], message["text"])
            if lesson in (3, 5, 6):
                self.assert_personal_invitation(message, self.user_id)

    def test_redownload_resends_all_unlocked_files_without_changing_progress(self):
        for lesson in range(1, 8):
            self.module.issue_lesson_once(self.user_id, lesson)
        before = self.module.storage.get_user(self.user_id)
        self.module.bot.documents.clear()
        with patch.object(self.module.storage, "claim_lesson_delivery", side_effect=AssertionError("must not issue again")):
            for lesson in range(1, 8):
                self.module.handle_lesson_download(callback(f"lesson_download:{lesson}", from_user_id=self.user_id))
        self.assertEqual(len(self.module.bot.documents), 11)
        self.assertEqual(self.module.storage.get_user(self.user_id), before)
        self.assertEqual(self.module.storage.issued_lessons(self.user_id), tuple(range(1, 8)))
        for lesson in range(1, 8):
            main = next(doc for doc in self.module.bot.documents if doc["filename"] == self.module.LESSON_FILES[lesson]["main"])
            self.assertEqual(main["chat_id"], self.user_id)
            self.assertIn(self.module.LESSON_VIDEO_URLS[lesson], main["caption"])

    def test_redownload_denies_locked_lesson_other_user_and_invalid_callbacks(self):
        self.module.issue_lesson_once(self.user_id, 1)
        self.module.bot.documents.clear()
        cases = [
            callback("lesson_download:2", from_user_id=self.user_id),
            callback("lesson_download:1", from_user_id=self.user_id + 1),
            callback("lesson_download:1", from_user_id=self.user_id, chat_id=-123),
            callback("lesson_download:1", from_user_id=self.user_id, chat_id=self.user_id + 1),
            callback("lesson_download:8", from_user_id=self.user_id),
            callback("lesson_download:1x", from_user_id=self.user_id),
        ]
        for call in cases:
            self.module.handle_lesson_download(call)
            self.assertTrue(self.module.bot.callback_answers[-1]["show_alert"])
        self.assertEqual(self.module.bot.documents, [])

    def test_redownload_storage_failure_denies_access(self):
        with patch.object(self.module.storage, "is_lesson_issued", side_effect=self.module.StorageError("unavailable")):
            self.module.handle_lesson_download(callback("lesson_download:1", from_user_id=self.user_id))
        self.assertEqual(self.module.bot.documents, [])
        self.assertTrue(self.module.bot.callback_answers[-1]["show_alert"])

    def test_redownload_failed_bonus_can_be_retried(self):
        self.module.issue_lesson_once(self.user_id, 2)
        self.module.bot.documents.clear()
        self.module.bot.document_attempts = 0
        self.module.bot.fail_document_attempts = {2}
        call = callback("lesson_download:2", from_user_id=self.user_id)
        self.module.handle_lesson_download(call)
        self.assertIn("Не удалось отправить все файлы", self.module.bot.messages[-1]["text"])
        self.assertTrue(self.module.storage.is_lesson_issued(self.user_id, 2))
        self.module.bot.documents.clear()
        self.module.handle_lesson_download(call)
        self.assertEqual(len(self.module.bot.documents), 2)

    def test_redownload_missing_file_keeps_lesson_unlocked(self):
        self.module.issue_lesson_once(self.user_id, 1)
        self.module.bot.documents.clear()
        with patch.object(self.module, "BASE_DIR", Path(self.temp_dir.name)):
            self.module.handle_lesson_download(callback("lesson_download:1", from_user_id=self.user_id))
        self.assertTrue(self.module.storage.is_lesson_issued(self.user_id, 1))
        self.assertEqual(self.module.bot.documents, [])
        self.assertIn("Скачать ещё раз", self.module.bot.messages[-1]["text"])

    def test_lesson_files_are_resolved_from_the_bot_directory(self):
        original_cwd = Path.cwd()
        try:
            os.chdir(self.temp_dir.name)
            self.assertTrue(self.module.issue_lesson_once(self.user_id, 1))
        finally:
            os.chdir(original_cwd)

        self.assertEqual(
            [document["filename"] for document in self.module.bot.documents],
            ["Фундаментальныеосновы.pdf"],
        )

    def test_lessons_3_and_7_create_review_with_admin_buttons(self):
        for lesson_number in (3, 7):
            with self.subTest(lesson_number=lesson_number):
                if lesson_number == 7:
                    self.user_id += 1
                    self.uid = "54458790"
                review = self.submit(lesson_number)
                admin_message = self.admin_messages()[-1]
                callback_data = {
                    button.callback_data
                    for row in admin_message["reply_markup"].rows
                    for button in row
                }

                self.assertEqual(review.status, LessonReviewStatus.PENDING)
                self.assertEqual(review.exchange_uid, self.uid)
                self.assertEqual(
                    callback_data,
                    {f"mr:a:{review.request_id}", f"mr:r:{review.request_id}"},
                )
                self.assertIn(str(self.user_id), admin_message["text"])
                self.assertTrue(
                    any(
                        message["chat_id"] == self.user_id
                        and "отправлена администратору" in message["text"]
                        for message in self.module.bot.messages
                    )
                )

    def test_duplicate_submission_reuses_request_without_admin_spam(self):
        first = self.submit()
        first_admin_count = len(self.admin_messages())
        second = self.submit()

        self.assertEqual(second.request_id, first.request_id)
        self.assertEqual(len(self.admin_messages()), first_admin_count)
        self.assertTrue(
            any("уже ожидает" in message["text"] for message in self.module.bot.messages)
        )

    def test_concurrent_submissions_send_one_admin_notification(self):
        self.module.storage.set_exchange(self.user_id, "mexc")
        self.module.storage.bind_exchange_uid(self.user_id, self.uid)
        original_request = self.module.storage.request_lesson_review
        both_requests_loaded = threading.Barrier(2)

        def request_then_wait(*args, **kwargs):
            submission = original_request(*args, **kwargs)
            both_requests_loaded.wait(timeout=5)
            return submission

        with patch.object(
            self.module.storage,
            "request_lesson_review",
            side_effect=request_then_wait,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: self.module.submit_lesson_review(self.user_id, 3),
                    range(2),
                )
            )

        self.assertEqual(results, [True, True])
        self.assertEqual(len(self.admin_messages()), 1)
        stored = self.module.storage.request_lesson_review(
            self.user_id,
            3,
        ).request
        self.assertIsNotNone(stored.admin_notified_at)

    def test_uid_not_found_does_not_create_manual_review(self):
        self.module.get_referral_cached = lambda uid, **kwargs: None

        self.module.check_lesson_with_uid(self.user_id, 3, self.uid)

        self.assertIsNone(self.module.storage.get_lesson_review_request(1))
        self.assertEqual(self.admin_messages(), [])
        self.assertIsNone(self.module.storage.get_user(self.user_id).exchange_uid)

    def test_lesson7_with_three_qualified_invites_is_automatic(self):
        self.module.storage.ensure_user(self.user_id)
        for offset in range(3):
            friend_id = 200_000_000 + offset
            self.module.storage.assign_inviter(friend_id, self.user_id)
            self.module.storage.set_exchange(friend_id, "mexc")
            self.module.storage.bind_exchange_uid(friend_id, str(80_000_000 + offset))
            self.module.storage.mark_qualified(friend_id)
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=self.user_id),
            text="📘 Методичка №7",
        )

        real_is_lesson_issued = self.module.storage.is_lesson_issued
        with patch.object(
            self.module.storage,
            "is_lesson_issued",
            side_effect=lambda user_id, lesson_number: (
                True
                if lesson_number < 7
                else real_is_lesson_issued(user_id, lesson_number)
            ),
        ):
            self.module.process_lesson_request(message, 7)

        self.assertTrue(self.module.storage.is_lesson_issued(self.user_id, 7))
        self.assertEqual(len(self.module.bot.documents), 2)
        self.assertIsNone(self.module.storage.get_lesson_review_request(1))
        self.assertEqual(self.admin_messages(), [])

    def test_failed_admin_notification_is_retried_without_duplicate_request(self):
        self.module.bot.fail_message_chat_ids.add(ADMIN_ID)
        first = self.submit()
        self.assertIsNone(first.admin_notified_at)

        self.module.bot.fail_message_chat_ids.clear()
        second = self.submit()

        self.assertEqual(second.request_id, first.request_id)
        self.assertEqual(len(self.admin_messages()), 1)
        stored = self.module.storage.get_lesson_review_request(first.request_id)
        self.assertIsNotNone(stored.admin_notified_at)

    def test_unauthorized_or_wrong_chat_callback_cannot_decide(self):
        review = self.submit()

        self.module.handle_lesson_review_callback(
            callback(f"mr:a:{review.request_id}", from_user_id=111, chat_id=111)
        )
        self.module.handle_lesson_review_callback(
            callback(f"mr:a:{review.request_id}", chat_id=-100123)
        )

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.PENDING)
        self.assertEqual(self.module.bot.documents, [])
        self.assertTrue(
            all(answer["show_alert"] for answer in self.module.bot.callback_answers)
        )

    def test_approve_issues_exact_lesson_and_replay_does_not_duplicate(self):
        review = self.submit(3)
        approve = callback(f"mr:a:{review.request_id}")

        self.module.handle_lesson_review_callback(approve)
        first_documents = list(self.module.bot.documents)
        self.module.handle_lesson_review_callback(approve)

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.FULFILLED)
        self.assertEqual([item["chat_id"] for item in first_documents], [self.user_id] * 2)
        self.assertEqual(len(self.module.bot.documents), 2)
        self.assertTrue(self.module.storage.is_lesson_issued(self.user_id, 3))

    def test_approved_review_reports_busy_when_global_delivery_is_claimed(self):
        review = self.submit(3)
        decision = self.module.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.APPROVED,
            ADMIN_ID,
        )
        self.assertEqual(decision.request.status, LessonReviewStatus.APPROVED)
        self.assertEqual(
            self.module.storage.claim_lesson_delivery(
                self.user_id,
                3,
                "global-delivery-token",
                60_000,
            ),
            LessonDeliveryClaimStatus.ACQUIRED,
        )

        delivery = self.module.deliver_approved_lesson_review(decision.request)

        self.assertEqual(delivery, self.module.REVIEW_DELIVERY_BUSY)
        self.assertEqual(
            self.module.storage.get_lesson_review_request(review.request_id).status,
            LessonReviewStatus.APPROVED,
        )
        self.assertFalse(self.module.storage.is_lesson_issued(self.user_id, 3))
        self.assertEqual(self.module.bot.documents, [])

    def test_atomic_lesson_completion_fulfills_review_without_followup_write(self):
        review = self.submit(3)
        approve = callback(f"mr:a:{review.request_id}")

        with patch.object(
            self.module.storage,
            "mark_lesson_review_fulfilled",
            side_effect=AssertionError("atomic completion should already fulfill"),
        ):
            self.module.handle_lesson_review_callback(approve)
            self.module.handle_lesson_review_callback(approve)

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.FULFILLED)
        self.assertTrue(self.module.storage.is_lesson_issued(self.user_id, 3))
        self.assertEqual(len(self.module.bot.documents), 2)
        self.assertIsNone(self.module.bot.edits[-1]["reply_markup"])

    def test_delivery_failure_keeps_approval_and_callback_retries(self):
        review = self.submit(3)
        approve = callback(f"mr:a:{review.request_id}")
        self.module.bot.fail_next_document = True

        self.module.handle_lesson_review_callback(approve)

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.APPROVED)
        self.assertFalse(self.module.storage.is_lesson_issued(self.user_id, 3))

        self.module.handle_lesson_review_callback(approve)
        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.FULFILLED)
        self.assertEqual(len(self.module.bot.documents), 2)

    def test_bonus_failure_retries_only_the_missing_bonus(self):
        review = self.submit(3)
        approve = callback(f"mr:a:{review.request_id}")
        self.module.bot.fail_document_attempts.add(2)

        self.module.handle_lesson_review_callback(approve)

        self.assertEqual(
            [document["filename"] for document in self.module.bot.documents],
            ["3_урок.pdf"],
        )
        self.assertTrue(
            self.module.storage.is_lesson_part_delivered(
                self.user_id,
                3,
                "main",
            )
        )
        self.assertFalse(
            self.module.storage.is_lesson_part_delivered(
                self.user_id,
                3,
                "bonus",
            )
        )

        self.module.handle_lesson_review_callback(approve)

        self.assertEqual(
            [document["filename"] for document in self.module.bot.documents],
            ["3_урок.pdf", "Дополнительно к 3 уроку.pdf"],
        )
        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.FULFILLED)

    def test_concurrent_approve_cannot_fulfill_an_inflight_failed_delivery(self):
        review = self.submit(3)
        approve = callback(f"mr:a:{review.request_id}")
        delivery_started = threading.Event()
        allow_failure = threading.Event()

        def blocked_failure(chat_id, lesson_number, delivery_token):
            delivery_started.set()
            allow_failure.wait(timeout=5)
            return False

        self.module.send_lesson = blocked_failure
        first_callback = threading.Thread(
            target=self.module.handle_lesson_review_callback,
            args=(approve,),
        )
        first_callback.start()
        self.assertTrue(delivery_started.wait(timeout=5))

        self.module.handle_lesson_review_callback(approve)
        inflight = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(inflight.status, LessonReviewStatus.APPROVED)

        allow_failure.set()
        first_callback.join(timeout=5)
        self.assertFalse(first_callback.is_alive())
        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.APPROVED)
        self.assertFalse(self.module.storage.is_lesson_issued(self.user_id, 3))
        self.assertTrue(
            any(
                "уже выполняется" in answer["text"]
                for answer in self.module.bot.callback_answers
            )
        )

    def test_reject_is_blocked_while_automatic_delivery_is_inflight(self):
        review = self.submit(7)
        delivery_started = threading.Event()
        allow_success = threading.Event()

        def blocked_success(chat_id, lesson_number, delivery_token):
            delivery_started.set()
            allow_success.wait(timeout=5)
            return True

        self.module.send_lesson = blocked_success
        delivery_thread = threading.Thread(
            target=self.module.issue_lesson_once,
            args=(self.user_id, 7),
        )
        delivery_thread.start()
        self.assertTrue(delivery_started.wait(timeout=5))

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{review.request_id}")
        )

        inflight = self.module.storage.get_lesson_review_request(review.request_id)
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertEqual(inflight.status, LessonReviewStatus.PENDING)
        self.assertEqual(rejection_notices, [])
        self.assertIn(
            "уже выполняется",
            self.module.bot.callback_answers[-1]["text"],
        )

        allow_success.set()
        delivery_thread.join(timeout=5)
        self.assertFalse(delivery_thread.is_alive())
        self.assertEqual(
            self.module.storage.get_lesson_review_request(review.request_id).status,
            LessonReviewStatus.FULFILLED,
        )

    def test_reject_notifies_once_and_cannot_be_reversed(self):
        review = self.submit(7)
        reject = callback(f"mr:r:{review.request_id}")

        self.module.handle_lesson_review_callback(reject)
        self.module.handle_lesson_review_callback(reject)
        self.module.handle_lesson_review_callback(
            callback(f"mr:a:{review.request_id}")
        )

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertEqual(stored.status, LessonReviewStatus.REJECTED)
        self.assertEqual(len(rejection_notices), 1)
        self.assertEqual(self.module.bot.documents, [])

    def test_concurrent_reject_callbacks_send_one_user_notice(self):
        review = self.submit(7)
        original_decide = self.module.storage.decide_lesson_review_request
        both_decisions_loaded = threading.Barrier(2)

        def decide_then_wait(*args, **kwargs):
            decision = original_decide(*args, **kwargs)
            both_decisions_loaded.wait(timeout=5)
            return decision

        with patch.object(
            self.module.storage,
            "decide_lesson_review_request",
            side_effect=decide_then_wait,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            list(
                executor.map(
                    self.module.handle_lesson_review_callback,
                    (
                        callback(f"mr:r:{review.request_id}", message_id=101),
                        callback(f"mr:r:{review.request_id}", message_id=102),
                    ),
                )
            )

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertEqual(stored.status, LessonReviewStatus.REJECTED)
        self.assertIsNotNone(stored.user_notified_at)
        self.assertEqual(len(rejection_notices), 1)

    def test_losing_concurrent_reject_leaves_no_retry_or_stale_notice_text(self):
        review = self.submit(7)
        original_decide = self.module.storage.decide_lesson_review_request
        both_decisions_loaded = threading.Barrier(2)
        winner_finished = threading.Event()
        decisions_by_thread = {}

        def decide_then_order_handlers(*args, **kwargs):
            decision = original_decide(*args, **kwargs)
            decisions_by_thread[threading.get_ident()] = decision
            both_decisions_loaded.wait(timeout=5)
            if not decision.notification_claimed:
                self.assertTrue(winner_finished.wait(timeout=5))
            return decision

        def handle(call):
            try:
                self.module.handle_lesson_review_callback(call)
            finally:
                decision = decisions_by_thread.get(threading.get_ident())
                if decision is not None and decision.notification_claimed:
                    winner_finished.set()
            return call, decisions_by_thread[threading.get_ident()]

        calls = (
            callback(f"mr:r:{review.request_id}", message_id=201),
            callback(f"mr:r:{review.request_id}", message_id=202),
        )
        calls[0].id = "concurrent-reject-1"
        calls[1].id = "concurrent-reject-2"
        with patch.object(
            self.module.storage,
            "decide_lesson_review_request",
            side_effect=decide_then_order_handlers,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(handle, calls))

        losing_call, losing_decision = next(
            item for item in results if not item[1].notification_claimed
        )
        self.assertFalse(losing_decision.changed)
        losing_edit = next(
            edit
            for edit in self.module.bot.edits
            if edit["message_id"] == losing_call.message.message_id
        )
        losing_answer = next(
            answer
            for answer in self.module.bot.callback_answers
            if answer["id"] == losing_call.id
        )

        self.assertIsNone(losing_edit["reply_markup"])
        self.assertNotIn("пока не уведомлён", losing_edit["text"])
        self.assertNotIn("уже отправляется", losing_edit["text"])
        self.assertEqual(losing_answer["text"], "Заявка уже отклонена.")
        self.assertFalse(losing_answer["show_alert"])
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertEqual(len(rejection_notices), 1)

    def test_failed_rejection_notice_is_retried(self):
        review = self.submit(7)
        reject = callback(f"mr:r:{review.request_id}")
        self.module.bot.fail_message_chat_ids.add(self.user_id)

        self.module.handle_lesson_review_callback(reject)
        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.REJECTED)
        self.assertIsNone(stored.user_notified_at)

        self.module.bot.fail_message_chat_ids.clear()
        self.module.handle_lesson_review_callback(reject)

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertIsNotNone(stored.user_notified_at)
        self.assertEqual(len(notices), 1)

    def test_failed_rejection_notice_keeps_retry_button_until_handler_succeeds(self):
        review = self.submit(7)
        self.module.bot.fail_message_chat_ids.add(self.user_id)

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{review.request_id}")
        )

        failed_edit = self.module.bot.edits[-1]
        retry_callbacks = self.markup_callbacks(failed_edit["reply_markup"])
        self.assertEqual(retry_callbacks, {f"mr:r:{review.request_id}"})
        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.REJECTED)
        self.assertIsNone(stored.user_notified_at)

        self.module.bot.fail_message_chat_ids.clear()
        self.module.handle_lesson_review_callback(
            callback(retry_callbacks.pop(), message_id=2)
        )

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertIsNotNone(stored.user_notified_at)
        self.assertEqual(len(notices), 1)
        self.assertIsNone(self.module.bot.edits[-1]["reply_markup"])

    def test_rejection_notification_recovers_after_crashed_claim_lease_expires(self):
        review = self.submit(7)
        clock = [self.module.storage.clock_ms()]
        self.module.storage.clock_ms = lambda: clock[0]
        crashed_token = "crashed-notification-worker"
        decision = self.module.storage.decide_lesson_review_request(
            review.request_id,
            LessonReviewStatus.REJECTED,
            ADMIN_ID,
            notification_token=crashed_token,
            notification_lease_ms=1_000,
        )
        self.assertTrue(decision.notification_claimed)

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{review.request_id}", message_id=501)
        )

        waiting_edit = self.module.bot.edits[-1]
        retry_callbacks = self.markup_callbacks(waiting_edit["reply_markup"])
        self.assertEqual(retry_callbacks, {f"mr:r:{review.request_id}"})
        self.assertIn("обрабатывается", waiting_edit["text"])
        self.assertIn("повторите", waiting_edit["text"])
        self.assertIsNone(
            self.module.storage.get_lesson_review_request(
                review.request_id
            ).user_notified_at
        )
        self.assertFalse(
            any(
                message["chat_id"] == self.user_id
                and "не прошла" in message["text"]
                for message in self.module.bot.messages
            )
        )

        clock[0] += 1_001
        self.module.handle_lesson_review_callback(
            callback(retry_callbacks.pop(), message_id=502)
        )

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertIsNotNone(stored.user_notified_at)
        self.assertEqual(len(rejection_notices), 1)
        self.assertIsNone(self.module.bot.edits[-1]["reply_markup"])

    def test_stale_opposite_buttons_normalize_to_current_terminal_or_retry_ui(self):
        approved = self.submit(3)
        self.module.storage.decide_lesson_review_request(
            approved.request_id,
            LessonReviewStatus.APPROVED,
            ADMIN_ID,
        )

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{approved.request_id}", message_id=601)
        )

        approved_edit = self.module.bot.edits[-1]
        self.assertIn("уже одобрена", approved_edit["text"])
        self.assertEqual(
            self.markup_callbacks(approved_edit["reply_markup"]),
            {f"mr:a:{approved.request_id}"},
        )
        self.assertEqual(
            self.module.storage.get_lesson_review_request(approved.request_id).status,
            LessonReviewStatus.APPROVED,
        )

        rejected = self.submit(7)
        self.module.storage.decide_lesson_review_request(
            rejected.request_id,
            LessonReviewStatus.REJECTED,
            ADMIN_ID,
        )

        self.module.handle_lesson_review_callback(
            callback(f"mr:a:{rejected.request_id}", message_id=602)
        )

        retry_edit = self.module.bot.edits[-1]
        self.assertIn("уже отклонена", retry_edit["text"])
        self.assertIn("пока не уведомлён", retry_edit["text"])
        self.assertEqual(
            self.markup_callbacks(retry_edit["reply_markup"]),
            {f"mr:r:{rejected.request_id}"},
        )

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{rejected.request_id}", message_id=603)
        )
        self.assertIsNotNone(
            self.module.storage.get_lesson_review_request(
                rejected.request_id
            ).user_notified_at
        )
        self.module.handle_lesson_review_callback(
            callback(f"mr:a:{rejected.request_id}", message_id=604)
        )

        terminal_edit = self.module.bot.edits[-1]
        self.assertIn("уже отклонена", terminal_edit["text"])
        self.assertNotIn("пока не уведомлён", terminal_edit["text"])
        self.assertIsNone(terminal_edit["reply_markup"])
        self.assertEqual(
            self.module.storage.get_lesson_review_request(rejected.request_id).status,
            LessonReviewStatus.REJECTED,
        )

    def test_reject_reconciles_pending_request_when_lesson_was_already_issued(self):
        review = self.submit(7)
        self.assertTrue(self.module.issue_lesson_once(self.user_id, 7))
        delivered_documents = list(self.module.bot.documents)

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{review.request_id}")
        )

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertEqual(stored.status, LessonReviewStatus.FULFILLED)
        self.assertIsNone(stored.user_notified_at)
        self.assertEqual(rejection_notices, [])
        self.assertEqual(self.module.bot.documents, delivered_documents)
        self.assertTrue(self.module.bot.callback_answers[-1]["show_alert"])
        self.assertIn("уже выдана", self.module.bot.callback_answers[-1]["text"])

    def test_old_unnotified_rejection_stays_rejected_after_newer_fulfillment(self):
        old_review = self.submit(7)
        self.module.bot.fail_message_chat_ids.add(self.user_id)
        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{old_review.request_id}")
        )
        old_stored = self.module.storage.get_lesson_review_request(
            old_review.request_id
        )
        self.assertEqual(old_stored.status, LessonReviewStatus.REJECTED)
        self.assertIsNone(old_stored.user_notified_at)

        self.module.bot.fail_message_chat_ids.clear()
        newer_review = self.submit(7)
        self.module.handle_lesson_review_callback(
            callback(f"mr:a:{newer_review.request_id}", message_id=301)
        )
        self.assertEqual(
            self.module.storage.get_lesson_review_request(newer_review.request_id).status,
            LessonReviewStatus.FULFILLED,
        )

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{old_review.request_id}", message_id=302)
        )

        old_stored = self.module.storage.get_lesson_review_request(
            old_review.request_id
        )
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertEqual(old_stored.status, LessonReviewStatus.REJECTED)
        self.assertIsNone(old_stored.user_notified_at)
        self.assertEqual(rejection_notices, [])
        self.assertIsNone(self.module.bot.edits[-1]["reply_markup"])
        self.assertIn("позже выдана", self.module.bot.edits[-1]["text"])

    def test_reject_on_fulfilled_review_clears_stale_buttons(self):
        review = self.submit(7)
        self.module.handle_lesson_review_callback(
            callback(f"mr:a:{review.request_id}")
        )
        self.assertEqual(
            self.module.storage.get_lesson_review_request(review.request_id).status,
            LessonReviewStatus.FULFILLED,
        )
        self.module.bot.edits.clear()

        self.module.handle_lesson_review_callback(
            callback(f"mr:r:{review.request_id}", message_id=401)
        )

        self.assertEqual(len(self.module.bot.edits), 1)
        edit = self.module.bot.edits[0]
        self.assertEqual(edit["message_id"], 401)
        self.assertIsNone(edit["reply_markup"])
        self.assertIn("уже выдана", edit["text"])
        rejection_notices = [
            message
            for message in self.module.bot.messages
            if message["chat_id"] == self.user_id and "не прошла" in message["text"]
        ]
        self.assertEqual(rejection_notices, [])

    def test_malformed_and_unknown_callbacks_do_not_mutate_requests(self):
        review = self.submit()

        for data in ("mr:a:0", "mr:x:1", "mr:a:99999999999999999999", "mr:a:999"):
            self.module.handle_lesson_review_callback(callback(data))

        stored = self.module.storage.get_lesson_review_request(review.request_id)
        self.assertEqual(stored.status, LessonReviewStatus.PENDING)
        self.assertEqual(self.module.bot.documents, [])


if __name__ == "__main__":
    unittest.main()
