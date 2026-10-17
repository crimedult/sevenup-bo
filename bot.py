import asyncio
import logging
import time
import secrets

import aiosqlite
from aiohttp import web
import aiohttp_cors

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    LabeledPrice, PreCheckoutQuery, Message, ChatMemberUpdated,
    CallbackQuery,
)

from telegram_init_data import validate, parse
from database import (
    init_db, upsert_user, set_subscribed, get_balance, add_balance,
    get_stats, record_game, create_promo, delete_promo, list_promos,
    redeem_promo, DB_PATH,
    create_withdrawal, get_withdrawal, set_withdrawal_status,
    save_admin_message, list_user_withdrawals,
)

from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.tl import types as tl_types


# ============================================================
# ====================== НАСТРОЙКИ ===========================
# ============================================================

BOT_TOKEN = "8894413838:AAEJKjV6ozkH50zz8vdoRohjtzujXkW6aCQ"
WEBAPP_URL = "https://seven-up-psi.vercel.app/"
CHANNEL_ID = -1003888559843
CHANNEL_URL = "https://t.me/SevenUpCasino"

API_HOST = "0.0.0.0"
API_PORT = int(os.environ.get("PORT", 3000))

TG_API_ID = 27947766
TG_API_HASH = "c4978ed1ee29460a66b12d8bd7a65dd0"

TG_SESSION_STRING = "1AZWarzMBuwGKI57nUHfFzCfqFrVRAO-5aX9H5mJ56FO3jg3EkYxMgp6_YT5IanF-QlVqvpCb42lRAui46_e7rPtdbw0Mhw-fEfwEqMOtZV01sNY3zHnQ6dbFioy-XoTPBmZngfEtoT9Wn7tgWsgBSBJ7PAfksHjQ1fvOq0oAeDh2ZBn8Olp1NPX8ZAiwdo6g0tZwSG3g4MzGoE3hckUD8p1M1JrbDbgQQx-02Y7XbsbtXGDkpEaI5hcmYlEJzp8HGLYtoB3rjUsTMo0Tui6htiXvC0yl4vZlA6Ubz2IF4NNemvxdwLfXN8AAAbftZGQd_HeOGYtFjpNhrxigziuXvdtrTqHI6hA="

STARS_PER_USD = 60

ADMIN_IDS = [7329380554, 1383466077]

# --- Лимиты вывода ---
WITHDRAWAL_STARS_MIN = 100
WITHDRAWAL_STARS_MAX = 50_000
WITHDRAWAL_TON_MIN = 1              # TON
WITHDRAWAL_TON_MAX = 100_000        # TON
# курс: сколько звёзд за 1 TON. Настраивается вручную/админом.
STARS_PER_TON = 500


# ============================================================
# ======================= ЛОГИ ===============================
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("sevenup")


# ============================================================
# ======================= БОТ ================================
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()


# ============================================================
# ============== CRASH ROUND MANAGER =========================
# ============================================================

