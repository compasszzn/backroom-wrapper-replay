#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backrooms 内挂（fullclear）：固定种子收齐 8 页 + 喝光 4 瓶杏仁水 + 推开出口门
走进亮光逃脱（state=won），全程自动录制 MP4 + log。

「内挂」的含义：它不是只看画面的玩家模型——它连的是 harness 的 numeric 信道
（桥接每帧推送的结构化状态：位置/朝向/页数/提示/实体），并且拿种子在游戏外
重建整张迷宫（bot/levelgen.py，扩展到杏仁水/出口门/实体出生格），从而**提前
知道每张页钉在哪面墙上、每瓶水放在哪块地上、出口门在哪、实体会从哪出生**。
输入仍走 wrapper 的标准键鼠通道（client.act），与真实玩家同一条链路。

一局的完整流程（由本文件编排）：
  1. 连 MODEB socket 与控制 socket；
  2. reset(seed=N) 固定随机，离线重建迷宫并校验出生点；
  3. rec_start 自动开录；
  4. 巡回：贪心近邻依次访问 8 页 + 4 水（走路距离最近的未收物资）；
     · 实体苏醒（首页后或开局 45s 到点）→ 每个控制周期评估威胁；
     · 威胁超阈值 → 规避：BFS 距离场选「离实体远 + 无视线」的撤离点，
       chase 贴近时冲刺（体力见底自动放弃冲刺改步行）；
  5. 8 页 + 4 水齐 → 走到出口门前按 [E] PUSH THE DOOR；
  6. exit_open → 冲进门内 1.05m，游戏 state=won 即逃脱成功；
  7. rec_stop + rec_keep，bot_log.jsonl / summary.json 写进录制目录。

用法（一般不直接跑，由 run.sh --strategy 02-full-clear-escape 调起）：
  python3 fullclear.py --socket <modeB.sock> --control <control.sock> \
      --modeb-dir ../../../../Backroom-Wrapper/modeB --seed 1234
