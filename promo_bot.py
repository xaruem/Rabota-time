#!/usr/bin/env python3
"""
promo_bot — три Telethon-аккаунта, плановые посты в группы + автоответ в личке.

Аккаунт 1 пишет тексты из "first", аккаунты 2 и 3 — из "second" (texts.json).
Группы: если groups.txt пуст — все группы, где состоят аккаунты (авто); иначе только из groups.txt.
Отчёт админу уходит ТОЛЬКО с одного аккаунта (REPORT_ACCOUNT), с общими данными.

Правила отправки (все настраиваются через переменные окружения, см. README.md):
  * у каждого аккаунта пауза между постами 10–30 минут (случайная);
  * между постами ЛЮБЫХ двух аккаунтов — глобальная пауза (по умолчанию 2.5–5 мин);
  * одна группа — не чаще раза в GROUP_COOLDOWN_HOURS (по умолчанию 24 ч), одним аккаунтом;
  * посты только в окне 9–22 по BOT_TZ, лимит постов на аккаунт в сутки;
  * тексты не повторяются, пока не пройден весь список.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("promo")
BASE = Path(__file__).resolve().parent
BACKUP_NAME = "promo_state.json"


# ───────────────────────────── настройки ─────────────────────────────

def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


@dataclass
class Cfg:
    tz: str = "Europe/Moscow"
    win_start: int = 9
    win_end: int = 22
    gap_min: float = 10.0          # минут, пауза между постами одного аккаунта
    gap_max: float = 30.0
    global_gap_sec: float = 150.0  # мин. пауза между постами любых аккаунтов (до x2 случайно)
    group_cooldown_h: float = 24.0
    daily_limit: int = 15
    report_every_h: float = 3.0
    report_account: int = 1
    admin: str = "@mannapov_ulugbek"
    greet_again_days: int = 7
    state_path: str = "state.json"
    groups_path: str = "groups.txt"
    texts_path: str = "texts.json"

    @classmethod
    def from_env(cls) -> "Cfg":
        return cls(
            tz=_env("BOT_TZ", cls.tz),
            win_start=int(_env("WINDOW_START_HOUR", str(cls.win_start))),
            win_end=int(_env("WINDOW_END_HOUR", str(cls.win_end))),
            gap_min=float(_env("POST_GAP_MIN_MINUTES", str(cls.gap_min))),
            gap_max=float(_env("POST_GAP_MAX_MINUTES", str(cls.gap_max))),
            global_gap_sec=float(_env("GLOBAL_GAP_SEC", str(cls.global_gap_sec))),
            group_cooldown_h=float(_env("GROUP_COOLDOWN_HOURS", str(cls.group_cooldown_h))),
            daily_limit=int(_env("DAILY_LIMIT_PER_ACCOUNT", str(cls.daily_limit))),
            report_every_h=float(_env("REPORT_EVERY_HOURS", str(cls.report_every_h))),
            report_account=int(_env("REPORT_ACCOUNT", str(cls.report_account))),
            admin=_env("ADMIN_USERNAME", cls.admin),
            greet_again_days=int(_env("GREET_AGAIN_DAYS", str(cls.greet_again_days))),
            state_path=_env("STATE_PATH", cls.state_path),
            groups_path=_env("GROUPS_FILE", cls.groups_path),
            texts_path=_env("TEXTS_FILE", cls.texts_path),
        )


# ───────────────────────────── группы ─────────────────────────────

@dataclass(frozen=True)
class GroupRef:
    key: str      # уникальный ключ: "@name", "+hash" или числовой id
    kind: str     # "user" | "invite" | "id"
    value: object
    rules: dict = field(default_factory=dict)   # свои правила группы (см. parse_rules)


def parse_windows(raw: str) -> list:
    out = []
    for part in re.split(r"[,;]", raw):
        m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", part)
        if m and 0 <= int(m.group(1)) <= 24 and 0 <= int(m.group(2)) <= 24:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def parse_rules(parts: list[str]) -> dict:
    """Опции группы: cooldown=6 (часов), window=9-12,18-22 (часы суток), acc=1,2 (аккаунты)."""
    rules: dict = {}
    for p in parts:
        if "=" not in p:
            continue
        k, v = (x.strip().lower() for x in p.split("=", 1))
        try:
            if k in ("cooldown", "every"):
                rules["cooldown"] = max(1.0, float(v.replace(",", ".").rstrip("h")))   # не чаще раза в час
            elif k in ("window", "windows"):
                w = parse_windows(v)
                if w:
                    rules["windows"] = w
            elif k in ("acc", "accounts"):
                a = {int(x) for x in re.split(r"[,;\s]+", v) if x}
                if a:
                    rules["acc"] = a
        except ValueError:
            log.warning("не понял опцию группы: %s", p)
    return rules


def hour_in_windows(hour: int, windows) -> bool:
    """Часы суток; окно 22-2 переходит через полночь."""
    for a, b in windows:
        if (a <= hour < b) if a < b else (hour >= a or hour < b):
            return True
    return False


def parse_group_ref(line: str) -> GroupRef | None:
    head, *opts = line.split("#", 1)[0].split("|")
    ref = _parse_ref(head)
    if ref and opts:
        ref = GroupRef(ref.key, ref.kind, ref.value, parse_rules(opts))
    return ref


def _parse_ref(line: str) -> GroupRef | None:
    s = line.strip()
    if not s:
        return None
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^(www\.)?(t\.me|telegram\.me|telegram\.dog)/", "", s, flags=re.I).strip().strip("/")
    s = re.sub(r"^s/", "", s)
    if not s:
        return None
    m = re.match(r"^(?:\+|joinchat/)([A-Za-z0-9_-]{8,})", s)
    if m:
        return GroupRef(f"+{m.group(1)}", "invite", m.group(1))
    m = re.match(r"^c/(\d+)", s)
    if m:
        return GroupRef(f"-100{m.group(1)}", "id", int(f"-100{m.group(1)}"))
    if re.fullmatch(r"-?\d{6,}", s):
        n = int(s)
        if n > 0:
            n = int(f"-100{n}")
        return GroupRef(str(n), "id", n)
    name = s.lstrip("@").split("/")[0].split("?")[0].lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{3,31}", name):
        return GroupRef(f"@{name}", "user", name)
    return None


def load_groups(path: str) -> list[GroupRef]:
    p = Path(path)
    if not p.is_absolute():
        p = BASE / p
    if not p.exists():
        return []
    seen, out = set(), []
    for line in p.read_text(encoding="utf-8").splitlines():
        ref = parse_group_ref(line)
        if ref and ref.key not in seen:
            seen.add(ref.key)
            out.append(ref)
    return out


# ───────────────────────────── время / состояние ─────────────────────────────

def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def pick_group(now: datetime, acc: int, members: dict, groups_state: dict,
               cooldown_h: float, rng: random.Random, busy: set = frozenset(),
               rules: dict | None = None):
    """Выбрать группу для аккаунта acc. members: {ключ_группы: {номера аккаунтов-участников}}.

    Приоритет: группы, куда может писать меньше всего аккаунтов, затем самые «давние».
    Группа с постом младше cooldown_h (от ЛЮБОГО аккаунта) пропускается.
    """
    floor = datetime.min.replace(tzinfo=now.tzinfo)
    cands = []
    for key, accs in members.items():
        if acc not in accs or key in busy:
            continue
        r = (rules or {}).get(key, {})
        if r.get("acc") and acc not in r["acc"]:
            continue
        if r.get("windows") and not hour_in_windows(now.hour, r["windows"]):
            continue
        g = groups_state.get(key, {})
        off = g.get("off", {})
        if str(acc) in off:
            continue
        last = parse_iso(g.get("last_post"))
        if last and now - last < timedelta(hours=r.get("cooldown", cooldown_h)):
            continue
        slow = parse_iso(g.get("slow_until"))
        if slow and now < slow:
            continue
        active = [a for a in accs if str(a) not in off and (not r.get("acc") or a in r["acc"])]
        cands.append((len(active), last or floor, rng.random(), key))
    if not cands:
        return None
    cands.sort()
    return cands[0][3]


def default_state() -> dict:
    return {"groups": {}, "accounts": {}, "greeted": {}, "events": [], "bags": {},
            "global_until": None, "paused": False, "report_since": None,
            "last_report": None, "backup_msg_id": None}


# ───────────────────────────── сам бот ─────────────────────────────

class Bot:
    def __init__(self, cfg: Cfg, texts: dict, groups: list[GroupRef], clients: dict,
                 state: dict | None = None, rng: random.Random | None = None):
        self.cfg = cfg
        self.texts = texts
        self.groups = groups
        self.clients = clients            # {номер: TelegramClient}
        self.state = state or default_state()
        for k, v in default_state().items():
            self.state.setdefault(k, v)
        self.rng = rng or random.Random()
        self.tz = ZoneInfo(cfg.tz)
        self.rules = {g.key: g.rules for g in groups if g.rules}
        self.members: dict[str, set] = {}      # {ключ_группы: {аккаунты}}
        self.chat_ids: dict[int, dict] = {}    # {аккаунт: {ключ_группы: peer_id}}
        self.dead: dict[int, str] = {}
        self.inflight: set = set()
        self.dirty = False
        self.version = 0                       # растёт при любом изменении (для бэкапа)
        self.alerts: list[str] = []            # срочные сообщения админу
        self.stop = False
        self.auto_groups = not groups           # groups.txt пуст → берём группы из аккаунтов
        self.titles: dict[str, str] = {}
        self._admin_ids: set = set()
        self._now = None                       # для тестов

    # --- время
    def now(self) -> datetime:
        return self._now() if self._now else datetime.now(self.tz)

    def today(self, now=None) -> str:
        return (now or self.now()).strftime("%Y-%m-%d")

    # --- состояние
    def touch(self):
        self.dirty = True
        self.version += 1

    def save(self):
        path = Path(self.cfg.state_path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        self.dirty = False

    def acc_state(self, acc: int) -> dict:
        a = self.state["accounts"].setdefault(str(acc), {})
        today = self.today()
        if a.get("day") != today:
            a["day"], a["sent_today"] = today, 0
        return a

    def group_state(self, key: str) -> dict:
        return self.state["groups"].setdefault(key, {})

    def event(self, kind: str, acc: int | None = None, group: str | None = None, detail: str = ""):
        ev = {"ts": iso(self.now()), "kind": kind, "acc": acc, "group": group, "detail": detail}
        evs = self.state["events"]
        evs.append(ev)
        if len(evs) > 1500:
            del evs[:-1500]
        if kind in ("acc_pause", "acc_dead"):
            self.alerts.append(f"⚠️ Акк.{acc}: {detail}" + (f" (группа {group})" if group else ""))
        self.touch()

    # --- тексты
    def pool_for(self, acc: int) -> str:
        return "first" if acc == 1 else "second"

    def peek_text(self, acc: int, key: str):
        pool = self.pool_for(acc)
        items = self.texts.get(pool) or []
        if not items:
            return None
        bag = self.state["bags"].setdefault(pool, [])
        bag[:] = [i for i in bag if i < len(items)]
        if not bag:
            bag.extend(range(len(items)))
            self.rng.shuffle(bag)
        last_idx = self.group_state(key).get("last_text")
        for i in bag:
            if i != last_idx or len(bag) == 1:
                return pool, i, items[i]
        return pool, bag[0], items[bag[0]]

    def commit_text(self, pool: str, idx: int):
        bag = self.state["bags"].setdefault(pool, [])
        if idx in bag:
            bag.remove(idx)

    # --- ограничения
    def next_window_start(self, now: datetime) -> datetime:
        start = now.replace(hour=self.cfg.win_start, minute=0, second=0, microsecond=0)
        return start if now < start else start + timedelta(days=1)

    def in_window(self, now: datetime) -> bool:
        return self.cfg.win_start <= now.hour < self.cfg.win_end

    def ready_in(self, acc: int, now: datetime) -> float:
        """Через сколько секунд аккаунт может отправлять (0 = уже можно)."""
        a = self.acc_state(acc)
        times = []
        if not self.in_window(now):
            times.append(self.next_window_start(now) + timedelta(minutes=acc * 7))
        elif a.get("sent_today", 0) >= self.cfg.daily_limit:
            times.append(self.next_window_start(now) + timedelta(minutes=acc * 7))
        for k in ("paused_until", "next_at"):
            t = parse_iso(a.get(k))
            if t:
                times.append(t)
        t = parse_iso(self.state.get("global_until"))
        if t:
            times.append(t)
        if not times:
            return 0
        return max(0.0, (max(times) - now).total_seconds())

    # --- один шаг цикла аккаунта (вынесен для тестов на виртуальном времени)
    async def step(self, acc: int) -> float | None:
        if acc in self.dead:
            return None
        now = self.now()
        if self.state.get("paused"):
            return 60
        a = self.acc_state(acc)
        if not a.get("next_at"):   # первый запуск: разнести аккаунты по времени
            a["next_at"] = iso(now + timedelta(minutes=self.rng.uniform(1, 8) * acc))
            self.touch()
        wait = self.ready_in(acc, now)
        if wait > 0:
            return min(wait, 300) + self.rng.uniform(1, 15)
        key = pick_group(now, acc, self.members, self.state["groups"],
                         self.cfg.group_cooldown_h, self.rng, self.inflight, self.rules)
        if key is None:
            return self.rng.uniform(300, 600)
        # резервируем глобальную паузу и группу ДО отправки
        gap = self.cfg.global_gap_sec
        self.state["global_until"] = iso(now + timedelta(seconds=self.rng.uniform(gap, gap * 2)))
        self.inflight.add(key)
        try:
            await self.send_one(acc, key)
        finally:
            self.inflight.discard(key)
        return 1

    async def account_loop(self, acc: int):
        while not self.stop:
            delay = await self.step(acc)
            if delay is None:
                return
            await asyncio.sleep(delay)

    # --- отправка
    async def send_one(self, acc: int, key: str):
        from telethon import errors as E
        now = self.now()
        a = self.acc_state(acc)
        g = self.group_state(key)
        picked = self.peek_text(acc, key)
        if not picked:
            a["next_at"] = iso(now + timedelta(hours=1))
            return
        pool, idx, text = picked
        chat = self.chat_ids[acc][key]

        def short_retry():
            a["next_at"] = iso(self.now() + timedelta(minutes=self.rng.uniform(2, 5)))

        try:
            await self.clients[acc].send_message(chat, text, link_preview=False)
        except E.FloodWaitError as e:
            secs = int(getattr(e, "seconds", 60))
            a["paused_until"] = iso(now + timedelta(seconds=secs + 30))
            self.event("flood", acc, key, f"FloodWait {secs}s")
        except E.SlowModeWaitError as e:
            secs = int(getattr(e, "seconds", 600))
            g["slow_until"] = iso(now + timedelta(seconds=secs + 30))
            self.event("slow", acc, key, f"SlowMode {secs}s")
            short_retry()
        except E.PeerFloodError:
            a["paused_until"] = iso(now + timedelta(hours=24))
            self.event("acc_pause", acc, key, "PeerFlood: аккаунт на паузе 24 ч")
        except (E.AuthKeyUnregisteredError, E.UserDeactivatedError,
                E.UserDeactivatedBanError, E.SessionRevokedError) as e:
            self.dead[acc] = type(e).__name__
            self.event("acc_dead", acc, None, type(e).__name__)
        except (E.ChatWriteForbiddenError, E.UserBannedInChannelError, E.ChatAdminRequiredError,
                E.ChannelPrivateError, E.UserNotParticipantError, E.ChatRestrictedError) as e:
            g.setdefault("off", {})[str(acc)] = type(e).__name__
            self.event("group_off", acc, key, type(e).__name__)
            short_retry()
        except Exception as e:  # noqa: BLE001
            g["fails"] = g.get("fails", 0) + 1
            self.event("post_err", acc, key, f"{type(e).__name__}: {e}"[:160])
            if g["fails"] >= 3:
                g.setdefault("off", {})[str(acc)] = f"3 ошибки подряд: {type(e).__name__}"
                self.event("group_off", acc, key, f"3 ошибки подряд: {type(e).__name__}")
            short_retry()
        else:
            g.update(last_post=iso(now), last_text=idx, fails=0, posts=g.get("posts", 0) + 1,
                     last_acc=acc)
            a["sent_today"] = a.get("sent_today", 0) + 1
            a["next_at"] = iso(now + timedelta(minutes=self.rng.uniform(self.cfg.gap_min, self.cfg.gap_max)))
            self.commit_text(pool, idx)
            self.event("post_ok", acc, key)
        self.touch()

    # ───────── членство в группах (без ResolveUsername и без вступления) ─────────

    @staticmethod
    def _can_write(ent) -> bool:
        """Группа, куда аккаунт реально может писать (не канал, не вышел, не кикнут, нет запрета)."""
        if getattr(ent, "broadcast", False) or getattr(ent, "left", False) or getattr(ent, "kicked", False):
            return False
        if getattr(ent, "deactivated", False):
            return False
        if getattr(ent, "creator", False) or getattr(ent, "admin_rights", None):
            return True
        rights = getattr(ent, "default_banned_rights", None)
        return not (rights and getattr(rights, "send_messages", False))

    async def refresh_members(self):
        from telethon import functions, types, utils
        scans = {}          # {акк: (by_user, by_id)}
        found = {}          # авто-режим: {ключ: (pid, название)}
        failed = set()
        for acc, client in self.clients.items():
            if acc in self.dead:
                continue
            by_user, by_id = {}, {}
            try:
                async for d in client.iter_dialogs():
                    ent = d.entity
                    if not (d.is_group or getattr(ent, "megagroup", False)):
                        continue
                    pid = utils.get_peer_id(ent)
                    by_id[pid] = pid
                    names = [getattr(ent, "username", None)]
                    names += [u.username for u in (getattr(ent, "usernames", None) or []) if u.active]
                    for n in names:
                        if n:
                            by_user[n.lower()] = pid
                    if self.auto_groups and self._can_write(ent):
                        found.setdefault(str(pid), (pid, getattr(ent, "title", None) or str(pid)))
                scans[acc] = (by_user, by_id)
            except Exception as e:  # noqa: BLE001
                log.warning("acc%s: не удалось получить диалоги: %s", acc, e)
                failed.add(acc)

        if self.auto_groups:
            # Список групп = все группы, где состоят аккаунты (groups.txt не нужен)
            self.titles = {k: t for k, (_, t) in found.items()}
            self.groups = [GroupRef(k, "id", pid) for k, (pid, _) in found.items()]
            for acc in failed:      # прежнее членство не теряем, если Telegram не ответил
                for k in self.chat_ids.get(acc, {}):
                    if k not in found:
                        self.groups.append(GroupRef(k, "id", int(k)))

        members: dict[str, set] = {r.key: set() for r in self.groups}
        for acc in failed:
            for k in self.chat_ids.get(acc, {}):
                members.setdefault(k, set()).add(acc)   # оставляем прежнее членство
        for acc, client in self.clients.items():
            if acc not in scans:
                continue
            by_user, by_id = scans[acc]
            ids, seen_pid = {}, {}
            for ref in self.groups:
                pid = None
                if ref.kind == "user":
                    pid = by_user.get(ref.value)
                elif ref.kind == "id":
                    pid = by_id.get(ref.value)
                elif ref.kind == "invite":
                    try:
                        r = await client(functions.messages.CheckChatInviteRequest(ref.value))
                        if isinstance(r, types.ChatInviteAlready):
                            pid = utils.get_peer_id(r.chat)
                        await asyncio.sleep(1.0)
                    except Exception as e:  # noqa: BLE001
                        log.info("acc%s: инвайт %s не проверен: %s", acc, ref.key, e)
                if pid is not None and pid in seen_pid:
                    log.warning("acc%s: %s и %s — одна и та же группа, вторая пропущена", acc, seen_pid[pid], ref.key)
                    pid = None
                if pid is not None:
                    seen_pid[pid] = ref.key
                    ids[ref.key] = pid
                    members[ref.key].add(acc)
            self.chat_ids[acc] = ids
        self.members = members

    def label(self, key: str) -> str:
        t = self.titles.get(key)
        return f"{t} ({key})" if t else key

    # ───────── личные сообщения ─────────

    def is_admin(self, sender) -> bool:
        uname = (getattr(sender, "username", None) or "").lower()
        return uname == self.cfg.admin.lstrip("@").lower() or getattr(sender, "id", None) in self._admin_ids

    def should_greet(self, acc: int, uid: int, now: datetime) -> bool:
        last = parse_iso(self.state["greeted"].get(f"{acc}:{uid}"))
        return not last or now - last > timedelta(days=self.cfg.greet_again_days)

    def register_handlers(self, acc: int, client):
        from telethon import events

        @client.on(events.NewMessage(incoming=True))
        async def on_message(event):
            if not event.is_private:
                return
            try:
                sender = await event.get_sender()
                if sender is None or getattr(sender, "bot", False) or sender.id == 777000:
                    return
                if self.is_admin(sender):
                    await self.admin_command(event, (event.raw_text or "").strip())
                    return
                now = self.now()
                if not self.should_greet(acc, sender.id, now):
                    return
                self.state["greeted"][f"{acc}:{sender.id}"] = iso(now)   # сразу, чтобы не ответить дважды
                self.touch()
                greetings = self.texts.get("dm_greeting") or []
                if not greetings:
                    return
                await asyncio.sleep(self.rng.uniform(4, 12))
                async with client.action(sender, "typing"):
                    await asyncio.sleep(self.rng.uniform(1.5, 4))
                await client.send_message(sender, self.rng.choice(greetings), link_preview=False)
                self.event("dm", acc)
            except Exception as e:  # noqa: BLE001
                log.warning("acc%s: ошибка автоответа: %s", acc, e)

    async def admin_command(self, event, text: str):
        cmd = text.split()[0].lower() if text else ""
        if cmd == "/status":
            await event.respond(self.report_text(since=self.now() - timedelta(hours=24), title="📊 Статус"))
        elif cmd == "/pause":
            self.state["paused"] = True
            self.touch()
            await event.respond("⏸ Посты приостановлены (автоответы работают). /resume — продолжить.")
        elif cmd == "/resume":
            self.state["paused"] = False
            self.touch()
            await event.respond("▶️ Посты продолжены.")
        elif cmd == "/groups":
            await event.respond(self.groups_text())
        elif cmd == "/open":
            n = 0
            for g in self.state["groups"].values():
                if g.get("off"):
                    g["off"] = {}
                    n += 1
            self.touch()
            await event.respond(f"Снято отключение с групп: {n}")
        elif cmd in ("/help", "/start"):
            await event.respond("/status — отчёт сейчас\n/groups — список групп\n/pause, /resume — пауза постов\n/open — вернуть отключённые группы")

    # ───────── отчёты ─────────

    def groups_text(self) -> str:
        rows = []
        for ref in self.groups:
            g = self.state["groups"].get(ref.key, {})
            last = parse_iso(g.get("last_post"))
            when = last.astimezone(self.tz).strftime("%d.%m %H:%M") if last else "—"
            accs = ",".join(str(a) for a in sorted(self.members.get(ref.key, []))) or "нет аккаунтов"
            off = " ⛔" if g.get("off") and len(g["off"]) >= len(self.members.get(ref.key, [])) else ""
            r = ref.rules
            extra = ""
            if r:
                extra = " · " + ", ".join(filter(None, [
                    f"каждые {r['cooldown']:g} ч" if "cooldown" in r else "",
                    "окна " + ",".join(f"{a}-{b}" for a, b in r["windows"]) if "windows" in r else "",
                    "только акк " + ",".join(map(str, sorted(r["acc"]))) if "acc" in r else ""]))
            rows.append(f"{self.label(ref.key)} · акк: {accs} · пост: {when}{off}{extra}")
        return "\n".join(rows)[:3900] or "Групп нет"

    def report_text(self, since: datetime | None, title: str = "📊 Отчёт") -> str:
        now = self.now()
        evs = [e for e in self.state["events"] if not since or (parse_iso(e["ts"]) and parse_iso(e["ts"]) >= since)]
        ok = [e for e in evs if e["kind"] == "post_ok"]
        per = {a: sum(1 for e in ok if e["acc"] == a) for a in sorted(self.clients)}
        errs = [e for e in evs if e["kind"] in ("post_err", "flood", "slow")]
        dms = sum(1 for e in evs if e["kind"] == "dm")
        day_ago = now - timedelta(hours=24)
        reached = sum(1 for k in self.members
                      if (parse_iso(self.state["groups"].get(k, {}).get("last_post")) or day_ago) > day_ago)
        usable = [k for k, m in self.members.items()
                  if any(str(a) not in self.state["groups"].get(k, {}).get("off", {}) for a in m)]
        sent_today = {a: self.acc_state(a).get("sent_today", 0) for a in sorted(self.clients)}
        lines = [f"{title} · {now.strftime('%d.%m %H:%M')}"]
        span = f" (за {round((now - since).total_seconds() / 3600, 1)} ч)" if since else ""
        lines.append(f"Постов{span}: {len(ok)} — " + " · ".join(f"акк.{a}: {n}" for a, n in per.items()))
        lines.append(f"Сегодня всего: {sum(sent_today.values())} (лимит {self.cfg.daily_limit}/акк)")
        lines.append(f"Охвачено групп за 24 ч: {reached} из {len(usable)} доступных (всего в списке {len(self.groups)})")
        lines.append(f"Автоответов в лс: {dms}")
        if errs:
            kinds: dict = {}
            for e in errs:
                kinds[e["detail"].split(":")[0][:40]] = kinds.get(e["detail"].split(":")[0][:40], 0) + 1
            lines.append("Ошибок: " + str(len(errs)) + " (" + ", ".join(f"{k} ×{v}" for k, v in kinds.items()) + ")")
        offs = [e for e in evs if e["kind"] == "group_off"]
        if offs:
            lines.append("⛔ Отключены группы: " + ", ".join(sorted({f"{e['group']} ({e['detail']})" for e in offs}))[:600])
        for acc in sorted(self.clients):
            if acc in self.dead:
                lines.append(f"🔴 Акк.{acc} мёртв: {self.dead[acc]}")
                continue
            pu = parse_iso(self.acc_state(acc).get("paused_until"))
            if pu and pu > now:
                lines.append(f"⏸ Акк.{acc} на паузе до {pu.astimezone(self.tz).strftime('%d.%m %H:%M')}")
        if self.state.get("paused"):
            lines.append("⏸ Все посты на паузе (/resume)")
        nxt = [parse_iso(self.acc_state(a).get("next_at")) for a in self.clients if a not in self.dead]
        nxt = [t for t in nxt if t]
        if nxt and not self.state.get("paused"):
            lines.append("Ближайший пост: ~" + max(now, min(nxt)).astimezone(self.tz).strftime("%H:%M"))
        return "\n".join(lines)

    def startup_text(self) -> str:
        no_acc = [k for k, m in self.members.items() if not m]
        lines = [f"🚀 Бот запущен · аккаунтов: {len(self.clients) - len(self.dead)}/{len(self.clients)} · "
                 f"групп в списке: {len(self.groups)}, доступных: {len(self.groups) - len(no_acc)}"]
        for acc in sorted(self.clients):
            n = sum(1 for m in self.members.values() if acc in m)
            lines.append(f"Акк.{acc}: состоит в {n} группах" + ("" if self.auto_groups else " из списка") + (f" · ❌ {self.dead[acc]}" if acc in self.dead else ""))
        if no_acc:
            lines.append("Ни один аккаунт не состоит в: " + ", ".join(self.label(k) for k in no_acc[:25]) + (" …" if len(no_acc) > 25 else ""))
        return "\n".join(lines)

    async def send_report(self, text: str):
        order = [self.cfg.report_account] + [a for a in sorted(self.clients) if a != self.cfg.report_account]
        for acc in order:
            if acc in self.dead or acc not in self.clients:
                continue
            try:
                await self.clients[acc].send_message(self.cfg.admin, text)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("отчёт через акк.%s не ушёл: %s", acc, e)
        return False

    async def report_loop(self):
        while not self.stop:
            await asyncio.sleep(60)
            if self.alerts:
                text, self.alerts = "\n".join(self.alerts), []
                await self.send_report(text)
            now = self.now()
            last = parse_iso(self.state.get("last_report"))
            if last and now - last < timedelta(hours=self.cfg.report_every_h):
                continue
            if not (self.cfg.win_start <= now.hour < self.cfg.win_end + 1):
                continue
            since = parse_iso(self.state.get("report_since")) or last or (now - timedelta(hours=self.cfg.report_every_h))
            if await self.send_report(self.report_text(since)):
                self.state["last_report"] = iso(now)
                self.state["report_since"] = iso(now)
                self.touch()

    # ───────── бэкап состояния в «Избранное» (переживает редеплой без Volume) ─────────

    async def restore_backup(self) -> dict | None:
        for acc in [self.cfg.report_account] + sorted(self.clients):
            c = self.clients.get(acc)
            if not c or acc in self.dead:
                continue
            try:
                async for m in c.iter_messages("me", limit=50):
                    if m.file and m.file.name == BACKUP_NAME:
                        raw = await m.download_media(file=bytes)
                        return json.loads(raw.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("restore: акк.%s: %s", acc, e)
        return None

    async def backup_loop(self):
        acc = self.cfg.report_account
        last_version = -1
        while not self.stop:
            await asyncio.sleep(900)
            if acc in self.dead or self.version == last_version:
                continue
            version = self.version
            try:
                c = self.clients[acc]
                buf = io.BytesIO(json.dumps(self.state, ensure_ascii=False).encode("utf-8"))
                buf.name = BACKUP_NAME
                msg = await c.send_file("me", buf, force_document=True, caption="backup")
                old = self.state.get("backup_msg_id")
                self.state["backup_msg_id"] = msg.id
                if old:
                    await c.delete_messages("me", [old])
                last_version = version
            except Exception as e:  # noqa: BLE001
                log.warning("backup не удался: %s", e)

    async def flush_loop(self):
        while not self.stop:
            await asyncio.sleep(10)
            if self.dirty:
                try:
                    self.save()
                except OSError as e:
                    log.warning("не удалось сохранить состояние: %s", e)

    async def members_loop(self):
        while not self.stop:
            await asyncio.sleep(6 * 3600)
            try:
                await self.refresh_members()
            except Exception as e:  # noqa: BLE001
                log.warning("refresh_members: %s", e)


# ───────────────────────────── запуск ─────────────────────────────

def load_texts(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = BASE / p
    data = json.loads(p.read_text(encoding="utf-8"))
    for k in ("first", "second", "dm_greeting"):
        if not data.get(k):
            raise SystemExit(f"texts.json: пустой список '{k}'")
    return data


def load_sessions() -> dict:
    out = {}
    for i in range(1, 10):
        s = os.getenv(f"SESSION_STRING_{i}", "").strip()
        if s:
            out[i] = s
    return out


async def supervised(name: str, factory, retry: float = 30):
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("%s упал, перезапуск через %ss", name, retry)
            await asyncio.sleep(retry)


async def main():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Cfg.from_env()
    texts = load_texts(cfg.texts_path)
    groups = load_groups(cfg.groups_path)
    sessions = load_sessions()
    if not sessions:
        raise SystemExit("Нет SESSION_STRING_1..3")
    if not groups:
        log.info("groups.txt пуст — беру все группы, где состоят аккаунты")
    api_id, api_hash = int(os.environ["API_ID"]), os.environ["API_HASH"]

    clients, dead = {}, {}
    for i, sess in sessions.items():
        try:
            c = TelegramClient(StringSession(sess), api_id, api_hash)
        except Exception as e:  # noqa: BLE001  (битая строка сессии)
            log.error("акк.%s: неверная SESSION_STRING: %s", i, e)
            continue
        clients[i] = c
        try:
            await c.connect()
            if not await c.is_user_authorized():
                dead[i] = "сессия не авторизована"
        except Exception as e:  # noqa: BLE001
            log.error("акк.%s: не удалось подключиться: %s", i, e)
            dead[i] = f"подключение: {type(e).__name__}"
    if not clients:
        raise SystemExit("Ни одна SESSION_STRING не подошла")

    state = None
    sp = Path(cfg.state_path)
    if sp.exists():
        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None
    bot = Bot(cfg, texts, groups, clients, state)
    bot.dead.update(dead)
    if state is None:
        restored = await bot.restore_backup()
        if restored:
            bot.state = restored
            for k, v in default_state().items():
                bot.state.setdefault(k, v)
            log.info("состояние восстановлено из «Избранного»")

    for acc, c in clients.items():
        if acc not in bot.dead:
            bot.register_handlers(acc, c)
    await bot.refresh_members()
    await bot.send_report(bot.startup_text())
    bot.state["report_since"] = bot.state["last_report"] = iso(bot.now())
    bot.touch()

    tasks = [supervised(f"acc{a}", lambda a=a: bot.account_loop(a)) for a in clients if a not in bot.dead]
    tasks += [supervised(n, f) for n, f in (("report", bot.report_loop), ("backup", bot.backup_loop),
                                            ("flush", bot.flush_loop), ("members", bot.members_loop))]
    tasks += [supervised(f"tg{a}", lambda c=c: c.run_until_disconnected(), 15) for a, c in clients.items() if a not in bot.dead]
    try:
        await asyncio.gather(*tasks)
    finally:
        bot.stop = True
        bot.save()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
