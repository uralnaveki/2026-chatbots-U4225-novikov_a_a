"""
Источники данных для бота-планировщика (лабораторная работа 2).

Здесь собрано всё, что не связано с Telegram и TickTick:

  1. SQLite   - история действий бота и статистика (класс HistoryDB, файл bot.db).
  2. Open-Meteo - публичный API погоды без ключа (функции geocode / fetch_weather).
  3. CSV      - выгрузка задач в файл и разбор присланного файла (tasks_to_csv / parse_tasks_csv).
  4. RSS      - новости о научных и технологических стартапах (fetch_news) и подписка на
                утренний дайджест (таблицы news_subs / news_sent в том же bot.db).

Модуль не импортирует bot.py, поэтому его удобно проверять отдельно.
"""

import asyncio
import csv
import html
import io
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger("planner-bot.data")

# Названия приоритетов TickTick (число -> слово). Используются и в чате, и в CSV.
PRIORITY_NAMES = {0: "нет", 1: "низкий", 3: "средний", 5: "высокий"}


# ===========================================================================
# 1. SQLite: история действий
# ===========================================================================
class HistoryDB:
    """Маленькая база SQLite: что и когда делал бот по просьбе пользователя."""

    def __init__(self, path: str):
        self.path = path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def _init_schema(self) -> None:
        """Создаёт таблицу при первом запуске (повторно ничего не ломает)."""
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL,
                    created_at  TEXT    NOT NULL,   -- время в UTC, ISO-формат
                    action      TEXT    NOT NULL,   -- task_create, task_complete, ...
                    title       TEXT,               -- название задачи / проекта / привычки
                    project     TEXT,               -- проект, в котором это произошло
                    details     TEXT,               -- что именно изменили
                    source_text TEXT                -- исходная фраза пользователя
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_user_time "
                "ON actions (user_id, created_at)"
            )
            # Подписки на утренние новости и ссылки, которые уже были отправлены.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS news_subs ("
                "user_id INTEGER PRIMARY KEY, send_time TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS news_sent ("
                "user_id INTEGER NOT NULL, link TEXT NOT NULL, sent_at TEXT NOT NULL, "
                "PRIMARY KEY (user_id, link))"
            )
            # Отзывы пользователей (команда /feedback).
            conn.execute(
                "CREATE TABLE IF NOT EXISTS feedback ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "created_at TEXT NOT NULL, text TEXT NOT NULL)"
            )
            conn.commit()

    def log(self, user_id: int, action: str, title: str = "", project: str = "",
            details: str = "", source_text: str = "") -> None:
        """Записывает одно действие."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO actions (user_id, created_at, action, title, project, details, source_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, now, action, title, project, details, source_text[:500]),
            )
            conn.commit()

    def history(self, user_id: int, days: int = 7, query: str | None = None,
                limit: int = 30) -> list[dict]:
        """Последние действия за `days` дней, новые сверху. query - слово из названия/проекта."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT created_at, action, title, project, details FROM actions "
                "WHERE user_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 1000",
                (user_id, since),
            ).fetchall()
        result = []
        needle = (query or "").strip().casefold()
        for created_at, action, title, project, details in rows:
            # LIKE в SQLite не умеет регистронезависимый поиск по кириллице,
            # поэтому фильтруем в Python.
            if needle and needle not in f"{title} {project} {details}".casefold():
                continue
            result.append({
                "created_at": datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc),
                "action": action, "title": title or "", "project": project or "",
                "details": details or "",
            })
            if len(result) >= limit:
                break
        return result

    def stats(self, user_id: int, days: int = 7) -> dict:
        """Сводка за период: сколько действий каждого вида и разбивка по проектам."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        with closing(self._connect()) as conn:
            by_action = dict(conn.execute(
                "SELECT action, COUNT(*) FROM actions WHERE user_id = ? AND created_at >= ? "
                "GROUP BY action", (user_id, since)).fetchall())
            by_project = conn.execute(
                "SELECT COALESCE(NULLIF(project, ''), '—'), action, COUNT(*) FROM actions "
                "WHERE user_id = ? AND created_at >= ? AND action IN "
                "('task_create', 'task_update', 'task_complete') GROUP BY 1, 2",
                (user_id, since)).fetchall()
        projects: dict[str, dict[str, int]] = {}
        for project, action, count in by_project:
            projects.setdefault(project, {})[action] = count
        return {"by_action": by_action, "by_project": projects}

    # --- отзывы и статистика использования ------------------------------------
    def add_feedback(self, user_id: int, text: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with closing(self._connect()) as conn:
            conn.execute("INSERT INTO feedback (user_id, created_at, text) VALUES (?, ?, ?)",
                         (user_id, now, text[:1000]))
            conn.commit()

    def feedback_list(self, limit: int = 10) -> list[tuple[int, str, str]]:
        """Последние отзывы: [(user_id, время UTC, текст), ...], новые сверху."""
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT user_id, created_at, text FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def usage(self) -> dict:
        """Сводка использования: всего действий, по пользователям, по видам, число отзывов."""
        with closing(self._connect()) as conn:
            per_user = conn.execute(
                "SELECT user_id, COUNT(*), MAX(created_at) FROM actions "
                "GROUP BY user_id ORDER BY COUNT(*) DESC").fetchall()
            per_action = conn.execute(
                "SELECT action, COUNT(*) FROM actions GROUP BY action ORDER BY COUNT(*) DESC LIMIT 8").fetchall()
            feedback = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        return {"per_user": per_user, "per_action": per_action, "feedback": feedback}

    # --- подписка на новости ------------------------------------------------
    def set_news_sub(self, user_id: int, send_time: str) -> None:
        """Включает (или меняет время) утреннего дайджеста. send_time - 'HH:MM'."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO news_subs (user_id, send_time) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET send_time = excluded.send_time",
                (user_id, send_time),
            )
            conn.commit()

    def remove_news_sub(self, user_id: int) -> bool:
        """Выключает дайджест. True, если подписка существовала."""
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM news_subs WHERE user_id = ?", (user_id,))
            conn.commit()
            return cur.rowcount > 0

    def news_subs(self) -> list[tuple[int, str]]:
        """Все подписки: [(user_id, 'HH:MM'), ...]."""
        with closing(self._connect()) as conn:
            return conn.execute("SELECT user_id, send_time FROM news_subs").fetchall()

    def sent_links(self, user_id: int) -> set[str]:
        """Ссылки на новости, которые пользователь уже получал."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT link FROM news_sent WHERE user_id = ?", (user_id,)).fetchall()
        return {r[0] for r in rows}

    def mark_sent(self, user_id: int, links: list[str]) -> None:
        """Запоминает отправленные ссылки (повторно они в дайджест не попадут)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with closing(self._connect()) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO news_sent (user_id, link, sent_at) VALUES (?, ?, ?)",
                [(user_id, link, now) for link in links],
            )
            # Старые записи не нужны: новости старше 30 дней всё равно не попадают в выборку.
            old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute("DELETE FROM news_sent WHERE sent_at < ?", (old,))
            conn.commit()


