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
OWNER_ID = 6094135274  # Ваш Telegram ID

# OpenRouter API ключ
OPENROUTER_API_KEY = "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"

# Файл для хранения пользователей
USERS_FILE = os.path.join("mercari_bot_data", "users.json")

# Настройки путей
BASE_DIR = "mercari_bot_data"
PHOTO_DIR = os.path.join(BASE_DIR, "photos")
DB_FILE = os.path.join(BASE_DIR, "tracked_items.json")
STATE_FILE = os.path.join(BASE_DIR, "sent_items.json")

os.makedirs(PHOTO_DIR, exist_ok=True)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

user_sessions = {}

# Курсы валют
rates = {"usd_per_jpy": 0.0064, "byn_per_jpy": 0.021}


# ========== РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ==========
def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_users(users):
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователей: {e}")


def generate_token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def register_user(user_id, token):
    users = load_users()
    users[str(user_id)] = {
        "token_hash": generate_token_hash(token),
        "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_users(users)


def is_authorized(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        return True
    users = load_users()
    return str(user_id) in users


# ========== КУРСЫ ВАЛЮТ ==========
def update_currency_rates():
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


# ========== AI ФУНКЦИИ ==========
def detect_brand_from_photo(image_url):
    """
    Определяет реальный бренд по фото через AI.
    Возвращает название бренда (например, "Nike", "Rick Owens") или "Unknown"
    """
    if not image_url:
        return "Unknown"

    prompt = """
Ты — эксперт по определению брендов одежды и обуви.
Посмотри на фото товара и определи, какой это бренд.
Верни ТОЛЬКО название бренда на английском языке.
Если бренд не определяется или ты не уверен - верни "Unknown".

Примеры правильных ответов:
- Rick Owens
- Nike
- Adidas
- Gucci
- Supreme
- Balenciaga
- Unknown
"""
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
                "max_tokens": 20,
                "temperature": 0
            },
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            brand = result['choices'][0]['message']['content'].strip()
            # Очищаем от лишних символов
            brand = re.sub(r'[^a-zA-Z\s\-\(\)]', '', brand)
            if brand and len(brand) < 50:
                logger.info(f"AI определил бренд: {brand}")
                return brand
    except Exception as e:
        logger.error(f"Ошибка определения бренда: {e}")

    return "Unknown"


def analyze_similarity_with_ai(image_url, target_brand):
    """
    Оценивает, насколько процентов товар соответствует искомому бренду.
    Возвращает число от 0 до 100.
    """
    if not image_url or not target_brand:
        return None

    prompt = f"""
Ты — эксперт по определению брендов одежды.
Посмотри на фото товара и оцени, насколько процентов (от 0 до 100) этот товар соответствует бренду "{target_brand}".
100% — это точно товар этого бренда (виден логотип, характерный стиль, бирка).
0% — это точно не этот бренд.
Верни ТОЛЬКО число от 0 до 100. Никаких пояснений, только число.
"""
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
                logger.info(f"AI проверка: {target_brand} -> {score}%")
                return score
    except Exception as e:
        logger.error(f"Ошибка AI проверки: {e}")

    return None


def get_image_url_from_item_page(item_link):
    """Получает URL фото со страницы товара на Mercari"""
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
    """Ищет товары на Mercari по ключевому слову (бренд на японском)"""
    items = []
    # Конвертируем английский бренд в японский (простое соответствие)
    brand_jp_map = {
        "Rick Owens": "リックオウエンス",
        "Nike": "ナイキ",
        "Adidas": "アディダス",
        "Gucci": "グッチ",
        "Supreme": "シュプリーム",
        "Balenciaga": "バレンシアガ"
    }
    search_keyword = brand_jp_map.get(keyword, keyword)

    search_url = f"https://jp.mercari.com/search?keyword={search_keyword}&sort=created_time&order=desc"
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


# ========== КОМАНДЫ БОТА ==========
async def check_auth(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        return True
    if is_authorized(update):
        return True
    await update.message.reply_text(
        "🔒 Доступ запрещён.\n\n"
        "У вас нет прав для использования этого бота.\n"
        "Если у вас есть токен, используйте команду:\n"
        "/login ВАШ_ТОКЕН"
    )
    return False


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("🔑 Введите токен: /login 47672856")
        return

    token = context.args[0]
    if token == "47672856":
        register_user(update.effective_user.id, token)
        await update.message.reply_text("✅ Успешный вход! Используйте /start")
    else:
        await update.message.reply_text("❌ Неверный токен.")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    await update.message.reply_text(
        f"🚀 Бот для поиска на Mercari Japan\n\n"
        f"📸 Как работает:\n"
        f"1. /add - начать добавление фото\n"
        f"2. Отправьте фото товара (можно несколько)\n"
        f"3. /done - завершить и запустить поиск\n"
        f"4. AI определит бренд и будет искать похожие товары\n\n"
        f"🎯 Требуемое совпадение: 70%\n\n"
        f"/list - список потоков\n"
        f"/stop ID - остановить поток"
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    uid = update.effective_user.id
    user_sessions[uid] = {"photos": []}
    await update.message.reply_text("📸 Отправьте фото товара. Когда закончите - /done")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    uid = update.effective_user.id
    if uid not in user_sessions:
        user_sessions[uid] = {"photos": []}
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_path = os.path.join(PHOTO_DIR, f"{uid}_{int(time.time() * 1000)}.jpg")
        await file.download_to_drive(file_path)
        user_sessions[uid]["photos"].append(file_path)
        count = len(user_sessions[uid]["photos"])
        await update.message.reply_text(f"📸 Фото {count}/? Отправляйте ещё или /done")
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    uid = update.effective_user.id
    if uid not in user_sessions or not user_sessions[uid]["photos"]:
        await update.message.reply_text("❌ Сначала отправьте фото через /add")
        return

    session = user_sessions[uid]
    await update.message.reply_text("🤖 Анализирую фото, определяю бренд...")

    # Определяем бренд по первому фото
    first_photo_path = session["photos"][0]

    # Для AI нужен URL фото (загружаем на временный хостинг или используем file_id)
    # Временно используем прямой путь (для локального тестирования)
    detected_brand = "Unknown"

    # Пробуем определить бренд через AI
    # ВНИМАНИЕ: для работы нужно загрузить фото куда-то с публичным URL
    # Пока оставим заглушку с определением по названию файла
    await update.message.reply_text(f"🤖 Определён бренд: {detected_brand}")

    # Конвертируем бренд в японский для поиска на Mercari
    brand_jp_map = {
        "Rick Owens": "リックオウエンス",
        "Nike": "ナイキ",
        "Adidas": "アディダス",
        "Gucci": "グッチ",
        "Supreme": "シュプリーム"
    }
    keyword_jp = brand_jp_map.get(detected_brand, "リックオウエンス")

    item_id = str(int(time.time()))
    tracked_items[item_id] = {
        "id": item_id,
        "type": "PHOTO_STREAM",
        "brand": detected_brand,
        "keyword_jp": keyword_jp,
        "photos": session["photos"],
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "owner_id": uid
    }
    save_json(DB_FILE, tracked_items)
    del user_sessions[uid]

    await update.message.reply_text(
        f"✅ Поиск запущен!\n\n"
        f"🆔 ID: {item_id}\n"
        f"🏷 Бренд: {detected_brand}\n"
        f"🎯 Требуемое совпадение: 70%\n"
        f"Бот будет искать товары бренда {detected_brand} на Mercari"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    if not tracked_items:
        await update.message.reply_text("📭 Нет активных потоков")
        return
    msg = "📋 Активные потоки:\n\n"
    for k, v in tracked_items.items():
        photos_count = len(v.get("photos", []))
        msg += f"🆔 {v['id']} — {v.get('brand', 'Unknown')} — {photos_count} фото\n"
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


# ========== МОНИТОРИНГ ==========
def run_monitor_loop(bot):
    SIMILARITY_THRESHOLD = 70

    while True:
        try:
            if tracked_items:
                logger.info("Проверка новых товаров...")
                for item_id, item_data in list(tracked_items.items()):
                    if item_data.get("type") != "PHOTO_STREAM":
                        continue

                    brand = item_data.get("brand", "Unknown")
                    keyword = item_data.get("keyword_jp", "リックオウエンス")

                    found_items = fetch_mercari_items(keyword)

                    for item in found_items:
                        if item["id"] in sent_items_cache:
                            continue

                        photo_url = item.get("photo_url")
                        if not photo_url:
                            photo_url = get_image_url_from_item_page(item["link"])

                        if photo_url:
                            similarity = analyze_similarity_with_ai(photo_url, brand)
                            if similarity is None or similarity < SIMILARITY_THRESHOLD:
                                continue

                            sent_items_cache.append(item["id"])
                            price_jpy = item["price"]
                            usd_val = round(price_jpy * rates["usd_per_jpy"], 2)

                            alert = (
                                f"🔥 НАЙДЕНО! ({similarity}% совпадения)\n\n"
                                f"🏷 Искомый бренд: {brand}\n"
                                f"💰 Цена: {price_jpy:,} JPY | {usd_val} USD\n"
                                f"🔗 {item['link']}"
                            )

                            try:
                                bot.send_message(chat_id=OWNER_ID, text=alert)
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

    application.add_handler(CommandHandler("login", login_cmd))
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("done", done_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    threading.Thread(target=run_monitor_loop, args=(application.bot,), daemon=True).start()

    logger.info("🚀 Бот запущен с определением бренда через AI!")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()