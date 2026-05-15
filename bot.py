import json
import os
import random
import re
import asyncio
import ast
import math
import hashlib
import base64
import io
import time
import pytz
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from dotenv import load_dotenv

# ================== ENV ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Kanal IDlar (K1 baza, K2 biznes, K3 treyler)
CHANNEL1_ID = int(os.getenv("BASE_CHANNEL_ID", "0"))
CHANNEL2_ID = int(os.getenv("BUSINESS_CHANNEL_ID", "0"))
CHANNEL3_ID = int(os.getenv("TRAILER_CHANNEL_ID", "0"))

# Majburiy obuna (2 ta kanal)
FORCE_SUB_1_ID = int(os.getenv("FORCE_SUB_1_ID", "0"))  # 2K
FORCE_SUB_1_LINK = os.getenv("FORCE_SUB_1_LINK", "")

FORCE_SUB_2_ID = int(os.getenv("FORCE_SUB_2_ID", "0"))  # 3K
FORCE_SUB_2_LINK = os.getenv("FORCE_SUB_2_LINK", "")

FORCE_SUB_ENABLED = (os.getenv("FORCE_SUB_ENABLED", "true").lower() == "true")

BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").lstrip("@").strip()
MOVIES_FILE = os.getenv("MOVIES_FILE", "movies.json")
STATS_FILE = os.getenv("STATS_FILE", "statistics.json")

AUTOPOST_FILE = os.getenv("AUTOPOST_FILE", "autopost.json")
TZ_NAME = os.getenv("TZ", "Asia/Tashkent")
TZ = pytz.timezone(TZ_NAME)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
AI_SESSION_TTL = int(os.getenv("AI_SESSION_TTL", "21600") if os.getenv("AI_SESSION_TTL", "21600").isdigit() else "21600")

ADMINS = {ADMIN_ID}

# ================== BOT ==================
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ================== XOTIRA ==================
# Yakuniy talab:
# - Yakka film: tugma 1 marta ishlasin (bosilgandan keyin eskirsin)
# - Serial: epizod tugmalari xohlagancha ishlasin
last_movie_request: Dict[int, str] = {}     # {user_id: code}
last_watch_token: Dict[int, str] = {}       # {user_id: token}
ai_poster_cache: Dict[int, Dict[str, Any]] = {}  # {base_channel_message_id: metadata}
ai_channel_session: Dict[int, Any] = {}           # {base_channel_id: {metadata, ts}}

# ================== EDIT BANNER ==================
MOVIE_BANNER = "♻️ Yangilandi"
SERIES_BANNER = "♻️ Yangi qismi qo'shildi yoki sifatli formatga almashtirildi"
TRAILER_BANNER = "♻️ Treyler yangilandi"  # 🆕 treyler uchun

BANNER_RE = re.compile(r"^♻️ .*?\n\n", re.IGNORECASE)

def _apply_edit_banner(caption: str, banner_text: str) -> str:
    cap = (caption or "").strip()
    cap = BANNER_RE.sub("", cap).strip()
    if not cap:
        return banner_text
    return f"{banner_text}\n\n{cap}"

# ================== PATH HELPERS ==================
def _ensure_parent_dir(path: str) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        pass

# ================== JSON (atomic) ==================
def _atomic_write_json(path: str, data: Any) -> None:
    _ensure_parent_dir(path)
    tmp = f"{path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

# ================== DB ==================
def load_db() -> Dict[str, Any]:
    if not os.path.exists(MOVIES_FILE):
        return {}
    try:
        with open(MOVIES_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        return {}

    # Backward compatibility (eski movies.json):
    fixed: Dict[str, Any] = {}
    for code, item in (db or {}).items():
        if not isinstance(item, dict):
            continue

        # eski format (type yo‘q)
        if "type" not in item:
            fixed[code] = {
                "type": "movie",
                "post_file_id": item.get("post_file_id"),
                "post_caption": item.get("post_caption", ""),
                "video_file_id": item.get("video_file_id"),
                "video_unique_id": item.get("video_unique_id"),
                "channel_msg_id": item.get("channel_msg_id"),
                "trailer": None  # 🆕
            }
        else:
            # yangi formatda ham trailer field bo‘lmasa qo‘shamiz
            item.setdefault("trailer", None)  # 🆕

            fixed[code] = item

    return fixed


def save_db(data: Dict[str, Any]) -> None:
    _atomic_write_json(MOVIES_FILE, data)

# ================== STATISTIKA (SODDA) ==================
def load_stats() -> Dict[str, Any]:
    # endi stats fayl deyarli ishlatilmaydi
    return {}

def save_stats(data: Dict[str, Any]) -> None:
    # hech narsa saqlamaymiz
    pass

def update_stats(user_id: int) -> None:
    # endi statistikani yig‘maymiz
    pass

# ================== AVTOKOD ==================
def generate_unique_code(db: Dict[str, Any]) -> str:
    # 1-bosqich: 4 xonali kodlar
    for _ in range(10000):
        code = str(random.randint(1000, 9999))
        if code not in db:
            return code

    # 2-bosqich: 5 xonali kodlarga o'tamiz (agar to'lib qolsa)
    for _ in range(100000):
        code = str(random.randint(10000, 99999))
        if code not in db:
            return code

    # fallback (deyarli hech qachon ishlamaydi)
    raise Exception("❌ Bo'sh kod qolmadi 😅")

# ================== OBUNA ==================
async def check_subscription(user_id: int) -> bool:
    if not FORCE_SUB_ENABLED:
        return True
    try:
        member1 = await bot.get_chat_member(FORCE_SUB_1_ID, user_id)
        member2 = await bot.get_chat_member(FORCE_SUB_2_ID, user_id)  # 🆕

        ok1 = member1.status in ("member", "administrator", "creator")
        ok2 = member2.status in ("member", "administrator", "creator")  # 🆕

        return ok1 and ok2  # 🆕 ikkalasiga ham obuna bo‘lishi kerak
    except Exception:
        return False


def subscribe_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔔 1-kanalga obuna bo‘lish", url=FORCE_SUB_1_LINK),
        types.InlineKeyboardButton("🔔 2-kanalga obuna bo‘lish", url=FORCE_SUB_2_LINK),  # 🆕
        types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")
    )
    return kb

# ================== MENULAR ==================
def user_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🎬 Qidiruv")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Kino qo‘shish", "➕ Serial qo‘shish")
    kb.row("✏️ Tahrirlash", "🗑 O‘chirish")
    kb.row("🎬 Qidiruv", "📊 Statistika")
    kb.row("📦 Kino backup")  # 🆕 stat backup olib tashlandi
    kb.row("♻️ Kino restore")  # 🆕 stat restore olib tashlandi
    kb.row("⚙️ Sozlash", "⏰ Avtopost")
    kb.row("❌ Bekor qilish")
    return kb

def is_admin(uid: int) -> bool:
    return uid in ADMINS

def protect_for(uid: int) -> bool:
    # oddiy user yopiq, admin ochiq
    return not is_admin(uid)

# ================== FSM ==================
class AddMovie(StatesGroup):
    post = State()
    trailer = State()  # 🆕 treyler bosqichi
    video = State()

class AddSeries(StatesGroup):
    poster = State()
    trailer = State()  # 🆕 treyler bosqichi
    episodes = State()

class EditFlow(StatesGroup):
    choose_type = State()    # movie / series
    choose_code = State()
    choose_action = State()
    await_forward = State()
    await_ep_delete = State()

class DeleteFlow(StatesGroup):
    code = State()

class RestoreFlow(StatesGroup):
    movies = State()
    stats = State()

class PublishLater(StatesGroup):
    code = State()

class AutoPostFlow(StatesGroup):
    menu = State()
    add_time = State()
    add_code = State()
    edit_id = State()
    edit_choose = State()
    edit_time = State()
    edit_code = State()
    del_id = State()

# ================== HELPERS ==================
CODE_LINE_RE = re.compile(r"(🆔\s*Kod:\s*([0-9]{4,5}))", re.IGNORECASE)  # 🆕 5 xonali ham

def _ensure_code_line_kept(new_caption: str, old_caption_with_code: str, code: str) -> str:
    m = CODE_LINE_RE.search(old_caption_with_code or "")
    code_line = m.group(1) if m else f"🆔 Kod: {code}"
    cleaned = CODE_LINE_RE.sub("", (new_caption or "")).strip()
    return f"{cleaned}\n\n{code_line}".strip() if cleaned else code_line


def _duplicate_video_exists(db: Dict[str, Any], video_unique_id: str) -> bool:
    for it in db.values():

        # 🎬 YAKKA FILM
        if it.get("type") == "movie":
            if it.get("video_unique_id") == video_unique_id:
                return True

        # 📺 SERIAL
        elif it.get("type") == "series":
            for epv in (it.get("episodes", {}) or {}).values():
                if isinstance(epv, dict) and epv.get("video_unique_id") == video_unique_id:
                    return True

    return False


async def _is_forward_from_base(message: types.Message) -> bool:
    return bool(message.forward_from_chat and int(message.forward_from_chat.id) == int(CHANNEL1_ID))


def _parse_episode_caption(caption: str) -> Tuple[Optional[int], str]:
    """
    QOIDALAR:
    - Birinchi uchragan raqam -> qism raqami
    - Qolgan matn -> nom (ichidagi boshqa raqamlar ahamiyatsiz)
    """
    if not caption:
        return None, ""
    text = caption.strip()
    m = re.search(r"\d+", text)
    if not m:
        return None, text
    ep = int(m.group(0))
    title = (text[:m.start()] + text[m.end():]).strip()
    title = re.sub(r"^[\s\|\-:–—]+", "", title).strip()
    return ep, title


def _episode_user_caption(ep: int, title: str) -> str:
    title = (title or "").strip()
    if title:
        return f"{ep}-qisim({title})"
    return f"{ep}-qisim"


def _sorted_episode_numbers(item: Dict[str, Any]) -> List[int]:
    eps = item.get("episodes", {}) or {}
    nums: List[int] = []
    for k in eps.keys():
        if str(k).isdigit():
            nums.append(int(k))
    return sorted(nums)

# ================== CAPTION FORMAT FIX ==================
def make_links_clickable(text: str) -> str:
    if not text:
        return ""

    # t.me linklarni topamiz
    pattern = r"(https?://t\.me/[^\s]+)"

    def repl(match):
        url = match.group(1)
        return f'<a href="{url}">🎬 Treyler va boshqa ma\'lumotlar</a>'

    return re.sub(pattern, repl, text)


def safe_caption(text: str) -> str:
    """
    Captionni buzmasdan yuborish:
    - HTML ni saqlaydi
    - oddiy linklarni avtomatik ko‘k clickable qiladi
    """
    text = (text or "").strip()
    return make_links_clickable(text)

# ================== INLINE KB ==================

def movie_watch_kb(code: str, token: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "🎬 Filmni ko‘rish",
            callback_data=f"watch2_{code}_{token}"
        )
    )
    return kb


def channel_movie_kb(code: str, trailer_url: Optional[str] = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)

    # 🎬 Bot orqali ko‘rish
    kb.add(
        types.InlineKeyboardButton(
            "🎬 Filmni bot orqali ko‘rish",
            url=f"https://t.me/{BOT_USERNAME}?start={code}"
        )
    )

    # 🎞 Treyler tugmasi
    if trailer_url:
        kb.add(
            types.InlineKeyboardButton(
                "🎞 Treyler va ma'lumotlar",
                url=trailer_url
            )
        )

    return kb


def channel_series_kb(code: str, trailer_url: Optional[str] = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)

    # 📺 Barcha qismlar
    kb.add(
        types.InlineKeyboardButton(
            "📺 Barcha qismlari",
            url=f"https://t.me/{BOT_USERNAME}?start=series_{code}"
        )
    )

    # 🎞 Treyler tugmasi
    if trailer_url:
        kb.add(
            types.InlineKeyboardButton(
                "🎞 Treyler va ma'lumotlar",
                url=trailer_url
            )
        )

    return kb