class CrashRoundManager:
    WAIT_TIME = 10.0
    CRASHED_TIME = 3.0
    TICK_RATE = 0.06
    CRASH_RTP = 0.96
    MAX_MULT = 500

    def __init__(self):
        self.phase = "waiting"
        self.phase_start = time.time()
        self.crash_point = 1.0
        self.players = {}
        self.round_id = 0
        self.history = []
        self._lock = asyncio.Lock()
        self._new_waiting_round()

    def _new_waiting_round(self):
        self.phase = "waiting"
        self.phase_start = time.time()
        self.crash_point = self._generate_crash_point()
        self.players = {}
        self.round_id += 1
        log.info(
            f"🎰 Crash round #{self.round_id} — waiting {self.WAIT_TIME}s, "
            f"crashPoint=×{self.crash_point:.2f}"
        )

    def _generate_crash_point(self) -> float:
        r = secrets.randbelow(10**9) / 10**9
        if r < 0.03:
            return 1.00
        point = self.CRASH_RTP / (1 - r)
        return max(1.01, min(point, self.MAX_MULT))

    def _current_multiplier(self) -> float:
        if self.phase != "flying":
            return 1.00
        elapsed = time.time() - self.phase_start
        return max(1.00, min(self.crash_point, pow(2.718281828, self.TICK_RATE * elapsed)))

    async def tick(self):
        async with self._lock:
            now = time.time()
            elapsed = now - self.phase_start

            if self.phase == "waiting" and elapsed >= self.WAIT_TIME:
                self.phase = "flying"
                self.phase_start = now
                log.info(f"🚀 Crash round #{self.round_id} — flying")

            elif self.phase == "flying":
                mult = self._current_multiplier()
                if mult >= self.crash_point:
                    self.phase = "crashed"
                    self.phase_start = now
                    self.history.insert(0, self.crash_point)
                    if len(self.history) > 20:
                        self.history.pop()
                    log.info(f"💥 Crash round #{self.round_id} — crashed at ×{self.crash_point:.2f}")

                    for uid, p in self.players.items():
                        if not p["cashed"]:
                            await record_game(uid, "crash", p["bet"], 0)

            elif self.phase == "crashed" and elapsed >= self.CRASHED_TIME:
                self._new_waiting_round()

    async def place_bet(self, user_id: int, bet: int, name: str, avatar: str):
        async with self._lock:
            if self.phase != "waiting":
                return {"ok": False, "error": "not_waiting"}
            if user_id in self.players:
                return {"ok": False, "error": "already_bet"}

            balance = await get_balance(user_id)
            if balance < bet:
                return {"ok": False, "error": "insufficient"}

            await add_balance(user_id, -bet)
            self.players[user_id] = {
                "bet": bet,
                "name": name or "Игрок",
                "avatar": avatar or "",
                "cashed": False,
                "cashout_mult": 0.0,
            }
            return {"ok": True}

    async def cashout(self, user_id: int):
        async with self._lock:
            if self.phase != "flying":
                return {"ok": False, "error": "not_flying"}
            player = self.players.get(user_id)
            if not player or player["cashed"]:
                return {"ok": False, "error": "no_bet"}

            mult = self._current_multiplier()
            if mult >= self.crash_point:
                return {"ok": False, "error": "crashed"}

            win = cap_win(round(player["bet"] * mult))
            player["cashed"] = True
            player["cashout_mult"] = mult

            await add_balance(user_id, win)
            await record_game(user_id, "crash", player["bet"], win)

            return {"ok": True, "mult": mult, "win": win}

    def state_for_client(self, user_id: int):
        now = time.time()
        elapsed = now - self.phase_start

        if self.phase == "waiting":
            remaining = max(0.0, self.WAIT_TIME - elapsed)
            current_mult = 1.00
        elif self.phase == "flying":
            remaining = 0.0
            current_mult = self._current_multiplier()
        else:
            remaining = max(0.0, self.CRASHED_TIME - elapsed)
            current_mult = self.crash_point

        players_list = []
        for uid, p in self.players.items():
            players_list.append({
                "user_id": uid,
                "name": p["name"],
                "avatar": p["avatar"],
                "bet": p["bet"],
                "cashed": p["cashed"],
                "cashout_mult": round(p["cashout_mult"], 2),
            })

        my = self.players.get(user_id)
        my_bet = None
        if my:
            my_bet = {
                "bet": my["bet"],
                "cashed": my["cashed"],
                "cashout_mult": round(my["cashout_mult"], 2),
            }

        return {
            "phase": self.phase,
            "round_id": self.round_id,
            "phase_remaining": round(remaining, 2),
            "current_mult": round(current_mult, 4),
            "crash_point": round(self.crash_point, 2) if self.phase == "crashed" else None,
            "players_count": len(self.players),
            "players": players_list,
            "my_bet": my_bet,
            "history": self.history[:20],
        }


crash_manager = CrashRoundManager()

MAX_WIN_STARS = 800_000
GAME_RTP = 0.96
MINES_GRID = 25


def cap_win(amount: int) -> int:
    return max(0, min(MAX_WIN_STARS, int(amount)))


