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


class VersionTests(unittest.TestCase):
    """版本号。CI 用它校验 tag，页面用它显示，不能有第二个来源。"""

    def test_version_format(self):
        import re
        from core import version

        self.assertRegex(version.VERSION, r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_display_has_v_prefix(self):
        from core import version

        self.assertEqual(version.display(), f"v{version.VERSION}")

    def test_no_hardcoded_version_elsewhere(self):
        """版本号只该在 core/version.py 里出现一次。

        散落会导致「镜像标着 0.3.0 但代码里是 0.2.9」——这种不一致比没有版本号更糟。
        """
        import re
        from pathlib import Path
        from core import version

        root = Path(__file__).resolve().parent.parent
        offenders = []
        pattern = re.compile(r"""["']v?[0-9]+\.[0-9]+\.[0-9]+["']""")
        for path in root.glob("**/*.py"):
            # 跳过测试与版本文件本身：测试里本来就要写各种版本字面量
            # （"99.99.99" 用来验证「查不到」的分支）。
            if "__pycache__" in str(path) or path.name in ("version.py", "changelog.py"):
                continue
            if "tests" in path.parts:
                continue
            for line_no, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{line_no}: {line.strip()[:60]}")
        self.assertEqual(offenders, [], f"发现硬编码版本号：{offenders}")


class EnvPasswordTests(unittest.TestCase):
    """环境变量提供的密码。Docker 部署的主路径 —— 在 compose 里写一次，
    比去翻容器日志找随机串方便得多。"""

    def setUp(self):
        import os, tempfile

        self.tmp = tempfile.mkdtemp()
        os.environ["BUDDYCRON_HOME"] = self.tmp
        os.environ.pop("BUDDYCRON_PASSWORD", None)

    def tearDown(self):
        import os, shutil

        os.environ.pop("BUDDYCRON_HOME", None)
        os.environ.pop("BUDDYCRON_PASSWORD", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_password_is_used(self):
        import os
        from core import auth

        os.environ["BUDDYCRON_PASSWORD"] = "mypass123"
        config = auth.ensure_config()
        self.assertTrue(config.get("from_env"))
        self.assertTrue(auth.verify("mypass123"))
        self.assertFalse(auth.verify("wrong"))
        # 环境变量提供了密码，就不该再留明文 hint
        self.assertIsNone(auth.current_password_hint())

    def test_changing_env_overrides_previous(self):
        """改了环境变量就等于改密码 —— 否则「我改了却没生效」更让人困惑。"""
        import os
        from core import auth

        os.environ["BUDDYCRON_PASSWORD"] = "first"
        auth.ensure_config()
        self.assertTrue(auth.verify("first"))

        os.environ["BUDDYCRON_PASSWORD"] = "second"
        auth.ensure_config()
        self.assertTrue(auth.verify("second"))
        self.assertFalse(auth.verify("first"))

    def test_without_env_keeps_last_password(self):
        """去掉环境变量后密码不该丢 —— 沿用上次算出的散列。"""
        import os
        from core import auth

        os.environ["BUDDYCRON_PASSWORD"] = "keepme"
        auth.ensure_config()

        os.environ.pop("BUDDYCRON_PASSWORD", None)
        config = auth.ensure_config()
        self.assertFalse(config.get("from_env"))
        self.assertTrue(auth.verify("keepme"))

    def test_empty_env_falls_back_to_random(self):
        """空字符串等于没设 —— 不该把空密码当有效值。"""
        import os
        from core import auth

        os.environ["BUDDYCRON_PASSWORD"] = "   "
        config = auth.ensure_config()
        self.assertFalse(config.get("from_env"))
        # 回落到随机生成，此时应有明文 hint
        self.assertIsNotNone(auth.current_password_hint())

    def test_passwd_refuses_in_env_mode(self):
        """passwd 在环境变量模式下必须拒绝，而不是产出一个会被覆盖的新口令。"""
        import io
        import os
        import sys
        from contextlib import redirect_stderr

        import cli

        os.environ["BUDDYCRON_PASSWORD"] = "envpass"
        before = None
        from core import auth

        auth.ensure_config()
        before = auth._config()["password_hash"]

        buffer = io.StringIO()
        with redirect_stderr(buffer):
            code = cli.main(["passwd"])
        self.assertEqual(code, 1)
        self.assertIn("BUDDYCRON_PASSWORD", buffer.getvalue())
        # 散列没被改动
        self.assertEqual(auth._config()["password_hash"], before)

    def test_passwd_show_refuses_in_env_mode(self):
        import io
        import os
        from contextlib import redirect_stderr

        import cli

        os.environ["BUDDYCRON_PASSWORD"] = "envpass"
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            code = cli.main(["passwd", "--show"])
        self.assertEqual(code, 1)
        self.assertIn("环境变量", buffer.getvalue())


class AssetTests(unittest.TestCase):
    """界面产物。CI 会检查它们和源码同步，这里检查它们本身有效。"""

    def test_favicon_is_valid_svg(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for name in ("web/src/favicon.svg", "web/dist/favicon.svg"):
            path = root / name
            self.assertTrue(path.exists(), f"缺少 {name}")
            text = path.read_text(encoding="utf-8")
            self.assertIn("<svg", text)
            self.assertIn('xmlns="http://www.w3.org/2000/svg"', text)
            # 标签要闭合，否则某些浏览器会整块丢弃
            self.assertEqual(text.count("<svg"), text.count("</svg>"))

    def test_favicon_is_linked_in_both_pages(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for name in ("web/src/index.html", "web/src/login.html"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn('rel="icon"', text, f"{name} 缺少 favicon 链接")
            self.assertIn("/favicon.svg", text)

    def test_favicon_has_no_background_plate(self):
        """图标不能铺底色。

        之前垫了一层深色纸底，结果在浅色标签栏上看起来就是一圈黑边。
        朱红本身在两种标签栏上都够醒目，不需要衬底。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/favicon.svg").read_text(encoding="utf-8")
        # 不允许铺满整个 viewBox 的 32×32 底块
        self.assertNotIn('width="32" height="32" rx="7"', text)
        self.assertNotIn('fill="#14120f"', text)

    def test_favicon_keeps_safe_margin(self):
        """图标本体要留一点边距，否则圆角会被裁掉。"""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/favicon.svg").read_text(encoding="utf-8")
        match = re.search(r'<rect x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', text)
        self.assertIsNotNone(match, "找不到图标本体的 rect")
        x, y, w, h = (int(g) for g in match.groups())
        # 不能贴到 0，也不能超出 32
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)
        self.assertLessEqual(x + w, 32)
        self.assertLessEqual(y + h, 32)

    def test_no_stray_text_in_heading(self):
        """副标题不该带调试残留（曾经出现过「盖一个章 v2」）。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/index.html").read_text(encoding="utf-8")
        self.assertIn("每个账号每天盖一个章", text)
        self.assertNotIn("盖一个章 v", text)


class MergedAccountRowTests(unittest.TestCase):
    """账号与积分合并在一行。同一个账号出现在两处会让人来回对照，
    账号一多两个列表各自滚动更找不到东西。"""

    def test_no_separate_credits_section(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/index.html").read_text(encoding="utf-8")
        # 独立的「各账号积分」区块应已删除
        self.assertNotIn('id="credits-accounts"', text)
        self.assertNotIn('id="cred-list"', text)
        # 合计区块保留
        self.assertIn('id="credits-summary"', text)

    def test_delete_button_inside_summary(self):
        """删除按钮必须在 summary 内部。

        details 折叠时会隐藏 summary 之外的所有子元素 —— 放外面就点不到了，
        而且这个问题在展开状态下看不出来，只有折叠时才暴露。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/index.html").read_text(encoding="utf-8")
        self.assertIn('data-del=', text)
        head_start = text.index("const head = ")
        summary_start = text.index('<summary class="acct-summary">${head}${actions}</summary>')
        self.assertLess(head_start, summary_start)

    def test_delete_click_does_not_toggle_details(self):
        """点删除不该顺带折叠。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/index.html").read_text(encoding="utf-8")
        self.assertIn("event.preventDefault()", text)
        self.assertIn("event.stopPropagation()", text)

    def test_actions_are_siblings_not_absolute(self):
        """删除与展开箭头必须在同一个 flex 操作区里。

        之前两个都用绝对定位、各算各的 top，永远对不齐 —— 视觉上就是「错位」。
        放进同一个 flex 容器后，垂直对齐由布局保证，不靠手调像素。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/index.html").read_text(encoding="utf-8")

        # 两者都在 acct-actions 里
        self.assertIn('class="acct-actions"', text)
        actions_idx = text.index('class="acct-actions"')
        self.assertIn("data-del=", text[actions_idx:actions_idx + 400])
        self.assertIn("acct-caret", text[actions_idx:actions_idx + 400])

        # 不该再有绝对定位的删除按钮 / ::after 箭头
        css = text[text.index("<style>") : text.index("</style>")]
        block = css.split(".acct-summary {")[1].split(".acct-name")[0]
        self.assertNotIn("position: absolute", block)

    def test_padding_on_row_not_container(self):
        """留白必须在 summary 上，不是外层容器。

        放外层的话 hover 背景只盖内容区、不含上下留白，看起来像浮在半空；
        而且元素会贴到行边。
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "web/src/index.html").read_text(encoding="utf-8")
        # 只取 <style> 块，避开 head 里的主题脚本和底部的页面脚本
        css = text[text.index("<style>") : text.index("</style>")]

        # .acct 本身不该有 padding（留白交给 summary / acct-plain）
        block = re.search(r"\.acct \{(.*?)\}", css, re.S)
        self.assertIsNotNone(block, "找不到 .acct 规则")
        self.assertNotIn("padding", block.group(1))

        # summary 与 plain 共享一段带 padding 的规则
        shared = re.search(r"\.acct-summary,\s*\.acct-plain \{(.*?)\}", css, re.S)
        self.assertIsNotNone(shared, "找不到 summary/plain 共享的留白规则")
        self.assertIn("padding", shared.group(1))


class LoginSessionPersistenceTests(unittest.TestCase):
    """登录会话落盘。容器重启（更新镜像、改配置、宿主机重启）会清掉内存，
    而此时用户的浏览器可能还停在登录页上 —— 不落盘就接不住那次授权。"""

    def setUp(self):
        import os, tempfile

        self.tmp = tempfile.mkdtemp()
        os.environ["BUDDYCRON_HOME"] = self.tmp

    def tearDown(self):
        import os, shutil

        os.environ.pop("BUDDYCRON_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self):
        from core import accounts

        return accounts.LoginSession(
            "state-abc", "https://www.codebuddy.cn/login?state=state-abc", "www.codebuddy.cn"
        )

    def test_round_trip(self):
        from core import accounts

        session = self._session()
        accounts.save_login_session(session)
        restored = accounts.load_login_session()
        self.assertIsNotNone(restored)
        self.assertEqual(restored.state, "state-abc")
        self.assertEqual(restored.auth_url, session.auth_url)
        self.assertEqual(restored.host, "www.codebuddy.cn")

    def test_expired_session_is_dropped(self):
        import time
        from core import accounts

        session = self._session()
        session.created_at = time.time() - accounts.LOGIN_SESSION_TIMEOUT - 1
        accounts.save_login_session(session)
        self.assertIsNone(accounts.load_login_session())
        # 过期的那份应当被清掉，不留在磁盘上
        self.assertFalse(accounts.pending_file().exists())

    def test_corrupt_file_is_ignored(self):
        from core import accounts, paths

        paths.write_private(accounts.pending_file(), "{不是 JSON")
        self.assertIsNone(accounts.load_login_session())

    def test_untrusted_host_is_rejected(self):
        """host 必须在白名单内 —— 篡改落盘文件不该把用户导向别的域名。"""
        import json
        from core import accounts, paths

        paths.write_private(
            accounts.pending_file(),
            json.dumps(
                {
                    "state": "s",
                    "authUrl": "https://evil.example/login",
                    "host": "evil.example",
                    "createdAt": 0,
                }
            ),
        )
        self.assertIsNone(accounts.load_login_session())

    def test_clear_removes_file(self):
        from core import accounts

        accounts.save_login_session(self._session())
        self.assertTrue(accounts.pending_file().exists())
        accounts.clear_login_session()
        self.assertFalse(accounts.pending_file().exists())

    def test_completed_session_is_not_persisted(self):
        """已完成的会话没有恢复价值，落盘只会留个垃圾文件。"""
        from core import accounts

        session = self._session()
        session.done = True
        accounts.save_login_session(session)
        self.assertFalse(accounts.pending_file().exists())


class BudgetSizingTests(unittest.TestCase):
    """预算要撑得住账号数。预算太小会让后面的账号静默跳过。"""

    def test_daily_budget_fits_seven_accounts(self):
        from core import runner

        # 7 个账号 × 每账号配额，必须装得进全局预算
        needed = runner.PER_ACCOUNT_BUDGET * 7
        self.assertGreaterEqual(
            runner.DAILY_BUDGET,
            needed,
            f"每日预算 {runner.DAILY_BUDGET}s 装不下 7 个账号"
            f"（需要 {needed}s），后面的账号会被跳过",
        )

    def test_per_account_budget_is_reasonable(self):
        from core import runner

        # 单账号实测约 20 个请求，正常 20~40 秒；网络抖动时重试会翻倍。
        # 低于 60 秒会让「一次重试就超时」变成常态。
        self.assertGreaterEqual(runner.PER_ACCOUNT_BUDGET, 60)
        self.assertLessEqual(runner.PER_ACCOUNT_BUDGET, 180)

    def test_systemd_timeout_exceeds_budget(self):
        """systemd 的 TimeoutStartSec 必须大于预算，否则进程会被 systemd 杀掉，
        而结果通常最后才落盘 —— 当天记录整条丢失。"""
        import re
        from pathlib import Path
        from core import runner

        root = Path(__file__).resolve().parent.parent
        daily = (root / "deploy/buddycron-daily.service").read_text(encoding="utf-8")
        poll = (root / "deploy/buddycron-poll.service").read_text(encoding="utf-8")

        daily_timeout = int(re.search(r"TimeoutStartSec=(\d+)", daily).group(1))
        poll_timeout = int(re.search(r"TimeoutStartSec=(\d+)", poll).group(1))

        self.assertGreater(daily_timeout, runner.DAILY_BUDGET)
        self.assertGreater(poll_timeout, runner.POLL_BUDGET)


class ScheduleDefaultsTests(unittest.TestCase):
    """定时时间。9 点后跑，不用熬夜等结果。"""

    def test_entrypoint_defaults_to_nine(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "deploy/entrypoint.sh").read_text(encoding="utf-8")
        hour = re.search(r'BUDDYCRON_DAILY_HOUR:-(\d+)', text).group(1)
        self.assertEqual(hour, "9", "默认签到时间应是 9 点")

    def test_systemd_timer_starts_at_nine(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        daily = (root / "deploy/buddycron-daily.timer").read_text(encoding="utf-8")
        self.assertIn("09:00", daily)

    def test_poll_timer_does_not_run_before_daily(self):
        """补签在 9 点前跑没有意义 —— 主签到还没跑过，白跑一趟。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        poll = (root / "deploy/buddycron-poll.timer").read_text(encoding="utf-8")
        self.assertIn("09,13,17,21", poll)


class EnvVarDocsTests(unittest.TestCase):
    """文档里的环境变量必须和代码一致。

    这类表格最容易腐烂：加了新变量忘了写、删了旧变量忘了删，最后文档
    比没有还误导。所以用测试对着代码扫一遍。
    """

    def _code_env_vars(self) -> set[str]:
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        found: set[str] = set()
        for path in root.glob("**/*.py"):
            if "__pycache__" in str(path) or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            found.update(re.findall(r"BUDDYCRON_[A-Z_]+", text))
        # 容器入口脚本也读环境变量
        entry = root / "deploy/entrypoint.sh"
        if entry.exists():
            found.update(re.findall(r"BUDDYCRON_[A-Z_]+", entry.read_text(encoding="utf-8")))
        return found

    def _documented_env_vars(self) -> set[str]:
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "README.md").read_text(encoding="utf-8")
        return set(re.findall(r"`(BUDDYCRON_[A-Z_]+)`", text))

    def test_readme_documents_every_env_var(self):
        """代码读到的每个变量都要在 README 里出现。"""
        missing = self._code_env_vars() - self._documented_env_vars()
        self.assertEqual(
            missing,
            set(),
            f"README 缺少这些环境变量的说明：{sorted(missing)}",
        )

    def test_readme_has_env_var_table(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("## 环境变量", text)
        # 至少要有默认值一列 —— 没有默认值的表格等于没说
        self.assertIn("| 变量 | 默认 | 作用 |", text)

    def test_compose_only_vars_are_labelled(self):
        """BUDDYCRON_UID / GID 只在 compose 里用，不该被误当成程序读的变量。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "README.md").read_text(encoding="utf-8")
        idx = text.index("### Docker Compose 专用")
        block = text[idx:idx + 400]
        self.assertIn("BUDDYCRON_UID", block)
        self.assertIn("BUDDYCRON_GID", block)

    def test_version_var_marked_as_not_read_at_runtime(self):
        """BUDDYCRON_VERSION 只是镜像 label，运行时不读 —— 别让人以为能覆盖版本号。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "README.md").read_text(encoding="utf-8")
        idx = text.index("### 只进镜像元数据")
        block = text[idx:idx + 300]
        self.assertIn("BUDDYCRON_VERSION", block)
        self.assertIn("运行时不读取", block)


class ScreenshotTests(unittest.TestCase):
    """README 里引用的截图必须真实存在。

    图片链接失效不会让任何测试失败，但会让首页看起来像半成品。
    """

    def test_all_referenced_screenshots_exist(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "README.md").read_text(encoding="utf-8")
        refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
        self.assertTrue(refs, "README 里没有任何截图引用")
        missing = [ref for ref in refs if not (root / ref).exists()]
        self.assertEqual(missing, [], f"引用了不存在的截图：{missing}")

    def test_screenshots_are_reasonably_sized(self):
        """截图要控制体积。仓库里放几张几 MB 的 PNG 会让 clone 变慢。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        shots = list((root / "docs/screenshots").glob("*"))
        self.assertGreater(len(shots), 0, "截图目录为空")
        for path in shots:
            size = path.stat().st_size
            self.assertLess(size, 400_000, f"{path.name} 体积 {size} 字节，超过 400KB")


class ThemeTests(unittest.TestCase):
    """明暗切换。两套配色都在 CSS 变量里，切换只改 data-theme。"""

    def test_both_themes_defined(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        css = (root / "web/src/input.css").read_text(encoding="utf-8")
        # 浅色在 @theme，深色在 [data-theme="dark"]
        self.assertIn("--color-paper:", css)
        self.assertIn(':root[data-theme="dark"]', css)
        # 关键变量两套都要有，漏一个会让某个元素在深色下「消失」
        for var in ("--color-paper", "--color-ink", "--color-seal", "--color-rule"):
            self.assertGreaterEqual(
                css.count(f"{var}:"), 2, f"{var} 只定义了一套配色"
            )

    def test_theme_set_before_css_loads(self):
        """主题必须在 head 里同步设置，否则刷新时会先闪一下另一套配色。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for name in ("web/src/index.html", "web/src/login.html"):
            text = (root / name).read_text(encoding="utf-8")
            head = text[:text.index("</head>")]
            self.assertIn("data-theme", head, f"{name} 没在 head 里设主题")
            # 要在 CSS 之前执行
            css_pos = head.index('href="/assets/app.css"')
            script_pos = head.index("data-theme")
            self.assertLess(script_pos, css_pos, f"{name} 的主题脚本在 CSS 之后执行")

    def test_theme_choice_persists(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for name in ("web/src/index.html", "web/src/login.html"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("buddycron-theme", text, f"{name} 没持久化主题选择")

    def test_both_pages_share_theme_key(self):
        """两页共用同一个 localStorage 键，否则登录前后配色会跳。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        keys = set()
        for name in ("web/src/index.html", "web/src/login.html"):
            text = (root / name).read_text(encoding="utf-8")
            import re
            keys.update(re.findall(r"localStorage\.(?:getItem|setItem)\('([^']+)'", text))
        self.assertEqual(keys, {"buddycron-theme"})


class ChangelogTests(unittest.TestCase):
    """CHANGELOG 与版本号。发布时 CI 从这里抽 Release 正文 ——
    段落缺失会让 Release 变成空白页。"""

    def test_current_version_has_changelog_entry(self):
        from core import version
        import scripts.changelog as changelog

        body = changelog.extract(version.VERSION)
        self.assertTrue(
            body, f"CHANGELOG.md 缺少 [{version.VERSION}] 段落"
        )

    def test_versions_are_semver(self):
        import re
        import scripts.changelog as changelog

        for v in changelog.versions():
            self.assertRegex(v, r"^[0-9]+\.[0-9]+\.[0-9]+$", f"{v} 不是合法语义化版本")

    def test_versions_newest_first(self):
        """CHANGELOG 约定最新版本在最上面 —— 抽段落时不依赖顺序，但人读的时候依赖。"""
        import scripts.changelog as changelog

        versions = changelog.versions()
        if len(versions) < 2:
            self.skipTest("只有一个版本，无从比较")
        self.assertEqual(versions, sorted(versions, reverse=True))

    def test_missing_version_returns_empty(self):
        """查不到就返回空串，由调用方决定报错 —— 不抛异常，方便 CI 分支判断。"""
        import scripts.changelog as changelog

        self.assertEqual(changelog.extract("99.99.99"), "")


class DeployStackTests(unittest.TestCase):
    """stack.yml 与 Caddyfile。这两个文件错了就是「部署后打不开」，
    而本地开发根本不会跑到这条路径，所以用测试钉住关键约束。"""

    def test_stack_has_required_public_vars(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "stack.yml").read_text(encoding="utf-8")
        # 公网部署三项必须存在
        for var in ("BUDDYCRON_REQUIRE_HTTPS", "BUDDYCRON_HOSTS", "BUDDYCRON_TRUST_PROXY"):
            self.assertIn(var, text, f"stack.yml 缺少 {var}")

    def test_both_services_clear_proxy(self):
        """两个服务都要清代理变量。

        Caddy 走代理会导致 Let's Encrypt 证书签不下来，表现为 443 一直连不上，
        而日志里只有一行 acme_client 的失败 —— 很容易误判成域名没解析。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "stack.yml").read_text(encoding="utf-8")

        # 按服务切段，各自都要有 NO_PROXY
        for service in ("buddycron", "caddy"):
            idx = text.index(f"  {service}:")
            next_idx = text.find("\n  # ---", idx + 1)
            block = text[idx:next_idx if next_idx > 0 else len(text)]
            self.assertIn("NO_PROXY", block, f"{service} 没有清代理变量")

    def test_stack_image_tag_matches_version(self):
        """stack.yml 的镜像 tag 必须和 core/version.py 一致。

        这个 tag 是硬编码的默认值，发版时很容易忘改 —— 结果部署的是旧镜像，
        而页面显示的版本号是新的，排查时非常迷惑。
        """
        import re
        from pathlib import Path
        from core import version

        root = Path(__file__).resolve().parent.parent
        text = (root / "stack.yml").read_text(encoding="utf-8")
        match = re.search(r"buddycron:\$\{BUDDYCRON_TAG:-([0-9.]+)\}", text)
        self.assertIsNotNone(match, "stack.yml 里找不到镜像 tag 默认值")
        self.assertEqual(
            match.group(1),
            version.VERSION,
            f"stack.yml 的 tag ({match.group(1)}) 与版本号 ({version.VERSION}) 不一致",
        )

    def test_compose_and_stack_agree_on_essentials(self):
        """两份编排文件的关键项必须一致，否则「本地测好了、线上不对」."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        stack = (root / "stack.yml").read_text(encoding="utf-8")

        for text, name in ((compose, "docker-compose.yml"), (stack, "stack.yml")):
            # 两份都要有这些，缺一个就是「某条路径上功能不工作」
            self.assertIn("BUDDYCRON_IN_CONTAINER", text, f"{name} 缺 IN_CONTAINER")
            self.assertIn("BUDDYCRON_HOME", text, f"{name} 缺 HOME")
            self.assertIn("NO_PROXY", text, f"{name} 缺代理清理")
            self.assertIn("user:", text, f"{name} 缺非 root 用户")
            self.assertIn("/data", text, f"{name} 缺数据挂载")
        """数据卷必须能被容器写入。

        两种方式都行：
          - 命名卷（Dockerfile 里 chown 过 /data，卷初始化继承属主）
          - 绑定挂载（宿主机目录由你建，属主天然正确）

        这里只检查「用了命名卷时 Dockerfile 必须有 chown」——
        否则容器以非 root 运行时写不了，报 PermissionError。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        stack = (root / "stack.yml").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

        idx = stack.index("  buddycron:")
        block = stack[idx : stack.index("  # ---", idx)]

        if "buddycron-data:/data" in block:
            # 用命名卷时，镜像里必须 chown 过 /data
            self.assertIn("chown", dockerfile, "用命名卷时 Dockerfile 必须 chown /data")
        else:
            # 用绑定挂载时，宿主机目录的属主要和 user: 一致
            self.assertIn("./data:/data", block, "既不是命名卷也不是绑定挂载")

    def test_password_is_required_not_silent(self):
        """缺密码要明确报错，而不是默默生成一个让人找不到的随机串。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "stack.yml").read_text(encoding="utf-8")
        # compose 的 `${VAR:?msg}` 语法：缺值时直接报错并显示 msg
        self.assertIn("${BUDDYCRON_PASSWORD:?", text)

    def test_caddyfile_forwards_required_headers(self):
        """两个 header 缺一不可，缺了就是「全部 403」或「锁错人」。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("X-Forwarded-Proto", text)
        self.assertIn("X-Forwarded-For", text)

    def test_caddyfile_domain_from_env(self):
        """域名从环境变量读，不在 Caddyfile 里写死 —— 否则改域名要改两处。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("{$DOMAIN}", text)


class DockerfileTests(unittest.TestCase):
    """Dockerfile 的关键约束。这些错了只有部署时才暴露 ——
    本地跑源码永远发现不了。"""

    def test_data_dir_chowned_before_user_switch(self):
        """`/data` 必须在 Dockerfile 里 chown 成 1000。

        命名卷首次挂载到空目录时，Docker 会复制镜像里该目录的**属主**。
        不 chown 的话属主是 root:root，而 `USER 1000` 是运行时才切换的，
        晚于卷初始化 —— 于是容器写不了 /data，报 PermissionError。

        这是「命名卷 + 非 root 用户」能工作的前提。
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "Dockerfile").read_text(encoding="utf-8")

        # mkdir 那一 RUN 里必须有 chown
        mkdir_line = next(
            (line for line in text.splitlines() if "mkdir -p /data" in line), None
        )
        self.assertIsNotNone(mkdir_line, "Dockerfile 里没有 mkdir /data")
        # chown 可能在同一行或紧随的续行里
        idx = text.index("mkdir -p /data")
        block = text[idx : idx + 200]
        self.assertIn("chown", block, "/data 没有 chown，命名卷会是 root:root")

    def test_chown_happens_before_user(self):
        """chown 必须在 USER 之前 —— 顺序反了就没用（USER 之后的 RUN 以非 root 执行）。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "Dockerfile").read_text(encoding="utf-8")
        chown_pos = text.index("chown")
        user_pos = text.index("USER ")
        self.assertLess(chown_pos, user_pos, "chown 在 USER 之后，属主改不了")

    def test_runs_as_non_root(self):
        """容器不该以 root 运行 —— 这个服务持有所有账号的 token。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER 1000", text)
        self.assertNotIn("USER root", text)


if __name__ == "__main__":
    unittest.main()
