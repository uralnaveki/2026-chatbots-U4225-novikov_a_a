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
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()  # ваш Telegram ID
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

CLI_TIMEOUT = 30          # секунд на один вызов CLI
PROJECTS_TTL = 300        # как долго (сек.) хранить кэш списка проектов
PRIORITY_NAMES = {0: "нет", 1: "низкий", 3: "средний", 5: "высокий"}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("planner-bot")

llm = AsyncOpenAI(api_key=LLM_API_KEY or "missing", base_url=LLM_BASE_URL)


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
#   last_task - последняя созданная/найденная задача, чтобы можно было сказать
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
    }


async def find_task(query, projects: list[dict]) -> dict:
    """Ищет одну задачу по словам из названия. Без запроса - берёт последнюю задачу."""
    if not query:
        last = DATA.get("last_task")
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
    DATA["last_task"] = task
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


def show_api_date(value) -> str:
    """Дата из ответа CLI (UTC) -> ваше время для показа."""
    if not value:
        return ""
    try:
        text = str(value).replace("Z", "+00:00").replace("+0000", "+00:00")
        return datetime.fromisoformat(text).astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(value)


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
- chat: если запрос не про задачи/проекты/привычки или непонятен - ответь в comment.

Правила:
- Относительные даты («завтра», «в пятницу», «через 2 часа») считай от текущего времени. Если указан день без времени - ставь 09:00.
- Проект выбирай по смыслу задачи (работа, учёба, дом, покупки и т.д.). Если ни один не подходит - project_id: null.
- Название задачи делай коротким и без служебных слов («поставь», «напомни»).
- Напоминание без срока невозможно: в этом случае всё равно верни reminder_minutes, бот сам сообщит об ошибке.
"""


async def ask_llm(text: str, projects: list[dict]) -> dict:
    last = DATA.get("last_task")
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
        DATA["last_task"] = norm_task({"projectId": project_id, **created})
        save_data()

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

    DATA["last_task"] = task
    save_data()
    return f"✏️ Задача «{task['title']}» обновлена: " + "; ".join(changes) + "."


async def act_task_complete(p: dict, projects: list[dict]) -> str:
    task = await find_task(p.get("task_query"), projects)
    await run_cli("task", "complete", task["project_id"], task["id"])
    DATA.pop("last_task", None)
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
    DATA["last_task"] = tasks[0] if len(tasks) == 1 else DATA.get("last_task")
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
    return f"✏️ Привычка «{habit['name']}» обновлена: " + "; ".join(changes) + "."


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
}

HELP_TEXT = (
    "Я перекладываю ваши сообщения в TickTick. Просто напишите, что нужно сделать:\n\n"
    "• «Завтра в 10 созвон с клиентом, высокий приоритет, напомни за 30 минут»\n"
    "• «Купить молоко #дом»\n"
    "• «Поставь ей средний приоритет» (про последнюю задачу)\n"
    "• «Перенеси задачу про отчёт в проект Работа»\n"
    "• «Найди задачи с тегом work и высоким приоритетом»\n"
    "• «Создай проект Отпуск», «Переименуй проект Отпуск в Путешествия»\n"
    "• «Покажи мои привычки», «Создай привычку пить воду, цель 8 стаканов»\n\n"
    "Команды: /projects — список проектов, /habits — привычки, /help — эта справка."
)


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------
def is_allowed(user_id: int) -> bool:
    """Если ALLOWED_USER_ID задан, бот отвечает только вам."""
    return not ALLOWED_USER_ID or str(user_id) == ALLOWED_USER_ID


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


async def reply_safely(update: Update, make_reply) -> None:
    """Выполняет действие и превращает любые ошибки в понятное сообщение."""
    try:
        text = await make_reply()
    except UserError as e:
        text = f"❓ {e}"
    except CliError as e:
        text = f"⚠️ Ошибка TickTick CLI: {e}"
    except Exception:
        logger.exception("Необработанная ошибка")
        text = "⚠️ Что-то пошло не так (LLM или сеть). Подробности в логе бота."
    await update.message.reply_text(text[:4000])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный обработчик: любое текстовое сообщение -> LLM -> действие в TickTick."""
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
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

        handler = ACTIONS.get(action)
        if handler is None:
            return decision.get("comment") or HELP_TEXT
        return await handler(params, projects)

    try:
        text = await process()
    except UserError as e:
        text = f"❓ {e}"
    except CliError as e:
        text = f"⚠️ Ошибка TickTick CLI: {e}"
    except Exception:
        logger.exception("Необработанная ошибка")
        text = "⚠️ Что-то пошло не так (LLM или сеть). Подробности в логе бота."
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
        await run_cli("project", "delete", value)
        await get_projects(force=True)
        await query.edit_message_text(f"🗑 Проект «{name}» удалён.")
    except (CliError, UserError) as e:
        await query.edit_message_text(f"⚠️ Не удалось удалить проект: {e}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка в обработчике", exc_info=context.error)


def main() -> None:
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
    if not ALLOWED_USER_ID:
        logger.warning(
            "ALLOWED_USER_ID не задан: бот отвечает ВСЕМ и может менять ваш TickTick!"
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("habits", cmd_habits))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
