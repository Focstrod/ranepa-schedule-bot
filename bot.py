import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://spb.ranepa.ru/raspisanie/mo-3-24-01-06/"
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

TARGET_GROUP = 3
MOSCOW = ZoneInfo("Europe/Moscow")
STATE_FILE = "state.json"

SLOTS = [
    ("08:30", "09:50"),
    ("10:00", "11:20"),
    ("12:00", "13:20"),
    ("13:30", "14:50"),
    ("15:00", "16:20"),
    ("16:30", "17:50"),
    ("18:30", "19:50"),
    ("20:00", "21:20"),
]

DAY_NAMES = {
    0: "ПОНЕДЕЛЬНИК",
    1: "ВТОРНИК",
    2: "СРЕДА",
    3: "ЧЕТВЕРГ",
    4: "ПЯТНИЦА",
    5: "СУББОТА",
    6: "ВОСКРЕСЕНЬЕ",
}


def normalize_time(value):
    return value.strip().replace(".", ":")


def clean(value):
    return " ".join(value.split()).strip()


def group_matches(group_text):
    """
    Возвращает True, если строка группы относится к МО-3-24-03.

    Примеры:
    МО-3-24-03       -> True
    МО-3-24-01-03    -> True
    МО-3-24-01-06    -> True
    МО-3-24-04-06    -> False
    """
    base = group_text.split("/")[0].strip()

    match = re.search(r"МО-3-24-(\d{2})(?:-(\d{2}))?$", base)
    if not match:
        return False

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start

    return start <= TARGET_GROUP <= end


def get_target_week(now):
    """
    До воскресенья 09:00 МСК работаем с текущей неделей.
    С воскресенья 09:00 — уже со следующей.
    """
    monday = now.date() - timedelta(days=now.weekday())

    if now.weekday() == 6 and now.time() >= time(9, 0):
        monday += timedelta(days=7)

    sunday = monday + timedelta(days=6)

    return monday, sunday


def fetch_schedule():
    response = requests.get(
        URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table")

    if not table:
        raise RuntimeError("Таблица расписания не найдена")

    rows = []

    for tr in table.find_all("tr")[1:]:
        cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]

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
            lesson_date = datetime.strptime(date_raw, "%d.%m.%Y").date()
        except ValueError:
            continue

        try:
            start_raw, end_raw = time_raw.split("-")
        except ValueError:
            continue

        start_time = normalize_time(start_raw)
        end_time = normalize_time(end_raw)

        rows.append({
            "date": lesson_date.isoformat(),
            "start": start_time,
            "end": end_time,
            "type": lesson_type,
            "group": group,
            "subject": subject,
            "teacher": teacher,
            "room": room,
        })

    return rows


def normalize_room(room):
    room = clean(room)

    if not room:
        return "кабинет не указан"

    if "СДО" in room.upper():
        return "СДО"

    return room


def teacher_surname(teacher):
    teacher = clean(teacher)

    if not teacher:
        return ""

    return teacher.split()[0]


def lesson_kind(subject):
    s = subject.lower().strip()

    if s == "второй иностранный язык":
        return "second_language"

    # На текущей странице английский называется "Иностранный язык"
    if s in {"иностранный язык", "английский язык"}:
        return "english"

    return "normal"


def prepare_rows(rows, week_start, week_end):
    """
    Оставляем только нужную неделю и превращаем
    иностранные языки в наши отображаемые сущности.
    """
    result = []

    for row in rows:
        lesson_date = datetime.fromisoformat(row["date"]).date()

        if not (week_start <= lesson_date <= week_end):
            continue

        kind = lesson_kind(row["subject"])

        if kind == "second_language":
            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],
                "kind": "second_language",
                "subject": "2 иностранный",
                "room": "",
                "teacher": "",
            }

        elif kind == "english":
            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],
                "kind": "english",
                "subject": "Английский",
                "room": "",
                "teacher": "",
            }

        else:
            item = {
                "date": row["date"],
                "start": row["start"],
                "end": row["end"],
                "kind": "normal",
                "subject": row["subject"],
                "room": normalize_room(row["room"]),
                "teacher": teacher_surname(row["teacher"]),
            }

        result.append(item)

    # Убираем дубли языковых подгрупп
    unique = {}
    for row in result:
        key = (
            row["date"],
            row["start"],
            row["end"],
            row["kind"],
            row["subject"],
            row["room"],
            row["teacher"],
        )
        unique[key] = row

    return list(unique.values())


def slot_index(start, end):
    try:
        return SLOTS.index((start, end))
    except ValueError:
        return None


def can_merge(a, b):
    if a["date"] != b["date"]:
        return False

    if a["subject"] != b["subject"]:
        return False

    if a["kind"] != b["kind"]:
        return False

    if a["kind"] == "normal":
        if a["room"] != b["room"]:
            return False
        if a["teacher"] != b["teacher"]:
            return False

    idx_a = slot_index(a["original_start"], a["end"])
    idx_b = slot_index(b["start"], b["end"])

    if idx_a is not None and idx_b is not None:
        return idx_b == idx_a + 1

    # запасной вариант, если вуз добавит нестандартные часы
    end_a = datetime.strptime(a["end"], "%H:%M")
    start_b = datetime.strptime(b["start"], "%H:%M")

    delta = start_b - end_a

    return timedelta(0) <= delta <= timedelta(minutes=45)