def series_eps_kb(code: str, eps: List[int]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.add(*[
        types.InlineKeyboardButton(str(n), callback_data=f"series_ep:{code}:{n}")
        for n in eps
    ])
    return kb


def edited_done_kb(code: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("✏️ Eskini tahrirlash", callback_data=f"edit_again:{code}"),
        types.InlineKeyboardButton("📣 Kanalga qayta yuborish", callback_data=f"republish:{code}"),
    )
    return kb

# ================== AI ADMIN YORDAMCHI ==================
AI_DEFAULT_METADATA: Dict[str, str] = {
    "title": "Noma'lum film",
    "countries": "Noma'lum",
    "year": "Noma'lum",
    "rating": "Noma'lum",
    "genres": "#film",
    "director": "Noma'lum",
    "cast": "Noma'lum",
    "description": "Film haqida qisqa ma'lumot aniqlanmadi.\nAdmin poster va treyler ma'lumotlariga qarab matnni qo'lda to'ldirishi mumkin.\nSyujet tafsilotlari tasdiqlangandan keyin e'lon qiling.",
}


def _cleanup_ai_sessions() -> None:
    now = time.time()
    expired = [k for k, v in ai_channel_session.items() if now - float((v or {}).get("ts", 0)) > AI_SESSION_TTL]
    for k in expired:
        ai_channel_session.pop(k, None)
    if len(ai_poster_cache) > 50:
        for k in list(ai_poster_cache.keys())[:-50]:
            ai_poster_cache.pop(k, None)


def _safe_literal_list(value: Any) -> Optional[List[Any]]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not ((text.startswith("[") and text.endswith("]")) or (text.startswith("(") and text.endswith(")"))):
        return None
    try:
        parsed = ast.literal_eval(text)
    except Exception:
        return None
    if isinstance(parsed, (list, tuple, set)):
        return list(parsed)
    return None


def clean_text_output(value: Any) -> str:
    parts = _safe_literal_list(value)
    if parts is not None:
        text = ", ".join(clean_text_output(part) for part in parts if clean_text_output(part))
    elif isinstance(value, dict):
        text = ", ".join(clean_text_output(v) for v in value.values() if clean_text_output(v))
    else:
        text = str(value or "")

    text = text.replace("#_#", "#")
    text = text.replace("_", " ")
    text = text.replace("`", "")
    text = text.replace("**", "")
    text = re.sub(r"^[\s\[\]{}()'\"]+|[\s\[\]{}()'\"]+$", "", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_country(value: Any) -> str:
    parts = _safe_literal_list(value)
    if parts is None:
        text = clean_text_output(value)
        parts = [p for p in re.split(r"\s*,\s*", text) if p.strip()]

    country_map = {
        "usa": "AQSH", "us": "AQSH", "u.s.a": "AQSH", "united states": "AQSH", "america": "AQSH",
        "uk": "Buyuk Britaniya", "united kingdom": "Buyuk Britaniya", "great britain": "Buyuk Britaniya",
        "russia": "Rossiya", "south korea": "Janubiy Koreya", "korea": "Janubiy Koreya",
        "japan": "Yaponiya", "china": "Xitoy", "india": "Hindiston", "turkey": "Turkiya",
    }
    cleaned = []
    for part in parts:
        name = clean_text_output(part)
        key = re.sub(r"[^a-z\s.]", "", name.lower()).strip()
        cleaned.append(country_map.get(key, name))
    cleaned = [part for part in cleaned if part]
    unique: List[str] = []
    for part in cleaned:
        if part not in unique:
            unique.append(part)
    return ", ".join(unique) or AI_DEFAULT_METADATA["countries"]


def _normalize_people(value: Any, default: str) -> str:
    parts = _safe_literal_list(value)
    if parts is None:
        text = clean_text_output(value)
        parts = [p for p in re.split(r"\s*,\s*|\s+va\s+|\s+and\s+", text, flags=re.IGNORECASE) if p.strip()]

    cleaned: List[str] = []
    noise = re.compile(r"\b(actors?|cast|starring|director|rejissyor|bosh rollar|rollarda|noma'lum|unknown)\b", re.IGNORECASE)
    for part in parts:
        name = noise.sub(" ", clean_text_output(part))
        name = re.sub(r"[^0-9A-Za-zА-Яа-яЁёІіЇїЄєҒғҚқҲҳЎўʻ‘’'\.\-\s]", " ", name)
        name = re.sub(r"\s+", " ", name).strip(" -,.:")
        if name and name.lower() not in {"n/a", "none", "null"} and name not in cleaned:
            cleaned.append(name)
    return ", ".join(cleaned[:8]) or default


def normalize_cast(value: Any) -> str:
    return _normalize_people(value, AI_DEFAULT_METADATA["cast"])


def normalize_director(value: Any) -> str:
    return _normalize_people(value, AI_DEFAULT_METADATA["director"])


def normalize_genres(value: Any) -> str:
    parts = _safe_literal_list(value)
    if parts is None:
        text = clean_text_output(value)
        text = text.replace("#_#", " ")
        text = text.replace("#", " #")
        if "," in text:
            parts = [p for p in text.split(",") if p.strip()]
        else:
            parts = [p for p in re.split(r"\s+", text) if p.strip()]

    tags: List[str] = []
    for part in parts:
        tag = clean_text_output(part).lower()
        tag = tag.replace("#", " ")
        tag = tag.replace("_", " ")
        tag = re.sub(r"[^0-9a-zа-яёіїєғқҳўʻ‘’'\s-]", " ", tag, flags=re.IGNORECASE)
        tag = re.sub(r"[ʻ‘’']", "", tag)
        words = [w for w in re.split(r"\s+", tag) if w]
        if not words:
            continue
        for word in words:
            word = re.sub(r"^-+|-+$", "", word)
            if word:
                tags.append(f"#{word}")

    unique: List[str] = []
    for tag in tags:
        if tag not in unique:
            unique.append(tag)
    return " ".join(unique[:6]) or AI_DEFAULT_METADATA["genres"]


def _normalize_ai_metadata(raw: Any) -> Dict[str, str]:
    meta: Dict[str, Any] = dict(AI_DEFAULT_METADATA)
    if isinstance(raw, dict):
        for key in meta:
            value = raw.get(key)
            if value is not None and clean_text_output(value):
                meta[key] = value

    meta["title"] = clean_text_output(meta.get("title")) or AI_DEFAULT_METADATA["title"]
    meta["countries"] = normalize_country(meta.get("countries"))
    meta["year"] = clean_text_output(meta.get("year")) or AI_DEFAULT_METADATA["year"]
    meta["rating"] = clean_text_output(meta.get("rating")) or AI_DEFAULT_METADATA["rating"]
    meta["genres"] = normalize_genres(meta.get("genres"))
    meta["director"] = normalize_director(meta.get("director"))
    meta["cast"] = normalize_cast(meta.get("cast"))

    desc = clean_text_output(meta.get("description"))
    desc_lines = [line.strip() for line in desc.splitlines() if line.strip()]
    if not desc_lines:
        desc_lines = AI_DEFAULT_METADATA["description"].splitlines()
    meta["description"] = "\n".join(desc_lines[:4])
    return {key: str(value) for key, value in meta.items()}


def parse_title_from_manual_caption(caption: str) -> str:
    text = clean_text_output(caption)
    if not text:
        return ""

    for line in (caption or "").splitlines():
        cleaned = clean_text_output(line)
        m = re.search(r"(?:🎬\s*)?(?:nomi|film nomi|title)\s*[:\-]\s*(.+)", cleaned, flags=re.IGNORECASE)
        if m:
            return normalize_detected_title(m.group(1))

    for line in text.splitlines() if "\n" in text else re.split(r"[\n\r]+", caption or ""):
        cleaned = clean_text_output(line)
        if cleaned and not cleaned.startswith(("🗣", "🌍", "🎭", "📅", "⭐", "🎞", "📃", "👍", "💡", "#")):
            return normalize_detected_title(cleaned)
    return normalize_detected_title(text)


def normalize_detected_title(title: str) -> str:
    text = clean_text_output(title)
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\{[^}]+\}", " ", text)
    text = re.sub(r"[|/]+.*$", " ", text)
    text = re.sub(r"\b(official|poster|trailer|treyler|teaser|uzbek|o'zbek|uzbekcha|tarjima|dublyaj|kino|film|full|hd|4k|1080p|720p|2160p|webrip|bluray|hdrip)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(uzmovi|uzmovie|asilmedia|tas-ix|premyera|premiere|yangi|kanalimizda|tomosha|ko'ring|korish)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+\s*[- ]?qism\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(season|mavsum|episode|seriya)\s*\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bchapter\s+([ivxlcdm]+|\d+)\b", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpart\s+([ivxlcdm]+|\d+)\b", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\((19|20)\d{2}\)", " ", text)
    text = re.sub(r"\([^)]*(?:mavsum|season|qism|episode|seriya|trailer|treyler)[^)]*\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -:|/")
    return text or AI_DEFAULT_METADATA["title"]


def _safe_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return {}
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def metadata_lookup_by_title(title: str) -> Dict[str, str]:
    normalized_title = normalize_detected_title(title)
    year_match = re.search(r"\b(19|20)\d{2}\b", clean_text_output(title))
    metadata = {"title": normalized_title}
    if year_match:
        metadata["year"] = year_match.group(0)
    return _normalize_ai_metadata(metadata)


def verify_metadata_confidence(metadata: Dict[str, str], reference: Optional[Dict[str, str]] = None) -> str:
    meta = _normalize_ai_metadata(metadata)
    if not meta.get("title") or meta.get("title") == AI_DEFAULT_METADATA["title"]:
        return "low"
    known = sum(1 for key in ("year", "countries", "genres", "director", "cast") if meta.get(key) and meta.get(key) != AI_DEFAULT_METADATA.get(key))
    if reference:
        ref = _normalize_ai_metadata(reference)
        if ref.get("year") != AI_DEFAULT_METADATA["year"] and meta.get("year") != AI_DEFAULT_METADATA["year"] and ref.get("year") != meta.get("year"):
            return "low"
    if known >= 3:
        return "high"
    if known >= 1:
        return "medium"
    return "medium"


async def admin_preview_if_uncertain(call: types.CallbackQuery, session_data: Dict[str, Any]) -> None:
    confidence = str(session_data.get("confidence", "medium")).lower()
    title = str(session_data.get("title", "Noma'lum"))
    if confidence == "low":
        await call.answer(f"⚠️ Film nomi taxmin qilindi: {title}\nIshonchlilik past. Kerak bo‘lsa qo‘lda yozing.", show_alert=True)
    elif confidence == "medium":
        await call.answer(f"ℹ️ Film nomi taxmin qilindi: {title}\nIshonchlilik o‘rtacha.", show_alert=True)


async def fallback_vision_if_missing(message: types.Message, metadata: Dict[str, str]) -> Tuple[Dict[str, str], Optional[str], bool]:
    meta = _normalize_ai_metadata(metadata)
    if meta.get("title") and meta.get("title") != AI_DEFAULT_METADATA["title"]:
        return meta, None, False
    return await _metadata_from_poster_message(message)


def manual_session_cache(chat_id: int, message_id: int, caption: str) -> Dict[str, str]:
    title = parse_title_from_manual_caption(caption)
    metadata = metadata_lookup_by_title(title or caption)
    metadata["title"] = title or metadata.get("title") or AI_DEFAULT_METADATA["title"]
    _save_ai_metadata(chat_id, message_id, metadata, source="manual", confidence=verify_metadata_confidence(metadata))
    return metadata


def source_priority_handler(chat_id: int, candidate: Dict[str, str], source: str = "ai") -> Dict[str, str]:
    session = ai_channel_session.get(int(chat_id))
    if isinstance(session, dict) and session.get("source") == "manual" and isinstance(session.get("metadata"), dict):
        return session["metadata"]
    meta = _normalize_ai_metadata(candidate)
    return meta


def _ai_session(chat_id: int) -> Dict[str, Any]:
    session = ai_channel_session.get(int(chat_id))
    if not isinstance(session, dict):
        session = {"metadata": dict(AI_DEFAULT_METADATA), "ts": time.time(), "episodes": {}}
        ai_channel_session[int(chat_id)] = session
    session.setdefault("episodes", {})
    return session


def _ai_update_session(chat_id: int, **kwargs: Any) -> Dict[str, Any]:
    session = _ai_session(chat_id)
    session.update(kwargs)
    session["ts"] = time.time()
    return session


def _ai_reset_session_for_new_poster(chat_id: int, message_id: int) -> None:
    ai_channel_session[int(chat_id)] = {"poster_message_id": int(message_id), "ts": time.time(), "episodes": {}, "ai_phase": "awaiting_type"}


def _publish_confirm_kb(kind: str, code: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if kind == "series":
        kb.add(
            types.InlineKeyboardButton("✅ Yuborish", callback_data=f"publish_series:{code}"),
            types.InlineKeyboardButton("❌ Yubormaslik", callback_data="cancel_send")
        )
    else:
        kb.add(
            types.InlineKeyboardButton("✅ Yuborish", callback_data=f"publish_movie:{code}"),
            types.InlineKeyboardButton("❌ Yubormaslik", callback_data="cancel_send")
        )
    return kb


async def _send_ai_publish_prompt(kind: str, code: str) -> None:
    if kind == "series":
        text = f"📺 Serial tayyorlandi.\n🆔 Kod: {code}\n📤 Kanalga yuborasizmi?"
    else:
        text = f"🎬 Film tayyorlandi.\n🆔 Kod: {code}\n📤 Kanalga yuborasizmi?"
    await bot.send_message(ADMIN_ID, text, reply_markup=_publish_confirm_kb(kind, code))


def ai_finalize_movie_session(message: types.Message, metadata: Dict[str, str]) -> Tuple[bool, str, Optional[str]]:
    return False, "❌ AI save/finalize o‘chirilgan", None


def _ai_store_series_episode(message: types.Message) -> Tuple[bool, str]:
    return False, "❌ AI save/finalize o‘chirilgan"


def ai_finalize_series_session(chat_id: int) -> Tuple[bool, str, Optional[str]]:
    return False, "❌ AI save/finalize o‘chirilgan", None


def _ai_main_post(meta: Dict[str, str]) -> str:
    meta = _normalize_ai_metadata(meta)
    return (
        f"🎬 Nomi: {meta['title']}\n"
        "🗣 Tili: o‘zbekcha\n"
        f"🌍 Davlat: {meta['countries']}\n"
        f"🎭 Janr: {meta['genres']}\n\n"
        "💡 Film haqida ko'proq bilish uchun \"Treyler va ma'lumotlar\" tugmasidan foydalaning\n\n"
        "👍 Layk bosishni unutmang"
    )


def _ai_trailer_post(meta: Dict[str, str]) -> str:
    meta = _normalize_ai_metadata(meta)
    return (
        f"🎬 Nomi: {meta['title']}\n"
        f"🌍 Davlat: {meta['countries']}\n"
        f"📅 Yili: {meta['year']}\n"
        f"⭐️ Reyting: {meta['rating']} / 10\n"
        f"🎭 Janr: {meta['genres']}\n"
        f"🎞 Rejissyor: {meta['director']}\n"
        f"🎭 Bosh rollar: {meta['cast']}\n\n"
        f"📃 Tasnif: {meta['description']}\n\n"
        "👍 Layk bosishni unutmang"
    )


def _current_ai_metadata(chat_id: int) -> Optional[Dict[str, str]]:
    _cleanup_ai_sessions()
    session = ai_channel_session.get(int(chat_id))
    if not isinstance(session, dict):
        return None
    return session.get("metadata") if isinstance(session.get("metadata"), dict) else None


async def _resolve_ai_metadata_for_message(message: types.Message, allow_vision: bool = False) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    metadata = _current_ai_metadata(message.chat.id)
    if metadata:
        meta = _normalize_ai_metadata(metadata)
        if meta.get("title") and meta.get("title") != AI_DEFAULT_METADATA["title"]:
            return meta, None

    title = parse_title_from_manual_caption(message.caption or "")
    if title and title != AI_DEFAULT_METADATA["title"]:
        fallback = metadata_lookup_by_title(title)
        _save_ai_metadata(message.chat.id, message.message_id, fallback, source="fallback", confidence=verify_metadata_confidence(fallback))
        return fallback, None

    if allow_vision and message.photo:
        meta, error_text, _ = await fallback_vision_if_missing(message, metadata or {})
        return meta, error_text

    return None, 'Cached title topilmadi. Avval poster ostidagi "🎬 Film postini yozish" tugmasini bosing. Vision qayta chaqirilmadi.'


def _save_ai_metadata(chat_id: int, message_id: int, metadata: Dict[str, str], source: str = "ai", confidence: Optional[str] = None) -> None:
    meta = _normalize_ai_metadata(metadata)
    confidence = confidence or verify_metadata_confidence(meta)
    old_session = ai_channel_session.get(int(chat_id))
    preserved = dict(old_session) if isinstance(old_session, dict) else {}
    session_data = {
        "source": source,
        "title": meta.get("title", ""),
        "normalized_title": normalize_detected_title(meta.get("title", "")),
        "year": meta.get("year", ""),
        "country": meta.get("countries", ""),
        "genres": meta.get("genres", ""),
        "director": meta.get("director", ""),
        "cast": meta.get("cast", ""),
        "description": meta.get("description", ""),
        "confidence": confidence,
        "timestamp": time.time(),
    }
    ai_poster_cache[int(message_id)] = meta
    preserved.update({"metadata": meta, "poster_message_id": int(message_id), "ts": time.time(), "source": source, "confidence": confidence, "session_data": session_data})
    ai_channel_session[int(chat_id)] = preserved
    _cleanup_ai_sessions()


async def _notify_admin(text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        print("ADMIN NOTIFY ERROR:", e)


async def _download_photo_data_url(photo: types.PhotoSize) -> Tuple[Optional[str], Optional[str]]:
    try:
        file = await bot.get_file(photo.file_id)
        bio = io.BytesIO()
        await bot.download_file(file.file_path, destination=bio)
        encoded = base64.b64encode(bio.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}", None
    except Exception as e:
        print("AI PHOTO DOWNLOAD ERROR:", e)
        return None, "Posterni Telegramdan yuklab olishda xatolik bo‘ldi. Manual workflow saqlanadi."


async def _openai_poster_metadata(image_data_url: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if not OPENAI_API_KEY:
        return None, "OPENAI_API_KEY Railway Variables ichida topilmadi. AI Vision ishlamadi."

    prompt = (
        "Poster rasmdagi film/serialni aniqlang. Faqat JSON qaytaring. "
        "Kalitlar: title, countries, year, rating, genres, director, cast, description. "
        "title faqat asosiy nom bo'lsin: subtitr, sifat, treyler, yil, qism, mavsum, reklama so'zlarini olib tashlang. "
        "countries davlat nomlari normal yozilsin. genres hashtag ko'rinishida bo'lsin. "
        "cast faqat aktyorlar ismlari bo'lsin. description 3-4 qatorlik o'zbekcha, spoilersiz bo'lsin. "
        "Aniq bo'lmagan maydonlarga Noma'lum yozing."
    )
    payload = {
        "model": OPENAI_MODEL,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": image_data_url, "detail": "low"},
            ],
        }],
        "text": {"format": {"type": "json_object"}},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=35)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                if resp.status >= 400:
                    err_text = await resp.text()
                    print("OPENAI ERROR:", resp.status, err_text)
                    if resp.status == 401:
                        return None, "OpenAI API kaliti noto‘g‘ri yoki ruxsat yo‘q."
                    if resp.status == 429:
                        return None, "OpenAI limit yoki balans/token muammosi bo‘lishi mumkin. Iltimos Railway/OpenAI hisobingizni tekshiring."
                    return None, f"OpenAI Vision xatolik qaytardi: HTTP {resp.status}."
                data = await resp.json()

        text = data.get("output_text")
        if not text:
            chunks: List[str] = []
            for item in data.get("output", []) or []:
                for content in item.get("content", []) or []:
                    if content.get("type") == "output_text" and content.get("text"):
                        chunks.append(content["text"])
            text = "".join(chunks)
        if not text:
            return None, "OpenAI javob berdi, lekin matn/metadata qaytmadi."
        parsed = _safe_json_object(text)
        if not parsed:
            return None, "OpenAI JSON javobini xavfsiz tahlil qilib bo‘lmadi."
        return _normalize_ai_metadata(parsed), None
    except asyncio.TimeoutError:
        return None, "OpenAI Vision javobi kechikdi (timeout). Manual workflow saqlanadi."
    except Exception as e:
        print("OPENAI PARSE ERROR:", e)
        return None, "OpenAI javobini o‘qish yoki tahlil qilishda xatolik bo‘ldi."


async def _metadata_from_poster_message(message: types.Message) -> Tuple[Dict[str, str], Optional[str], bool]:
    cached = ai_poster_cache.get(int(message.message_id))
    if cached:
        return cached, None, True

    metadata = None
    error_text = None
    if message.photo:
        image_data_url, download_error = await _download_photo_data_url(message.photo[-1])
        if download_error:
            error_text = download_error
        elif image_data_url:
            metadata, error_text = await _openai_poster_metadata(image_data_url)
    else:
        error_text = "Poster rasmi topilmadi. AI Vision ishlamadi."

    from_openai = bool(metadata)
    if not metadata:
        metadata = _normalize_ai_metadata({"title": (message.caption or "").strip() or AI_DEFAULT_METADATA["title"]})
    metadata["title"] = normalize_detected_title(metadata.get("title", ""))
    metadata = source_priority_handler(message.chat.id, metadata, source="ai")
    _save_ai_metadata(message.chat.id, message.message_id, metadata, source="ai", confidence=verify_metadata_confidence(metadata))
    return metadata, error_text, from_openai


def _ai_video_caption(message: types.Message, meta: Dict[str, str]) -> str:
    meta = _normalize_ai_metadata(meta)
    title = normalize_detected_title(meta.get("title", ""))
    ep_num, _ = _parse_episode_caption(message.caption or "")
    if ep_num is not None:
        return f"{title} {ep_num}-qism"
    return title


async def _remove_inline_buttons(message: types.Message) -> None:
    try:
        await bot.edit_message_reply_markup(message.chat.id, message.message_id, reply_markup=None)
    except Exception:
        pass


def ai_content_type_kb(message_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎬 Film", callback_data=f"ai:type:movie:{message_id}"),
        types.InlineKeyboardButton("📺 Serial", callback_data=f"ai:type:series:{message_id}")
    )
    return kb


def _ai_content_type(chat_id: int) -> Optional[str]:
    session = ai_channel_session.get(int(chat_id))
    if isinstance(session, dict):
        ctype = session.get("content_type")
        if ctype in ("movie", "series"):
            return ctype
    return None


def ai_poster_kb(message_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🎬 Film", callback_data=f"ai:type:movie:{message_id}"),
        types.InlineKeyboardButton("📺 Serial", callback_data=f"ai:type:series:{message_id}")
    )
    return kb


def ai_poster_action_kb(message_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🎬 Film postini yozish", callback_data=f"ai:poster:{message_id}"),
        types.InlineKeyboardButton("✍️ Qo‘lda yozaman", callback_data=f"ai:manual:poster:{message_id}")
    )
    return kb


def ai_trailer_kb(message_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🎞 Treyler uchun post", callback_data=f"ai:trailer:{message_id}"),
        types.InlineKeyboardButton("✍️ Qo‘lda yozaman", callback_data=f"ai:manual:trailer:{message_id}")
    )
    return kb


def ai_name_kb(message_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📁 Nomlash", callback_data=f"ai:name:{message_id}"),
        types.InlineKeyboardButton("✍️ Qo‘lda yozaman", callback_data=f"ai:manual:name:{message_id}")
    )
    return kb


def ai_video_kb(message: types.Message) -> types.InlineKeyboardMarkup:
    session = ai_channel_session.get(int(message.chat.id))
    phase = session.get("ai_phase") if isinstance(session, dict) else None
    if phase == "awaiting_trailer":
        return ai_trailer_kb(message.message_id)

    caption = (message.caption or "").lower()
    if "treyler" in caption or "trailer" in caption:
        return ai_trailer_kb(message.message_id)

    ctype = _ai_content_type(message.chat.id)
    if ctype == "movie":
        return ai_name_kb(message.message_id)
    if ctype == "series":
        return ai_name_kb(message.message_id)

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🎬 Film", callback_data=f"ai:type:movie:{message.message_id}"),
        types.InlineKeyboardButton("📺 Serial", callback_data=f"ai:type:series:{message.message_id}"),
        types.InlineKeyboardButton("🎞 Treyler uchun post", callback_data=f"ai:trailer:{message.message_id}"),
        types.InlineKeyboardButton("📁 Nomlash", callback_data=f"ai:name:{message.message_id}"),
        types.InlineKeyboardButton("✍️ Qo‘lda yozaman", callback_data=f"ai:manual:video:{message.message_id}")
    )
    return kb

