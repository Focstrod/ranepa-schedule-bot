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


URL = "https://spb.ranepa.ru/raspisanie/mo-3-24-01-06/"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    ""
)

CALENDAR_ID = os.environ.get(
    "CALENDAR_ID",
    ""
)

TARGET_GROUP = 3

MOSCOW = ZoneInfo(
    "Europe/Moscow"
)

STATE_FILE = "state.json"

STATE_SCHEMA_VERSION = 2

# Глубокий синий
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
    return " ".join(
        str(value or "").split()
    ).strip()


def normalize_time(value):
    return clean(
        value
    ).replace(
        ".",
        ":"
    )


# =========================================================
# ПРОВЕРКА ГРУППЫ
# =========================================================

def group_matches(group_text):

    base = (
        clean(group_text)
        .split("/")[0]
        .replace(" ", "")
    )

    match = re.fullmatch(
        r"МО-3-24-(\d{2})(?:-(\d{2}))?(?:[А-ЯЁA-Z]+)?",
        base,
        flags=re.IGNORECASE,
    )

    if not match:
        return False

    start = int(
        match.group(1)
    )

    end = (
        int(match.group(2))
        if match.group(2)
        else start
    )

    return (
        start
        <= TARGET_GROUP
        <= end
    )


# =========================================================
# ДВЕ НЕДЕЛИ
# =========================================================

def get_weeks(now):

    current_monday = (
        now.date()
        - timedelta(
            days=now.weekday()
        )
    )

    # Суббота с 22:00
    # переключаемся вперёд
    if (
        now.weekday() == 5
        and now.time() >= time(22, 0)
    ):
        current_monday += timedelta(
            days=7
        )

    # Всё воскресенье
    elif now.weekday() == 6:

        current_monday += timedelta(
            days=7
        )

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
# ЗАГОЛОВКИ ТАБЛИЦЫ
# =========================================================

def header_key(text):

    headers = {
        "дата":
            "date",

        "время":
            "time",

        "вид занятия":
            "type",

        "тип занятия":
            "type",

        "группа":
            "group",

        "дисциплина":
            "subject",

        "наименование дисциплины":
            "subject",

        "преподаватель":
            "teacher",

        "аудитория":
            "room",

        "адрес":
            "address",
    }

    return headers.get(
        clean(text).lower()
    )


# =========================================================
# ЗАГРУЗКА РАСПИСАНИЯ
# =========================================================

