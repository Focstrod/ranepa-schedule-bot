import os
import re
import json
import hashlib
from html import escape
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from google.oauth2 import service_account
from googleapiclient.discovery import build


# =========================================================
# НАСТРОЙКИ
# =========================================================

URL = "https://spb.ranepa.ru/raspisanie/mo-3-24-01-06/"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ[
    "GOOGLE_SERVICE_ACCOUNT_JSON"
]
CALENDAR_ID = os.environ["CALENDAR_ID"]

TARGET_GROUP = 3

MOSCOW = ZoneInfo("Europe/Moscow")

STATE_FILE = "state.json"

# Глубокий синий цвет Google Calendar
CALENDAR_COLOR_ID = "9"


DAY_NAMES = {
    0: "ПОНЕДЕЛЬНИК",
    1: "ВТОРНИК",
    2: "СРЕДА",
    3: "ЧЕТВЕРГ",
    4: "ПЯТНИЦА",
    5: "СУББОТА",
    6: "ВОСКРЕСЕНЬЕ",
}


# =========================================================
# БАЗОВЫЕ ФУНКЦИИ
# =========================================================

def clean(value):
    return " ".join(str(value).split()).strip()


def normalize_time(value):
    return clean(value).replace(".", ":")


def group_matches(group_text):
    """
    МО-3-24-03       -> да
    МО-3-24-01-03    -> да
    МО-3-24-01-06    -> да
    МО-3-24-01-02    -> нет
    МО-3-24-04-06    -> нет
    """

    text = clean(group_text)

    base = text.split("/")[0]

    match = re.search(
        r"МО-3-24-(\d{2})(?:-(\d{2}))?$",
        base
    )

    if not match:
        return False

    start = int(match.group(1))

    end = (
        int(match.group(2))
        if match.group(2)
        else start
    )

    return start <= TARGET_GROUP <= end


# =========================================================
# ДВЕ НЕДЕЛИ
# =========================================================

def get_weeks(now):
    """
    До субботы 22:00:
    текущая = текущая календарная неделя.

    С субботы 22:00:
    текущая = неделя с ближайшего понедельника.

    Всё воскресенье уже показывается новый период.
    """

    current_monday = (
        now.date()
        - timedelta(days=now.weekday())
    )

    if (
        now.weekday() == 5
        and now.time() >= time(22, 0)
    ):
        current_monday += timedelta(days=7)

    elif now.weekday() == 6:
        current_monday += timedelta(days=7)

    current_sunday = (
        current_monday
        + timedelta(days=6)
    )

    next_monday = (
        current_monday
        + timedelta(days=7)
    )

    next_sunday = (
        next_monday
        + timedelta(days=6)
    )

    return (
        current_monday,
        current_sunday,
        next_monday,
        next_sunday,
    )


# =========================================================
# ЗАГРУЗКА РАСПИСАНИЯ
# =========================================================