"""
import argparse
import json
import math
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from levelgen import Level, CELL  # noqa: E402

# ── 游戏本体常数（app/game/engine/*.ts，勿凭感觉改） ─────────────────────
EYE_HEIGHT = 1.62          # 相机高度（player.ts）
WALK_SPEED = 2.7           # 步行 m/s
SPRINT_SPEED = 4.7         # 冲刺 m/s（实体 chase 4.55 —— 比它快一丝）
MOUSE_SENS = 0.0021        # 鼠标灵敏度：弧度/像素（player.onMouseDelta）
TURN_CHUNK_PX = 280        # 单个鼠标事件的有效上限（onMouseDelta 的 clamp）
CENTER_X = 512             # 光标虚拟原点（1024x768 的中心）
CENTER_Y = 384
PAGE_REACH = 2.6           # 取页提示的最大相机距离（items.findInteractable）
WATER_REACH = 2.4          # 喝水提示的最大相机距离
DOOR_REACH = 3.0           # 开门提示的最大相机距离
PROMPT_PAGE = "TAKE PAGE"
PROMPT_WATER = "DRINK ALMOND WATER"
PROMPT_DOOR = "PUSH THE DOOR"
PROMPT_ESCAPE = "ESCAPE"

# ── 实体威胁模型（entity.ts 的状态机 + 速度，两轮实测校准） ───────────────
# roam 1.3 / stalk 2.15 / chase 4.55 / search 2.4，击杀距离 1.3；
# chase 的放弃条件是「失明 3.5s 且距离 > 11m」——被贴身后直线逃跑每秒只拉开
# 0.15m，永远凑不够 11m。所以：绝不许它进 15m；进了就先拐角断视线再拉远。
# 另有「恐怖导演」：离玩家 > 50m 满 30s 会把实体传送到玩家 6~9 格外（无视线处），
# 实测第一轮它在 3 秒内从 70m 出现在 17m —— 1 级（注意带）绝不能当没看见。
THREAT_CHASE_ALWAYS = 40    # chase 状态一律视为威胁（>40m 它自己也快放弃了）
THREAT_STALK = 24           # stalk：它已注意到我们并以 2.15 m/s 接近
THREAT_ROAM_L2 = 15         # roam：贴这么近随时会注意到（注意半径 22 有视线时）
THREAT_ROAM_L1 = 24         # roam：注意带——只观察距离趋势，逼近就升级
THREAT_SEARCH_L2 = 16       # search：有视线且 <20 会重新 chase，贴脸必规避
THREAT_SEARCH_L1 = 24
THREAT_ABS_CLOSE = 10       # 任何状态贴到 10m 内一律强制规避
EVADE_CLEAR_DIST = 26       # roam/search 的解除线（stalk 单独在 threat_clear 里
                             # 按状态分档——它的放弃线是 36m，推不过去就是永久缠斗）
EVADE_BORDER_PENALTY = 4    # 撤离点对边界环格的惩罚——「离实体最远」的格子往往
                             # 是地图死角，被它一堵就没路了（实测：被引进东南角围死）
EVADE_MAX_SECONDS = 150    # 单次规避的时长护栏（防对峙死循环）
EVADE_MIN_STEP_CELLS = 2   # 撤离点至少离自己 2 格——躲墙角不动=对峙死锁
SPRINT_IF_CHASE = 32       # chase/stalk 且 <32m：冲刺甩开（walk 2.7 甩不掉 stalk 2.15）
SPRINT_IF_ANY = 7           # 任何状态 <7m：紧急冲刺
SPRINT_MIN_STAMINA = 0.10   # 体力低于此不再冲刺（防 exhausted 跛行 1.89 m/s）

TOTAL_PAGES = 8
TOTAL_WATERS = 4


def wrap_angle(a):
    """归一化到 (-π, π]。"""
    while a > math.pi:
        a -= 2 * math.pi
    while a <= -math.pi:
        a += 2 * math.pi
    return a


def yaw_toward(px, pz, tx, tz):
    """朝向 (tx,tz) 该有的 yaw。前向 = (-sin yaw, -cos yaw) ⇒ yaw = atan2(-dx,-dz)。"""
    return math.atan2(-(tx - px), -(tz - pz))


class Control:
    """harness 控制socket 的极简客户端（录制这类只给人用的操作在这里）。"""

    def __init__(self, path):
        self.path = path

    def call(self, op, **args):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(300)
        s.connect(self.path)
        try:
            f = s.makefile("rwb")
            f.write((json.dumps({"op": op, "args": args}) + "\n").encode())
            f.flush()
            line = f.readline()
        finally:
            s.close()
        d = json.loads(line)
        if not d.get("ok"):
            raise RuntimeError("控制操作 %s 失败：%s" % (op, d.get("error")))
        return d


class FullClearBot:
    def __init__(self, args):
        self.args = args
        self.seed = args.seed
        self.max_seconds = args.max_seconds
        sys.path.insert(0, args.modeb_dir)
        from client import Game  # noqa: E402 — wrapper 的标准客户端
        self.Game = Game
        self.g = Game(args.socket)
        self.control = Control(args.control)
        self.level = None
        self.rec_id = None
        self.rec_dir = None
        self.t0 = time.time()          # 墙钟（不受游戏速度影响）
        self.game_t0 = None            # 开局时的 obs.time
        # 光标模型：必须与页面侧 bridge 的 lastMouse 一一对应（差分算视角增量）。
        # 策略 02 要瞄准地上的水瓶/门牌，纵向（pitch）也得走同一套代数。
        self.cursor_x = None
        self.cursor_y = None
        self.keys_held = set()
        self.log_rows = []
        # ── 巡回状态 ──
        self.pages_taken = []          # 已取页索引（顺序即巡回顺序）
        self.waters_drunk = []         # 已喝水索引
        self.pages_failed = set()
        self.waters_failed = set()
        self.retry_round = 0
        self.exit_open_seen = False
        # ── 实体遭遇记录 ──
        self.entity_ever_active = False
        self.evade_count = 0
        self.entity_d_hist = []         # 最近几个周期的实体距离（逼近趋势用）
        self.closest_entity_dist = None
        self.cycles = 0
        self.reset_seconds = None
        self.last_progress_log = 0.0

    # ── 日志 ────────────────────────────────────────────────────────────
    def log(self, row):
        row["t_wall"] = round(time.time() - self.t0, 3)
        self.log_rows.append(row)
        if row.get("type") == "milestone":
            print("[bot] %s %s" % (row["name"], json.dumps(
                {k: v for k, v in row.items() if k not in ("type", "t_wall", "name")},
                ensure_ascii=False)))

    def dump_logs(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "bot_log.jsonl"), "w", encoding="utf-8") as f:
            for row in self.log_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def base_summary(self, success, error=None):
        s = {
            "run": os.environ.get("REPLAY_RUN", ""),
            "strategy": "02-full-clear-escape",
            "seed": self.seed,
            "success": success,
            "pages_goal": TOTAL_PAGES,
            "pages_taken": [[i, [round(v, 2) for v in self.level.page_spots[i]["pos"]]]
                            for i in self.pages_taken] if self.level else [],
            "pages_abandoned": sorted(self.pages_failed),
            "waters_goal": TOTAL_WATERS,
            "waters_drunk": [[i, [round(v, 2) for v in self.level.water_spots[i]]]
                             for i in self.waters_drunk] if self.level else [],
            "waters_abandoned": sorted(self.waters_failed),
            "exit_open": self.exit_open_seen,
            "escaped": success,
            "entity_ever_active": self.entity_ever_active,
            "entity_closest_dist": (round(self.closest_entity_dist, 2)
                                    if self.closest_entity_dist is not None else None),
            "evade_episodes": self.evade_count,
            "decision_cycles": self.cycles,
            "note": "迷宫/页/水/出口门/实体出生由种子离线重建（bot/levelgen.py，对照上游 "
                    "level.ts 移植到第 9 步）；灯光爆闪/实体抖动等 Math.random 装饰性随机"
                    "不在固定范围内（见 SCOPE.md）",
        }
        if error is not None:
            s["error"] = str(error)
        return s

    def cycle_log(self, obs, phase, **extra):
        p = obs.get("player") or {}
        self.cycles += 1
        ent = obs.get("entity")
        if ent and (self.closest_entity_dist is None
                    or ent.get("dist", 1e9) < self.closest_entity_dist):
            self.closest_entity_dist = ent.get("dist")
        if ent:
            # 距离趋势史（每周期恰一次，供 threat() 的逼近升级判断）
            self.entity_d_hist.append(ent.get("dist"))
            if len(self.entity_d_hist) > 6:
                self.entity_d_hist.pop(0)
        self.log({
            "type": "cycle", "phase": phase,
            "t_game": obs.get("time"), "iter": obs.get("iter"),
            "state": obs.get("state"), "pages": obs.get("pages"),
            "exit_open": obs.get("exit_open"), "prompt": obs.get("prompt"),
            "player": {"x": p.get("x"), "z": p.get("z"), "yaw": p.get("yaw"),
                       "pitch": p.get("pitch"), "speed": p.get("speed"),
                       "stamina": p.get("stamina"), "sprinting": p.get("sprinting")},
            "entity": ent,
            **extra,
        })
        if ent and not self.entity_ever_active:
            self.entity_ever_active = True
            # 苏醒瞬间的观测位置 vs 移植预测的出生格 —— 移植对不对这里见分晓
            pred = self.level.entity_spawn_cell if self.level else None
            pred_pos = (self.level.world_x(pred[0]), self.level.world_z(pred[1])) \
                if self.level else (None, None)
            err = (math.hypot(ent.get("x", 0) - pred_pos[0], ent.get("z", 0) - pred_pos[1])
                   if pred_pos[0] is not None else None)
            self.log({"type": "milestone", "name": "entity_active",
                      "entity": ent, "pages": obs.get("pages"),
                      "predicted_spawn_cell": pred, "predicted_spawn_pos":
                      [round(v, 2) for v in pred_pos] if pred_pos[0] is not None else None,
                      "spawn_err_m": round(err, 2) if err is not None else None,
                      "note": "实体已苏醒（首页后自动激活，或开局 45 秒到点）"})
        # 周期性进度里程碑（~30s 一条，方便人读日志）
        now = time.time() - self.t0
        if now - self.last_progress_log >= 30:
            self.last_progress_log = now
            self.log({"type": "milestone", "name": "progress",
                      "pages": obs.get("pages"),
                      "pages_taken": len(self.pages_taken),
                      "waters_drunk": len(self.waters_drunk),
                      "player": [round(p.get("x", 0), 1), round(p.get("z", 0), 1)],
                      "entity": ent})
        return obs

    # ── 观测与输入 ──────────────────────────────────────────────────────
    def observe(self):
        """最新一帧桥接观测（realtime 下由页面每帧推送，不推进时间）。

        harness 的 numeric 档把桥接观测包了一层：
        client.observe() 返回 {"t_ms", "frame", "state": <桥接观测>}，
        桥接观测自己才有 state/player/pages/…… —— 这里拆包，之后字段平铺可用。
        """
        r = self.g.observe()
        if isinstance(r, dict) and isinstance(r.get("state"), dict) and "world" in r["state"]:
            return r["state"]
        return r

    def wait_playing(self, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            obs = self.observe()
            if obs.get("state") == "playing":
                if self.game_t0 is None:
                    self.game_t0 = obs.get("time") or 0.0
                return obs
            self.g.act([], advance_ms=200)
        raise RuntimeError("%d 秒内游戏没有进入 playing（state=%r）"
                           % (timeout, self.observe().get("state")))

    def budget_left(self):
        return self.max_seconds - (time.time() - self.t0)

    def check_alive(self, obs):
        if obs.get("state") in ("dying", "dead"):
            raise RuntimeError("玩家死了（state=%r，实体距离 %s）"
                               % (obs.get("state"), (obs.get("entity") or {}).get("dist")))
        if obs.get("state") == "paused":
            raise RuntimeError("游戏意外进入暂停——没有发过 Escape，这是异常路径")

    # ── 键鼠注入 ────────────────────────────────────────────────────────
    def _sync_cursor(self):
        """reset 后页面侧 lastMouse 为空：先发一次绝对坐标把光标模型对齐（无旋转）。"""
        if self.cursor_x is not None:
            return
        self.g.act([{"type": "mouse_move", "x": CENTER_X, "y": CENTER_Y}], advance_ms=60)
        self.cursor_x, self.cursor_y = CENTER_X, CENTER_Y

    def _mouse_events_for_delta(self, dx_total, dy_total=0):
        """把 (dx,dy) 像素的鼠标位移拆成事件序列（横纵同一套代数）。

        页面侧 bridge 用相邻两次绝对坐标的差当 movementX/Y 送进
        player.onMouseDelta；游戏把 |dx|>600 或 |dy|>600 当垃圾事件**整个丢弃**
        （不旋转），|d|≤280 的按 0.0021 弧度/像素旋转。所以：
          · 旋转按 ≤280px 的块发，每块都全额生效；
          · 光标某轴漂出中心 600px 后，先发一次「回中」事件——该轴位移必然 >600，
            被游戏丢弃，但页面侧 lastMouse 更新了，光标回中。
          · 一条事件可以同时携带横纵两个增量（游戏对两轴各自 clamp）。
        """
        events = []
        rx, ry = int(dx_total), int(dy_total)
        while rx != 0 or ry != 0:
            if abs(self.cursor_x - CENTER_X) > 600:
                events.append({"type": "mouse_move", "x": CENTER_X, "y": self.cursor_y})
                self.cursor_x = CENTER_X
            if abs(self.cursor_y - CENTER_Y) > 600:
                events.append({"type": "mouse_move", "x": self.cursor_x, "y": CENTER_Y})
                self.cursor_y = CENTER_Y
            dx = max(-TURN_CHUNK_PX, min(TURN_CHUNK_PX, rx))
            dy = max(-TURN_CHUNK_PX, min(TURN_CHUNK_PX, ry))
            self.cursor_x += dx
            self.cursor_y += dy
            events.append({"type": "mouse_move", "x": self.cursor_x, "y": self.cursor_y})
            rx -= dx
            ry -= dy
        return events

    def aim_events(self, cur_yaw, cur_pitch, tgt_yaw, tgt_pitch):
        """从当前视角转到目标视角需要的事件。yaw -= dx*sens；pitch -= dy*sens。"""
        dyaw = wrap_angle(tgt_yaw - cur_yaw)
        dpitch = tgt_pitch - cur_pitch   # pitch 无环绕，天然在 ±(π/2-0.06) 内
        return self._mouse_events_for_delta(int(round(-dyaw / MOUSE_SENS)),
                                            int(round(-dpitch / MOUSE_SENS)))

    def key_event(self, key, down):
        """维护按键状态表，返回 down/up 事件（重复按下会被跳过）。"""
        want = "key_down" if down else "key_up"
        if down and key in self.keys_held:
            return []
        if not down and key not in self.keys_held:
            return []
        (self.keys_held.add if down else self.keys_held.discard)(key)
        return [{"type": want, "key": key}]

    def release_all(self):
        evs = []
        for k in list(self.keys_held):
            evs.extend(self.key_event(k, False))
        if evs:
            self.g.act(evs, advance_ms=60)

    # ── 走路 ────────────────────────────────────────────────────────────
    def _immediate_target(self, pos, path):
        """从当前真实位置出发，取路径上直线可走的**最远**点（拉直走，少拐弯）。"""
        for i in range(len(path) - 1, -1, -1):
            if self.level.corridor_clear(pos[0], pos[1], path[i][0], path[i][1]):
                return path[i]
        for pt in path:
            if self.level.corridor_clear(pos[0], pos[1], pt[0], pt[1]):
                return pt
        return path[0]

    def walk_to(self, target, arrive_r=0.5, phase="nav", threat_abort=True,
                sprint=False, max_cycles=None, keep_moving=False):
        """反馈式走到 target：观测→修正视角→按 w 前进，每圈 ~300ms。

        返回：到达（或游戏已 won）时返回最新观测；威胁中止 / 圈数用尽返回 None。
        sprint=True 时按住 Shift（体力低于 SPRINT_MIN_STAMINA 自动放弃）。
        keep_moving=True 时**不停车转身**——边走边转（规避/被追时停车=送死），
        转向精度略降换速度，卡位自救仍会照常触发。
        """
        last_cell = None
        path = None
        stall_pos, stall_t = None, 0.0
        n_cycles = 0
        while True:
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽（%ds）" % self.max_seconds)
            obs = self.observe()
            if obs.get("state") == "won":
                self.release_all()
                return obs
            self.check_alive(obs)
            if threat_abort:
                lvl, _ent = self.threat(obs)
                if lvl >= 2:
                    self.release_all()
                    self.cycle_log(obs, phase, action="threat_abort", target=list(target))
                    return None
            n_cycles += 1
            if max_cycles is not None and n_cycles > max_cycles:
                self.release_all()
                return None
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            dist = math.hypot(target[0] - pos[0], target[1] - pos[1])
            if dist <= arrive_r:
                self.cycle_log(obs, phase, target=list(target), dist=dist,
                               action="arrived")
                evs = self.key_event("w", False)
                if evs:
                    self.g.act(evs, advance_ms=60)
                return obs
            # 迷宫格变了或还没有路径 → 重规划
            cell = self.level.cell_of(*pos)
            if path is None or last_cell != cell:
                tgt_cell = self.level.cell_of(*target)
                cells = self.level.find_path(cell, tgt_cell)
                if cells is None:
                    raise RuntimeError("寻路失败：%s → %s（目标不可达？）" % (cell, tgt_cell))
                path = cells + [target]
                last_cell = cell
            aim = self._immediate_target(pos, path)
            aim_dist = math.hypot(aim[0] - pos[0], aim[1] - pos[1])
            if aim_dist < 0.35:   # 拉直点贴脸了，直接瞄真目标
                aim = target
            desired = yaw_toward(pos[0], pos[1], aim[0], aim[1])
            dyaw = wrap_angle(desired - p.get("yaw"))
            events = []
            # 冲刺管理：体力不够就改步行（exhausted 跛行 1.89 m/s 比 walk 还慢）
            want_sprint = sprint and (p.get("stamina") or 1) > SPRINT_MIN_STAMINA
            if abs(dyaw) > 0.55 and not keep_moving:   # ≈31°：先停下原地转身
                events.extend(self.key_event("w", False))
                events.extend(self.aim_events(p.get("yaw"), p.get("pitch") or 0.0,
                                               desired, p.get("pitch") or 0.0))
                self.cycle_log(obs, phase, target=list(aim), dist=dist, dyaw=round(dyaw, 3),
                               action="turn_in_place", sprint=want_sprint)
                self.g.act(events, advance_ms=max(120, int(abs(dyaw) * 130)))
                continue
            events.extend(self.aim_events(p.get("yaw"), p.get("pitch") or 0.0,
                                          desired, p.get("pitch") or 0.0))
            events.extend(self.key_event("w", True))
            events.extend(self.key_event("Shift_L", want_sprint))
            # 停滞检测：2.5 秒内挪不动就要自救（卡墙角/卡门框）
            now = time.time()
            if stall_pos is None or math.hypot(pos[0] - stall_pos[0],
                                               pos[1] - stall_pos[1]) > 0.2:
                stall_pos, stall_t = pos, now
            elif now - stall_t > 2.5:
                self.cycle_log(obs, phase, target=list(aim), dist=dist,
                               action="stall_recovery")
                self._unstick()
                path, last_cell = None, None
                stall_pos, stall_t = None, 0.0
                continue
            self.cycle_log(obs, phase, target=list(aim), dist=dist,
                           dyaw=round(dyaw, 3), action="walk_steer",
                           speed=p.get("speed"), sprint=want_sprint)
            self.g.act(events, advance_ms=300)

    def _unstick(self):
        """卡住自救：倒 0.4s，随机侧转 90°，向前 0.7s，然后重规划。"""
        side = 1 if (time.time() * 1000) % 2 < 1 else -1
        self.g.act(self.key_event("s", True), advance_ms=400)
        obs = self.observe()
        self.g.act(self.key_event("s", False)
                   + self.aim_events((obs.get("player") or {}).get("yaw"),
                                     (obs.get("player") or {}).get("pitch") or 0.0,
                                     (obs.get("player") or {}).get("yaw") + side * math.pi / 2,
                                     (obs.get("player") or {}).get("pitch") or 0.0),
                   advance_ms=150)
        self.g.act(self.key_event("w", True), advance_ms=700)
        self.g.act(self.key_event("w", False), advance_ms=60)

    # ── 实体威胁与规避 ──────────────────────────────────────────────────
    def threat(self, obs):
        """威胁分级。返回 (level, entity)：0 无 / 1 注意 / 2 必须规避。
        1 级只在距离趋势上「连续逼近」时升级为 2——恐怖导演的传送经常把它
        直接扔到 20m 出头的地方，趋势比阈值先一步暴露意图。"""
        ent = obs.get("entity")
        if not ent:
            return 0, None
        st = ent.get("state")
        d = ent.get("dist")
        if d is None:
            return 0, ent
        if st == "chase":
            lvl = 2 if d < THREAT_CHASE_ALWAYS else 1
        elif st == "stalk":
            lvl = 2 if d < THREAT_STALK else (1 if d < THREAT_STALK + 5 else 0)
        elif st == "roam":
            lvl = 2 if d < THREAT_ROAM_L2 else (1 if d < THREAT_ROAM_L1 else 0)
        elif st == "search":
            lvl = 2 if d < THREAT_SEARCH_L2 else (1 if d < THREAT_SEARCH_L1 else 0)
        else:
            lvl = 0
        if lvl == 1 and self._entity_closing():
            lvl = 2
        if d < THREAT_ABS_CLOSE:
            lvl = 2
        return lvl, ent

    def _entity_closing(self):
        """实体是否在连续逼近：最近 3 个周期距离单调下降且累计 ≥1m。
        距离史由 cycle_log 每周期追加（每周期恰好一次，不会重复计数）。"""
        h = self.entity_d_hist
        if len(h) < 3:
            return False
        a, b, c = h[-3], h[-2], h[-1]
        return c < b < a and (a - c) >= 1.0

    def threat_clear(self, obs):
        """规避解除条件（滞回防抖动）。**按状态分档**（实测教训）：
        · stalk 的放弃线是 36m——推不到 36 它永远跟着（2.15 m/s 纠缠不休），
          所以 stalk 必须撤过 38 才算解除，用冲刺一口气推过线让它回 roam；
        · search 7 秒后自己回 roam，重新 chase 需要有视线且 <20——26m 外安全；
        · roam 只有 1.3 m/s 追不上步行，注意半径 22（有视线）——26m 留余量；
        · chase 只能等它自己放弃（失明 3.5s 且 >11m），期间持续规避。"""
        ent = obs.get("entity")
        if not ent:
            return True
        st = ent.get("state")
        d = ent.get("dist") or 0
        if st == "chase":
            return False
        if st == "stalk":
            return d >= 38
        return d >= EVADE_CLEAR_DIST   # roam / search

    def pick_evade_target(self, pos, ent):
        """撤离点分两档选（都必须离自己 ≥2 格——实测教训：允许贴身「躲墙角」
        时，walk_to 立即到站、规避循环零睡眠自旋，玩家原地站到超时）：
        ① 现在能被它看见 → 12 步内找**无视线**的格子（拐角），实体 BFS 距离加权
          —— chase 的放弃条件是「失明 3.5s 且 >11m」，先断视线才谈得上拉远；
          躲点离实体 BFS 距离须 ≥6，别贴着它隔墙对峙；
        ② 已经看不见它（或 12 步内没有拐角）→ 18 步内找「离实体 BFS 最远」的
          格子继续拉大（粗排前 48 再精算视线，LOS 限制在小集合里）。
        BFS 步数比直线距离诚实——墙越多它绕得越久。"""
        S = self.level.SIZE
        ec = self.level.cell_of(ent.get("x", 0), ent.get("z", 0))
        pc = self.level.cell_of(*pos)
        efield = self.level.distance_field(ec)
        pfield = self.level.distance_field(pc)

        def border_pen(x, z):
            # 贴边界的格是死角高发区：离实体最远的格子经常是地图角，
            # 被它一堵门就出不来（实测：东南角围杀）
            return EVADE_BORDER_PENALTY if min(x, z, S - 1 - x, S - 1 - z) <= 1 else 0

        seen = self.level.line_of_sight_cells(pc[0], pc[1], ec[0], ec[1])
        if seen:
            best, best_score = None, None
            for z in range(S):
                prow = pfield[z * S:(z + 1) * S]
                erow = efield[z * S:(z + 1) * S]
                for x in range(S):
                    pd = prow[x]
                    if pd < EVADE_MIN_STEP_CELLS or pd > 12:
                        continue
                    if erow[x] < 6:
                        continue          # 离它太近的「躲点」=对峙死锁
                    if self.level.line_of_sight_cells(x, z, ec[0], ec[1]):
                        continue          # 还是被看得见，不算拐角
                    score = min(erow[x], 25) - 0.35 * pd - border_pen(x, z)
                    if best_score is None or score > best_score:
                        best, best_score = (x, z), score
            if best is not None:
                return (self.level.world_x(best[0]), self.level.world_z(best[1]))
        # 拉远档：全格粗排取前 48，再对这 48 个精算视线加分
        top = []
        for z in range(S):
            prow = pfield[z * S:(z + 1) * S]
            erow = efield[z * S:(z + 1) * S]
            for x in range(S):
                pd = prow[x]
                if pd < EVADE_MIN_STEP_CELLS + 1 or pd > 18:
                    continue
                ed = erow[x]
                if ed < 0:
                    ed = 99      # 实体到不了（理论上不存在，保险）
                base = min(ed, 30) - 0.45 * pd - border_pen(x, z)
                if len(top) < 48:
                    top.append((base, x, z))
                    if len(top) == 48:
                        top.sort()
                elif base > top[0][0]:
                    top[0] = (base, x, z)
                    top.sort()
        best, best_score = None, None
        for base, x, z in top:
            los = self.level.line_of_sight_cells(x, z, ec[0], ec[1])
            score = base + (6 if not los else 0)
            if best_score is None or score > best_score:
                best, best_score = (x, z), score
        if best is None:
            return None
        return (self.level.world_x(best[0]), self.level.world_z(best[1]))

    def evade(self, obs):
        """规避循环：选撤离点→走过去→重复直到威胁解除（滞回：撤到 30m 外）。
        **每圈强制 ≥150ms 推进**——observe() 在流式档是零等待的（返回缓存帧），
        撤离点若在到站半径内，walk_to 会立即返回，没有这口气循环就退化成
        零睡眠自旋：CPU 满转、玩家原地罚站到超时（实测教训）。
        单次规避超过 EVADE_MAX_SECONDS 强制返回（防对峙死循环烧预算）。"""
        self.evade_count += 1
        t_start = time.time()
        ent = obs.get("entity") or {}
        self.log({"type": "milestone", "name": "evade_start",
                  "episode": self.evade_count,
                  "entity": {"state": ent.get("state"), "dist": ent.get("dist"),
                             "x": ent.get("x"), "z": ent.get("z")}})
        while True:
            t_iter = time.time()
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽（规避中）")
            obs = self.observe()
            if obs.get("state") == "won":
                return
            self.check_alive(obs)
            ent = obs.get("entity")
            if self.threat_clear(obs):
                self.log({"type": "milestone", "name": "evade_end",
                          "episode": self.evade_count,
                          "duration_s": round(time.time() - t_start, 1),
                          "entity": {"state": (ent or {}).get("state"),
                                      "dist": (ent or {}).get("dist")}})
                return
            if time.time() - t_start > EVADE_MAX_SECONDS:
                self.log({"type": "milestone", "name": "evade_timeout",
                          "episode": self.evade_count,
                          "duration_s": round(time.time() - t_start, 1),
                          "note": "单次规避超时，强制返回巡回（威胁仍在）"})
                return
            if not ent:
                return
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            target = self.pick_evade_target(pos, ent)
            if target is None:
                # 理论到不了这里（迷宫连通）。兜底：回自己格心（walk_to 立即到站，
                # 下一圈重新选点）——绝不能拿一个可能落在墙里的裸坐标去寻路。
                target = pos
            st = ent.get("state")
            d = ent.get("dist") or 999
            # stalk 2.15 / chase 4.55 都比步行快——被盯上就得冲刺拉开；
            # 体力低于 SPRINT_MIN_STAMINA 时 walk_to 内部会自动放弃冲刺
            want_sprint = (st in ("chase", "stalk") and d < SPRINT_IF_CHASE) \
                or d < SPRINT_IF_ANY
            self.cycle_log(obs, "evade", action="evade_replan",
                           evade_target=[round(target[0], 1), round(target[1], 1)],
                           sprint=want_sprint)
            self.walk_to(target, arrive_r=0.8, phase="evade", threat_abort=False,
                         sprint=want_sprint, max_cycles=6, keep_moving=True)
            # 防自旋：这一圈若没花掉 150ms，就强制推进一段——游戏时钟必须走
            if time.time() - t_iter < 0.15:
                self.g.act([], advance_ms=200)

    # ── 巡回目标选择 ────────────────────────────────────────────────────
    def pick_target(self, pos, ent=None):
        """贪心近邻：在未收的页 + 水里选走路距离最近的一个（平局取直线更近）。
        ent 非 None（1 级威胁下继续巡回）时先剔除「朝它走过去」的物资——
        实体的 BFS 距离必须不小于我们当前的距离，否则每次规避一结束
        又一头撞回去，evade↔巡回 无限震荡；全被剔除就取离它最远的那个。"""
        cell = self.level.cell_of(*pos)
        if ent is not None:
            ec = self.level.cell_of(ent.get("x", 0), ent.get("z", 0))
            efield = self.level.distance_field(ec)
            my_ed = efield[cell[1] * self.level.SIZE + cell[0]]
        else:
            efield = None
            my_ed = None

        def cand_ed(item_cell):
            return efield[item_cell[1] * self.level.SIZE + item_cell[0]] \
                if efield is not None else None

        candidates = []
        for i, pg in enumerate(self.level.page_spots):
            if i in self.pages_taken or i in self.pages_failed:
                continue
            d = self.level.walk_distance(cell, pg["cell"])
            if d is None:
                continue
            candidates.append(("page", i, pg["cell"], d,
                               math.hypot(pg["pos"][0] - pos[0], pg["pos"][2] - pos[1])))
        for i, w in enumerate(self.level.water_spots):
            if i in self.waters_drunk or i in self.waters_failed:
                continue
            d = self.level.walk_distance(cell, self.level.cell_of(*w))
            if d is None:
                continue
            candidates.append(("water", i, self.level.cell_of(*w), d,
                               math.hypot(w[0] - pos[0], w[1] - pos[1])))
        if not candidates:
            return None
        if efield is not None:
            safe = [c for c in candidates
                    if cand_ed(c[2]) is not None and cand_ed(c[2]) >= my_ed]
            if safe:
                candidates = safe
            else:
                candidates = sorted(candidates,
                                   key=lambda c: -(cand_ed(c[2]) or -1))
        kind, idx, _c, d, eu = min(candidates, key=lambda c: (c[3], c[4]))
        return (kind, idx)

    # ── 交互：取页 ──────────────────────────────────────────────────────
    def grab_page(self, idx):
        """走到页对面、瞄准、等 [E] TAKE PAGE 提示出现时取页。
        返回 taken / threat（主循环先规避再回来）/ failed。"""
        page = self.level.page_spots[idx]
        px, py, pz = page["pos"]
        nx, nz = page["normal"]
        stand = (px + nx * 1.35, pz + nz * 1.35)
        r = self.walk_to(stand, arrive_r=0.4, phase="page_approach")
        if r is None:
            return "threat"
        for attempt in range(8):
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽")
            obs = self.observe()
            if obs.get("state") == "won":
                return "taken"      # 不会发生，保险
            self.check_alive(obs)
            lvl, _ = self.threat(obs)
            if lvl >= 2:
                return "threat"
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            if PROMPT_PAGE in (obs.get("prompt") or ""):
                self.cycle_log(obs, "page_grab", action="press_e",
                               page=[round(px, 2), round(py, 2), round(pz, 2)],
                               attempt=attempt)
                self.g.act([{"type": "key_tap", "key": "e", "hold_ms": 60}],
                           advance_ms=450)
                obs2 = self.observe()
                self.cycle_log(obs2, "page_grab", action="after_e",
                               pages=obs2.get("pages"))
                if (obs2.get("pages") or 0) > (obs.get("pages") or 0):
                    return "taken"     # 这张页确实进了口袋
                continue              # 没取上：重新瞄准再试
            # 还没有提示：把视角精确对到页上（横纵一起瞄），必要时凑近半步
            desired = yaw_toward(pos[0], pos[1], px, pz)
            horiz = math.hypot(px - pos[0], pz - pos[1])
            desired_pitch = math.atan2(py - EYE_HEIGHT, horiz) if horiz > 0.3 else 0.0
            events = self.aim_events(p.get("yaw"), p.get("pitch") or 0.0,
                                     desired, desired_pitch)
            dist = math.hypot(px - pos[0], pz - pos[1])
            if dist > PAGE_REACH - 0.35:
                events.extend(self.key_event("w", True))
                self.cycle_log(obs, "page_grab", action="aim_and_step",
                               dist=round(dist, 2), attempt=attempt)
                self.g.act(events, advance_ms=280)
            else:
                events.extend(self.key_event("w", False))   # 距离够了只转身
                self.cycle_log(obs, "page_grab", action="aim",
                               dist=round(dist, 2), attempt=attempt)
                self.g.act(events, advance_ms=200)
        return "failed"

    # ── 交互：喝杏仁水 ──────────────────────────────────────────────────
    def drink_water(self, idx):
        """走到瓶前 1.35m、俯身瞄准瓶底、等 [E] DRINK ALMOND WATER 出现时喝掉。
        喝成功的判据：提示消失（瓶子被拿走）+ 体力回满（喝水必回满）。
        返回 drunk / threat / failed。"""
        wx, wz = self.level.water_spots[idx]
        wc = self.level.cell_of(wx, wz)
        cx, cz = self.level.world_x(wc[0]), self.level.world_z(wc[1])
        # 站位：瓶底往格心方向退 1.35m。格心永远开阔；瓶→格心连线不出格，
        # 所以这段直线不穿墙。瓶就贴着格心时随便取一个固定方向。
        vx, vz = cx - wx, cz - wz
        L = math.hypot(vx, vz)
        if L < 0.3:
            vx, vz, L = 1.0, 0.0, 1.0
        stand = (wx + vx / L * 1.35, wz + vz / L * 1.35)
        r = self.walk_to(stand, arrive_r=0.3, phase="water_approach")
        if r is None:
            return "threat"
        stamina_before = None
        for attempt in range(8):
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽")
            obs = self.observe()
            self.check_alive(obs)
            lvl, _ = self.threat(obs)
            if lvl >= 2:
                self._restore_pitch(obs)
                return "threat"
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            prompt = obs.get("prompt") or ""
            if PROMPT_WATER in prompt:
                stamina_before = p.get("stamina")
                self.cycle_log(obs, "water_grab", action="press_e",
                               water=[round(wx, 2), round(wz, 2)], attempt=attempt)
                self.g.act([{"type": "key_tap", "key": "e", "hold_ms": 60}],
                           advance_ms=500)
                # 喝掉后瓶子消失：重新瞄准同一位置，提示必然不再出现
                obs2 = self.observe()
                p2 = obs2.get("player") or {}
                aim = self.aim_events(p2.get("yaw"), p2.get("pitch") or 0.0,
                                      yaw_toward(p2.get("x"), p2.get("z"), wx, wz),
                                      math.atan2(-EYE_HEIGHT,
                                                 math.hypot(wx - p2.get("x", 0),
                                                            wz - p2.get("z", 0))))
                self.g.act(aim, advance_ms=250)
                obs3 = self.observe()
                gone = PROMPT_WATER not in (obs3.get("prompt") or "")
                full = (obs3.get("player") or {}).get("stamina") is not None and \
                    (obs3.get("player") or {}).get("stamina") >= 0.999
                self.cycle_log(obs3, "water_grab", action="after_e",
                               prompt_gone=gone, stamina_full=full)
                if gone:
                    self.log({"type": "milestone", "name": "water_drunk",
                              "index": idx, "pos": [round(wx, 2), round(wz, 2)],
                              "stamina_before": stamina_before,
                              "stamina_after":
                              (obs3.get("player") or {}).get("stamina")})
                    self._restore_pitch(obs3)
                    return "drunk"
                continue              # 提示还在：没喝上，重新瞄再试
            # 还没有提示：瞄准瓶底（组原点在地面，必须低头），必要时凑近半步
            desired = yaw_toward(pos[0], pos[1], wx, wz)
            horiz = math.hypot(wx - pos[0], wz - pos[1])
            desired_pitch = math.atan2(0 - EYE_HEIGHT, horiz) if horiz > 0.2 else -1.2
            events = self.aim_events(p.get("yaw"), p.get("pitch") or 0.0,
                                     desired, desired_pitch)
            d3 = math.hypot(horiz, EYE_HEIGHT)     # 相机到瓶底的 3D 距离
            if d3 > WATER_REACH - 0.2:
                events.extend(self.key_event("w", True))
                self.cycle_log(obs, "water_grab", action="aim_and_step",
                               dist=round(d3, 2), attempt=attempt)
                self.g.act(events, advance_ms=280)
            else:
                events.extend(self.key_event("w", False))
                self.cycle_log(obs, "water_grab", action="aim",
                               dist=round(d3, 2), attempt=attempt)
                self.g.act(events, advance_ms=220)
        self._restore_pitch(self.observe())
        return "failed"

    def _restore_pitch(self, obs):
        """交互完把俯仰角归零（回水平视线），导航只关心 yaw。"""
        p = obs.get("player") or {}
        evs = self.aim_events(p.get("yaw"), p.get("pitch") or 0.0,
                              p.get("yaw"), 0.0)
        if evs:
            self.g.act(evs, advance_ms=120)

    # ── 交互：开门与逃脱 ────────────────────────────────────────────────
    def open_door(self):
        """8 页集齐后走到出口门前按 [E] PUSH THE DOOR。被实体打断就规避后重试。
        结构与取页/喝水一致：**先一次走到门口，再进独立瞄准循环**——瞄准循环里
        绝不再调 walk_to（它的朝向修正是朝站位点的，会把门瞄准打掉，视角在
        门↔站位间永远振荡，按 e 踩不到提示出现的那一刻——实测死循环根因）。"""
        ex = self.level.exit
        dx, dz = ex["door_pos"]
        fx, fz = ex["facing_in"]
        stand = (dx + fx * 1.2, dz + fz * 1.2)
        # 1) 走到门口（威胁中断 → 规避后重走，最多 6 轮）
        arrived = False
        for _approach in range(6):
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽")
            obs = self.observe()
            if obs.get("exit_open"):
                self.exit_open_seen = True
                return True
            self.check_alive(obs)
            lvl, _ = self.threat(obs)
            if lvl >= 2:
                self.evade(obs)
                continue
            r = self.walk_to(stand, arrive_r=0.6, phase="door_approach")
            if r is None:
                continue              # 路上被威胁打断 → 顶上规避后重走
            arrived = True
            break
        if not arrived:
            return False               # 交回主循环（先规避/歇口气再来）
        # 2) 站定后的瞄准循环：只瞄门 + 按 e + 必要时小步凑近
        for attempt in range(30):
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽")
            obs = self.observe()
            if obs.get("state") == "won":
                return True
            if obs.get("exit_open"):
                self.exit_open_seen = True
                self.release_all()
                return True
            self.check_alive(obs)
            lvl, _ = self.threat(obs)
            if lvl >= 2:
                self.release_all()
                return False           # 交回主循环先规避
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            prompt = obs.get("prompt") or ""
            if PROMPT_DOOR in prompt or PROMPT_ESCAPE in prompt:
                self.cycle_log(obs, "door", action="press_e", attempt=attempt)
                self.g.act([{"type": "key_tap", "key": "e", "hold_ms": 60}],
                           advance_ms=400)
                obs2 = self.observe()
                self.cycle_log(obs2, "door", action="after_e",
                               exit_open=obs2.get("exit_open"))
                if obs2.get("exit_open"):
                    self.exit_open_seen = True
                    self.log({"type": "milestone", "name": "door_opened",
                              "door_pos": [round(dx, 2), round(dz, 2)],
                              "pages": obs2.get("pages")})
                    self.release_all()
                    return True
                continue              # 没推开：重新瞄准再试
            # 还没有提示：瞄准门牌（门 pos 高 1.1m，比视线略低），必要时小步凑近
            desired = yaw_toward(pos[0], pos[1], dx, dz)
            horiz = math.hypot(dx - pos[0], dz - pos[1])
            desired_pitch = math.atan2(1.1 - EYE_HEIGHT, horiz) if horiz > 0.2 else 0.0
            events = self.aim_events(p.get("yaw"), p.get("pitch") or 0.0,
                                     desired, desired_pitch)
            if horiz > 2.0:
                # 同一格内直线凑近半步（站位与门同格，直线必不穿墙）
                events.extend(self.key_event("w", True))
                self.cycle_log(obs, "door", action="aim_and_step",
                               dist=round(horiz, 2), attempt=attempt)
                self.g.act(events, advance_ms=280)
            else:
                events.extend(self.key_event("w", False))
                self.cycle_log(obs, "door", action="aim",
                               dist=round(horiz, 2), attempt=attempt)
                self.g.act(events, advance_ms=250)
        raise RuntimeError("出口门推了 30 轮都没开（提示始终不出现）")

    def escape_walk(self):
        """exit_open 之后冲进门内。胜利判定每帧都在跑：玩家与门位 XZ 距离 < 1.05
        即 state=won。walk_to 里发现 won 会立即返回。"""
        ex = self.level.exit
        target = ex["door_pos"]
        for _leg in range(40):
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽（逃脱段）")
            obs = self.observe()
            if obs.get("state") == "won":
                return True
            self.check_alive(obs)
            ent = obs.get("entity") or {}
            # 实体贴脸且离门还远：先规避一轮再冲
            horiz_door = math.hypot(target[0] - (obs.get("player") or {}).get("x", 0),
                                    target[1] - (obs.get("player") or {}).get("z", 0))
            if (ent.get("state") == "chase" and (ent.get("dist") or 99) < 8
                    and horiz_door > 15):
                self.evade(obs)
                continue
            r = self.walk_to(target, arrive_r=1.0, phase="escape",
                             threat_abort=False, sprint=True, max_cycles=12,
                             keep_moving=True)
            if r is not None and r.get("state") == "won":
                return True
            # 12 圈没到（被墙角/卡位拖住）→ 重新规划冲刺
        raise RuntimeError("冲门 40 段都没触发 won")

    # ── 一局编排 ────────────────────────────────────────────────────────
    def play(self):
        """主循环：巡回 → 开门 → 逃脱。返回 True 当且仅当 state=won。"""
        while True:
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽（%ds）" % self.max_seconds)
            obs = self.observe()
            if obs.get("state") == "won":
                self.log({"type": "milestone", "name": "escaped",
                          "pages": obs.get("pages"),
                          "game_seconds": round((obs.get("time") or 0)
                                                 - (self.game_t0 or 0), 2)})
                return True
            self.check_alive(obs)
            if obs.get("exit_open"):
                self.exit_open_seen = True
            lvl, ent = self.threat(obs)
            if lvl >= 2:
                self.evade(obs)
                continue
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            if len(self.pages_taken) < TOTAL_PAGES or len(self.waters_drunk) < TOTAL_WATERS:
                # 1 级威胁下继续巡回，但选点避开「朝实体走」的方向
                tgt = self.pick_target(pos, ent if lvl >= 1 else None)
                if tgt is None:
                    # 只剩失败过的物资：清掉失败标记重试一轮（最多 3 轮）
                    if self.pages_failed or self.waters_failed:
                        self.retry_round += 1
                        if self.retry_round > 3:
                            raise RuntimeError(
                                "物资收不齐且重试耗尽：缺页 %s 缺水 %s"
                                % (sorted(set(range(TOTAL_PAGES)) - set(self.pages_taken)),
                                   sorted(set(range(TOTAL_WATERS)) - set(self.waters_drunk))))
                        self.log({"type": "milestone", "name": "retry_round",
                                  "round": self.retry_round,
                                  "pages_failed": sorted(self.pages_failed),
                                  "waters_failed": sorted(self.waters_failed)})
                        self.pages_failed.clear()
                        self.waters_failed.clear()
                        continue
                    raise RuntimeError("没有可选目标但物资未收齐（页 %d/8 水 %d/4）"
                                        % (len(self.pages_taken), len(self.waters_drunk)))
                kind, idx = tgt
                if kind == "page":
                    page = self.level.page_spots[idx]
                    self.log({"type": "milestone", "name": "page_target",
                              "index": idx, "pos": [round(v, 2) for v in page["pos"]],
                              "cell": page["cell"],
                              "remaining_pages": TOTAL_PAGES - len(self.pages_taken),
                              "remaining_waters": TOTAL_WATERS - len(self.waters_drunk)})
                    r = self.grab_page(idx)
                    if r == "taken":
                        self.pages_taken.append(idx)
                        self.log({"type": "milestone", "name": "page_taken",
                                  "index": idx,
                                  "pages_total": len(self.pages_taken),
                                  "waters_total": len(self.waters_drunk)})
                    elif r == "failed":
                        self.pages_failed.add(idx)
                        self.log({"type": "milestone", "name": "page_abandoned",
                                  "index": idx, "failed_total": len(self.pages_failed)})
                else:
                    w = self.level.water_spots[idx]
                    self.log({"type": "milestone", "name": "water_target",
                              "index": idx, "pos": [round(v, 2) for v in w],
                              "remaining_pages": TOTAL_PAGES - len(self.pages_taken),
                              "remaining_waters": TOTAL_WATERS - len(self.waters_drunk)})
                    r = self.drink_water(idx)
                    if r == "drunk":
                        self.waters_drunk.append(idx)
                    elif r == "failed":
                        self.waters_failed.add(idx)
                        self.log({"type": "milestone", "name": "water_abandoned",
                                  "index": idx, "failed_total": len(self.waters_failed)})
                continue
            # 物资收齐：8 页在手 → 门可以开了
            if not self.exit_open_seen and not obs.get("exit_open"):
                self.open_door()
                continue
            self.escape_walk()

    def run(self):
        info = self.g.info()
        if info.get("clock") != "realtime":
            raise RuntimeError("录制需要流式档（--clock realtime），当前 %r" % info.get("clock"))
        if info.get("channel") != "numeric":
            raise RuntimeError("内挂需要 numeric 信道（--channel numeric），当前 %r"
                               % info.get("channel"))
        self.log({"type": "milestone", "name": "connected", "info": info})

        # 1) 固定随机：reset 到指定种子（迷宫/页/水/门/实体全由种子决定）
        print("[bot] reset(seed=%d) —— 固定随机，等游戏重开……" % self.seed)
        t0 = time.time()
        self.g._call("reset", seed=self.seed)
        obs = self.wait_playing(timeout=120)
        self.reset_seconds = round(time.time() - t0, 1)
        self.cursor_x = None           # 新页面，lastMouse 归零，光标模型重新对齐
        self.cursor_y = None
        self.keys_held.clear()
        self._sync_cursor()
        self.log({"type": "milestone", "name": "seed_fixed",
                  "seed": obs.get("seed"), "reset_seconds": self.reset_seconds,
                  "player": obs.get("player"), "pages": obs.get("pages")})
        if obs.get("seed") != self.seed:
            raise RuntimeError("观测里的种子是 %r，不是要求的 %r" % (obs.get("seed"), self.seed))

        # 2) 离线重建迷宫并校验出生点（移植或种子任何一处不对，这里就炸）
        self.level = Level(self.seed)
        lv_spawn = self.level.spawn
        p = obs.get("player") or {}
        err = math.hypot(p.get("x", 0) - lv_spawn[0], p.get("z", 0) - lv_spawn[1])
        if err > 0.25:
            raise RuntimeError("出生点对不上：观测 (%.2f, %.2f) vs 重建 (%.2f, %.2f)"
                               % (p.get("x", 0), p.get("z", 0), lv_spawn[0], lv_spawn[1]))
        self.log({"type": "milestone", "name": "map_built",
                  "level": self.level.describe(),
                  "spawn_err_m": round(err, 3),
                  "pages_xz": [[round(v, 2) for v in pg["pos"]] for pg in self.level.page_spots],
                  "waters_xz": [[round(v, 2) for v in w] for w in self.level.water_spots],
                  "exit": {"cell": self.level.exit["cell"],
                           "door_pos": [round(v, 2) for v in self.level.exit["door_pos"]],
                           "facing_in": self.level.exit["facing_in"]},
                  "entity_spawn_cell": self.level.entity_spawn_cell,
                  "entity_spawn_pos": [round(self.level.world_x(self.level.entity_spawn_cell[0]), 2),
                                       round(self.level.world_z(self.level.entity_spawn_cell[1]), 2)]})

        # 3) 自动开录：从这一刻起的画面/状态/输入/声音全进录制
        rec = self.control.call("rec_start", fps=self.args.rec_fps)
        self.rec_id = rec["id"]
        self.log({"type": "milestone", "name": "rec_started", "rec_id": self.rec_id,
                  "fps": self.args.rec_fps})

        # 4) 巡回收集 + 开门 + 逃脱
        success = False
        try:
            success = self.play()
        finally:
            self.release_all()

        obs = self.observe()
        game_seconds = (obs.get("time") or 0) - (self.game_t0 or 0)
        # 让结算层（YOU GOT OUT）多渲染一会儿再停录，录像里要有胜利画面
        if success:
            self.g.act([], advance_ms=2000)

        # 5) 收录：停录、保留、把内挂日志写进录制目录
        summary = self.control.call("rec_stop")
        kept = self.control.call("rec_keep", id=self.rec_id)
        self.rec_dir = kept["path"]
        run_summary = self.base_summary(success)
        run_summary.update({
            "pages_collected": obs.get("pages"),
            "wall_seconds": round(time.time() - self.t0, 2),
            "game_seconds": round(game_seconds, 2),
            "reset_wall_seconds": self.reset_seconds,
            "spawn": [round(v, 3) for v in self.level.spawn],
            "recording": {"id": self.rec_id, "dir": self.rec_dir,
                          "duration_s": (summary.get("meta") or {}).get("duration_s"),
                          "qc": (summary.get("meta") or {}).get("qc")},
        })
        self.dump_logs(self.rec_dir)
        with open(os.path.join(self.rec_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(run_summary, f, ensure_ascii=False, indent=1)
        print("[bot] ✔ 收齐 %d 页 + %d 水，逃脱成功（%s），用时 %.1fs（游戏内 %.1fs）"
              % (len(self.pages_taken), len(self.waters_drunk),
                 "state=won" if success else "未完成",
                 time.time() - self.t0, game_seconds))
        print("[bot] 录制与日志：%s" % self.rec_dir)
        return success

    def close(self):
        try:
            self.g.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Backrooms 内挂：固定种子收齐 8 页 + 4 瓶杏仁水并逃脱")
    ap.add_argument("--socket", required=True, help="harness 的 modeB socket 路径")
    ap.add_argument("--control", required=True, help="harness 的控制 socket 路径")
    ap.add_argument("--modeb-dir", default=os.path.normpath(
        os.path.join(_HERE, "..", "..", "..", "..", "Backroom-Wrapper", "modeB")),
        help="wrapper 的 modeB 目录（提供 client.py）")
    ap.add_argument("--seed", type=int, default=1234, help="固定随机种子")
    ap.add_argument("--max-seconds", type=float, default=900, help="墙钟预算（秒）")
    ap.add_argument("--rec-fps", type=int, default=30, help="MP4 录制帧率")
    a = ap.parse_args()

    bot = FullClearBot(a)
    rc = 1
    try:
        rc = 0 if bot.run() else 1
    except Exception as e:
        print("[bot] ✘ 失败：%s" % e)
        bot.log({"type": "error", "error": str(e)})
        # 失败也要把现场留下来：开过录就收尾保留（录像里有失败过程）；
        # 还没开录（reset/建图阶段就炸）就把日志落进本策略 logs/，别让决策过程蒸发。
        out_dir = None
        try:
            if bot.rec_id:
                bot.control.call("rec_stop")
                kept = bot.control.call("rec_keep", id=bot.rec_id)
                out_dir = kept["path"]
        except Exception as e2:
            print("[bot] 收尾录制也失败了：%s" % e2)
        if out_dir is None:
            out_dir = os.path.join(_HERE, "..", "logs",
                                   "bot-failure-%s" % time.strftime("%Y%m%d-%H%M%S"))
        s = bot.base_summary(False, error=e)
        s["wall_seconds"] = round(time.time() - bot.t0, 2)
        if bot.rec_dir:
            s["recording_dir"] = bot.rec_dir
        bot.dump_logs(out_dir)
        with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
        print("[bot] 失败现场已保留：%s" % out_dir)
    finally:
        bot.close()
    sys.exit(rc)


if __name__ == "__main__":
    main()