def _ncr(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    k = min(k, n - k)
    r = 1
    for i in range(k):
        r = r * (n - i) // (i + 1)
    return r


def mine_multipliers(mines: int) -> list:
    mines = max(1, min(24, int(mines)))
    safe = MINES_GRID - mines
    out = []
    for k in range(1, safe + 1):
        den = _ncr(MINES_GRID, k)
        prob = (_ncr(safe, k) / den) if den else 1
        out.append(GAME_RTP / prob if prob > 0 else 1.0)
    return out


def _cow_diff(steps: int, last: float) -> dict:
    s = (GAME_RTP / last) ** (1 / steps)
    mults = []
    for k in range(1, steps + 1):
        mults.append(round(GAME_RTP / (s ** k), 2))
    mults[-1] = float(last)
    return {"steps": steps, "crash_p": 1.0 - s, "mults": mults, "survive": s}


COW_DIFFS = {
    "easy": {**_cow_diff(8, 26), "label": "Лёгкая"},
    "medium": {**_cow_diff(10, 650), "label": "Средняя"},
    "hard": {**_cow_diff(12, 12000), "label": "Тяжёлая"},
    "hardcore": {**_cow_diff(15, 300000), "label": "Хардкор"},
}


class CowManager:
    def __init__(self):
        self.sessions = {}
        self._lock = asyncio.Lock()

    def _public(self, user_id: int):
        sess = self.sessions.get(user_id)
        if not sess or not sess.get("active"):
            return {"active": False}
        cfg = COW_DIFFS[sess["diff"]]
        step = sess["step"]
        mult = cfg["mults"][step - 1] if step else 1.0
        win = cap_win(round(sess["bet"] * mult)) if step else 0
        return {
            "active": True,
            "difficulty": sess["diff"],
            "bet": sess["bet"],
            "step": step,
            "mult": mult,
            "win": win,
            "steps": cfg["steps"],
        }

    async def start(self, user_id: int, bet: int, difficulty: str):
        async with self._lock:
            if difficulty not in COW_DIFFS:
                return {"ok": False, "error": "bad_diff"}
            if bet < 1:
                return {"ok": False, "error": "bet_too_low"}
            prev = self.sessions.get(user_id)
            if prev and prev.get("active"):
                return {"ok": False, "error": "already_playing"}
            balance = await get_balance(user_id)
            if balance < bet:
                return {"ok": False, "error": "insufficient"}
            await add_balance(user_id, -bet)
            cfg = COW_DIFFS[difficulty]
            self.sessions[user_id] = {
                "bet": bet,
                "diff": difficulty,
                "step": 0,
                "active": True,
            }
            return {
                "ok": True,
                "difficulty": difficulty,
                "bet": bet,
                "steps": cfg["steps"],
                "mults": cfg["mults"],
                "balance": await get_balance(user_id),
            }

    async def step(self, user_id: int):
        async with self._lock:
            sess = self.sessions.get(user_id)
            if not sess or not sess.get("active"):
                return {"ok": False, "error": "no_game"}
            cfg = COW_DIFFS[sess["diff"]]
            if sess["step"] >= cfg["steps"]:
                return {"ok": False, "error": "finished"}
            r = secrets.randbelow(10**9) / 10**9
            if r < cfg["crash_p"]:
                sess["active"] = False
                await record_game(user_id, "cow", sess["bet"], 0)
                return {
                    "ok": True,
                    "crashed": True,
                    "step": sess["step"],
                    "hit_lane": sess["step"] + 1,
                    "balance": await get_balance(user_id),
                }
            sess["step"] += 1
            mult = cfg["mults"][sess["step"] - 1]
            win = cap_win(round(sess["bet"] * mult))
            finished = sess["step"] >= cfg["steps"]
            if finished:
                sess["active"] = False
                await add_balance(user_id, win)
                await record_game(user_id, "cow", sess["bet"], win)
            return {
                "ok": True,
                "crashed": False,
                "finished": finished,
                "step": sess["step"],
                "mult": mult,
                "win": win,
                "balance": await get_balance(user_id),
            }

    async def cashout(self, user_id: int):
        async with self._lock:
            sess = self.sessions.get(user_id)
            if not sess or not sess.get("active"):
                return {"ok": False, "error": "no_game"}
            if sess["step"] < 1:
                return {"ok": False, "error": "no_step"}
            cfg = COW_DIFFS[sess["diff"]]
            mult = cfg["mults"][sess["step"] - 1]
            win = cap_win(round(sess["bet"] * mult))
            sess["active"] = False
            await add_balance(user_id, win)
            await record_game(user_id, "cow", sess["bet"], win)
            return {
                "ok": True,
                "mult": mult,
                "win": win,
                "balance": await get_balance(user_id),
            }


cow_manager = CowManager()


class MinesManager:
    def __init__(self):
        self.sessions = {}
        self._lock = asyncio.Lock()

    async def start(self, user_id: int, bet: int, mines: int):
        async with self._lock:
            mines = max(1, min(24, int(mines)))
            if bet < 1:
                return {"ok": False, "error": "bet_too_low"}
            prev = self.sessions.get(user_id)
            if prev and prev.get("active"):
                return {"ok": False, "error": "already_playing"}
            balance = await get_balance(user_id)
            if balance < bet:
                return {"ok": False, "error": "insufficient"}
            await add_balance(user_id, -bet)
            positions = set()
            while len(positions) < mines:
                positions.add(secrets.randbelow(MINES_GRID))
            self.sessions[user_id] = {
                "bet": bet,
                "mines": mines,
                "positions": positions,
                "revealed": set(),
                "active": True,
            }
            return {
                "ok": True,
                "bet": bet,
                "mines": mines,
                "mults": [round(m, 4) for m in mine_multipliers(mines)],
                "balance": await get_balance(user_id),
            }

    async def reveal(self, user_id: int, index: int):
        async with self._lock:
            sess = self.sessions.get(user_id)
            if not sess or not sess.get("active"):
                return {"ok": False, "error": "no_game"}
            if index < 0 or index >= MINES_GRID:
                return {"ok": False, "error": "bad_index"}
            if index in sess["revealed"]:
                return {"ok": False, "error": "already"}
            if index in sess["positions"]:
                sess["active"] = False
                await record_game(user_id, "mines", sess["bet"], 0)
                return {
                    "ok": True,
                    "hit": True,
                    "index": index,
                    "mines": sorted(sess["positions"]),
                    "revealed": sorted(sess["revealed"]),
                    "balance": await get_balance(user_id),
                }
            sess["revealed"].add(index)
            mults = mine_multipliers(sess["mines"])
            mult = mults[len(sess["revealed"]) - 1]
            win = cap_win(round(sess["bet"] * mult))
            return {
                "ok": True,
                "hit": False,
                "index": index,
                "revealed": sorted(sess["revealed"]),
                "mult": round(mult, 4),
                "win": win,
                "balance": await get_balance(user_id),
            }

    async def cashout(self, user_id: int):
        async with self._lock:
            sess = self.sessions.get(user_id)
            if not sess or not sess.get("active"):
                return {"ok": False, "error": "no_game"}
            if not sess["revealed"]:
                return {"ok": False, "error": "no_gems"}
            mults = mine_multipliers(sess["mines"])
            mult = mults[len(sess["revealed"]) - 1]
            win = cap_win(round(sess["bet"] * mult))
            sess["active"] = False
            await add_balance(user_id, win)
            await record_game(user_id, "mines", sess["bet"], win)
            return {
                "ok": True,
                "mult": round(mult, 4),
                "win": win,
                "mines": sorted(sess["positions"]),
                "balance": await get_balance(user_id),
            }


mines_manager = MinesManager()


async def crash_loop():
    while True:
        try:
            await crash_manager.tick()
        except Exception as e:
            log.error(f"crash_loop error: {e}")
        await asyncio.sleep(0.1)


# ============================================================
# ================== AIOGRAM =================================
# ============================================================

def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")],
    ])


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Запустить Mini App 🚀", web_app=WebAppInfo(url=WEBAPP_URL))],
    ])


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.error(f"Ошибка проверки подписки для {user_id}: {e}")
        return False


