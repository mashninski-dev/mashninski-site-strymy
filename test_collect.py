"""Тесты collect.py: дневные снимки подписчиков (followers.daily), каапы (общий чат),
гадзіны прагляду (зрителе-секунды сессий).

Запуск: python -m unittest test_collect
"""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import collect


def stats_with(*logins):
    return {"streamers": {login: collect.blank_stats() for login in logins}}


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class DailyFollowersTest(unittest.TestCase):
    def test_writes_snapshot_of_the_day(self):
        stats = stats_with("a")
        collect.update_followers(stats, ["a"], {"a": 100}, utc("2026-10-22T10:00:00"))
        self.assertEqual(stats["streamers"]["a"]["followers"]["daily"], {"2026-10-22": 100})

    def test_same_day_is_not_overwritten(self):
        stats = stats_with("a")
        collect.update_followers(stats, ["a"], {"a": 100}, utc("2026-10-22T10:00:00"))
        collect.update_followers(stats, ["a"], {"a": 105}, utc("2026-10-22T18:00:00"))
        f = stats["streamers"]["a"]["followers"]
        self.assertEqual(f["daily"], {"2026-10-22": 100})
        self.assertEqual(f["count"], 105)

    def test_day_changes_at_minsk_midnight(self):
        stats = stats_with("a")
        # 20:59 UTC — 23:59 по Мінску, 21:00 UTC — уже следующие сутки.
        collect.update_followers(stats, ["a"], {"a": 100}, utc("2026-10-22T20:59:00"))
        collect.update_followers(stats, ["a"], {"a": 101}, utc("2026-10-22T21:00:00"))
        self.assertEqual(
            stats["streamers"]["a"]["followers"]["daily"],
            {"2026-10-22": 100, "2026-10-23": 101},
        )

    def test_failed_request_writes_no_day(self):
        stats = stats_with("a", "b")
        collect.update_followers(stats, ["a", "b"], None, utc("2026-10-22T10:00:00"))
        # Twitch не отдал b — и его день не пишется.
        collect.update_followers(stats, ["a", "b"], {"a": 100}, utc("2026-10-22T11:00:00"))
        self.assertEqual(stats["streamers"]["a"]["followers"]["daily"], {"2026-10-22": 100})
        self.assertEqual(stats["streamers"]["b"]["followers"]["daily"], {})

    def test_record_without_daily_gets_it(self):
        stats = stats_with("a")
        del stats["streamers"]["a"]["followers"]["daily"]
        ts = utc("2026-10-22T10:00:00")
        collect.update_followers(stats, ["a"], {"a": 100}, ts)
        collect.prune_stats(stats["streamers"]["a"], ts)
        self.assertEqual(stats["streamers"]["a"]["followers"]["daily"], {"2026-10-22": 100})

    def test_prune_keeps_31_days(self):
        ss = collect.blank_stats()
        ss["followers"]["daily"] = {
            "2026-09-20": 1,
            "2026-09-21": 2,  # 31 день назад — удаляется
            "2026-09-22": 3,  # 30 дней назад — остаётся: это база прироста
            "2026-10-21": 4,
            "2026-10-22": 5,
        }
        # 23:30 по Мінску 22.10 — сутки ещё 22-го.
        collect.prune_stats(ss, utc("2026-10-22T20:30:00"))
        self.assertEqual(
            ss["followers"]["daily"], {"2026-09-22": 3, "2026-10-21": 4, "2026-10-22": 5}
        )

    def test_monthly_snapshots_untouched(self):
        stats = stats_with("a")
        f = stats["streamers"]["a"]["followers"]
        f["snapshots"] = {"2026-10-01": 90, "2026-09-01": 80}
        ts = utc("2026-10-22T10:00:00")
        collect.update_followers(stats, ["a"], {"a": 100}, ts)
        collect.prune_stats(stats["streamers"]["a"], ts)
        self.assertEqual(f["snapshots"], {"2026-10-01": 90, "2026-09-01": 80})


