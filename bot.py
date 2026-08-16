import os
import io
import time
import random
import logging
import re
import secrets
import sqlite3
import threading
import hashlib
from pathlib import Path
import telebot
from telebot import types
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont

from eligibility import (
    ACTIVITY_PERIOD_MS,
    ActivityState,
    EligibilityStatus,
    evaluate_lesson,
    lesson_requires_referral_data,
)
from mexc_client import MexcClient, MexcClientError, MexcConfigurationError
from referrals import build_referral_link, parse_referral_payload
from storage import (
    LessonAlreadyIssuedError,
    LessonDeliveryInProgressError,
    LessonDeliveryClaimStatus,
    LessonReviewStatus,
    MexcUidAlreadyBoundError,
    ReferralAssignment,
    StorageError,
    UserMexcUidConflictError,
    create_storage_from_env,
)


LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


def log_telegram_error(context, exception):
    """Log Telegram failures without exposing request URLs or bot tokens."""
    error_code = getattr(exception, 'error_code', None)
    description = getattr(exception, 'description', None)
    if description:
        logger.error(
            "%s type=%s code=%s description=%s",
            context,
            type(exception).__name__,
            error_code,
            str(description)[:200],
        )
    else:
        logger.error("%s type=%s", context, type(exception).__name__)


class SafeBotExceptionHandler:
    """Make exceptions from TeleBot worker threads visible in Render logs."""

    def handle(self, exception):
        log_telegram_error("Telegram handler failed", exception)
        return True

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# Process webhook updates inside the Gunicorn request. TeleBot's own worker
# queue can acknowledge an update before its handler has actually run.
# Gunicorn already provides request-level concurrency for this service.
bot = telebot.TeleBot(
    TOKEN,
    threaded=False,
    exception_handler=SafeBotExceptionHandler(),
)
app = Flask(__name__)

# --- НАСТРОЙКИ ---
CHANNEL_USERNAME = "tradegrowthh"
DEFAULT_ADMIN_TELEGRAM_ID = 1042857576
MAX_POSTGRES_BIGINT = 9_223_372_036_854_775_807
LESSON_DELIVERY_LEASE_MS = 10 * 60 * 1000
LESSON_REVIEW_NOTIFICATION_LEASE_MS = 2 * 60 * 1000
REVIEW_DELIVERY_DELIVERED = "delivered"
REVIEW_DELIVERY_BUSY = "busy"
REVIEW_DELIVERY_FAILED = "failed"


def load_admin_telegram_ids():
    raw_ids = os.environ.get("ADMIN_TELEGRAM_IDS")
    if raw_ids is None:
        raw_ids = os.environ.get("ADMIN_TELEGRAM_ID")
    if raw_ids is None:
        return frozenset({DEFAULT_ADMIN_TELEGRAM_ID})

    try:
        parsed = frozenset(
            int(value.strip())
            for value in raw_ids.split(",")
            if value.strip()
        )
    except ValueError as exc:
        raise ValueError(
            "ADMIN_TELEGRAM_IDS contains a non-numeric Telegram ID"
        ) from exc
    if not parsed or any(
        admin_id <= 0 or admin_id > MAX_POSTGRES_BIGINT for admin_id in parsed
    ):
        raise ValueError(
            "ADMIN_TELEGRAM_IDS must contain positive signed 64-bit Telegram IDs"
        )
    return parsed


ADMIN_TELEGRAM_IDS = load_admin_telegram_ids()
MANUAL_REVIEW_CALLBACK = re.compile(r"^mr:([ar]):([1-9][0-9]{0,18})$")
# Never put BOT_TOKEN into a public URL: Flask/Render access logs include the
# request path.  The path remains stable for this bot but cannot be reversed
# to obtain the Telegram token.
WEBHOOK_PATH = "telegram-" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:32]
storage = create_storage_from_env()
logger.info("Persistent state initialized backend=%s", storage.backend_name)
if os.environ.get('RENDER_EXTERNAL_HOSTNAME') and storage.backend_name == "sqlite":
    logger.warning(
        "Render is using ephemeral SQLite; configure SUPABASE_DATABASE_URL "
        "for persistent state"
    )

# --- ФАЙЛЫ МЕТОДИЧЕК ---
LESSON_FILES = {
    1: {"main": "Фундаментальныеосновы.pdf", "bonus": None},
    2: {"main": "2_урок.pdf", "bonus": "Дополнительно к 2 уроку.pdf"},
    3: {"main": "3_урок.pdf", "bonus": "Дополнительно к 3 уроку.pdf"},
    4: {"main": "4_урок.pdf", "bonus": None},
    5: {"main": "5_урок.pdf", "bonus": "Дополнительно к 5 уроку.pdf"},
    6: {"main": "6_урок.pdf", "bonus": None},
    7: {"main": "7_урок.pdf", "bonus": "Дополнительно к 7 уроку.pdf"},
}

# --- ИНИЦИАЛИЗАЦИЯ MEXC API И КЭША ---
try:
    mexc = MexcClient.from_env()
except MexcConfigurationError:
    mexc = None
    logger.error("MEXC credentials are invalid")

if mexc is None:
    logger.warning("MEXC credentials are not configured; verifiable lessons are disabled")
else:
    logger.info("MEXC client configured; access will be checked on the first request")

MEXC_CACHE_TTL_SECONDS = 30
MEXC_CACHE_MAX_ENTRIES = 256
_mexc_cache = {}
_mexc_cache_lock = threading.Lock()
_bot_username = os.environ.get('BOT_USERNAME', '').strip().lstrip('@') or None
_bot_username_lock = threading.Lock()


# Постоянное меню видно внизу диалога после первого прохождения капчи. Оно
# заменяет необходимость искать и вводить команды вручную.
MENU_LESSON_BUTTONS = {
    "📘 Методичка №1": 1,
    "📘 Методичка №2": 2,
    "📘 Методичка №3": 3,
    "📘 Методичка №4": 4,
    "📘 Методичка №5": 5,
    "📘 Методичка №6": 6,
    "📘 Методичка №7": 7,
}
MENU_REFERRAL_BUTTON = "👥 Моя ссылка и друзья"


def build_main_menu():
    """Return a compact persistent keyboard for the course flow."""

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton("📘 Методичка №1"),
        types.KeyboardButton("📘 Методичка №2"),
    )
    markup.row(
        types.KeyboardButton("📘 Методичка №3"),
        types.KeyboardButton("📘 Методичка №4"),
    )
    markup.row(
        types.KeyboardButton("📘 Методичка №5"),
        types.KeyboardButton("📘 Методичка №6"),
    )
    markup.row(
        types.KeyboardButton("📘 Методичка №7"),
        types.KeyboardButton(MENU_REFERRAL_BUTTON),
    )
    return markup