@dp.message(CommandStart())
async def start_command(message: types.Message):
    user = message.from_user
    await upsert_user({
        "user_id": user.id, "username": user.username,
        "first_name": user.first_name, "last_name": user.last_name,
    })
    if await is_subscribed(user.id):
        await set_subscribed(user.id, True)
        await message.answer(
            f"Привет, {user.first_name}! 👋\n\n"
            "Добро пожаловать в <b>Seven Up Casino</b>.\n"
            "Нажми на кнопку ниже, чтобы открыть приложение:",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer(
            "🔒 <b>Доступ ограничен</b>\n\n"
            "Чтобы продолжить, подпишись на наш канал:\n"
            f"{CHANNEL_URL}\n\n"
            "После подписки нажми <b>«Проверить подписку»</b>.",
            reply_markup=subscribe_keyboard(),
            disable_web_page_preview=True,
        )


@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: types.CallbackQuery):
    user = callback.from_user
    if await is_subscribed(user.id):
        await set_subscribed(user.id, True)
        await callback.message.edit_text(
            f"✅ <b>Подписка подтверждена!</b>\n\n"
            f"Привет, {user.first_name}! Добро пожаловать в <b>Seven Up Casino</b>.\n"
            "Нажми на кнопку ниже, чтобы открыть приложение:",
            reply_markup=main_keyboard(),
        )
    else:
        await callback.answer("❌ Ты ещё не подписан на канал.", show_alert=True)


@dp.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID:
        return
    user_id = event.from_user.id
    new_status = event.new_chat_member.status
    if new_status in ("member", "administrator", "creator"):
        await set_subscribed(user_id, True)
    elif new_status in ("left", "kicked"):
        await set_subscribed(user_id, False)


# ============================================================
# ============ ВЫВОДЫ: уведомления и callback'и ==============
# ============================================================

def _fmt_user_full(w: dict) -> str:
    uname = f"@{w['username']}" if w.get("username") else "—"
    name = " ".join(filter(None, [w.get("first_name"), w.get("last_name")])) or "—"
    return f"<b>{name}</b>\n{uname}\nID: <code>{w['user_id']}</code>"


def _withdrawal_message_text(w: dict) -> str:
    method_label = "⭐ Telegram Stars" if w["method"] == "stars" else "💎 TON"
    if w["method"] == "stars":
        detail = f"<b>{w['amount']} ⭐</b>"
    else:
        detail = f"<b>{w['amount']} TON</b>"
        if w.get("wallet"):
            detail += f"\nКошелёк: <code>{w['wallet']}</code>"

    status_label = {
        "pending": "⏳ Ожидает обработки",
        "paid": "✅ Выплачено",
        "cancelled": "❌ Отменено",
    }.get(w["status"], w["status"])

    return (
        f"<b>💸 Заявка на вывод №{w['id']}</b>\n\n"
        f"{_fmt_user_full(w)}\n\n"
        f"Способ: {method_label}\n"
        f"Сумма: {detail}\n"
        f"Статус: {status_label}\n"
        f"Создана: {w['created_at'][:19].replace('T', ' ')}"
    )


def _admin_withdrawal_keyboard(w: dict) -> InlineKeyboardMarkup:
    if w["status"] != "pending":
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выплачено", callback_data=f"wd_paid:{w['id']}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"wd_cancel:{w['id']}"),
        ]
    ])


async def notify_admins_new_withdrawal(w: dict):
    text = _withdrawal_message_text(w)
    kb = _admin_withdrawal_keyboard(w)
    for admin_id in ADMIN_IDS:
        try:
            msg = await bot.send_message(admin_id, text, reply_markup=kb)
            # сохраняем id сообщения только для первого админа (кнопки обновляем у всех через callback)
            await save_admin_message(w["id"], msg.message_id, msg.chat.id)
        except Exception as e:
            log.warning(f"Не удалось отправить заявку №{w['id']} админу {admin_id}: {e}")


