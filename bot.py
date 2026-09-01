import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# =========================================================
# НАСТРОЙКИ
# =========================================================

URL = "https://spb.ranepa.ru/raspisanie/mo-3-24-01-06/"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

TARGET_GROUP = 3

MOSCOW = ZoneInfo("Europe/Moscow")

STATE_FILE = "state.json"


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
    Проверяем, относится ли строка к МО-3-24-03.

    МО-3-24-03       -> да
    МО-3-24-01-03    -> да
    МО-3-24-01-06    -> да
    МО-3-24-01-02    -> нет
    МО-3-24-04-06    -> нет

    Суффиксы подгрупп после / не учитываем.
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
# ОПРЕДЕЛЕНИЕ ДВУХ НЕДЕЛЬ
# =========================================================

def get_weeks(now):
    """
    Возвращает:

    1. Текущую неделю
    2. Следующую неделю

    В воскресенье после 09:00 МСК
    переключаем сообщение вперёд.

    То есть после 09:00 воскресенья:
    текущей считаем неделю,
    которая начинается завтра.
    """

    current_monday = (
        now.date()
        - timedelta(days=now.weekday())
    )

    if (
        now.weekday() == 6
        and now.time() >= time(9, 0)
    ):
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

    # Ищем именно таблицу расписания
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

            lesson_date = (
                datetime.strptime(
                    date_raw,
                    "%d.%m.%Y"
                ).date()
            )

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
    """
    Например:

    Элективные курсы по физической культуре:
    Лёгкая атлетика; Фитнес; Спортивные игры

    превращаем в:

    Физра · стадион
    """

    subject = clean(
        subject
    ).lower()

    return (
        "физическая культура" in subject
        or "физ. культура" in subject
        or "элективные курсы по физической культуре" in subject
        or "элективные дисциплины по физической культуре" in subject
    )


