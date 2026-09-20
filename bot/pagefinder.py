#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backrooms 内挂（pagefinder）：自动游玩到拿到一页纸，全程自动录制 MP4 + log。

「内挂」的含义：它不是只看画面的玩家模型——它连的是 harness 的 numeric 信道
（桥接每帧推送的结构化状态：位置/朝向/页数/提示/实体），并且拿种子在游戏外
重建整张迷宫（bot/levelgen.py），从而**提前知道每一张页钉在哪面墙上**。
输入仍走 wrapper 的标准键鼠通道（client.act），与真实玩家同一条链路。

一局的完整流程（由本文件编排）：
  1. 连 MODEB socket 与控制 socket；
  2. reset(seed=N) **固定随机**——迷宫、8 张页、4 瓶水、出口门、实体初始位置
     全部由种子决定，mulberry32 同种子必同图；
  3. 用同一粒种子离线重建迷宫，校验出生点与观测一致（移植错了立刻暴露）；
  4. rec_start 自动开录（X11 抓帧 → ffmpeg，30fps；流式档才允许录制）；
  5. A* 找到最近一张页 → 边走边修正视角 → 站到页对面 → 瞄准 → 出现
     [E] TAKE PAGE 提示时按 e 取页；
  6. rec_stop + rec_keep，把 bot_log.jsonl / summary.json 写进录制目录。

用法（一般不直接跑，由 run.sh 调起）：
  python3 pagefinder.py --socket <modeB.sock> --control <control.sock> \
      --modeb-dir ../Backroom-Wrapper/modeB --seed 1234 --pages-goal 1
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

# ── 游戏本体常数（app/game/engine/player.ts / items.ts，勿凭感觉改） ──────
EYE_HEIGHT = 1.62          # 相机高度
WALK_SPEED = 2.7           # 步行 m/s
MOUSE_SENS = 0.0021        # 鼠标灵敏度：弧度/像素（player.onMouseDelta）
TURN_CHUNK_PX = 280        # 单个鼠标事件的有效上限（onMouseDelta 的 clamp）
MOUSE_Y = 384              # 视角控制的固定纵坐标（不动 pitch）
CENTER_X = 512             # 光标虚拟原点（1024x768 的中心）
PAGE_REACH = 2.6           # 出现取页提示的最大相机距离（items.findInteractable）
PROMPT_PAGE = "TAKE PAGE"  # 取页提示文案（[E] TAKE PAGE）


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
    """harness 控制socket 的极简客户端（录制这类只给人用的操作在这里）。
    每次调用一条短连接——与 play_ui.py 的 Control 同一套用法。"""

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


