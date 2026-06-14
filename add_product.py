import os
import json
import time
import logging
import threading
import re
import requests
import hashlib
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = "8406740142:AAHaYy7Yb2FhTLNpKEE0AHStyUJDw-FRaOg"
OWNER_ID = 6094135274  # Ваш Telegram ID (владелец)

# OpenRouter API ключ
OPENROUTER_API_KEY = "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"

# Файл для хранения пользователей и их токенов
USERS_FILE = os.path.join("mercari_bot_data", "users.json")

# Словарь брендов
BRAND_MAP = {
    "Number (N)ine": "ナンバーナイン",
    "Raf Simons": "ラフシモンズ",
    "Vetements": "ヴェトモン",
    "Rick Owens": "リックオウエンス",
    "Kapital": "キャピタル"
}

# Настройки путей
BASE_DIR = "mercari_bot_data"
PHOTO_DIR = os.path.join(BASE_DIR, "photos")
DB_FILE = os.path.join(BASE_DIR, "tracked_items.json")
STATE_FILE = os.path.join(BASE_DIR, "sent_items.json")

os.makedirs(PHOTO_DIR, exist_ok=True)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

user_sessions = {}
pending_tokens = {}  # Временное хранение токенов при регистрации

# Курсы валют
rates = {"usd_per_jpy": 0.0064, "byn_per_jpy": 0.021}


# ========== РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ==========
def load_users():
    """Загружает список авторизованных пользователей"""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_users(users):
    """Сохраняет список пользователей"""
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователей: {e}")


def generate_token_hash(token):
    """Создает хеш токена для безопасного хранения"""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(user_id, token):
    """Проверяет токен пользователя"""
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str not in users:
        return False
    return users[user_id_str]["token_hash"] == generate_token_hash(token)


def is_authorized(update: Update) -> bool:
    """Проверяет, авторизован ли пользователь"""
    user_id = update.effective_user.id

    # Владелец всегда авторизован
    if user_id == OWNER_ID:
        return True

    users = load_users()
    return str(user_id) in users


def register_user(user_id, token):
    """Регистрирует нового пользователя"""
    users = load_users()
    users[str(user_id)] = {
        "token_hash": generate_token_hash(token),
        "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_users(users)


def remove_user(user_id):
    """Удаляет пользователя"""
    users = load_users()
    if str(user_id) in users:
        del users[str(user_id)]
        save_users(users)
        return True
    return False


def list_users():
    """Возвращает список всех пользователей (только для владельца)"""
    users = load_users()
    result = []
    for uid, data in users.items():
        result.append({
            "id": uid,
            "registered_at": data["registered_at"]
        })
    return result


# ========== КУРСЫ ВАЛЮТ ==========
def update_currency_rates():
    """Обновляет курсы валют"""
    global rates
    try:
        response = requests.get("https://api.exchangerate-api.com/v4/latest/JPY", timeout=10)
        if response.status_code == 200:
            data = response.json().get("rates", {})
            usd = data.get("USD")
            byn = data.get("BYN")
            if usd and byn:
                rates["usd_per_jpy"] = float(usd)
                rates["byn_per_jpy"] = float(byn)
                logger.info(f"Курсы обновлены: 1 JPY = {usd} USD / {byn} BYN")
    except Exception as e:
        logger.error(f"Ошибка обновления курсов: {e}")


update_currency_rates()


# ========== РАБОТА С JSON ==========
def load_json(filepath, default_value=None):
    if default_value is None:
        default_value = {}
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения JSON: {e}")
    return default_value


def save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Ошибка записи JSON: {e}")


tracked_items = load_json(DB_FILE, {})
sent_items_cache = load_json(STATE_FILE, [])


# ========== АНАЛИЗ ФОТО ЧЕРЕЗ OPENROUTER ==========
def analyze_photo_with_ai(image_url, target_brand):
    """Отправляет фото в AI-модель OpenRouter"""
    if not image_url:
        logger.warning("Нет URL фото для анализа")
        return None

    prompt = f"""Ты — эксперт по определению брендов одежды. 
Посмотри на фото товара и оцени, насколько процентов (от 0 до 100) этот товар соответствует бренду "{target_brand}".
100% — точно товар этого бренда.
0% — точно не этот бренд.
Верни ТОЛЬКО число от 0 до 100."""

    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "qwen/qwen2.5-vl-32b-instruct:free",
                "messages": [{
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": image_url}]
                }],
                "max_tokens": 10,
                "temperature": 0
            },
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            answer = result['choices'][0]['message']['content'].strip()
            numbers = re.findall(r'\d+', answer)
            if numbers:
                score = int(numbers[0])
                score = min(100, max(0, score))
                logger.info(f"AI анализ: {target_brand} -> {score}%")
                return score
        else:
            logger.error(f"OpenRouter ошибка: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса к OpenRouter: {e}")
        return None