# ===========================================================================
# 2. Open-Meteo: публичный API погоды (ключ не нужен)
# ===========================================================================
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TTL = 600          # секунд хранить ответ в памяти (кэширование)
HTTP_TIMEOUT = 10          # секунд на один запрос

# Расшифровка кодов погоды WMO, которые возвращает Open-Meteo.
WEATHER_CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман с изморозью",
    51: "слабая морось", 53: "морось", 55: "сильная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежные зёрна",
    80: "ливень", 81: "сильный ливень", 82: "очень сильный ливень",
    85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}

_cache: dict[str, tuple[float, object]] = {}


class ApiError(Exception):
    """Внешний сервис недоступен или вернул непонятный ответ."""


class CityNotFound(Exception):
    """Сервис геокодирования не знает такого города."""


async def _get_json(url: str, params: dict) -> dict:
    """GET-запрос с таймаутом; любые сетевые проблемы превращает в ApiError."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        raise ApiError("сервис погоды не ответил вовремя")
    except httpx.HTTPStatusError as e:
        raise ApiError(f"сервис погоды вернул ошибку {e.response.status_code}")
    except httpx.HTTPError:
        raise ApiError("нет связи с сервисом погоды")
    except ValueError:
        raise ApiError("сервис погоды вернул непонятный ответ")


def _cached(key: str):
    item = _cache.get(key)
    if item and time.time() - item[0] < WEATHER_TTL:
        return item[1]
    return None


async def geocode(city: str) -> dict:
    """Название города -> {name, lat, lon}. Нет такого города - CityNotFound."""
    city = city.strip()
    key = f"geo:{city.casefold()}"
    if (hit := _cached(key)) is not None:
        return hit
    data = await _get_json(GEOCODE_URL, {"name": city, "count": 1, "language": "ru"})
    results = data.get("results") or []
    if not results:
        raise CityNotFound(f"Не нашёл город «{city}». Проверьте название.")
    first = results[0]
    place = {"name": first.get("name", city), "lat": first["latitude"], "lon": first["longitude"]}
    _cache[key] = (time.time(), place)
    return place


async def fetch_weather(lat: float, lon: float) -> dict:
    """Текущая погода и прогноз на сегодня по координатам."""
    key = f"wx:{lat:.3f},{lon:.3f}"
    if (hit := _cached(key)) is not None:
        return hit
    data = await _get_json(FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "wind_speed_unit": "ms", "timezone": "auto", "forecast_days": 1,
    })
    if not isinstance(data.get("current"), dict):
        raise ApiError("сервис погоды не прислал текущие данные")
    _cache[key] = (time.time(), data)
    return data


def _temp(value) -> str:
    return f"{value:+.0f}°C" if isinstance(value, (int, float)) else "н/д"


def format_weather(place_name: str, data: dict) -> str:
    """Данные Open-Meteo -> короткий текст для чата."""
    cur = data["current"]
    daily = data.get("daily") or {}

    def first(name):
        values = daily.get(name) or [None]
        return values[0]

    line = (
        f"🌤 {place_name}: сейчас {_temp(cur.get('temperature_2m'))} "
        f"(ощущается {_temp(cur.get('apparent_temperature'))}), "
        f"{WEATHER_CODES.get(cur.get('weather_code'), 'погода не определена')}"
    )
    wind = cur.get("wind_speed_10m")
    if isinstance(wind, (int, float)):
        line += f", ветер {wind:.0f} м/с"
    line += f".\nСегодня от {_temp(first('temperature_2m_min'))} до {_temp(first('temperature_2m_max'))}"
    rain = first("precipitation_probability_max")
    if isinstance(rain, (int, float)):
        line += f", вероятность осадков {rain:.0f}%"
    return line + "."


# ===========================================================================
# 3. CSV: выгрузка и загрузка задач
# ===========================================================================
CSV_FIELDS = ["title", "project", "due", "priority", "tags", "description", "status"]
MAX_CSV_ROWS = 50      # столько строк за один импорт (каждая - вызов CLI)

# Допустимые названия колонок во входном файле (регистр не важен).
COLUMN_ALIASES = {
    "title": {"title", "название", "задача", "name"},
    "project": {"project", "проект", "список"},
    "due": {"due", "срок", "дата", "deadline"},
    "priority": {"priority", "приоритет"},
    "tags": {"tags", "теги", "тег"},
    "content": {"description", "описание", "content"},
}
PRIORITY_WORDS = {
    "": None, "нет": 0, "none": 0, "0": 0,
    "низкий": 1, "low": 1, "1": 1,
    "средний": 3, "medium": 3, "3": 3,
    "высокий": 5, "high": 5, "5": 5,
}
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y")


class CsvError(Exception):
    """Файл нельзя разобрать целиком (не тот формат, нет колонки с названием)."""


def tasks_to_csv(rows: list[dict]) -> bytes:
    """Список задач -> CSV в кодировке UTF-8 с BOM (чтобы Excel не ломал кириллицу)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):   # cp1251 - русский Excel
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvError("Не удалось прочитать файл: сохраните его в кодировке UTF-8.")