def send_main_menu(chat_id, text="Выберите методичку или откройте свою реферальную ссылку:"):
    bot.send_message(chat_id, text, reply_markup=build_main_menu())


def get_referral_cached(uid, *, force_refresh=False):
    """Avoid duplicate MEXC lookups for the same UID within this process."""
    if mexc is None:
        raise MexcConfigurationError("MEXC client is not configured")

    now = time.monotonic()
    with _mexc_cache_lock:
        cached = _mexc_cache.get(uid)
        if not force_refresh and cached and cached[0] > now:
            return cached[1]

        expired_keys = [key for key, value in _mexc_cache.items() if value[0] <= now]
        for key in expired_keys:
            _mexc_cache.pop(key, None)

    referral = mexc.get_rebate_referral(uid)

    with _mexc_cache_lock:
        if len(_mexc_cache) >= MEXC_CACHE_MAX_ENTRIES:
            _mexc_cache.pop(next(iter(_mexc_cache)))
        _mexc_cache[uid] = (now + MEXC_CACHE_TTL_SECONDS, referral)
    return referral


def get_bot_username():
    """Resolve the public bot username without logging Telegram credentials."""
    global _bot_username
    with _bot_username_lock:
        if _bot_username is None:
            _bot_username = bot.get_me().username
        return _bot_username

# --- КАПЧА ---
captcha_data = {}

def generate_captcha_text(length=5):
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(random.choices(chars, k=length))

def draw_captcha(text):
    W, H = 400, 130
    img = Image.new('RGB', (W, H), color=(245, 245, 245))
    draw = ImageDraw.Draw(img)

    for _ in range(random.randint(100, 150)):
        x = random.randint(0, W-1)
        y = random.randint(0, H-1)
        draw.point((x, y), fill=(random.randint(180, 220), random.randint(180, 220), random.randint(180, 220)))

    for _ in range(random.randint(1, 2)):
        x1 = random.randint(0, W)
        y1 = random.randint(0, H)
        x2 = random.randint(0, W)
        y2 = random.randint(0, H)
        draw.line([(x1, y1), (x2, y2)], fill=(random.randint(200, 230), random.randint(200, 230), random.randint(200, 230)), width=2)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
    except:
        try:
            font = ImageFont.truetype("arial.ttf", 60)
        except:
            font = ImageFont.load_default()

    x_offset = 25
    for ch in text:
        txt_img = Image.new('RGBA', (80, 100), (0, 0, 0, 0))
        txt_draw = ImageDraw.Draw(txt_img)
        txt_draw.text((5, 5), ch, font=font, fill=(10, 10, 10))
        angle = random.randint(-15, 15)
        rotated = txt_img.rotate(angle, expand=1, resample=Image.BICUBIC)
        img.paste(rotated, (x_offset, random.randint(20, 35)), rotated)
        x_offset += 55 + random.randint(5, 10)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

def send_captcha(chat_id, attempts):
    text = generate_captcha_text()
    image_buffer = draw_captcha(text)
    try:
        bot.send_photo(
            chat_id,
            types.InputFile(image_buffer, file_name="captcha.png"),
            caption="Введите символы с картинки (заглавные буквы/цифры):",
            timeout=20,
        )
    finally:
        image_buffer.close()
    captcha_data[chat_id] = {
        "answer": text,
        "attempts": attempts,
        "blocked_until": 0
    }

def check_captcha(user_id, user_text):
    if user_id not in captcha_data:
        return False
    data = captcha_data[user_id]
    if data["blocked_until"] > 0:
        if time.time() < data["blocked_until"]:
            remaining = int(data["blocked_until"] - time.time())
            bot.send_message(user_id, f"⏳ Вы исчерпали попытки. Попробуйте через {remaining} сек.")
            return False
        else:
            del captcha_data[user_id]
            return False

    if user_text.strip().upper() == data["answer"].upper():
        del captcha_data[user_id]
        return True
    else:
        data["attempts"] += 1
        if data["attempts"] >= 5:
            data["blocked_until"] = time.time() + 300
            bot.send_message(user_id, "❌ 5 неверных попыток. Доступ заблокирован на 5 минут.")
            return False
        else:
            remaining = 5 - data["attempts"]
            bot.send_message(user_id, f"❌ Неверно. Осталось попыток: {remaining}")
            send_captcha(user_id, data["attempts"])
            return False

# --- ВЕБХУКИ ---
def configure_render_webhook():
    render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
    if not render_host:
        return False

    webhook_url = f'https://{render_host}/{WEBHOOK_PATH}'
    try:
        # setWebhook replaces the previous URL atomically. Removing it first
        # creates a delivery gap where Telegram updates can be missed.
        bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query", "my_chat_member"],
            drop_pending_updates=False,
        )
        logger.info("Telegram webhook configured")
        return True
    except Exception:
        # Telegram exceptions can contain request URLs; do not log their raw text.
        logger.error("Telegram webhook configuration failed")
        return False


@app.route('/' + WEBHOOK_PATH, methods=['POST'])
def getMessage():
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            logger.warning("Telegram webhook received invalid JSON")
            return "Bad request", 400
        update = telebot.types.Update.de_json(payload)
        update_type = next(
            (
                name
                for name in (
                    "message",
                    "callback_query",
                    "my_chat_member",
                    "chat_member",
                )
                if getattr(update, name, None) is not None
            ),
            "other",
        )
        logger.info("Telegram update received type=%s", update_type)
        bot.process_new_updates([update])
        logger.info("Telegram update processed type=%s", update_type)
    except Exception as exc:
        # Do not include exception text: transport/database exceptions may
        # contain request URLs or credentials.
        logger.error(
            "Telegram update processing failed type=%s",
            type(exc).__name__,
        )
        return "Temporary failure", 500
    return "!", 200

@app.route('/')
def webhook():
    if not os.environ.get('RENDER_EXTERNAL_HOSTNAME'):
        return "RENDER_EXTERNAL_HOSTNAME not set.", 500
    # Render health checks must be read-only. Reconfiguring the Telegram
    # webhook on every GET can briefly interrupt update delivery.
    return "Bot is running", 200

# --- ТЕКСТЫ ---
WELCOME_TEXT = (
    "Приветствую!\n"
    "Наша команда дает возможность обучиться криптотрейдингу бесплатно. "
    "Никакой воды, только рабочие инструменты. "
    "Жми кнопку и успевай забрать знания бесплатно."
)
SUBSCRIBE_TEXT = "Подпишись на канал и получи методичку 👇"