@dp.callback_query(F.data.startswith("wd_paid:") | F.data.startswith("wd_cancel:"))
async def withdrawal_admin_action(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("⛔ Нет прав", show_alert=True)
        return

    action, wd_id_str = cb.data.split(":", 1)
    try:
        wd_id = int(wd_id_str)
    except ValueError:
        await cb.answer("Некорректный id", show_alert=True)
        return

    w = await get_withdrawal(wd_id)
    if not w:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if w["status"] != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    new_status = "paid" if action == "wd_paid" else "cancelled"
    updated = await set_withdrawal_status(wd_id, new_status, cb.from_user.id)
    if not updated:
        await cb.answer("Не удалось обновить", show_alert=True)
        return

    # Обновляем сообщение у всех админов — но проще у того, кто нажал
    try:
        await cb.message.edit_text(
            _withdrawal_message_text(updated),
            reply_markup=_admin_withdrawal_keyboard(updated),
        )
    except Exception as e:
        log.warning(f"Не удалось отредактировать сообщение: {e}")

    # Уведомляем юзера
    try:
        if new_status == "paid":
            user_text = (
                f"✅ <b>Вывод №{wd_id} успешно выполнен!</b>\n\n"
                f"Сумма: <b>{w['amount']} "
                f"{'⭐' if w['method'] == 'stars' else 'TON'}</b>\n\n"
                f"Спасибо за игру в Seven Up!"
            )
        else:
            user_text = (
                f"❌ <b>Вывод №{wd_id} отменён</b>\n\n"
                f"Сумма <b>{w['amount']} "
                f"{'⭐' if w['method'] == 'stars' else 'TON'}</b> "
                f"возвращена на баланс.\n\n"
                f"Если есть вопросы — напиши в поддержку."
            )
        await bot.send_message(w["user_id"], user_text)
    except Exception as e:
        log.warning(f"Не удалось уведомить юзера {w['user_id']}: {e}")

    await cb.answer("✅ Готово")


# ============================================================
# ================ API (AIOHTTP) =============================
# ============================================================

def get_user_from_init_data(init_data_str: str):
    try:
        validate(init_data_str, BOT_TOKEN)
        parsed = parse(init_data_str)
        return parsed["user"]["id"], parsed["user"]
    except Exception as e:
        log.warning(f"Invalid initData: {e}")
        return None, None


async def create_invoice_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    amount_stars = int(data.get("amount_stars", 0))
    if amount_stars < 10:
        return web.json_response({"error": "amount_too_low"}, status=400)
    if amount_stars > 10000:
        return web.json_response({"error": "amount_too_high"}, status=400)

    try:
        link = await bot.create_invoice_link(
            title="Пополнение баланса",
            description=f"{amount_stars} звёзд на баланс Seven Up",
            payload=f"topup_{user_id}_{amount_stars}",
            currency="XTR",
            prices=[LabeledPrice(label="Stars", amount=amount_stars)],
        )
    except Exception as e:
        log.error(f"create_invoice_link failed: {e}")
        return web.json_response({"error": "invoice_failed"}, status=500)

    return web.json_response({"invoice_link": link})


async def get_balance_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    balance = await get_balance(user_id)
    return web.json_response({"balance": balance})


async def get_stats_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    stats = await get_stats(user_id)
    return web.json_response(stats)


async def record_game_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    game = data.get("game", "mines")
    bet = int(data.get("bet", 0))
    won = int(data.get("won", 0))

    await record_game(user_id, game, bet, won)
    return web.json_response({"ok": True})


async def online_counts_handler(request: web.Request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*) FROM users
            WHERE strftime('%s', REPLACE(last_seen, 'T', ' ')) >= strftime('%s', 'now', '-3 minutes')
        """) as cursor:
            row = await cursor.fetchone()
            real_online = row[0] if row else 0

    crash_real = len(crash_manager.players)
    crash_fake = 15 + (int(time.time() // 60) % 30)
    crash_total = crash_real + crash_fake

    mines_fake = 20 + (int(time.time() // 45) % 40)
    cow_fake = 18 + (int(time.time() // 50) % 35)
    upgrader_fake = 12 + (int(time.time() // 40) % 25)

    return web.json_response({
        "mines": mines_fake,
        "crash": crash_total,
        "cow": cow_fake,
        "upgrader": upgrader_fake,
        "online_total": max(real_online + 10, 15),
    })


async def crash_state_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    state = crash_manager.state_for_client(user_id)
    balance = await get_balance(user_id)
    state["balance"] = balance
    return web.json_response(state)


async def crash_bet_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, user_obj = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    bet = int(data.get("bet", 0))
    if bet < 1:
        return web.json_response({"ok": False, "error": "bet_too_low"}, status=400)

    name = user_obj.get("first_name", "Игрок")
    avatar = user_obj.get("photo_url", "")

    result = await crash_manager.place_bet(user_id, bet, name, avatar)
    if not result["ok"]:
        return web.json_response(result, status=400)

    return web.json_response({"ok": True})


async def crash_cashout_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    result = await crash_manager.cashout(user_id)
    if not result["ok"]:
        return web.json_response(result, status=400)

    return web.json_response(result)


async def cow_config_handler(request: web.Request):
    diffs = {}
    for key, cfg in COW_DIFFS.items():
        diffs[key] = {
            "label": cfg["label"],
            "steps": cfg["steps"],
            "mults": cfg["mults"],
            "crash_p": round(cfg["crash_p"], 4),
        }
    return web.json_response({"ok": True, "max_win": MAX_WIN_STARS, "diffs": diffs})


async def cow_start_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    bet = int(data.get("bet", 0))
    difficulty = str(data.get("difficulty", "easy"))
    result = await cow_manager.start(user_id, bet, difficulty)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def cow_step_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    result = await cow_manager.step(user_id)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def cow_cashout_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    result = await cow_manager.cashout(user_id)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def mines_start_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    result = await mines_manager.start(user_id, int(data.get("bet", 0)), int(data.get("mines", 3)))
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def mines_reveal_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    result = await mines_manager.reveal(user_id, int(data.get("index", -1)))
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def mines_cashout_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    result = await mines_manager.cashout(user_id)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def upgrader_play_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    bet = int(data.get("bet", 0))
    try:
        mult = float(data.get("mult", 0))
    except Exception:
        mult = 0
    if bet < 1:
        return web.json_response({"ok": False, "error": "bet_too_low"}, status=400)
    if mult < 1.1 or mult > 20:
        return web.json_response({"ok": False, "error": "bad_mult"}, status=400)
    balance = await get_balance(user_id)
    if balance < bet:
        return web.json_response({"ok": False, "error": "insufficient"}, status=400)
    await add_balance(user_id, -bet)
    chance = min(1.0, GAME_RTP / mult)
    will_win = (secrets.randbelow(10**9) / 10**9) < chance
    win = cap_win(round(bet * mult)) if will_win else 0
    if win:
        await add_balance(user_id, win)
    await record_game(user_id, "upgrader", bet, win)
    return web.json_response({
        "ok": True,
        "win": will_win,
        "payout": win,
        "mult": mult,
        "chance": chance,
        "balance": await get_balance(user_id),
    })


async def admin_add_balance_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    admin_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not admin_id or admin_id not in ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)

    target_id = int(data.get("user_id", 0))
    amount = int(data.get("amount", 0))

    if not target_id or target_id <= 0:
        return web.json_response({"error": "bad_user_id"}, status=400)
    if amount == 0:
        return web.json_response({"error": "bad_amount"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return web.json_response({"error": "user_not_found"}, status=404)

    new_balance = await add_balance(target_id, amount)
    log.info(f"💰 Admin {admin_id} added {amount} to {target_id} → {new_balance}")

    return web.json_response({"ok": True, "new_balance": new_balance})


async def redeem_promo_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    code = data.get("code", "").strip()
    if not code:
        return web.json_response({"ok": False, "error": "Пустой код"}, status=400)

    result = await redeem_promo(code, user_id)
    if result["ok"]:
        return web.json_response({
            "ok": True,
            "amount": result["amount"],
            "balance": await get_balance(user_id),
        })
    return web.json_response({"ok": False, "error": result["error"]})


async def admin_create_promo_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id or user_id not in ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)

    code = data.get("code", "").strip()
    amount = int(data.get("amount", 0))
    max_uses = int(data.get("max_uses", 0))

    if not code or len(code) < 3:
        return web.json_response({"error": "code_too_short"}, status=400)
    if amount <= 0:
        return web.json_response({"error": "amount_invalid"}, status=400)

    ok = await create_promo(code, amount, max_uses, user_id)
    if not ok:
        return web.json_response({"error": "code_exists"}, status=409)

    return web.json_response({"ok": True})


async def admin_list_promos_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id or user_id not in ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)

    promos = await list_promos()
    return web.json_response({"promos": promos})


async def admin_delete_promo_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id or user_id not in ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)

    code = data.get("code", "").strip()
    ok = await delete_promo(code)
    return web.json_response({"ok": ok})


# === ВЫВОДЫ: API ===

async def withdraw_request_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id, user_obj = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    method = str(data.get("method", "")).lower()
    if method not in ("stars", "ton"):
        return web.json_response({"ok": False, "error": "bad_method"}, status=400)

    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad_amount"}, status=400)

    wallet = None
    if method == "stars":
        if amount < WITHDRAWAL_STARS_MIN:
            return web.json_response({"ok": False, "error": f"Минимум {WITHDRAWAL_STARS_MIN} ⭐"}, status=400)
        if amount > WITHDRAWAL_STARS_MAX:
            return web.json_response({"ok": False, "error": f"Максимум {WITHDRAWAL_STARS_MAX} ⭐"}, status=400)
    else:  # ton
        if amount < WITHDRAWAL_TON_MIN:
            return web.json_response({"ok": False, "error": f"Минимум {WITHDRAWAL_TON_MIN} TON"}, status=400)
        if amount > WITHDRAWAL_TON_MAX:
            return web.json_response({"ok": False, "error": f"Максимум {WITHDRAWAL_TON_MAX} TON"}, status=400)
        wallet = str(data.get("wallet", "")).strip()
        # Простая проверка формата TON-кошелька
        if not wallet or len(wallet) < 10:
            return web.json_response({"ok": False, "error": "Некорректный TON-кошелёк"}, status=400)
        if not (wallet.startswith("EQ") or wallet.startswith("UQ")
                or wallet.startswith("0:") or wallet.startswith("-1:")):
            return web.json_response({"ok": False, "error": "Неверный формат кошелька"}, status=400)

    # При выборе TON: нужно конвертировать TON → звёзды для списания
    # 1 TON = STARS_PER_TON звёзд. Списываем в звёздах, в заявке храним TON.
    if method == "ton":
        stars_cost = amount * STARS_PER_TON
    else:
        stars_cost = amount

    # Сохраняем в БД заявку: поле amount — это то, что выдаём (звёзды или TON).
    # Но списываем всегда stars_cost.
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if not row or row["balance"] < stars_cost:
            return web.json_response({
                "ok": False,
                "error": f"Недостаточно средств. Нужно {stars_cost} ⭐",
            }, status=400)

    # Создаём заявку через хелпер, но с ручным списанием (нам нужно списать stars_cost, а в amount — то, что выдаём)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            INSERT INTO withdrawals
                (user_id, username, first_name, last_name, method, amount, wallet, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', datetime('now'))
        """, (
            user_id,
            user_obj.get("username"),
            user_obj.get("first_name"),
            user_obj.get("last_name"),
            method,
            amount,
            wallet,
        ))
        wd_id = cur.lastrowid
        await db.execute(
            "UPDATE users SET balance = MAX(0, balance - ?) WHERE user_id = ?",
            (stars_cost, user_id),
        )
        await db.commit()

    w = await get_withdrawal(wd_id)
    # отправляем админам
    await notify_admins_new_withdrawal(w)

    new_balance = await get_balance(user_id)
    return web.json_response({
        "ok": True,
        "withdrawal_id": wd_id,
        "balance": new_balance,
    })