class FakeTwitch:
    """Ответы shared_chat/session по user_id; None — нет сессии, Exception — сбой запроса."""

    def __init__(self, sessions, users=None):
        self.sessions = sessions
        self.users = users or {}
        self.calls = []
        self.user_calls = []

    def shared_chat(self, uid):
        self.calls.append(uid)
        r = self.sessions.get(uid)
        if isinstance(r, Exception):
            raise r
        return r

    def users_by_id(self, ids):
        self.user_calls.append(list(ids))
        return {i: self.users[i] for i in ids if i in self.users}


def coop_world():
    """Трое наших: a (id 1), b (id 2), c (id 3)."""
    state = {"streamers": {
        "a": {"user_id": "1", "display_name": "A"},
        "b": {"user_id": "2", "display_name": "B"},
        "c": {"user_id": "3", "display_name": "C"},
    }}
    return {"streamers": {}}, state, ["a", "b", "c"]


def session(sid, *ids, created="2026-10-23T10:00:00.744001862Z"):
    return {"session_id": sid, "host_broadcaster_id": ids[0], "created_at": created,
            "updated_at": "2026-10-23T10:05:00Z",
            "participants": [{"broadcaster_id": i} for i in ids]}


def live(*pairs):
    return {uid: {"user_id": uid, "game_name": game} for uid, game in pairs}


