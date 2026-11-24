import asyncio, heapq, json, random, sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from telethon import errors as E
import promo_bot as P

TZ = ZoneInfo("Europe/Moscow")
TEXTS = json.loads((Path(__file__).resolve().parent.parent / "texts.json").read_text(encoding="utf-8"))


class FakeMsg:
    def __init__(self, id, text, date):
        self.id, self.message, self.out, self.date = id, text, True, date


class FakeClient:
    """fail — исключение (или список исключений по очереди); lost — «сообщение сразу удалили»."""
    def __init__(self, fail=None, lost=False):
        self.sent, self.fail, self.lost, self.store, self.n = [], fail, lost, {}, 0
        self.connected = True

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def send_message(self, chat, text, **kw):
        f = self.fail.pop(0) if isinstance(self.fail, list) and self.fail else (None if isinstance(self.fail, list) else self.fail)
        if f:
            raise f
        self.n += 1
        self.sent.append((chat, text))
        m = FakeMsg(self.n, text, datetime.now(TZ))
        if not self.lost:
            self.store[(chat, m.id)] = m
        return m

    async def get_messages(self, chat, ids=None):
        return self.store.get((chat, ids))

    async def iter_messages(self, chat, limit=10):
        for (c, _i), m in list(self.store.items()):
            if c == chat:
                yield m


def make_bot(n_groups=40, accs=(1, 2, 3), fail=None, tmp=None, lost=False, **cfg):
    groups = [P.parse_group_ref(f"@group_{i:03d}") for i in range(n_groups)]
    clients = {a: FakeClient(fail, lost) for a in accs}
    bot = P.Bot(P.Cfg(state_path=str(tmp or "/tmp/_st.json"), **cfg), TEXTS, groups, clients, rng=random.Random(7))

    async def no_sleep(_s):
        return None
    bot.sleep = no_sleep
    bot.members = {g.key: set(accs) for g in groups}
    bot.chat_ids = {a: {g.key: i for i, g in enumerate(groups)} for a in accs}
    return bot


def test_parse_group_ref():
    f = P.parse_group_ref
    assert f("https://t.me/Some_Group").key == "@some_group"
    assert f("t.me/some_group/123").key == "@some_group"
    assert f("@some_group  # коммент").key == "@some_group"
    assert f("https://t.me/+AbCdEfGh123").kind == "invite"
    assert f("https://t.me/joinchat/AbCdEfGh123").kind == "invite"
    assert f("https://t.me/c/1775719590/5").value == -1001775719590
    assert f("-1001775719590").value == -1001775719590
    assert f("# только коммент") is None and f("") is None and f("ab") is None


def test_texts_ok():
    assert len(TEXTS["first"]) == 20 and len(TEXTS["second"]) == 20
    assert len(set(TEXTS["first"])) == 20 and len(set(TEXTS["second"])) == 20
    assert all("xaruem.github.io/Partner" in t for t in TEXTS["dm_greeting"])


def test_text_bag_no_repeat_until_exhausted():
    bot = make_bot()
    seen = []
    for _ in range(20):
        pool, i, _t = bot.peek_text(2, "@group_000")
        bot.commit_text(pool, i)
        seen.append(i)
    assert sorted(seen) == list(range(20))


def test_text_not_same_as_last_in_group():
    bot = make_bot()
    bot.group_state("@g")["last_text"] = 5
    for _ in range(50):
        bot.state["bags"] = {"second": [5, 1, 2]}
        assert bot.peek_text(2, "@g")[1] != 5


def test_pick_group_respects_cooldown_and_off():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=TZ)
    members = {"a": {1, 2}, "b": {1}, "c": {1}}
    gs = {"a": {"last_post": P.iso(now - timedelta(hours=2))},
          "b": {"off": {"1": "x"}},
          "c": {"slow_until": P.iso(now + timedelta(minutes=5))}}
    assert P.pick_group(now, 1, members, gs, 24, random.Random(1)) is None
    gs["a"]["last_post"] = P.iso(now - timedelta(hours=25))
    assert P.pick_group(now, 1, members, gs, 24, random.Random(1)) == "a"
    assert P.pick_group(now, 1, members, gs, 24, random.Random(1), busy={"a"}) is None


def test_window():
    bot = make_bot()
    assert bot.in_window(datetime(2026, 11, 17, 9, 0, tzinfo=TZ))
    assert not bot.in_window(datetime(2026, 11, 17, 22, 0, tzinfo=TZ))
    assert not bot.in_window(datetime(2026, 11, 17, 3, 0, tzinfo=TZ))
    assert bot.next_window_start(datetime(2026, 11, 17, 23, 0, tzinfo=TZ)).day == 18


