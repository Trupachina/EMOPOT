import sqlite3
from pathlib import Path
import csv
import re

# Путь к базе данных (у тебя она лежит здесь после scp)
DB_FILE = Path(r"C:\Users\Кирилл\Desktop\SONP_data\sonp.sqlite3")

# Файлы, в которые будем сохранять результаты (основные)
OUT_PLAYERS = DB_FILE.with_name("valid_players.csv")
OUT_ANSWERS = DB_FILE.with_name("valid_answers.csv")

# Дополнительные анонимизированные файлы
OUT_PLAYERS_ANON = DB_FILE.with_name("valid_players_anon.csv")
OUT_ANSWERS_ANON = DB_FILE.with_name("valid_answers_anon.csv")


def get_connection():
    """Возвращает подключение к SQLite-базе."""
    return sqlite3.connect(DB_FILE)


def create_views():
    """
    Создаёт/пересоздаёт представления:

    valid_players_base:
      - только те записи, где name начинается с @
      - исключён @username
      - по каждому Telegram берётся последняя запись (максимальный id)

    v_sessions_filtered:
      - комнаты, в которых валидных игроков >= 2
      - и rounds <= 12 (не более 12 заданий)

    valid_players:
      - valid_players_base, но только в комнатах из v_sessions_filtered

    valid_answers:
      - ответы только игроков из valid_players
      - привязка по (room_code, player_id)
    """
    if not DB_FILE.exists():
        raise SystemExit(f"Файл базы данных не найден: {DB_FILE}")

    with get_connection() as con:
        cur = con.cursor()

        # На всякий случай дропаем старые view (в правильном порядке)
        cur.execute("DROP VIEW IF EXISTS valid_answers")
        cur.execute("DROP VIEW IF EXISTS valid_players")
        cur.execute("DROP VIEW IF EXISTS v_sessions_filtered")
        cur.execute("DROP VIEW IF EXISTS valid_players_base")

        # 1) Базовое представление с валидными игроками
        #    (последняя запись по каждому Telegram-нику)
        cur.execute("""
        CREATE VIEW valid_players_base AS
        WITH latest_per_telegram AS (
          SELECT
            MAX(id) AS id
          FROM players
          WHERE
            name <> '@username'
            AND name LIKE '@%%'
            AND LENGTH(name) > 1
          GROUP BY
            name
        )
        SELECT
          p.id,
          p.room_code,
          p.player_id,
          p.name,
          p.score
        FROM players AS p
        JOIN latest_per_telegram AS l
          ON p.id = l.id
        """)

        # 2) Представление с отфильтрованными сессиями:
        #    - валидных игроков в комнате >= 2
        #    - rounds <= 12
        cur.execute("""
        CREATE VIEW v_sessions_filtered AS
        WITH room_player_counts AS (
          SELECT
            room_code,
            COUNT(*) AS players_count
          FROM valid_players_base
          GROUP BY
            room_code
        )
        SELECT
          r.code AS room_code,
          r.rounds,
          r.status,
          rpc.players_count
        FROM rooms AS r
        JOIN room_player_counts AS rpc
          ON r.code = rpc.room_code
        WHERE
          rpc.players_count >= 2
          AND r.rounds <= 12
        """)

        # 3) Финальное представление игроков:
        #    только те валидные игроки, которые играли в "хороших" сессиях
        cur.execute("""
        CREATE VIEW valid_players AS
        SELECT
          p.id,
          p.room_code,
          p.player_id,
          p.name,
          p.score
        FROM valid_players_base AS p
        JOIN v_sessions_filtered AS s
          ON p.room_code = s.room_code
        """)

        # 4) Финальное представление ответов этих игроков
        cur.execute("""
        CREATE VIEW valid_answers AS
        SELECT
          a.id,
          a.room_code,
          a.round_no,
          a.question_id,
          a.category,
          a.player_id,
          a.player_name,
          a.answer_text,
          a.answer_choice,
          a.is_correct,
          a.awarded,
          a.time_spent_ms
        FROM answers AS a
        JOIN valid_players AS vp
          ON a.room_code = vp.room_code
         AND a.player_id = vp.player_id
        """)

        con.commit()