class Bot:
    def __init__(self, args):
        self.args = args
        self.seed = args.seed
        self.pages_goal = args.pages_goal
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
        # 光标模型：必须与页面侧 bridge 的 lastMouse 一一对应（差分算视角增量）
        self.cursor_x = None
        self.keys_held = set()
        self.log_rows = []
        self.grabbed = []             # 已取页的索引
        self.failed_pages = set()     # 取页失败被放弃的索引（换下一张再试）
        self.entity_ever_active = False
        self.cycles = 0
        self.reset_seconds = None

    # ── 日志 ────────────────────────────────────────────────────────────
    def log(self, row):
        row["t_wall"] = round(time.time() - self.t0, 3)
        self.log_rows.append(row)
        if row.get("type") == "milestone":
            print("[bot] %s %s" % (row["name"], json.dumps(
                {k: v for k, v in row.items() if k not in ("type", "t_wall", "name")},
                ensure_ascii=False)))

    def cycle_log(self, obs, phase, **extra):
        p = obs.get("player") or {}
        self.cycles += 1
        self.log({
            "type": "cycle", "phase": phase,
            "t_game": obs.get("time"), "iter": obs.get("iter"),
            "state": obs.get("state"), "pages": obs.get("pages"),
            "prompt": obs.get("prompt"),
            "player": {"x": p.get("x"), "z": p.get("z"), "yaw": p.get("yaw"),
                       "pitch": p.get("pitch"), "speed": p.get("speed"),
                       "stamina": p.get("stamina")},
            "entity": obs.get("entity"),
            **extra,
        })
        ent = obs.get("entity")
        if ent and not self.entity_ever_active:
            self.entity_ever_active = True
            self.log({"type": "milestone", "name": "entity_active",
                      "entity": ent, "pages": obs.get("pages"),
                      "note": "实体已苏醒（首页后自动激活，或开局 45 秒到点）"})
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
        """等引擎进入 playing（reset 返回时 world=true，但留几拍余量）。"""
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
        self.g.act([{"type": "mouse_move", "x": CENTER_X, "y": MOUSE_Y}], advance_ms=60)
        self.cursor_x = CENTER_X

    def _mouse_events_for_delta(self, dx_total):
        """把 dx_total 像素的水平鼠标位移拆成事件序列。

        页面侧 bridge 用相邻两次绝对坐标的差当 movementX 送进
        player.onMouseDelta；游戏把 |dx|>600 当垃圾事件**整个丢弃**（不旋转），
        |dx|≤280 的按 0.0021 弧度/像素旋转。所以：
          · 旋转按 ≤280px 的块发，每块都全额生效；
          · 光标漂出 |x-512|>600 后，先发一次「回中」事件——它的位移必然 >600，
            被游戏丢弃，但页面侧 lastMouse 更新了，光标回到中心。
        """
        events = []
        remaining = int(dx_total)
        step = TURN_CHUNK_PX if remaining > 0 else -TURN_CHUNK_PX
        while remaining != 0:
            if abs(self.cursor_x - CENTER_X) > 600:
                events.append({"type": "mouse_move", "x": CENTER_X, "y": MOUSE_Y})
                self.cursor_x = CENTER_X
            d = step if abs(remaining) > TURN_CHUNK_PX else remaining
            self.cursor_x += d
            events.append({"type": "mouse_move", "x": self.cursor_x, "y": MOUSE_Y})
            remaining -= d
        return events

    def turn_events(self, cur_yaw, target_yaw):
        """从 cur_yaw 转到 target_yaw 需要的事件（yaw -= dx*sens ⇒ dx = -Δ/sens）。"""
        dyaw = wrap_angle(target_yaw - cur_yaw)
        return self._mouse_events_for_delta(int(round(-dyaw / MOUSE_SENS)))

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
        best = path[-1]
        for i in range(len(path) - 1, -1, -1):
            if self.level.corridor_clear(pos[0], pos[1], path[i][0], path[i][1]):
                best = path[i]
                break
        return best

    def walk_to(self, target, arrive_r=0.5, phase="nav"):
        """反馈式走到 target：观测→修正视角→按 w 前进，每圈 ~300ms。"""
        last_cell = None
        path = None
        stall_pos, stall_t = None, 0.0
        while True:
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽（%ds）" % self.max_seconds)
            obs = self.observe()
            self.check_alive(obs)
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            dist = math.hypot(target[0] - pos[0], target[1] - pos[1])
            if dist <= arrive_r:
                self.cycle_log(obs, phase, target=list(target), dist=dist,
                               action="arrived")
                return obs
            # 迷宫格变了或还没有路径 → 重规划
            cell = self.level.cell_of(*pos)
            if path is None or last_cell != cell:
                tgt_cell = self.level.cell_of(*target)
                cells = self.level.find_path(cell, tgt_cell)
                if cells is None:
                    raise RuntimeError("寻路失败：%s → %s（页不可达？）" % (cell, tgt_cell))
                # 最后一跳补上真实目标点（它不是格心，通常是页对面的站位）
                path = cells + [target]
                last_cell = cell
            aim = self._immediate_target(pos, path)
            aim_dist = math.hypot(aim[0] - pos[0], aim[1] - pos[1])
            if aim_dist < 0.35:   # 拉直点贴脸了，直接瞄真目标
                aim = target
            desired = yaw_toward(pos[0], pos[1], aim[0], aim[1])
            dyaw = wrap_angle(desired - p.get("yaw"))
            events = []
            if abs(dyaw) > 0.55:          # ≈31°：先停下原地转身
                events.extend(self.key_event("w", False))
                events.extend(self.turn_events(p.get("yaw"), desired))
                self.cycle_log(obs, phase, target=list(aim), dist=dist, dyaw=round(dyaw, 3),
                               action="turn_in_place")
                self.g.act(events, advance_ms=max(120, int(abs(dyaw) * 130)))
                continue
            events.extend(self.turn_events(p.get("yaw"), desired))
            events.extend(self.key_event("w", True))
            # 停滞检测：2.5 秒内挪不动就要自救（卡墙角/卡门框）
            now = time.time()
            if stall_pos is None or math.hypot(pos[0] - stall_pos[0], pos[1] - stall_pos[1]) > 0.2:
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
                           speed=p.get("speed"))
            self.g.act(events, advance_ms=300)

    def _unstick(self):
        """卡住自救：倒 0.4s，随机侧转 90°，向前 0.7s，然后重规划。"""
        side = 1 if (time.time() * 1000) % 2 < 1 else -1
        self.g.act(self.key_event("s", True), advance_ms=400)
        obs = self.observe()
        self.g.act(self.key_event("s", False)
                   + self.turn_events((obs.get("player") or {}).get("yaw"),
                                      (obs.get("player") or {}).get("yaw") + side * math.pi / 2),
                   advance_ms=150)
        self.g.act(self.key_event("w", True), advance_ms=700)
        self.g.act(self.key_event("w", False), advance_ms=60)

    # ── 取页 ────────────────────────────────────────────────────────────
    def try_grab_page(self, page):
        """走到页对面、瞄准、等 [E] TAKE PAGE 提示出现时取页。"""
        px, py, pz = page["pos"]
        nx, nz = page["normal"]
        # 站位：页法线方向退 1.35m（相机距页 ~1.4m < 2.6m 提示阈值；同格保证 LOS）
        stand = (px + nx * 1.35, pz + nz * 1.35)
        self.walk_to(stand, arrive_r=0.4, phase="approach")
        for attempt in range(6):
            if self.budget_left() <= 0:
                raise RuntimeError("墙钟预算耗尽")
            obs = self.observe()
            self.check_alive(obs)
            p = obs.get("player") or {}
            pos = (p.get("x"), p.get("z"))
            if PROMPT_PAGE in (obs.get("prompt") or ""):
                self.cycle_log(obs, "grab", action="press_e",
                               page=list(page["pos"]), attempt=attempt)
                self.g.act([{"type": "key_tap", "key": "e", "hold_ms": 60}],
                           advance_ms=450)
                obs2 = self.observe()
                self.cycle_log(obs2, "grab", action="after_e",
                                pages=obs2.get("pages"))
                if (obs2.get("pages") or 0) > (obs.get("pages") or 0):
                    return True     # 这张页确实进了口袋
                continue            # 没取上：重新瞄准再试
            # 还没有提示：把视角精确对到页上，必要时凑近半步
            desired = yaw_toward(pos[0], pos[1], px, pz)
            events = self.turn_events(p.get("yaw"), desired)
            dist = math.hypot(px - pos[0], pz - pos[1])
            if dist > PAGE_REACH - 0.35:
                events.extend(self.key_event("w", True))
                self.cycle_log(obs, "grab", action="aim_and_step", dist=round(dist, 2),
                               attempt=attempt)
                self.g.act(events, advance_ms=280)
            else:
                events.extend(self.key_event("w", False))   # 距离够了只转身
                self.cycle_log(obs, "grab", action="aim", dist=round(dist, 2),
                               attempt=attempt)
                self.g.act(events, advance_ms=200)
        return False

    def pick_next_page(self, pos):
        """按「走路易达 + 近」挑下一张要取的页（跳过已取过与已放弃的）。"""
        cell = self.level.cell_of(*pos)
        best, best_key = None, None
        for i, pg in enumerate(self.level.page_spots):
            if i in self.grabbed or i in self.failed_pages:
                continue
            d = self.level.walk_distance(cell, pg["cell"])
            if d is None:
                continue
            key = (d, math.hypot(pg["pos"][0] - pos[0], pg["pos"][2] - pos[1]))
            if best is None or key < best_key:
                best, best_key = i, key
        return best

    # ── 一局编排 ────────────────────────────────────────────────────────
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
                  "pages_xz": [[round(v, 2) for v in pg["pos"]] for pg in self.level.page_spots]})

        # 3) 自动开录：从这一刻起的画面/状态/输入/声音全进录制
        rec = self.control.call("rec_start", fps=self.args.rec_fps)
        self.rec_id = rec["id"]
        self.log({"type": "milestone", "name": "rec_started", "rec_id": self.rec_id,
                  "fps": self.args.rec_fps})

        # 4) 找页 → 取页（目标 = 一页纸；可配更多）
        success = False
        try:
            while True:
                obs = self.observe()
                self.check_alive(obs)
                if obs.get("pages", 0) >= self.pages_goal:
                    success = True
                    break
                pos = ((obs.get("player") or {}).get("x"),
                       (obs.get("player") or {}).get("z"))
                idx = self.pick_next_page(pos)
                if idx is None:
                    raise RuntimeError("没有可取的页了（已取 %d 张、放弃 %d 张）"
                                        % (len(self.grabbed), len(self.failed_pages)))
                page = self.level.page_spots[idx]
                self.log({"type": "milestone", "name": "page_target",
                          "index": idx, "pos": [round(v, 2) for v in page["pos"]],
                          "cell": page["cell"]})
                if self.try_grab_page(page):
                    self.grabbed.append(idx)
                    self.log({"type": "milestone", "name": "page_taken",
                              "index": idx, "pages_total": len(self.grabbed)})
                else:
                    # 这张取不上（提示始终不出现）：多半是站位/法线的边缘情况，
                    # 放弃它换下一张；连续三张都失败基本说明地图重建出了问题
                    self.failed_pages.add(idx)
                    self.log({"type": "milestone", "name": "page_abandoned",
                              "index": idx, "failed_total": len(self.failed_pages)})
                    if len(self.failed_pages) >= 3:
                        raise RuntimeError("连续 %d 张页都取不上——怀疑离线地图与游戏不符"
                                           % len(self.failed_pages))
        finally:
            self.release_all()

        obs = self.observe()
        game_seconds = (obs.get("time") or 0) - (self.game_t0 or 0)
        self.log({"type": "milestone", "name": "goal_reached",
                  "pages": obs.get("pages"), "game_seconds": round(game_seconds, 2),
                  "entity_active": self.entity_ever_active})

        # 5) 收录：停录、保留、把内挂日志写进录制目录
        summary = self.control.call("rec_stop")
        kept = self.control.call("rec_keep", id=self.rec_id)
        self.rec_dir = kept["path"]
        with open(os.path.join(self.rec_dir, "bot_log.jsonl"), "w", encoding="utf-8") as f:
            for row in self.log_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        run_summary = {
            "run": os.environ.get("REPLAY_RUN", ""),
            "seed": self.seed,
            "success": success,
            "pages_collected": obs.get("pages"),
            "pages_goal": self.pages_goal,
            "wall_seconds": round(time.time() - self.t0, 2),
            "game_seconds": round(game_seconds, 2),
            "reset_wall_seconds": self.reset_seconds,
            "spawn": [round(v, 3) for v in self.level.spawn],
            "pages_taken": [[i, [round(v, 2) for v in self.level.page_spots[i]["pos"]]]
                             for i in self.grabbed],
            "pages_abandoned": sorted(self.failed_pages),
            "entity_ever_active": self.entity_ever_active,
            "decision_cycles": self.cycles,
            "recording": {"id": self.rec_id, "dir": self.rec_dir,
                          "duration_s": (summary.get("meta") or {}).get("duration_s"),
                          "qc": (summary.get("meta") or {}).get("qc")},
            "note": "迷宫与页位由种子离线重建（bot/levelgen.py，对照上游 level.ts 逐位移植）；"
                    "灯光爆闪/实体抖动等 Math.random 装饰性随机不在固定范围内（见 SCOPE.md）",
        }
        with open(os.path.join(self.rec_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(run_summary, f, ensure_ascii=False, indent=1)
        print("[bot] ✔ 找到第 %d 张页（目标 %d），用时 %.1fs（游戏内 %.1fs）"
              % (obs.get("pages"), self.pages_goal, time.time() - self.t0, game_seconds))
        print("[bot] 录制与日志：%s" % self.rec_dir)
        return success

    def close(self):
        try:
            self.g.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Backrooms 内挂：自动找到一页纸并录制")
    ap.add_argument("--socket", required=True, help="harness 的 modeB socket 路径")
    ap.add_argument("--control", required=True, help="harness 的控制 socket 路径")
    ap.add_argument("--modeb-dir", default=os.path.normpath(
        os.path.join(_HERE, "..", "..", "Backroom-Wrapper", "modeB")),
        help="wrapper 的 modeB 目录（提供 client.py）")
    ap.add_argument("--seed", type=int, default=1234, help="固定随机种子")
    ap.add_argument("--pages-goal", type=int, default=1, help="要取几张页（默认 1）")
    ap.add_argument("--max-seconds", type=float, default=240, help="墙钟预算（秒）")
    ap.add_argument("--rec-fps", type=int, default=30, help="MP4 录制帧率")
    a = ap.parse_args()

    bot = Bot(a)
    rc = 1
    try:
        rc = 0 if bot.run() else 1
    except Exception as e:
        print("[bot] ✘ 失败：%s" % e)
        bot.log({"type": "error", "error": str(e)})
        # 失败也要把已开的录制收尾保留下来（录像里就有失败现场）
        try:
            if bot.rec_id:
                bot.control.call("rec_stop")
                kept = bot.control.call("rec_keep", id=bot.rec_id)
                with open(os.path.join(kept["path"], "bot_log.jsonl"), "w",
                          encoding="utf-8") as f:
                    for row in bot.log_rows:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                print("[bot] 失败现场已保留：%s" % kept["path"])
        except Exception as e2:
            print("[bot] 收尾录制也失败了：%s" % e2)
    finally:
        bot.close()
    sys.exit(rc)


if __name__ == "__main__":
    main()