def fetch_schedule():

    response = requests.get(
        URL,
        timeout=30,
        headers={
            "User-Agent":
                "Mozilla/5.0"
        },
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    schedule_table = None
    header_map = {}

    for table in soup.find_all(
        "table"
    ):

        headers = [
            clean(
                th.get_text(
                    " ",
                    strip=True
                )
            )
            for th in table.find_all(
                "th"
            )
        ]

        if not headers:

            first_row = table.find(
                "tr"
            )

            if first_row:

                headers = [
                    clean(
                        cell.get_text(
                            " ",
                            strip=True
                        )
                    )
                    for cell
                    in first_row.find_all(
                        ["td", "th"]
                    )
                ]

        candidate = {}

        for index, header in enumerate(
            headers
        ):

            key = header_key(
                header
            )

            if key:
                candidate[key] = index

        required = {
            "date",
            "time",
            "group",
            "subject",
        }

        if required.issubset(
            candidate
        ):

            schedule_table = table
            header_map = candidate
            break

    if schedule_table is None:

        raise RuntimeError(
            "Таблица расписания не найдена."
        )

    defaults = {
        "date": 0,
        "time": 2,
        "type": 4,
        "group": 5,
        "subject": 6,
        "teacher": 7,
        "room": 8,
        "address": 9,
    }

    indices = {
        key:
            header_map.get(
                key,
                value
            )
        for key, value
        in defaults.items()
    }

    rows = []

    for tr in schedule_table.find_all(
        "tr"
    ):

        cells = [
            clean(
                cell.get_text(
                    " ",
                    strip=True
                )
            )
            for cell in tr.find_all(
                ["td", "th"]
            )
        ]

        if not cells:
            continue

        def cell(name):

            index = indices[
                name
            ]

            if index < len(cells):
                return cells[index]

            return ""

        required_fields = (
            "date",
            "time",
            "group",
            "subject",
        )

        if not all(
            indices[name] < len(cells)
            for name
            in required_fields
        ):
            continue

        if not group_matches(
            cell("group")
        ):
            continue

        try:

            lesson_date = (
                datetime.strptime(
                    cell("date"),
                    "%d.%m.%Y"
                ).date()
            )

        except ValueError:
            continue

        time_raw = cell(
            "time"
        )

        if "-" not in time_raw:
            continue

        start_raw, end_raw = (
            time_raw.split(
                "-",
                1
            )
        )

        rows.append({
            "date":
                lesson_date.isoformat(),

            "start":
                normalize_time(
                    start_raw
                ),

            "end":
                normalize_time(
                    end_raw
                ),

            "type":
                clean(
                    cell("type")
                ),

            "group":
                clean(
                    cell("group")
                ),

            "subject":
                clean(
                    cell("subject")
                ),

            "teacher":
                clean(
                    cell("teacher")
                ),

            "room":
                clean(
                    cell("room")
                ),

            "address":
                clean(
                    cell("address")
                ),
        })

    if not rows:

        raise RuntimeError(
            "Не удалось получить "
            "расписание МО-3-24-03."
        )

    return rows


# =========================================================
# НОРМАЛИЗАЦИЯ
# =========================================================

def normalize_room(room):

    room = clean(
        room
    )

    if not room:

        return (
            "кабинет не указан"
        )

    if "СДО" in room.upper():

        return "СДО"

    return room


def normalize_address(address):

    address = clean(
        address
    )

    low = address.lower()

    if not address:
        return ""

    if "тучков" in low:
        return "Тучков"

    if "средн" in low:
        return "Средний"

    return address


def normalize_lesson_type(value):

    value = clean(
        value
    ).lower()

    if "лекц" in value:
        return "лекция"

    if (
        "практич" in value
        or "семинар" in value
    ):
        return "семинар"

    return value


# =========================================================
# ИНОСТРАННЫЕ ЯЗЫКИ
# =========================================================

def is_second_language(subject):

    subject = clean(
        subject
    ).lower()

    return (
        subject
        == "второй иностранный язык"

        or subject.startswith(
            "второй иностранный язык "
        )
    )


def is_english(subject):

    subject = clean(
        subject
    ).lower()

    return subject in {
        "иностранный язык",
        "английский язык",
        "иностранный язык (английский)",
        "английский",
    }


def language_kind(subject):

    if is_english(
        subject
    ):
        return "english"

    if is_second_language(
        subject
    ):
        return "second_language"

    return None


# =========================================================
# ФИЗРА
# =========================================================

def is_physical_education(subject):

    subject = clean(
        subject
    ).lower()

    return (
        "физическая культура"
        in subject

        or "физической культуре"
        in subject

        or "физ. культура"
        in subject

        or "физ. культуре"
        in subject
    )


# =========================================================
# ПОДГОТОВКА СТРОК
# =========================================================

def prepare_rows(
    rows,
    period_start,
    period_end
):

    period_rows = []

    for row in rows:

        lesson_date = (
            datetime.fromisoformat(
                row["date"]
            ).date()
        )

        if (
            period_start
            <= lesson_date
            <= period_end
        ):

            period_rows.append(
                row
            )

    # Считаем количество языковых
    # строк на одно время
    language_counts = {}

    for row in period_rows:

        lang = language_kind(
            row["subject"]
        )

        if not lang:
            continue

        key = (
            row["date"],
            row["start"],
            row["end"],
            lang,
        )

        language_counts[key] = (
            language_counts.get(
                key,
                0
            )
            + 1
        )

    result = []

    for row in period_rows:

        lang = language_kind(
            row["subject"]
        )

        # =================================================
        # ИНОСТРАННЫЙ ЯЗЫК
        # =================================================

        if lang:

            key = (
                row["date"],
                row["start"],
                row["end"],
                lang,
            )

            count = (
                language_counts[
                    key
                ]
            )

            if lang == "english":

                display_subject = (
                    "Английский язык"
                )

            else:

                display_subject = (
                    "2 иностранный"
                )

            # ---------------------------------------------
            # Если строка одна:
            # выводим как обычную пару
            # с аудиторией и адресом
            # ---------------------------------------------

            if count == 1:

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
                        display_subject,

                    "lesson_type":
                        normalize_lesson_type(
                            row["type"]
                        ),

                    "room":
                        normalize_room(
                            row["room"]
                        ),

                    "address":
                        normalize_address(
                            row["address"]
                        ),

                    "teacher":
                        clean(
                            row["teacher"]
                        ),
                }

            # ---------------------------------------------
            # Если подгрупп много:
            # схлопываем в одну строку
            # ---------------------------------------------

            else:

                item = {
                    "date":
                        row["date"],

                    "start":
                        row["start"],

                    "end":
                        row["end"],

                    "kind":
                        (
                            f"{lang}_grouped"
                        ),

                    "subject":
                        display_subject,

                    "lesson_type":
                        "",

                    "room":
                        "",

                    "address":
                        "",

                    "teacher":
                        "",
                }

        # =================================================
        # ФИЗРА
        # =================================================

        elif is_physical_education(
            row["subject"]
        ):

            item = {
                "date":
                    row["date"],

                "start":
                    row["start"],

                "end":
                    row["end"],

                "kind":
                    "physical",

                "subject":
                    "Физра",

                "lesson_type":
                    "",

                "room":
                    "стадион",

                "address":
                    "",

                "teacher":
                    "",
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
                    row["subject"],

                "lesson_type":
                    normalize_lesson_type(
                        row["type"]
                    ),

                "room":
                    normalize_room(
                        row["room"]
                    ),

                "address":
                    normalize_address(
                        row["address"]
                    ),

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
            "english_grouped",
            "second_language_grouped",
            "physical",
        }:

            key = (
                row["date"],
                row["start"],
                row["end"],
                row["kind"],
                row["subject"],
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
                row["address"],
                row["teacher"],
            )

        unique[
            key
        ] = row

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

    if (
        a["date"]
        != b["date"]
    ):
        return False

    if (
        a["kind"]
        != b["kind"]
    ):
        return False

    if (
        a["subject"]
        != b["subject"]
    ):
        return False

    if (
        a["kind"]
        == "normal"
    ):

        fields = (
            "lesson_type",
            "room",
            "address",
            "teacher",
        )

        for field in fields:

            if (
                a.get(field, "")
                != b.get(field, "")
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
            x.get(
                "room",
                ""
            ),
        ),
    )

    merged = []

    for row in rows:

        current = (
            row.copy()
        )

        if (
            merged
            and can_merge(
                merged[-1],
                current
            )
        ):

            merged[-1][
                "end"
            ] = current[
                "end"
            ]

        else:

            merged.append(
                current
            )

    return merged