def export_to_csv(sql: str, params: tuple, output_path: Path):
    """
    Выполняет SQL-запрос и сохраняет результат в CSV с заголовками столбцов.
    Кодировка: UTF-8 с BOM (utf-8-sig), чтобы Excel корректно показывал русский текст.
    Разделитель: ';' — удобнее для русской локали.
    """
    with get_connection() as con:
        cur = con.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        col_names = [d[0] for d in cur.description]

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(col_names)
        writer.writerows(rows)

    print(f"Сохранено {len(rows)} строк в {output_path}")


# ===== Обработка задания "ЛИСА - НОРА" =====

def _normalize_russian(text: str) -> str:
    """
    Приводим строку к верхнему регистру, приводим Ё -> Е.
    """
    if text is None:
        return ""
    s = text.strip().upper()
    s = s.replace("Ё", "Е")
    return s


def _extract_four_letter_words_ru(text: str):
    """
    Достаём все подряд идущие русские слова из 4 букв.
    Используем только кириллицу.
    """
    s = _normalize_russian(text)
    # Ищем последовательности из 4 русских букв
    return re.findall(r"[А-Я]{4}", s)


def _diff_by_one_letter(w1: str, w2: str) -> bool:
    """
    Проверка, что слова одинаковой длины и отличаются ровно в одной букве.
    """
    if len(w1) != len(w2):
        return False
    diff = 0
    for c1, c2 in zip(w1, w2):
        if c1 != c2:
            diff += 1
            if diff > 1:
                return False
    return diff == 1


def is_correct_word_ladder_lisa_nora(answer_text: str) -> bool:
    """
    Новая, более строгая проверка "лестницы слов" ЛИСА -> ... -> НОРА.

    Требования:
    - в ответе должны быть как минимум 3 четырехбуквенных русских слова;
    - первый элемент лестницы должен быть ЛИСА;
    - последний элемент лестницы должен быть НОРА;
    - каждое следующее слово отличается от предыдущего ровно в одной букве.
    """
    words = _extract_four_letter_words_ru(answer_text)
    if len(words) < 3:
        return False

    if words[0] != "ЛИСА":
        return False
    if words[-1] != "НОРА":
        return False

    for i in range(len(words) - 1):
        if not _diff_by_one_letter(words[i], words[i + 1]):
            return False

    return True


def is_lisa_nora_candidate(answer_text: str) -> bool:
    """
    Определяем, относится ли ответ к заданию "ЛИСА - НОРА".
    Здесь не используем question_id, только содержание текста.

    Правило: в тексте должны встречаться подстроки "ЛИСА" и "НОРА".
    """
    s = _normalize_russian(answer_text)
    return ("ЛИСА" in s) and ("НОРА" in s)


