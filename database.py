import aiosqlite
from datetime import datetime

DB_PATH = "sevenup.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Юзеры
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                last_name     TEXT,
                is_subscribed INTEGER DEFAULT 0,
                created_at    TEXT,
                last_seen     TEXT
            )
        """)

        # Промокоды
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code         TEXT PRIMARY KEY,
                amount       INTEGER NOT NULL,
                max_uses     INTEGER DEFAULT 0,
                uses         INTEGER DEFAULT 0,
                created_by   INTEGER,
                created_at   TEXT
            )
        """)

        # Активации промокодов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_uses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                code       TEXT NOT NULL,
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL,
                used_at    TEXT,
                UNIQUE(code, user_id)
            )
        """)

        # === ЗАЯВКИ НА ВЫВОД ===
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                username      TEXT,
                first_name    TEXT,
                last_name     TEXT,
                method        TEXT NOT NULL,          -- 'stars' | 'ton'
                amount        INTEGER NOT NULL,       -- звёзды (для stars) или TON в копейках? нет — целые TON
                wallet        TEXT,                    -- TON-кошелёк (только для method='ton')
                status        TEXT DEFAULT 'pending', -- pending | paid | cancelled
                admin_id      INTEGER,                 -- кто обработал
                admin_msg_id  INTEGER,                 -- id сообщения в чате админа (для редактирования)
                admin_chat_id INTEGER,
                created_at    TEXT,
                processed_at  TEXT
            )
        """)

        await db.commit()

        # === МИГРАЦИИ ===
        async with db.execute("PRAGMA table_info(users)") as cursor:
            existing_cols = {row[1] for row in await cursor.fetchall()}

        if "balance" not in existing_cols:
            await db.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0")
            await db.commit()
            print("✅ Миграция: добавлена колонка balance")

        for col, typ in [
            ("total_bet", "INTEGER DEFAULT 0"),
            ("total_won", "INTEGER DEFAULT 0"),
            ("games_played", "INTEGER DEFAULT 0"),
            ("wins_count", "INTEGER DEFAULT 0"),
            ("best_win", "INTEGER DEFAULT 0"),
            ("favorite_game", "TEXT"),
            ("favorite_game_bets", "INTEGER DEFAULT 0"),
        ]:
            if col not in existing_cols:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
                await db.commit()
                print(f"✅ Миграция: добавлена колонка {col}")


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def upsert_user(user: dict):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, is_subscribed, balance, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                last_seen  = excluded.last_seen
        """, (
            user["user_id"], user.get("username"),
            user.get("first_name"), user.get("last_name"),
            user.get("is_subscribed", 0), user.get("balance", 0),
            now, now,
        ))
        await db.commit()


async def set_subscribed(user_id: int, value: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_subscribed = ? WHERE user_id = ?",
                         (1 if value else 0, user_id))
        await db.commit()


async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def add_balance(user_id: int, amount: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id = ?",
                         (amount, user_id))
        await db.commit()
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


# ===== СТАТИСТИКА =====

async def get_stats(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT total_bet, total_won, games_played, wins_count,
                   best_win, favorite_game, favorite_game_bets, balance
            FROM users WHERE user_id = ?
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return {
                    "total_bet": 0, "total_won": 0, "games_played": 0,
                    "wins_count": 0, "best_win": 0,
                    "favorite_game": None, "favorite_game_bets": 0,
                    "balance": 0,
                }
            return dict(row)


async def record_game(user_id: int, game: str, bet: int, won: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT favorite_game, favorite_game_bets FROM users WHERE user_id = ?
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()

        fav = row["favorite_game"] if row else None
        fav_bets = row["favorite_game_bets"] if row else 0

        if fav == game:
            fav_bets += 1
        elif fav is None:
            fav = game
            fav_bets = 1

        is_win = 1 if won > 0 else 0

        await db.execute("""
            UPDATE users SET
                total_bet          = total_bet + ?,
                total_won          = total_won + ?,
                games_played       = games_played + 1,
                wins_count         = wins_count + ?,
                best_win           = MAX(best_win, ?),
                favorite_game      = ?,
                favorite_game_bets = ?
            WHERE user_id = ?
        """, (bet, won, is_win, won, fav, fav_bets, user_id))
        await db.commit()


# ===== ПРОМОКОДЫ =====

async def create_promo(code: str, amount: int, max_uses: int, created_by: int) -> bool:
    code = code.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO promocodes (code, amount, max_uses, uses, created_by, created_at)
                VALUES (?, ?, ?, 0, ?, ?)
            """, (code, amount, max_uses, created_by, datetime.utcnow().isoformat()))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def delete_promo(code: str) -> bool:
    code = code.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM promocodes WHERE code = ?", (code,))
        await db.commit()
        return cursor.rowcount > 0


