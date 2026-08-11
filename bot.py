import os
import io
import time
import random
import logging
import threading
import telebot
from telebot import types
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont

from eligibility import EligibilityStatus, evaluate_lesson, lesson_requires_referral_data
from mexc_client import MexcClient, MexcClientError, MexcConfigurationError


LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# --- НАСТРОЙКИ ---
CHANNEL_USERNAME = "tradegrowthh"
WEBHOOK_PATH = TOKEN

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
    logger.info("MEXC client initialized")

MEXC_CACHE_TTL_SECONDS = 30
MEXC_CACHE_MAX_ENTRIES = 256
_mexc_cache = {}
_mexc_cache_lock = threading.Lock()


def get_referral_cached(uid):
    """Avoid duplicate MEXC lookups for the same UID within this process."""
    if mexc is None:
        raise MexcConfigurationError("MEXC client is not configured")

    now = time.monotonic()
    with _mexc_cache_lock:
        cached = _mexc_cache.get(uid)
        if cached and cached[0] > now:
            return cached[1]

        expired_keys = [key for key, value in _mexc_cache.items() if value[0] <= now]
        for key in expired_keys:
            _mexc_cache.pop(key, None)

    referral = mexc.get_affiliate_referral(uid)

    with _mexc_cache_lock:
        if len(_mexc_cache) >= MEXC_CACHE_MAX_ENTRIES:
            _mexc_cache.pop(next(iter(_mexc_cache)))
        _mexc_cache[uid] = (now + MEXC_CACHE_TTL_SECONDS, referral)
    return referral

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
    return buf.getvalue()

def send_captcha(chat_id, attempts):
    text = generate_captcha_text()
    img_bytes = draw_captcha(text)
    captcha_data[chat_id] = {
        "answer": text,
        "attempts": attempts,
        "blocked_until": 0
    }
    bot.send_photo(chat_id, img_bytes, caption="Введите символы с картинки (заглавные буквы/цифры):")

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
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        logger.info("Telegram webhook configured")
        return True
    except Exception:
        # Telegram exceptions can contain request URLs; do not log their raw text.
        logger.error("Telegram webhook configuration failed")
        return False