def _parse_due(text: str) -> str:
    """Дата из файла -> 'YYYY-MM-DDTHH:MM'. Без времени ставим 09:00."""
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%H" not in fmt:
            dt = dt.replace(hour=9, minute=0)
        return dt.strftime("%Y-%m-%dT%H:%M")
    raise ValueError(f"не понял дату «{text}» (пример: 2026-10-14 18:00 или 14.10.2026)")


def parse_tasks_csv(data: bytes) -> tuple[list[dict], list[str]]:
    """
    Разбирает присланный CSV.
    Возвращает (задачи, ошибки): корректные строки идут в работу, остальные
    описываются в списке ошибок, чтобы пользователь мог их исправить.
    """
    text = _decode(data)
    if not text.strip():
        raise CsvError("Файл пустой.")
    first_line = text.splitlines()[0]
    delimiter = ";" if first_line.count(";") >= first_line.count(",") and ";" in first_line else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    header = next(reader, [])
    columns: dict[str, int] = {}
    for index, name in enumerate(header):
        for key, aliases in COLUMN_ALIASES.items():
            if name.strip().casefold() in aliases and key not in columns:
                columns[key] = index
    if "title" not in columns:
        raise CsvError("В файле нет колонки с названием задачи (title или название).")

    tasks: list[dict] = []
    errors: list[str] = []
    for line_no, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue                                   # пустая строка
        if len(tasks) + len(errors) >= MAX_CSV_ROWS:
            errors.append(f"файл обрезан: обработано первых {MAX_CSV_ROWS} строк")
            break

        def cell(key: str) -> str:
            index = columns.get(key)
            return row[index].strip() if index is not None and index < len(row) else ""

        title = cell("title")
        if not title:
            errors.append(f"строка {line_no}: пустое название")
            continue
        try:
            due = _parse_due(cell("due")) if cell("due") else None
            priority_text = cell("priority").casefold()
            if priority_text not in PRIORITY_WORDS:
                raise ValueError(f"не понял приоритет «{cell('priority')}» (нет, низкий, средний, высокий)")
            tasks.append({
                "line": line_no,
                "title": title,
                "project": cell("project"),
                "due": due,
                "priority": PRIORITY_WORDS[priority_text],
                "tags": [t.strip().lstrip("#") for t in re.split(r"[;,]", cell("tags")) if t.strip()],
                "content": cell("content"),
            })
        except ValueError as e:
            errors.append(f"строка {line_no}: {e}")
    return tasks, errors


