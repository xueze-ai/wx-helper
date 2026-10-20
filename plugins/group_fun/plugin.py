"""群玩法：菜单、签到、积分、排行、抽奖。

命令（群聊内发送）：
  /菜单   显示玩法菜单
  /签到   每日签到领积分，连续签到有加成（每天一次，北京时间结算）
  /积分   查看我的积分与连续签到天数
  /排行   本群积分排行榜 TOP10
  /抽奖   花积分抽奖，奖品与概率可在配置中调整

数据存在 .runtime/fun_data.sqlite3，按机器人账号+群隔离。
"""

import json
import random
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from wxbot_framework.members import BEIJING
from wxbot_framework.mentions import strip_at_labels

MENU = (
    "🤖 群玩法菜单\n"
    "/签到 —— 每日签到领积分，连续签到有加成\n"
    "/积分 —— 查看我的积分和连续签到天数\n"
    "/排行 —— 本群积分排行榜 TOP10\n"
    "/抽奖 —— 花积分抽奖，试试手气\n"
    "/菜单 —— 显示这份菜单"
)

DEFAULT_PRIZES = [
    {"name": "谢谢参与", "points": 0, "weight": 50},
    {"name": "小奖", "points": 5, "weight": 25},
    {"name": "中奖", "points": 20, "weight": 15},
    {"name": "大奖", "points": 50, "weight": 8},
    {"name": "欧皇", "points": 100, "weight": 2},
]