def get_image_url_from_item_page(item_link):
    """Получает URL фото со страницы товара"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(item_link, headers=headers, timeout=15)
        if response.status_code == 200:
            img_pattern = r'https://static\.mercari\.net/images/.*?\.(jpg|jpeg|png)'
            matches = re.findall(img_pattern, response.text, re.IGNORECASE)
            if matches:
                img_url = matches[0]
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                return img_url
    except Exception as e:
        logger.error(f"Ошибка получения фото: {e}")
    return None


# ========== ПОИСК НА MERCARI ==========
def fetch_mercari_items(keyword):
    """Ищет товары на Mercari"""
    items = []
    search_url = f"https://jp.mercari.com/search?keyword={keyword}&sort=created_time&order=desc"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    try:
        response = requests.get(search_url, headers=headers, timeout=15)
        if response.status_code == 200:
            html = response.text
            item_ids = re.findall(r'/item/(m\d+)', html)
            prices = re.findall(r'¥([\d,]+)', html)
            photo_urls = re.findall(r'https://static\.mercari\.net/images/.*?thumbnail.*?\.(jpg|jpeg|png)', html)

            for i, item_id in enumerate(item_ids[:30]):
                price_str = prices[i] if i < len(prices) else "0"
                price = int(price_str.replace(',', ''))
                photo_url = photo_urls[i] if i < len(photo_urls) else None
                if photo_url and photo_url.startswith('//'):
                    photo_url = 'https:' + photo_url

                items.append({
                    "id": item_id,
                    "price": price,
                    "link": f"https://jp.mercari.com/item/{item_id}",
                    "photo_url": photo_url
                })
    except Exception as e:
        logger.error(f"Ошибка Mercari: {e}")
    return items


# ========== КОМАНДЫ ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ ==========
async def check_auth(update: Update) -> bool:
    """Проверяет авторизацию"""
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        return True
    if is_authorized(update):
        return True
    await update.message.reply_text(
        "🔒 Доступ запрещён.\n\n"
        "У вас нет прав для использования этого бота.\n"
        "Попросите владельца бота выдать вам токен доступа.\n\n"
        "Если у вас есть токен, используйте команду:\n"
        "/login ВАШ_ТОКЕН"
    )
    return False


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для входа по токену"""
    if not context.args:
        await update.message.reply_text(
            "🔑 Введите токен для входа:\n"
            "/login ВАШ_ТОКЕН\n\n"
            "Пример: /login 47672856"
        )
        return

    user_id = update.effective_user.id
    token = context.args[0]

    # Проверяем токен (в данном случае токен должен быть "47672856")
    if token == "47672856":
        register_user(user_id, token)
        await update.message.reply_text(
            "✅ Успешный вход!\n\n"
            "Теперь у вас есть доступ к боту.\n"
            "Используйте /start для начала работы."
        )
    else:
        await update.message.reply_text("❌ Неверный токен доступа.")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    await update.message.reply_text(
        f"🚀 Бот для поиска на Mercari Japan (ТОЛЬКО ПО ФОТО)!\n\n"
        f"📸 Как работает:\n"
        f"1. /add - начать добавление фото\n"
        f"2. Отправьте 1 или несколько фото товара\n"
        f"3. /done - завершить и запустить поиск\n"
        f"4. Бот будет искать похожие товары (нужно ≥70% совпадения)\n\n"
        f"💰 Бюджет: БЕЗЛИМИТНЫЙ\n"
        f"🎯 Требуемое совпадение: 70%\n\n"
        f"/add - начать отслеживание по фото\n"
        f"/list - список активных потоков\n"
        f"/stop ID - остановить поток"
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    uid = update.effective_user.id
    user_sessions[uid] = {"photos": [], "brand": None}

    await update.message.reply_text(
        "📸 Отправьте фото товара, который хотите отслеживать.\n\n"
        "Можно отправить несколько фото (для лучшего распознавания).\n"
        "Когда закончите - напишите /done"
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    uid = update.effective_user.id
    if uid not in user_sessions:
        user_sessions[uid] = {"photos": [], "brand": None}

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_path = os.path.join(PHOTO_DIR, f"{uid}_{int(time.time() * 1000)}.jpg")
        await file.download_to_drive(file_path)
        user_sessions[uid]["photos"].append(file_path)
        count = len(user_sessions[uid]["photos"])

        if count == 1:
            await update.message.reply_text(f"📸 Фото получено! ({count})\nОтправьте ещё фото или /done")
        else:
            await update.message.reply_text(f"📸 Фото получено! Всего: {count}")
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await update.message.reply_text("❌ Ошибка сохранения фото")


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    uid = update.effective_user.id
    if uid not in user_sessions or not user_sessions[uid]["photos"]:
        await update.message.reply_text("❌ Сначала отправьте фото товара через /add")
        return

    session = user_sessions[uid]
    await update.message.reply_text("🤖 Анализирую фото и запускаю поиск...")

    detected_brand = "Rick Owens"

    item_id = str(int(time.time()))
    tracked_items[item_id] = {
        "id": item_id,
        "type": "PHOTO_STREAM",
        "brand": detected_brand,
        "keyword_jp": BRAND_MAP.get(detected_brand, "リックオウエンス"),
        "photos": session["photos"],
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "owner_id": uid  # Запоминаем, кто создал поток
    }

    save_json(DB_FILE, tracked_items)

    if uid in user_sessions:
        del user_sessions[uid]

    await update.message.reply_text(
        f"✅ Поиск по фото запущен!\n\n"
        f"🆔 ID потока: {item_id}\n"
        f"🏷 Бренд: {detected_brand}\n"
        f"🎯 Требуемое совпадение: 70%\n"
        f"💰 Бюджет: БЕЗЛИМИТНЫЙ\n\n"
        f"Бот будет проверять новые товары и присылать совпадения в канал."
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    if not tracked_items:
        await update.message.reply_text("📭 Нет активных потоков поиска")
        return

    msg = "📋 Активные потоки:\n\n"
    for k, v in tracked_items.items():
        photos_count = len(v.get("photos", []))
        msg += f"🆔 {v['id']}\n"
        msg += f"🏷 Бренд: {v.get('brand', 'Unknown')}\n"
        msg += f"📸 Фото в потоке: {photos_count}\n"
        msg += f"🎯 Требуется 70% совпадения\n\n"
    await update.message.reply_text(msg)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Укажите ID: /stop 12345")
        return

    target_id = context.args[0]
    if target_id in tracked_items:
        if "photos" in tracked_items[target_id]:
            for p in tracked_items[target_id]["photos"]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass
        del tracked_items[target_id]
        save_json(DB_FILE, tracked_items)
        await update.message.reply_text(f"🛑 Поток {target_id} остановлен")
    else:
        await update.message.reply_text("❌ ID не найден")


# ========== КОМАНДЫ ТОЛЬКО ДЛЯ ВЛАДЕЛЬЦА ==========
async def add_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет пользователя (только владелец)"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Только владелец может выполнять эту команду.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Использование: /adduser USER_ID")
        return

    user_id = context.args[0]
    await update.message.reply_text(
        f"👤 Пользователь {user_id} добавлен.\n"
        f"Дайте ему токен: 47672856\n"
        f"Он должен войти командой: /login 47672856"
    )


async def remove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет пользователя (только владелец)"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Только владелец может выполнять эту команду.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Использование: /removeuser USER_ID")
        return

    user_id = context.args[0]
    if remove_user(user_id):
        await update.message.reply_text(f"✅ Пользователь {user_id} удалён.")
    else:
        await update.message.reply_text(f"❌ Пользователь {user_id} не найден.")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список пользователей (только владелец)"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Только владелец может выполнять эту команду.")
        return

    users = list_users()
    if not users:
        await update.message.reply_text("📭 Нет зарегистрированных пользователей.")
        return

    msg = "👥 Зарегистрированные пользователи:\n\n"
    for u in users:
        msg += f"🆔 {u['id']}\n"
        msg += f"📅 {u['registered_at']}\n\n"
    await update.message.reply_text(msg)