class CoopsTest(unittest.TestCase):
    def test_session_starts(self):
        stats, state, logins = coop_world()
        s = session("S", "1", "2")
        tw = FakeTwitch({"1": s, "2": s})
        collect.update_coops(tw, stats, state, logins, live(("1", "Dota 2"), ("2", "Dota 2")),
                             utc("2026-10-23T10:10:00"))
        c = stats["coops"]["S"]
        self.assertEqual(c["started_at"], "2026-10-23T10:00:00Z")
        self.assertIsNone(c["ended_at"])
        self.assertEqual(c["host_id"], "1")
        self.assertEqual([p["login"] for p in c["participants"]], ["a", "b"])
        self.assertEqual(c["games"], ["Dota 2"])
        # Один запрос на сессию: b уже найден в сессии a.
        self.assertEqual(tw.calls, ["1"])
        self.assertNotIn("updated_at", c)

    def test_session_goes_on_without_changes(self):
        stats, state, logins = coop_world()
        s = session("S", "1", "2")
        tw = FakeTwitch({"1": s, "2": s})
        streams = live(("1", "Dota 2"), ("2", "Dota 2"))
        collect.update_coops(tw, stats, state, logins, streams, utc("2026-10-23T10:10:00"))
        before = copy.deepcopy(stats)
        collect.update_coops(tw, stats, state, logins, streams, utc("2026-10-23T10:20:00"))
        self.assertEqual(stats, before)

    def test_new_game_and_participant_join(self):
        stats, state, logins = coop_world()
        collect.update_coops(FakeTwitch({"1": session("S", "1")}), stats, state, logins,
                             live(("1", "Dota 2")), utc("2026-10-23T10:10:00"))
        s = session("S", "1", "2")
        collect.update_coops(FakeTwitch({"1": s, "2": s}), stats, state, logins,
                             live(("1", "Dota 2"), ("2", "Minecraft")), utc("2026-10-23T10:20:00"))
        c = stats["coops"]["S"]
        self.assertEqual([p["id"] for p in c["participants"]], ["1", "2"])
        self.assertEqual(c["games"], ["Dota 2", "Minecraft"])

    def test_session_ends_when_nobody_returns_it(self):
        stats, state, logins = coop_world()
        streams = live(("1", "Dota 2"), ("2", "Dota 2"))
        collect.update_coops(FakeTwitch({"1": session("S", "1", "2")}), stats, state, logins,
                             streams, utc("2026-10-23T10:10:00"))
        # Оба ещё в эфире, но общего чата больше нет.
        collect.update_coops(FakeTwitch({}), stats, state, logins, streams,
                             utc("2026-10-23T11:00:00"))
        self.assertEqual(stats["coops"]["S"]["ended_at"], "2026-10-23T11:00:00Z")

    def test_session_ends_when_ours_go_offline(self):
        stats, state, logins = coop_world()
        collect.update_coops(FakeTwitch({"1": session("S", "1", "2")}), stats, state, logins,
                             live(("1", "Dota 2"), ("2", "Dota 2")), utc("2026-10-23T10:10:00"))
        tw = FakeTwitch({})
        collect.update_coops(tw, stats, state, logins, {}, utc("2026-10-23T11:00:00"))
        self.assertEqual(stats["coops"]["S"]["ended_at"], "2026-10-23T11:00:00Z")
        self.assertEqual(tw.calls, [])

    def test_failed_request_keeps_session_open(self):
        stats, state, logins = coop_world()
        streams = live(("1", "Dota 2"), ("2", "Dota 2"))
        collect.update_coops(FakeTwitch({"1": session("S", "1", "2")}), stats, state, logins,
                             streams, utc("2026-10-23T10:10:00"))
        collect.update_coops(FakeTwitch({"1": RuntimeError("500"), "2": None}), stats, state,
                             logins, streams, utc("2026-10-23T10:20:00"))
        self.assertIsNone(stats["coops"]["S"]["ended_at"])

    def test_foreign_participant_one_get_users(self):
        stats, state, logins = coop_world()
        tw = FakeTwitch({"1": session("S", "1", "900", "901")}, users={
            "900": {"id": "900", "login": "Stranger", "display_name": "Stranger"},
            "901": {"id": "901", "login": "other", "display_name": "Other"},
        })
        collect.update_coops(tw, stats, state, logins, live(("1", "Dota 2")),
                             utc("2026-10-23T10:10:00"))
        parts = stats["coops"]["S"]["participants"]
        self.assertEqual([(p["id"], p["login"], p["name"]) for p in parts],
                         [("1", "a", "A"), ("900", "stranger", "Stranger"), ("901", "other", "Other")])
        self.assertEqual(tw.user_calls, [["900", "901"]])
        # Игры чужих не знаем — только наши.
        self.assertEqual(stats["coops"]["S"]["games"], ["Dota 2"])
        # Второй прогон чужих уже знает — Get Users не нужен.
        collect.update_coops(tw, stats, state, logins, live(("1", "Dota 2")),
                             utc("2026-10-23T10:20:00"))
        self.assertEqual(tw.user_calls, [["900", "901"]])

    def test_foreign_without_login_is_filled_later(self):
        stats, state, logins = coop_world()
        s = session("S", "1", "900")
        collect.update_coops(FakeTwitch({"1": s}), stats, state, logins, live(("1", "X")),
                             utc("2026-10-23T10:10:00"))
        self.assertIsNone(stats["coops"]["S"]["participants"][1]["login"])
        tw = FakeTwitch({"1": s}, users={"900": {"id": "900", "login": "z", "display_name": "Z"}})
        collect.update_coops(tw, stats, state, logins, live(("1", "X")), utc("2026-10-23T10:20:00"))
        self.assertEqual(stats["coops"]["S"]["participants"][1]["login"], "z")

    def test_prune_after_31_days(self):
        stats, state, logins = coop_world()
        stats["coops"] = {
            "old": {"host_id": "1", "started_at": "2026-09-20T10:00:00Z",
                    "ended_at": "2026-09-20T12:00:00Z", "participants": [], "games": []},
            "new": {"host_id": "1", "started_at": "2026-10-20T10:00:00Z",
                    "ended_at": "2026-10-20T12:00:00Z", "participants": [], "games": []},
        }
        collect.update_coops(FakeTwitch({}), stats, state, logins, {}, utc("2026-10-23T10:00:00"))
        self.assertEqual(list(stats["coops"]), ["new"])

    def test_archive_keeps_coops_over_old_version(self):
        stats, state, logins = coop_world()
        stats["collecting_since"] = "2026-09-23"
        stats["streamers"] = {l: collect.blank_stats() for l in logins}
        stats["coops"] = {"S2": {"host_id": "1", "started_at": "2026-10-22T10:00:00Z",
                                 "ended_at": None, "participants": [], "games": []}}
        registry = [{"login": l} for l in logins]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            collect.dump_json(d / "2026-10.json", {"month": "2026-10", "coops": {
                "S1": {"host_id": "2", "started_at": "2026-10-01T10:00:00Z",
                       "ended_at": "2026-10-01T12:00:00Z", "participants": [], "games": []}}})
            collect.update_archive(stats, state, registry, logins, utc("2026-10-23T10:00:00"), {}, d)
            october = collect.load_json(d / "2026-10.json", {})
            september = collect.load_json(d / "2026-09.json", {})
        self.assertEqual(sorted(october["coops"]), ["S1", "S2"])
        # Месяц без каапов ключа не получает.
        self.assertNotIn("coops", september)