async def my_withdrawals_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    user_id, _ = get_user_from_init_data(data.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "invalid_init_data"}, status=401)
    items = await list_user_withdrawals(user_id, 20)
    return web.json_response({"items": items})


async def withdraw_config_handler(request: web.Request):
    return web.json_response({
        "stars_min": WITHDRAWAL_STARS_MIN,
        "stars_max": WITHDRAWAL_STARS_MAX,
        "ton_min": WITHDRAWAL_TON_MIN,
        "ton_max": WITHDRAWAL_TON_MAX,
        "stars_per_ton": STARS_PER_TON,
    })


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def on_payment(msg: Message):
    sp = msg.successful_payment
    payload = sp.invoice_payload
    charge_id = sp.telegram_payment_charge_id
    try:
        _, user_id_str, stars_str = payload.split("_")
        user_id = int(user_id_str)
        stars = int(stars_str)
    except Exception:
        log.error(f"Bad payload: {payload}")
        stars = 0
        user_id = msg.from_user.id

    if stars > 0:
        new_balance = await add_balance(user_id, stars)
        log.info(f"✅ Оплата: user={user_id}, stars={stars}, charge={charge_id}, new_balance={new_balance}")
        await msg.answer(
            f"✅ <b>Оплата получена!</b>\n\n"
            f"Зачислено: <b>{stars} ⭐</b>\n"
            f"Твой баланс: <b>{new_balance} ⭐</b>\n\n"
            f"Спасибо за пополнение!"
        )