def load_valid_answers_with_fixed_ladder():
    """
    Загружает ответы из представления valid_answers и
    пересчитывает корректность для заданий "ЛИСА - НОРА".

    Возвращает:
      col_names, rows_fixed
    где rows_fixed — уже изменённые строки (is_correct и awarded
    для задач "лестницы" скорректированы).
    """
    with get_connection() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT
              id,
              room_code,
              round_no,
              question_id,
              category,
              player_id,
              player_name,
              answer_text,
              answer_choice,
              is_correct,
              awarded,
              time_spent_ms
            FROM valid_answers
            ORDER BY room_code, round_no, player_name
        """)
        rows = cur.fetchall()
        col_names = [d[0] for d in cur.description]

    # Индексы нужных колонок
    idx_answer_text = col_names.index("answer_text")
    idx_is_correct = col_names.index("is_correct")
    idx_awarded = col_names.index("awarded")

    fixed_rows = []
    for row in rows:
        row = list(row)

        ans_text = row[idx_answer_text]

        if is_lisa_nora_candidate(ans_text):
            # Пересчитываем корректность для лестницы слов
            ok = 1 if is_correct_word_ladder_lisa_nora(ans_text) else 0
            row[idx_is_correct] = ok
            row[idx_awarded] = ok  # предполагаем, что awarded = 1 для верных
        # иначе оставляем как есть

        fixed_rows.append(tuple(row))

    return col_names, fixed_rows


def export_answers_with_fix():
    """
    Делает две выгрузки:
    1) valid_answers.csv — все поля, но с исправленными is_correct/awarded
       для задания "ЛИСА - НОРА".
    2) valid_answers_anon.csv — анонимизированный вариант:
       без player_name и без question_id, но тоже с исправленными полями.
    """
    col_names, rows = load_valid_answers_with_fixed_ladder()

    # 1) Полная выгрузка (как и раньше, только с исправленными значениями)
    with open(OUT_ANSWERS, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(col_names)
        writer.writerows(rows)
    print(f"Сохранено {len(rows)} строк в {OUT_ANSWERS}")

    # 2) Анонимизированная выгрузка: убираем question_id и player_name
    cols_to_exclude = {"question_id", "player_name"}
    anon_col_names = [c for c in col_names if c not in cols_to_exclude]
    anon_indexes = [col_names.index(c) for c in anon_col_names]

    anon_rows = []
    for row in rows:
        anon_row = [row[i] for i in anon_indexes]
        anon_rows.append(anon_row)

    with open(OUT_ANSWERS_ANON, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(anon_col_names)
        writer.writerows(anon_rows)
    print(f"Сохранено {len(anon_rows)} строк в {OUT_ANSWERS_ANON}")


def export_players_with_and_without_names():
    """
    1) valid_players.csv — как раньше (с именем).
    2) valid_players_anon.csv — без имени игрока, только player_id и другие поля.
    """
    with get_connection() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT
              id,
              room_code,
              player_id,
              name,
              score
            FROM valid_players
            ORDER BY room_code, name
        """)
        rows = cur.fetchall()
        col_names = [d[0] for d in cur.description]

    # 1) Полный вариант (как раньше)
    with open(OUT_PLAYERS, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(col_names)
        writer.writerows(rows)
    print(f"Сохранено {len(rows)} строк в {OUT_PLAYERS}")

    # 2) Анонимизированный вариант: удаляем name
    cols_to_exclude = {"name"}
    anon_col_names = [c for c in col_names if c not in cols_to_exclude]
    anon_indexes = [col_names.index(c) for c in anon_col_names]

    anon_rows = []
    for row in rows:
        anon_row = [row[i] for i in anon_indexes]
        anon_rows.append(anon_row)

    with open(OUT_PLAYERS_ANON, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(anon_col_names)
        writer.writerows(anon_rows)
    print(f"Сохранено {len(anon_rows)} строк в {OUT_PLAYERS_ANON}")


def demo_select_for_room(room_code: str):
    """
    Пример того, как работать с SQL из Python:
    печатаем игроков и их ответы по конкретной комнате.
    Берутся уже отфильтрованные представления valid_players и valid_answers.
    """
    with get_connection() as con:
        cur = con.cursor()

        print(f"\nИгроки в комнате {room_code}:")
        cur.execute(
            """
            SELECT room_code, name, score
            FROM valid_players
            WHERE room_code = ?
            ORDER BY score DESC, name
            """,
            (room_code,)
        )
        for row in cur.fetchall():
            print(row)

        print(f"\nОтветы в комнате {room_code}:")
        cur.execute(
            """
            SELECT room_code, round_no, player_name, is_correct, awarded, time_spent_ms
            FROM valid_answers
            WHERE room_code = ?
            ORDER BY round_no, player_name
            """,
            (room_code,)
        )
        for row in cur.fetchall():
            print(row)


if __name__ == "__main__":
    print(f"Использую базу данных: {DB_FILE}")

    # 1. Создаём/обновляем представления
    create_views()

    # 2. Экспортируем игроков (оригинал + анонимизированный)
    export_players_with_and_without_names()

    # 3. Экспортируем ответы с исправлением "ЛИСА - НОРА"
    #    (оригинал + анонимизированный без player_name и question_id)
    export_answers_with_fix()

    # Пример интерактива: можно раскомментировать и подставить реальный код комнаты
    # demo_select_for_room("ABCD")