def simulate(bot, days=3, start=None):
    """Виртуальное время: крутим step() всех аккаунтов, как это делает account_loop."""
    t = {"now": start or datetime(2026, 11, 17, 8, 0, tzinfo=TZ)}
    bot._now = lambda: t["now"]
    heap = [(t["now"] + timedelta(seconds=a), a) for a in bot.clients]
    heapq.heapify(heap)
    end = t["now"] + timedelta(days=days)
    while heap:
        when, acc = heapq.heappop(heap)
        if when > end:
            break
        t["now"] = when
        delay = asyncio.run(bot.step(acc))
        if delay is not None:
            heapq.heappush(heap, (when + timedelta(seconds=delay), acc))


def posts(bot):
    return [e for e in bot.state["events"] if e["kind"] == "post_ok"]


def test_simulation_never_bursts(tmp_path):
    bot = make_bot(tmp=tmp_path / "s.json")
    simulate(bot, days=3)
    ps = posts(bot)
    assert len(ps) > 60
    times = [P.parse_iso(e["ts"]) for e in ps]
    assert times == sorted(times)
    # 1) пауза между любыми двумя постами >= глобальной
    assert min(b - a for a, b in zip(times, times[1:])) >= timedelta(seconds=bot.cfg.global_gap_sec - 1)
    # 2) пауза одного аккаунта >= 10 минут и <= ~30 (+ ожидание окна/опроса)
    for acc in (1, 2, 3):
        ts = [P.parse_iso(e["ts"]) for e in ps if e["acc"] == acc]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        assert min(gaps) >= timedelta(minutes=10)
    # 3) не больше 3 постов в любом 10-минутном окне (три аккаунта, не «залп»)
    for i, a in enumerate(times):
        assert sum(1 for b in times[i:] if b - a < timedelta(minutes=10)) <= 3
    # 4) только в окне 9-22
    assert all(9 <= t.hour < 22 for t in times)
    # 5) лимит в сутки на аккаунт
    for acc in (1, 2, 3):
        per_day = {}
        for e in ps:
            if e["acc"] == acc:
                d = e["ts"][:10]
                per_day[d] = per_day.get(d, 0) + 1
        assert max(per_day.values()) <= bot.cfg.daily_limit
    # 6) одна группа не чаще раза в 24 часа
    by_group = {}
    for e in ps:
        by_group.setdefault(e["group"], []).append(P.parse_iso(e["ts"]))
    for ts in by_group.values():
        assert all(b - a >= timedelta(hours=24) for a, b in zip(ts, ts[1:]))
    # 7) аккаунт 1 и 2/3 берут тексты из своих пулов
    for acc, cl in bot.clients.items():
        pool = TEXTS["first"] if acc == 1 else TEXTS["second"]
        assert all(t in pool for _c, t in cl.sent)


def test_simulation_does_not_post_all_groups_in_one_day(tmp_path):
    bot = make_bot(tmp=tmp_path / "s.json")
    simulate(bot, days=1, start=datetime(2026, 11, 17, 8, 0, tzinfo=TZ))
    assert len(posts(bot)) <= 3 * bot.cfg.daily_limit
    # 3 аккаунта × паузы 10–30 мин → не больше ~12 постов в час
    first_hour = [e for e in posts(bot) if P.parse_iso(e["ts"]) < datetime(2026, 11, 17, 10, 0, tzinfo=TZ)]
    assert len(first_hour) <= 12


@pytest.mark.parametrize("exc,check", [
    (E.PeerFloodError(request=None), lambda b: b.acc_state(1).get("paused_until")),
    (E.ChatWriteForbiddenError(request=None), lambda b: b.state["groups"]["@group_000"]["off"]["1"]),
    (E.SlowModeWaitError(request=None, capture=600), lambda b: b.state["groups"]["@group_000"]["slow_until"]),
    (E.FloodWaitError(request=None, capture=120), lambda b: b.acc_state(1).get("paused_until")),
    (E.AuthKeyUnregisteredError(request=None), lambda b: 1 in b.dead),
])
def test_error_handling(exc, check, tmp_path):
    bot = make_bot(accs=(1,), fail=exc, tmp=tmp_path / "s.json")
    asyncio.run(bot.send_one(1, "@group_000"))
    assert check(bot)
    assert not posts(bot)