# =========================================================
# ВРЕМЯ
# =========================================================

def display_start(row):

    if (
        row["kind"]
        == "physical"
    ):

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

    # Физра
    if (
        row["kind"]
        == "physical"
    ):

        return (
            f'⏰ {time_text} — '
            f'<b>Физра</b> · стадион'
        )

    # Несколько языковых подгрупп
    if row["kind"] in {
        "english_grouped",
        "second_language_grouped",
    }:

        return (
            f'⏰ {time_text} — '
            f'<b>{subject}</b>'
        )

    details = []

    lesson_type = clean(
        row.get(
            "lesson_type",
            ""
        )
    )

    room = clean(
        row.get(
            "room",
            ""
        )
    )

    address = clean(
        row.get(
            "address",
            ""
        )
    )

    if lesson_type:
        details.append(
            lesson_type
        )

    if room:
        details.append(
            room
        )

    if address:
        details.append(
            address
        )

    if details:

        suffix = (
            " · "
            + " · ".join(
                escape(item)
                for item
                in details
            )
        )

    else:

        suffix = ""

    return (
        f'⏰ {time_text} — '
        f'<b>{subject}</b>'
        f'{suffix}'
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
        + timedelta(
            days=5
        )
    )

    sunday = (
        monday
        + timedelta(
            days=6
        )
    )

    by_date = {}

    for row in rows:

        by_date.setdefault(
            row["date"],
            []
        ).append(
            row
        )

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

    has_sunday = (
        sunday.isoformat()
        in by_date
    )

    if has_sunday:

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
            + timedelta(
                days=offset
            )
        )

        lessons = sorted(
            by_date.get(
                day.isoformat(),
                []
            ),
            key=lambda x: (
                x["start"],
                x["subject"],
            ),
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
# ПРОШЕДШИЕ ПАРЫ
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
        lesson_end
        < now
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
# ИЗМЕНЕНИЯ
# =========================================================

def pretty_date(
    iso_date
):

    return (
        datetime.fromisoformat(
            iso_date
        )
        .strftime(
            "%d.%m"
        )
    )


def lesson_brief(row):

    return (
        f'<b>{escape(row["subject"])}</b>'
        f' · '
        f'{pretty_date(row["date"])}'
        f' · '
        f'{display_time_range(row)}'
    )


def metadata_changes(
    old,
    new
):

    changes = []

    if (
        old["kind"]
        != "normal"
        or new["kind"]
        != "normal"
    ):

        return changes

    subject = escape(
        new["subject"]
    )

    date_text = (
        pretty_date(
            new["date"]
        )
    )

    time_text = (
        display_time_range(
            new
        )
    )

    # =====================================================
    # АУДИТОРИЯ
    # =====================================================

    old_room = old.get(
        "room",
        ""
    )

    new_room = new.get(
        "room",
        ""
    )

    if (
        old_room
        != new_room
    ):

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

        if (
            old_missing
            and not new_missing
        ):

            changes.append(
                f'📍 Добавлена аудитория: '
                f'<b>{subject}</b>'
                f' · '
                f'{escape(new_room)}'
                f' · '
                f'{date_text}'
                f' · '
                f'{time_text}'
            )

        elif (
            not old_missing
            and new_missing
        ):

            changes.append(
                f'📍 Убрана аудитория: '
                f'<b>{subject}</b>'
                f' · '
                f'{date_text}'
                f' · '
                f'{time_text}'
            )

        else:

            changes.append(
                f'📍 Изменена аудитория: '
                f'<b>{subject}</b>'
                f' · '
                f'{escape(old_room)}'
                f' → '
                f'{escape(new_room)}'
                f' · '
                f'{date_text}'
                f' · '
                f'{time_text}'
            )

    # =====================================================
    # АДРЕС
    # =====================================================

    # В старом state.json адреса не было.
    # Поэтому это не считаем ложным изменением.
    if "address" in old:

        old_address = old.get(
            "address",
            ""
        )

        new_address = new.get(
            "address",
            ""
        )

        if (
            old_address
            != new_address
        ):

            if (
                not old_address
                and new_address
            ):

                changes.append(
                    f'🏫 Добавлен адрес: '
                    f'<b>{subject}</b>'
                    f' · '
                    f'{escape(new_address)}'
                    f' · '
                    f'{date_text}'
                    f' · '
                    f'{time_text}'
                )

            elif (
                old_address
                and not new_address
            ):

                changes.append(
                    f'🏫 Убран адрес: '
                    f'<b>{subject}</b>'
                    f' · '
                    f'{date_text}'
                    f' · '
                    f'{time_text}'
                )

            else:

                changes.append(
                    f'🏫 Изменён адрес: '
                    f'<b>{subject}</b>'
                    f' · '
                    f'{escape(old_address)}'
                    f' → '
                    f'{escape(new_address)}'
                    f' · '
                    f'{date_text}'
                    f' · '
                    f'{time_text}'
                )

    # =====================================================
    # ТИП
    # =====================================================

    if (
        old.get(
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
            f' · '
            f'{date_text}'
            f' · '
            f'{time_text}'
        )

    # =====================================================
    # ПРЕПОДАВАТЕЛЬ
    # =====================================================

    if (
        old.get(
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
            f' · '
            f'{date_text}'
            f' · '
            f'{time_text}'
        )

    return changes


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
    # ТА ЖЕ ПАРА
    # =====================================================

    for old_index, old_row in enumerate(
        old
    ):

        for new_index, new_row in enumerate(
            new
        ):

            if (
                new_index
                in matched_new
            ):
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
    # ИЗМЕНЕНО ВРЕМЯ
    # =====================================================

    for old_index, old_row in enumerate(
        old
    ):

        if (
            old_index
            in matched_old
        ):
            continue

        candidates = []

        for new_index, new_row in enumerate(
            new
        ):

            if (
                new_index
                in matched_new
            ):
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

        if len(
            candidates
        ) == 1:

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

            if (
                old_time
                != new_time
            ):

                changes.append(
                    f'⏰ Изменено время: '
                    f'<b>{escape(new_row["subject"])}</b>'
                    f' · '
                    f'{pretty_date(new_row["date"])}'
                    f' · '
                    f'{old_time}'
                    f' → '
                    f'{new_time}'
                )

            changes.extend(
                metadata_changes(
                    old_row,
                    new_row
                )
            )

    # =====================================================
    # УБРАННЫЕ
    # =====================================================

    for old_index, old_row in enumerate(
        old
    ):

        if (
            old_index
            not in matched_old
        ):

            changes.append(
                f'❌ Убрана пара: '
                f'{lesson_brief(old_row)}'
            )

    # =====================================================
    # ДОБАВЛЕННЫЕ
    # =====================================================

    for new_index, new_row in enumerate(
        new
    ):

        if (
            new_index
            not in matched_new
        ):

            changes.append(
                f'🆕 Добавлена пара: '
                f'{lesson_brief(new_row)}'
            )

    return changes


# =========================================================
# TELEGRAM — ПОЛНОЕ СООБЩЕНИЕ
# =========================================================

def plain_length(html):

    return len(
        re.sub(
            r"<[^>]+>",
            "",
            html
        )
    )


def format_schedule(
    current_rows,
    next_rows,
    current_monday,
    next_monday,
    changes=None
):

    current_text = (
        format_week(
            merge_lessons(
                current_rows
            ),
            current_monday,
            "ТЕКУЩАЯ НЕДЕЛЯ",
            "📅",
        )
    )

    next_text = (
        format_week(
            merge_lessons(
                next_rows
            ),
            next_monday,
            "СЛЕДУЮЩАЯ НЕДЕЛЯ",
            "⏭️",
        )
    )

    schedule_body = (
        current_text
        + "\n\n━━━━━━━━━━━━━\n\n"
        + next_text
    )

    if not changes:
        return schedule_body

    shown = list(
        changes
    )

    while shown:

        hidden_count = (
            len(changes)
            - len(shown)
        )

        if hidden_count > 0:

            hidden_note = (
                f"\n… и ещё "
                f"{hidden_count} изменений"
            )

        else:

            hidden_note = ""

        message = (
            '🔔 <b>РАСПИСАНИЕ ОБНОВЛЕНО</b>'
            '\n\n'
            '<b>Что изменилось:</b>'
            '\n\n'
            + "\n".join(
                shown
            )
            + hidden_note
            + "\n\n━━━━━━━━━━━━━\n\n"
            + schedule_body
        )

        if (
            plain_length(
                message
            )
            <= 4096
        ):

            return message

        shown.pop()

    if (
        plain_length(
            schedule_body
        )
        <= 4096
    ):

        return schedule_body

    raise RuntimeError(
        "Расписание не помещается "
        "в одно сообщение Telegram."
    )


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_request(
    method,
    data
):

    response = requests.post(
        (
            f"https://api.telegram.org/"
            f"bot{BOT_TOKEN}/{method}"
        ),
        data=data,
        timeout=30,
    )

    response.raise_for_status()

    result = (
        response.json()
    )

    if not result.get(
        "ok"
    ):

        raise RuntimeError(
            result.get(
                "description",
                "Telegram error"
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

    # Сначала новое
    new_message_id = (
        send_schedule(
            text
        )
    )

    # Потом старое
    if old_message_id:

        try:

            delete_message(
                old_message_id
            )

        except Exception as exc:

            print(
                "Новое сообщение отправлено, "
                "но старое не удалено:",
                repr(exc)
            )

    return new_message_id


# =========================================================
# GOOGLE CALENDAR
# =========================================================

def get_calendar_service():

    if (
        not GOOGLE_SERVICE_ACCOUNT_JSON
        or not CALENDAR_ID
    ):

        raise RuntimeError(
            "Не заданы Google Calendar secrets."
        )

    credentials = (
        service_account
        .Credentials
        .from_service_account_info(
            json.loads(
                GOOGLE_SERVICE_ACCOUNT_JSON
            ),
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


# =========================================================
# КАЛЕНДАРЬ — МЕСТО
# =========================================================

def calendar_location(row):

    if (
        row["kind"]
        == "physical"
    ):

        return "стадион"

    if row["kind"] in {
        "english_grouped",
        "second_language_grouped",
    }:

        return ""

    room = clean(
        row.get(
            "room",
            ""
        )
    )

    address = clean(
        row.get(
            "address",
            ""
        )
    )

    parts = []

    if (
        room
        and room
        != "кабинет не указан"
    ):

        parts.append(
            room
        )

    if address:

        parts.append(
            address
        )

    return (
        " · ".join(
            parts
        )
    )


# =========================================================
# КАЛЕНДАРЬ — НАЗВАНИЕ
# =========================================================

def calendar_summary(row):

    location = (
        calendar_location(
            row
        )
    )

    if location:

        return (
            f'{row["subject"]}'
            f' — '
            f'{location}'
        )

    return row[
        "subject"
    ]


# =========================================================
# УНИКАЛЬНЫЙ ID ПАРЫ
# =========================================================

def calendar_lesson_uid(
    row,
    occurrence
):

    source = (
        f'{row["date"]}'
        f'|'
        f'{row["kind"]}'
        f'|'
        f'{row["subject"]}'
        f'|'
        f'{occurrence}'
    )

    return (
        hashlib.sha1(
            source.encode(
                "utf-8"
            )
        ).hexdigest()
    )


# =========================================================
# СОБЫТИЯ КАЛЕНДАРЯ
# =========================================================

def build_calendar_events(rows):

    merged = sorted(
        merge_lessons(
            rows
        ),
        key=lambda x: (
            x["date"],
            x["subject"],
            x["start"],
            x.get(
                "room",
                ""
            ),
        ),
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
            counters[
                base
            ]
        )

        uid = (
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

        desired[
            uid
        ] = {
            "summary":
                calendar_summary(
                    row
                ),

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

            # ТОЛЬКО одно уведомление:
            # за 60 минут
            "reminders": {
                "useDefault":
                    False,

                "overrides": [
                    {
                        "method":
                            "popup",

                        "minutes":
                            60,
                    }
                ],
            },

            "extendedProperties": {
                "private": {
                    "ranepa_schedule_bot":
                        "true",

                    "lesson_uid":
                        uid,
                }
            },
        }

    return desired


# =========================================================
# СУЩЕСТВУЮЩИЕ СОБЫТИЯ
# =========================================================

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
            + timedelta(
                days=1
            ),
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


# =========================================================
# ПРОВЕРКА НАПОМИНАНИЙ
# =========================================================

def reminder_signature(
    reminders
):

    reminders = (
        reminders
        or {}
    )

    overrides = (
        reminders.get(
            "overrides",
            []
        )
        or []
    )

    normalized = sorted(
        (
            item.get(
                "method",
                ""
            ),
            int(
                item.get(
                    "minutes",
                    0
                )
            ),
        )
        for item in overrides
    )

    return (
        bool(
            reminders.get(
                "useDefault",
                False
            )
        ),
        normalized,
    )


# =========================================================
# ИЗМЕНИЛОСЬ ЛИ СОБЫТИЕ
# =========================================================

def event_changed(
    existing,
    desired
):

    existing_private = (
        existing.get(
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

        or reminder_signature(
            existing.get(
                "reminders"
            )
        )
        != reminder_signature(
            desired.get(
                "reminders"
            )
        )
    )


# =========================================================
# СИНХРОНИЗАЦИЯ GOOGLE CALENDAR
# =========================================================

def sync_google_calendar(
    current_rows,
    next_rows,
    current_monday,
    next_sunday
):

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

        uid = (
            event.get(
                "extendedProperties",
                {}
            )
            .get(
                "private",
                {}
            )
            .get(
                "lesson_uid"
            )
        )

        if uid:

            existing_by_uid[
                uid
            ] = event

    created = 0
    updated = 0
    deleted = 0

    # =====================================================
    # СОЗДАНИЕ И ОБНОВЛЕНИЕ
    # =====================================================

    for uid, body in (
        desired.items()
    ):

        existing = (
            existing_by_uid.get(
                uid
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

        elif event_changed(
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
    # УДАЛЕНИЕ
    # =====================================================

    for uid, event in (
        existing_by_uid.items()
    ):

        if uid in desired:
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
        "schema_version":
            STATE_SCHEMA_VERSION,

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

    now = (
        datetime.now(
            MOSCOW
        )
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
        "Сейчас:",
        now.isoformat()
    )

    print(
        "Текущая неделя:",
        current_monday,
        "—",
        current_sunday
    )

    print(
        "Следующая неделя:",
        next_monday,
        "—",
        next_sunday
    )

    # =====================================================
    # ПОЛУЧАЕМ САЙТ
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
    # STATE
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
    # ЗАЩИТА ОТ ПУСТОГО САЙТА
    # =====================================================

    if (
        saved_period
        == current_period

        and not current_rows

        and state.get(
            "current_schedule"
        )
    ):

        raise RuntimeError(
            "Текущая неделя неожиданно "
            "стала пустой. Ничего не меняем."
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

        raise RuntimeError(
            "Следующая неделя неожиданно "
            "стала пустой. Ничего не меняем."
        )

    # =====================================================
    # GOOGLE CALENDAR
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
    # =====================================================

    if (
        saved_period
        != current_period
    ):

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
            "Новый двухнедельный "
            "период опубликован."
        )

        return

    # =====================================================
    # ПЕРЕХОД СО СТАРОГО STATE
    #
    # Один раз обновит сообщение чисто,
    # БЕЗ старой истории.
    # =====================================================

    if (
        state.get(
            "schema_version"
        )
        != STATE_SCHEMA_VERSION
    ):

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
            "Новый формат состояния "
            "сохранён без истории изменений."
        )

        return

    # =====================================================
    # СРАВНЕНИЕ
    # =====================================================

    current_changes = (
        find_changes(
            state.get(
                "current_schedule",
                []
            ),
            current_rows,
            now,
            ignore_past=True,
        )
    )

    next_changes = (
        find_changes(
            state.get(
                "next_schedule",
                []
            ),
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
    # НЕТ ИЗМЕНЕНИЙ
    # =====================================================

    if not changes:

        print(
            "Изменений на сайте нет. "
            "Telegram не обновляем."
        )

        return

    # =====================================================
    # ЕСТЬ ИЗМЕНЕНИЯ
    # =====================================================

    print(
        "Изменения на сайте:"
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

    # Сохраняется только НОВОЕ состояние.
    # Текст прошлых изменений не сохраняется.
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
