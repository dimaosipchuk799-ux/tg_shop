import logging, re, json, csv, os, asyncio, yaml, datetime
from typing import Dict, Any, Optional

from rapidfuzz import fuzz, process
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from config import TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, OPENAI_MODEL, BOT_LANG

# Optional OpenAI client (safe import)
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    openai_client = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(__file__), "faq.yaml")
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt_system.txt")
LEADS_CSV = os.path.join(os.path.dirname(__file__), "leads.csv")

with open(DATA_PATH, "r", encoding="utf-8") as f:
    DATA = yaml.safe_load(f)

with open(PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

def detect_lang(text: str) -> str:
    # naive detector: Cyrillic + a few words
    ru_markers = ["привет","здравствуйте","сколько","цена","доставка","адрес","меню","оплата","резерв"]
    uk_markers = ["привіт","скільки","ціна","доставка","адреса","меню","оплата","бронювання","режим"]
    score_ru = sum(1 for w in ru_markers if w in text.lower())
    score_uk = sum(1 for w in uk_markers if w in text.lower())
    return "ru" if score_ru > score_uk else "uk"

def build_keyboard(lang: str):
    if lang == "ru":
        buttons = [["Меню", "Доставка"], ["Оставить заявку"], ["Контакты", "Часы работы"]]
    else:
        buttons = [["Меню", "Доставка"], ["Залишити заявку"], ["Контакти", "Години роботи"]]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def faq_answer(user_text: str, threshold: int = 78) -> Optional[str]:
    # Search across all 'q' regex/rules
    for item in DATA.get("faq", []):
        patterns = item["q"].split("|")
        for p in patterns:
            if re.search(p.strip(), user_text, re.IGNORECASE):
                return item["a"]
    # Fuzzy match as backup
    qlist = [i["q"] for i in DATA.get("faq", [])]
    best = process.extractOne(user_text, qlist, scorer=fuzz.token_set_ratio)
    if best and best[1] >= threshold:
        idx = qlist.index(best[0])
        return DATA["faq"][idx]["a"]
    return None

async def gen_ai_reply(user_text: str, lang_hint: str) -> str:
    # Compose context
    context_blob = json.dumps(DATA, ensure_ascii=False, indent=2)
    user_lang = lang_hint or detect_lang(user_text)
    system = SYSTEM_PROMPT + f"\n\nYAML:\n{context_blob}\n\nLanguage to respond: {user_lang}"
    if openai_client:
        try:
            resp = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.4,
                max_tokens=300
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"OpenAI error: {e}")

    # Fallback if no LLM
    if user_lang == "ru":
        return "Я уточню у менеджера і вернусь с ответом. Могу оформить заявку на звонок? Напишите телефон."
    else:
        return "Уточню у менеджера і повернуся з відповіддю. Можу оформити заявку на дзвінок? Напишіть номер."

def save_lead(user_id: int, username: str, answers: Dict[str, str]):
    file_exists = os.path.exists(LEADS_CSV)
    with open(LEADS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp","user_id","username","full_name","phone","note"])
        writer.writerow([datetime.datetime.utcnow().isoformat(), user_id, username, answers.get("full_name",""), answers.get("phone",""), answers.get("note","")])

# Simple per-user state for lead collection
LEAD_STATE: Dict[int, Dict[str, Any]] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = BOT_LANG
    text_ru = "Привет! Я AI‑помощник магазина Shop Cozy. Задайте вопрос или нажмите кнопки ниже."
    text_uk = "Привіт! Я AI‑помічник магазину Shop Cozy. Питайте що завгодно або тисніть кнопки нижче."
    await update.message.reply_text(text_ru if lang=="ru" else text_uk, reply_markup=build_keyboard(lang))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = BOT_LANG
    msg = (
        "Команди:\n/start — старт\n/help — допомога\n/lead — залишити заявку\n"
        if lang=="uk" else
        "Команды:\n/start — старт\n/help — помощь\n/lead — оставить заявку\n"
    )
    await update.message.reply_text(msg)

async def lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    LEAD_STATE[user_id] = {"step": 0, "answers": {}}
    fields = DATA["leads"]["fields"]
    await update.message.reply_text(fields[0]["label"])

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    lang = detect_lang(text) if BOT_LANG not in ("uk","ru") else BOT_LANG

    # Handle lead flow
    if user_id in LEAD_STATE:
        state = LEAD_STATE[user_id]
        fields = DATA["leads"]["fields"]
        step = state["step"]
        key = fields[step]["name"]
        state["answers"][key] = text
        state["step"] += 1
        if state["step"] >= len(fields):
            save_lead(user_id, username, state["answers"])
            del LEAD_STATE[user_id]
            done_msg = "Дякуємо! Заявку передано менеджеру. Ми на зв'язку 👌" if lang=="uk" else "Спасибо! Заявка передана менеджеру. Мы на связи 👌"
            await update.message.reply_text(done_msg)
        else:
            await update.message.reply_text(fields[state["step"]]["label"])
        return

    # Quick buttons mapping
    lower = text.lower()
    if lower in ("залишити заявку","оставить заявку"):
        return await lead(update, context)
    if lower in ("контакти","контакты","контакты","контакти"):
        phone = DATA["company"]["contacts"]["phone"]
        msg = f"Телефон: {phone}"
        return await update.message.reply_text(msg)
    if lower in ("години роботи","часы работы"):
        wh = DATA["company"]["work_hours"]
        return await update.message.reply_text(wh)

    # 1) Try FAQ
    ans = faq_answer(text)
    if ans:
        await update.message.reply_text(ans)
        return
    # 2) AI fallback
    reply = await gen_ai_reply(text, lang)
    await update.message.reply_text(reply, reply_markup=build_keyboard(lang))

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Please set TELEGRAM_BOT_TOKEN in .env")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("lead", lead))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    logger.info("Bot is starting...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