def fetch_schedule():

    response = requests.get(
        URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    schedule_table = None

    for table in soup.find_all("table"):

        table_text = table.get_text(
            " ",
            strip=True
        )

        if (
            "Наименование дисциплины" in table_text
            and "Группа" in table_text
            and "Время" in table_text
        ):
            schedule_table = table
            break

    if schedule_table is None:
        raise RuntimeError(
            "Таблица расписания не найдена."
        )

    rows = []

    for tr in schedule_table.find_all("tr"):

        cells = [
            clean(
                td.get_text(
                    " ",
                    strip=True
                )
            )
            for td in tr.find_all(
                ["td", "th"]
            )
        ]

        if len(cells) < 9:
            continue

        date_raw = cells[0]
        time_raw = cells[2]
        lesson_type = cells[4]
        group = cells[5]
        subject = cells[6]
        teacher = cells[7]
        room = cells[8]

        if not group_matches(group):
            continue

        try:

            lesson_date = datetime.strptime(
                date_raw,
                "%d.%m.%Y"
            ).date()

        except ValueError:
            continue

        if "-" not in time_raw:
            continue

        start_raw, end_raw = (
            time_raw.split("-", 1)
        )

        rows.append({
            "date":
                lesson_date.isoformat(),

            "start":
                normalize_time(start_raw),

            "end":
                normalize_time(end_raw),

            "type":
                clean(lesson_type),

            "group":
                clean(group),

            "subject":
                clean(subject),

            "teacher":
                clean(teacher),

            "room":
                clean(room),
        })

    if not rows:

        raise RuntimeError(
            "Не удалось получить расписание группы."
        )

    return rows


# =========================================================
# НОРМАЛИЗАЦИЯ
# =========================================================

def normalize_room(room):

    room = clean(room)

    if not room:
        return "кабинет не указан"

    if "СДО" in room.upper():
        return "СДО"

    return room


def normalize_lesson_type(value):

    value = clean(value).lower()

    if "лекц" in value:
        return "лекция"

    if (
        "практич" in value
        or "семинар" in value
    ):
        return "семинар"

    return value


# =========================================================
# СПЕЦИАЛЬНЫЕ ДИСЦИПЛИНЫ
# =========================================================

def is_second_language(subject):

    subject = clean(
        subject
    ).lower()

    return (
        subject == "второй иностранный язык"
        or subject.startswith(
            "второй иностранный язык "
        )
    )


def is_english(subject):

    subject = clean(
        subject
    ).lower()

    variants = {
        "иностранный язык",
        "английский язык",
        "иностранный язык (английский)",
        "английский",
    }

    return subject in variants


def is_physical_education(subject):

    subject = clean(
        subject
    ).lower()

    return (
        "физическая культура" in subject
        or "физической культуре" in subject
        or "физ. культура" in subject
        or "физ. культуре" in subject
    )


# =========================================================
# ПОДГОТОВКА ДАННЫХ
# =========================================================

def prepare_rows(
    rows,
    period_start,
    period_end
):

    result = []

    for row in rows:

        lesson_date = (
            datetime.fromisoformat(
                row["date"]
            ).date()
        )

        if not (
            period_start
            <= lesson_date
            <= period_end
        ):
            continue

        subject = row["subject"]

        # =================================================
        # ВТОРОЙ ИНОСТРАННЫЙ
        # =================================================

        if is_second_language(subject):

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],
                "kind": "second_language",
                "subject": "2 иностранный",
                "lesson_type": "",
                "room": "",
                "teacher": "",
            }

        # =================================================
        # АНГЛИЙСКИЙ
        # =================================================

        elif is_english(subject):

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],
                "kind": "english",
                "subject": "Английский язык",
                "lesson_type": "",
                "room": "",
                "teacher": "",
            }

        # =================================================
        # ФИЗРА
        # =================================================

        elif is_physical_education(subject):

            item = {
                "date": row["date"],

                # Настоящее время оставляем внутри
                "start": row["start"],
                "end": row["end"],

                "kind": "physical",
                "subject": "Физра",
                "lesson_type": "",
                "room": "стадион",
                "teacher": "",
            }

        # =================================================
        # ОБЫЧНАЯ ПАРА
        # =================================================

        else:

            item = {
                "date":
                    row["date"],

                "start":
                    row["start"],

                "end":
                    row["end"],

                "kind":
                    "normal",

                "subject":
                    subject,

                "lesson_type":
                    normalize_lesson_type(
                        row["type"]
                    ),

                "room":
                    normalize_room(
                        row["room"]
                    ),

                # В Telegram преподаватель не показывается,
                # но его изменение отслеживается.
                "teacher":
                    clean(
                        row["teacher"]
                    ),
            }

        result.append(
            item
        )

    # =====================================================
    # УДАЛЕНИЕ ДУБЛЕЙ
    # =====================================================

    unique = {}

    for row in result:

        if row["kind"] in {
            "english",
            "second_language",
            "physical",
        }:

            key = (
                row["date"],
                row["start"],
                row["end"],
                row["kind"],
            )

        else:

            key = (
                row["date"],
                row["start"],
                row["end"],
                row["kind"],
                row["subject"],
                row["lesson_type"],
                row["room"],
                row["teacher"],
            )

        unique[key] = row

    return list(
        unique.values()
    )


# =========================================================
# ОБЪЕДИНЕНИЕ ПАР
# =========================================================

OFFICIAL_ADJACENCY = {
    ("09:50", "10:00"),
    ("11:20", "12:00"),
    ("13:20", "13:30"),
    ("14:50", "15:00"),
    ("16:20", "16:30"),
    ("17:50", "18:30"),
    ("19:50", "20:00"),
}


def to_minutes(value):

    hours, minutes = map(
        int,
        value.split(":")
    )

    return (
        hours * 60
        + minutes
    )