def stream(sid, started, viewers, game="Dota 2"):
    """Ответ Get Streams для одного канала."""
    return {"id": sid, "user_id": "1", "started_at": started, "game_name": game,
            "title": "t", "viewer_count": viewers}


def live_from(s):
    return {"since": s["started_at"], "title": s["title"], "game": s["game_name"],
            "viewers": s["viewer_count"], "stream_id": s["id"]}


def closed_at(s, ended):
    return collect.session_from_live(live_from(s), ended)


class ViewersTest(unittest.TestCase):
    def test_trapezoid_on_uneven_runs(self):
        ss = collect.blank_stats()
        s = stream("A", "2026-11-02T10:00:00Z", 10)
        # Первый прогон через 5 минут после начала, потом через 7 и через 23 минуты.
        collect.track_live("a", ss, s, utc("2026-11-02T10:05:00"))
        collect.track_live("a", ss, dict(s, viewer_count=30), utc("2026-11-02T10:12:00"))
        collect.track_live("a", ss, dict(s, viewer_count=20), utc("2026-11-02T10:35:00"))
        # 10 × 300 с (до первого замера — по нему) + (10+30)/2 × 420 + (30+20)/2 × 1380.
        self.assertEqual(ss["in_progress"]["viewer_sec"], 3000 + 8400 + 34500)
        self.assertEqual(ss["in_progress"]["viewers"], 20)

    def test_edges_by_nearest_sample(self):
        ss = collect.blank_stats()
        s = stream("A", "2026-11-02T10:00:00Z", 12)
        collect.track_live("a", ss, s, utc("2026-11-02T10:10:00"))
        collect.track_live("a", ss, dict(s, viewer_count=18), utc("2026-11-02T10:20:00"))
        # Конец — через 15 минут после последнего замера: хвост по нему, 18 зрителей.
        collect.close_stats(ss, live_from(s), closed_at(s, utc("2026-11-02T10:35:00")))
        self.assertIsNone(ss["in_progress"])
        rec = ss["recent_sessions"][0]
        # 12 × 600 + 15 × 600 + 18 × 900 = 32 400 с = 540 мин.
        self.assertEqual(rec["viewer_minutes"], 540)
        self.assertEqual(rec["viewers_last"], 18)
        self.assertEqual(ss["monthly"]["2026-11"]["viewer_minutes"], 540)
        # Границы те же, что у часов эфира: 35 минут.
        self.assertEqual(ss["monthly"]["2026-11"]["minutes"], 35)

    def test_vod_cuts_tail(self):
        ss = collect.blank_stats()
        s = stream("A", "2026-11-02T10:00:00Z", 10)
        collect.track_live("a", ss, s, utc("2026-11-02T10:10:00"))
        collect.track_live("a", ss, dict(s, viewer_count=20), utc("2026-11-02T10:20:00"))
        # Офлайн бот увидел только через два часа — хвост 120 минут по 20 зрителей.
        collect.close_stats(ss, live_from(s), closed_at(s, utc("2026-11-02T12:20:00")))
        self.assertEqual(ss["recent_sessions"][0]["viewer_minutes"], 100 + 150 + 2400)
        # Запись говорит: эфир кончился в 10:30. Хвост 110 минут × 20 снимается.
        self.assertTrue(collect.retime_stats(ss, "2026-11-02T10:00:00Z", "2026-11-02T10:30:00Z"))
        rec = ss["recent_sessions"][0]
        self.assertEqual(rec["viewer_minutes"], 100 + 150 + 200)
        self.assertEqual(ss["monthly"]["2026-11"]["viewer_minutes"], 450)
        self.assertEqual(ss["monthly"]["2026-11"]["minutes"], 30)
        # Повторный пересчёт ничего не меняет.
        before = copy.deepcopy(ss)
        self.assertFalse(collect.retime_stats(ss, "2026-11-02T10:00:00Z", "2026-11-02T10:30:00Z"))
        self.assertEqual(ss, before)

    def test_old_in_progress_does_not_count(self):
        # in_progress, открытый до появления полей, зрителей не копит и ключей не даёт:
        # октябрь, в котором зрителей не собирали, не меняется.
        ss = collect.blank_stats()
        s = stream("A", "2026-10-31T20:00:00Z", 10)
        prog = collect.open_progress("A", s["started_at"], "Dota 2")
        del prog["viewer_sec"], prog["viewers"]
        prog["last_seen_at"] = "2026-10-31T20:30:00Z"
        ss["in_progress"] = prog
        collect.track_live("a", ss, s, utc("2026-10-31T21:00:00"))
        self.assertNotIn("viewer_sec", ss["in_progress"])
        collect.close_stats(ss, live_from(s), closed_at(s, utc("2026-10-31T21:10:00")))
        self.assertNotIn("viewer_minutes", ss["recent_sessions"][0])
        self.assertNotIn("viewer_minutes", ss["monthly"]["2026-10"])

    def test_missed_session_is_zero(self):
        st = {"live": {"stream_id": None, "since": None},
              "history": [{"started_at": "2026-11-01T10:00:00Z", "ended_at": "2026-11-01T12:00:00Z",
                           "stream_id": "OLD"}]}
        ss = collect.blank_stats()
        video = {"id": "v1", "url": "u", "stream_id": "NEW", "created_at": "2026-11-02T08:00:00Z",
                 "duration": "1h0m0s", "title": "t", "view_count": 900}
        with mock.patch.object(collect, "fetch_chapters", return_value=[("Dota 2", 3600)]), \
                mock.patch.object(collect, "now", return_value=utc("2026-11-02T12:00:00")):
            collect.missed_sessions("a", st, ss, [video], "2026-09-23", None)
        self.assertEqual(ss["recent_sessions"][0]["viewer_minutes"], 0)
        self.assertEqual(ss["monthly"]["2026-11"]["viewer_minutes"], 0)
        self.assertEqual(ss["monthly"]["2026-11"]["minutes"], 60)

    def test_archive_month_gets_viewers_only_where_counted(self):
        stats = {"collecting_since": "2026-09-23", "streamers": {"a": collect.blank_stats()}}
        ss = stats["streamers"]["a"]
        ss["monthly"]["2026-10"] = collect.blank_month()
        s = stream("A", "2026-11-02T10:00:00Z", 10)
        collect.track_live("a", ss, s, utc("2026-11-02T10:10:00"))
        collect.close_stats(ss, live_from(s), closed_at(s, utc("2026-11-02T10:20:00")))
        with tempfile.TemporaryDirectory() as d:
            collect.update_archive(stats, {"streamers": {"a": {"history": []}}}, [{"login": "a"}],
                                   ["a"], utc("2026-11-02T11:00:00"), {}, Path(d))
            nov = collect.load_json(Path(d) / "2026-11.json", {})["streamers"]["a"]
            octo = collect.load_json(Path(d) / "2026-10.json", {})["streamers"]["a"]
        self.assertEqual(nov["viewer_minutes"], 200)
        self.assertEqual(nov["sessions"][0]["viewer_minutes"], 200)
        self.assertNotIn("viewer_minutes", octo)