# ============================================================
# ============ TELETHON — СЛУШАТЕЛЬ ПОДАРКОВ =================
# ============================================================

tg_client = TelegramClient(
    StringSession(TG_SESSION_STRING),
    TG_API_ID,
    TG_API_HASH,
)

try:
    from TelegramGifts import TelegramGifts
    gifts_lib = TelegramGifts()
    log.info("✅ TelegramGifts загружен")
except Exception as e:
    gifts_lib = None
    log.warning(f"⚠️ TelegramGifts не загружен: {e}")


def extract_gift_from_message(msg):
    action = msg.action
    if action is None:
        return None, False
    if isinstance(action, tl_types.MessageActionStarGiftUnique):
        return getattr(action, "gift", None), True
    if isinstance(action, tl_types.MessageActionStarGift):
        gift = getattr(action, "gift", None)
        is_unique = bool(getattr(action, "upgrade_to_unique", False)) or \
                    bool(getattr(action, "saved", False))
        return gift, is_unique
    return None, False


async def get_gift_price_usd(gift) -> float:
    try:
        if hasattr(gift, "value_usd_amount") and gift.value_usd_amount:
            return gift.value_usd_amount / 100
        slug = getattr(gift, "slug", None)
        if slug:
            try:
                info = await tg_client(
                    functions.payments.GetUniqueStarGiftValueInfoRequest(slug=slug)
                )
                if getattr(info, "value", None):
                    return info.value / 100
                if getattr(info, "floor_price", None):
                    return info.floor_price * 0.015
            except Exception as e:
                log.warning(f"uniqueStarGiftValueInfo failed: {e}")
        title = getattr(gift, "title", None)
        if gifts_lib and title:
            try:
                info = gifts_lib.get_gift(title)
                ton_price = info.get("prices", {}).get("tgmrkt_price_ton", 0)
                if ton_price:
                    return ton_price * 5.0
            except Exception as e:
                log.warning(f"TelegramGifts failed: {e}")
        return 0.0
    except Exception as e:
        log.error(f"Price extraction error: {e}")
        return 0.0


