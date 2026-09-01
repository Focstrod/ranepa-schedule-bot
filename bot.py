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

    Например:
    МО-3-24-03       -> да
    МО-3-24-01-03    -> да
    МО-3-24-01-06    -> да
    МО-3-24-01-02    -> нет
    МО-3-24-04-06    -> нет

    Суффиксы типа /1англ тоже допускаются.
    """

    text = clean(group_text)

    # Отбрасываем обозначение подгруппы после /
    base = text.split("/")[0]

    match = re.search(
        r"МО-3-24-(\d{2})(?:-(\d{2}))?$",
        base
    )

    if not match:
        return False

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start

    return start <= TARGET_GROUP <= end


# =========================================================
# ОПРЕДЕЛЕНИЕ НЕДЕЛИ
# =========================================================

def get_target_week(now):
    """
    В обычные дни показываем текущую неделю.

    В воскресенье с 09:00 МСК переключаемся
    на следующую неделю.
    """

    monday = now.date() - timedelta(days=now.weekday())

    if now.weekday() == 6 and now.time() >= time(9, 0):
        monday += timedelta(days=7)

    sunday = monday + timedelta(days=6)

    return monday, sunday


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

    tables = soup.find_all("table")

    schedule_table = None

    # Ищем именно таблицу расписания,
    # а не просто первую таблицу страницы
    for table in tables:
        text = table.get_text(" ", strip=True)

        if (
            "Наименование дисциплины" in text
            and "Группа" in text
            and "Время" in text
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
            clean(td.get_text(" ", strip=True))
            for td in tr.find_all(["td", "th"])
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

        start_raw, end_raw = time_raw.split("-", 1)

        rows.append({
            "date": lesson_date.isoformat(),
            "start": normalize_time(start_raw),
            "end": normalize_time(end_raw),
            "type": clean(lesson_type),
            "group": clean(group),
            "subject": clean(subject),
            "teacher": clean(teacher),
            "room": clean(room),
        })

    if not rows:
        raise RuntimeError(
            "Не удалось получить ни одной строки "
            "расписания для группы."
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

    # Если появится необычный тип,
    # лучше показать его, чем потерять
    return value


def is_second_language(subject):
    subject = clean(subject).lower()

    return (
        subject == "второй иностранный язык"
        or subject.startswith("второй иностранный язык ")
    )


def is_english(subject):
    """
    Английский считаем специальной дисциплиной.

    Не используем просто проверку:
    "англий" in subject,
    потому что может встретиться другая дисциплина
    "на английском языке".
    """

    subject = clean(subject).lower()

    variants = {
        "иностранный язык",
        "английский язык",
        "иностранный язык (английский)",
        "английский",
    }

    return subject in variants


def is_physical_education(subject):
    subject = clean(subject).lower()

    markers = [
        "физическая культура",
        "физическая культура и спорт",
        "элективные дисциплины по физической культуре",
        "элективная дисциплина по физической культуре",
    ]

    return any(
        marker in subject
        for marker in markers
    )


# =========================================================
# ПОДГОТОВКА ЗАНЯТИЙ
# =========================================================

def prepare_rows(rows, week_start, week_end):
    result = []

    for row in rows:

        lesson_date = datetime.fromisoformat(
            row["date"]
        ).date()

        # Жёстко ограничиваем нужной неделей
        if not (
            week_start
            <= lesson_date
            <= week_end
        ):
            continue

        subject = row["subject"]

        # -----------------------------
        # 2 ИНОСТРАННЫЙ
        # -----------------------------

        if is_second_language(subject):

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],

                "kind": "second_language",

                "subject": "2 иностранный",

                "lesson_type": "",
                "room": "",

                # Не показываем преподавателя,
                # но сохраняем исходные данные
                # отдельно для внутренней логики
                "teacher": "",
            }

        # -----------------------------
        # АНГЛИЙСКИЙ
        # -----------------------------

        elif is_english(subject):

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],

                "kind": "english",

                "subject": "Английский",

                "lesson_type": "",
                "room": "",
                "teacher": "",
            }

        # -----------------------------
        # ФИЗРА
        # -----------------------------

        elif is_physical_education(subject):

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],

                "kind": "physical",

                "subject": "Физра",

                "lesson_type": "",
                "room": "стадион",

                "teacher": "",
            }

        # -----------------------------
        # ОБЫЧНАЯ ПАРА
        # -----------------------------

        else:

            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],

                "kind": "normal",

                "subject": subject,

                "lesson_type":
                    normalize_lesson_type(
                        row["type"]
                    ),

                "room":
                    normalize_room(
                        row["room"]
                    ),

                # Преподаватель НЕ выводится,
                # но сохраняется.
                #
                # Если преподавателя поменяют,
                # бот заметит изменение.
                "teacher":
                    clean(row["teacher"]),
            }

        result.append(item)

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

            # Для специальных дисциплин
            # подгруппы / преподаватели не важны.
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

    return list(unique.values())


# =========================================================
# ОБЪЕДИНЕНИЕ ПОСЛЕДОВАТЕЛЬНЫХ ПАР
# =========================================================

def to_minutes(value):
    hours, minutes = map(
        int,
        value.split(":")
    )

    return hours * 60 + minutes


def can_merge(a, b):
    # Разные дни
    if a["date"] != b["date"]:
        return False

    # Разные категории
    if a["kind"] != b["kind"]:
        return False

    # Разные предметы
    if a["subject"] != b["subject"]:
        return False

    # Для обычных занятий должны совпадать
    # тип, кабинет и преподаватель
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

    # Между парами допускаем университетский перерыв.
    # Например:
    # 12:00–13:20
    # 13:30–14:50
    previous_end = to_minutes(a["end"])
    next_start = to_minutes(b["start"])

    gap = next_start - previous_end

    return 0 <= gap <= 30


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
            merged.append(current)
            continue

        previous = merged[-1]

        if can_merge(previous, current):
            previous["end"] = current["end"]
        else:
            merged.append(current)

    return merged


# =========================================================
# ФОРМАТ TELEGRAM
# =========================================================

def format_lesson(row):
    time_text = (
        f'{row["start"]}–{row["end"]}'
    )

    # Английский
    if row["kind"] == "english":

        return (
            f'⏰ {time_text} — '
            f'<b>Английский</b>'
        )

    # Второй иностранный
    if row["kind"] == "second_language":

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

    # Обычная дисциплина
    return (
        f'⏰ {time_text} — '
        f'<b>{row["subject"]}</b>'
        f' · {row["lesson_type"]}'
        f' · {row["room"]}'
    )


def format_schedule(
    rows,
    week_start,
    updated=False
):
    by_date = {}

    for row in rows:

        by_date.setdefault(
            row["date"],
            []
        ).append(row)

    saturday = (
        week_start
        + timedelta(days=5)
    )

    sunday = (
        week_start
        + timedelta(days=6)
    )

    # В обычной ситуации показываем ПН–СБ.
    # Если реально есть занятие в воскресенье,
    # автоматически добавляем воскресенье.
    if sunday.isoformat() in by_date:
        display_end = sunday
        number_of_days = 7
    else:
        display_end = saturday
        number_of_days = 6

    if updated:

        title = (
            "⚠️ <b>РАСПИСАНИЕ ОБНОВЛЕНО"
        )

    else:

        title = (
            "📚 <b>РАСПИСАНИЕ"
        )

    title += (
        f' | {week_start.strftime("%d.%m")}'
        f' — '
        f'{display_end.strftime("%d.%m")}</b>'
    )

    blocks = [title]

    for offset in range(number_of_days):

        day = (
            week_start
            + timedelta(days=offset)
        )

        key = day.isoformat()

        lessons = by_date.get(
            key,
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
                    format_lesson(lesson)
                )

        else:

            block.append(
                "Нет занятий"
            )

        blocks.append(
            "\n".join(block)
        )

    # ОДНА пустая строка между днями
    return "\n\n".join(blocks)


# =========================================================
# TELEGRAM
# =========================================================

def telegram_request(method, data):

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
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )

    return result["result"]["message_id"]


def delete_message(message_id):

    telegram_request(
        "deleteMessage",
        {
            "chat_id": CHAT_ID,
            "message_id": message_id,
        },
    )


# =========================================================
# STATE.JSON
# =========================================================

def load_state():

    if not os.path.exists(STATE_FILE):
        return {}

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

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
# СРАВНЕНИЕ ИЗМЕНЕНИЙ
# =========================================================

def future_signature(rows, now):
    """
    Прошедшие пары игнорируем при определении изменений.

    Например:
    если в пятницу университет исправил
    преподавателя у пары, которая была в понедельник,
    сообщение НЕ заменяем.
    """

    future = []

    for row in rows:

        lesson_date = datetime.fromisoformat(
            row["date"]
        ).date()

        try:

            end_time = datetime.strptime(
                row["end"],
                "%H:%M"
            ).time()

        except ValueError:
            continue

        lesson_end = datetime.combine(
            lesson_date,
            end_time,
            tzinfo=MOSCOW,
        )

        if lesson_end >= now:

            future.append({
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],
                "kind": row["kind"],
                "subject": row["subject"],
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
        future,
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

    now = datetime.now(MOSCOW)

    print(
        "Сейчас:",
        now.isoformat()
    )

    week_start, week_end = (
        get_target_week(now)
    )

    print(
        "Целевая неделя:",
        week_start,
        "—",
        week_end,
    )

    # Сначала безопасно получаем расписание.
    # Если сайт упал — выполнение закончится
    # ДО удаления старого сообщения.
    all_rows = fetch_schedule()

    prepared = prepare_rows(
        all_rows,
        week_start,
        week_end,
    )

    # Очень важная страховка:
    # пустой результат не считаем отменой всех занятий.
    if not prepared:

        raise RuntimeError(
            "Для целевой недели не найдено "
            "ни одного занятия. "
            "Старое сообщение оставляем."
        )

    merged = merge_lessons(
        prepared
    )

    state = load_state()

    current_week = (
        week_start.isoformat()
    )

    saved_week = state.get(
        "week_start"
    )

    # =====================================================
    # НОВАЯ НЕДЕЛЯ
    # =====================================================

    if saved_week != current_week:

        print(
            "Публикуем расписание новой недели."
        )

        text = format_schedule(
            merged,
            week_start,
            updated=False,
        )

        # Сначала отправляем НОВОЕ сообщение.
        new_message_id = send_schedule(
            text
        )

        print(
            "Новое сообщение отправлено:",
            new_message_id
        )

        old_message_id = state.get(
            "message_id"
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

                # Новое сообщение уже существует,
                # поэтому это не критическая ошибка.
                print(
                    "Не удалось удалить "
                    "старое сообщение:",
                    exc
                )

        state = {
            "week_start":
                week_start.isoformat(),

            "week_end":
                week_end.isoformat(),

            "message_id":
                new_message_id,

            "schedule":
                merged,
        }

        save_state(state)

        print(
            "Состояние новой недели сохранено."
        )

        return

    # =====================================================
    # ОБЫЧНАЯ ЕЖЕЧАСНАЯ ПРОВЕРКА
    # =====================================================

    old_schedule = state.get(
        "schedule",
        []
    )

    old_future = future_signature(
        old_schedule,
        now,
    )

    new_future = future_signature(
        merged,
        now,
    )

    if old_future == new_future:

        print(
            "Изменений нет."
        )

        return

    # =====================================================
    # РАСПИСАНИЕ ИЗМЕНИЛОСЬ
    # =====================================================

    print(
        "Обнаружены изменения расписания."
    )

    text = format_schedule(
        merged,
        week_start,
        updated=True,
    )

    # СНАЧАЛА отправляем новое.
    new_message_id = send_schedule(
        text
    )

    print(
        "Обновлённое сообщение отправлено:",
        new_message_id
    )

    # Только после успешной отправки
    # удаляем старое.
    old_message_id = state.get(
        "message_id"
    )

    if old_message_id:

        try:

            delete_message(
                old_message_id
            )

            print(
                "Предыдущее сообщение удалено."
            )

        except Exception as exc:

            print(
                "Не удалось удалить "
                "старое сообщение:",
                exc
            )

    state = {
        "week_start":
            week_start.isoformat(),

        "week_end":
            week_end.isoformat(),

        "message_id":
            new_message_id,

        "schedule":
            merged,
    }

    save_state(state)

    print(
        "Новое состояние сохранено."
    )


if __name__ == "__main__":
    main()