# =========================================================
# ПОДГОТОВКА ЗАНЯТИЙ
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

        # Только нужный период
        if not (
            period_start
            <= lesson_date
            <= period_end
        ):
            continue

        subject = row["subject"]

        # -------------------------------------------------
        # ВТОРОЙ ИНОСТРАННЫЙ
        # -------------------------------------------------

        if is_second_language(subject):

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],

                "kind":
                    "second_language",

                "subject":
                    "2 иностранный",

                "lesson_type": "",
                "room": "",
                "teacher": "",
            }

        # -------------------------------------------------
        # АНГЛИЙСКИЙ
        # -------------------------------------------------

        elif is_english(subject):

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],

                "kind":
                    "english",

                "subject":
                    "Английский",

                "lesson_type": "",
                "room": "",
                "teacher": "",
            }

        # -------------------------------------------------
        # ФИЗРА
        # -------------------------------------------------

        elif is_physical_education(
            subject
        ):

            item = {
                "date": row["date"],

                # Реальное время сохраняем
                # для сравнения и объединения.
                "start": row["start"],
                "end": row["end"],

                "kind":
                    "physical",

                "subject":
                    "Физра",

                "lesson_type": "",

                "room":
                    "стадион",

                "teacher": "",
            }

        # -------------------------------------------------
        # ОБЫЧНАЯ ПАРА
        # -------------------------------------------------

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

                # Не показываем преподавателя,
                # но сохраняем его внутри.
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
# ОБЪЕДИНЕНИЕ ПОСЛЕДОВАТЕЛЬНЫХ ПАР
# =========================================================

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

    # Для обычной пары должны совпадать:
    # тип, кабинет и преподаватель.
    if a["kind"] == "normal":

        if (
            a["lesson_type"]
            != b["lesson_type"]
        ):
            return False

        if a["room"] != b["room"]:
            return False

        if a["teacher"] != b["teacher"]:
            return False

    previous_end = (
        to_minutes(
            a["end"]
        )
    )

    next_start = (
        to_minutes(
            b["start"]
        )
    )

    gap = (
        next_start
        - previous_end
    )

    # Университетский перерыв
    return (
        0
        <= gap
        <= 30
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
# ФОРМАТ ОДНОЙ ПАРЫ
# =========================================================

def format_lesson(row):

    # -----------------------------------------------------
    # ФИЗРА
    # -----------------------------------------------------

    if row["kind"] == "physical":

        # В Telegram физра начинается в 09:00.
        # Реальное начало остаётся внутри данных.
        time_text = (
            f'09:00–{row["end"]}'
        )

        return (
            f'⏰ {time_text} — '
            f'<b>Физра</b> · стадион'
        )

    # -----------------------------------------------------
    # ОСТАЛЬНЫЕ
    # -----------------------------------------------------

    time_text = (
        f'{row["start"]}'
        f'–'
        f'{row["end"]}'
    )

    if row["kind"] == "english":

        return (
            f'⏰ {time_text} — '
            f'<b>Английский</b>'
        )

    if (
        row["kind"]
        == "second_language"
    ):

        return (
            f'⏰ {time_text} — '
            f'<b>2 иностранный</b>'
        )

    return (
        f'⏰ {time_text} — '
        f'<b>{row["subject"]}</b>'
        f' · {row["lesson_type"]}'
        f' · {row["room"]}'
    )


# =========================================================
# ФОРМАТ ОДНОЙ НЕДЕЛИ
# =========================================================

def format_week(
    rows,
    monday,
    title
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

    # Если расписания на эту неделю
    # вообще ещё нет
    if not rows:

        return (
            f'<b>{title} · '
            f'{monday.strftime("%d.%m")}'
            f' — '
            f'{saturday.strftime("%d.%m")}'
            f'</b>\n\n'
            f'Расписание пока не опубликовано'
        )

    # Обычно ПН–СБ.
    # Воскресенье добавляем только
    # при наличии занятия.
    if sunday.isoformat() in by_date:

        display_end = sunday
        number_of_days = 7

    else:

        display_end = saturday
        number_of_days = 6

    blocks = [
        (
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
                f'<b>— '
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
            "\n".join(block)
        )

    # Одна пустая строка между днями
    return "\n\n".join(
        blocks
    )


# =========================================================
# ФОРМАТ ДВУХ НЕДЕЛЬ
# =========================================================

def format_schedule(
    current_rows,
    next_rows,
    current_monday,
    next_monday,
    updated=False
):

    if updated:

        heading = (
            "⚠️ <b>РАСПИСАНИЕ ОБНОВЛЕНО</b>"
        )

    else:

        heading = (
            "📚 <b>РАСПИСАНИЕ</b>"
        )

    current_text = format_week(
        current_rows,
        current_monday,
        "ТЕКУЩАЯ НЕДЕЛЯ"
    )

    next_text = format_week(
        next_rows,
        next_monday,
        "СЛЕДУЮЩАЯ НЕДЕЛЯ"
    )

    # Чуть больше воздуха именно
    # между двумя неделями
    return (
        heading
        + "\n\n"
        + current_text
        + "\n\n━━━━━━━━━━━━━\n\n"
        + next_text
    )


# =========================================================
# TELEGRAM
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

    result = (
        response.json()
    )

    if not result.get("ok"):

        raise RuntimeError(
            "Telegram error: "
            + str(
                result.get(
                    "description",
                    "unknown error"
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
        result["result"]["message_id"]
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


# =========================================================
# СРАВНЕНИЕ РАСПИСАНИЯ
# =========================================================

def schedule_signature(
    rows,
    now,
    ignore_past
):
    """
    Для текущей недели:
    прошедшие занятия игнорируем.

    Для следующей недели:
    сравниваем всё.
    """

    result = []

    for row in rows:

        if ignore_past:

            lesson_date = (
                datetime.fromisoformat(
                    row["date"]
                ).date()
            )

            try:

                end_time = (
                    datetime.strptime(
                        row["end"],
                        "%H:%M"
                    ).time()
                )

            except ValueError:

                continue

            lesson_end = (
                datetime.combine(
                    lesson_date,
                    end_time,
                    tzinfo=MOSCOW,
                )
            )

            if lesson_end < now:
                continue

        result.append({
            "date":
                row["date"],

            "start":
                row["start"],

            "end":
                row["end"],

            "kind":
                row["kind"],

            "subject":
                row["subject"],

            "lesson_type":
                row.get(
                    "lesson_type",
                    ""
                ),

            "room":
                row.get(
                    "room",
                    ""
                ),

            "teacher":
                row.get(
                    "teacher",
                    ""
                ),
        })

    return sorted(
        result,
        key=lambda x: (
            x["date"],
            x["start"],
            x["subject"],
            x["kind"],
        )
    )


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
    # ПОЛУЧАЕМ ДАННЫЕ
    # =====================================================

    all_rows = (
        fetch_schedule()
    )

    # Берём сразу двухнедельный период
    prepared = prepare_rows(
        all_rows,
        current_monday,
        next_sunday,
    )

    # =====================================================
    # РАЗДЕЛЯЕМ ДВЕ НЕДЕЛИ
    # =====================================================

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
    # ЗАЩИТА ТЕКУЩЕЙ НЕДЕЛИ
    # =====================================================

    # Если текущая неделя неожиданно полностью пустая,
    # но она уже была опубликована ранее,
    # не будем автоматически считать,
    # что университет отменил вообще всё.
    #
    # Это защищает от ошибок сайта/парсера.

    state = load_state()

    saved_period = state.get(
        "current_week_start"
    )

    if (
        not current_rows
        and saved_period
        == current_monday.isoformat()
        and state.get("current_schedule")
    ):

        raise RuntimeError(
            "Текущая неделя неожиданно стала пустой. "
            "Старое сообщение оставляем без изменений."
        )

    # Следующая неделя МОЖЕТ быть пустой.
    # Это нормально: расписание могли ещё
    # просто не опубликовать.

    current_rows = merge_lessons(
        current_rows
    )

    next_rows = merge_lessons(
        next_rows
    )

    # =====================================================
    # НОВЫЙ ДВУХНЕДЕЛЬНЫЙ ПЕРИОД
    # =====================================================

    if (
        saved_period
        != current_monday.isoformat()
    ):

        print(
            "Начался новый период."
        )

        text = format_schedule(
            current_rows,
            next_rows,
            current_monday,
            next_monday,
            updated=False,
        )

        # Сначала отправляем новое
        new_message_id = (
            send_schedule(
                text
            )
        )

        old_message_id = (
            state.get(
                "message_id"
            )
        )

        # Только после успешной отправки
        # удаляем старое
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
                    "Не удалось удалить "
                    "старое сообщение:",
                    exc
                )

        state = {
            "current_week_start":
                current_monday.isoformat(),

            "current_week_end":
                current_sunday.isoformat(),

            "next_week_start":
                next_monday.isoformat(),

            "next_week_end":
                next_sunday.isoformat(),

            "message_id":
                new_message_id,

            "current_schedule":
                current_rows,

            "next_schedule":
                next_rows,
        }

        save_state(
            state
        )

        print(
            "Новое расписание опубликовано."
        )

        return

    # =====================================================
    # ЕЖЕЧАСНОЕ СРАВНЕНИЕ
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

    # У текущей недели игнорируем прошлые пары
    old_current_signature = (
        schedule_signature(
            old_current,
            now,
            ignore_past=True,
        )
    )

    new_current_signature = (
        schedule_signature(
            current_rows,
            now,
            ignore_past=True,
        )
    )

    # У следующей недели сравниваем всё
    old_next_signature = (
        schedule_signature(
            old_next,
            now,
            ignore_past=False,
        )
    )

    new_next_signature = (
        schedule_signature(
            next_rows,
            now,
            ignore_past=False,
        )
    )

    current_changed = (
        old_current_signature
        != new_current_signature
    )

    next_changed = (
        old_next_signature
        != new_next_signature
    )

    # =====================================================
    # НИЧЕГО НЕ ИЗМЕНИЛОСЬ
    # =====================================================

    if (
        not current_changed
        and not next_changed
    ):

        print(
            "Изменений нет."
        )

        return

    # =====================================================
    # ЕСТЬ ИЗМЕНЕНИЯ
    # =====================================================

    print(
        "Обнаружены изменения."
    )

    if current_changed:

        print(
            "Изменилась текущая неделя."
        )

    if next_changed:

        print(
            "Изменилась следующая неделя."
        )

    text = format_schedule(
        current_rows,
        next_rows,
        current_monday,
        next_monday,
        updated=True,
    )

    # Сначала отправляем новое
    new_message_id = (
        send_schedule(
            text
        )
    )

    old_message_id = (
        state.get(
            "message_id"
        )
    )

    # Только после успешной отправки
    # удаляем старое
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
                "Не удалось удалить "
                "старое сообщение:",
                exc
            )

    # =====================================================
    # СОХРАНЯЕМ НОВОЕ СОСТОЯНИЕ
    # =====================================================

    state = {
        "current_week_start":
            current_monday.isoformat(),

        "current_week_end":
            current_sunday.isoformat(),

        "next_week_start":
            next_monday.isoformat(),

        "next_week_end":
            next_sunday.isoformat(),

        "message_id":
            new_message_id,

        "current_schedule":
            current_rows,

        "next_schedule":
            next_rows,
    }

    save_state(
        state
    )

    print(
        "Обновлённое расписание опубликовано."
    )


if __name__ == "__main__":
    main()