def can_merge(a, b):

    if a["date"] != b["date"]:
        return False

    if a["kind"] != b["kind"]:
        return False

    if a["subject"] != b["subject"]:
        return False

    # Обычные пары объединяем только
    # при полном совпадении параметров.
    if a["kind"] == "normal":

        if (
            a["lesson_type"]
            != b["lesson_type"]
        ):
            return False

        if (
            a["room"]
            != b["room"]
        ):
            return False

        if (
            a["teacher"]
            != b["teacher"]
        ):
            return False

    if (
        a["end"],
        b["start"]
    ) in OFFICIAL_ADJACENCY:

        return True

    gap = (
        to_minutes(
            b["start"]
        )
        - to_minutes(
            a["end"]
        )
    )

    return (
        0 <= gap <= 30
    )


def merge_lessons(rows):

    rows = sorted(
        rows,
        key=lambda x: (
            x["date"],
            x["start"],
            x["subject"],
            x["kind"],
        )
    )

    merged = []

    for row in rows:

        current = row.copy()

        if not merged:

            merged.append(
                current
            )

            continue

        previous = (
            merged[-1]
        )

        if can_merge(
            previous,
            current
        ):

            previous["end"] = (
                current["end"]
            )

        else:

            merged.append(
                current
            )

    return merged


# =========================================================
# ОТОБРАЖАЕМОЕ ВРЕМЯ
# =========================================================

def display_start(row):

    # Физра отображается с 09:00
    if row["kind"] == "physical":
        return "09:00"

    return row["start"]


def display_time_range(row):

    return (
        f'{display_start(row)}'
        f'–'
        f'{row["end"]}'
    )


# =========================================================
# TELEGRAM — ОДНА ПАРА
# =========================================================

def format_lesson(row):

    time_text = (
        display_time_range(
            row
        )
    )

    subject = escape(
        row["subject"]
    )

    # Английский
    if row["kind"] == "english":

        return (
            f'⏰ {time_text} — '
            f'<b>Английский язык</b>'
        )

    # Второй иностранный
    if (
        row["kind"]
        == "second_language"
    ):

        return (
            f'⏰ {time_text} — '
            f'<b>2 иностранный</b>'
        )

    # Физра
    if row["kind"] == "physical":

        return (
            f'⏰ {time_text} — '
            f'<b>Физра</b> · стадион'
        )

    return (
        f'⏰ {time_text} — '
        f'<b>{subject}</b>'
        f' · {escape(row["lesson_type"])}'
        f' · {escape(row["room"])}'
    )


# =========================================================
# TELEGRAM — НЕДЕЛЯ
# =========================================================

def format_week(
    rows,
    monday,
    title,
    emoji
):

    saturday = (
        monday
        + timedelta(days=5)
    )

    sunday = (
        monday
        + timedelta(days=6)
    )

    by_date = {}

    for row in rows:

        by_date.setdefault(
            row["date"],
            []
        ).append(row)

    # Если неделя ещё не опубликована
    if not rows:

        return (
            f'{emoji} '
            f'<b>{title} · '
            f'{monday.strftime("%d.%m")}'
            f' — '
            f'{saturday.strftime("%d.%m")}'
            f'</b>\n\n'
            f'Расписание пока не опубликовано'
        )

    # Обычно ПН-СБ
    # Воскресенье добавляем только если есть пара
    if sunday.isoformat() in by_date:

        display_end = sunday
        number_of_days = 7

    else:

        display_end = saturday
        number_of_days = 6

    blocks = [
        (
            f'{emoji} '
            f'<b>{title} · '
            f'{monday.strftime("%d.%m")}'
            f' — '
            f'{display_end.strftime("%d.%m")}'
            f'</b>'
        )
    ]

    for offset in range(
        number_of_days
    ):

        day = (
            monday
            + timedelta(days=offset)
        )

        lessons = by_date.get(
            day.isoformat(),
            []
        )

        block = [
            (
                f'<b>— 📌 '
                f'{DAY_NAMES[day.weekday()]}'
                f' · '
                f'{day.strftime("%d.%m")}'
                f' —</b>'
            ),
            "",
        ]

        if lessons:

            lessons = sorted(
                lessons,
                key=lambda x: x["start"]
            )

            for lesson in lessons:

                block.append(
                    format_lesson(
                        lesson
                    )
                )

        else:

            block.append(
                "Нет занятий"
            )

        blocks.append(
            "\n".join(
                block
            )
        )

    return "\n\n".join(
        blocks
    )


# =========================================================
# ПРОШЕДШАЯ ЛИ ПАРА
# =========================================================

