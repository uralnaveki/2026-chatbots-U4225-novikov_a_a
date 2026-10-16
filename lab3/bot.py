"""
Telegram-бот «Планировщик» для TickTick.

Как это работает:
  1. Вы пишете боту обычным текстом: «завтра в 10 созвон с клиентом, высокий приоритет,
     напомни за 30 минут».
  2. LLM превращает фразу в JSON-команду (действие + параметры) и выбирает подходящий
     список (проект) из ваших проектов TickTick.
  3. Бот выполняет команду через официальный TickTick CLI (`ticktick ...`) и
     отвечает, что именно было сделано.

Поддерживается: задачи (создать, изменить, завершить, найти), проекты
(создать, посмотреть, изменить, удалить), привычки (создать, посмотреть, изменить).
"""

import asyncio
import contextvars
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import integrations  # лабораторная 2: SQLite, Open-Meteo, CSV (файл integrations.py)
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Настройки (берутся из файла .env, см. .env.example)
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Telegram ID тех, кому можно пользоваться ботом: один ID или несколько через запятую.
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()
ALLOWED_IDS = {x.strip() for x in re.split(r"[,; ]+", ALLOWED_USER_ID) if x.strip()}
# Убираем случайные пробелы, кавычки и слово Bearer, которые часто попадают при копировании.
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip().strip("\"'").strip()
if LLM_API_KEY.lower().startswith("bearer "):
    LLM_API_KEY = LLM_API_KEY[7:].strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or None  # пусто = официальный OpenAI
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
TICKTICK_BIN = os.getenv("TICKTICK_BIN", "ticktick")
DEFAULT_PROJECT_ID = os.getenv("DEFAULT_PROJECT_ID", "").strip()  # запасной список
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
DB_FILE = os.getenv("DB_FILE", "bot.db")               # SQLite: история действий
WEATHER_CITY = os.getenv("WEATHER_CITY", "Москва")     # город по умолчанию для /today и /weather
NEWS_COUNT = max(1, min(10, int(os.getenv("NEWS_COUNT", "5") or 5)))  # новостей в одном дайджесте
STARTED_AT = time.time()                               # для /status (аптайм)
MAX_CSV_BYTES = 200_000                                # больше этого размера CSV не принимаем

CLI_TIMEOUT = 30          # секунд на один вызов CLI
PROJECTS_TTL = 300        # как долго (сек.) хранить кэш списка проектов
PRIORITY_NAMES = integrations.PRIORITY_NAMES

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("planner-bot")

llm = AsyncOpenAI(api_key=LLM_API_KEY or "missing", base_url=LLM_BASE_URL)

# База истории открывается в main(); None означает, что открыть её не удалось.
DB: "integrations.HistoryDB | None" = None
# Кто и какой фразой сейчас обращается к боту (нужно для записи в историю).
CTX_USER = contextvars.ContextVar("ctx_user", default=0)
CTX_TEXT = contextvars.ContextVar("ctx_text", default="")


# ---------------------------------------------------------------------------
# Свои исключения
# ---------------------------------------------------------------------------
class UserError(Exception):
    """Понятная пользователю проблема: не хватает данных, запрос неоднозначен."""


class CliError(Exception):
    """TickTick CLI вернул ошибку или недоступен."""