@app.route('/' + WEBHOOK_PATH, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@app.route('/')
def webhook():
    if not os.environ.get('RENDER_EXTERNAL_HOSTNAME'):
        return "RENDER_EXTERNAL_HOSTNAME not set.", 500
    if configure_render_webhook():
        return "Webhook configured", 200
    return "Webhook configuration failed", 500

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
            "✅ Условие: Пополнить MEXC от 100 USDT и совершить первую сделку\n\n"
            "---\n\n"
            "Как получить следующую методичку (№2):\n\n"
            "1. Пополни MEXC от 100 USDT и соверши первую сделку\n"
            "2. Напиши боту команду: /get_lesson2\n"
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
            "2. Напиши боту команду: /get_lesson3\n"
            "3. Бот попросит ввести твой UID (цифры из профиля MEXC)\n"
            "4. Введи UID — и получишь третий урок!\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 3:
        return (
            "Красава! Ты освоил Price Action!\n\n"
            "Теперь ты понимаешь, как читать свечные паттерны и кластеры. Дальше — объёмы.\n\n"
            "Следующая методичка:\n\n"
            "📘 Методичка №4 — Горизонтальный объём, Value Area, POC, трейлинг стопа\n"
            "✅ Условие: Привести 1 друга, который зарегистрируется по твоей ссылке, пополнит от 100 USDT и совершит сделку\n\n"
            "---\n\n"
            "Как получить следующую методичку (№4):\n\n"
            "1. Приведи друга на MEXC по своей реферальной ссылке\n"
            "(Свою ссылку можно взять в разделе «Реферальная программа» на MEXC)\n"
            "2. Дождись, пока он пополнит от 100 USDT и совершит сделку\n"
            "3. Напиши боту команду: /get_lesson4\n"
            "4. Бот попросит ввести твой UID (цифры из профиля MEXC)\n"
            "5. Введи UID — и получишь четвёртый урок!\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 4:
        return (
            "Ты лидер! Ты привёл первого друга!\n\n"
            "Теперь у тебя есть команда. Время нарабатывать опыт.\n\n"
            "Следующая методичка:\n\n"
            "📘 Методичка №5 — Паттерны (вымпел, флаг, клин, М, W, Голова-плечи, Дракон)\n"
            "✅ Условие: Совершить 20 сделок с момента регистрации на MEXC\n\n"
            "---\n\n"
            "Как получить следующую методичку (№5):\n\n"
            "1. Продолжай торговать, пока не наберёшь 20 сделок\n"
            "(Считаются любые сделки — даже с минимальным объёмом. Главное — количество)\n"
            "2. Напиши боту команду: /get_lesson5\n"
            "3. Бот попросит ввести твой UID (цифры из профиля MEXC)\n"
            "4. Введи UID — и получишь пятый урок!\n\n"
            "Удачи на пути к профи! 🚀"
        )
    elif lesson_number == 5:
        return (
            "Ты уже профи! 20 сделок — это серьёзный опыт!\n\n"
            "Теперь ты готов к пониманию структуры рынка.\n\n"
            "Следующая методичка:\n\n"
            "📘 Методичка №6 — Структура рынка, накопление/распределение, 90% Value Area\n"
            "✅ Условие: Привести 2 друзей, которые зарегистрируются по твоей ссылке, пополнят от 100 USDT и совершат сделку\n\n"
            "---\n\n"
            "Как получить следующую методичку (№6):\n\n"
            "1. Приведи двух друзей на MEXC по своей реферальной ссылке\n"
            "(Свою ссылку можно взять в разделе «Реферальная программа» на MEXC)\n"
            "2. Дождись, пока они пополнят от 100 USDT и совершат сделку\n"
            "3. Напиши боту команду: /get_lesson6\n"
            "4. Бот попросит ввести твой UID (цифры из профиля MEXC)\n"
            "5. Введи UID — и получишь шестой урок!\n\n"
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
            "(Свою реферальную ссылку можно взять в разделе «Реферальная программа» на MEXC)\n"
            "2. Напиши боту команду: /get_lesson7\n"
            "3. Бот попросит ввести твой UID (цифры из профиля MEXC)\n"
            "4. Введи UID — и получишь финальный урок!\n\n"
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
def send_lesson(chat_id, lesson_number):
    files = LESSON_FILES.get(lesson_number)
    if not files:
        bot.send_message(chat_id, "❌ Методичка не найдена.")
        return False

    main_file = files.get("main")
    bonus_file = files.get("bonus")

    try:
        with open(main_file, 'rb') as f:
            bot.send_document(chat_id, f, caption=f"📘 Методичка №{lesson_number}")
    except FileNotFoundError:
        bot.send_message(chat_id, f"❌ Файл {main_file} не найден.")
        return False

    if bonus_file:
        try:
            with open(bonus_file, 'rb') as f:
                bot.send_document(chat_id, f, caption="🎁 Бонус! Дополнительный материал к уроку.")
        except FileNotFoundError:
            bot.send_message(chat_id, "⚠️ Бонусный файл не найден.")

    after_text = get_after_lesson_text(lesson_number)
    if after_text:
        bot.send_message(chat_id, after_text)
    return True

# --- ОБРАБОТЧИКИ ---
@bot.message_handler(commands=['start'])
def start_handler(message):
    captcha_data.pop(message.chat.id, None)
    send_captcha(message.chat.id, 0)

@bot.message_handler(func=lambda msg: msg.from_user.id in captcha_data and not msg.text.startswith('/'))
def captcha_input(message):
    user_id = message.from_user.id
    if check_captcha(user_id, message.text):
        markup = types.InlineKeyboardMarkup()
        btn_get = types.InlineKeyboardButton("📥 Забрать методичку", callback_data='request_pdf')
        markup.add(btn_get)
        bot.send_message(user_id, WELCOME_TEXT, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'request_pdf')
def handle_request_pdf(call):
    markup = types.InlineKeyboardMarkup()
    btn_sub = types.InlineKeyboardButton("🔔 Подписаться на канал", url=f'https://t.me/{CHANNEL_USERNAME}')
    btn_check = types.InlineKeyboardButton("✅ Проверить подписку", callback_data='check_sub')
    markup.add(btn_sub, btn_check)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=SUBSCRIBE_TEXT,
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'check_sub')
def handle_check_sub(call):
    user_id = call.from_user.id
    if is_subscribed(user_id):
        send_lesson(user_id, 1)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Методичка отправлена!")
    else:
        bot.answer_callback_query(call.id, "❌ Вы ещё не подписались на канал.", show_alert=True)

# --- ОБЩАЯ ФУНКЦИЯ ДЛЯ ПРОВЕРКИ ---
def process_lesson_request(message, lesson_number):
    user_id = message.chat.id

    if not lesson_requires_referral_data(lesson_number):
        result = evaluate_lesson(lesson_number)
        bot.send_message(user_id, result.message)
        return

    if mexc is None:
        bot.send_message(
            user_id,
            "⚠️ Автоматическая проверка MEXC временно недоступна. Обратитесь к администратору."
        )
        return

    instruction_text = (
        "🔍 Где найти твой UID на MEXC:\n\n"
        "1. Открой приложение или сайт MEXC\n"
        "2. Нажми на иконку профиля в правом верхнем углу\n"
        "3. Твой UID — это число, написанное под твоим именем\n\n"
        "📌 Пример UID: 12345678\n\n"
        "👇 Скопируй свой UID и отправь его в ответ на это сообщение:"
    )
    prompt_message = bot.send_message(user_id, instruction_text)
    bot.register_next_step_handler(prompt_message, process_uid, lesson_number)

def process_uid(message, lesson_number):
    user_id = message.chat.id
    uid = (message.text or '').strip()
    logger.info("Processing MEXC lesson request lesson=%s", lesson_number)
    
    if not uid.isdigit():
        bot.send_message(user_id, "❌ Введите только цифры! Попробуйте снова через /get_lesson" + str(lesson_number))
        return

    bot.send_chat_action(user_id, 'typing')
    try:
        referral = get_referral_cached(uid)
    except MexcClientError as exc:
        logger.warning("MEXC lesson check failed lesson=%s kind=%s", lesson_number, exc.kind)
        bot.send_message(user_id, exc.public_message)
        return

    if referral is None:
        bot.send_message(
            user_id,
            "❌ UID не найден среди прямых рефералов этого MEXC Affiliate-аккаунта."
        )
        return

    result = evaluate_lesson(lesson_number, referral)
    bot.send_message(user_id, result.message)
    if result.status is EligibilityStatus.ELIGIBLE:
        send_lesson(user_id, lesson_number)

# --- КОМАНДЫ ДЛЯ МЕТОДИЧЕК №2-№7 ---
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

# --- ЗАПУСК ---
if __name__ == '__main__':
    if os.environ.get('RENDER_EXTERNAL_HOSTNAME') is None:
        logger.info("Starting bot in long-polling mode")
        bot.infinity_polling()
    else:
        configure_render_webhook()
        
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