def lesson_is_past(
    row,
    now
):

    lesson_date = (
        datetime.fromisoformat(
            row["date"]
        ).date()
    )

    end_time = (
        datetime.strptime(
            row["end"],
            "%H:%M"
        ).time()
    )

    lesson_end = (
        datetime.combine(
            lesson_date,
            end_time,
            tzinfo=MOSCOW,
        )
    )

    return (
        lesson_end < now
    )


def comparison_rows(
    rows,
    now,
    ignore_past
):

    filtered = []

    for row in rows:

        if (
            ignore_past
            and lesson_is_past(
                row,
                now
            )
        ):

            continue

        filtered.append(
            row.copy()
        )

    return merge_lessons(
        filtered
    )


# =========================================================
# БЛОК ИЗМЕНЕНИЙ
# =========================================================

def pretty_date(
    iso_date
):

    return (
        datetime.fromisoformat(
            iso_date
        ).strftime(
            "%d.%m"
        )
    )


def lesson_brief(row):

    return (
        f'<b>{escape(row["subject"])}</b>'
        f' · {pretty_date(row["date"])}'
        f' · {display_time_range(row)}'
    )


def metadata_changes(
    old,
    new
):

    changes = []

    subject = escape(
        new["subject"]
    )

    date_text = pretty_date(
        new["date"]
    )

    time_text = display_time_range(
        new
    )

    # =====================================================
    # АУДИТОРИЯ
    # =====================================================

    if (
        old["kind"] == "normal"
        and new["kind"] == "normal"
        and old.get(
            "room",
            ""
        )
        != new.get(
            "room",
            ""
        )
    ):

        old_room = old.get(
            "room",
            ""
        )

        new_room = new.get(
            "room",
            ""
        )

        old_missing = (
            not old_room
            or old_room
            == "кабинет не указан"
        )

        new_missing = (
            not new_room
            or new_room
            == "кабинет не указан"
        )

        # Добавили аудиторию
        if (
            old_missing
            and not new_missing
        ):

            changes.append(
                f'📍 Добавлена аудитория: '
                f'<b>{subject}</b>'
                f' · {escape(new_room)}'
                f' · {date_text}'
                f' · {time_text}'
            )

        # Убрали аудиторию
        elif (
            not old_missing
            and new_missing
        ):

            changes.append(
                f'📍 Убрана аудитория: '
                f'<b>{subject}</b>'
                f' · {date_text}'
                f' · {time_text}'
            )

        # Изменили аудиторию
        else:

            changes.append(
                f'📍 Изменена аудитория: '
                f'<b>{subject}</b>'
                f' · {escape(old_room)}'
                f' → {escape(new_room)}'
                f' · {date_text}'
                f' · {time_text}'
            )

    # =====================================================
    # ТИП ЗАНЯТИЯ
    # =====================================================

    if (
        old["kind"] == "normal"
        and new["kind"] == "normal"
        and old.get(
            "lesson_type",
            ""
        )
        != new.get(
            "lesson_type",
            ""
        )
    ):

        changes.append(
            f'📝 Изменён тип занятия: '
            f'<b>{subject}</b>'
            f' · '
            f'{escape(old.get("lesson_type", ""))}'
            f' → '
            f'{escape(new.get("lesson_type", ""))}'
            f' · {date_text}'
            f' · {time_text}'
        )

    # =====================================================
    # ПРЕПОДАВАТЕЛЬ
    # =====================================================

    if (
        old["kind"] == "normal"
        and new["kind"] == "normal"
        and old.get(
            "teacher",
            ""
        )
        != new.get(
            "teacher",
            ""
        )
    ):

        changes.append(
            f'👤 Изменён преподаватель: '
            f'<b>{subject}</b>'
            f' · {date_text}'
            f' · {time_text}'
        )

    return changes


# =========================================================
# ПОИСК ИЗМЕНЕНИЙ
# =========================================================