# ================== AUTPOST STORAGE ==================
def load_autopost() -> Dict[str, Any]:
    if not os.path.exists(AUTOPOST_FILE):
        return {"meta": {"daily_done_sent": {}}, "jobs": []}
    try:
        with open(AUTOPOST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # migrate old list format -> dict
            return {"meta": {"daily_done_sent": {}}, "jobs": data}
        if not isinstance(data, dict):
            return {"meta": {"daily_done_sent": {}}, "jobs": []}
        data.setdefault("meta", {"daily_done_sent": {}})
        data.setdefault("jobs", [])
        if not isinstance(data["jobs"], list):
            data["jobs"] = []
        if not isinstance(data["meta"], dict):
            data["meta"] = {"daily_done_sent": {}}
        data["meta"].setdefault("daily_done_sent", {})
        return data
    except Exception:
        return {"meta": {"daily_done_sent": {}}, "jobs": []}


def save_autopost(data: Dict[str, Any]) -> None:
    _atomic_write_json(AUTOPOST_FILE, data)


# ================== BASE CHANNEL AI BUTTONS ==================
@dp.channel_post_handler(content_types=types.ContentType.PHOTO)
async def base_channel_photo_ai(message: types.Message):
    try:
        if int(message.chat.id) != int(CHANNEL1_ID):
            return
        _ai_reset_session_for_new_poster(message.chat.id, message.message_id)
        last_error = None
        for attempt in range(2):
            try:
                await bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=ai_poster_kb(message.message_id)
                )
                return
            except Exception as e:
                last_error = e
                if attempt == 0:
                    await asyncio.sleep(0.5)
        raise last_error
    except Exception as e:
        print("AI POSTER BUTTON ERROR:", e)
        await _notify_admin(f"⚠️ AI poster tugmasini baza kanaldagi rasmga qo‘sha olmadi. Bot kanal postlarini tahrirlash huquqini tekshiring. Xato: {e}")