class FunStore:
    """积分与签到存储。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        db = sqlite3.connect(str(self.path))
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS points(
                account TEXT NOT NULL, room TEXT NOT NULL, wxid TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(account, room, wxid))""")
            db.execute("""CREATE TABLE IF NOT EXISTS signins(
                account TEXT NOT NULL, room TEXT NOT NULL, wxid TEXT NOT NULL,
                last_date TEXT NOT NULL DEFAULT '', streak INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account, room, wxid))""")
            db.commit()
        finally:
            db.close()

    def _connect(self):
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        return db

    def get_points(self, account, room, wxid):
        with self._lock:
            db = self._connect()
            try:
                row = db.execute("SELECT points FROM points WHERE account=? AND room=? AND wxid=?",
                                 (account, room, wxid)).fetchone()
                return row["points"] if row else 0
            finally:
                db.close()

    def add_points(self, account, room, wxid, delta):
        """增减积分，返回最新余额（不低于 0）。"""
        with self._lock:
            db = self._connect()
            try:
                now = datetime.now(BEIJING).isoformat(timespec="seconds")
                db.execute("""INSERT INTO points(account, room, wxid, points, updated_at)
                    VALUES(?,?,?,MAX(?,0),?)
                    ON CONFLICT(account, room, wxid) DO UPDATE SET
                    points=MAX(points.points+?,0), updated_at=?""",
                           (account, room, wxid, delta, now, delta, now))
                db.commit()
                row = db.execute("SELECT points FROM points WHERE account=? AND room=? AND wxid=?",
                                 (account, room, wxid)).fetchone()
                return row["points"]
            finally:
                db.close()

    def get_signin(self, account, room, wxid):
        with self._lock:
            db = self._connect()
            try:
                row = db.execute("SELECT last_date, streak, total FROM signins WHERE account=? AND room=? AND wxid=?",
                                 (account, room, wxid)).fetchone()
                if row:
                    return row["last_date"], row["streak"], row["total"]
                return "", 0, 0
            finally:
                db.close()

    def signin(self, account, room, wxid):
        """签到。返回 (ok, streak, total)。"""
        today = datetime.now(BEIJING).date().isoformat()
        with self._lock:
            db = self._connect()
            try:
                row = db.execute("SELECT last_date, streak, total FROM signins WHERE account=? AND room=? AND wxid=?",
                                 (account, room, wxid)).fetchone()
                if row and row["last_date"] == today:
                    return False, row["streak"], row["total"]
                if row and row["last_date"]:
                    delta = (datetime.fromisoformat(today).date()
                             - datetime.fromisoformat(row["last_date"]).date()).days
                    streak = row["streak"] + 1 if delta == 1 else 1
                else:
                    streak = 1
                total = (row["total"] if row else 0) + 1
                db.execute("""INSERT INTO signins(account, room, wxid, last_date, streak, total)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account, room, wxid) DO UPDATE SET
                    last_date=excluded.last_date, streak=excluded.streak, total=excluded.total""",
                           (account, room, wxid, today, streak, total))
                db.commit()
                return True, streak, total
            finally:
                db.close()

    def leaderboard(self, account, room, limit=10):
        with self._lock:
            db = self._connect()
            try:
                return db.execute("""SELECT wxid, points FROM points
                    WHERE account=? AND room=? AND points>0 ORDER BY points DESC LIMIT ?""",
                                  (account, room, limit)).fetchall()
            finally:
                db.close()


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        cfg = ctx.config
        db_file = cfg.get("database_file", ".runtime/fun_data.sqlite3")
        path = Path(db_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        self.store = FunStore(path)
        self.signin_points = self._int(cfg, "signin_points", 10)
        self.streak_bonus_per_day = self._int(cfg, "streak_bonus_per_day", 2)
        self.streak_bonus_cap = self._int(cfg, "streak_bonus_cap", 20)
        self.lottery_cost = self._int(cfg, "lottery_cost", 10)
        self.prizes = cfg.get("prizes", DEFAULT_PRIZES)
        if (not isinstance(self.prizes, list) or not self.prizes or
                any(not isinstance(p, dict) or not isinstance(p.get("name"), str)
                    or not isinstance(p.get("points"), int) or not isinstance(p.get("weight"), (int, float))
                    or p["weight"] <= 0 for p in self.prizes)):
            raise ValueError("group_fun.prizes 格式不正确")
        ctx.logger.info("群玩法已加载：签到+%d/天，抽奖每次 %d 积分",
                        self.signin_points, self.lottery_cost)

    @staticmethod
    def _int(cfg, key, default):
        value = cfg.get(key, default)
        if type(value) is not int or value < 0:
            raise ValueError("group_fun.%s 应为非负整数" % key)
        return value

    def on_unload(self, ctx):
        pass

    def _name(self, wxid, room):
        try:
            name = self.members.display_name(wxid, room)
        except Exception:
            name = None
        return name or wxid

    def on_message(self, message, ctx):
        if message.type != 1 or message.data.get("bot_generated"):
            return
        if not message.is_group or message.chat_id not in self.members.groups:
            return
        # 兼容 "@机器人 /签到"：先去掉 @ 头再匹配指令
        text = strip_at_labels(message.text).strip()
        if text == "/菜单":
            ctx.reply(message, MENU)
        elif text == "/签到":
            self._signin(message, ctx)
        elif text == "/积分":
            self._points(message, ctx)
        elif text == "/排行":
            self._ranking(message, ctx)
        elif text == "/抽奖":
            self._lottery(message, ctx)

    def _signin(self, message, ctx):
        account, room, wxid = self.members.account, message.chat_id, message.sender_id
        ok, streak, total = self.store.signin(account, room, wxid)
        if not ok:
            ctx.reply_at(message, "今天已经签到过了，明天再来吧")
            return
        bonus = min(self.streak_bonus_per_day * (streak - 1), self.streak_bonus_cap)
        gained = self.signin_points + bonus
        balance = self.store.add_points(account, room, wxid, gained)
        text = "签到成功！连续签到 %d 天（累计 %d 天），+%d 积分，当前 %d 积分" % (streak, total, gained, balance)
        if bonus:
            text += "（含连续加成 +%d）" % bonus
        ctx.reply_at(message, text)
        ctx.logger.info("签到：room=%s user=%s streak=%d +%d", room, wxid, streak, gained)

    def _points(self, message, ctx):
        account, room, wxid = self.members.account, message.chat_id, message.sender_id
        pts = self.store.get_points(account, room, wxid)
        _, streak, total = self.store.get_signin(account, room, wxid)
        ctx.reply_at(message, "当前 %d 积分，连续签到 %d 天（累计 %d 天）" % (pts, streak, total))

    def _ranking(self, message, ctx):
        rows = self.store.leaderboard(self.members.account, message.chat_id, 10)
        if not rows:
            ctx.reply(message, "还没有人有积分，快来 /签到 吧")
            return
        lines = ["🏆 积分排行榜 TOP%d" % len(rows)]
        for i, row in enumerate(rows, 1):
            lines.append("%d. %s —— %d 分" % (i, self._name(row["wxid"], message.chat_id), row["points"]))
        ctx.reply(message, "\n".join(lines))

    def _draw(self):
        total = sum(p["weight"] for p in self.prizes)
        hit = random.uniform(0, total)
        acc = 0.0
        for prize in self.prizes:
            acc += prize["weight"]
            if hit < acc:
                return prize
        return self.prizes[-1]

    def _lottery(self, message, ctx):
        account, room, wxid = self.members.account, message.chat_id, message.sender_id
        balance = self.store.get_points(account, room, wxid)
        if balance < self.lottery_cost:
            ctx.reply_at(message, "积分不足，抽奖每次需要 %d 积分，当前 %d 积分，先去 /签到 吧"
                         % (self.lottery_cost, balance))
            return
        prize = self._draw()
        balance = self.store.add_points(account, room, wxid, -self.lottery_cost)
        if prize["points"] > 0:
            balance = self.store.add_points(account, room, wxid, prize["points"])
            text = "🎉 恭喜抽中「%s」+%d 积分！当前 %d 积分" % (prize["name"], prize["points"], balance)
        else:
            text = "很遗憾，「%s」，下次好运！当前 %d 积分" % (prize["name"], balance)
        ctx.reply_at(message, text)
        ctx.logger.info("抽奖：room=%s user=%s prize=%s", room, wxid, prize["name"])