def find_changes(
    old_rows,
    new_rows,
    now,
    ignore_past
):

    old = comparison_rows(
        old_rows,
        now,
        ignore_past
    )

    new = comparison_rows(
        new_rows,
        now,
        ignore_past
    )

    matched_old = set()
    matched_new = set()

    changes = []

    # =====================================================
    # 1. ТА ЖЕ ПАРА, ТО ЖЕ ВРЕМЯ
    # =====================================================

    for old_index, old_row in enumerate(
        old
    ):

        for new_index, new_row in enumerate(
            new
        ):

            if new_index in matched_new:
                continue

            same = (
                old_row["date"]
                == new_row["date"]

                and old_row["start"]
                == new_row["start"]

                and old_row["end"]
                == new_row["end"]

                and old_row["kind"]
                == new_row["kind"]

                and old_row["subject"]
                == new_row["subject"]
            )

            if not same:
                continue

            matched_old.add(
                old_index
            )

            matched_new.add(
                new_index
            )

            changes.extend(
                metadata_changes(
                    old_row,
                    new_row
                )
            )

            break

    # =====================================================
    # 2. ИЗМЕНИЛОСЬ ВРЕМЯ
    # =====================================================

    for old_index, old_row in enumerate(
        old
    ):

        if old_index in matched_old:
            continue

        candidates = []

        for new_index, new_row in enumerate(
            new
        ):

            if new_index in matched_new:
                continue

            same_lesson = (
                old_row["date"]
                == new_row["date"]

                and old_row["kind"]
                == new_row["kind"]

                and old_row["subject"]
                == new_row["subject"]
            )

            if same_lesson:

                candidates.append(
                    (
                        new_index,
                        new_row
                    )
                )

        if len(candidates) == 1:

            new_index, new_row = (
                candidates[0]
            )

            matched_old.add(
                old_index
            )

            matched_new.add(
                new_index
            )

            old_time = (
                display_time_range(
                    old_row
                )
            )

            new_time = (
                display_time_range(
                    new_row
                )
            )

            if old_time != new_time:

                changes.append(
                    f'⏰ Изменено время: '
                    f'<b>{escape(new_row["subject"])}</b>'
                    f' · {pretty_date(new_row["date"])}'
                    f' · {old_time}'
                    f' → {new_time}'
                )

            changes.extend(
                metadata_changes(
                    old_row,
                    new_row
                )
            )

    # =====================================================
    # 3. УБРАЛИ ПАРУ
    # =====================================================

    for old_index, old_row in enumerate(
        old
    ):

        if old_index in matched_old:
            continue

        changes.append(
            f'❌ Убрана пара: '
            f'{lesson_brief(old_row)}'
        )

    # =====================================================
    # 4. ДОБАВИЛИ ПАРУ
    # =====================================================

    for new_index, new_row in enumerate(
        new
    ):

        if new_index in matched_new:
            continue

        changes.append(
            f'🆕 Добавлена пара: '
            f'{lesson_brief(new_row)}'
        )

    return changes


# =========================================================
# TELEGRAM — ПОЛНОЕ СООБЩЕНИЕ
# =========================================================

def format_schedule(
    current_rows,
    next_rows,
    current_monday,
    next_monday,
    changes=None
):

    current_merged = (
        merge_lessons(
            current_rows
        )
    )

    next_merged = (
        merge_lessons(
            next_rows
        )
    )

    parts = []

    # =====================================================
    # ЕСЛИ РАСПИСАНИЕ ИЗМЕНИЛОСЬ
    # =====================================================

    if changes:

        parts.append(
            '🔔 <b>РАСПИСАНИЕ ОБНОВЛЕНО</b>'
        )

        parts.append(
            '<b>Что изменилось:</b>\n\n'
            + "\n".join(
                changes
            )
        )

        parts.append(
            "━━━━━━━━━━━━━"
        )

    # =====================================================
    # ТЕКУЩАЯ НЕДЕЛЯ
    # =====================================================

    parts.append(
        format_week(
            current_merged,
            current_monday,
            "ТЕКУЩАЯ НЕДЕЛЯ",
            "📅"
        )
    )

    parts.append(
        "━━━━━━━━━━━━━"
    )

    # =====================================================
    # СЛЕДУЮЩАЯ НЕДЕЛЯ
    # =====================================================

    parts.append(
        format_week(
            next_merged,
            next_monday,
            "СЛЕДУЮЩАЯ НЕДЕЛЯ",
            "⏭️"
        )
    )

    return "\n\n".join(
        parts
    )


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_request(
    method,
    data
):

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    response = requests.post(
        url,
        data=data,
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):

        raise RuntimeError(
            str(
                result.get(
                    "description",
                    "Telegram error"
                )
            )
        )

    return result


def send_schedule(text):

    result = telegram_request(
        "sendMessage",
        {
            "chat_id":
                CHAT_ID,

            "text":
                text,

            "parse_mode":
                "HTML",

            "disable_web_page_preview":
                True,
        },
    )

    return (
        result[
            "result"
        ][
            "message_id"
        ]
    )


def delete_message(
    message_id
):

    telegram_request(
        "deleteMessage",
        {
            "chat_id":
                CHAT_ID,

            "message_id":
                message_id,
        },
    )