@tg_client.on(events.NewMessage(incoming=True))
async def on_gift_message(event):
    if not event.is_private:
        return
    msg = event.message
    gift, is_unique = extract_gift_from_message(msg)
    if gift is None:
        return
    if not is_unique:
        log.info("   ⚠️ Обычный подарок — не принимаем")
        return
    sender = await event.get_sender()
    if sender is None:
        return

    title = getattr(gift, "title", "?")
    slug = getattr(gift, "slug", "?")
    log.info(f"🎁 NFT-подарок от {sender.id} (@{getattr(sender, 'username', '?')})")
    log.info(f"   Gift: {title} / slug={slug}")

    price_usd = await get_gift_price_usd(gift)
    log.info(f"   Price: ${price_usd:.2f}")

    if price_usd <= 0:
        log.warning("   ⚠️ Цена не определена")
        try:
            await bot.send_message(
                sender.id,
                "⚠️ <b>Подарок получен, но не удалось определить цену.</b>"
            )
        except Exception:
            pass
        return

    stars = int(price_usd * STARS_PER_USD)
    await upsert_user({
        "user_id": sender.id,
        "username": getattr(sender, "username", None),
        "first_name": getattr(sender, "first_name", "") or "",
        "last_name": getattr(sender, "last_name", "") or "",
    })
    new_balance = await add_balance(sender.id, stars)
    log.info(f"   ✅ Баланс {sender.id}: {new_balance} ⭐")

    try:
        await bot.send_message(
            sender.id,
            f"🎁 <b>NFT-подарок принят!</b>\n\n"
            f"Подарок: <b>{title}</b>\n"
            f"Оценка: <b>${price_usd:.2f}</b>\n"
            f"Зачислено: <b>+{stars} ⭐</b>\n"
            f"Твой баланс: <b>{new_balance} ⭐</b>"
        )
    except Exception as e:
        log.warning(f"Не удалось уведомить {sender.id}: {e}")


async def start_gift_listener():
    while True:
        try:
            await tg_client.start()
            me = await tg_client.get_me()
            log.info(f"🎧 Слушатель подарков: @{me.username} (id={me.id})")
            await tg_client.run_until_disconnected()
            log.warning("⚠️ Telethon отключился — переподключаюсь...")
        except Exception as e:
            log.error(f"❌ Telethon упал: {e}")
        await asyncio.sleep(10)


# ============================================================
# ====================== ЗАПУСК ==============================
# ============================================================

async def main():
    await init_db()
    log.info("База данных инициализирована.")

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=me.id)
        if member.status not in ("administrator", "creator"):
            log.warning("⚠️ Бот НЕ админ в канале %s", CHANNEL_ID)
        else:
            log.info("✅ Бот подтверждён как админ в канале %s", CHANNEL_ID)
    except Exception as e:
        log.error("Не удалось проверить статус бота: %s", e)

    asyncio.create_task(start_gift_listener())
    asyncio.create_task(crash_loop())

    app = web.Application()
    app.router.add_post("/api/create-invoice", create_invoice_handler)
    app.router.add_post("/api/get-balance", get_balance_handler)
    app.router.add_post("/api/get-stats", get_stats_handler)
    app.router.add_post("/api/record-game", record_game_handler)
    app.router.add_get("/api/online-counts", online_counts_handler)
    app.router.add_post("/api/redeem-promo", redeem_promo_handler)
    app.router.add_post("/api/admin/create-promo", admin_create_promo_handler)
    app.router.add_post("/api/admin/list-promos", admin_list_promos_handler)
    app.router.add_post("/api/admin/delete-promo", admin_delete_promo_handler)
    app.router.add_post("/api/admin/add-balance", admin_add_balance_handler)
    app.router.add_post("/api/crash-state", crash_state_handler)
    app.router.add_post("/api/crash-bet", crash_bet_handler)
    app.router.add_post("/api/crash-cashout", crash_cashout_handler)
    app.router.add_get("/api/cow-config", cow_config_handler)
    app.router.add_post("/api/cow-start", cow_start_handler)
    app.router.add_post("/api/cow-step", cow_step_handler)
    app.router.add_post("/api/cow-cashout", cow_cashout_handler)
    app.router.add_post("/api/mines-start", mines_start_handler)
    app.router.add_post("/api/mines-reveal", mines_reveal_handler)
    app.router.add_post("/api/mines-cashout", mines_cashout_handler)
    app.router.add_post("/api/upgrader-play", upgrader_play_handler)

    # === Выводы ===
    app.router.add_get("/api/withdraw-config", withdraw_config_handler)
    app.router.add_post("/api/withdraw-request", withdraw_request_handler)
    app.router.add_post("/api/my-withdrawals", my_withdrawals_handler)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True, expose_headers="*",
            allow_headers="*", allow_methods=["GET", "POST", "OPTIONS"],
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    log.info(f"API server started on {API_HOST}:{API_PORT}")

    log.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())