@dp.channel_post_handler(content_types=types.ContentType.VIDEO)
async def base_channel_video_ai(message: types.Message):
    try:
        if int(message.chat.id) != int(CHANNEL1_ID):
            return
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=ai_video_kb(message)
        )
    except Exception as e:
        print("AI VIDEO BUTTON ERROR:", e)
        await _notify_admin(f"⚠️ AI video tugmasini baza kanaldagi videoga qo‘sha olmadi. Bot kanal postlarini tahrirlash huquqini tekshiring. Xato: {e}")


@dp.callback_query_handler(lambda c: c.data.startswith("ai:type:"))
async def ai_choose_content_type(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return
    if not call.message or int(call.message.chat.id) != int(CHANNEL1_ID):
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    parts = call.data.split(":")
    if len(parts) < 4 or parts[2] not in ("movie", "series"):
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    content_type = parts[2]
    _ai_update_session(call.message.chat.id, content_type=content_type, ai_phase="awaiting_poster_post")

    try:
        if call.message.photo:
            _ai_update_session(call.message.chat.id, poster_file_id=call.message.photo[-1].file_id)
            await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=ai_poster_action_kb(call.message.message_id))
        elif call.message.video:
            await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=ai_video_kb(call.message))
    except Exception as e:
        print("AI TYPE CALLBACK ERROR:", e)
        await _notify_admin(f"⚠️ AI content type callback ishlamadi. Xato: {e}")
        await call.answer("❌ Tugmalarni yangilab bo‘lmadi. Bot kanal postini tahrirlash huquqini tekshiring.", show_alert=True)
        return

    await call.answer("🎬 Film rejimi tanlandi" if content_type == "movie" else "📺 Serial rejimi tanlandi", show_alert=True)


@dp.callback_query_handler(lambda c: c.data.startswith("ai:manual:"))
async def ai_manual_mode(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return
    if not call.message or int(call.message.chat.id) != int(CHANNEL1_ID):
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    await _remove_inline_buttons(call.message)
    action = call.data.split(":")[2] if len(call.data.split(":")) > 2 else ""
    title = parse_title_from_manual_caption(call.message.caption or "")
    if title:
        metadata = manual_session_cache(call.message.chat.id, call.message.message_id, call.message.caption or "")
        if call.message.photo:
            _ai_update_session(call.message.chat.id, poster_file_id=call.message.photo[-1].file_id, poster_caption=call.message.caption or "", ai_phase="awaiting_trailer")
        elif call.message.video and action == "trailer":
            _ai_update_session(call.message.chat.id, trailer={"from_chat_id": call.message.chat.id, "message_id": call.message.message_id}, trailer_caption=call.message.caption or "", ai_phase="awaiting_video")
        elif call.message.video and action == "name":
            _ai_update_session(call.message.chat.id, ai_phase="naming_done")
        await call.answer(f"✅ Manual title saqlandi: {metadata.get('title', title)}", show_alert=True)
    else:
        if call.message.video and action == "trailer":
            _ai_update_session(call.message.chat.id, trailer={"from_chat_id": call.message.chat.id, "message_id": call.message.message_id}, trailer_caption=call.message.caption or "", ai_phase="awaiting_video")
        await call.answer("✅ Qo‘lda yozish rejimi tanlandi. AI/Vision ishlamadi. Captiondan film nomi topilmadi.", show_alert=True)


@dp.callback_query_handler(lambda c: c.data.startswith("ai:poster:"))
async def ai_write_main_post(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return
    if not call.message or int(call.message.chat.id) != int(CHANNEL1_ID):
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    if not _ai_content_type(call.message.chat.id):
        await call.answer("❗ Avval content turini tanlang: 🎬 Film yoki 📺 Serial", show_alert=True)
        return

    metadata, error_text, _ = await _metadata_from_poster_message(call.message)
    caption = _ai_main_post(metadata)

    try:
        await bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=None
        )
    except Exception as e:
        print("AI MAIN CAPTION EDIT ERROR:", e)
        await _notify_admin(f"⚠️ AI poster caption edit error. Xato: {e}")
        await _remove_inline_buttons(call.message)
        await call.answer("❌ Poster captionini kanal postiga qo‘shib bo‘lmadi. Bot kanal postini tahrirlash huquqini tekshiring.", show_alert=True)
        return

    if call.message.photo:
        _ai_update_session(call.message.chat.id, poster_file_id=call.message.photo[-1].file_id, poster_caption=caption, ai_phase="awaiting_trailer")

    session = ai_channel_session.get(int(call.message.chat.id), {})
    confidence = str(session.get("confidence", "high")).lower() if isinstance(session, dict) else "high"
    if error_text:
        await call.answer(f"⚠️ {error_text}\nFallback caption kanal postiga qo‘yildi.", show_alert=True)
    elif confidence in ("low", "medium"):
        await admin_preview_if_uncertain(call, session.get("session_data", metadata) if isinstance(session, dict) else metadata)
    else:
        await call.answer("✅ Poster captioni kanal postiga qo‘yildi")


@dp.callback_query_handler(lambda c: c.data.startswith("ai:trailer:"))
async def ai_write_trailer_post(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return
    if not call.message or int(call.message.chat.id) != int(CHANNEL1_ID):
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    metadata, metadata_error = await _resolve_ai_metadata_for_message(call.message)
    if not metadata:
        await _notify_admin(f"⚠️ AI treyler metadata topilmadi. Xato: {metadata_error}")
        await call.answer(f"❌ {metadata_error}", show_alert=True)
        return

    try:
        await bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=_ai_trailer_post(metadata),
            parse_mode="HTML",
            reply_markup=None
        )
    except Exception as e:
        print("AI TRAILER CAPTION EDIT ERROR:", e)
        await _notify_admin(f"⚠️ AI treyler caption edit error. Xato: {e}")
        await _remove_inline_buttons(call.message)
        await call.answer("❌ Treyler captionini kanal postiga qo‘shib bo‘lmadi. Bot kanal postini tahrirlash huquqini tekshiring.", show_alert=True)
        return

    _ai_update_session(call.message.chat.id, trailer={"from_chat_id": call.message.chat.id, "message_id": call.message.message_id}, trailer_caption=_ai_trailer_post(metadata), ai_phase="awaiting_video")
    await call.answer("✅ Treyler captioni kanal postiga qo‘yildi")


@dp.callback_query_handler(lambda c: c.data.startswith("ai:name:"))
async def ai_name_video(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return
    if not call.message or int(call.message.chat.id) != int(CHANNEL1_ID):
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    metadata, metadata_error = await _resolve_ai_metadata_for_message(call.message)
    if not metadata:
        await _notify_admin(f"⚠️ AI nomlash metadata topilmadi. Xato: {metadata_error}")
        await call.answer(f"❌ {metadata_error}", show_alert=True)
        return

    content_type = _ai_content_type(call.message.chat.id)
    if not content_type:
        await call.answer("❗ Avval content turini tanlang: 🎬 Film yoki 📺 Serial", show_alert=True)
        return

    new_caption = _ai_video_caption(call.message, metadata)
    try:
        await bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=new_caption,
            parse_mode="HTML",
            reply_markup=None
        )
    except Exception as e:
        print("AI NAME ERROR:", e)
        await _notify_admin(f"⚠️ AI video nomlash callback error. Xato: {e}")
        await _remove_inline_buttons(call.message)
        await call.answer("❌ Videoni avtomatik nomlab bo‘lmadi. Captionni qo‘lda tahrirlashingiz mumkin.", show_alert=True)
        return

    _ai_update_session(call.message.chat.id, ai_phase="naming_done")
    if content_type == "series":
        await call.answer("✅ Qism captioni kanal postiga qo‘yildi")
        return

    await call.answer("✅ Film captioni kanal postiga qo‘yildi")


@dp.callback_query_handler(lambda c: c.data.startswith("ai:series_done:"))
async def ai_series_done(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return
    await call.answer("❌ AI save/finalize o‘chirilgan. Bazaga saqlash uchun eski admin workflowdan foydalaning.", show_alert=True)


def _ap_new_id(jobs: List[Dict[str, Any]]) -> str:
    # simple unique id
    while True:
        x = random.randint(1000, 9999)
        apid = f"AP-{x}"
        if all(j.get("id") != apid for j in jobs):
            return apid


def _parse_dt_local(s: str) -> Optional[datetime]:
    try:
        naive = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
        return TZ.localize(naive)
    except Exception:
        return None


def autopost_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Rejalashtirish", "📋 Rejalashtirilganlar")
    kb.row("✏️ Tahrirlash", "🗑 O‘chirish")
    kb.row("❌ Bekor qilish")
    return kb


def autopost_edit_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🕒 Vaqtni o‘zgartirish", callback_data="ap_edit_time"),
        types.InlineKeyboardButton("🎬 Kinoni almashtirish", callback_data="ap_edit_code"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data="ap_edit_cancel"),
    )
    return kb


def settings_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📣 Kanalga yuborish")
    kb.row("📭 Kanalga yuborilmaganlar")
    kb.row("❌ Bekor qilish")
    return kb


def _content_display_title(item: Dict[str, Any]) -> str:
    caption = item.get("post_caption") if item.get("type") == "movie" else item.get("poster_caption")
    title = parse_title_from_manual_caption(caption or "")
    return title or clean_text_output(item.get("title") or item.get("name")) or "Noma'lum"


def _content_display_emoji(item: Dict[str, Any]) -> str:
    return "📺" if item.get("type") == "series" else "🎬"

# ================== PUBLISH HELPERS ==================
async def publish_to_channel(code: str) -> Tuple[bool, str]:
    db = load_db()
    item = db.get(code)
    if not item:
        return False, "❌ Bunaqa kino o'zi yo'q"

    if item.get("channel_msg_id"):
        return False, "⚠️ Bu kino kanalda bor. Dublikat chiqarmaymiz."

    trailer = item.get("trailer")
    trailer_url = None

    # ================== 1. TREYLERNI 3K GA YUBORAMIZ ==================
    if isinstance(trailer, dict) and trailer.get("from_chat_id") and trailer.get("message_id"):
        try:
            msg_tr = await bot.copy_message(
                chat_id=CHANNEL3_ID,
                from_chat_id=trailer["from_chat_id"],
                message_id=trailer["message_id"]
            )

            trailer_url = f"https://t.me/c/{str(CHANNEL3_ID)[4:]}/{msg_tr.message_id}"

            # 🔥 DB ga ham saqlab qo‘yamiz
            trailer["channel_msg_id"] = msg_tr.message_id
            trailer["post_url"] = trailer_url
            item["trailer"] = trailer

        except Exception as e:
            print("TRAILER PUBLISH ERROR:", e)
            trailer_url = None

    # ================== 2. MOVIE ==================
    if item.get("type") == "movie":
        caption = safe_caption(
            f"{(item.get('post_caption') or '').strip()}\n\n🆔 Kod: {code}"
        )

        kb = channel_movie_kb(code, trailer_url)

        try:
            msg = await bot.send_photo(
                CHANNEL2_ID,
                item["post_file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception as e:
            print("MOVIE PUBLISH ERROR:", e)
            return False, "❌ Xatolik chiqdi qanaqadir"

        item["channel_msg_id"] = msg.message_id
        db[code] = item
        save_db(db)

        return True, "🚀 Kanalga keeetti"

    # ================== 3. SERIES ==================
    if item.get("type") == "series":
        caption = safe_caption(
            f"{(item.get('poster_caption') or '').strip()}\n\n🆔 Kod: {code}"
        )

        kb = channel_series_kb(code, trailer_url)

        try:
            msg = await bot.send_photo(
                CHANNEL2_ID,
                item["poster_file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception as e:
            print("SERIES PUBLISH ERROR:", e)
            return False, "❌ Xatolik chiqdi qanaqadir"

        item["channel_msg_id"] = msg.message_id
        db[code] = item
        save_db(db)

        return True, "🚀 Kanalga keeetti"

    return False, "❌ Topilmadi"
    
# ================== AUTPOST WATCHDOG ==================
async def autopost_loop():
    while True:
        try:
            data = load_autopost()
            jobs: List[Dict[str, Any]] = data.get("jobs", [])
            meta: Dict[str, Any] = data.get("meta", {})
            daily_done_sent: Dict[str, Any] = meta.get("daily_done_sent", {})

            now = datetime.now(TZ)
            changed = False

            # process due jobs
            for job in jobs:
                if job.get("status") not in (None, "pending"):
                    continue
                run_at = _parse_dt_local(job.get("run_at", ""))
                code = str(job.get("code", "")).strip()
                if not run_at or not code.isdigit():
                    job["status"] = "cancelled"
                    job["note"] = "bad job data"
                    changed = True
                    continue

                if run_at <= now:
                    ok, msg = await publish_to_channel(code)
                    job["status"] = "done" if ok else "skipped"
                    job["done_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    job["result"] = msg
                    changed = True

                    # Admin log
                    try:
                        if ok:
                            await bot.send_message(
                                ADMIN_ID,
                                f"🚀 Avtopost chiqdi\n\n🎬 Kod: {code}\n⏰ Vaqt: {run_at.strftime('%H:%M')}\n📣 Kanalga muvaffaqiyatli joylandi",
                            )
                        else:
                            await bot.send_message(
                                ADMIN_ID,
                                f"⚠️ Avtopost bekor qilindi\n\n🎬 Kod: {code}\nSabab: {msg}",
                            )
                    except Exception:
                        pass

            # daily completion check
            dates = set()
            for job in jobs:
                run_at = _parse_dt_local(job.get("run_at", ""))
                if run_at:
                    dates.add(run_at.strftime("%Y-%m-%d"))

            for d in sorted(dates):
                if str(daily_done_sent.get(d, "")).lower() == "true":
                    continue
                day_jobs = [j for j in jobs if (_parse_dt_local(j.get("run_at", "")) and _parse_dt_local(j.get("run_at", "")).strftime("%Y-%m-%d") == d)]
                if not day_jobs:
                    continue
                if all(j.get("status") in ("done", "skipped", "cancelled") for j in day_jobs):
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            "📅 Bugungi avtopostlar tugadi\n\nBugun rejalashtirilgan barcha kinolar tekshirildi va yakunlandi.",
                        )
                    except Exception:
                        pass
                    daily_done_sent[d] = True
                    meta["daily_done_sent"] = daily_done_sent
                    data["meta"] = meta
                    changed = True

            # cleanup
            if len(jobs) > 200:
                jobs_sorted = sorted(jobs, key=lambda j: j.get("created_at", ""))
                data["jobs"] = jobs_sorted[-200:]
                changed = True

            if changed:
                save_autopost(data)

        except Exception:
            pass

        await asyncio.sleep(20)

# ================== BEKOR (har qanday holatda) ==================
@dp.message_handler(lambda m: (m.text or "").strip() == "❌ Bekor qilish" or ("bekor" in (m.text or "").lower()), state="*")
async def cancel_anytime(message: types.Message, state: FSMContext):
    await state.finish()
    if is_admin(message.from_user.id):
        await message.answer("❎ Bekor qilindi", reply_markup=admin_menu())
    else:
        await message.answer("❎ Bekor qilindi", reply_markup=user_menu())

# ================== START ==================
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message, state: FSMContext):
    await state.finish()

    args = (message.get_args() or "").strip()

    if args.startswith("series_"):
        code = args.replace("series_", "").strip()
        if code.isdigit():
            await send_series_to_user(message.from_user.id, code)
            return

    if args.isdigit():
        message.text = args
        await search_movie(message)
        return

    if is_admin(message.from_user.id):
        await message.answer("👑 <b>Admin panel</b>", reply_markup=admin_menu())
    else:
        await message.answer("🎬 Kino kodini yuboring", reply_markup=user_menu())

# ================== QIDIRUV ==================
@dp.message_handler(lambda m: m.text == "🎬 Qidiruv")
async def search_btn(message: types.Message):
    kb = admin_menu() if is_admin(message.from_user.id) else user_menu()
    await message.answer("🔎 Kino kodini yuboring", reply_markup=kb)

# ================== KINO QO‘SHISH (YAKKA) ==================
@dp.message_handler(lambda m: m.text == "➕ Kino qo‘shish")
async def add_movie_btn(message: types.Message):
    if message.from_user.id not in ADMINS:
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await message.answer("📨 Rasm-pasimlarini tashang", reply_markup=admin_menu())
    await AddMovie.post.set()


@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddMovie.post)
async def add_post(message: types.Message, state: FSMContext):
    db = load_db()
    code = generate_unique_code(db)

    await state.update_data(
        code=code,
        post_file_id=message.photo[-1].file_id,
        post_caption=message.caption or ""
    )

    # 🆕 TREYLER BOSQICHI
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ Treyler yo‘q")

    await message.answer(
        f"🆔 <b>Kino kodi avtomatik berildi:</b> {code}\n\n🎞 Endi treyler yuboring yoki o'tkazib yuboring",
        reply_markup=kb
    )
    await AddMovie.trailer.set()

# ================== TREYLER ==================
@dp.message_handler(lambda m: m.text == "❌ Treyler yo‘q", state=AddMovie.trailer)
async def skip_trailer(message: types.Message, state: FSMContext):
    await state.update_data(trailer=None)

    await message.answer("🎥 Endi video tashang", reply_markup=admin_menu())
    await AddMovie.video.set()


@dp.message_handler(state=AddMovie.trailer, content_types=types.ContentType.ANY)
async def add_trailer_any(message: types.Message, state: FSMContext):

    # ❗ faqat 1K dan forward bo‘lishi shart
    if not await _is_forward_from_base(message):
        await message.answer("❗ Iltimos, treylerni <b>Kanal1 (baza)</b>dan forward qiling.", reply_markup=admin_menu())
        return

    try:
        trailer_data = {
            "from_chat_id": message.forward_from_chat.id,
            "message_id": message.forward_from_message_id
        }

        await state.update_data(trailer=trailer_data)

        await message.answer("🎥 Endi film videosini yuboring", reply_markup=admin_menu())
        await AddMovie.video.set()

    except Exception as e:
        print("TRAILER SAVE ERROR:", e)
        await message.answer("❌ Treylerni saqlashda xatolik bo‘ldi", reply_markup=admin_menu())

# ================== VIDEO ==================
@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddMovie.video)
async def add_video(message: types.Message, state: FSMContext):
    db = load_db()

    if _duplicate_video_exists(db, message.video.file_unique_id):
        await message.answer("❗ Bu kino borku", reply_markup=admin_menu())
        await state.finish()
        return

    data = await state.get_data()
    code = data["code"]

    db[code] = {
        "type": "movie",
        "post_file_id": data["post_file_id"],
        "post_caption": data["post_caption"],
        "video_file_id": message.video.file_id,
        "video_unique_id": message.video.file_unique_id,
        "channel_msg_id": None,
        "trailer": data.get("trailer")  # 🆕
    }
    save_db(db)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Yuborish", callback_data=f"publish_movie:{code}"),
        types.InlineKeyboardButton("❌ Yubormaslik", callback_data="cancel_send")
    )

    await message.answer(f"🎬 Film tayyorlandi.\n🆔 Kod: {code}\n📤 Kanalga yuborasizmi?", reply_markup=kb)
    await state.finish()

# ================== SERIAL QO‘SHISH ==================
@dp.message_handler(lambda m: m.text == "➕ Serial qo‘shish")
async def add_series_btn(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await message.answer("📨 Serial posteri (rasm + caption)ni yuboring", reply_markup=admin_menu())
    await AddSeries.poster.set()


@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddSeries.poster)
async def add_series_poster(message: types.Message, state: FSMContext):
    db = load_db()
    code = generate_unique_code(db)

    await state.update_data(
        code=code,
        poster_file_id=message.photo[-1].file_id,
        poster_caption=message.caption or "",
        episodes={}
    )

    # 🆕 TREYLER BOSQICHI
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ Treyler yo‘q")

    await message.answer(
        f"🆔 <b>Kino kodi avtomatik berildi:</b> {code}\n\n🎞 Endi treyler yuboring yoki o'tkazib yuboring",
        reply_markup=kb
    )
    await AddSeries.trailer.set()

# ================== TREYLER ==================
@dp.message_handler(lambda m: m.text == "❌ Treyler yo‘q", state=AddSeries.trailer)
async def skip_series_trailer(message: types.Message, state: FSMContext):
    await state.update_data(trailer=None)

    await message.answer(
        "Endi Kanal1 (baza)dan videoni forward qiling.\n"
        "Caption misol: <b>1 Yura davri 3</b>\n\n"
        "Tugatish uchun <b>Ha</b> deb yozing.",
        reply_markup=admin_menu()
    )
    await AddSeries.episodes.set()


@dp.message_handler(state=AddSeries.trailer, content_types=types.ContentType.ANY)
async def add_series_trailer_any(message: types.Message, state: FSMContext):

    # ❗ faqat 1K dan forward
    if not await _is_forward_from_base(message):
        await message.answer("❗ Iltimos, treylerni <b>Kanal1 (baza)</b>dan forward qiling.", reply_markup=admin_menu())
        return

    try:
        trailer_data = {
            "from_chat_id": message.forward_from_chat.id,
            "message_id": message.forward_from_message_id
        }

        await state.update_data(trailer=trailer_data)

        await message.answer(
            "Endi Kanal1 (baza)dan videoni forward qiling.\n"
            "Caption misol: <b>1 Yura davri 3</b>\n\n"
            "Tugatish uchun <b>Ha</b> deb yozing.",
            reply_markup=admin_menu()
        )
        await AddSeries.episodes.set()

    except Exception as e:
        print("SERIES TRAILER SAVE ERROR:", e)
        await message.answer("❌ Treylerni saqlashda xatolik bo‘ldi", reply_markup=admin_menu())
        
# ================== FINISH ==================
@dp.message_handler(lambda m: (m.text or "").strip().lower() == "ha", state=AddSeries.episodes)
async def add_series_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    episodes = data.get("episodes", {})

    if not episodes:
        await message.answer("❗ Hech bo‘lmasa bitta qism qo‘shing.", reply_markup=admin_menu())
        return

    db = load_db()
    code = data["code"]

    db[code] = {
        "type": "series",
        "poster_file_id": data["poster_file_id"],
        "poster_caption": data["poster_caption"],
        "episodes": episodes,
        "channel_msg_id": None,
        "trailer": data.get("trailer")  # 🆕
    }
    save_db(db)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Yuborish", callback_data=f"publish_series:{code}"),
        types.InlineKeyboardButton("❌ Yubormaslik", callback_data="cancel_send")
    )

    await message.answer(f"📺 Serial tayyorlandi.\n🆔 Kod: {code}\n📤 Kanalga yuborasizmi?", reply_markup=kb)
    await state.finish()


