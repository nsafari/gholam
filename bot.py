import asyncio
import html
import os
import re
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient
from telethon.tl.types import MessageEntityTextUrl


load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_LINKS = int(os.getenv("MAX_LINKS", "3"))
MAX_PAGE_CHARS = int(os.getenv("MAX_PAGE_CHARS", "18000"))
TELETHON_SESSION = os.getenv(
    "TELETHON_SESSION",
    str(Path.home() / ".local" / "share" / "telegram-gemini-summary-bot" / "telegram_user_session"),
)
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}
ALLOWED_CHAT_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
    if value.strip()
}
ALLOWED_CHANNEL_USERNAMES = {
    value.strip().lower().lstrip("@")
    for value in os.getenv("ALLOWED_CHANNEL_USERNAMES", "").split(",")
    if value.strip()
}

POST_URL_RE = re.compile(r"https?://t\.me/(?:s/)?([^/\s]+)/(\d+)")
URL_RE = re.compile(r"https?://[^\s<>)\]]+")
DAILY_DIGEST_RE = re.compile(
    r"(خلاصه\s+اخبار\s*(?:۲۴|24)\s*ساعت\s+گذشته|24[-\s]?hour\s+daily\s+digest|daily\s+digest)",
    re.IGNORECASE,
)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
Path(TELETHON_SESSION).parent.mkdir(parents=True, exist_ok=True)
telegram_client = TelegramClient(TELETHON_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)


def parse_telegram_post_url(text: str) -> Optional[tuple[str, int]]:
    match = POST_URL_RE.search(text)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def access_control_enabled() -> bool:
    return True


def is_allowed_update(update: Update) -> bool:
    if not access_control_enabled():
        return True

    user = update.effective_user
    chat = update.effective_chat

    if user and user.id in ALLOWED_USER_IDS:
        return True

    if chat and chat.id in ALLOWED_CHAT_IDS:
        return True

    if chat and chat.username and chat.username.lower() in ALLOWED_CHANNEL_USERNAMES:
        return True

    return False


def is_allowed_source_channel(channel: str) -> bool:
    if not ALLOWED_CHANNEL_USERNAMES:
        return True
    return channel.lower().lstrip("@") in ALLOWED_CHANNEL_USERNAMES


def is_channel_post_comment_target(message) -> bool:
    chat = message.chat

    if chat.type == ChatType.PRIVATE:
        return True

    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return bool(getattr(message, "is_automatic_forward", False))

    return False


def extract_urls(text: str) -> list[str]:
    urls = []
    seen = set()

    for url in URL_RE.findall(text):
        clean_url = url.rstrip(".,;:!?\"'")
        if "t.me/" in clean_url:
            continue
        if clean_url not in seen:
            urls.append(clean_url)
            seen.add(clean_url)

    return urls[:MAX_LINKS]


def is_daily_digest(text: str) -> bool:
    return bool(DAILY_DIGEST_RE.search(text))


def normalize_fetch_url(url: str) -> str:
    if "://x.com/" in url:
        return url.replace("://x.com/", "://fxtwitter.com/", 1)
    if "://twitter.com/" in url:
        return url.replace("://twitter.com/", "://fxtwitter.com/", 1)
    return url


def utf16_offset_to_py_index(text: str, offset: int) -> int:
    utf16_units = 0

    for index, char in enumerate(text):
        if utf16_units >= offset:
            return index
        utf16_units += 2 if ord(char) > 0xFFFF else 1

    return len(text)


def preserve_telethon_text_links(text: str, entities) -> str:
    if not entities:
        return text

    replacements = []
    for entity in entities:
        if isinstance(entity, MessageEntityTextUrl):
            start = utf16_offset_to_py_index(text, entity.offset)
            end = utf16_offset_to_py_index(text, entity.offset + entity.length)
            label = text[start:end]
            replacements.append((start, end, f"[{label}]({entity.url})"))

    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]

    return text


def preserve_bot_text_links(message) -> str:
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []

    replacements = []
    for entity in entities:
        if entity.type == "text_link" and entity.url:
            start = utf16_offset_to_py_index(text, entity.offset)
            end = utf16_offset_to_py_index(text, entity.offset + entity.length)
            label = message.parse_entity(entity)
            replacements.append(
                (
                    start,
                    end,
                    f"[{label}]({entity.url})",
                )
            )

    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]

    return text


async def fetch_telegram_post_text(channel: str, message_id: int) -> str:
    message = await telegram_client.get_messages(channel, ids=message_id)

    if not message or not message.message:
        raise ValueError("I could not find text in that Telegram post.")

    return preserve_telethon_text_links(message.message, message.entities)


def extract_readable_page_text(url: str) -> str:
    fetch_url = normalize_fetch_url(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "KHTML, like Gecko Chrome/120.0 Safari/537.36"
        )
    }

    with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
        response = client.get(fetch_url)
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise ValueError("The link did not return a readable HTML page.")

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "form", "nav", "footer", "aside"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description_tag = soup.find("meta", attrs={"name": "description"})
    description = description_tag.get("content", "").strip() if description_tag else ""

    article = soup.find("article") or soup.find("main") or soup.body or soup
    page_text = article.get_text("\n", strip=True)
    page_text = re.sub(r"\n{3,}", "\n\n", page_text)

    parts = [part for part in (title, description, page_text) if part]
    return "\n\n".join(parts)[:MAX_PAGE_CHARS]