# ===========================================================================
# 4. RSS: новости о научных и технологических стартапах
# ===========================================================================
# Источники (английские RSS-ленты; перевод на русский делает LLM в bot.py).
# filter=True - лента общая, берём только записи с ключевыми словами о стартапах.
NEWS_FEEDS = [
    {"name": "TechCrunch Startups", "url": "https://techcrunch.com/category/startups/feed/", "filter": False},
    {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/feed/", "filter": True},
    {"name": "Singularity Hub", "url": "https://singularityhub.com/feed/", "filter": True},
    {"name": "ScienceDaily Technology", "url": "https://www.sciencedaily.com/rss/top/technology.xml", "filter": True},
]
# Слова-маркеры «научный/технологический стартап» для общих лент.
NEWS_KEYWORDS = (
    "startup", "start-up", "spinout", "spin-out", "founder", "raises", "raised", "funding",
    "seed round", "series a", "series b", "venture", "deep tech", "biotech", "quantum",
    "fusion", "robot", "battery", "chip", "semiconductor", "space", "satellite", "rocket",
    "drug discovery", "gene", "crispr", "climate tech", "carbon capture", "ai ",
)
NEWS_MAX_AGE_DAYS = 3          # более старые записи в дайджест не берём
NEWS_MAX_BYTES = 2_000_000     # защита от слишком больших ответов
NEWS_USER_AGENT = "VibeCodingPlannerBot/1.0 (student project; RSS reader)"


def _clean_html(text: str) -> str:
    """Убирает HTML-теги и лишние пробелы из описания записи."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_rss(xml_text: str | bytes, source: str = "") -> list[dict]:
    """Разбирает RSS 2.0 в список {title, link, summary, published, source}.

    Записи без заголовка или ссылки пропускаются. Некорректный XML -> ApiError.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise ApiError(f"лента {source or 'новостей'} вернула не RSS")
    channel = root.find("channel")
    if channel is None:
        raise ApiError(f"лента {source or 'новостей'} имеет неожиданный формат")
    items = []
    for node in channel.findall("item"):
        title = _clean_html(node.findtext("title") or "")
        link = (node.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        published = None
        raw_date = node.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        items.append({
            "title": title,
            "link": link,
            "summary": _clean_html(node.findtext("description") or "")[:400],
            "published": published,
            "source": source,
        })
    return items


async def _fetch_feed(feed: dict) -> list[dict]:
    """Скачивает и разбирает одну ленту. Любая сетевая проблема -> ApiError."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": NEWS_USER_AGENT}) as client:
            response = await client.get(feed["url"])
            response.raise_for_status()
            if len(response.content) > NEWS_MAX_BYTES:
                raise ApiError(f"лента {feed['name']} слишком большая")
            return parse_rss(response.content, feed["name"])
    except httpx.TimeoutException:
        raise ApiError(f"лента {feed['name']} не ответила вовремя")
    except httpx.HTTPError:
        raise ApiError(f"лента {feed['name']} недоступна")


def matches_keywords(item: dict) -> bool:
    """Подходит ли запись под тему «стартапы / научные технологии»."""
    haystack = f"{item['title']} {item['summary']} ".casefold()
    return any(word in haystack for word in NEWS_KEYWORDS)


async def fetch_news(limit: int = 5, exclude: set[str] | None = None,
                     max_age_days: int = NEWS_MAX_AGE_DAYS) -> list[dict]:
    """Свежие новости из всех лент: без повторов, новые сверху, не больше `limit`.

    exclude - ссылки, которые уже отправлялись. Если часть лент недоступна, берём остальные;
    ApiError возникает только когда не ответила ни одна лента.
    """
    exclude = exclude or set()
    results = await asyncio.gather(*(_fetch_feed(f) for f in NEWS_FEEDS), return_exceptions=True)
    items: list[dict] = []
    failures = 0
    for feed, result in zip(NEWS_FEEDS, results):
        if isinstance(result, Exception):
            failures += 1
            logger.warning("Лента %s: %s", feed["name"], result)
            continue
        items += [i for i in result if not feed["filter"] or matches_keywords(i)]
    if failures == len(NEWS_FEEDS):
        raise ApiError("ни одна из лент новостей не ответила")

    since = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    seen, fresh = set(), []
    for item in sorted(items, key=lambda i: i["published"] or since, reverse=True):
        if item["link"] in exclude or item["link"] in seen:
            continue
        if item["published"] and item["published"] < since:
            continue
        seen.add(item["link"])
        fresh.append(item)
    # Чтобы один источник не занял весь дайджест, берём по кругу: не более 2 из каждого.
    per_source: dict[str, int] = {}
    picked = []
    for item in fresh:
        if per_source.get(item["source"], 0) >= 2:
            continue
        per_source[item["source"]] = per_source.get(item["source"], 0) + 1
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def parse_send_time(text: str) -> str:
    """'8:00', '08.30', '9' -> '08:00'. Неверный формат -> ValueError."""
    text = (text or "").strip("[]() \t")  # на случай «/news_on [21:43]» (скобки из справки)
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.\-](\d{2}))?\s*", text or "")
    if not m:
        raise ValueError("время должно быть в формате ЧЧ:ММ, например 08:00")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ValueError("такого времени не бывает, пример: 08:00")
    return f"{hour:02d}:{minute:02d}"