# ================== EPISODES ==================
@dp.message_handler(content_types=types.ContentType.VIDEO, state=AddSeries.episodes)
async def add_series_episode(message: types.Message, state: FSMContext):
    if not await _is_forward_from_base(message):
        await message.answer("❗ Iltimos, <b>Kanal1 (baza)</b>dan forward qiling.", reply_markup=admin_menu())
        return

    ep_num, ep_title = _parse_episode_caption(message.caption or "")
    if ep_num is None:
        await message.answer("❗ Video captionida qism raqami yo‘q.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
        return

    db = load_db()
    if _duplicate_video_exists(db, message.video.file_unique_id):
        await message.answer("❗ Bu kino borku", reply_markup=admin_menu())
        return

    data = await state.get_data()
    episodes: Dict[str, Any] = data.get("episodes", {})

    episodes[str(ep_num)] = {
        "video_file_id": message.video.file_id,
        "video_unique_id": message.video.file_unique_id,
        "title": (ep_title or "").strip()
    }

    await state.update_data(episodes=episodes)
    await message.answer(f"✅ Qabul qilindi: <b>{ep_num}-qisim</b>", reply_markup=admin_menu())


@dp.message_handler(state=AddSeries.episodes, content_types=types.ContentType.TEXT)
async def add_series_text_in_episodes(message: types.Message, state: FSMContext):
    await message.answer(
        "🎥 Kanal1 (baza)dan videoni forward qiling.\n"
        "Tugatish uchun <b>Ha</b> deb yozing.",
        reply_markup=admin_menu()
    )

# ================== KANALGA YUBORISH ==================
@dp.callback_query_handler(lambda c: c.data == "cancel_send")
async def cancel_send(call: types.CallbackQuery):
    await call.message.edit_text("❎ Yuborilmadi. Draft bot bazasida saqlandi.")
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("publish_movie:"))
async def publish_movie(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    ok, msg = await publish_to_channel(code)
    await call.message.edit_text(msg if ok else msg)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("publish_series:"))