def merge_lessons(rows):
    rows = sorted(rows, key=lambda x: (x["date"], x["start"], x["subject"]))

    merged = []

    for row in rows:
        current = row.copy()
        current["original_start"] = current["start"]

        if not merged:
            merged.append(current)
            continue

        previous = merged[-1]

        if can_merge(previous, current):
            previous["end"] = current["end"]
        else:
            merged.append(current)

    for row in merged:
        row.pop("original_start", None)

    return merged


def format_lesson(row):
    time_text = f'{row["start"]}–{row["end"]}'

    if row["kind"] in {"english", "second_language"}:
        return f'⏰ {time_text} — <b>{row["subject"]}</b>'

    parts = [
        f'⏰ {time_text} — <b>{row["subject"]}</b>',
        row["room"],
    ]

    if row["teacher"]:
        parts.append(row["teacher"])

    return " · ".join(parts)


def format_schedule(rows, week_start, updated=False):
    by_date = {}

    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    last_day = week_start + timedelta(days=5)

    # Если реально есть занятия в воскресенье — добавляем его
    sunday = week_start + timedelta(days=6)
    if sunday.isoformat() in by_date:
        last_day = sunday

    if updated:
        title = "⚠️ <b>РАСПИСАНИЕ ОБНОВЛЕНО"
    else:
        title = "📚 <b>РАСПИСАНИЕ"

    title += (
        f' | {week_start.strftime("%d.%m")} — '
        f'{last_day.strftime("%d.%m")}</b>'
    )

    blocks = [title]

    total_days = 7 if last_day == sunday else 6

    for offset in range(total_days):
        day = week_start + timedelta(days=offset)
        key = day.isoformat()

        block = [
            f'<b>{DAY_NAMES[day.weekday()]} · {day.strftime("%d.%m")}</b>',
            "",
        ]

        lessons = by_date.get(key, [])

        if lessons:
            for lesson in sorted(lessons, key=lambda x: x["start"]):
                block.append(format_lesson(lesson))
        else:
            block.append("— выходной —")

        blocks.append("\n".join(block))

    return "\n\n\n".join(blocks)


def telegram_request(method, data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    response = requests.post(url, data=data, timeout=30)
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram error: {result.get('description', 'unknown error')}"
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


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )


def future_signature(rows, now):
    """
    Изменения уже прошедших занятий не должны
    инициировать замену поста.
    """
    result = []

    for row in rows:
        lesson_date = datetime.fromisoformat(row["date"]).date()
        end_time = datetime.strptime(row["end"], "%H:%M").time()

        lesson_end = datetime.combine(
            lesson_date,
            end_time,
            tzinfo=MOSCOW,
        )

        if lesson_end >= now:
            result.append(row)

    return result


def main():
    now = datetime.now(MOSCOW)
    week_start, week_end = get_target_week(now)

    print(f"Сейчас: {now}")
    print(f"Целевая неделя: {week_start} — {week_end}")

    all_rows = fetch_schedule()

    prepared = prepare_rows(
        all_rows,
        week_start,
        week_end,
    )

    if not prepared:
        raise RuntimeError(
            "Для целевой недели не найдено ни одного занятия. "
            "Старое сообщение оставляем без изменений."
        )

    merged = merge_lessons(prepared)

    state = load_state()

    current_week = week_start.isoformat()
    saved_week = state.get("week_start")

    # Новая неделя / первый запуск
    if saved_week != current_week:
        text = format_schedule(
            merged,
            week_start,
            updated=False,
        )

        new_message_id = send_schedule(text)

        old_message_id = state.get("message_id")

        if old_message_id:
            try:
                delete_message(old_message_id)
            except Exception as exc:
                print(f"Не удалось удалить старое сообщение: {exc}")

        state = {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "message_id": new_message_id,
            "schedule": merged,
        }

        save_state(state)

        print("Опубликовано расписание новой недели")
        return

    old_schedule = state.get("schedule", [])

    old_future = future_signature(old_schedule, now)
    new_future = future_signature(merged, now)

    if old_future == new_future:
        print("Изменений нет")
        return

    print("Обнаружены изменения")

    text = format_schedule(
        merged,
        week_start,
        updated=True,
    )

    # Сначала отправляем новое
    new_message_id = send_schedule(text)

    # Только после успешной отправки удаляем старое
    old_message_id = state.get("message_id")

    if old_message_id:
        try:
            delete_message(old_message_id)
        except Exception as exc:
            print(f"Не удалось удалить старое сообщение: {exc}")

    state["message_id"] = new_message_id
    state["schedule"] = merged
    state["week_start"] = week_start.isoformat()
    state["week_end"] = week_end.isoformat()

    save_state(state)

    print("Расписание обновлено")


if __name__ == "__main__":
    main()