def test_three_unknown_errors_disable_group(tmp_path):
    bot = make_bot(accs=(1,), fail=RuntimeError("boom"), tmp=tmp_path / "s.json")
    for _ in range(3):
        asyncio.run(bot.send_one(1, "@group_000"))
    assert "1" in bot.state["groups"]["@group_000"]["off"]


def test_report_and_state_roundtrip(tmp_path):
    bot = make_bot(tmp=tmp_path / "s.json")
    simulate(bot, days=1)
    txt = bot.report_text(since=None)
    assert "Постов" in txt and "акк.1" in txt and "акк.3" in txt
    bot.save()
    again = json.loads((tmp_path / "s.json").read_text())
    assert again["groups"] == bot.state["groups"]
    assert "@group_000" in bot.groups_text()


def test_greeting_once_per_user(tmp_path):
    bot = make_bot(tmp=tmp_path / "s.json")
    now = bot.now()
    assert bot.should_greet(1, 42, now)
    bot.state["greeted"]["1:42"] = P.iso(now)
    assert not bot.should_greet(1, 42, now + timedelta(days=1))
    assert bot.should_greet(1, 42, now + timedelta(days=8))
    assert bot.should_greet(2, 42, now)   # другой аккаунт — своя личка


def test_pause_blocks_posts(tmp_path):
    bot = make_bot(tmp=tmp_path / "s.json")
    bot.state["paused"] = True
    simulate(bot, days=1)
    assert not posts(bot)


def test_parse_rules():
    r = P.parse_group_ref("@big_group | cooldown=6 | window=9-12, 18-22 | acc=1,3  # коммент")
    assert r.key == "@big_group"
    assert r.rules == {"cooldown": 6.0, "windows": [(9, 12), (18, 22)], "acc": {1, 3}}
    assert P.parse_group_ref("@plain_group").rules == {}


def test_per_group_rules_in_simulation(tmp_path):
    bot = make_bot(n_groups=6, tmp=tmp_path / "s.json")
    bot.rules = {"@group_000": {"cooldown": 6, "windows": [(9, 12), (18, 22)], "acc": {2}},
                 "@group_001": {"cooldown": 48}}
    simulate(bot, days=3)
    g0 = [e for e in posts(bot) if e["group"] == "@group_000"]
    ts0 = [P.parse_iso(e["ts"]) for e in g0]
    assert len(g0) >= 4
    assert all(e["acc"] == 2 for e in g0)
    assert all(9 <= t.hour < 12 or 18 <= t.hour < 22 for t in ts0)
    assert all(b - a >= timedelta(hours=6) for a, b in zip(ts0, ts0[1:]))
    ts1 = [P.parse_iso(e["ts"]) for e in posts(bot) if e["group"] == "@group_001"]
    assert all(b - a >= timedelta(hours=48) for a, b in zip(ts1, ts1[1:]))


def test_wrap_window_and_cooldown_clamp():
    assert P.hour_in_windows(23, [(22, 2)]) and P.hour_in_windows(1, [(22, 2)])
    assert not P.hour_in_windows(12, [(22, 2)])
    assert P.parse_group_ref("@abcde_group | cooldown=0").rules["cooldown"] == 1.0
    assert P.parse_group_ref("https://t.me/s/abcde_group").key == "@abcde_group"


def test_backup_version_changes_on_every_touch(tmp_path):
    bot = make_bot(tmp=tmp_path / "s.json")
    v = bot.version
    bot.save()                 # сохранение на диск не должно «прятать» изменения от бэкапа
    bot.event("dm", 1)
    assert bot.version > v


def test_alert_on_pause(tmp_path):
    bot = make_bot(accs=(1,), fail=E.PeerFloodError(request=None), tmp=tmp_path / "s.json")
    asyncio.run(bot.send_one(1, "@group_000"))
    assert bot.alerts and "Акк.1" in bot.alerts[0]


def test_status_counts_only_listed_groups(tmp_path):
    bot = make_bot(tmp=tmp_path / "s.json")
    bot.state["groups"]["@old_removed"] = {"last_post": P.iso(bot.now())}
    assert "Охвачено групп за 24 ч: 0 из 40" in bot.report_text(None)


# ───────────── надёжность: проверка поста, повторы, причины сбоев ─────────────

def test_explain_error_has_reason_and_fix():
    for exc, kind in [(E.ChatWriteForbiddenError(request=None), "group_off"),
                      (E.PeerFloodError(request=None), "peerflood"),
                      (E.AuthKeyDuplicatedError(request=None), "dead"),
                      (ConnectionError("x"), "transient"),
                      (asyncio.TimeoutError(), "transient"),
                      (RuntimeError("boom"), "unknown")]:
        k, reason, fix = P.explain_error(exc)
        assert k == kind and reason and fix