async def publish_series(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    ok, msg = await publish_to_channel(code)
    await call.message.edit_text(msg if ok else msg)
    await call.answer()

# ================== QIDIRISH ==================
@dp.message_handler(lambda m: m.text and m.text.strip().isdigit())
async def search_movie(message: types.Message):
    kb = admin_menu() if is_admin(message.from_user.id) else user_menu()

    if not await check_subscription(message.from_user.id):
        await message.answer("❗ Avval kanalga obuna bo‘ling", reply_markup=subscribe_kb())
        return

    db = load_db()
    code = message.text.strip()
    item = db.get(code)

    if not item:
        await message.answer("❌ Bunday kodli kino topilmadi", reply_markup=kb)
        return

    update_stats(message.from_user.id)

    # ================= MOVIE =================
    if item.get("type") == "movie":
        token = str(random.randint(100000, 999999))
        last_movie_request[message.from_user.id] = code
        last_watch_token[message.from_user.id] = token

        kb_inline = movie_watch_kb(code, token)

        # 🔥 TREYLER TUGMA (faqat post_url orqali)
        trailer = item.get("trailer")
        if isinstance(trailer, dict) and trailer.get("post_url"):
            kb_inline.add(
                types.InlineKeyboardButton(
                    "🎞 Treyler va ma'lumotlar",
                    url=trailer.get("post_url")
                )
            )

        await message.answer_photo(
            item["post_file_id"],
            safe_caption(item.get("post_caption", "")),
            reply_markup=kb_inline,
            protect_content=protect_for(message.from_user.id),
            parse_mode="HTML"
        )
        return

    # ================= SERIES =================
    kb_inline = types.InlineKeyboardMarkup()
    kb_inline.add(
        types.InlineKeyboardButton("📺 Barcha qismlari", callback_data=f"series_private:{code}")
    )

    # 🔥 TREYLER TUGMA (faqat post_url orqali)
    trailer = item.get("trailer")
    if isinstance(trailer, dict) and trailer.get("post_url"):
        kb_inline.add(
            types.InlineKeyboardButton(
                "🎞 Treyler va ma'lumotlar",
                url=trailer.get("post_url")
            )
        )

    await message.answer_photo(
        item["poster_file_id"],
        safe_caption(item.get("poster_caption", "")),
        reply_markup=kb_inline,
        protect_content=protect_for(message.from_user.id),
        parse_mode="HTML"
    )

# ================== FILMNI KO‘RISH (YAKKA) ==================

@dp.callback_query_handler(lambda c: c.data.startswith("watch_"))
async def watch_old(call: types.CallbackQuery):
    await call.answer(
        "❗️ Tugma eskirgan. Faqat oxirgi so'ralgan filmni ko'rishingiz mumkin. "
        "Ushbu filmni ko'rish uchun esa kod orqali qayta qidiring yoki "
        "kanaldagi bu film posti ostidagi ko'rish tugmasini bosing ",
        show_alert=True
    )


@dp.callback_query_handler(lambda c: c.data.startswith("watch2_"))
async def watch_movie(call: types.CallbackQuery):

    try:
        parts = call.data.split("_", 2)
        if len(parts) != 3:
            await call.answer("❌ Topilmadi", show_alert=True)
            return

        code = parts[1]
        token = parts[2]

        # 🔒 TOKEN TEKSHIRISH
        if last_movie_request.get(call.from_user.id) != code or last_watch_token.get(call.from_user.id) != token:
            await call.answer(
                "❗️ Tugma eskirgan. Faqat oxirgi so'ralgan filmni ko'rishingiz mumkin. "
                "Ushbu filmni ko'rish uchun esa kod orqali qayta qidiring yoki "
                "kanaldagi bu film posti ostidagi ko'rish tugmasini bosing ",
                show_alert=True
            )
            return

        # 🔒 OBUNA
        if not await check_subscription(call.from_user.id):
            await call.message.answer("❗️ Avval kanalga obuna bo‘lingda", reply_markup=subscribe_kb())
            await call.answer()
            return

        db = load_db()
        item = db.get(code)

        if not item or item.get("type") != "movie":
            await call.answer("❌ Topilmadi", show_alert=True)
            return

        # 🔥 ENG MUHIM FIX
        video_id = item.get("video_file_id")
        if not video_id:
            await call.message.answer("❌ Video topilmadi", reply_markup=user_menu())
            await call.answer()
            return

        # 🎥 VIDEO YUBORISH
        await bot.send_video(
            chat_id=call.from_user.id,
            video=video_id,
            protect_content=protect_for(call.from_user.id)
        )

        # 🔄 TOKENNI O‘CHIRAMIZ
        last_watch_token.pop(call.from_user.id, None)

        await call.answer()

    except Exception as e:
        # 🔥 Railway jim qolmasligi uchun
        print("WATCH ERROR:", e)

        await call.message.answer("❌ Xatolik chiqdi qanaqadir", reply_markup=user_menu())
        await call.answer()

# ================== SERIALNI USERGA YUBORISH (kanalga emas) ==================

async def send_series_to_user(user_id: int, code: str):

    try:
        if not await check_subscription(user_id):
            await bot.send_message(user_id, "❗️ Avval kanalga obuna bo‘ling", reply_markup=subscribe_kb())
            return

        db = load_db()
        item = db.get(code)

        if not item or item.get("type") != "series":
            await bot.send_message(user_id, "❌ Bunday kodli kino topilmadi", reply_markup=user_menu())
            return

        ep_nums = _sorted_episode_numbers(item)
        if not ep_nums:
            await bot.send_message(user_id, "❌ Qismlar topilmadi", reply_markup=user_menu())
            return

        ch_msg_id = item.get("channel_msg_id")

        # 🔥 Kanal postini copy qilish
        if ch_msg_id:
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=CHANNEL2_ID,
                    message_id=ch_msg_id,
                    reply_markup=series_eps_kb(code, ep_nums),
                    protect_content=protect_for(user_id)
                )
                return
            except Exception as e:
                print("COPY ERROR:", e)

        # 🔁 fallback (agar copy ishlamasa)
        await bot.send_photo(
            chat_id=user_id,
            photo=item["poster_file_id"],
            caption=safe_caption(item.get("poster_caption", "")),
            reply_markup=series_eps_kb(code, ep_nums),
            protect_content=protect_for(user_id)
        )

    except Exception as e:
        print("SERIES SEND ERROR:", e)
        await bot.send_message(user_id, "❌ Xatolik chiqdi qanaqadir", reply_markup=user_menu())


# ================== CALLBACKLAR ==================

@dp.callback_query_handler(lambda c: c.data.startswith("series_private:"))
async def series_private_from_bot(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]
    await send_series_to_user(call.from_user.id, code)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("series_ep:"))
async def series_ep(call: types.CallbackQuery):

    try:
        _, code, ep_str = call.data.split(":")
        ep_num = int(ep_str)

        if not await check_subscription(call.from_user.id):
            await call.message.answer("❗️ Avval kanalga obuna bo‘ling", reply_markup=subscribe_kb())
            await call.answer()
            return

        db = load_db()
        item = db.get(code)

        if not item or item.get("type") != "series":
            await call.answer("❌ Topilmadi", show_alert=True)
            return

        ep = (item.get("episodes", {}) or {}).get(str(ep_num))

        if not ep:
            await call.answer("❌ Topilmadi", show_alert=True)
            return

        video_id = ep.get("video_file_id")
        if not video_id:
            await call.message.answer("❌ Video topilmadi", reply_markup=user_menu())
            await call.answer()
            return

        cap = _episode_user_caption(ep_num, (ep or {}).get("title", ""))

        await bot.send_video(
            chat_id=call.from_user.id,
            video=video_id,
            caption=cap,
            protect_content=protect_for(call.from_user.id)
        )

        await call.answer()

    except Exception as e:
        print("SERIES EP ERROR:", e)
        await call.message.answer("❌ Xatolik chiqdi qanaqadir", reply_markup=user_menu())
        await call.answer()

# ================== STATISTIKA ==================
def stats_text():
    db = load_db()

    movies_count = sum(1 for v in db.values() if v.get("type") == "movie")
    series_count = sum(1 for v in db.values() if v.get("type") == "series")

    # 🆕 treyler soni (film + serial ichida)
    trailers_count = sum(
        1 for v in db.values()
        if isinstance(v.get("trailer"), dict)
    )

    return (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"🎬 Filmlar: <b>{movies_count}</b>\n"
        f"📺 Seriallar: <b>{series_count}</b>\n"
        f"🎞 Treylerlar: <b>{trailers_count}</b>"
    )


def stats_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔄 Yangilash", callback_data="stats_refresh"),
        types.InlineKeyboardButton("❌ Yopish", callback_data="stats_close")
    )
    return kb


@dp.message_handler(lambda m: m.text == "📊 Statistika")
async def show_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await message.answer(stats_text(), reply_markup=stats_kb())