async def list_promos() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT code, amount, max_uses, uses, created_at
            FROM promocodes ORDER BY created_at DESC
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def redeem_promo(code: str, user_id: int) -> dict:
    code = code.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT * FROM promocodes WHERE code = ?", (code,)) as cursor:
            promo = await cursor.fetchone()
            if not promo:
                return {"ok": False, "amount": 0, "error": "Промокод не найден"}

            promo = dict(promo)

            if promo["max_uses"] > 0 and promo["uses"] >= promo["max_uses"]:
                return {"ok": False, "amount": 0, "error": "Промокод больше не действует"}

            async with db.execute(
                "SELECT id FROM promo_uses WHERE code = ? AND user_id = ?",
                (code, user_id)
            ) as cur:
                if await cur.fetchone():
                    return {"ok": False, "amount": 0, "error": "Ты уже использовал этот промокод"}

            await db.execute(
                "INSERT INTO promo_uses (code, user_id, amount, used_at) VALUES (?, ?, ?, ?)",
                (code, user_id, promo["amount"], datetime.utcnow().isoformat())
            )
            await db.execute(
                "UPDATE promocodes SET uses = uses + 1 WHERE code = ?", (code,)
            )
            await db.execute(
                "UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id = ?",
                (promo["amount"], user_id)
            )
            await db.commit()

            return {"ok": True, "amount": promo["amount"], "error": ""}


# ===== ВЫВОДЫ =====

async def create_withdrawal(user: dict, method: str, amount: int, wallet: str | None) -> dict:
    """
    Создаёт заявку на вывод и сразу списывает баланс (холд).
    Возвращает {ok, withdrawal_id, balance} либо {ok: False, error}
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Проверяем баланс
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user["user_id"],)) as cur:
            row = await cur.fetchone()
            current_balance = row["balance"] if row else 0
        if current_balance < amount:
            return {"ok": False, "error": "Недостаточно средств"}

        # Списание
        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, user["user_id"])
        )

        cur = await db.execute("""
            INSERT INTO withdrawals
                (user_id, username, first_name, last_name, method, amount, wallet, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            user["user_id"],
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
            method,
            amount,
            wallet,
            now,
        ))
        withdrawal_id = cur.lastrowid

        await db.commit()

        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user["user_id"],)) as cur:
            row = await cur.fetchone()
            new_balance = row["balance"] if row else 0

        return {"ok": True, "withdrawal_id": withdrawal_id, "balance": new_balance}


async def get_withdrawal(withdrawal_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_withdrawal_status(withdrawal_id: int, status: str, admin_id: int) -> dict | None:
    """status: 'paid' | 'cancelled'. При cancelled возвращает баланс пользователю."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)) as cur:
            w = await cur.fetchone()
        if not w:
            return None
        w = dict(w)
        if w["status"] != "pending":
            return {**w, "_already_processed": True}

        await db.execute("""
            UPDATE withdrawals SET status = ?, admin_id = ?, processed_at = ? WHERE id = ?
        """, (status, admin_id, now, withdrawal_id))

        if status == "cancelled":
            # вернуть баланс
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (w["amount"], w["user_id"])
            )

        await db.commit()

        async with db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)


async def save_admin_message(withdrawal_id: int, admin_msg_id: int, admin_chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE withdrawals SET admin_msg_id = ?, admin_chat_id = ? WHERE id = ?",
            (admin_msg_id, admin_chat_id, withdrawal_id)
        )
        await db.commit()


async def list_user_withdrawals(user_id: int, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, method, amount, wallet, status, created_at, processed_at
            FROM withdrawals WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]