def test_post_is_verified(tmp_path):
    bot = make_bot(accs=(1,), tmp=tmp_path / "s.json")
    asyncio.run(bot.send_one(1, "@group_000"))
    ev = posts(bot)
    assert len(ev) == 1 and ev[0]["detail"] == "проверено"
    assert bot.pending and bot.pending[0]["key"] == "@group_000"
    assert "Проверено, что пост на месте: 1 из 1" in bot.report_text(None)


def test_deleted_post_is_reported_with_reason(tmp_path):
    bot = make_bot(accs=(1,), lost=True, tmp=tmp_path / "s.json")
    asyncio.run(bot.send_one(1, "@group_000"))
    assert not posts(bot)
    assert any(e["kind"] == "post_deleted" for e in bot.state["events"])
    assert bot.alerts and "удалил админ" in bot.alerts[0] and "Что делать" in bot.alerts[0]
    assert "@group_000" in bot.undelivered_text()


def test_late_check_catches_deletion(tmp_path):
    bot = make_bot(accs=(1,), tmp=tmp_path / "s.json")
    asyncio.run(bot.send_one(1, "@group_000"))
    bot.clients[1].store.clear()                       # модератор удалил пост позже
    bot._now = lambda: datetime.now(TZ) + timedelta(minutes=20)
    asyncio.run(bot.process_pending())
    assert any(e["kind"] == "post_deleted" for e in bot.state["events"])


def test_transient_error_is_retried_without_disabling_group(tmp_path):
    bot = make_bot(accs=(1,), fail=[ConnectionError("net"), ConnectionError("net")], tmp=tmp_path / "s.json")
    asyncio.run(bot.send_one(1, "@group_000"))
    assert len(posts(bot)) == 1 and len(bot.clients[1].sent) == 1     # дошло с 3-й попытки, без дублей
    bot2 = make_bot(accs=(1,), fail=ConnectionError("net"), tmp=tmp_path / "s2.json")
    for _ in range(4):
        asyncio.run(bot2.send_one(1, "@group_000"))
    assert not bot2.state["groups"]["@group_000"].get("off")          # сеть — не повод отключать группу
    assert any(e["kind"] == "post_fail" for e in bot2.state["events"])


def test_no_duplicate_when_send_succeeded_but_connection_dropped(tmp_path):
    bot = make_bot(accs=(1,), tmp=tmp_path / "s.json")
    cl = bot.clients[1]
    real = cl.send_message
    calls = {"n": 0}

    async def flaky(chat, text, **kw):
        calls["n"] += 1
        m = await real(chat, text, **kw)          # сообщение ушло...
        if calls["n"] == 1:
            raise ConnectionError("обрыв после отправки")   # ...но ответ мы не получили
        return m
    cl.send_message = flaky
    asyncio.run(bot.send_one(1, "@group_000"))
    assert len(cl.sent) == 1 and len(posts(bot)) == 1


def test_group_off_reason_in_alert_and_auto_reopen(tmp_path):
    bot = make_bot(accs=(1,), fail=E.ChatWriteForbiddenError(request=None), tmp=tmp_path / "s.json")
    asyncio.run(bot.send_one(1, "@group_000"))
    g = bot.state["groups"]["@group_000"]
    assert "1" in g["off"] and "Что делать" in bot.alerts[0]
    assert "@group_000" in bot.undelivered_text() and "нельзя писать" in bot.undelivered_text()
    t0 = bot.now()
    bot.reopen_due(t0 + timedelta(hours=11))
    assert "1" in g["off"]                                            # рано
    bot.reopen_due(t0 + timedelta(hours=13))
    assert "1" not in g["off"]                                        # перепроверка через 12 ч


def test_missing_chat_does_not_crash(tmp_path):
    bot = make_bot(accs=(1,), tmp=tmp_path / "s.json")
    bot.chat_ids[1].pop("@group_000")
    asyncio.run(bot.send_one(1, "@group_000"))                        # раньше: KeyError → падение цикла
    assert "1" in bot.state["groups"]["@group_000"]["off"]
    assert "не нашёл" in bot.undelivered_text()


def test_alerts_are_not_repeated(tmp_path):
    bot = make_bot(accs=(1,), lost=True, tmp=tmp_path / "s.json")
    for _ in range(3):
        bot.event("group_off", 1, "@group_000", "x", "y")
    assert len(bot.alerts) == 1
