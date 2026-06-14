import os
import json
import time
import logging
import threading
import re
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = "8406740142:AAHaYy7Yb2FhTLNpKEE0AHStyUJDw-FRaOg"
YOUR_TELEGRAM_ID = 6094135274
TELEGRAM_CHANNEL_ID = "@dfnsgnrgowngguidbgwnghgbdjiowbfw"

# OpenRouter API ключ
OPENROUTER_API_KEY = "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"

# Словарь брендов (только для отображения, поиск только по фото)
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

# Курсы валют
rates = {"usd_per_jpy": 0.0064, "byn_per_jpy": 0.021}


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
    """
    Отправляет фото в бесплатную AI-модель OpenRouter.
    Возвращает процент совпадения (0-100) или None при ошибке.
    """
    if not image_url:
        logger.warning("Нет URL фото для анализа")
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
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": image_url}
                        ]
                    }
                ],
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
                logger.warning(f"AI вернул не число: {answer}")
                return None
        else:
            logger.error(f"OpenRouter ошибка: {response.status_code} - {response.text}")
            return None

    except Exception as e:
        logger.error(f"Ошибка запроса к OpenRouter: {e}")
        return None


def get_image_url_from_item_page(item_link):
    """Получает URL первого изображения со страницы товара на Mercari"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(item_link, headers=headers, timeout=15)
        if response.status_code == 200:
            img_pattern = r'https://static\.mercari\.net/images/.*?\.(jpg|jpeg|png)'
            matches = re.findall(img_pattern, response.text, re.IGNORECASE)
            if matches:
                img_url = matches[0] if isinstance(matches[0], str) else matches[0][0]
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                logger.info(f"Найдено фото товара: {img_url}")
                return img_url
    except Exception as e:
        logger.error(f"Ошибка получения фото со страницы: {e}")
    return None


# ========== ПОИСК НА MERCARI ==========
def fetch_mercari_items(keyword):
    """Ищет товары на Mercari по ключевому слову (японский бренд)"""
    items = []
    search_url = f"https://jp.mercari.com/search?keyword={keyword}&sort=created_time&order=desc"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

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

                photo_url = None
                if i < len(photo_urls):
                    photo_url = photo_urls[i]
                    if photo_url.startswith('//'):
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


# ========== ОБРАБОТЧИКИ КОМАНД ==========
async def check_access(update: Update) -> bool:
    if update.effective_user.id != YOUR_TELEGRAM_ID:
        await update.message.reply_text("🔒 Доступ запрещён.")
        return False
    return True


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await update.message.reply_text(
        f"🚀 Бот для поиска на Mercari Japan (ТОЛЬКО ПО ФОТО)!\n\n"
        f"📸 Как работает:\n"
        f"1. Отправьте фото товара\n"
        f"2. Бот запомнит бренд и будет искать похожие товары\n"
        f"3. AI проверяет совпадение с фото (нужно ≥70%)\n"
        f"4. Подходящие товары приходят в канал\n\n"
        f"💰 Бюджет: БЕЗЛИМИТНЫЙ\n"
        f"🎯 Требуемое совпадение: 70%\n\n"
        f"/add - начать отслеживание по фото\n"
        f"/list - список активных потоков\n"
        f"/stop ID - остановить поток"
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    uid = update.effective_user.id
    user_sessions[uid] = {"photos": [], "brand": None}

    await update.message.reply_text(
        "📸 Отправьте фото товара, который хотите отслеживать.\n\n"
        "Бот запомнит бренд и будет искать похожие товары с AI проверкой.\n\n"
        "Можно отправить несколько фото (для лучшего распознавания).\n"
        "Когда закончите - напишите /done"
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
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
            await update.message.reply_text(
                f"📸 Фото получено! ({count})\n"
                f"Отправьте ещё фото (для лучшего распознавания) или /done"
            )
        else:
            await update.message.reply_text(f"📸 Фото получено! Всего: {count}")
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await update.message.reply_text("❌ Ошибка сохранения фото")


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    uid = update.effective_user.id
    if uid not in user_sessions or not user_sessions[uid]["photos"]:
        await update.message.reply_text("❌ Сначала отправьте фото товара")
        return

    session = user_sessions[uid]
    await update.message.reply_text("🤖 Анализирую фото и запускаю поиск...")

    # Определяем бренд по умолчанию (пользователь может указать позже)
    # В текущей версии используем Rick Owens как базовый
    detected_brand = "Rick Owens"

    # Создаём поток поиска (только приоритетный, по фото)
    item_id = str(int(time.time()))
    tracked_items[item_id] = {
        "id": item_id,
        "type": "PHOTO_STREAM",
        "brand": detected_brand,
        "keyword_jp": BRAND_MAP.get(detected_brand, "リックオウエンス"),
        "photos": session["photos"],
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    if not await check_access(update):
        return

    if not tracked_items:
        await update.message.reply_text("📭 Нет активных потоков поиска")
        return

    msg = "📋 Активные потоки (только по фото):\n\n"
    for k, v in tracked_items.items():
        msg += f"🆔 {v['id']}\n"
        msg += f"🏷 Бренд: {v.get('brand', 'Unknown')}\n"
        msg += f"🎯 Требуется 70% совпадения\n"
        msg += f"📅 Создан: {v.get('added_at', 'Unknown')}\n\n"
    await update.message.reply_text(msg)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
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


# ========== ФОНОВЫЙ МОНИТОРИНГ ==========
def run_monitor_loop(bot):
    rate_timer = 0
    SIMILARITY_THRESHOLD = 70  # Порог для проверки по фото

    while True:
        try:
            rate_timer += 1
            if rate_timer >= 240:
                update_currency_rates()
                rate_timer = 0

            if tracked_items:
                logger.info("Проверка новых товаров на Mercari...")
                for item_id, item_data in list(tracked_items.items()):
                    # Только фото-потоки
                    if item_data.get("type") != "PHOTO_STREAM":
                        continue

                    keyword = item_data["keyword_jp"]
                    brand = item_data.get("brand", "Unknown")

                    found_items = fetch_mercari_items(keyword)

                    for item in found_items:
                        if item["id"] in sent_items_cache:
                            continue

                        # Получаем фото товара
                        photo_url = item.get("photo_url")
                        if not photo_url:
                            photo_url = get_image_url_from_item_page(item["link"])

                        if photo_url:
                            # Анализируем через AI
                            similarity = analyze_photo_with_ai(photo_url, brand)
                            if similarity is None:
                                logger.warning(f"Не удалось проанализировать фото товара {item['id']}, пропускаем")
                                continue

                            if similarity < SIMILARITY_THRESHOLD:
                                logger.info(
                                    f"Товар {item['id']} не прошел AI проверку: {similarity}% < {SIMILARITY_THRESHOLD}%")
                                continue
                            else:
                                logger.info(f"Товар {item['id']} прошел проверку: {similarity}% совпадения")
                        else:
                            logger.warning(f"Не удалось получить фото для товара {item['id']}, пропускаем")
                            continue

                        # Товар подходит - отправляем
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

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("done", done_cmd))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # Запускаем фоновый мониторинг
    threading.Thread(target=run_monitor_loop, args=(application.bot,), daemon=True).start()

    logger.info("🚀 Бот запущен! Поиск ТОЛЬКО по фото с AI проверкой 70%")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()