# ---------------------------------------------------------------------------
# Хранилище данных: маленький JSON-файл
# Что лежит внутри:
#   projects  - кэш списка проектов (чтобы не вызывать CLI на каждое сообщение)
#   last_tasks - (по пользователям) последняя созданная/найденная задача, чтобы можно было сказать
#               «поставь ей высокий приоритет»
# ---------------------------------------------------------------------------
def load_data() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_data() -> None:
    try:
        DATA_FILE.write_text(
            json.dumps(DATA, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.exception("Не удалось записать %s", DATA_FILE)


DATA: dict = load_data()


# ---------------------------------------------------------------------------
# Работа с TickTick CLI
# ---------------------------------------------------------------------------

def get_last_task():
    """Последняя задача ТЕКУЩЕГО пользователя (у каждого человека своя)."""
    return DATA.get("last_tasks", {}).get(str(CTX_USER.get()))


def set_last_task(task) -> None:
    """Запоминает (или забывает, если task=None) последнюю задачу текущего пользователя."""
    tasks = DATA.setdefault("last_tasks", {})
    if task is None:
        tasks.pop(str(CTX_USER.get()), None)
    else:
        tasks[str(CTX_USER.get())] = task
    save_data()


async def run_cli(*args: str) -> str:
    """Запускает `ticktick <args>` и возвращает stdout. При ошибке - CliError."""
    # shutil.which учитывает PATHEXT, поэтому на Windows находит ticktick.cmd
    # (npm создаёт именно .cmd, а не .exe - без этого Python его не видит).
    exe = shutil.which(TICKTICK_BIN)
    if exe is None:
        raise CliError(
            "команда 'ticktick' не найдена. Установите: npm install -g @ticktick/ticktick-cli "
            "или укажите полный путь в TICKTICK_BIN"
        )
    # .cmd-файлы запускаются через cmd.exe, который трактует символы & | < > ^ % "
    # как служебные. Чтобы текст задачи не превратился в команду, заменяем их пробелом.
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        args = tuple(re.sub(r'[&|<>^%"]', " ", a) for a in args)
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise CliError(
            "команда 'ticktick' не найдена. Установите: npm install -g @ticktick/ticktick-cli"
        )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise CliError(f"CLI не ответил за {CLI_TIMEOUT} секунд")

    out_text = out.decode(errors="replace").strip()
    err_text = err.decode(errors="replace").strip()
    if proc.returncode != 0:
        message = err_text or out_text or f"код выхода {proc.returncode}"
        if "auth" in message.lower() or "login" in message.lower():
            message += " (возможно, нужно выполнить: ticktick auth login)"
        raise CliError(message[:500])
    return out_text


def parse_json(text: str):
    """Разбирает JSON или JSON-строки (по объекту на строку). Не вышло - None."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            return None
    return items or None


async def run_cli_json(*args: str):
    """Как run_cli, но добавляет --json и возвращает разобранный результат."""
    data = parse_json(await run_cli(*args, "--json"))
    if data is None:
        raise CliError("CLI вернул ответ, который не удалось разобрать как JSON")
    return data


def as_list(data, *keys: str) -> list:
    """Достаёт список из ответа CLI, независимо от того, список это или объект."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (*keys, "data", "items", "result", "tasks"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Проекты, задачи, привычки: получение данных
# ---------------------------------------------------------------------------
async def get_projects(force: bool = False) -> list[dict]:
    """Список проектов [{id, name}] с кэшем на PROJECTS_TTL секунд."""
    cache = DATA.get("projects")
    if cache and not force and time.time() - cache.get("ts", 0) < PROJECTS_TTL:
        return cache["items"]
    raw = await run_cli_json("project", "list")
    items = [
        {"id": str(p["id"]), "name": str(p["name"])}
        for p in as_list(raw, "projects")
        if isinstance(p, dict) and p.get("id") and p.get("name") and not p.get("closed")
    ]
    DATA["projects"] = {"ts": time.time(), "items": items}
    save_data()
    return items


def project_name(project_id: str, projects: list[dict] | None = None) -> str:
    items = projects or DATA.get("projects", {}).get("items", [])
    for p in items:
        if p["id"] == project_id:
            return p["name"]
    return project_id


def valid_project(project_id, projects: list[dict]) -> str | None:
    """Возвращает id, только если такой проект реально существует."""
    if project_id and any(p["id"] == str(project_id) for p in projects):
        return str(project_id)
    return None


def norm_task(t: dict) -> dict:
    """Приводит задачу из ответа CLI к единому виду."""
    return {
        "id": str(t.get("id", "")),
        "project_id": str(t.get("projectId", "")),
        "title": str(t.get("title", "")),
        "priority": to_int(t.get("priority")),
        "due": t.get("dueDate"),
        "tags": t.get("tags") or [],
        "status": to_int(t.get("status")),
        "content": str(t.get("content") or ""),
    }


async def find_task(query, projects: list[dict]) -> dict:
    """Ищет одну задачу по словам из названия. Без запроса - берёт последнюю задачу."""
    if not query:
        last = get_last_task()
        if last:
            return last
        raise UserError("Не понял, о какой задаче речь. Назовите её по названию.")

    args = ["task", "search", str(query), "--status", "0"]
    if projects:
        args += ["--projects", ",".join(p["id"] for p in projects)]
    raw = await run_cli_json(*args)
    tasks = [norm_task(t) for t in as_list(raw, "tasks") if isinstance(t, dict)]
    tasks = [t for t in tasks if t["id"] and t["project_id"]]

    if not tasks:
        raise UserError(f"Не нашёл активных задач по запросу «{query}».")
    if len(tasks) > 1:
        exact = [t for t in tasks if t["title"].lower() == str(query).lower()]
        if len(exact) == 1:
            tasks = exact
        else:
            names = "\n".join(f"• {t['title']}" for t in tasks[:5])
            raise UserError(f"Нашлось несколько задач, уточните название:\n{names}")
    task = tasks[0]
    set_last_task(task)
    save_data()
    return task


async def get_habits() -> list[dict]:
    raw = await run_cli_json("habit", "list")
    return [
        {
            "id": str(h["id"]),
            "name": str(h.get("name", "")),
            "goal": h.get("goal"),
            "unit": h.get("unit"),
        }
        for h in as_list(raw, "habits")
        if isinstance(h, dict) and h.get("id")
    ]


# ---------------------------------------------------------------------------
# Даты, приоритеты, напоминания
# ---------------------------------------------------------------------------
def to_cli_date(local_str: str) -> str:
    """'2026-10-10T09:00' (ваше время) -> '2026-10-10T06:00:00+0000' (UTC, как ждёт CLI)."""
    try:
        dt = datetime.fromisoformat(str(local_str))
    except ValueError:
        raise UserError(f"Не понял дату: {local_str}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def show_local(local_str: str) -> str:
    try:
        return datetime.fromisoformat(str(local_str)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(local_str)


def parse_api_dt(value) -> datetime | None:
    """Дата из ответа CLI (UTC) -> datetime в вашем часовом поясе. Нет даты - None."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00").replace("+0000", "+00:00")
        return datetime.fromisoformat(text).astimezone(TZ)
    except ValueError:
        return None


def show_api_date(value) -> str:
    """Дата из ответа CLI (UTC) -> ваше время для показа."""
    dt = parse_api_dt(value)
    if dt is None:
        return str(value) if value else ""
    return dt.strftime("%d.%m.%Y %H:%M")


def reminder_trigger(minutes: int) -> str:
    """За сколько минут напомнить -> формат напоминания TickTick (iCalendar TRIGGER)."""
    if minutes <= 0:
        return "TRIGGER:PT0S"                      # в момент срока
    if minutes % 1440 == 0:
        return f"TRIGGER:-P{minutes // 1440}D"     # за N дней
    return f"TRIGGER:-PT{minutes}M"                # за N минут


def describe_reminder(minutes: int) -> str:
    if minutes <= 0:
        return "в момент срока"
    if minutes % 1440 == 0:
        return f"за {minutes // 1440} дн. до срока"
    if minutes % 60 == 0:
        return f"за {minutes // 60} ч до срока"
    return f"за {minutes} мин до срока"


def task_options(p: dict) -> list[str]:
    """Параметры задачи (кроме проекта) -> аргументы CLI. Пустые поля пропускаются."""
    args: list[str] = []
    if p.get("title"):
        args += ["--title", str(p["title"]).strip()]
    if p.get("content"):
        args += ["--content", str(p["content"])]
    if p.get("tags"):
        tags = p["tags"] if isinstance(p["tags"], list) else [p["tags"]]
        args += ["--tags", ",".join(str(t).strip().lstrip("#") for t in tags if str(t).strip())]
    if p.get("priority") is not None and to_int(p["priority"], -1) in PRIORITY_NAMES:
        args += ["--priority", str(to_int(p["priority"]))]
    if p.get("due"):
        args += ["--due-date", to_cli_date(p["due"])]
    if p.get("reminder_minutes") is not None:
        args += ["--reminders", reminder_trigger(to_int(p["reminder_minutes"]))]
    return args


def describe_changes(p: dict) -> list[str]:
    """Человекочитаемый список того, что было задано/изменено."""
    parts = []
    if p.get("title"):
        parts.append(f"название: «{p['title']}»")
    if p.get("priority") is not None and to_int(p["priority"], -1) in PRIORITY_NAMES:
        parts.append(f"приоритет: {PRIORITY_NAMES[to_int(p['priority'])]}")
    if p.get("due"):
        parts.append(f"срок: {show_local(p['due'])}")
    if p.get("reminder_minutes") is not None:
        parts.append(f"напоминание: {describe_reminder(to_int(p['reminder_minutes']))}")
    if p.get("tags"):
        tags = p["tags"] if isinstance(p["tags"], list) else [p["tags"]]
        parts.append("теги: " + ", ".join(f"#{str(t).lstrip('#')}" for t in tags))
    if p.get("content"):
        parts.append("описание обновлено")
    return parts


# ---------------------------------------------------------------------------
# LLM: превращает текст пользователя в команду
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Ты - модуль разбора команд для планировщика задач TickTick.
Сейчас: {NOW} (часовой пояс пользователя). Проекты пользователя (id и название): {PROJECTS}
Последняя задача, с которой работали: {LAST_TASK}

Верни ТОЛЬКО один JSON-объект без пояснений и без markdown:
{"action": "<действие>", "params": {...}, "comment": "<короткий ответ, если действие chat>"}

Допустимые действия и их params (необязательные поля можно опускать или ставить null):
- task_create: title, project_id (СТРОГО из списка проектов выше), content (описание), tags (список строк),
  priority (0 нет, 1 низкий, 3 средний, 5 высокий), due ("YYYY-MM-DDTHH:MM", локальное время),
  reminder_minutes (за сколько минут до срока напомнить, 0 = в момент срока)
- task_update: task_query (слова из названия задачи; null, если речь о последней задаче),
  новые значения: title, content, tags, priority, due, reminder_minutes, new_project_id (перенос в другой проект)
- task_complete: task_query (null = последняя задача)
- task_search: query (слова из названия или null), tags, priority, status ("active" | "done" | null)
- project_list
- project_create: name, color (например "#F18181", необязательно)
- project_update: project_id, name, color
- project_delete: project_id
- habit_list
- habit_create: name, goal (число), unit (строка), repeat (RRULE, по умолчанию "RRULE:FREQ=DAILY;INTERVAL=1")
- habit_update: habit_query (слова из названия привычки), name (новое название), goal
- history: days (число дней, по умолчанию 7), query (слово из названия задачи или проекта, необязательно) - что бот делал за период
- stats: days - статистика действий по проектам за период
- today: city (необязательно) - план на сегодня: задачи со сроком на сегодня, просроченные и погода
- weather: city (необязательно; null - город по умолчанию) - только погода
- export: status ("active" | "done") - выгрузить задачи в CSV-файл
- news: count (число, необязательно) - свежие новости о научных и технологических стартапах
- chat: если запрос не про задачи/проекты/привычки/историю/погоду или непонятен - ответь в comment.

Правила:
- Относительные даты («завтра», «в пятницу», «через 2 часа») считай от текущего времени. Если указан день без времени - ставь 09:00.
- Проект выбирай по смыслу задачи (работа, учёба, дом, покупки и т.д.). Если ни один не подходит - project_id: null.
- Название задачи делай коротким и без служебных слов («поставь», «напомни»).
- Напоминание без срока невозможно: в этом случае всё равно верни reminder_minutes, бот сам сообщит об ошибке.
- «Что я делал / какие задачи создавал за неделю» - это history; «план на сегодня», «что у меня сегодня» - today; «погода» - weather; «выгрузи задачи в файл» - export; «новости про стартапы», «что нового в науке» - news.
"""


async def ask_llm(text: str, projects: list[dict]) -> dict:
    last = get_last_task()
    system = (
        SYSTEM_PROMPT.replace("{NOW}", datetime.now(TZ).strftime("%Y-%m-%d %H:%M, %A"))
        .replace("{PROJECTS}", json.dumps(projects, ensure_ascii=False))
        .replace("{LAST_TASK}", json.dumps(last, ensure_ascii=False) if last else "нет")
    )
    response = await llm.chat.completions.create(
        model=LLM_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    )
    content = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", content, re.DOTALL)  # на случай ```json ... ```
    if not match:
        raise UserError("Не удалось разобрать ответ LLM. Переформулируйте запрос.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        raise UserError("LLM вернула некорректный JSON. Попробуйте ещё раз.")


# ---------------------------------------------------------------------------
# Действия (каждое возвращает текст ответа пользователю)
# ---------------------------------------------------------------------------
async def act_task_create(p: dict, projects: list[dict]) -> str:
    title = str(p.get("title") or "").strip()
    if not title:
        raise UserError("Не нашёл название задачи.")
    project_id = valid_project(p.get("project_id"), projects) or DEFAULT_PROJECT_ID
    if not project_id:
        raise UserError(
            "Не смог выбрать список для задачи. Назовите проект явно или задайте "
            "DEFAULT_PROJECT_ID в .env."
        )
    if p.get("reminder_minutes") is not None and not p.get("due"):
        raise UserError("Чтобы поставить напоминание, нужен срок: когда выполнить задачу?")

    raw = await run_cli("task", "create", "--project", project_id, *task_options(p), "--json")

    # Запоминаем задачу, чтобы потом можно было её изменить словами «ей», «эту задачу».
    created = parse_json(raw)
    if isinstance(created, dict) and created.get("id"):
        set_last_task(norm_task({"projectId": project_id, **created}))
        save_data()

    await log_action("task_create", title, project_name(project_id, projects),
                     "; ".join(describe_changes(p)))
    lines = [f"✅ Задача «{title}» создана в проекте «{project_name(project_id, projects)}»."]
    if p.get("due"):
        lines.append(f"📅 Срок: {show_local(p['due'])}")
    if p.get("reminder_minutes") is not None:
        lines.append(f"⏰ Напоминание: {describe_reminder(to_int(p['reminder_minutes']))}")
    if p.get("priority") is not None and to_int(p["priority"], -1) in PRIORITY_NAMES:
        lines.append(f"🔥 Приоритет: {PRIORITY_NAMES[to_int(p['priority'])]}")
    if p.get("tags"):
        tags = p["tags"] if isinstance(p["tags"], list) else [p["tags"]]
        lines.append("🏷 Теги: " + ", ".join(f"#{str(t).lstrip('#')}" for t in tags))
    if p.get("content"):
        lines.append("📝 Описание добавлено")
    return "\n".join(lines)


async def act_task_update(p: dict, projects: list[dict]) -> str:
    task = await find_task(p.get("task_query"), projects)
    options = task_options(p)
    new_project = valid_project(p.get("new_project_id"), projects)
    if not options and not new_project:
        raise UserError("Не понял, что именно изменить в задаче.")

    if options:
        await run_cli(
            "task", "update", task["id"], "--id", task["id"],
            "--project", task["project_id"], *options,
        )
    changes = describe_changes(p)
    if new_project and new_project != task["project_id"]:
        await run_cli(
            "task", "move", "--from", task["project_id"], "--to", new_project,
            "--task", task["id"],
        )
        task["project_id"] = new_project
        changes.append(f"проект: «{project_name(new_project, projects)}»")

    await log_action("task_update", task["title"], project_name(task["project_id"], projects),
                     "; ".join(changes))
    set_last_task(task)
    save_data()
    return f"✏️ Задача «{task['title']}» обновлена: " + "; ".join(changes) + "."


async def act_task_complete(p: dict, projects: list[dict]) -> str:
    task = await find_task(p.get("task_query"), projects)
    await run_cli("task", "complete", task["project_id"], task["id"])
    await log_action("task_complete", task["title"], project_name(task["project_id"], projects))
    set_last_task(None)
    save_data()
    return f"🎉 Задача «{task['title']}» выполнена."


async def act_task_search(p: dict, projects: list[dict]) -> str:
    status = {"active": "0", "done": "2"}.get(p.get("status") or "active", "0")
    project_ids = ",".join(x["id"] for x in projects)
    tags = p.get("tags")
    tags_arg = ",".join(tags) if isinstance(tags, list) else (tags or "")

    if p.get("query"):
        args = ["task", "search", str(p["query"]), "--status", status]
        if project_ids:
            args += ["--projects", project_ids]
        if tags_arg:
            args += ["--tags", tags_arg]
    else:
        # Без текста используем фильтр: по проектам, приоритету и статусу.
        args = ["task", "filter", "--status", status]
        if project_ids:
            args += ["--projects", project_ids]
        if p.get("priority") is not None:
            args += ["--priority", str(to_int(p["priority"]))]

    raw = await run_cli_json(*args)
    tasks = [norm_task(t) for t in as_list(raw, "tasks") if isinstance(t, dict)]
    # Приоритет и теги дополнительно проверяем у себя - так надёжнее.
    if p.get("priority") is not None:
        tasks = [t for t in tasks if t["priority"] == to_int(p["priority"])]
    if tags_arg:
        wanted = {t.strip().lstrip("#").lower() for t in tags_arg.split(",") if t.strip()}
        tasks = [t for t in tasks if wanted & {str(x).lower() for x in t["tags"]}]

    if not tasks:
        return "🔍 Ничего не найдено."
    lines = [f"🔍 Найдено задач: {len(tasks)}"]
    for t in tasks[:20]:
        extra = [project_name(t["project_id"], projects)]
        if t["priority"]:
            extra.append(PRIORITY_NAMES.get(t["priority"], str(t["priority"])))
        if t["due"]:
            extra.append(show_api_date(t["due"]))
        if t["tags"]:
            extra.append(" ".join(f"#{x}" for x in t["tags"]))
        lines.append(f"• {t['title']} ({', '.join(extra)})")
    if len(tasks) > 20:
        lines.append(f"…и ещё {len(tasks) - 20}")
    if len(tasks) == 1:
        set_last_task(tasks[0])
    save_data()
    return "\n".join(lines)


async def act_project_list(p: dict, projects: list[dict]) -> str:
    projects = await get_projects(force=True)
    if not projects:
        return "У вас пока нет проектов."
    return "📂 Ваши проекты:\n" + "\n".join(f"• {x['name']}" for x in projects)


async def act_project_create(p: dict, projects: list[dict]) -> str:
    name = str(p.get("name") or "").strip()
    if not name:
        raise UserError("Как назвать новый проект?")
    args = ["project", "create", "--name", name, "--view-mode", "list", "--kind", "TASK"]
    if p.get("color"):
        args += ["--color", str(p["color"])]
    await run_cli(*args)
    await get_projects(force=True)
    await log_action("project_create", name, name)
    return f"📂 Проект «{name}» создан."


async def act_project_update(p: dict, projects: list[dict]) -> str:
    project_id = valid_project(p.get("project_id"), projects)
    if not project_id:
        raise UserError("Не нашёл такой проект. Напишите его название точнее.")
    args = ["project", "update", project_id]
    if p.get("name"):
        args += ["--name", str(p["name"]).strip()]
    if p.get("color"):
        args += ["--color", str(p["color"])]
    if len(args) == 3:
        raise UserError("Что изменить в проекте: название или цвет?")
    old_name = project_name(project_id, projects)
    await run_cli(*args)
    await get_projects(force=True)
    await log_action("project_update", old_name, old_name,
                     "; ".join(x for x in (f"новое название «{p['name']}»" if p.get("name") else "",
                                           f"цвет {p['color']}" if p.get("color") else "") if x))
    return f"✏️ Проект «{old_name}» обновлён: " + "; ".join(
        ([f"новое название «{p['name']}»"] if p.get("name") else [])
        + ([f"цвет {p['color']}"] if p.get("color") else [])
    ) + "."


async def act_habit_list(p: dict, projects: list[dict]) -> str:
    habits = await get_habits()
    if not habits:
        return "У вас пока нет привычек."
    lines = ["🔁 Ваши привычки:"]
    for h in habits:
        goal = ""
        if h.get("goal"):
            goal = f" — цель: {h['goal']} {h.get('unit') or ''}".rstrip()
        lines.append(f"• {h['name']}{goal}")
    return "\n".join(lines)


async def act_habit_create(p: dict, projects: list[dict]) -> str:
    name = str(p.get("name") or "").strip()
    if not name:
        raise UserError("Как назвать привычку?")
    args = ["habit", "create", "--name", name]
    if p.get("goal") is not None:
        args += ["--goal", str(to_int(p["goal"], 1))]
    if p.get("unit"):
        args += ["--unit", str(p["unit"])]
    args += ["--repeat", str(p.get("repeat") or "RRULE:FREQ=DAILY;INTERVAL=1")]
    await run_cli(*args)
    await log_action("habit_create", name)
    return f"🔁 Привычка «{name}» создана."


async def act_habit_update(p: dict, projects: list[dict]) -> str:
    query = str(p.get("habit_query") or "").strip().lower()
    if not query:
        raise UserError("Какую привычку изменить?")
    habits = [h for h in await get_habits() if query in h["name"].lower()]
    if not habits:
        raise UserError(f"Не нашёл привычку «{query}».")
    if len(habits) > 1:
        names = "\n".join(f"• {h['name']}" for h in habits[:5])
        raise UserError(f"Подходит несколько привычек, уточните:\n{names}")
    habit = habits[0]
    args = ["habit", "update", habit["id"]]
    changes = []
    if p.get("name"):
        args += ["--name", str(p["name"]).strip()]
        changes.append(f"новое название «{p['name']}»")
    if p.get("goal") is not None:
        args += ["--goal", str(to_int(p["goal"], 1))]
        changes.append(f"цель: {to_int(p['goal'], 1)}")
    if len(args) == 3:
        raise UserError("Что изменить в привычке: название или цель?")
    await run_cli(*args)
    await log_action("habit_update", habit["name"], details="; ".join(changes))
    return f"✏️ Привычка «{habit['name']}» обновлена: " + "; ".join(changes) + "."


# ---------------------------------------------------------------------------
# Лабораторная 2: данные из SQLite, публичного API погоды и CSV-файлов
# ---------------------------------------------------------------------------
ACTION_LABELS = {
    "task_create": "создана задача", "task_update": "изменена задача",
    "task_complete": "завершена задача", "task_export": "выгрузка задач в файл",
    "project_create": "создан проект", "project_update": "изменён проект",
    "project_delete": "удалён проект",
    "habit_create": "создана привычка", "habit_update": "изменена привычка",
    "news_on": "включён дайджест новостей", "news_off": "выключен дайджест новостей",
}


async def log_action(action: str, title: str = "", project: str = "", details: str = "") -> None:
    """Записывает действие в SQLite. Сбой записи не должен ломать основную работу бота."""
    if DB is None:
        return
    try:
        await asyncio.to_thread(
            DB.log, CTX_USER.get(), action, title, project, details, CTX_TEXT.get()
        )
    except Exception:
        logger.exception("Не удалось записать действие в историю")


def clamp_days(value, default: int = 7) -> int:
    """Число дней из запроса, ограниченное разумными рамками (1-365)."""
    return max(1, min(to_int(value, default) or default, 365))


async def act_history(p: dict, projects: list[dict]) -> str:
    """Что делал бот за последние N дней (SQLite), с необязательным поиском по слову."""
    if DB is None:
        raise UserError("История недоступна: не удалось открыть базу данных.")
    days = clamp_days(p.get("days"))
    query = str(p["query"]).strip() if p.get("query") else None
    rows = await asyncio.to_thread(DB.history, CTX_USER.get(), days, query)
    suffix = f" по запросу «{query}»" if query else ""
    if not rows:
        return f"📜 За последние {days} дн. ничего не найдено{suffix}."
    lines = [f"📜 История за {days} дн.{suffix}:"]
    for r in rows:
        when = r["created_at"].astimezone(TZ).strftime("%d.%m %H:%M")
        line = f"• {when} — {ACTION_LABELS.get(r['action'], r['action'])} «{r['title']}»"
        if r["project"] and r["project"] != r["title"]:
            line += f" ({r['project']})"
        lines.append(line)
    return "\n".join(lines)


async def act_stats(p: dict, projects: list[dict]) -> str:
    """Статистика действий за период: общие счётчики и разбивка по проектам."""
    if DB is None:
        raise UserError("Статистика недоступна: не удалось открыть базу данных.")
    days = clamp_days(p.get("days"))
    stats = await asyncio.to_thread(DB.stats, CTX_USER.get(), days)
    by_action = stats["by_action"]
    if not by_action:
        return f"📊 За последние {days} дн. действий не было."
    lines = [
        f"📊 Статистика за {days} дн.:",
        f"• создано задач: {by_action.get('task_create', 0)}",
        f"• изменено задач: {by_action.get('task_update', 0)}",
        f"• завершено задач: {by_action.get('task_complete', 0)}",
    ]
    if stats["by_project"]:
        lines.append("\nПо проектам (создано / изменено / завершено):")
        ordered = sorted(stats["by_project"].items(), key=lambda kv: -sum(kv[1].values()))
        for name, c in ordered:
            lines.append(
                f"• {name}: {c.get('task_create', 0)} / {c.get('task_update', 0)} / {c.get('task_complete', 0)}"
            )
    return "\n".join(lines)


async def weather_text(city: str | None = None) -> str:
    """Погода по названию города (или по городу из настроек) через Open-Meteo."""
    try:
        place = await integrations.geocode(city or WEATHER_CITY)
        data = await integrations.fetch_weather(place["lat"], place["lon"])
    except integrations.CityNotFound as e:
        raise UserError(str(e))
    return integrations.format_weather(place["name"], data)


async def act_weather(p: dict, projects: list[dict]) -> str:
    return await weather_text(p.get("city"))


async def fetch_tasks(projects: list[dict], status: str = "0") -> list[dict]:
    """Задачи из TickTick: status '0' - активные, '2' - выполненные."""
    args = ["task", "filter", "--status", status]
    if projects:
        args += ["--projects", ",".join(x["id"] for x in projects)]
    raw = await run_cli_json(*args)
    return [norm_task(t) for t in as_list(raw, "tasks") if isinstance(t, dict) and t.get("id")]


async def act_today(p: dict, projects: list[dict]) -> str:
    """План на сегодня: погода (API) + задачи со сроком на сегодня и просроченные (TickTick)."""
    projects = projects or await get_projects()
    tasks = await fetch_tasks(projects)
    today = datetime.now(TZ).date()
    due_today, overdue = [], []
    for t in tasks:
        dt = parse_api_dt(t["due"])
        if dt is None:
            continue
        if dt.date() < today:
            overdue.append((dt, t))
        elif dt.date() == today:
            due_today.append((dt, t))

    def fmt(dt: datetime, t: dict) -> str:
        when = f"{dt:%H:%M} " if (dt.hour or dt.minute) else ""
        extra = [project_name(t["project_id"], projects)]
        if t["priority"]:
            extra.append(PRIORITY_NAMES.get(t["priority"], str(t["priority"])))
        return f"• {when}{t['title']} ({', '.join(extra)})"

    lines = [f"📅 План на сегодня, {today:%d.%m.%Y}"]
    try:  # погода не должна ломать список дел
        lines.append(await weather_text(p.get("city")))
    except UserError as e:
        lines.append(f"🌤 {e}")
    except integrations.ApiError as e:
        lines.append(f"🌤 Погода недоступна: {e}.")
    lines.append("")
    if due_today:
        lines.append(f"Задач на сегодня: {len(due_today)}")
        lines += [fmt(dt, t) for dt, t in sorted(due_today, key=lambda x: (x[0], -x[1]["priority"]))]
    else:
        lines.append("На сегодня задач со сроком нет.")
    if overdue:
        lines.append(f"\n⚠️ Просрочено: {len(overdue)}")
        lines += [fmt(dt, t) for dt, t in sorted(overdue, key=lambda x: x[0])]
    return "\n".join(lines)


async def send_export(update: Update, status: str = "0") -> str:
    """Выгружает задачи в CSV-файл и отправляет его в чат. Возвращает пустую строку."""
    projects = await get_projects()
    tasks = await fetch_tasks(projects, status)
    if not tasks:
        raise UserError("Нет задач для выгрузки.")
    rows = []
    for t in tasks:
        dt = parse_api_dt(t["due"])
        rows.append({
            "title": t["title"],
            "project": project_name(t["project_id"], projects),
            "due": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
            "priority": PRIORITY_NAMES.get(t["priority"], ""),
            "tags": ";".join(str(x) for x in t["tags"]),
            "description": t["content"],
            "status": "выполнена" if t["status"] else "активна",
        })
    filename = f"tasks_{datetime.now(TZ):%Y-%m-%d}.csv"
    await update.message.reply_document(
        document=io.BytesIO(integrations.tasks_to_csv(rows)),
        filename=filename,
        caption=f"📤 Выгружено задач: {len(rows)}",
    )
    await log_action("task_export", filename, details=f"{len(rows)} задач")
    return ""


async def import_csv(update: Update, data: bytes) -> str:
    """Создаёт в TickTick задачи из присланного CSV; строки с ошибками пропускает и перечисляет."""
    try:
        tasks, errors = integrations.parse_tasks_csv(data)
    except integrations.CsvError as e:
        raise UserError(str(e))
    if not tasks:
        details = "\n".join(f"• {e}" for e in errors[:10])
        raise UserError("В файле нет подходящих строк." + (f"\n{details}" if details else ""))

    projects = await get_projects()
    by_name = {p["name"].casefold(): p["id"] for p in projects}
    await update.message.reply_text(f"📥 Читаю файл: найдено задач — {len(tasks)}. Создаю в TickTick…")

    created = 0
    for t in tasks:
        project_id = by_name.get(t["project"].casefold()) if t["project"] else None
        if t["project"] and not project_id:
            errors.append(f"строка {t['line']}: нет проекта «{t['project']}»")
            continue
        params = {
            "title": t["title"], "project_id": project_id, "content": t["content"] or None,
            "tags": t["tags"] or None, "priority": t["priority"], "due": t["due"],
        }
        try:
            await act_task_create(params, projects)  # без проекта задача уйдёт в DEFAULT_PROJECT_ID
            created += 1
        except (UserError, CliError) as e:
            errors.append(f"строка {t['line']}: {e}")

    lines = [f"📥 Импорт CSV: создано задач — {created}, пропущено строк — {len(errors)}."]
    if errors:
        lines.append("Что не получилось:")
        lines += [f"• {e}" for e in errors[:10]]
        if len(errors) > 10:
            lines.append(f"…и ещё {len(errors) - 10}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Новости о научных стартапах: RSS (английские источники) + перевод через LLM
# ---------------------------------------------------------------------------
TRANSLATE_PROMPT = """Ты - редактор новостного дайджеста. Тебе дают JSON-список английских новостей
(поля: i, title, summary). Для КАЖДОЙ новости верни по-русски:
- title_ru: перевод заголовка (коротко, без кавычек-ёлочек вокруг всего заголовка);
- summary_ru: пересказ сути ОДНИМ предложением своими словами (не больше 25 слов).
Названия компаний и продуктов оставляй латиницей. Ничего не выдумывай: опирайся только на данные.
Верни ТОЛЬКО JSON-массив вида [{"i": 0, "title_ru": "...", "summary_ru": "..."}, ...] без пояснений."""


async def translate_news(items: list[dict]) -> list[dict]:
    """Добавляет к новостям title_ru и summary_ru. Если LLM не справилась - остаются оригиналы."""
    payload = [{"i": n, "title": it["title"], "summary": it["summary"][:300]} for n, it in enumerate(items)]
    try:
        response = await llm.chat.completions.create(
            model=LLM_MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": TRANSLATE_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        content = response.choices[0].message.content or ""
        match = re.search(r"\[.*\]", content, re.DOTALL)
        translated = json.loads(match.group(0)) if match else []
        for row in translated:
            n = row.get("i")
            if isinstance(n, int) and 0 <= n < len(items):
                items[n]["title_ru"] = str(row.get("title_ru") or "").strip()
                items[n]["summary_ru"] = str(row.get("summary_ru") or "").strip()
    except Exception:
        # Перевод - приятное дополнение: без него дайджест всё равно отправится на английском.
        logger.exception("Не удалось перевести новости через LLM")
    return items


def format_news(items: list[dict], translated_ok: bool = True) -> str:
    """Собирает текст дайджеста: заголовок, пересказ, источник и ссылка."""
    lines = ["📰 Новости о научных и технологических стартапах", ""]
    for n, it in enumerate(items, 1):
        title = it.get("title_ru") or it["title"]
        lines.append(f"{n}. {title}")
        if it.get("summary_ru"):
            lines.append(f"   {it['summary_ru']}")
        lines.append(f"   {it['source']} · {it['link']}")
        lines.append("")
    if not any(it.get("title_ru") for it in items):
        lines.append("(перевод недоступен, показаны оригинальные заголовки)")
    return "\n".join(lines).strip()


async def act_news(p: dict, projects: list[dict]) -> str:
    """Свежие новости по запросу. Не помечает их отправленными (это делает только утренний дайджест)."""
    count = max(1, min(10, to_int(p.get("count"), NEWS_COUNT)))
    items = await integrations.fetch_news(limit=count)
    if not items:
        return "📰 За последние дни подходящих новостей не нашлось."
    items = await translate_news(items)
    return format_news(items)


async def news_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная задача JobQueue: присылает только новые (ещё не отправленные) новости."""
    user_id = context.job.chat_id
    CTX_USER.set(user_id)
    CTX_TEXT.set("утренний дайджест новостей")
    try:
        sent = await asyncio.to_thread(DB.sent_links, user_id) if DB else set()
        items = await integrations.fetch_news(limit=NEWS_COUNT, exclude=sent)
        if not items:
            logger.info("Дайджест: новых новостей нет")
            return
        items = await translate_news(items)
        await context.bot.send_message(user_id, format_news(items)[:4000], disable_web_page_preview=True)
        if DB:
            await asyncio.to_thread(DB.mark_sent, user_id, [it["link"] for it in items])
    except Exception as e:
        logger.warning("Дайджест не отправлен: %s", e)


def schedule_news(job_queue, user_id: int, hhmm: str) -> None:
    """Создаёт (или пересоздаёт) ежедневную задачу отправки дайджеста в указанное время."""
    for job in job_queue.get_jobs_by_name(f"news_{user_id}"):
        job.schedule_removal()
    hour, minute = map(int, hhmm.split(":"))
    job_queue.run_daily(
        news_digest_job, time=dtime(hour, minute, tzinfo=TZ),
        chat_id=user_id, name=f"news_{user_id}",
    )


ACTIONS = {
    "task_create": act_task_create,
    "task_update": act_task_update,
    "task_complete": act_task_complete,
    "task_search": act_task_search,
    "project_list": act_project_list,
    "project_create": act_project_create,
    "project_update": act_project_update,
    "habit_list": act_habit_list,
    "habit_create": act_habit_create,
    "habit_update": act_habit_update,
    "history": act_history,
    "stats": act_stats,
    "today": act_today,
    "weather": act_weather,
    "news": act_news,
}  # действие "export" обрабатывается отдельно: ему нужен доступ к чату для отправки файла

HELP_TEXT = (
    "Я перекладываю ваши сообщения в TickTick. Просто напишите, что нужно сделать:\n\n"
    "• «Завтра в 10 созвон с клиентом, высокий приоритет, напомни за 30 минут»\n"
    "• «Купить молоко #дом»\n"
    "• «Поставь ей средний приоритет» (про последнюю задачу)\n"
    "• «Перенеси задачу про отчёт в проект Работа»\n"
    "• «Найди задачи с тегом work и высоким приоритетом»\n"
    "• «Создай проект Отпуск», «Переименуй проект Отпуск в Путешествия»\n"
    "• «Покажи мои привычки», «Создай привычку пить воду, цель 8 стаканов»\n"
    "• «Что я делал за неделю?», «План на сегодня», «Какая погода в Казани?»\n\n"
    "Команды: /projects — проекты, /habits — привычки, /today [город] — план на сегодня и погода, "
    "/weather [город] — погода, /history [дни] [слово] — история действий, /stats [дни] — статистика, "
    "/export [done] — выгрузка задач в CSV, /news — новости о научных стартапах, "
    "/news_on [ЧЧ:ММ] — утренний дайджест (по умолчанию 08:00), /news_off — выключить, "
    "/feedback текст — оставить отзыв, /status — состояние бота, /help — справка.\n"
    "Пришлите CSV-файл с задачами (колонки: title, project, due, priority, tags, description), "
    "и я создам их в TickTick."
)


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------
def is_allowed(user_id: int) -> bool:
    """Если ALLOWED_USER_ID задан, бот отвечает только перечисленным людям."""
    return not ALLOWED_IDS or str(user_id) in ALLOWED_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text("Привет! Я бот-планировщик для TickTick.\n\n" + HELP_TEXT)


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await reply_safely(update, lambda: act_project_list({}, []))


async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await reply_safely(update, lambda: act_habit_list({}, []))


def format_error(exc: Exception) -> str:
    """Любое исключение -> понятное сообщение для чата."""
    if isinstance(exc, UserError):
        return f"❓ {exc}"
    if isinstance(exc, CliError):
        return f"⚠️ Ошибка TickTick CLI: {exc}"
    if isinstance(exc, integrations.ApiError):
        return f"⚠️ Внешний сервис недоступен: {exc}."
    # Ошибки сервиса LLM определяем по имени класса (так не нужно импортировать их из openai).
    llm_errors = {
        "NotFoundError": f"модель «{LLM_MODEL}» недоступна (на OpenRouter бесплатные модели часто убирают). "
                         "Выберите другую на openrouter.ai/models (фильтр Prices: Free), впишите её id в LLM_MODEL в .env и перезапустите бота",
        "BadRequestError": f"модель «{LLM_MODEL}» не подходит для чата или отклонила запрос. Нужна обычная текстовая (chat/instruct) модель, "
                           "а не специальная (decisions, image, embedding). Смените LLM_MODEL в .env и перезапустите бота",
        "AuthenticationError": "ключ LLM_API_KEY не принят. Проверьте его в .env и перезапустите бота",
        "RateLimitError": "лимит запросов к модели исчерпан. Подождите минуту или смените модель в LLM_MODEL",
        "APIConnectionError": "нет связи с сервисом LLM. Проверьте интернет и LLM_BASE_URL",
        "APITimeoutError": "сервис LLM не ответил вовремя. Повторите запрос",
    }
    if type(exc).__name__ in llm_errors:
        logger.error("Ошибка LLM: %s", exc)
        return f"⚠️ Ошибка LLM: {llm_errors[type(exc).__name__]}."
    if isinstance(exc, sqlite3.Error):
        logger.error("Ошибка базы данных", exc_info=exc)
        return "⚠️ Ошибка базы данных. Подробности в логе бота."
    logger.error("Необработанная ошибка", exc_info=exc)
    return "⚠️ Что-то пошло не так (LLM или сеть). Подробности в логе бота."


async def reply_safely(update: Update, make_reply) -> None:
    """Выполняет действие и превращает любые ошибки в понятное сообщение."""
    try:
        text = await make_reply()
    except Exception as e:
        text = format_error(e)
    if text:  # пустая строка - ответ уже отправлен (например, файл)
        await update.message.reply_text(text[:4000])


def remember_request(update: Update) -> None:
    """Запоминает, кто и чем обратился к боту (для записи в историю)."""
    CTX_USER.set(update.effective_user.id)
    CTX_TEXT.set((update.message.text or update.message.caption or "")[:500])


def split_days_and_query(args: list[str]) -> tuple[str | None, str | None]:
    """'/history 14 молоко' -> ('14', 'молоко'); число дней необязательно."""
    words = list(args)
    days = words.pop(0) if words and words[0].isdigit() else None
    return days, (" ".join(words) or None)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)
    days, query = split_days_and_query(context.args)
    await reply_safely(update, lambda: act_history({"days": days, "query": query}, []))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)
    days, _ = split_days_and_query(context.args)
    await reply_safely(update, lambda: act_stats({"days": days}, []))


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)
    await reply_safely(update, lambda: act_today({"city": " ".join(context.args) or None}, []))


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)
    await reply_safely(update, lambda: act_weather({"city": " ".join(context.args) or None}, []))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)
    done = any(a.casefold() in ("done", "выполненные", "выполнено") for a in context.args)
    await reply_safely(update, lambda: send_export(update, "2" if done else "0"))


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)
    await update.message.chat.send_action("typing")
    count = context.args[0] if context.args and context.args[0].isdigit() else None
    await reply_safely(update, lambda: act_news({"count": count}, []))


async def cmd_news_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/news_on [ЧЧ:ММ] - включает утренний дайджест."""
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)

    async def process() -> str:
        if DB is None:
            raise UserError("База данных недоступна, подписку сохранить нельзя.")
        if context.job_queue is None:
            raise UserError(
                "Планировщик не установлен. Выполните: pip install \"python-telegram-bot[job-queue]\" и перезапустите бота."
            )
        try:
            hhmm = integrations.parse_send_time(" ".join(context.args) or "08:00")
        except ValueError as e:
            raise UserError(str(e))
        user_id = update.effective_user.id
        await asyncio.to_thread(DB.set_news_sub, user_id, hhmm)
        schedule_news(context.job_queue, user_id, hhmm)
        await log_action("news_on", details=f"каждый день в {hhmm}")
        return f"✅ Дайджест включён: каждый день в {hhmm} ({TZ.key}) пришлю новости о научных стартапах. Выключить: /news_off"

    await reply_safely(update, process)


async def cmd_news_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)

    async def process() -> str:
        user_id = update.effective_user.id
        existed = await asyncio.to_thread(DB.remove_news_sub, user_id) if DB else False
        if context.job_queue is not None:
            for job in context.job_queue.get_jobs_by_name(f"news_{user_id}"):
                job.schedule_removal()
        if not existed:
            return "Дайджест и так не был включён. Включить: /news_on 08:00"
        await log_action("news_off")
        return "🔕 Дайджест выключен."

    await reply_safely(update, process)


def memory_mb() -> str:
    """Память процесса в МБ (читается из /proc на Linux; на Windows данных нет)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return f"{int(line.split()[1]) / 1024:.0f} МБ"
    except OSError:
        pass
    return "нет данных"


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/feedback текст - сохраняет отзыв пользователя в SQLite."""
    if not is_allowed(update.effective_user.id):
        return
    remember_request(update)
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Напишите отзыв после команды, например:\n/feedback Удобно, но хотелось бы голосовые сообщения.\n"
            "Что понравилось, что нет, что улучшить?"
        )
        return
    if DB is None:
        await update.message.reply_text("⚠️ База недоступна, отзыв сохранить не удалось.")
        return
    try:
        await asyncio.to_thread(DB.add_feedback, update.effective_user.id, text)
    except sqlite3.Error:
        logger.exception("Не удалось сохранить отзыв")
        await update.message.reply_text("⚠️ Не удалось сохранить отзыв.")
        return
    await update.message.reply_text("Спасибо! Отзыв сохранён 🙏")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status - состояние бота и статистика использования (для проверки стабильности и отчёта)."""
    if not is_allowed(update.effective_user.id):
        return
    uptime = int(time.time() - STARTED_AT)
    lines = [
        "🩺 Состояние бота",
        f"Работает: {uptime // 86400} д {uptime % 86400 // 3600} ч {uptime % 3600 // 60} мин",
        f"Память: {memory_mb()}",
        f"Модель LLM: {LLM_MODEL}",
    ]
    if DB is not None:
        try:
            usage = await asyncio.to_thread(DB.usage)
            total = sum(n for _, n, _ in usage["per_user"])
            lines += ["", f"Действий всего: {total}, пользователей: {len(usage['per_user'])}, отзывов: {usage['feedback']}"]
            for user_id, n, last in usage["per_user"][:10]:
                lines.append(f"• {user_id}: {n} (последнее {last[:16].replace('T', ' ')} UTC)")
            if usage["per_action"]:
                lines.append("Чаще всего: " + ", ".join(f"{a} — {n}" for a, n in usage["per_action"]))
        except sqlite3.Error:
            logger.exception("Не удалось получить статистику использования")
            lines.append("\nСтатистика недоступна (ошибка базы).")
    else:
        lines.append("\nБаза недоступна: статистики нет.")
    await update.message.reply_text("\n".join(lines)[:4000])


async def cmd_feedback_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/feedback_list - последние отзывы (только первому ID из ALLOWED_USER_ID, то есть владельцу)."""
    if not is_allowed(update.effective_user.id):
        return
    owner = re.split(r"[,; ]+", ALLOWED_USER_ID)[0] if ALLOWED_USER_ID else ""
    if owner and str(update.effective_user.id) != owner:
        await update.message.reply_text("Список отзывов доступен только владельцу бота.")
        return
    if DB is None:
        await update.message.reply_text("⚠️ База недоступна.")
        return
    rows = await asyncio.to_thread(DB.feedback_list, 15)
    if not rows:
        await update.message.reply_text("Отзывов пока нет.")
        return
    await update.message.reply_text("\n\n".join(
        f"{created[:16].replace('T', ' ')} UTC, {uid}:\n{text}" for uid, created, text in rows)[:4000])


async def handle_csv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь прислал CSV: создаём задачи из файла."""
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    remember_request(update)
    doc = update.message.document
    CTX_TEXT.set(f"CSV-файл: {doc.file_name}")

    async def process() -> str:
        if doc.file_size and doc.file_size > MAX_CSV_BYTES:
            raise UserError(f"Файл слишком большой (максимум {MAX_CSV_BYTES // 1000} КБ).")
        await update.message.chat.send_action("typing")
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        return await import_csv(update, data)

    await reply_safely(update, process)


async def handle_other_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "📎 Я принимаю только CSV-файлы с задачами. Колонки: title, project, due, priority, "
        "tags, description (достаточно title). Пример файла есть в README."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный обработчик: любое текстовое сообщение -> LLM -> действие в TickTick."""
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    remember_request(update)
    await update.message.chat.send_action("typing")

    async def process() -> str:
        projects = await get_projects()
        decision = await ask_llm(update.message.text, projects)
        action = decision.get("action")
        params = decision.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        logger.info("Действие: %s %s", action, params)

        if action == "project_delete":
            # Удаление необратимо - просим подтверждение кнопкой.
            project_id = valid_project(params.get("project_id"), projects)
            if not project_id:
                raise UserError("Не нашёл такой проект. Напишите его название точнее.")
            keyboard = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("🗑 Да, удалить", callback_data=f"delproj:{project_id}"),
                    InlineKeyboardButton("Отмена", callback_data="cancel:"),
                ]]
            )
            await update.message.reply_text(
                f"Удалить проект «{project_name(project_id, projects)}» вместе с задачами?",
                reply_markup=keyboard,
            )
            return ""

        if action == "export":
            return await send_export(update, "2" if params.get("status") == "done" else "0")

        handler = ACTIONS.get(action)
        if handler is None:
            return decision.get("comment") or HELP_TEXT
        return await handler(params, projects)

    try:
        text = await process()
    except Exception as e:
        text = format_error(e)
    if text:
        await update.message.reply_text(text[:4000])


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие кнопок подтверждения (удаление проекта)."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        return
    kind, _, value = (query.data or "").partition(":")
    if kind != "delproj":
        await query.edit_message_text("Отменено.")
        return
    try:
        name = project_name(value)
        CTX_USER.set(query.from_user.id)
        CTX_TEXT.set("кнопка подтверждения удаления проекта")
        await run_cli("project", "delete", value)
        await get_projects(force=True)
        await log_action("project_delete", name, name)
        await query.edit_message_text(f"🗑 Проект «{name}» удалён.")
    except (CliError, UserError) as e:
        await query.edit_message_text(f"⚠️ Не удалось удалить проект: {e}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка в обработчике", exc_info=context.error)


def main() -> None:
    global DB
    if not BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN. Скопируйте .env.example в .env и заполните.")
    if not LLM_API_KEY:
        raise SystemExit("Не задан LLM_API_KEY. Скопируйте .env.example в .env и заполните.")
    # Диагностика настроек LLM (сам ключ не печатается, только его длина и начало).
    logger.info(
        "LLM: адрес=%s, модель=%s, ключ: длина=%d, начало=%s...",
        LLM_BASE_URL or "OpenAI (по умолчанию)", LLM_MODEL, len(LLM_API_KEY), LLM_API_KEY[:6],
    )
    if LLM_BASE_URL and "openrouter" in LLM_BASE_URL and not LLM_API_KEY.startswith("sk-or-"):
        logger.warning("Ключ OpenRouter обычно начинается с 'sk-or-'. Проверьте LLM_API_KEY в .env!")
    if not ALLOWED_IDS:
        logger.warning(
            "ALLOWED_USER_ID не задан: бот отвечает ВСЕМ и может менять ваш TickTick!"
        )

    try:
        DB = integrations.HistoryDB(DB_FILE)
    except (sqlite3.Error, OSError):
        logger.exception("Не удалось открыть базу %s: история и статистика отключены", DB_FILE)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("habits", cmd_habits))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("feedback_list", cmd_feedback_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("news_on", cmd_news_on))
    app.add_handler(CommandHandler("news_off", cmd_news_off))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.FileExtension("csv"), handle_csv))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.FileExtension("csv"), handle_other_file))
    app.add_error_handler(on_error)

    # Восстанавливаем подписки на утренний дайджест после перезапуска бота.
    if DB is not None:
        if app.job_queue is None:
            logger.warning('JobQueue недоступен: установите "python-telegram-bot[job-queue]" для дайджеста новостей')
        else:
            try:
                for user_id, hhmm in DB.news_subs():
                    schedule_news(app.job_queue, user_id, hhmm)
                    logger.info("Дайджест новостей: пользователь %s, каждый день в %s", user_id, hhmm)
            except sqlite3.Error:
                logger.exception("Не удалось прочитать подписки на новости")

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