async def fetch_pages(urls: list[str]) -> list[str]:
    pages = []

    for index, url in enumerate(urls, start=1):
        try:
            page_text = await asyncio.to_thread(extract_readable_page_text, url)
            if page_text:
                pages.append(f"Linked page {index}:\n{page_text}")
        except Exception as exc:
            pages.append(f"Linked page {index}: Could not read this page. Reason: {exc}")

    return pages


async def summarize_with_gemini(text: str) -> str:
    prompt = f"""
Process this NEW Telegram channel post.

1. SOURCE HANDLING:
- Read the actual post content and any fetched linked-page content.
- If the post is an X/Twitter or fxtwitter link, use the tweet text, author, and linked article as the primary source.
- Do not summarize based solely on an attached image.

2. TONE & STYLE:
- Write a richer 3-5 sentence conversational summary in natural Iranian colloquial Farsi.
- Use a داش مشتی / لاتی flavor, but keep it respectful.
- Keep it accurate, engaging, and vivid. Never use formal news-translation prose.
- Include the most important details: who/what happened, why it happened, key numbers, dates, names, consequences, and any important caveats.
- Rotate openings naturally. Use a mix of: "داش", "آبجی", "رفیق", "داداشی", "ببین", "راستش", "خلاصه", "طرف".
- Make the address inclusive. Sometimes assume the audience is female or mixed, and naturally use words like "آبجی"، "رفیق"، "دوست من"، or gender-neutral phrasing instead of always addressing men.
- Never use profanity, insults, or disrespectful language.

3. SIGNATURE:
- End the summary with this exact signature on a real standalone new line:
--داش غلام

4. WHY IT MATTERS:
- After the summary, add one final short sentence in the same colloquial Farsi style explaining why this post matters.
- Start that sentence naturally with "چرا مهمه؟" or "اهمیتش اینه که".
- Keep this before the signature.

5. EXCLUSIONS:
- If the content is a 24-hour daily digest, return exactly: SKIP_DAILY_DIGEST

6. OUTPUT FORMAT:
[3-5 sentences of casual Farsi summary with important details]
[1 sentence explaining why it matters]
--داش غلام

7. DO NOT INCLUDE:
- Do not include URLs.
- Do not include Markdown links.
- Do not include explanations, labels, or headers.

Content:
{text}
""".strip()

    response = await asyncio.to_thread(
        gemini_client.models.generate_content,
        model=GEMINI_MODEL,
        contents=prompt,
    )

    return response.text.strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_update(update):
        return

    await update.message.reply_text(
        "Send me a Telegram post URL or a message with a link. "
        "I will open the link and write a short colloquial Farsi summary with the channel signature."
    )


async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_update(update):
        return

    await update.message.reply_text(
        "This bot is configured for short colloquial Iranian Farsi summaries."
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    user_id = user.id if user else "none"
    chat_id = chat.id if chat else "none"
    chat_username = f"@{chat.username}" if chat and chat.username else "none"

    await update.effective_message.reply_text(
        f"user_id: {user_id}\nchat_id: {chat_id}\nchat_username: {chat_username}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telegram channel comments are messages in the linked discussion group.
    # Ignore direct channel posts so the bot never creates a separate channel post.
    message = update.message
    if not message:
        return

    if not is_allowed_update(update):
        return

    if not is_channel_post_comment_target(message):
        return

    await message.chat.send_action(ChatAction.TYPING)

    incoming_text = message.text or message.caption or ""
    post_ref = parse_telegram_post_url(incoming_text)

    try:
        if post_ref:
            channel, message_id = post_ref
            if not is_allowed_source_channel(channel):
                await message.reply_text("That source channel is not allowed.")
                return
            source_text = await fetch_telegram_post_text(channel, message_id)
        else:
            source_text = preserve_bot_text_links(message).strip()

        if not source_text:
            await message.reply_text("Please send a Telegram post URL or a message with a link.")
            return

        if is_daily_digest(source_text):
            return

        urls = extract_urls(source_text)
        if urls:
            page_parts = await fetch_pages(urls)
            summary_input = "\n\n".join(page_parts)
        else:
            summary_input = source_text

        if is_daily_digest(summary_input):
            return

        summary = await summarize_with_gemini(summary_input)
        if summary.strip() == "SKIP_DAILY_DIGEST":
            return

        summary += "\n\n#gholam"

        # Telegram messages are limited to 4096 chars. Leave room for formatting.
        for chunk_start in range(0, len(summary), 3900):
            chunk = summary[chunk_start : chunk_start + 3900]
            await message.reply_text(chunk, disable_web_page_preview=True)

    except Exception as exc:
        await message.reply_text(f"Could not summarize that message: {html.escape(str(exc))}")


async def post_init(app: Application) -> None:
    await telegram_client.start()


async def post_shutdown(app: Application) -> None:
    await telegram_client.disconnect()


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lang", set_language))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(MessageHandler(filters.TEXT | filters.CaptionRegex(r".+"), handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