class FakeHelix:
    """Twitch для полного прогона run(): один канал «a» (user_id 1)."""
    live = {}

    def __init__(self, *_):
        pass

    def users(self, logins):
        return {"a": {"id": "1", "login": "a", "display_name": "A", "profile_image_url": "p",
                      "description": "", "broadcaster_type": ""}}

    def users_by_id(self, ids):
        return {}

    def streams(self, ids):
        return dict(FakeHelix.live)

    def archives(self, user_id, limit=20):
        return []

    def shared_chat(self, uid):
        return None


class RunTest(unittest.TestCase):
    """Полный прогон run() на временной папке: Twitch и GraphQL подменены."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "streamers.json").write_text(json.dumps(
            {"streamers": [{"login": "a", "added_at": "2026-09-11"}]}), encoding="utf-8")
        patches = [
            mock.patch.object(collect, "REGISTRY_PATH", d / "streamers.json"),
            mock.patch.object(collect, "STATE_PATH", d / "data" / "twitch-state.json"),
            mock.patch.object(collect, "STATS_PATH", d / "data" / "twitch-stats.json"),
            mock.patch.object(collect, "ARCHIVE_DIR", d / "data" / "archive"),
            mock.patch.object(collect, "Twitch", FakeHelix),
            mock.patch.object(collect, "fetch_followers", return_value=({"a": 100}, {})),
            mock.patch.dict(os.environ, {"TWITCH_CLIENT_ID": "x", "TWITCH_CLIENT_SECRET": "y"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)
        self.data = d / "data"

    def run_at(self, when, live=None):
        FakeHelix.live = live or {}
        with mock.patch.object(collect, "now", return_value=utc(when)):
            self.assertEqual(collect.run(), 0)

    def files(self):
        return {p.relative_to(self.data).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(self.data.rglob("*.json"))}

    def stats(self):
        return collect.load_json(self.data / "twitch-stats.json", {})

    def test_viewers_since_written_once(self):
        self.run_at("2026-11-02T10:05:00")
        self.assertEqual(self.stats()["viewers_since"], "2026-11-02")
        self.run_at("2026-11-05T10:05:00")
        self.assertEqual(self.stats()["viewers_since"], "2026-11-02")

    def test_run_without_streams_changes_nothing(self):
        s = stream("A", "2026-11-02T10:00:00Z", 10)
        self.run_at("2026-11-02T10:13:00", {"1": s})
        self.run_at("2026-11-02T10:23:00")  # эфир закончился — сессия закрыта
        before = self.files()
        self.run_at("2026-11-02T10:33:00")
        self.run_at("2026-11-02T10:43:00")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.stats()["streamers"]["a"]["recent_sessions"][0]["viewer_minutes"], 230)

    def test_restart_does_not_double(self):
        a = stream("A", "2026-11-02T10:00:00Z", 10)
        self.run_at("2026-11-02T10:10:00", {"1": a})
        # Канал упал в 10:15 и поднялся в 10:16 новым stream_id — офлайна бот не видел.
        b = stream("B", "2026-11-02T10:16:00Z", 40)
        self.run_at("2026-11-02T10:20:00", {"1": b})
        ss = self.stats()["streamers"]["a"]
        first = ss["recent_sessions"][0]
        # Первая — до начала второй: 16 минут по 10 зрителей (хвост по последнему замеру).
        self.assertEqual(first["ended_at"], "2026-11-02T10:16:00Z")
        self.assertEqual(first["viewer_minutes"], 160)
        # Вторая — с собственного начала, 4 минуты по первому замеру 40.
        self.assertEqual(ss["in_progress"]["stream_id"], "B")
        self.assertEqual(ss["in_progress"]["viewer_sec"], 40 * 240)
        self.assertEqual(ss["monthly"]["2026-11"]["viewer_minutes"], 160)


if __name__ == "__main__":
    unittest.main()