def replace_schedule_message(
    text,
    old_message_id
):

    # Сначала отправляем новое
    new_message_id = (
        send_schedule(
            text
        )
    )

    # И только потом удаляем старое
    if old_message_id:

        try:

            delete_message(
                old_message_id
            )

            print(
                "Старое сообщение удалено."
            )

        except Exception as exc:

            print(
                "Новое сообщение отправлено, "
                "но старое удалить не удалось:",
                exc
            )

    return new_message_id


# =========================================================
# GOOGLE CALENDAR
# =========================================================

def get_calendar_service():

    service_account_info = (
        json.loads(
            GOOGLE_SERVICE_ACCOUNT_JSON
        )
    )

    credentials = (
        service_account
        .Credentials
        .from_service_account_info(
            service_account_info,
            scopes=[
                "https://www.googleapis.com/auth/calendar"
            ],
        )
    )

    return build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def calendar_location(row):

    if row["kind"] == "physical":
        return "стадион"

    if row["kind"] in {
        "english",
        "second_language",
    }:
        return ""

    room = row.get(
        "room",
        ""
    )

    if room == "кабинет не указан":
        return ""

    return room


def calendar_lesson_uid(
    row,
    occurrence
):

    source = (
        f'{row["date"]}|'
        f'{row["kind"]}|'
        f'{row["subject"]}|'
        f'{occurrence}'
    )

    return hashlib.sha1(
        source.encode(
            "utf-8"
        )
    ).hexdigest()


def build_calendar_events(rows):

    merged = merge_lessons(
        rows
    )

    merged = sorted(
        merged,
        key=lambda x: (
            x["date"],
            x["subject"],
            x["start"],
        )
    )

    counters = {}

    desired = {}

    for row in merged:

        base = (
            row["date"],
            row["kind"],
            row["subject"],
        )

        counters[base] = (
            counters.get(
                base,
                0
            )
            + 1
        )

        occurrence = (
            counters[base]
        )

        lesson_uid = (
            calendar_lesson_uid(
                row,
                occurrence
            )
        )

        lesson_date = (
            datetime.fromisoformat(
                row["date"]
            ).date()
        )

        start_time = (
            datetime.strptime(
                display_start(
                    row
                ),
                "%H:%M"
            ).time()
        )

        end_time = (
            datetime.strptime(
                row["end"],
                "%H:%M"
            ).time()
        )

        start_dt = (
            datetime.combine(
                lesson_date,
                start_time,
                tzinfo=MOSCOW,
            )
        )

        end_dt = (
            datetime.combine(
                lesson_date,
                end_time,
                tzinfo=MOSCOW,
            )
        )

        body = {
            "summary":
                row["subject"],

            "location":
                calendar_location(
                    row
                ),

            "start": {
                "dateTime":
                    start_dt.isoformat(),

                "timeZone":
                    "Europe/Moscow",
            },

            "end": {
                "dateTime":
                    end_dt.isoformat(),

                "timeZone":
                    "Europe/Moscow",
            },

            "colorId":
                CALENDAR_COLOR_ID,

            "reminders": {
                "useDefault": False,
                "overrides": [
                    {
                        "method": "popup",
                        "minutes": 60
                    }
                ]
            },

            "extendedProperties": {
                "private": {
                    "ranepa_schedule_bot":
                        "true",

                    "lesson_uid":
                        lesson_uid,
                }
            },
        }

        desired[
            lesson_uid
        ] = body

    return desired


def get_existing_calendar_events(
    service,
    period_start,
    period_end
):

    time_min = (
        datetime.combine(
            period_start,
            time.min,
            tzinfo=MOSCOW,
        ).isoformat()
    )

    time_max = (
        datetime.combine(
            period_end
            + timedelta(days=1),
            time.min,
            tzinfo=MOSCOW,
        ).isoformat()
    )

    events = []

    page_token = None

    while True:

        result = (
            service.events()
            .list(
                calendarId=
                    CALENDAR_ID,

                timeMin=
                    time_min,

                timeMax=
                    time_max,

                singleEvents=
                    True,

                showDeleted=
                    False,

                maxResults=
                    2500,

                pageToken=
                    page_token,

                privateExtendedProperty=
                    "ranepa_schedule_bot=true",
            )
            .execute()
        )

        events.extend(
            result.get(
                "items",
                []
            )
        )

        page_token = (
            result.get(
                "nextPageToken"
            )
        )

        if not page_token:
            break

    return events


