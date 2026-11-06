"""纯逻辑单测：不依赖网络、不需要真 token。

覆盖的是**容易出错且出错代价高**的那几处：
    - 双码校验（HTTP 200 + 业务失败不能被当成成功）
    - 鉴权判定（普通业务文案不能触发刷新）
    - 补登日期选择（makeup_dates 是历史不是待办）
    - 脱敏与预算（多账号下最容易互相污染的两处）
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accounts, growth, http, runner  # noqa: E402


class ApiSuccessTests(unittest.TestCase):
    def test_http_200_with_business_failure_is_not_success(self):
        # 这是整个项目最容易踩的坑：只看 HTTP 码会把业务失败当成功。
        self.assertFalse(http.api_success(200, {"code": 10001, "msg": "今天已签到"}))
        self.assertTrue(http.api_success(200, {"code": 0, "data": {}}))
        self.assertTrue(http.api_success(200, {}))
        self.assertFalse(http.api_success(500, {"code": 0}))
        self.assertFalse(http.api_success(400, {"code": 0}))

    def test_string_code_is_not_treated_as_success(self):
        # 服务端只会给 int。字符串 "0" 属于畸形数据，按「不成功」处理：
        # 误判成功会让一次失败的写操作被当成已完成，而误判失败最坏只是多查一次。
        self.assertFalse(http.api_success(200, {"code": "0"}))
        self.assertFalse(http.api_success(200, {"code": 10001}))


class UnauthorizedTests(unittest.TestCase):
    def test_explicit_auth_semantics(self):
        self.assertTrue(http.is_unauthorized(401, {}))
        self.assertTrue(http.is_unauthorized(403, {}))
        self.assertTrue(http.is_unauthorized(200, {"msg": "invalid_token"}))
        self.assertTrue(http.is_unauthorized(200, {"msg": "登录已失效，请重新登录"}))
        self.assertTrue(http.is_unauthorized(200, {"msg": "凭证已过期"}))

    def test_business_copy_does_not_trigger_refresh(self):
        # 旧实现用 message.contains("token") 兜底，这几种都会误触发刷新，
        # 而刷新失败又整轮中止 —— 一次无关报错就把当天签到弄没了。
        self.assertFalse(http.is_unauthorized(200, {"msg": "token 兑换活动已结束"}))
        self.assertFalse(http.is_unauthorized(200, {"msg": "登录即送 100 额度"}))
        self.assertFalse(http.is_unauthorized(200, {"msg": "请登录后查看活动列表"}))
        self.assertFalse(http.is_unauthorized(200, {"msg": "请求不合法"}))

    def test_network_failure_is_not_auth_failure(self):
        self.assertFalse(http.is_unauthorized(http.CODE_NO_NETWORK, {"error": "timeout"}))


class DigTests(unittest.TestCase):
    def test_penetrates_wrappers(self):
        self.assertEqual(http.dig({"data": {"today_checked_in": True}}, "today_checked_in"), True)
        self.assertEqual(
            http.dig({"result": {"resp": {"streak": {"days": 3}}}}, "streak"), {"days": 3}
        )
        self.assertIsNone(http.dig({"data": {}}, "missing"))

    def test_array_items_are_searched(self):
        self.assertEqual(http.dig({"data": [{"name": "a"}, {"name": "b"}]}, "name"), "a")

    def test_null_is_treated_as_absent(self):
        self.assertIsNone(http.dig({"a": None}, "a"))


class AsIntTests(unittest.TestCase):
    def test_handles_strings_and_garbage(self):
        self.assertEqual(http.as_int("12"), 12)
        self.assertEqual(http.as_int(12.7), 12)
        self.assertEqual(http.as_int("abc", 5), 5)
        self.assertEqual(http.as_int(None, 7), 7)
        # bool 是 int 的子类，必须排除，否则 True 会变成 1
        self.assertEqual(http.as_int(True, 9), 9)


class MakeupCandidateTests(unittest.TestCase):
    """补登选日的边界。这一条错一次就白耗一张不可逆的补登卡。"""

    def _streak(self, made_up=(), launch="2026-06-17", cards=2):
        return {
            "launch_date": launch,
            "streak": {"days": 1, "makeup_dates": list(made_up)},
            "makeup_cards": {"balance": cards},
        }

    def _heatmap(self, today, cells):
        return {"today": {"date": today}, "cells": cells}

    def test_history_is_excluded_from_candidates(self):
        # makeup_dates 是「已补登」历史，不能当待办清单。
        candidates = growth.makeup_candidates(
            self._streak(made_up=["2026-09-28"]),
            self._heatmap("2026-09-30", [{"date": "2026-09-28", "score": 0}, {"date": "2026-09-29", "score": 0}]),
        )
        self.assertEqual(candidates, ["2026-09-29"])

    def test_only_current_month_and_before_today(self):
        candidates = growth.makeup_candidates(
            self._streak(),
            self._heatmap(
                "2026-09-02",
                [
                    {"date": "2026-08-31", "score": 0},  # 上个月，不可补
                    {"date": "2026-09-01", "score": 0},  # 本月历史，可补
                    {"date": "2026-09-02", "score": 0},  # 今天，不算断登
                ],
            ),
        )
        self.assertEqual(candidates, ["2026-09-01"])

    def test_active_days_are_not_candidates(self):
        candidates = growth.makeup_candidates(
            self._streak(),
            self._heatmap("2026-09-10", [{"date": "2026-09-05", "score": 12}]),
        )
        self.assertEqual(candidates, [])

    def test_missing_calendar_entry_is_not_guessed_as_zero(self):
        # 日历没出现的日期不能推测为断登 —— 猜错就是白耗一张卡。
        candidates = growth.makeup_candidates(
            self._streak(),
            self._heatmap("2026-09-10", [{"date": "2026-09-08", "score": 0}]),
        )
        self.assertEqual(candidates, ["2026-09-08"])

    def test_latest_gap_comes_first(self):
        candidates = growth.makeup_candidates(
            self._streak(),
            self._heatmap(
                "2026-09-10",
                [{"date": "2026-09-02", "score": 0}, {"date": "2026-09-08", "score": 0}],
            ),
        )
        self.assertEqual(candidates, ["2026-09-08", "2026-09-02"])

    def test_before_launch_date_is_not_candidate(self):
        candidates = growth.makeup_candidates(
            self._streak(launch="2026-09-05"),
            self._heatmap("2026-09-10", [{"date": "2026-09-02", "score": 0}]),
        )
        self.assertEqual(candidates, [])

    def test_never_started_account_has_no_gap_to_fill(self):
        # 实测形态：账号从未活跃过时，本月所有历史日期都是 score == 0。
        # 那不是「断登」而是「还没开始」，用补登卡去补一个从没开始过的月份
        # 没有意义，而补登卡是不可逆消耗。
        streak = {
            "launch_date": "2026-06-17",
            "streak": {"days": 0, "month_total_days": 0, "makeup_dates": []},
            "makeup_cards": {"balance": 2},
        }
        heatmap = self._heatmap(
            "2026-11-06",
            [{"date": f"2026-11-0{n}", "score": 0} for n in range(1, 6)],
        )
        self.assertEqual(growth.makeup_candidates(streak, heatmap), [])

    def test_started_account_still_finds_gap(self):
        # 有连签历史但中间断了，才是真断登。
        streak = {
            "launch_date": "2026-06-17",
            "streak": {"days": 3, "month_total_days": 5, "makeup_dates": []},
            "makeup_cards": {"balance": 2},
        }
        heatmap = self._heatmap(
            "2026-11-06",
            [
                {"date": "2026-11-02", "score": 0},
                {"date": "2026-11-03", "score": 12},
                {"date": "2026-11-04", "score": 8},
            ],
        )
        self.assertEqual(growth.makeup_candidates(streak, heatmap), ["2026-11-02"])

    def test_bad_shapes_raise_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            growth.makeup_candidates(self._streak(), {"cells": []})  # 缺 today
        with self.assertRaises(ValueError):
            growth.makeup_candidates(self._streak(), self._heatmap("2026-09-10", None))
        with self.assertRaises(ValueError):
            growth.makeup_candidates(
                self._streak(),
                self._heatmap(
                    "2026-09-10",
                    [{"date": "2026-09-05", "score": 0}, {"date": "2026-09-05", "score": 5}],
                ),
            )


class BudgetTests(unittest.TestCase):
    def test_child_is_capped_by_remaining(self):
        parent = http.Budget(10)
        child = parent.child(100)
        self.assertLessEqual(child.left(), parent.left() + 0.01)

    def test_expired_budget_short_circuits(self):
        budget = http.Budget(0)
        self.assertTrue(budget.expired())
        self.assertEqual(budget.child(50).seconds, 0)


class MultiAccountIsolationTests(unittest.TestCase):
    """多账号下最容易互相污染的两处。"""

    def test_mask_only_removes_own_token(self):
        message = "失败：token abc123 无效"
        self.assertEqual(runner._mask(message, "abc123"), "失败：token [REDACTED] 无效")
        # 别人的 token 不该被动到
        self.assertEqual(runner._mask(message, "other-token"), message)
        self.assertEqual(runner._mask(message, ""), message)

    def test_quiet_judged_per_account(self):
        quiet_item = {
            "checkin": {"result": "ALREADY", "ok": True},
            "growth": {"acted": 0, "failed": 0},
            "extras": {"state": "skip"},
        }
        self.assertTrue(runner._is_quiet(quiet_item))

        acted = dict(quiet_item)
        acted["growth"] = {"acted": 1, "failed": 0}
        self.assertFalse(runner._is_quiet(acted))

        failed_checkin = {"checkin": {"result": "ERROR", "ok": False}}
        self.assertFalse(runner._is_quiet(failed_checkin))


class ComposeTests(unittest.TestCase):
    """汇报拼接。这里错一次，用户就会以为一切正常而实际丢了上千积分。"""

    def test_skip_with_root_cause_reaches_cli_output(self):
        # 实测形态：签到成功，但 16 项任务因「需先领养 Buddy」全部被拒。
        # 只在 acted 时拼汇报的话，用户只看到「今日已签过」，完全不知道
        # 还有 1800 积分拿不到。
        result = {
            "checkin": {"message": "今日已签过"},
            "growth": {
                "message": "任务·16 项接取被拒（需先领养 Buddy），18 项待体验",
                "acted": 0,
                "failed": 0,
            },
            "extras": {"state": "skip"},
        }
        text = runner._compose(result, "daily")
        self.assertIn("今日已签过", text)
        self.assertIn("需先领养 Buddy", text)

    def test_empty_growth_message_is_dropped(self):
        result = {
            "checkin": {"message": "今日已签过"},
            "growth": {"message": "成长中心无可领取项", "acted": 0, "failed": 0},
        }
        text = runner._compose(result, "daily")
        self.assertEqual(text, "今日已签过")

    def test_checkin_only_mode_ignores_growth(self):
        result = {
            "checkin": {"message": "成功领取 100 积分"},
            "growth": {"message": "任务·领了 3 项"},
        }
        text = runner._compose(result, "checkin")
        self.assertEqual(text, "成功领取 100 积分")


class GrowthSectionTests(unittest.TestCase):
    def test_finish_counts_only_hard_failures(self):
        result = {
            "travel": {"state": "skip", "message": "今日未出发"},
            "tasks": {"state": "ok", "message": "领了 2 项", "credit": 200},
            "makeup": {"state": "fail", "message": "补登失败"},
            "redeem": {"state": "skip", "message": "未解锁"},
            "lottery": {"state": "ok", "message": "抽了 1 次", "credit": 10},
            "buddy": {"state": "skip", "message": "能量不足"},
            "depart": {"state": "ok", "message": "派 Buddy"},
            "energy": 4,
            "streakDays": 3,
        }
        out = growth._finish(result, None)
        self.assertFalse(out["ok"])  # 有一段硬失败
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["acted"], 3)
        self.assertEqual(out["credit"], 210)
        self.assertIn("+210 积分", out["message"])

    def test_finish_all_skip_is_ok(self):
        result = {
            "travel": {"state": "skip"},
            "tasks": {"state": "skip"},
            "makeup": {"state": "skip"},
            "redeem": {"state": "skip"},
            "lottery": {"state": "skip"},
            "buddy": {"state": "skip"},
            "depart": {"state": "skip"},
        }
        out = growth._finish(result, None)
        self.assertTrue(out["ok"])
        self.assertEqual(out["acted"], 0)
        self.assertEqual(out["message"], "成长中心无可领取项")

    def test_skip_with_root_cause_is_surfaced(self):
        # 「16 项接取被拒（需先领养 Buddy）」这类带根因的 skip 必须透出，
        # 否则最终汇报只剩「无可领取项」，用户看不出为什么没领到。
        result = {
            "tasks": {
                "state": "skip",
                "message": "16 项接取被拒（需先领养 Buddy），18 项待体验",
                "pending": [{"code": "a"}],
            },
        }
        out = growth._finish(result, None)
        self.assertIn("需先领养 Buddy", out["message"])
        self.assertTrue(out["ok"])

    def test_plain_skip_stays_quiet(self):
        # 「今日未出发」「没有抽奖次数」是常态，不该写进汇报。
        result = {"travel": {"state": "skip", "message": "今日未出发"}}
        out = growth._finish(result, None)
        self.assertEqual(out["message"], "成长中心无可领取项")


class TaskStatusTests(unittest.TestCase):
    def test_completed_detection(self):
        self.assertTrue(growth._task_done({"accept_status": "completed"}))
        self.assertFalse(growth._task_done({"accept_status": "claimed"}))
        self.assertTrue(
            growth._task_done({"accept_status": "accepted", "progress": {"current": 5, "target": 5}})
        )
        self.assertFalse(
            growth._task_done({"accept_status": "accepted", "progress": {"current": 4, "target": 5}})
        )
        # progress 为 null 时不能判完成
        self.assertFalse(growth._task_done({"accept_status": "not_accepted", "progress": None}))

    def test_pending_view_excludes_settled(self):
        self.assertIsNone(growth._pending_view({"task_code": "a", "accept_status": "claimed"}))
        self.assertIsNone(
            growth._pending_view({"task_code": "a", "progress": {"current": 3, "target": 3}})
        )
        view = growth._pending_view({"task_code": "b", "title": "T", "progress": {"current": 1, "target": 4}})
        self.assertEqual(view["code"], "b")
        self.assertEqual(view["target"], 4)


class LeaderGateTests(unittest.TestCase):
    """first_buddy 门控。它决定要不要接取进阶任务 —— 判错就会白打 16 个请求。"""

    def test_not_started_means_locked(self):
        items = [
            {"task_code": "first_buddy", "accept_status": "not_accepted"},
            {"task_code": "chat_5", "accept_status": "not_accepted"},
        ]
        unlocked, leader = growth._leader_unlocked(items)
        self.assertFalse(unlocked)
        self.assertIsNotNone(leader)

    def test_completed_but_unclaimed_means_unlocked(self):
        items = [
            {
                "task_code": "first_buddy",
                "accept_status": "completed",
                "progress": {"current": 1, "target": 1},
            }
        ]
        unlocked, _ = growth._leader_unlocked(items)
        self.assertTrue(unlocked)

    def test_claimed_means_unlocked(self):
        items = [{"task_code": "first_buddy", "accept_status": "claimed"}]
        unlocked, _ = growth._leader_unlocked(items)
        self.assertTrue(unlocked)

    def test_missing_leader_is_treated_as_locked(self):
        # 接口改版没给这一项时，保守按未打通：宁可少接取，也不白打 16 个被拒请求。
        unlocked, leader = growth._leader_unlocked(
            [{"task_code": "chat_5", "accept_status": "not_accepted"}]
        )
        self.assertFalse(unlocked)
        self.assertIsNone(leader)

    def test_locked_task_reports_unlock_hint_not_rejection(self):
        """未打通时的汇报不能出现「接取被拒」——那不是故障，是还没开始。"""

        def fake_call(account, budget, method, path, payload=None, retry=None):
            if path.endswith("/tasks"):
                return 200, {
                    "code": 0,
                    "data": {
                        "tasks": [
                            {
                                "task_code": "first_buddy",
                                "title": "领取一只 Buddy",
                                "accept_status": "not_accepted",
                                "progress": None,
                            },
                            {
                                "task_code": "chat_5",
                                "title": "聊 5 次",
                                "accept_status": "not_accepted",
                                "progress": None,
                            },
                        ]
                    },
                }, "h"
            raise AssertionError(f"未打通时不该调用 {path}")

        original = growth._call
        growth._call = fake_call
        try:
            result, auth = growth._tasks({"auth": {}}, http.Budget(30))
        finally:
            growth._call = original

        self.assertTrue(result["locked"])
        self.assertIn("待解锁", result["message"])
        self.assertNotIn("被拒", result["message"])
        self.assertEqual(result["accepted"], 0)

    def test_locked_reporting_is_surfaced_in_summary(self):
        result = {"tasks": {"state": "skip", "message": "进阶任务待解锁", "locked": True}}
        out = growth._finish(result, None)
        self.assertIn("待解锁", out["message"])


class ClientTokenTests(unittest.TestCase):
    def test_format_matches_official_frontend(self):
        token = growth.client_token()
        self.assertTrue(token.startswith("u-"))
        self.assertEqual(len(token), 38)  # "u-" + uuid4
        self.assertNotEqual(growth.client_token(), token)


class TravelClaimTests(unittest.TestCase):
    """旅行领奖的容错。官方前端认这两个文案是正常竞争，不是故障。"""

    def _claim(self, status, body, state="arrived"):
        """用 patch 替掉 _call，直接驱动 _travel。"""
        calls = []

        def fake_call(account, budget, method, path, payload=None, retry=None):
            calls.append((method, path, payload))
            if path.endswith("/travel/status"):
                return 200, {"code": 0, "data": {"state": state, "record_id": 7}}, "h"
            if path.endswith("/travel/claim"):
                return status, body, "h"
            return 200, {"code": 0, "data": {}}, "h"

        original = growth._call
        growth._call = fake_call
        try:
            return growth._travel({"auth": {}}, http.Budget(30))
        finally:
            growth._call = original

    def test_not_arrived_is_skip_not_failure(self):
        result, state, _ = self._claim(400, {"code": 400, "msg": "not arrived yet"})
        self.assertEqual(result["state"], "skip")

    def test_no_unclaimed_travel_is_skip_not_failure(self):
        # 已被客户端或另一轮领过 —— 轮询每轮报假错误会把日志淹没。
        result, state, _ = self._claim(400, {"code": 400, "msg": "no unclaimed travel"})
        self.assertEqual(result["state"], "skip")

    def test_real_claim_failure_stays_failure(self):
        result, state, _ = self._claim(500, {"code": 500, "msg": "grant credits failed"})
        self.assertEqual(result["state"], "fail")

    def test_successful_claim_reports_credit(self):
        result, state, _ = self._claim(200, {"code": 0, "data": {"reward_credit": 8}})
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["credit"], 8)
        # 领成功后旅行记录清空，允许开 Buddy / 派出
        self.assertEqual(state, "idle")

    def test_traveling_state_skips_claim(self):
        result, state, _ = self._claim(200, {"code": 0}, state="traveling")
        self.assertEqual(result["state"], "skip")
        self.assertEqual(state, "traveling")


class ChainDeadTests(unittest.TestCase):
    def test_only_server_revocation_counts(self):
        self.assertTrue(accounts.chain_dead("刷新失败：12153: refresh token is invalid"))
        self.assertTrue(accounts.chain_dead("invalid_grant: Session doesn't have required client"))
        self.assertTrue(accounts.chain_dead("请重新登录"))
        self.assertFalse(accounts.chain_dead("网络异常：error sending request"))
        self.assertFalse(accounts.chain_dead("刷新失败：400 Bad Request"))


class AuthTests(unittest.TestCase):
    """本地口令。它是这个工具唯一的门禁，判错就等于没有。"""

    def setUp(self):
        import tempfile, os
        from core import paths

        self.tmp = tempfile.mkdtemp()
        os.environ["BUDDYCRON_HOME"] = self.tmp
        self._orig = paths.home_dir

    def tearDown(self):
        import os, shutil
        os.environ.pop("BUDDYCRON_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_run_generates_password(self):
        from core import auth

        config = auth.ensure_config()
        self.assertTrue(config.get("created"))
        self.assertTrue(config.get("password_hash"))
        self.assertTrue(config.get("password_salt"))
        # 密码本身只在终端打印一次，配置文件里不该留明文
        self.assertIn("password_hint", config)

    def test_hint_is_cleared_after_first_read(self):
        from core import auth

        auth.ensure_config()
        password = auth.take_new_password()
        self.assertIsNotNone(password)
        self.assertEqual(len(password), 8)
        # 第二次读不到，保证只在终端打印一次
        self.assertIsNone(auth.take_new_password())

    def test_verify_accepts_correct_and_rejects_wrong(self):
        from core import auth

        auth.ensure_config()
        password = auth.take_new_password()
        self.assertTrue(auth.verify(password))
        self.assertFalse(auth.verify("wrongpass"))
        self.assertFalse(auth.verify(""))

    def test_cookie_round_trip(self):
        from core import auth

        auth.ensure_config()
        cookie = auth.issue_cookie()
        self.assertTrue(auth.check_cookie(cookie))
        self.assertFalse(auth.check_cookie(None))
        self.assertFalse(auth.check_cookie(""))
        # 篡改签名必须失败
        self.assertFalse(auth.check_cookie(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")))

    def test_password_change_invalidates_sessions(self):
        from core import auth

        auth.ensure_config()
        old = auth.issue_cookie()
        auth.reset_password()
        # 换密码会换签名密钥，旧会话立刻失效
        self.assertFalse(auth.check_cookie(old))

    def test_lockout_after_repeated_failures(self):
        from core import auth

        client = "127.0.0.1"
        for _ in range(auth.MAX_FAILS):
            auth.note_failure(client)
        self.assertGreater(auth.locked(client), 0)
        auth.clear_failures(client)
        self.assertEqual(auth.locked(client), 0)

    def test_password_alphabet_avoids_ambiguous_chars(self):
        from core import auth

        # 去掉 0/O、1/l/I：口令要在终端里手抄一次，看错的代价是重来一遍。
        for _ in range(40):
            password = auth._random_password()
            self.assertEqual(len(password), 8)
            for ch in password:
                self.assertNotIn(ch, "01loOI")


class CreditsParseTests(unittest.TestCase):
    def test_num_handles_string_and_garbage(self):
        from core import credits

        self.assertEqual(credits._num("223.92000018"), 223.92000018)
        self.assertEqual(credits._num(None), 0.0)
        self.assertEqual(credits._num("abc"), 0.0)
        self.assertEqual(credits._num(True), 0.0)


class ExpiryTests(unittest.TestCase):
    """到期时间。积分会过期，算错就等于让人白白看着额度作废。"""

    def test_date_accepts_datetime_string_and_timestamp(self):
        from core import credits

        # packages 接口给的是这种形态
        self.assertEqual(credits._date("2026-12-03 16:57:04"), "2026-12-03")
        self.assertEqual(credits._date("2026-12-03"), "2026-12-03")
        # 毫秒时间戳
        self.assertEqual(credits._date(1796288224000), "2026-12-03")
        self.assertEqual(credits._date(None), "")
        self.assertEqual(credits._date(""), "")
        self.assertEqual(credits._date("garbage"), "")

    def test_days_left_counts_from_today(self):
        import datetime as dt
        from core import credits

        today = dt.date.today()
        self.assertEqual(credits._days_left(today.isoformat()), 0)
        self.assertEqual(
            credits._days_left((today + dt.timedelta(days=7)).isoformat()), 7
        )
        self.assertEqual(
            credits._days_left((today - dt.timedelta(days=1)).isoformat()), -1
        )
        self.assertIsNone(credits._days_left(""))

    def test_nearest_expiry_skips_expired(self):
        """已过期的包不该当「最近到期」——它已经没有「还剩几天」的意义了。"""
        from core import credits

        packages = [
            {"expiresAt": "2020-01-01", "daysLeft": -300, "remain": 999, "name": "旧包"},
            {"expiresAt": "2026-12-03", "daysLeft": 27, "remain": 1500, "name": "近的"},
            {"expiresAt": "2027-01-01", "daysLeft": 420, "remain": 100, "name": "远的"},
        ]
        nearest = credits._nearest_expiry(packages)
        self.assertEqual(nearest["name"], "近的")

    def test_nearest_expiry_none_when_all_expired(self):
        from core import credits

        self.assertIsNone(credits._nearest_expiry([{"expiresAt": "", "daysLeft": None}]))

    def test_packages_sort_by_expiry_and_drop_empty(self):
        """空包（剩余和总量都是 0）不该占版面。"""
        from core import credits

        def fake_post(url, headers, body, budget=None, **kwargs):
            return 200, {
                "code": 0,
                "data": {
                    "Accounts": [
                        {
                            "PackageCode": "A",
                            "SubProductName": "晚的",
                            "CycleCapacitySizePrecise": "100",
                            "CycleCapacityRemainPrecise": "100",
                            "CycleEndTime": "2027-01-01 00:00:00",
                        },
                        {
                            "PackageCode": "B",
                            "SubProductName": "早的",
                            "CycleCapacitySizePrecise": "500",
                            "CycleCapacityRemainPrecise": "200",
                            "CycleEndTime": "2026-12-01 00:00:00",
                        },
                        {
                            "PackageCode": "C",
                            "SubProductName": "空的",
                            "CycleCapacitySizePrecise": "0",
                            "CycleCapacityRemainPrecise": "0",
                            "CycleEndTime": "2026-12-01 00:00:00",
                        },
                    ]
                },
            }

        original = credits.http.post_retry
        credits.http.post_retry = fake_post
        try:
            packages = credits._packages(
                {"auth": {"endpoint": "https://www.codebuddy.cn"}},
                http.Budget(30),
                "/billing/meter/get-user-resource-free-packages",
                credits.FREE_CODES,
            )
        finally:
            credits.http.post_retry = original

        self.assertEqual([p["name"] for p in packages], ["早的", "晚的"])
        self.assertEqual(packages[0]["remain"], 200.0)
        self.assertEqual(packages[0]["expiresAt"], "2026-12-01")


class MonthCheckinTests(unittest.TestCase):
    def test_checkin_dates_not_heatmap_scores(self):
        """本月签到用 checkin_dates，不是 heatmap 的 score。

        两者含义不同：checkin_dates 是签到记录，score 是活跃度分数
        （含对话、用技能等行为）。混用会让「本月签到」显示成活跃天数。
        """
        from core import checkin

        body = {
            "data": {
                "checkin_dates": ["2026-11-06", "2026-11-05"],
                "week_progress": [False, False, False, True, True, False, False],
            }
        }
        self.assertEqual(checkin._month_checkins(body), ["2026-11-06", "2026-11-05"])
        self.assertEqual(len(checkin._week_progress(body)), 7)

    def test_missing_fields_degrade_quietly(self):
        from core import checkin

        self.assertEqual(checkin._month_checkins({}), [])
        self.assertEqual(checkin._month_checkins({"data": {}}), [])
        self.assertEqual(checkin._week_progress({"data": {}}), [])


class PasswordHintTests(unittest.TestCase):
    """明文口令的保留策略。

    散列存储意味着用户忘了就再也看不到，只能重置。而「刚生成还没登录过」
    恰恰是最可能没记下来的时刻，所以明文留到首次登录成功为止。
    """

    def setUp(self):
        import os, tempfile

        self.tmp = tempfile.mkdtemp()
        os.environ["BUDDYCRON_HOME"] = self.tmp

    def tearDown(self):
        import os, shutil

        os.environ.pop("BUDDYCRON_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hint_survives_until_first_login(self):
        from core import auth

        auth.ensure_config()
        hint = auth.current_password_hint()
        self.assertIsNotNone(hint)
        # 反复读不会消失（与 take_new_password 不同）
        self.assertEqual(auth.current_password_hint(), hint)
        # 登录成功后清除
        auth.clear_password_hint()
        self.assertIsNone(auth.current_password_hint())

    def test_reset_writes_hint_again(self):
        """重设密码往往正是「我忘了密码」，所以必须重新留下明文。"""
        from core import auth

        auth.ensure_config()
        auth.clear_password_hint()
        password = auth.reset_password()
        self.assertEqual(auth.current_password_hint(), password)
        self.assertTrue(auth.verify(password))

    def test_explicit_password_clears_hint(self):
        """用户自己指定的口令不需要留明文——他知道自己设了什么。"""
        from core import auth

        auth.ensure_config()
        auth.set_password("my-own-password")
        self.assertIsNone(auth.current_password_hint())
        self.assertTrue(auth.verify("my-own-password"))


class PortDefaultsTests(unittest.TestCase):
    def test_single_fixed_port(self):
        """只允许一个默认端口。开发期间开过一堆，散落会让人不知道访问哪个。"""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in list(root.glob("**/*.py")) + list(root.glob("**/*.html")):
            if "node_modules" in str(path) or "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for port in re.findall(r"\b87[0-9][0-9]\b", text):
                if port != "8787":
                    offenders.append(f"{path.name}:{port}")
        self.assertEqual(offenders, [], f"发现额外端口：{offenders}")


class LogTests(unittest.TestCase):
    """日志。它是「翻昨天那次为什么失败」的唯一依据，格式不能随便改。"""

    def setUp(self):
        import os, tempfile

        self.tmp = tempfile.mkdtemp()
        os.environ["BUDDYCRON_HOME"] = self.tmp

    def tearDown(self):
        import os, shutil

        os.environ.pop("BUDDYCRON_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _summary(self, mode="daily", quiet=False, credit=100):
        return {
            "mode": mode,
            "total": 1,
            "succeeded": 1,
            "failed": 0,
            "finishedAt": 1793940000000,
            "accounts": [
                {
                    "label": "测试号",
                    "ok": True,
                    "quiet": quiet,
                    "message": "今日已签过",
                    "checkin": {"result": "ALREADY", "credit": credit, "streakDays": 3},
                    "growth": {
                        "credit": 5,
                        "acted": 1,
                        "failed": 0,
                        "sections": {
                            "travel": {"state": "ok", "message": "领旅行礼物 +5 积分"},
                            "tasks": {"state": "skip", "message": "16 项待体验"},
                        },
                    },
                    "credits": {"ok": True, "remain": 2185.05, "used": 314.95, "plan": "体验版"},
                }
            ],
        }

    def test_structured_fields_survive_round_trip(self):
        from core import logs

        logs.append(self._summary())
        records = logs.tail(1)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["mode"], "daily")
        account = record["accounts"][0]
        self.assertEqual(account["checkin"]["credit"], 100)
        self.assertEqual(account["growth"]["credit"], 5)
        # 逐段状态必须保留：出问题时靠它定位是哪一段
        self.assertEqual(account["growth"]["sections"]["travel"]["state"], "ok")
        self.assertEqual(account["credits"]["remain"], 2185.05)

    def test_token_is_redacted(self):
        """日志会被人 cat 或进 journald，token 绝不能落盘。"""
        from core import logs

        summary = self._summary()
        summary["accounts"][0]["message"] = "失败：Bearer abcdefghij" + "x" * 50
        logs.append(summary)
        record = logs.tail(1)[0]
        self.assertIn("[已隐藏]", record["accounts"][0]["message"])
        self.assertNotIn("x" * 50, record["accounts"][0]["message"])

    def test_quiet_poll_is_not_written(self):
        """轮询一天跑 4 次，绝大多数无事。全写进去会淹没真事件。"""
        from core import logs

        logs.append(self._summary(mode="poll", quiet=True))
        self.assertEqual(logs.tail(10), [])
        # 有动作的轮询要写
        logs.append(self._summary(mode="poll", quiet=False))
        self.assertEqual(len(logs.tail(10)), 1)

    def test_latest_first_and_limit(self):
        from core import logs

        for i in range(3):
            summary = self._summary()
            summary["finishedAt"] = 1000 + i
            logs.append(summary)
        records = logs.tail(2)
        self.assertEqual(len(records), 2)
        # 最新的在前
        self.assertEqual(records[0]["ts"], 1002)

    def test_broken_line_is_skipped(self):
        """写到一半断电会留下半行 JSON，不能因此让整个读取失败。"""
        from core import logs

        logs.append(self._summary())
        with open(logs.log_file(), "a", encoding="utf-8") as handle:
            handle.write("{不是合法 JSON\n")
        records = logs.tail(5)
        self.assertEqual(len(records), 1)  # 坏行被跳过，好行还在

    def test_months_sorted_newest_first(self):
        from core import logs
        from core import paths

        paths.ensure_private_dir(paths.logs_dir())
        for month in ("2026-09", "2026-11", "2026-10"):
            (paths.logs_dir() / f"{month}.log").write_text("", encoding="utf-8")
        # 非 YYYY-MM 的文件不该被列进来
        (paths.logs_dir() / "notes.log").write_text("", encoding="utf-8")
        self.assertEqual(logs.months(), ["2026-11", "2026-10", "2026-09"])

    def test_prune_keeps_recent_months(self):
        from core import logs
        from core import paths

        paths.ensure_private_dir(paths.logs_dir())
        months = [f"2026-{m:02d}" for m in range(1, 10)]
        for month in months:
            (paths.logs_dir() / f"{month}.log").write_text("", encoding="utf-8")
        removed = logs.prune()
        self.assertEqual(len(removed), len(months) - logs.KEEP_MONTHS)
        self.assertEqual(len(logs.months()), logs.KEEP_MONTHS)


if __name__ == "__main__":
    unittest.main()
