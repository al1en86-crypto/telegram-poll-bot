import aiosqlite
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                poll_id TEXT PRIMARY KEY,
                question TEXT,
                options TEXT,
                correct_option_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS poll_sends (
                telegram_poll_id TEXT PRIMARY KEY,
                canonical_poll_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS poll_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT,
                user_id INTEGER,
                option_ids TEXT,
                answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(poll_id, user_id)
            )
        """)
        try:
            await db.execute("ALTER TABLE polls ADD COLUMN correct_option_id INTEGER")
        except Exception:
            pass
        await db.commit()


async def save_user(user_id: int, username: str | None, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username, first_name),
        )
        await db.commit()


async def get_all_users() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, first_name FROM users") as cursor:
            return await cursor.fetchall()


async def get_users_list() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, first_name, username, joined_at FROM users ORDER BY joined_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {"user_id": r[0], "first_name": r[1], "username": r[2], "joined_at": r[3]}
        for r in rows
    ]


async def save_poll(
    poll_id: str,
    question: str,
    options: list[str],
    correct_option_id: int | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO polls (poll_id, question, options, correct_option_id)
            VALUES (?, ?, ?, ?)
            """,
            (poll_id, question, json.dumps(options), correct_option_id),
        )
        await db.commit()


async def save_poll_sends(sends: list[tuple[str, str, int]]):
    """Save (telegram_poll_id, canonical_poll_id, user_id) mappings."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO poll_sends (telegram_poll_id, canonical_poll_id, user_id) VALUES (?, ?, ?)",
            sends,
        )
        await db.commit()


async def get_canonical_poll_id(telegram_poll_id: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT canonical_poll_id FROM poll_sends WHERE telegram_poll_id = ?",
            (telegram_poll_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def save_poll_answer(poll_id: str, user_id: int, option_ids: list[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO poll_answers (poll_id, user_id, option_ids)
            VALUES (?, ?, ?)
            ON CONFLICT(poll_id, user_id)
            DO UPDATE SET option_ids = excluded.option_ids,
                          answered_at = CURRENT_TIMESTAMP
            """,
            (poll_id, user_id, json.dumps(option_ids)),
        )
        await db.commit()


async def get_unanswered_polls(user_id: int) -> list[dict]:
    """Return all canonical polls this user has not yet answered."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT p.poll_id, p.question, p.options, p.correct_option_id
            FROM polls p
            WHERE p.poll_id NOT IN (
                SELECT pa.poll_id FROM poll_answers pa WHERE pa.user_id = ?
            )
            ORDER BY p.created_at ASC
            """,
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        {
            "poll_id": poll_id,
            "question": question,
            "options": json.loads(options_json),
            "correct_option_id": correct_option_id,
        }
        for poll_id, question, options_json, correct_option_id in rows
    ]


async def get_polls_with_results() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT poll_id, question, options FROM polls ORDER BY created_at DESC"
        ) as cursor:
            polls = await cursor.fetchall()

        result = []
        for poll_id, question, options_json in polls:
            options = json.loads(options_json)
            counts = [0] * len(options)

            async with db.execute(
                "SELECT option_ids FROM poll_answers WHERE poll_id = ?", (poll_id,)
            ) as cursor:
                answers = await cursor.fetchall()

            for (option_ids_json,) in answers:
                for idx in json.loads(option_ids_json):
                    if 0 <= idx < len(counts):
                        counts[idx] += 1

            total = sum(counts)
            result.append(
                {
                    "question": question,
                    "total": total,
                    "options": [
                        {"text": opt, "count": counts[i]}
                        for i, opt in enumerate(options)
                    ],
                }
            )

        return result


async def get_all_polls() -> list[dict]:
    """Return all polls ordered by creation date (newest first)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT poll_id, question, options, correct_option_id FROM polls ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        {
            "poll_id": poll_id,
            "question": question,
            "options": json.loads(options_json),
            "correct_option_id": correct_option_id,
        }
        for poll_id, question, options_json, correct_option_id in rows
    ]


async def delete_poll(poll_id: str):
    """Delete a poll and all its answers and send records."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM poll_answers WHERE poll_id = ?", (poll_id,))
        await db.execute("DELETE FROM poll_sends WHERE canonical_poll_id = ?", (poll_id,))
        await db.execute("DELETE FROM polls WHERE poll_id = ?", (poll_id,))
        await db.commit()


async def get_quiz_polls() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT poll_id, question, options, correct_option_id
            FROM polls
            WHERE correct_option_id IS NOT NULL
            ORDER BY created_at DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        {
            "poll_id": poll_id,
            "question": question,
            "options": json.loads(options_json),
            "correct_option_id": correct_option_id,
        }
        for poll_id, question, options_json, correct_option_id in rows
    ]


async def get_top5_correct(poll_id: str, correct_option_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT u.user_id, u.first_name, u.username, pa.option_ids, pa.answered_at
            FROM poll_answers pa
            JOIN users u ON u.user_id = pa.user_id
            WHERE pa.poll_id = ?
            ORDER BY pa.answered_at ASC
            """,
            (poll_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    result = []
    for user_id, first_name, username, option_ids_json, answered_at in rows:
        option_ids = json.loads(option_ids_json)
        if correct_option_id in option_ids:
            result.append(
                {
                    "user_id": user_id,
                    "first_name": first_name,
                    "username": username,
                    "answered_at": answered_at,
                }
            )
        if len(result) == 5:
            break

    return result