# --- ТЕКСТЫ ПОСЛЕ МЕТОДИЧЕК ---
def get_after_lesson_text(lesson_number):
    if lesson_number == 1:
        return (
            "Поздравляю! Ты сделал первый шаг!\n\n"
            "Теперь у тебя есть база. Дальше — реальный трейдинг.\n\n"
            "Следующая методичка:\n\n"
            "📘 Методичка №2 — Уровни, Фибоначчи, OI, EMA, RSI\n"
            "✅ Условие: Зарегистрироваться по реферальной ссылке и совершить сделку\n\n"
            "---\n\n"
            "Как получить следующую методичку (№2):\n\n"
            "1. Зарегистрируйся по реферальной ссылке и соверши первую сделку\n"
            "2. Нажми кнопку «📘 Методичка №2» в меню внизу\n"
            "3. Бот попросит ввести твой UID (цифры из профиля MEXC)\n"
            "4. Введи UID — и получишь второй урок!\n"
            "(UID можно найти в профиле MEXC — это число из 8–10 цифр)\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 2:
        return (
            "Отлично! Ты получил вторую методичку!\n\n"
            "Ты уже научился работать с уровнями и индикаторами. Время двигаться дальше.\n\n"
            "Следующая методичка:\n\n"
            "📘 Методичка №3 — Price Action, инсайд-бары, кластерный анализ\n"
            "✅ Условие: Общий объём сделок от 300 USDT\n\n"
            "---\n\n"
            "Как получить следующую методичку (№3):\n\n"
            "1. Продолжай торговать, пока общий объём не достигнет 300 USDT\n"
            "(Объём считается с учётом плеча. Пример: сделка на 100 USDT с плечом ×3 даёт объём 300 USDT. Всего одна такая сделка — и условие выполнено!)\n"
            "2. Нажми кнопку «📘 Методичка №3» и введи свой UID\n"
            "3. Бот проверит реферальную привязку, а торговый объём подтверди "
            "у администратора\n"
            "4. После ручной проверки администратор выдаст третий урок\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 3:
        return (
            "Красава! Ты освоил Price Action!\n\n"
            "📘 Методичка №4 выдаётся за 1 квалифицированного приглашённого.\n\n"
            "Нажми «👥 Моя ссылка и друзья» в меню внизу.\n"
            "Друг должен перейти по ней, привязать свой MEXC UID и совершить сделку.\n"
            "После этого нажми «📘 Методичка №4».\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 4:
        return (
            "Ты лидер! Ты привёл первого друга!\n\n"
            "📘 Методичка №5 — подтверждение сохранения торговой активности.\n\n"
            "После первой подтверждённой проверки MEXC начинается отсчёт 30 дней. "
            "По истечении срока нажми «📘 Методичка №5». Бот повторно запросит MEXC "
            "и проверит официальное время последней сделки.\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 5:
        return (
            "Торговая активность спустя 30 дней подтверждена!\n\n"
            "Теперь ты готов к пониманию структуры рынка.\n\n"
            "📘 Методичка №6 — Структура рынка, накопление/распределение, 90% Value Area\n"
            "✅ Условие: 2 квалифицированных приглашённых через персональную ссылку.\n"
            "Ссылка и текущий счётчик — кнопка «👥 Моя ссылка и друзья».\n"
            "Проверка методички — кнопка «📘 Методичка №6».\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 6:
        return (
            "Огонь! У тебя уже 2 друга в команде!\n\n"
            "Остался последний рывок — самый мощный урок.\n\n"
            "Следующая (финальная) методичка:\n\n"
            "📘 Методичка №7 — Фундаментальный анализ 2.0 (токеномика, тренды, оценка проектов)\n"
            "✅ Условие: Объём от 5 000 USDT или привести 3 друзей\n\n"
            "---\n\n"
            "Как получить финальную методичку (№7):\n\n"
            "1. Наторгуй на объём 5 000 USDT ИЛИ приведи третьего друга\n"
            "(Объём считается с учётом плеча. Пример: сделка на 100 USDT с плечом ×3 даёт 300 USDT. Для 5 000 USDT нужно около 17 таких сделок. Реально за пару недель!)\n"
            "(Свою персональную Telegram-ссылку можно получить кнопкой «👥 Моя ссылка и друзья»)\n"
            "2. Нажми кнопку «📘 Методичка №7»\n"
            "3. Ветку с тремя приглашёнными бот проверяет автоматически; объём — через администратора.\n\n"
            "Это финиш! Ты почти у цели! 🚀"
        )
    elif lesson_number == 7:
        return (
            "ПОЗДРАВЛЯЮ! ТЫ ПРОШЁЛ ВЕСЬ КУРС!\n\n"
            "Ты прошёл путь от новичка до полноценного трейдера, который умеет:\n"
            "- Анализировать рынок\n"
            "- Читать объёмы\n"
            "- Видеть паттерны\n"
            "- Оценивать проекты\n\n"
            "Теперь ты — самостоятельный трейдер. Дальше только практика и твой личный рост!\n\n"
            "Если хочешь оставаться в курсе новых материалов — следи за каналом.\n"
            "По всем вопросам пиши админу.\n\n"
            "Удачи в больших сделках! 🔥💎"
        )
    return ""

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

# --- ОТПРАВКА МЕТОДИЧКИ ---
def send_lesson_part(
    chat_id,
    lesson_number,
    part,
    file_name,
    caption,
    delivery_token,
):
    if storage.is_lesson_part_delivered(chat_id, lesson_number, part):
        return True

    if not storage.renew_lesson_delivery(
        chat_id,
        lesson_number,
        delivery_token,
        LESSON_DELIVERY_LEASE_MS,
    ):
        return False

    try:
        with (BASE_DIR / file_name).open('rb') as file_object:
            bot.send_document(chat_id, file_object, caption=caption, timeout=120)
    except FileNotFoundError:
        bot.send_message(chat_id, f"❌ Файл {file_name} не найден.")
        return False

    return storage.mark_claimed_lesson_part_delivered(
        chat_id,
        lesson_number,
        part,
        delivery_token,
    )


def send_lesson(chat_id, lesson_number, delivery_token):
    files = LESSON_FILES.get(lesson_number)
    if not files:
        bot.send_message(chat_id, "❌ Методичка не найдена.")
        return False

    main_file = files.get("main")
    bonus_file = files.get("bonus")

    if not send_lesson_part(
        chat_id,
        lesson_number,
        "main",
        main_file,
        f"📘 Методичка №{lesson_number}",
        delivery_token,
    ):
        return False

    if bonus_file and not send_lesson_part(
        chat_id,
        lesson_number,
        "bonus",
        bonus_file,
        "🎁 Бонус! Дополнительный материал к уроку.",
        delivery_token,
    ):
        return False

    after_text = get_after_lesson_text(lesson_number)
    if after_text:
        try:
            bot.send_message(
                chat_id,
                after_text,
                reply_markup=build_main_menu(),
                timeout=30,
            )
        except Exception as exc:
            # Both PDFs are checkpointed already. A transient failure of the
            # follow-up text must not cause them to be sent again.
            log_telegram_error("Post-lesson guidance delivery failed", exc)
    return True


def issue_lesson_once(chat_id, lesson_number):
    """Atomically prevent repeated delivery of lessons 1-7."""
    delivery_token = secrets.token_urlsafe(24)
    try:
        claim_status = storage.claim_lesson_delivery(
            chat_id,
            lesson_number,
            delivery_token,
            LESSON_DELIVERY_LEASE_MS,
        )
    except (StorageError, sqlite3.Error):
        logger.error("Lesson delivery state is unavailable lesson=%s", lesson_number)
        bot.send_message(chat_id, "⚠️ Хранилище временно недоступно. Попробуйте позже.")
        return False

    if claim_status is LessonDeliveryClaimStatus.ALREADY_ISSUED:
        bot.send_message(chat_id, f"ℹ️ Методичка №{lesson_number} уже была вам выдана.")
        return False
    if claim_status is LessonDeliveryClaimStatus.BUSY:
        bot.send_message(chat_id, "⏳ Методичка уже отправляется. Дождитесь завершения.")
        return False

    try:
        delivered = send_lesson(chat_id, lesson_number, delivery_token)
    except Exception:
        try:
            storage.release_lesson_delivery(
                chat_id,
                lesson_number,
                delivery_token,
            )
        except (StorageError, sqlite3.Error):
            logger.error("Failed to release lesson delivery claim lesson=%s", lesson_number)
        logger.error("Telegram lesson delivery failed lesson=%s", lesson_number)
        bot.send_message(chat_id, "⚠️ Не удалось отправить файл. Попробуйте позже.")
        return False

    if not delivered:
        try:
            storage.release_lesson_delivery(
                chat_id,
                lesson_number,
                delivery_token,
            )
        except (StorageError, sqlite3.Error):
            logger.error("Failed to release lesson delivery claim lesson=%s", lesson_number)
        return False

    try:
        completed = storage.complete_lesson_delivery(
            chat_id,
            lesson_number,
            delivery_token,
        )
    except (StorageError, sqlite3.Error):
        completed = False
        logger.error("Lesson delivery completion could not be persisted lesson=%s", lesson_number)
    if not completed:
        try:
            storage.release_lesson_delivery(
                chat_id,
                lesson_number,
                delivery_token,
            )
        except (StorageError, sqlite3.Error):
            logger.error("Failed to release incomplete lesson delivery lesson=%s", lesson_number)
        bot.send_message(
            chat_id,
            "⚠️ Файлы отправлены, но состояние не сохранилось. "
            "Повторите проверку позже.",
        )
        return False
    return True


def build_lesson_review_markup(request_id, *, retry_only=False):
    markup = types.InlineKeyboardMarkup()
    approve_text = "🔁 Повторить отправку" if retry_only else "✅ Выдать"
    markup.add(
        types.InlineKeyboardButton(
            approve_text,
            callback_data=f"mr:a:{request_id}",
        )
    )
    if not retry_only:
        markup.add(
            types.InlineKeyboardButton(
                "❌ Отказать",
                callback_data=f"mr:r:{request_id}",
            )
        )
    return markup


def build_rejection_notification_retry_markup(request_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "🔁 Повторить уведомление",
            callback_data=f"mr:r:{request_id}",
        )
    )
    return markup


def notify_admins_of_lesson_review(review):
    threshold = 300 if review.lesson_number == 3 else 5000
    text = (
        f"🔎 Заявка №{review.request_id} на методичку "
        f"№{review.lesson_number}\n\n"
        f"Telegram ID: {review.telegram_id}\n"
        f"MEXC UID: {review.mexc_uid}\n"
        f"Нужно подтвердить объём от {threshold} USDT."
    )
    delivered = 0
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            bot.send_message(
                admin_id,
                text,
                reply_markup=build_lesson_review_markup(review.request_id),
            )
            delivered += 1
        except Exception as exc:
            log_telegram_error("Manual review notification failed", exc)
    return delivered


def release_review_notification(request_id, recipient, notification_token):
    try:
        storage.release_lesson_review_notification(
            request_id,
            recipient,
            notification_token,
        )
    except (StorageError, sqlite3.Error):
        logger.error(
            "Manual review notification claim could not be released recipient=%s",
            recipient,
        )


def mark_review_fulfilled(review, delivery_token):
    try:
        return storage.mark_lesson_review_fulfilled(
            review.request_id,
            delivery_token,
        )
    except (StorageError, sqlite3.Error):
        # The PDF has already been delivered. A repeated approve will reconcile
        # this state from issued_lessons without sending the files twice.
        logger.error(
            "Manual review fulfillment could not be persisted lesson=%s",
            review.lesson_number,
        )
        return False


def deliver_approved_lesson_review(review):
    delivery_token = secrets.token_urlsafe(24)
    try:
        claimed = storage.claim_lesson_review_delivery(
            review.request_id,
            delivery_token,
            LESSON_DELIVERY_LEASE_MS,
        )
    except (StorageError, sqlite3.Error):
        logger.error(
            "Manual review delivery claim is unavailable lesson=%s",
            review.lesson_number,
        )
        return REVIEW_DELIVERY_FAILED

    if not claimed:
        try:
            current = storage.get_lesson_review_request(review.request_id)
        except (StorageError, sqlite3.Error):
            return REVIEW_DELIVERY_FAILED
        if current and current.status is LessonReviewStatus.FULFILLED:
            return REVIEW_DELIVERY_DELIVERED
        return REVIEW_DELIVERY_BUSY

    fulfilled = False
    try:
        try:
            already_issued = storage.is_lesson_issued(
                review.telegram_id,
                review.lesson_number,
            )
        except (StorageError, sqlite3.Error):
            return REVIEW_DELIVERY_FAILED

        delivered = already_issued or issue_lesson_once(
            review.telegram_id,
            review.lesson_number,
        )
        if not delivered:
            try:
                current = storage.get_lesson_review_request(review.request_id)
                if current and current.status is LessonReviewStatus.FULFILLED:
                    fulfilled = True
                    return REVIEW_DELIVERY_DELIVERED
                delivery_busy = storage.is_lesson_delivery_in_progress(
                    review.telegram_id,
                    review.lesson_number,
                )
                if delivery_busy:
                    return REVIEW_DELIVERY_BUSY
                current = storage.get_lesson_review_request(review.request_id)
                if current and current.status is LessonReviewStatus.FULFILLED:
                    fulfilled = True
                    return REVIEW_DELIVERY_DELIVERED
            except (StorageError, sqlite3.Error):
                return REVIEW_DELIVERY_FAILED
            return REVIEW_DELIVERY_FAILED

        try:
            current = storage.get_lesson_review_request(review.request_id)
        except (StorageError, sqlite3.Error):
            current = None
        fulfilled = (
            current is not None
            and current.status is LessonReviewStatus.FULFILLED
        )
        if not fulfilled:
            fulfilled = mark_review_fulfilled(review, delivery_token)
        return (
            REVIEW_DELIVERY_DELIVERED
            if fulfilled
            else REVIEW_DELIVERY_FAILED
        )
    finally:
        if not fulfilled:
            try:
                storage.release_lesson_review_delivery(
                    review.request_id,
                    delivery_token,
                )
            except (StorageError, sqlite3.Error):
                logger.error(
                    "Manual review delivery claim could not be released lesson=%s",
                    review.lesson_number,
                )


def submit_lesson_review(user_id, lesson_number):
    try:
        submission = storage.request_lesson_review(user_id, lesson_number)
    except LessonAlreadyIssuedError:
        bot.send_message(user_id, f"ℹ️ Методичка №{lesson_number} уже была вам выдана.")
        return True
    except LessonDeliveryInProgressError:
        bot.send_message(
            user_id,
            f"⏳ Методичка №{lesson_number} уже отправляется. "
            "Дождитесь завершения.",
        )
        return True
    except (StorageError, sqlite3.Error):
        logger.error("Manual lesson review could not be persisted lesson=%s", lesson_number)
        bot.send_message(user_id, "⚠️ Не удалось создать заявку. Попробуйте позже.")
        return False

    review = submission.request
    if review.status is LessonReviewStatus.APPROVED:
        bot.send_message(
            user_id,
            f"✅ Заявка №{review.request_id} уже одобрена. "
            "Повторяю отправку методички.",
        )
        delivery = deliver_approved_lesson_review(review)
        if delivery == REVIEW_DELIVERY_BUSY:
            bot.send_message(user_id, "⏳ Отправка уже выполняется.")
        return delivery != REVIEW_DELIVERY_FAILED

    if review.admin_notified_at is None:
        notification_token = secrets.token_urlsafe(24)
        try:
            notification_claimed = storage.claim_lesson_review_notification(
                review.request_id,
                "admin",
                notification_token,
                LESSON_REVIEW_NOTIFICATION_LEASE_MS,
            )
        except (StorageError, sqlite3.Error):
            logger.error(
                "Manual review notification claim is unavailable lesson=%s",
                lesson_number,
            )
            bot.send_message(user_id, "⚠️ Не удалось уведомить администратора. Попробуйте позже.")
            return False

        if notification_claimed:
            notification_completed = False
            try:
                notified_count = notify_admins_of_lesson_review(review)
                if notified_count:
                    notification_completed = (
                        storage.complete_lesson_review_notification(
                            review.request_id,
                            "admin",
                            notification_token,
                        )
                    )
            except (StorageError, sqlite3.Error):
                logger.error(
                    "Manual review notification state could not be persisted lesson=%s",
                    lesson_number,
                )
            finally:
                if not notification_completed:
                    release_review_notification(
                        review.request_id,
                        "admin",
                        notification_token,
                    )

            if not notification_completed:
                bot.send_message(
                    user_id,
                    f"⚠️ Заявка №{review.request_id} сохранена, но администратора "
                    "не удалось надёжно уведомить. Нажмите кнопку методички ещё раз.",
                )
                return False
        else:
            try:
                current = storage.get_lesson_review_request(review.request_id)
            except (StorageError, sqlite3.Error):
                current = None
            if current is None:
                bot.send_message(user_id, "⚠️ Не удалось проверить статус заявки.")
                return False
            if current.status is LessonReviewStatus.FULFILLED:
                bot.send_message(
                    user_id,
                    f"ℹ️ Методичка №{lesson_number} уже была вам выдана.",
                )
                return True
            if current.status is LessonReviewStatus.REJECTED:
                bot.send_message(
                    user_id,
                    f"❌ Заявка №{current.request_id} уже отклонена. "
                    "После выполнения условия подайте новую.",
                )
                return False
            if current.status is LessonReviewStatus.APPROVED:
                bot.send_message(
                    user_id,
                    f"✅ Заявка №{current.request_id} уже одобрена; "
                    "методичка готовится к отправке.",
                )
                return True
            if current.admin_notified_at is None:
                bot.send_message(
                    user_id,
                    f"⏳ Заявка №{review.request_id} сохранена; "
                    "уведомление администратору уже отправляется.",
                )
                return True

    if submission.created:
        bot.send_message(
            user_id,
            f"✅ Заявка №{review.request_id} на методичку №{lesson_number} "
            "отправлена администратору. После проверки бот сам пришлёт PDF.",
        )
    else:
        bot.send_message(
            user_id,
            f"⏳ Заявка №{review.request_id} уже ожидает проверки администратором.",
        )
    return True


def answer_review_callback(call, text, *, show_alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=show_alert)
    except Exception as exc:
        log_telegram_error("Manual review callback answer failed", exc)


def edit_review_message(call, text, *, reply_markup=None):
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        # The database decision and user delivery must not depend on whether
        # Telegram still allows the old admin message to be edited.
        log_telegram_error("Manual review admin message edit failed", exc)

# --- ОБРАБОТЧИКИ ---
@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    logger.info("Telegram /start handler started")
    try:
        storage.ensure_user(user_id)
        inviter_id = parse_referral_payload(message.text)
        if inviter_id is not None:
            assignment = storage.assign_inviter(user_id, inviter_id)
            if assignment is ReferralAssignment.ASSIGNED:
                bot.send_message(user_id, "✅ Пригласивший пользователь зафиксирован.")
            elif assignment is ReferralAssignment.SELF_REFERRAL:
                bot.send_message(user_id, "ℹ️ Нельзя использовать собственную реферальную ссылку.")
            elif assignment is ReferralAssignment.ALREADY_ASSIGNED:
                bot.send_message(user_id, "ℹ️ Пригласивший уже был зафиксирован ранее и не изменён.")
    except (StorageError, sqlite3.Error):
        logger.error("Telegram referral state could not be persisted")
        bot.send_message(
            user_id,
            "⚠️ Реферальная система временно недоступна. Обычный запуск продолжен."
        )

    captcha_data.pop(user_id, None)
    try:
        send_captcha(user_id, 0)
    except Exception as exc:
        log_telegram_error("Telegram captcha delivery failed", exc)
        try:
            bot.send_message(
                user_id,
                "⚠️ Не удалось отправить капчу. Попробуйте ещё раз через минуту.",
            )
        except Exception as fallback_exc:
            log_telegram_error("Telegram fallback message failed", fallback_exc)
        return
    logger.info("Telegram captcha sent")


@bot.message_handler(commands=['referral', 'my_referral'])
def referral_handler(message):
    user_id = message.from_user.id
    try:
        storage.ensure_user(user_id)
        referral_link = build_referral_link(get_bot_username(), user_id)
        qualified_count = storage.count_qualified_invites(user_id)
    except (StorageError, sqlite3.Error):
        logger.error("Telegram referral storage is unavailable")
        bot.send_message(user_id, "⚠️ Реферальная система временно недоступна.")
        return
    except Exception:
        logger.error("Telegram bot username lookup failed")
        bot.send_message(
            user_id,
            "⚠️ Не удалось сформировать реферальную ссылку. Попробуйте позже."
        )
        return

    bot.send_message(
        user_id,
        "Ваша персональная ссылка:\n"
        f"{referral_link}\n\n"
        f"Квалифицированных приглашённых: {qualified_count}\n\n"
        "Приглашённый засчитывается после привязки своего MEXC UID "
        "и сделки, по которой MEXC начислил комиссию."
    )


@bot.message_handler(commands=['menu'])
def menu_handler(message):
    send_main_menu(message.from_user.id)

@bot.message_handler(func=lambda msg: msg.from_user.id in captcha_data and not msg.text.startswith('/'))
def captcha_input(message):
    user_id = message.from_user.id
    if check_captcha(user_id, message.text):
        markup = types.InlineKeyboardMarkup()
        btn_get = types.InlineKeyboardButton("📥 Забрать методичку", callback_data='request_pdf')
        markup.add(btn_get)
        bot.send_message(user_id, WELCOME_TEXT, reply_markup=markup)
        send_main_menu(
            user_id,
            "Готово! Кнопки методичек теперь всегда доступны внизу диалога.",
        )


def build_subscription_markup():
    markup = types.InlineKeyboardMarkup()
    btn_sub = types.InlineKeyboardButton("🔔 Подписаться на канал", url=f'https://t.me/{CHANNEL_USERNAME}')
    btn_check = types.InlineKeyboardButton("✅ Проверить подписку", callback_data='check_sub')
    markup.add(btn_sub, btn_check)
    return markup


def show_lesson1_subscription_prompt(chat_id):
    bot.send_message(chat_id, SUBSCRIBE_TEXT, reply_markup=build_subscription_markup())

@bot.callback_query_handler(func=lambda call: call.data == 'request_pdf')
def handle_request_pdf(call):
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=SUBSCRIBE_TEXT,
        reply_markup=build_subscription_markup()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'check_sub')
def handle_check_sub(call):
    user_id = call.from_user.id
    if is_subscribed(user_id):
        delivered = issue_lesson_once(user_id, 1)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(
            call.id,
            "Методичка отправлена!" if delivered else "Проверка завершена."
        )
    else:
        bot.answer_callback_query(call.id, "❌ Вы ещё не подписались на канал.", show_alert=True)


@bot.callback_query_handler(
    func=lambda call: (getattr(call, 'data', '') or '').startswith('mr:')
)
def handle_lesson_review_callback(call):
    match = MANUAL_REVIEW_CALLBACK.fullmatch(getattr(call, 'data', '') or '')
    if match is None:
        answer_review_callback(call, "❌ Неверная кнопка.", show_alert=True)
        return

    request_id = int(match.group(2))
    if request_id > MAX_POSTGRES_BIGINT:
        answer_review_callback(call, "❌ Неверный номер заявки.", show_alert=True)
        return

    admin_id = getattr(getattr(call, 'from_user', None), 'id', None)
    admin_chat_id = getattr(
        getattr(getattr(call, 'message', None), 'chat', None),
        'id',
        None,
    )
    if admin_id not in ADMIN_TELEGRAM_IDS or admin_chat_id != admin_id:
        answer_review_callback(call, "❌ Нет прав на это действие.", show_alert=True)
        return

    requested_status = (
        LessonReviewStatus.APPROVED
        if match.group(1) == 'a'
        else LessonReviewStatus.REJECTED
    )
    rejection_notification_token = (
        secrets.token_urlsafe(24)
        if requested_status is LessonReviewStatus.REJECTED
        else None
    )
    try:
        decision = storage.decide_lesson_review_request(
            request_id,
            requested_status,
            admin_id,
            notification_token=rejection_notification_token,
            notification_lease_ms=(
                LESSON_REVIEW_NOTIFICATION_LEASE_MS
                if rejection_notification_token is not None
                else 0
            ),
        )
    except (StorageError, sqlite3.Error):
        logger.error("Manual review decision could not be persisted")
        answer_review_callback(
            call,
            "⚠️ Хранилище недоступно. Попробуйте позже.",
            show_alert=True,
        )
        return

    review = decision.request
    if review is None:
        answer_review_callback(call, "❌ Заявка не найдена.", show_alert=True)
        return

    if decision.delivery_in_progress:
        answer_review_callback(
            call,
            "⏳ Отправка методички уже выполняется автоматически. "
            "Повторите позже.",
            show_alert=True,
        )
        return

    if decision.lesson_already_issued:
        edit_review_message(
            call,
            f"ℹ️ Заявка №{review.request_id} была отклонена, но методичка "
            f"№{review.lesson_number} позже выдана автоматически.",
        )
        answer_review_callback(
            call,
            "Методичка уже выдана; уведомление об отказе не отправлено.",
            show_alert=True,
        )
        return

    if review.status is LessonReviewStatus.FULFILLED:
        if requested_status is LessonReviewStatus.APPROVED:
            edit_review_message(
                call,
                f"✅ Заявка №{review.request_id}: методичка "
                f"№{review.lesson_number} уже выдана.",
            )
            answer_review_callback(call, "Уже выдана.")
        else:
            edit_review_message(
                call,
                f"✅ Заявка №{review.request_id}: методичка "
                f"№{review.lesson_number} уже выдана.",
            )
            answer_review_callback(
                call,
                "❌ Методичка уже выдана; отказать нельзя.",
                show_alert=True,
            )
        return

    if review.status is not requested_status:
        if review.status is LessonReviewStatus.APPROVED:
            edit_review_message(
                call,
                f"✅ Заявка №{review.request_id} уже одобрена.\n"
                f"Методичка №{review.lesson_number} ожидает отправки или уже "
                "отправляется.",
                reply_markup=build_lesson_review_markup(
                    review.request_id,
                    retry_only=True,
                ),
            )
        elif review.status is LessonReviewStatus.REJECTED:
            user_notified = review.user_notified_at is not None
            edit_review_message(
                call,
                f"❌ Заявка №{review.request_id} уже отклонена.\n"
                f"Telegram ID: {review.telegram_id}\nMEXC UID: {review.mexc_uid}"
                + (
                    ""
                    if user_notified
                    else "\n\n⚠️ Пользователь пока не уведомлён."
                ),
                reply_markup=(
                    None
                    if user_notified
                    else build_rejection_notification_retry_markup(
                        review.request_id
                    )
                ),
            )
        answer_review_callback(
            call,
            "❌ По этой заявке уже принято другое решение.",
            show_alert=True,
        )
        return

    if requested_status is LessonReviewStatus.REJECTED:
        notification_result = "already" if review.user_notified_at is not None else "busy"
        notification_token = rejection_notification_token
        if review.user_notified_at is None:
            claimed = decision.notification_claimed

            if claimed:
                notification_result = "failed"
                notification_completed = False
                try:
                    bot.send_message(
                        review.telegram_id,
                        f"❌ Заявка №{review.request_id} на методичку "
                        f"№{review.lesson_number} не прошла проверку. "
                        "После выполнения условия можно подать новую заявку.",
                    )
                    notification_completed = (
                        storage.complete_lesson_review_notification(
                            review.request_id,
                            "user",
                            notification_token,
                        )
                    )
                    notification_result = (
                        "sent" if notification_completed else "failed"
                    )
                except (StorageError, sqlite3.Error):
                    logger.error("Manual review rejection notice state was not persisted")
                except Exception as exc:
                    log_telegram_error("Manual review rejection notice failed", exc)
                finally:
                    if not notification_completed:
                        release_review_notification(
                            review.request_id,
                            "user",
                            notification_token,
                        )

        try:
            current = storage.get_lesson_review_request(review.request_id)
        except (StorageError, sqlite3.Error):
            current = None
        if current is not None and current.status is LessonReviewStatus.FULFILLED:
            edit_review_message(
                call,
                f"✅ Заявка №{review.request_id}: методичка "
                f"№{review.lesson_number} уже выдана.",
            )
            answer_review_callback(
                call,
                "❌ Методичка уже выдана; отказать нельзя.",
                show_alert=True,
            )
            return
        user_notified = current is not None and current.user_notified_at is not None
        if user_notified and notification_result == "busy":
            notification_result = "already"
        notification_pending = notification_result == "busy" and not user_notified
        edit_review_message(
            call,
            f"❌ Заявка №{review.request_id} отклонена.\n"
            f"Telegram ID: {review.telegram_id}\nMEXC UID: {review.mexc_uid}"
            + (
                ""
                if user_notified
                else (
                    "\n\n⏳ Уведомление пользователю обрабатывается. "
                    "Если оно не придёт, повторите через 2 минуты."
                    if notification_pending
                    else "\n\n⚠️ Пользователь пока не уведомлён."
                )
            ),
            reply_markup=(
                None
                if user_notified
                else build_rejection_notification_retry_markup(review.request_id)
            ),
        )
        answer_review_callback(
            call,
            {
                "sent": "Заявка отклонена, пользователь уведомлён.",
                "already": "Заявка уже отклонена.",
                "busy": "⏳ Обработка заявки уже выполняется.",
                "failed": "⚠️ Отказ сохранён, но уведомление не доставлено.",
            }[notification_result],
            show_alert=not user_notified,
        )
        return

    delivery = deliver_approved_lesson_review(review)
    if delivery == REVIEW_DELIVERY_DELIVERED:
        edit_review_message(
            call,
            f"✅ Заявка №{review.request_id} одобрена.\n"
            f"Методичка №{review.lesson_number} выдана Telegram ID "
            f"{review.telegram_id}.",
        )
        answer_review_callback(call, "Методичка выдана.")
    elif delivery == REVIEW_DELIVERY_BUSY:
        answer_review_callback(call, "⏳ Отправка уже выполняется.")
    else:
        edit_review_message(
            call,
            f"⚠️ Заявка №{review.request_id} одобрена, но PDF не "
            "доставлен. Повторите отправку.",
            reply_markup=build_lesson_review_markup(
                review.request_id,
                retry_only=True,
            ),
        )
        answer_review_callback(
            call,
            "⚠️ Не удалось доставить PDF. Кнопка осталась для повтора.",
            show_alert=True,
        )


@bot.message_handler(
    func=lambda message: (message.text or '') in MENU_LESSON_BUTTONS
    or (message.text or '') == MENU_REFERRAL_BUTTON
)
def main_menu_handler(message):
    action = message.text or ''
    if action == MENU_REFERRAL_BUTTON:
        referral_handler(message)
        return

    lesson_number = MENU_LESSON_BUTTONS[action]
    if lesson_number == 1:
        show_lesson1_subscription_prompt(message.from_user.id)
        return
    process_lesson_request(message, lesson_number)

# --- КОМАНДЫ ДЛЯ МЕТОДИЧЕК №2-№7 ---
def process_lesson_request(message, lesson_number):
    user_id = message.from_user.id
    try:
        storage.ensure_user(user_id)
        already_issued = storage.is_lesson_issued(user_id, lesson_number)
        qualified_invites = storage.count_qualified_invites(user_id)
        user_state = storage.get_user(user_id)
    except (StorageError, sqlite3.Error):
        logger.error("Lesson state storage is unavailable lesson=%s", lesson_number)
        bot.send_message(user_id, "⚠️ Хранилище временно недоступно. Попробуйте позже.")
        return

    if already_issued:
        bot.send_message(user_id, f"ℹ️ Методичка №{lesson_number} уже была вам выдана.")
        return

    if lesson_number in (4, 6) or (lesson_number == 7 and qualified_invites >= 3):
        result = evaluate_lesson(
            lesson_number,
            qualified_invites=qualified_invites,
        )
        bot.send_message(user_id, result.message)
        if result.is_eligible:
            issue_lesson_once(user_id, lesson_number)
        return

    activity_state = None
    if user_state and user_state.activity_confirmed_at is not None:
        activity_state = ActivityState(
            confirmed_at_ms=user_state.activity_confirmed_at,
            baseline_last_trade_time_ms=user_state.activity_baseline_last_trade_time,
        )

    now_ms = int(time.time() * 1000)
    if lesson_number == 5:
        preliminary = evaluate_lesson(
            5,
            activity_state=activity_state,
            now_ms=now_ms,
        )
        if activity_state is None or now_ms < (
            activity_state.confirmed_at_ms + ACTIVITY_PERIOD_MS
        ):
            bot.send_message(user_id, preliminary.message)
            return

    if not lesson_requires_referral_data(
        lesson_number,
        qualified_invites=qualified_invites,
    ):
        return

    if mexc is None:
        bot.send_message(
            user_id,
            "⚠️ Автоматическая проверка MEXC временно недоступна. "
            "Обратитесь к администратору."
        )
        return

    if user_state and user_state.mexc_uid:
        check_lesson_with_uid(
            user_id,
            lesson_number,
            user_state.mexc_uid,
            force_refresh=(lesson_number == 5),
        )
        return

    instruction_text = (
        "🔍 Где найти свой UID на MEXC:\n\n"
        "1. Откройте приложение или сайт MEXC.\n"
        "2. Нажмите на иконку профиля.\n"
        "3. Скопируйте числовой UID под своим именем.\n\n"
        "Отправьте UID ответом на это сообщение:"
    )
    prompt_message = bot.send_message(user_id, instruction_text)
    bot.register_next_step_handler(prompt_message, process_uid, lesson_number)


def process_uid(message, lesson_number):
    user_id = message.from_user.id
    uid = (message.text or '').strip()
    logger.info("Processing MEXC lesson request lesson=%s", lesson_number)

    if not uid.isdigit():
        bot.send_message(
            user_id,
            "❌ UID должен содержать только цифры. Повторите через /get_lesson"
            + str(lesson_number)
        )
        return

    check_lesson_with_uid(
        user_id,
        lesson_number,
        uid,
        force_refresh=(lesson_number == 5),
    )


def check_lesson_with_uid(user_id, lesson_number, uid, *, force_refresh=False):
    try:
        user_state = storage.get_user(user_id)
    except (StorageError, sqlite3.Error):
        logger.error("MEXC binding storage is unavailable lesson=%s", lesson_number)
        bot.send_message(user_id, "⚠️ Хранилище временно недоступно. Попробуйте позже.")
        return
    if user_state and user_state.mexc_uid and user_state.mexc_uid != uid:
        bot.send_message(
            user_id,
            "❌ К вашему Telegram уже привязан другой MEXC UID. "
            "Автоматическая замена запрещена."
        )
        return

    bot.send_chat_action(user_id, 'typing')
    try:
        referral = get_referral_cached(uid, force_refresh=force_refresh)
    except MexcClientError as exc:
        logger.warning(
            "MEXC lesson check failed lesson=%s kind=%s",
            lesson_number,
            exc.kind,
        )
        bot.send_message(user_id, exc.public_message)
        return

    if referral is None:
        bot.send_message(
            user_id,
            "❌ UID не найден в истории рефералов MEXC. Проверьте, что регистрация "
            "была по вашей ссылке, и повторите после первой сделки."
        )
        return

    try:
        storage.bind_mexc_uid(user_id, uid)
    except MexcUidAlreadyBoundError:
        bot.send_message(
            user_id,
            "❌ Этот MEXC UID уже привязан к другому Telegram-пользователю."
        )
        return
    except UserMexcUidConflictError:
        bot.send_message(
            user_id,
            "❌ К вашему Telegram уже привязан другой MEXC UID."
        )
        return
    except (StorageError, sqlite3.Error):
        logger.error("MEXC UID binding could not be persisted lesson=%s", lesson_number)
        bot.send_message(user_id, "⚠️ Хранилище временно недоступно. Попробуйте позже.")
        return

    now_ms = int(time.time() * 1000)
    try:
        if referral.first_trade_time is not None:
            storage.record_activity_confirmation(
                user_id,
                confirmed_at=now_ms,
                baseline_last_trade_time=(
                    referral.last_trade_time or referral.first_trade_time
                ),
            )
        if referral.first_trade_time is not None:
            storage.mark_qualified(user_id)

        user_state = storage.get_user(user_id)
        qualified_invites = storage.count_qualified_invites(user_id)
    except (StorageError, sqlite3.Error):
        logger.error("MEXC eligibility state could not be persisted lesson=%s", lesson_number)
        bot.send_message(user_id, "⚠️ Хранилище временно недоступно. Попробуйте позже.")
        return
    activity_state = None
    if user_state and user_state.activity_confirmed_at is not None:
        activity_state = ActivityState(
            confirmed_at_ms=user_state.activity_confirmed_at,
            baseline_last_trade_time_ms=user_state.activity_baseline_last_trade_time,
        )

    result = evaluate_lesson(
        lesson_number,
        referral,
        qualified_invites=qualified_invites,
        activity_state=activity_state,
        now_ms=now_ms if lesson_number == 5 else None,
    )
    if (
        lesson_number in (3, 7)
        and referral.trading_amount is None
        and result.status is EligibilityStatus.INELIGIBLE
    ):
        submit_lesson_review(user_id, lesson_number)
        return

    bot.send_message(user_id, result.message)
    if result.status is EligibilityStatus.ELIGIBLE:
        issue_lesson_once(user_id, lesson_number)


@bot.message_handler(commands=['get_lesson2'])
def get_lesson2(message):
    process_lesson_request(message, 2)

@bot.message_handler(commands=['get_lesson3'])
def get_lesson3(message):
    process_lesson_request(message, 3)

@bot.message_handler(commands=['get_lesson4'])
def get_lesson4(message):
    process_lesson_request(message, 4)

@bot.message_handler(commands=['get_lesson5'])
def get_lesson5(message):
    process_lesson_request(message, 5)

@bot.message_handler(commands=['get_lesson6'])
def get_lesson6(message):
    process_lesson_request(message, 6)

@bot.message_handler(commands=['get_lesson7'])
def get_lesson7(message):
    process_lesson_request(message, 7)


@bot.message_handler(content_types=['text'], func=lambda message: True)
def fallback_text_handler(message):
    """Never leave an ordinary text message without a clear next action."""
    send_main_menu(
        message.from_user.id,
        "Не понял сообщение. Выберите действие кнопкой ниже или отправьте /start.",
    )

# --- ЗАПУСК ---
IS_RENDER = bool(os.environ.get('RENDER_EXTERNAL_HOSTNAME'))

# Gunicorn imports ``app`` from this module instead of executing it as a
# script, so configure the Telegram webhook during module initialization.
if IS_RENDER:
    configure_render_webhook()

if __name__ == '__main__':
    if not IS_RENDER:
        logger.info("Starting bot in long-polling mode")
        bot.infinity_polling()
    else:
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