def event_changed(
    existing,
    desired
):

    existing_private = (
        existing
        .get(
            "extendedProperties",
            {}
        )
        .get(
            "private",
            {}
        )
    )

    desired_private = (
        desired[
            "extendedProperties"
        ][
            "private"
        ]
    )

    return (
        existing.get(
            "summary",
            ""
        )
        != desired.get(
            "summary",
            ""
        )

        or existing.get(
            "location",
            ""
        )
        != desired.get(
            "location",
            ""
        )

        or str(
            existing.get(
                "colorId",
                ""
            )
        )
        != str(
            desired.get(
                "colorId",
                ""
            )
        )

        or existing.get(
            "start",
            {}
        ).get(
            "dateTime",
            ""
        )
        != desired.get(
            "start",
            {}
        ).get(
            "dateTime",
            ""
        )

        or existing.get(
            "end",
            {}
        ).get(
            "dateTime",
            ""
        )
        != desired.get(
            "end",
            {}
        ).get(
            "dateTime",
            ""
        )

        or existing_private.get(
            "lesson_uid"
        )
        != desired_private.get(
            "lesson_uid"
        )
    )


def sync_google_calendar(
    current_rows,
    next_rows,
    current_monday,
    next_sunday
):

    print(
        "Синхронизация Google Calendar..."
    )

    service = (
        get_calendar_service()
    )

    desired = (
        build_calendar_events(
            current_rows
            + next_rows
        )
    )

    existing_events = (
        get_existing_calendar_events(
            service,
            current_monday,
            next_sunday,
        )
    )

    existing_by_uid = {}

    for event in existing_events:

        private = (
            event
            .get(
                "extendedProperties",
                {}
            )
            .get(
                "private",
                {}
            )
        )

        lesson_uid = (
            private.get(
                "lesson_uid"
            )
        )

        if lesson_uid:

            existing_by_uid[
                lesson_uid
            ] = event

    created = 0
    updated = 0
    deleted = 0

    # =====================================================
    # СОЗДАЁМ / ОБНОВЛЯЕМ
    # =====================================================

    for lesson_uid, body in (
        desired.items()
    ):

        existing = (
            existing_by_uid.get(
                lesson_uid
            )
        )

        if existing is None:

            (
                service.events()
                .insert(
                    calendarId=
                        CALENDAR_ID,

                    body=
                        body,
                )
                .execute()
            )

            created += 1

            continue

        if event_changed(
            existing,
            body
        ):

            (
                service.events()
                .update(
                    calendarId=
                        CALENDAR_ID,

                    eventId=
                        existing["id"],

                    body=
                        body,
                )
                .execute()
            )

            updated += 1

    # =====================================================
    # УДАЛЯЕМ ПАРЫ, КОТОРЫХ БОЛЬШЕ НЕТ
    # =====================================================

    for lesson_uid, event in (
        existing_by_uid.items()
    ):

        if lesson_uid in desired:
            continue

        (
            service.events()
            .delete(
                calendarId=
                    CALENDAR_ID,

                eventId=
                    event["id"],
            )
            .execute()
        )

        deleted += 1

    print(
        "Google Calendar:",
        f"добавлено {created},",
        f"изменено {updated},",
        f"удалено {deleted}"
    )


# =========================================================
# STATE.JSON
# =========================================================

def load_state():

    if not os.path.exists(
        STATE_FILE
    ):

        return {}

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(
                file
            )

    except Exception:

        return {}


def save_state(state):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )


def build_state(
    current_monday,
    current_sunday,
    next_monday,
    next_sunday,
    message_id,
    current_rows,
    next_rows
):

    return {
        "current_week_start":
            current_monday.isoformat(),

        "current_week_end":
            current_sunday.isoformat(),

        "next_week_start":
            next_monday.isoformat(),

        "next_week_end":
            next_sunday.isoformat(),

        "message_id":
            message_id,

        "current_schedule":
            current_rows,

        "next_schedule":
            next_rows,
    }


# =========================================================
# ОСНОВНАЯ ЛОГИКА
# =========================================================