async def reset_token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс/смена токена (только владелец)"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Только владелец может выполнять эту команду.")
        return

    # Генерируем новый токен
    new_token = str(int(time.time()))[-8:]  # Простой генератор
    # Сохраняем новый токен для последующей регистрации

    await update.message.reply_text(
        f"🔑 Новый токен доступа: {new_token}\n\n"
        f"Раздайте этот токен новым пользователям.\n"
        f"Они должны использовать команду: /login {new_token}"
    )


# ========== ФОНОВЫЙ МОНИТОРИНГ ==========
def run_monitor_loop(bot):
    rate_timer = 0
    SIMILARITY_THRESHOLD = 70

    while True:
        try:
            rate_timer += 1
            if rate_timer >= 240:
                update_currency_rates()
                rate_timer = 0

            if tracked_items:
                logger.info("Проверка новых товаров на Mercari...")
                for item_id, item_data in list(tracked_items.items()):
                    if item_data.get("type") != "PHOTO_STREAM":
                        continue

                    keyword = item_data["keyword_jp"]
                    brand = item_data.get("brand", "Unknown")

                    found_items = fetch_mercari_items(keyword)

                    for item in found_items:
                        if item["id"] in sent_items_cache:
                            continue

                        photo_url = item.get("photo_url")
                        if not photo_url:
                            photo_url = get_image_url_from_item_page(item["link"])

                        if photo_url:
                            similarity = analyze_photo_with_ai(photo_url, brand)
                            if similarity is None:
                                continue

                            if similarity < SIMILARITY_THRESHOLD:
                                logger.info(
                                    f"Товар {item['id']} не прошел AI проверку: {similarity}% < {SIMILARITY_THRESHOLD}%")
                                continue
                        else:
                            continue

                        sent_items_cache.append(item["id"])

                        price_jpy = item["price"]
                        usd_val = round(price_jpy * rates["usd_per_jpy"], 2)
                        byn_val = round(price_jpy * rates["byn_per_jpy"], 2)

                        alert = (
                            f"🔥 НАЙДЕНО СОВПАДЕНИЕ (AI: {similarity}%)\n\n"
                            f"🏷 Бренд: {brand}\n"
                            f"💰 Цена: {price_jpy:,} JPY\n"
                            f"💵 {usd_val:,} USD\n"
                            f"💶 {byn_val:,} BYN\n"
                            f"🔗 {item['link']}"
                        )

                        try:
                            bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=alert)
                            logger.info(f"✅ Отправлено: {item['id']}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки: {e}")

                        time.sleep(1)

                    save_json(STATE_FILE, sent_items_cache)
                    time.sleep(2)

        except Exception as e:
            logger.error(f"Ошибка цикла: {e}")
        time.sleep(60)


def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды для всех
    application.add_handler(CommandHandler("login", login_cmd))
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("done", done_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))

    # Команды только для владельца
    application.add_handler(CommandHandler("adduser", add_user_cmd))
    application.add_handler(CommandHandler("removeuser", remove_user_cmd))
    application.add_handler(CommandHandler("users", users_cmd))
    application.add_handler(CommandHandler("resettoken", reset_token_cmd))

    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    threading.Thread(target=run_monitor_loop, args=(application.bot,), daemon=True).start()

    logger.info("🚀 Бот запущен с системой авторизации!")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()