@dp.callback_query_handler(lambda c: c.data == "stats_refresh")
async def refresh_stats(call: types.CallbackQuery):
    await call.message.edit_text(stats_text(), reply_markup=stats_kb())
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "stats_close")
async def close_stats(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()

# ================== BACKUP ==================
@dp.message_handler(lambda m: m.text == "📦 Kino backup")
async def backup_movies(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    if not os.path.exists(MOVIES_FILE):
        await message.answer("❌ movies.json topilmadi", reply_markup=admin_menu())
        return
    await message.answer_document(types.InputFile(MOVIES_FILE), reply_markup=admin_menu())


@dp.message_handler(lambda m: m.text == "📈 Statistika backup")
async def backup_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    if not os.path.exists(STATS_FILE):
        await message.answer("❌ statistics.json topilmadi", reply_markup=admin_menu())
        return
    await message.answer_document(types.InputFile(STATS_FILE), reply_markup=admin_menu())

# ================== RESTORE (ADMIN PANEL) ==================
@dp.message_handler(lambda m: m.text == "♻️ Kino restore")
async def restore_movies_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer(
        "♻️ <b>Kino restore</b>\n\n"
        "📎 Endi <b>movies.json</b> faylni shu botga yuboring (Document sifatida).",
        reply_markup=admin_menu()
    )
    await RestoreFlow.movies.set()


@dp.message_handler(lambda m: m.text == "♻️ Statistika restore")
async def restore_stats_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer(
        "♻️ <b>Statistika restore</b>\n\n"
        "📎 Endi <b>statistics.json</b> faylni shu botga yuboring (Document sifatida).",
        reply_markup=admin_menu()
    )
    await RestoreFlow.stats.set()


@dp.message_handler(state=RestoreFlow.movies, content_types=types.ContentType.DOCUMENT)
async def restore_movies_file(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    doc = message.document
    if not doc or (doc.file_name or "").lower() != "movies.json":
        await message.answer("❗ Faqat <b>movies.json</b> yuboring.", reply_markup=admin_menu())
        return

    try:
        f = await bot.get_file(doc.file_id)
        _ensure_parent_dir(MOVIES_FILE)
        await bot.download_file(f.file_path, MOVIES_FILE)

        # 🆕 DB ni majburan reload + validate
        db = load_db()

        # 🆕 eski fayllarda trailer bo‘lmasa qo‘shib chiqamiz
        for code, item in db.items():
            if "trailer" not in item:
                item["trailer"] = None

        save_db(db)

        await message.answer(f"✅ Tiklandi!\n📌 Saqlandi: <code>{MOVIES_FILE}</code>", reply_markup=admin_menu())

    except Exception:
        await message.answer("❌ Restore bo‘lmadi. Fayl buzilgan yoki ruxsat muammosi bo‘lishi mumkin.", reply_markup=admin_menu())
    finally:
        await state.finish()


@dp.message_handler(state=RestoreFlow.stats, content_types=types.ContentType.DOCUMENT)
async def restore_stats_file(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    doc = message.document
    if not doc or (doc.file_name or "").lower() != "statistics.json":
        await message.answer("❗ Faqat <b>statistics.json</b> yuboring.", reply_markup=admin_menu())
        return

    try:
        f = await bot.get_file(doc.file_id)
        _ensure_parent_dir(STATS_FILE)
        await bot.download_file(f.file_path, STATS_FILE)
        _ = load_stats()
        await message.answer(f"✅ Tiklandi!\n📌 Saqlandi: <code>{STATS_FILE}</code>", reply_markup=admin_menu())
    except Exception:
        await message.answer("❌ Restore bo‘lmadi. Fayl buzilgan yoki ruxsat muammosi bo‘lishi mumkin.", reply_markup=admin_menu())
    finally:
        await state.finish()


@dp.message_handler(state=RestoreFlow.movies)
async def restore_movies_wait(message: types.Message):
    await message.answer("📎 Iltimos, <b>movies.json</b> faylni Document qilib yuboring.", reply_markup=admin_menu())


@dp.message_handler(state=RestoreFlow.stats)
async def restore_stats_wait(message: types.Message):
    await message.answer("📎 Iltimos, <b>statistics.json</b> faylni Document qilib yuboring.", reply_markup=admin_menu())

# ================== O‘CHIRISH ==================
@dp.message_handler(lambda m: m.text == "🗑 O‘chirish")
async def del_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer("🗑 Koddi ayting", reply_markup=admin_menu())
    await DeleteFlow.code.set()


@dp.message_handler(state=DeleteFlow.code)
async def delete_item(message: types.Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🗑 Koddi ayting", reply_markup=admin_menu())
        return

    db = load_db()
    item = db.get(code)
    if not item:
        await message.answer("❌ Bunaqa kino o'zi yo'q", reply_markup=admin_menu())
        await state.finish()
        return

    # 🔴 2K POSTNI O‘CHIRISH
    msg_id = item.get("channel_msg_id")
    if msg_id:
        try:
            await bot.delete_message(CHANNEL2_ID, msg_id)
        except Exception:
            pass

    # 🔴 3K TREYLERNI O‘CHIRISH
    trailer = item.get("trailer")
    if isinstance(trailer, dict):
        trailer_msg_id = trailer.get("channel_msg_id")
        if trailer_msg_id:
            try:
                await bot.delete_message(CHANNEL3_ID, trailer_msg_id)
            except Exception:
                pass

    del db[code]
    save_db(db)

    await message.answer(f"🗑 O'chirib tashadim\n🆔 Kod: {code}", reply_markup=admin_menu())
    await state.finish()

# ================== TAHRIRLASH ==================
def edit_type_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎬 Yakka film", callback_data="edit_type:movie"),
        types.InlineKeyboardButton("📺 Serial", callback_data="edit_type:series"),
    )
    return kb

def edit_movie_kb(code: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("♻️ Kanal1 postni yuboring", callback_data=f"edit_movie_post:{code}"),
        types.InlineKeyboardButton("🎥 Kanal1 video yuboring", callback_data=f"edit_movie_video:{code}"),
        types.InlineKeyboardButton("🗑 O‘chirish", callback_data=f"edit_delete:{code}")
    )
    return kb

def edit_series_kb(code: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("♻️ Kanal1 postni yuboring", callback_data=f"edit_series_post:{code}"),
        types.InlineKeyboardButton("➕ Yangi qism (video yuboring)", callback_data=f"series_add:{code}"),
        types.InlineKeyboardButton("🔁 Qismni almashtirish (video yuboring)", callback_data=f"series_replace:{code}"),
        types.InlineKeyboardButton("🗑 Qismni o‘chirish", callback_data=f"series_del:{code}"),
        types.InlineKeyboardButton("🗑 Serialni o‘chirish", callback_data=f"edit_delete:{code}")
    )
    return kb

@dp.message_handler(lambda m: m.text == "✏️ Tahrirlash")
async def edit_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer("Nimani tahrirlaymiz?", reply_markup=edit_type_kb())
    await EditFlow.choose_type.set()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_type:"), state=EditFlow.choose_type)
async def edit_choose_type(call: types.CallbackQuery, state: FSMContext):
    typ = call.data.split(":", 1)[1]
    await state.update_data(edit_type=typ)
    await call.message.edit_text("🆔 Koddi ayting")
    await EditFlow.choose_code.set()
    await call.answer()

@dp.message_handler(state=EditFlow.choose_code)
async def edit_choose_code(message: types.Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Koddi ayting", reply_markup=admin_menu())
        return

    db = load_db()
    data = await state.get_data()
    typ = data.get("edit_type")
    item = db.get(code)

    if not item or item.get("type") != typ:
        await message.answer("❌ Bunaqa kino o'zi yo'q", reply_markup=admin_menu())
        await state.finish()
        return

    await state.update_data(code=code)
    if typ == "movie":
        await message.answer("🎬 Tahrirlash:", reply_markup=edit_movie_kb(code))
    else:
        await message.answer("📺 Tahrirlash:", reply_markup=edit_series_kb(code))
    await EditFlow.choose_action.set()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_movie_post:"), state=EditFlow.choose_action)
async def edit_movie_post(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("movie_post", code))
    await call.message.answer("♻️ Kanal1 (baza)dagi <b>yangilangan postni</b> forward qiling.", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_movie_video:"), state=EditFlow.choose_action)
async def edit_movie_video(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("movie_video", code))
    await call.message.answer("🎥 Kanal1 (baza)dagi <b>yangilangan videoni</b> forward qiling.", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_series_post:"), state=EditFlow.choose_action)
async def edit_series_post(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_post", code))
    await call.message.answer("♻️ Kanal1 (baza)dagi <b>yangilangan poster postni</b> forward qiling.", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("series_add:"), state=EditFlow.choose_action)
async def edit_series_add(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_add", code))
    await call.message.answer("➕ Kanal1 dan videoni forward qiling.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("series_replace:"), state=EditFlow.choose_action)
async def edit_series_replace(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_replace", code))
    await call.message.answer("🔁 Kanal1 dan videoni forward qiling.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
    await EditFlow.await_forward.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("series_del:"), state=EditFlow.choose_action)
async def edit_series_del(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    await state.update_data(pending=("series_del", code))
    await call.message.answer("🗑 Qaysi qisimni o‘chiramiz? (raqam yuboring, masalan: 1)", reply_markup=admin_menu())
    await EditFlow.await_ep_delete.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_delete:"), state=EditFlow.choose_action)
async def edit_delete(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    db = load_db()
    item = db.get(code)
    if not item:
        await call.answer("❌ Topilmadi", show_alert=True)
        await state.finish()
        return

    msg_id = item.get("channel_msg_id")
    if msg_id:
        try:
            await bot.delete_message(CHANNEL2_ID, msg_id)
        except Exception:
            pass

    del db[code]
    save_db(db)
    await call.message.answer(f"🗑 O'chirib tashadim\n🆔 Kod: {code}", reply_markup=admin_menu())
    await state.finish()
    await call.answer()

@dp.message_handler(state=EditFlow.await_ep_delete)
async def edit_series_del_number(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("🗑 Qaysi qisimni o‘chiramiz? (raqam yuboring, masalan: 1)", reply_markup=admin_menu())
        return

    ep_num = int(text)
    data = await state.get_data()
    pending = data.get("pending")

    if not pending or pending[0] != "series_del":
        await message.answer("❎ Bekor qilindi", reply_markup=admin_menu())
        await state.finish()
        return

    code = pending[1]
    db = load_db()
    item = db.get(code)
    if not item or item.get("type") != "series":
        await message.answer("❌ Bunaqa kino o'zi yo'q", reply_markup=admin_menu())
        await state.finish()
        return

    eps = item.get("episodes", {}) or {}
    if str(ep_num) not in eps:
        await message.answer("❌ Bunaqa qisim yo'q", reply_markup=admin_menu())
        return

    del eps[str(ep_num)]
    item["episodes"] = eps
    db[code] = item
    save_db(db)

    await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
    await state.finish()

@dp.message_handler(state=EditFlow.await_forward, content_types=types.ContentType.ANY)
async def edit_receive_forward(message: types.Message, state: FSMContext):
    if not await _is_forward_from_base(message):
        await message.answer("❗ Iltimos, <b>Kanal1 (baza)</b>dan forward qiling.", reply_markup=admin_menu())
        return

    data = await state.get_data()
    pending = data.get("pending")
    if not pending:
        await message.answer("❎ Bekor qilindi", reply_markup=admin_menu())
        await state.finish()
        return

    action, code = pending
    db = load_db()
    item = db.get(code)

    if not item:
        await message.answer("❌ Bunaqa kino o'zi yo'q", reply_markup=admin_menu())
        await state.finish()
        return

    # 🔥 TREYLER URLNI OLDINDAN OLAMIZ (hamma joyda ishlatamiz)
    trailer = item.get("trailer")
    trailer_url = trailer.get("post_url") if isinstance(trailer, dict) else None

    # -------- movie_post --------
    if action == "movie_post":
        if message.content_type != types.ContentType.PHOTO:
            await message.answer("❗ Rasm (photo) forward qiling.", reply_markup=admin_menu())
            return

        new_photo = message.photo[-1].file_id
        new_caption = message.caption or ""

        ch_msg_id = item.get("channel_msg_id")
        if ch_msg_id:
            old_with_code = f"{(item.get('post_caption') or '').strip()}\n\n🆔 Kod: {code}"
            final_caption = _ensure_code_line_kept(new_caption, old_with_code, code)
            final_caption = _apply_edit_banner(final_caption, MOVIE_BANNER)
            try:
                media = types.InputMediaPhoto(media=new_photo, caption=final_caption, parse_mode="HTML")
                await bot.edit_message_media(
                    CHANNEL2_ID,
                    ch_msg_id,
                    media=media,
                    reply_markup=channel_movie_kb(code, trailer_url)  # 🔥 MUHIM
                )
            except Exception:
                try:
                    await bot.edit_message_caption(
                        CHANNEL2_ID,
                        ch_msg_id,
                        caption=final_caption,
                        reply_markup=channel_movie_kb(code, trailer_url)  # 🔥 MUHIM
                    )
                except Exception:
                    pass

        item["post_file_id"] = new_photo
        item["post_caption"] = new_caption
        db[code] = item
        save_db(db)

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    # -------- movie_video --------
    if action == "movie_video":
        if message.content_type != types.ContentType.VIDEO:
            await message.answer("❗ Video forward qiling.", reply_markup=admin_menu())
            return

        if _duplicate_video_exists(db, message.video.file_unique_id):
            await message.answer("❗ Bu kino borku", reply_markup=admin_menu())
            return

        item["video_file_id"] = message.video.file_id
        item["video_unique_id"] = message.video.file_unique_id
        db[code] = item
        save_db(db)

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    # -------- series_post --------
    if action == "series_post":
        if message.content_type != types.ContentType.PHOTO:
            await message.answer("❗ Rasm (photo) forward qiling.", reply_markup=admin_menu())
            return

        new_photo = message.photo[-1].file_id
        new_caption = message.caption or ""

        ch_msg_id = item.get("channel_msg_id")
        if ch_msg_id:
            old_with_code = f"{(item.get('poster_caption') or '').strip()}\n\n🆔 Kod: {code}"
            final_caption = _ensure_code_line_kept(new_caption, old_with_code, code)
            final_caption = _apply_edit_banner(final_caption, SERIES_BANNER)
            try:
                media = types.InputMediaPhoto(media=new_photo, caption=final_caption, parse_mode="HTML")
                await bot.edit_message_media(
                    CHANNEL2_ID,
                    ch_msg_id,
                    media=media,
                    reply_markup=channel_series_kb(code, trailer_url)  # 🔥 MUHIM
                )
            except Exception:
                try:
                    await bot.edit_message_caption(
                        CHANNEL2_ID,
                        ch_msg_id,
                        caption=final_caption,
                        reply_markup=channel_series_kb(code, trailer_url)  # 🔥 MUHIM
                    )
                except Exception:
                    pass

        item["poster_file_id"] = new_photo
        item["poster_caption"] = new_caption
        db[code] = item
        save_db(db)

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    # -------- series add/replace --------
    if action in ("series_add", "series_replace"):
        if message.content_type != types.ContentType.VIDEO:
            await message.answer("❗ Video forward qiling.", reply_markup=admin_menu())
            return

        ep_num, ep_title = _parse_episode_caption(message.caption or "")
        if ep_num is None:
            await message.answer("❗ Video captionida qism raqimi yo‘q.\nMasalan: <b>1 Yura davri 3</b>", reply_markup=admin_menu())
            return

        if _duplicate_video_exists(db, message.video.file_unique_id):
            await message.answer("❗ Bu kino borku", reply_markup=admin_menu())
            return

        eps = item.get("episodes", {}) or {}
        exists = str(ep_num) in eps

        if action == "series_add" and exists:
            await message.answer("❗ Bu qisim bor. Almashtirish tanlang.", reply_markup=admin_menu())
            return
        if action == "series_replace" and not exists:
            await message.answer("❗ Bu qisim yo'q. Yangi qisim qo‘shish tanlang.", reply_markup=admin_menu())
            return

        eps[str(ep_num)] = {
            "video_file_id": message.video.file_id,
            "video_unique_id": message.video.file_unique_id,
            "title": (ep_title or "").strip()
        }
        item["episodes"] = eps
        db[code] = item
        save_db(db)

        # 🔥 KANAL POSTNI HAM YANGILAYMIZ (tugma saqlanadi)
        ch_msg_id = item.get("channel_msg_id")
        if ch_msg_id:
            try:
                old_with_code = f"{(item.get('poster_caption') or '').strip()}\n\n🆔 Kod: {code}"
                final_caption = _ensure_code_line_kept(item.get("poster_caption") or "", old_with_code, code)
                final_caption = _apply_edit_banner(final_caption, SERIES_BANNER)
                await bot.edit_message_caption(
                    CHANNEL2_ID,
                    ch_msg_id,
                    caption=final_caption,
                    reply_markup=channel_series_kb(code, trailer_url)  # 🔥 MUHIM
                )
            except Exception:
                pass

        await message.answer("♻️ Yangilandi", reply_markup=edited_done_kb(code))
        await state.finish()
        return

    await message.answer("❎ Bekor qilindi", reply_markup=admin_menu())
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_again:"))
async def edit_again(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat siz admin emassiz 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    db = load_db()
    item = db.get(code)
    if not item:
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    if item.get("type") == "movie":
        await call.message.answer("🎬 Tahrirlash:", reply_markup=edit_movie_kb(code))
    else:
        await call.message.answer("📺 Tahrirlash:", reply_markup=edit_series_kb(code))
    await call.answer()

# ================== REPUBLISH ==================
@dp.callback_query_handler(lambda c: c.data.startswith("republish:"))
async def republish(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Brat, bu joy adminniki 😄", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    db = load_db()
    item = db.get(code)
    if not item:
        await call.answer("❌ Topilmadi", show_alert=True)
        return

    old_msg_id = item.get("channel_msg_id")
    if old_msg_id:
        try:
            await bot.delete_message(CHANNEL2_ID, old_msg_id)
        except Exception:
            pass
        item["channel_msg_id"] = None
        db[code] = item
        save_db(db)

    ok, msg = await publish_to_channel(code)
    try:
        await call.message.edit_text("♻️ Yangilandi")
    except Exception:
        pass
    await call.answer()


# ================== OBUNA TEKSHIR ==================
@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def recheck(call: types.CallbackQuery):
    if await check_subscription(call.from_user.id):
        try:
            await call.message.edit_text("✅ Obuna tasdiqlandi. Endi kino kodini yuboring.")
        except Exception:
            await call.message.answer("✅ Obuna tasdiqlandi. Endi kino kodini yuboring.")
    else:
        await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz 😕", show_alert=True)

# ================== KANALGA YUBORILMAGANLAR ==================
@dp.message_handler(lambda m: m.text == "⚙️ Sozlash")
async def settings_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return

    await state.finish()
    await message.answer("⚙️ Sozlash", reply_markup=settings_menu_kb())


@dp.message_handler(lambda m: m.text == "📭 Kanalga yuborilmaganlar")
async def unpublished_list_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return

    await state.finish()
    db = load_db()
    rows = []
    for code, item in db.items():
        if not isinstance(item, dict) or item.get("channel_msg_id") is not None:
            continue
        rows.append((_content_display_emoji(item), _content_display_title(item), str(code)))

    if not rows:
        await message.answer("📭 Kanalga yuborilmagan kino yo‘q", reply_markup=settings_menu_kb())
        return

    lines = ["📭 Kanalga yuborilmaganlar\n"]
    for emoji, title, code in rows[:80]:
        lines.append(f"{emoji} {title} — {code}")
    await message.answer("\n".join(lines), reply_markup=settings_menu_kb())


@dp.message_handler(lambda m: m.text == "📣 Kanalga yuborish")
async def publish_later_btn(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return

    await state.finish()
    await message.answer("🆔 Kodni yuboring (kanalga chiqmagan bo'lsa jo'natamiz)", reply_markup=settings_menu_kb())
    await PublishLater.code.set()


@dp.message_handler(state=PublishLater.code)
async def publish_later_code(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Kodni yuboring", reply_markup=settings_menu_kb())
        return

    db = load_db()
    item = db.get(code)

    if not item:
        await message.answer("❌ Bunaqa kino o'zi yo'q", reply_markup=settings_menu_kb())
        await state.finish()
        return

    # ================== DUBLIKAT TEKSHIRUV ==================
    if item.get("channel_msg_id"):
        await message.answer("⚠️ Bu kino 2K kanalda bor. Dublikat yubormaymiz.", reply_markup=settings_menu_kb())
        await state.finish()
        return

    # ================== TASDIQ TUGMALARI ==================
    kb = types.InlineKeyboardMarkup()

    if item.get("type") == "movie":
        kb.add(
            types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_movie:{code}"),
            types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
        )
    else:
        kb.add(
            types.InlineKeyboardButton("✅ Kanalga jo'nataymi", callback_data=f"publish_series:{code}"),
            types.InlineKeyboardButton("❌ Yo jo'natmayinmi?", callback_data="cancel_send")
        )

    await message.answer("📣 Kanalga yuboraymi? (2K + 3K)", reply_markup=kb)
    await state.finish()

# ================== AVTOPOST ==================
@dp.message_handler(lambda m: m.text == "⏰ Avtopost")
async def ap_open(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
        return
    await state.finish()
    await message.answer(
        "⏰ Avtopost bo‘limi\n\nBu yerda kinolarni vaqtga qo‘yib,\nkanalga avtomatik chiqarasiz.\n\n👇 Nimani qilamiz?",
        reply_markup=autopost_menu_kb()
    )
    await AutoPostFlow.menu.set()

@dp.message_handler(state=AutoPostFlow.menu)
async def ap_menu_router(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    txt = (message.text or "").strip()

    if txt == "➕ Rejalashtirish":
        await message.answer("📅 Qaysi vaqtga qo‘yamiz?\n\nFormat:\n2026-03-06 21:30", reply_markup=autopost_menu_kb())
        await AutoPostFlow.add_time.set()
        return

    if txt == "📋 Rejalashtirilganlar":
        data = load_autopost()
        jobs = data.get("jobs", [])
        # show only pending (future or due not done yet)
        pending = [j for j in jobs if j.get("status") in (None, "pending")]
        if not pending:
            await message.answer("📭 Hozircha rejalashtirilgan kino yo‘q", reply_markup=autopost_menu_kb())
            return

        # sort by time
        pending_sorted = sorted(pending, key=lambda j: j.get("run_at", ""))
        lines = ["📋 Rejalashtirilgan kinolar\n"]
        db = load_db()
        for j in pending_sorted[:40]:
            code = str(j.get("code", "")).strip()
            item = db.get(code, {}) if isinstance(db, dict) else {}
            title = _content_display_title(item) if isinstance(item, dict) else "Noma'lum"
            emoji = _content_display_emoji(item) if isinstance(item, dict) else "🎬"
            run_at_text = str(j.get("run_at", "")).strip()
            lines.append(f"{emoji} {title} — {code}")
            lines.append(f"📅 {run_at_text}")
            lines.append(f"🆔 {j.get('id')}")
            lines.append("")
        await message.answer("\n".join(lines).strip(), reply_markup=autopost_menu_kb())
        return

    if txt == "✏️ Tahrirlash":
        await message.answer("✏️ Qaysi avtopostni tahrirlaymiz?\n\nID ni yuboring\nMasalan: AP-1047", reply_markup=autopost_menu_kb())
        await AutoPostFlow.edit_id.set()
        return

    if txt == "🗑 O‘chirish":
        await message.answer("🗑 Qaysi avtopostni o‘chiramiz?\n\nID ni yuboring\nMasalan: AP-1047", reply_markup=autopost_menu_kb())
        await AutoPostFlow.del_id.set()
        return

    await message.answer("❌ Noto'g'ri buyruq.\n👇 Menudan foydalaning.", reply_markup=autopost_menu_kb())

@dp.message_handler(state=AutoPostFlow.add_time)
async def ap_add_time(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    s = (message.text or "").strip()
    dt = _parse_dt_local(s)
    if not dt:
        await message.answer("❌ Vaqt noto‘g‘ri\n\nMana bunday yozing:\n2026-03-06 21:30", reply_markup=autopost_menu_kb())
        return

    await state.update_data(ap_time=s)
    await message.answer("🆔 Endi kino kodini yuboring", reply_markup=autopost_menu_kb())
    await AutoPostFlow.add_code.set()

@dp.message_handler(state=AutoPostFlow.add_code)
async def ap_add_code(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Kodni to'g'ri yuboring (4 raqam)", reply_markup=autopost_menu_kb())
        return

    db = load_db()
    item = db.get(code)
    if not item:
        await message.answer("❌ Bunaqa kino o‘zi yo‘q", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    if item.get("channel_msg_id") or (isinstance(item.get("trailer"), dict) and item["trailer"].get("channel_msg_id")):
        await message.answer("⚠️ Bu kino kanalda bor\nDublikat chiqarmaymiz.", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    st = await state.get_data()
    run_at = st.get("ap_time")

    data = load_autopost()
    jobs = data.get("jobs", [])
    apid = _ap_new_id(jobs)

    jobs.append({
        "id": apid,
        "code": code,
        "run_at": run_at,
        "created_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
    })
    data["jobs"] = jobs
    save_autopost(data)

    await message.answer(
        f"✅ Avtopost saqlandi\n\n🆔 ID: {apid}\n🎬 Kod: {code}\n⏰ Vaqt: {run_at}",
        reply_markup=autopost_menu_kb()
    )
    await state.finish()

@dp.message_handler(state=AutoPostFlow.edit_id)
async def ap_edit_id(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    apid = (message.text or "").strip().upper()
    data = load_autopost()
    jobs = data.get("jobs", [])
    job = next((j for j in jobs if str(j.get("id", "")).upper() == apid and j.get("status") in (None, "pending")), None)
    if not job:
        await message.answer("❌ Bunaqa avtopost yo‘q", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    await state.update_data(apid=apid)
    await message.answer("✏️ Nimani o‘zgartiramiz?", reply_markup=autopost_edit_kb())
    await AutoPostFlow.edit_choose.set()

@dp.callback_query_handler(lambda c: c.data in ("ap_edit_time", "ap_edit_code", "ap_edit_cancel"), state=AutoPostFlow.edit_choose)
async def ap_edit_choose(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Admin emas ekansiz 😄", show_alert=True)
        await state.finish()
        return

    if call.data == "ap_edit_cancel":
        await call.message.answer("❎ Bekor qilindi", reply_markup=autopost_menu_kb())
        await state.finish()
        await call.answer()
        return

    if call.data == "ap_edit_time":
        await call.message.answer("🕒 Yangi vaqtni yuboring\n\nFormat:\n2026-03-06 22:30", reply_markup=autopost_menu_kb())
        await AutoPostFlow.edit_time.set()
        await call.answer()
        return

    if call.data == "ap_edit_code":
        await call.message.answer("🎬 Yangi kino kodini yuboring", reply_markup=autopost_menu_kb())
        await AutoPostFlow.edit_code.set()
        await call.answer()
        return

@dp.message_handler(state=AutoPostFlow.edit_time)
async def ap_edit_time(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    s = (message.text or "").strip()
    dt = _parse_dt_local(s)
    if not dt:
        await message.answer("❌ Vaqt noto‘g‘ri\n\nMana bunday yozing:\n2026-03-06 21:30", reply_markup=autopost_menu_kb())
        return

    st = await state.get_data()
    apid = st.get("apid")

    data = load_autopost()
    jobs = data.get("jobs", [])
    for j in jobs:
        if str(j.get("id", "")).upper() == str(apid).upper() and j.get("status") in (None, "pending"):
            j["run_at"] = s
            save_autopost(data)
            await message.answer("♻️ Vaqt yangilandi", reply_markup=autopost_menu_kb())
            await state.finish()
            return

    await message.answer("❌ Bunaqa avtopost yo‘q", reply_markup=autopost_menu_kb())
    await state.finish()

@dp.message_handler(state=AutoPostFlow.edit_code)
async def ap_edit_code(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer("🆔 Kodni to'g'ri yuboring (4 raqam)", reply_markup=autopost_menu_kb())
        return

    db = load_db()
    item = db.get(code)
    if not item:
        await message.answer("❌ Bunaqa kino o‘zi yo‘q", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    if item.get("channel_msg_id"):
        await message.answer("⚠️ Bu kino kanalda bor\nDublikat chiqarmaymiz.", reply_markup=autopost_menu_kb())
        await state.finish()
        return

    st = await state.get_data()
    apid = st.get("apid")

    data = load_autopost()
    jobs = data.get("jobs", [])
    for j in jobs:
        if str(j.get("id", "")).upper() == str(apid).upper() and j.get("status") in (None, "pending"):
            j["code"] = code
            save_autopost(data)
            await message.answer("♻️ Kino almashtirildi", reply_markup=autopost_menu_kb())
            await state.finish()
            return

    await message.answer("❌ Bunaqa avtopost yo‘q", reply_markup=autopost_menu_kb())
    await state.finish()

@dp.message_handler(state=AutoPostFlow.del_id)
async def ap_delete(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.finish()
        return

    apid = (message.text or "").strip().upper()
    data = load_autopost()
    jobs = data.get("jobs", [])
    for j in jobs:
        if str(j.get("id", "")).upper() == apid and j.get("status") in (None, "pending"):
            j["status"] = "cancelled"
            j["done_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
            j["result"] = "cancelled by admin"
            save_autopost(data)
            await message.answer(f"🗑 O‘chirib tashadim\n\n🆔 {apid}", reply_markup=autopost_menu_kb())
            await state.finish()
            return

    await message.answer("❌ Bunaqa avtopost yo‘q", reply_markup=autopost_menu_kb())
    await state.finish()

# ================== FALLBACK (hech qachon jim emas) ==================
@dp.message_handler(content_types=types.ContentType.ANY, state="*")
async def fallback_all(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "❌ <b>Brat siz admin emassiz!</b>\n"
            "🎬 Faqat <b>Qidiruv</b> tugmasidan foydalanishingiz mumkin.",
            reply_markup=user_menu()
        )
    else:
        await message.answer("❌ Noto'g'ri buyruq.\n👇 Menudan foydalaning.", reply_markup=admin_menu())

# ================== STARTUP ==================
async def on_startup(dp):
    await bot.delete_webhook(drop_pending_updates=True)

    # 🔥 autopost loop start (barqaror usul)
    asyncio.create_task(autopost_loop())

if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup
    )