def main():

    now = datetime.now(
        MOSCOW
    )

    print(
        "Сейчас:",
        now.isoformat()
    )

    (
        current_monday,
        current_sunday,
        next_monday,
        next_sunday,
    ) = get_weeks(
        now
    )

    print(
        "Текущая неделя:",
        current_monday,
        "—",
        current_sunday,
    )

    print(
        "Следующая неделя:",
        next_monday,
        "—",
        next_sunday,
    )

    # =====================================================
    # ПОЛУЧАЕМ РАСПИСАНИЕ
    # =====================================================

    all_rows = (
        fetch_schedule()
    )

    prepared = (
        prepare_rows(
            all_rows,
            current_monday,
            next_sunday,
        )
    )

    current_rows = []
    next_rows = []

    for row in prepared:

        lesson_date = (
            datetime.fromisoformat(
                row["date"]
            ).date()
        )

        if (
            current_monday
            <= lesson_date
            <= current_sunday
        ):

            current_rows.append(
                row
            )

        elif (
            next_monday
            <= lesson_date
            <= next_sunday
        ):

            next_rows.append(
                row
            )

    # =====================================================
    # СОСТОЯНИЕ
    # =====================================================

    state = (
        load_state()
    )

    current_period = (
        current_monday.isoformat()
    )

    saved_period = (
        state.get(
            "current_week_start"
        )
    )

    old_message_id = (
        state.get(
            "message_id"
        )
    )

    # =====================================================
    # ЗАЩИТА ОТ ОШИБКИ САЙТА
    # =====================================================

    if (
        saved_period
        == current_period

        and not current_rows

        and state.get(
            "current_schedule"
        )
    ):

        print(
            "Текущая неделя внезапно пустая. "
            "Используем сохранённые данные."
        )

        current_rows = (
            state[
                "current_schedule"
            ]
        )

    if (
        state.get(
            "next_week_start"
        )
        == next_monday.isoformat()

        and not next_rows

        and state.get(
            "next_schedule"
        )
    ):

        print(
            "Следующая неделя внезапно пустая. "
            "Используем сохранённые данные."
        )

        next_rows = (
            state[
                "next_schedule"
            ]
        )

    # =====================================================
    # GOOGLE CALENDAR
    #
    # Проверяется и синхронизируется каждый запуск.
    # =====================================================

    try:

        sync_google_calendar(
            current_rows,
            next_rows,
            current_monday,
            next_sunday,
        )

    except Exception as exc:

        print(
            "ОШИБКА GOOGLE CALENDAR:",
            repr(exc)
        )

    # =====================================================
    # НОВЫЙ ПЕРИОД
    #
    # Срабатывает в субботу после 22:00,
    # потому что current_monday переключается вперёд.
    # =====================================================

    if (
        saved_period
        != current_period
    ):

        print(
            "Публикуем новый двухнедельный период."
        )

        text = (
            format_schedule(
                current_rows,
                next_rows,
                current_monday,
                next_monday,
                changes=None,
            )
        )

        new_message_id = (
            replace_schedule_message(
                text,
                old_message_id
            )
        )

        save_state(
            build_state(
                current_monday,
                current_sunday,
                next_monday,
                next_sunday,
                new_message_id,
                current_rows,
                next_rows,
            )
        )

        print(
            "Новый период опубликован."
        )

        return

    # =====================================================
    # СРАВНИВАЕМ С САЙТОМ
    # =====================================================

    old_current = (
        state.get(
            "current_schedule",
            []
        )
    )

    old_next = (
        state.get(
            "next_schedule",
            []
        )
    )

    # Текущая неделя:
    # уже прошедшие пары не вызывают Telegram-обновление.
    current_changes = (
        find_changes(
            old_current,
            current_rows,
            now,
            ignore_past=True,
        )
    )

    # Следующая неделя:
    # отслеживаем полностью.
    next_changes = (
        find_changes(
            old_next,
            next_rows,
            now,
            ignore_past=False,
        )
    )

    changes = (
        current_changes
        + next_changes
    )

    # =====================================================
    # НИЧЕГО НА САЙТЕ НЕ ИЗМЕНИЛОСЬ
    # =====================================================

    if not changes:

        print(
            "Изменений на сайте нет. "
            "Telegram не обновляем."
        )

        return

    # =====================================================
    # НА САЙТЕ ЕСТЬ ИЗМЕНЕНИЯ
    # =====================================================

    print(
        "На сайте изменилось расписание:"
    )

    for change in changes:

        print(
            re.sub(
                r"<[^>]+>",
                "",
                change
            )
        )

    text = (
        format_schedule(
            current_rows,
            next_rows,
            current_monday,
            next_monday,
            changes=changes,
        )
    )

    new_message_id = (
        replace_schedule_message(
            text,
            old_message_id
        )
    )

    save_state(
        build_state(
            current_monday,
            current_sunday,
            next_monday,
            next_sunday,
            new_message_id,
            current_rows,
            next_rows,
        )
    )

    print(
        "Telegram обновлён."
    )


if __name__ == "__main__":
    main()
