# -*- coding: utf-8 -*-
"""Backrooms — Level 0 迷宫生成的 Python 移植（给内挂离线复现用）。

为什么需要它：观测里**故意不给** 8 张日记页 / 4 瓶杏仁水 / 出口门的坐标（那是
「找」这道题的分母），但整张迷宫由种子经 mulberry32 决定（SCOPE.md「随机性」一节：
同种子必同图）。内挂拿着种子在游戏外把同一份生成算法再跑一遍，就得到墙、出生点、
每张页的钉放位置、每瓶水的落地位置、出口门位置、实体的出生格——这是「内挂」
区别于普通玩家的特权信息，也是全收集路线与实体规避的规划依据。

移植基准：Backroom-Erase 上游 `app/game/engine/level.ts` + `rng.ts`
（补丁基准提交 26e5481，本包经 source/game.patch 核对过：生成逻辑未被改动）。

本文件是策略 02（全收集+逃脱）用的**扩展版**：移植到第 9 步（实体出生格）为止——
  6) BFS 距离场 + 可达表        （策略 01 已有）
  7) 出口格 + computeExit        ← 新增：出口门位置/朝向（不消费随机流）
  8) 八张页按距离带钉墙          （策略 01 已有）
  8.5) 墙涂鸦 14 幅              ← 新增：只为对齐随机流，位置本身用不到
  8.7) 杏仁水 4 瓶               ← 新增：喝水点
  8.8) 假 EXIT 牌 6 块           ← 新增：只为对齐随机流
  9) 实体出生格                  ← 新增：实体苏醒后从这里出现
之后的步骤 10（顶灯阵列/暗区/色相异常）不消费在实体出生之前的随机流之外的位置
信息，内挂用不到，不移植。

**必须逐位一致**：JS 的 Math.imul / |0 / >>> 语义、`&&`/`||` 的短路求值顺序、
`?:` 只求值一个分支、shuffle 的调用次数、以及每一条 rng() 出现的位置。任何一处
不对，水就会放在别的地上、实体就会从别的格子里出生。移植时逐行对着源码抄，
别「顺手优化」。

**离线地面真值**：seed=777 的历史失败局（strategies/01-grab-one-page/recordings/
backrooms/20260918-130616-1795e5/bot_log.jsonl）里，实体苏醒后首次观测位置
(9.54, -85.71) ≈ 格 (26,2) 中心 (10, -86) 加上 roam 半秒的漂移——自测用它钉死
实体出生格的移植正确性（连带校验了 8.5/8.7/8.8 的整条随机流）。
"""
import math

# ── level.ts 的常量 ──────────────────────────────────────────────────────
CELL = 4          # 每格 4 米
WALL_H = 3        # 层高
WALL_HALF = 0.12  # 隔断墙厚 24cm
PILLAR_HALF = 0.55
OPEN, SOLID, PILLAR = 0, 1, 2


# ── rng.ts ──────────────────────────────────────────────────────────────
def mulberry32(seed):
    """mulberry32，与 JS 版逐位一致（全程用 32 位无符号模式运算）。"""
    state = [seed & 0xFFFFFFFF]

    def rng():
        state[0] = (state[0] + 0x6D2B79F5) & 0xFFFFFFFF
        a = state[0]
        # t = Math.imul(a ^ (a >>> 15), 1 | a)
        t = ((a ^ (a >> 15)) * ((1 | a) & 0xFFFFFFFF)) & 0xFFFFFFFF
        # t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
        t = ((t + (((t ^ (t >> 7)) * ((61 | t) & 0xFFFFFFFF)) & 0xFFFFFFFF)) ^ t) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    return rng


def rand_range(rng, lo, hi):
    return lo + (hi - lo) * rng()


def rand_int(rng, lo, hi):
    # JS: Math.floor(randRange(rng, min, max + 1)) —— 恰好一次 rng()
    return int(math.floor(lo + (hi + 1 - lo) * rng()))


def shuffle(rng, arr):
    """Fisher–Yates，与 JS 同序（i 从 n-1 到 1），恰好 n-1 次 rng()。"""
    a = list(arr)
    for i in range(len(a) - 1, 0, -1):
        j = int(math.floor(rng() * (i + 1)))
        a[i], a[j] = a[j], a[i]
    return a


class Level:
    """`level.ts` 的 `Level.generate()`：构造即生成（与 TS 相同）。"""

    SIZE = 48  # readonly size = 48

    def __init__(self, seed):
        self.seed = seed
        self.rng = mulberry32(seed)
        S = self.SIZE
        self.grid = [OPEN] * (S * S)
        # wallV[x*size+z]: 格 (x,z) 西边缘的墙
        self.wallV = [0] * ((S + 1) * S)
        # wallH[z*size+x]: 格 (x,z) 北边缘的墙
        self.wallH = [0] * (S * (S + 1))
        self.page_spots = []          # [{pos:(x,y,z), normal:(nx,nz), cell:(x,z)}]
        self.art_spots = []           # 墙涂鸦（只为对齐随机流）
        self.water_spots = []         # [(x, z)] 杏仁水落地位置（y=0）
        self.false_exits = []        # 假 EXIT 牌所在格（只为对齐随机流）
        self.spawn = (0.0, 0.0)      # 世界坐标
        self.spawn_cell = (0, 0)
        self.entity_spawn_cell = None  # 实体苏醒后出现的格
        self.exit = None             # {cell, door_pos, facing_in}
        self.dist_from_spawn = None
        self.page_cells = []         # 落页的候选格（TS 里 chosen 闭包，8.5/8.7 还要用）
        self._generate()

    # ── 网格索引与判定（cell/isBlocked/hasWall*/canMove 原样） ───────────
    def _vidx(self, x, z):
        return x * self.SIZE + z

    def _hidx(self, x, z):
        return z * self.SIZE + x

    def cell(self, x, z):
        if x < 0 or z < 0 or x >= self.SIZE or z >= self.SIZE:
            return SOLID
        return self.grid[z * self.SIZE + x]

    def is_blocked(self, x, z):
        return self.cell(x, z) != OPEN

    def has_wall_v(self, x, z):
        if x < 0 or x > self.SIZE or z < 0 or z >= self.SIZE:
            return True
        return self.wallV[self._vidx(x, z)] == 1

    def has_wall_h(self, x, z):
        if z < 0 or z > self.SIZE or x < 0 or x >= self.SIZE:
            return True
        return self.wallH[self._hidx(x, z)] == 1

    def can_move(self, x, z, dx, dz):
        nx, nz = x + dx, z + dz
        if self.is_blocked(nx, nz):
            return False
        if dx == 1:
            return not self.has_wall_v(x + 1, z)
        if dx == -1:
            return not self.has_wall_v(x, z)
        if dz == 1:
            return not self.has_wall_h(x, z + 1)
        if dz == -1:
            return not self.has_wall_h(x, z)
        return True

    # ── 世界坐标换算 ─────────────────────────────────────────────────────
    def world_x(self, cx):
        return (cx - self.SIZE / 2) * CELL + CELL / 2

    def world_z(self, cz):
        return (cz - self.SIZE / 2) * CELL + CELL / 2

    def cell_of(self, x, z):
        # JS: Math.floor(x / CELL + this.size / 2) —— 负数也向 -inf 取整
        return (int(math.floor(x / CELL + self.SIZE / 2)),
                int(math.floor(z / CELL + self.SIZE / 2)))

    def solid_at_world(self, px, pz):
        """这个点是否落在隔断墙或柱子里（XZ 平面）。TS 的 solidAtWorld 原样。"""
        c = self.cell_of(px, pz)
        kind = self.cell(c[0], c[1])
        if kind == SOLID:
            return True
        if (kind == PILLAR
                and abs(px - self.world_x(c[0])) <= PILLAR_HALF
                and abs(pz - self.world_z(c[1])) <= PILLAR_HALF):
            return True
        T = WALL_HALF
        if self.has_wall_v(c[0], c[1]) and px - (self.world_x(c[0]) - CELL / 2) <= T:
            return True
        if self.has_wall_v(c[0] + 1, c[1]) and (self.world_x(c[0]) + CELL / 2) - px <= T:
            return True
        if self.has_wall_h(c[0], c[1]) and pz - (self.world_z(c[1]) - CELL / 2) <= T:
            return True
        if self.has_wall_h(c[0], c[1] + 1) and (self.world_z(c[1]) + CELL / 2) - pz <= T:
            return True
        return False

    def line_of_sight_cells(self, ax, az, bx, bz):
        """格心间视线（TS 的 lineOfSight：沿格心连线逐段查穿墙与柱）。"""
        if self.is_blocked(bx, bz) and not (ax == bx and az == bz):
            return False
        x0, z0 = self.world_x(ax), self.world_z(az)
        x1, z1 = self.world_x(bx), self.world_z(bz)
        dist = math.hypot(x1 - x0, z1 - z0)
        if dist < 0.01:
            return True
        steps = int(math.ceil(dist / 0.5))
        cx, cz = ax, az
        for i in range(1, steps + 1):
            t = i / steps
            px = x0 + (x1 - x0) * t
            pz = z0 + (z1 - z0) * t
            c = self.cell_of(px, pz)
            while cx != c[0]:
                sx = 1 if c[0] > cx else -1
                if self.has_wall_v(cx + 1, cz) if sx > 0 else self.has_wall_v(cx, cz):
                    return False
                cx += sx
                if self.cell(cx, cz) == PILLAR:
                    return False
            while cz != c[1]:
                sz = 1 if c[1] > cz else -1
                if self.has_wall_h(cx, cz + 1) if sz > 0 else self.has_wall_h(cx, cz):
                    return False
                cz += sz
                if self.cell(cx, cz) == PILLAR:
                    return False
        return True

    # ── generate()：逐段对照 level.ts ────────────────────────────────────
    def _generate(self):
        S = self.SIZE
        rng = self.rng

        # 1) 封边框
        for z in range(S):
            self.wallV[self._vidx(0, z)] = 1
            self.wallV[self._vidx(S, z)] = 1
        for x in range(S):
            self.wallH[self._hidx(x, 0)] = 1
            self.wallH[self._hidx(x, S)] = 1

        # 2) 递归分割（带门洞）
        self._divide(0, 0, S - 1, S - 1, 0)

        # 3) 额外开洞：6% 概率拆墙。rng() 只在「这里确实有墙」时被消费（短路）
        for x in range(1, S):
            for z in range(S):
                if self.wallV[self._vidx(x, z)] == 1 and rng() < 0.06:
                    self.wallV[self._vidx(x, z)] = 0
        for z in range(1, S):
            for x in range(S):
                if self.wallH[self._hidx(x, z)] == 1 and rng() < 0.06:
                    self.wallH[self._hidx(x, z)] = 0

        # 4) 柱厅：8 片 7x7 的柱阵。短路顺序：x 奇 → z 奇 → rng()>0.7
        for _ in range(8):
            cx = rand_int(rng, 4, S - 5)
            cz = rand_int(rng, 4, S - 5)
            for z in range(cz - 3, cz + 4):
                for x in range(cx - 3, cx + 4):
                    if x % 2 != 0 or z % 2 != 0:
                        continue
                    if rng() > 0.7:
                        continue
                    clear = (not self.has_wall_v(x, z) and not self.has_wall_v(x + 1, z)
                             and not self.has_wall_h(x, z) and not self.has_wall_h(x, z + 1))
                    if clear:
                        self.grid[z * S + x] = PILLAR

        # 5) 出生点：从迷宫中心向外扫描的第一个开阔格（含完整方块扫描顺序）
        self._pick_spawn()

        # 6) 从出生点做 BFS 距离场（墙敏感），产出按距离稳定排序的可达格表
        self._build_reachable()

        # 7) 出口格：可达表里最远的边界环格。**不消费随机流**（TS 就在页之前选）
        self._compute_exit()

        # 8) 八张页按距离带钉墙（pageSpots 由此定）
        self._place_pages()

        # 8.5) 墙涂鸦：对齐随机流必须移植（页格 chosen 参与 8.7 的距离过滤）
        self._place_arts()

        # 8.7) 杏仁水：4 瓶，中深区，互相隔开
        self._place_waters()

        # 8.8) 假 EXIT 牌：对齐随机流
        self._place_false_exits()

        # 9) 实体出生格：可达表远池里随机一格
        self._pick_entity_spawn()

    def _divide(self, x0, z0, x1, z1, depth):
        """递归分割。**rng 消费顺序必须与 TS 逐条一致**（短路都保真）。"""
        rng = self.rng
        w = x1 - x0 + 1
        h = z1 - z0 + 1
        if w < 3 and h < 3:
            return
        # if (w*h <= 30 && rng() < 0.3 && depth > 2) return;
        # rng 只要 w*h<=30 就会被消费（无论 depth）——depth 在它后面判断
        if w * h <= 30:
            if rng() < 0.3 and depth > 2:
                return
        # const vertical = w === h ? rng() < 0.5 : w > h;
        if w == h:
            vertical = rng() < 0.5
        else:
            vertical = w > h
        if vertical and w >= 3:
            sx = rand_int(rng, x0 + 1, x1)
            for z in range(z0, z1 + 1):
                self.wallV[self._vidx(sx, z)] = 1
            # gaps = 1 + (h > 5 && rng() < 0.55 ? 1 : 0)
            gaps = 1
            if h > 5 and rng() < 0.55:
                gaps = 2
            for _ in range(gaps):
                gz = rand_int(rng, z0, z1)
                self.wallV[self._vidx(sx, gz)] = 0
                # rng() 恒被消费，边界判断在它后面（短路）
                if rng() < 0.45 and gz + 1 <= z1:
                    self.wallV[self._vidx(sx, gz + 1)] = 0
            self._divide(x0, z0, sx - 1, z1, depth + 1)
            self._divide(sx, z0, x1, z1, depth + 1)
        elif h >= 3:
            sz = rand_int(rng, z0 + 1, z1)
            for x in range(x0, x1 + 1):
                self.wallH[self._hidx(x, sz)] = 1
            gaps = 1
            if w > 5 and rng() < 0.55:
                gaps = 2
            for _ in range(gaps):
                gx = rand_int(rng, x0, x1)
                self.wallH[self._hidx(gx, sz)] = 0
                if rng() < 0.45 and gx + 1 <= x1:
                    self.wallH[self._hidx(gx + 1, sz)] = 0
            self._divide(x0, z0, x1, sz - 1, depth + 1)
            self._divide(x0, sz, x1, z1, depth + 1)

    def _pick_spawn(self):
        S = self.SIZE
        c = S // 2
        for radius in range(S):
            for z in range(c - radius, c + radius + 1):
                for x in range(c - radius, c + radius + 1):
                    if self.cell(x, z) == OPEN:
                        self.spawn_cell = (x, z)
                        self.spawn = (self.world_x(x), self.world_z(z))
                        return
        raise RuntimeError("迷宫里没有开阔格——生成算法出错了")

    def _build_reachable(self):
        S = self.SIZE
        sx, sz = self.spawn_cell
        dist = [-1] * (S * S)
        dist[sz * S + sx] = 0
        queue = [(sx, sz)]
        qi = 0
        while qi < len(queue):
            cx, cz = queue[qi]
            qi += 1
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if not self.can_move(cx, cz, dx, dz):
                    continue
                ni = (cz + dz) * S + (cx + dx)
                if dist[ni] == -1:
                    dist[ni] = dist[cz * S + cx] + 1
                    queue.append((cx + dx, cz + dz))
        self.dist_from_spawn = dist
        # reachable 按 z 外层、x 内层构建，再按 d 稳定排序（与 JS sort 一致）
        reachable = []
        for z in range(S):
            for x in range(S):
                d = dist[z * S + x]
                if d > 0:
                    reachable.append((x, z, d))
        reachable.sort(key=lambda r: r[2])
        self.reachable = reachable

    def _compute_exit(self):
        """TS 步骤 7 + computeExit：出口门 = 可达表里最远的边界环格。
        只挑格不消费随机流，放在哪一步算都一样，这里与 TS 同位（页之前）。"""
        S = self.SIZE
        exit_cell = self.reachable[-1]
        for i in range(len(self.reachable) - 1, -1, -1):
            x, z, _d = self.reachable[i]
            if x == 0 or z == 0 or x == S - 1 or z == S - 1:
                exit_cell = (x, z)
                break
        # 门朝最近的那面边界墙
        if exit_cell[0] == 0:
            facing = (-1, 0)
        elif exit_cell[0] == S - 1:
            facing = (1, 0)
        elif exit_cell[1] == 0:
            facing = (0, -1)
        else:
            facing = (0, 1)
        inset = CELL / 2 - WALL_HALF - 0.05
        self.exit = {
            "cell": exit_cell,
            # doorPos：门面上，朝墙方向退 inset（胜利判定用 player 与它的 XZ 距离 < 1.05）
            "door_pos": (self.world_x(exit_cell[0]) + facing[0] * inset,
                         self.world_z(exit_cell[1]) + facing[1] * inset),
            # facing_in：指向迷宫内侧（TS 的 exit.facing，站在它上面按 e 开门）
            "facing_in": (-facing[0], -facing[1]),
        }

    def _adjacent_wall(self, x, z):
        """格 (x,z) 边上某面墙的法线（指向格内）。**有候选墙就会消费 rng**（shuffle），
        与 TS 的 adjacentWall 一致——调用方（过滤/落页/涂鸦/水）全按同一顺序，别改。"""
        if self.cell(x, z) != OPEN:
            return None
        candidates = []
        if self.has_wall_v(x, z):
            candidates.append((1, 0))     # 西墙朝 +X
        if self.has_wall_v(x + 1, z):
            candidates.append((-1, 0))   # 东墙朝 -X
        if self.has_wall_h(x, z):
            candidates.append((0, 1))     # 北墙朝 +Z
        if self.has_wall_h(x, z + 1):
            candidates.append((0, -1))    # 南墙朝 -Z
        if not candidates:
            return None
        return shuffle(self.rng, candidates)[0]

    def _place_page(self, cand):
        """落一张页：先再问一次邻墙（消费 shuffle 的 rng），再取侧偏与高度（2 次 rng）。
        成功时把候选格记入 page_cells（TS 里就在 placePage 里 push）。"""
        x, z = cand[0], cand[1]
        wall = self._adjacent_wall(x, z)
        if wall is None:
            return False
        self.page_cells.append((x, z))
        inset = CELL / 2 - WALL_HALF - 0.03
        lateral = (self.rng() - 0.5) * 2.2
        y = 1.35 + self.rng() * 0.4
        self.page_spots.append({
            "pos": (self.world_x(x) - wall[0] * inset + wall[1] * lateral,
                    y,
                    self.world_z(z) - wall[1] * inset + wall[0] * lateral),
            "normal": wall,
            "cell": (x, z),
        })
        return True

    def _place_pages(self):
        rng = self.rng
        reachable = self.reachable
        bands = 8

        def too_close(cand, min_gap):
            return any(abs(p[0] - cand[0]) + abs(p[1] - cand[1]) < min_gap
                       for p in self.page_cells)

        # 带 0 是新手页：离出生点 2..6 步 BFS（~8-24 米）。选不出就把环放大直到选出。
        # 过滤条件里 _adjacent_wall 会消费 rng——顺序与 TS 的短路一致。
        max_d = 6
        while len(self.page_cells) == 0 and max_d <= 30:
            pool = shuffle(rng, [c for c in reachable
                                 if c[2] >= 2 and c[2] <= max_d
                                 and self._adjacent_wall(c[0], c[1]) is not None])
            for cand in pool:
                if self._place_page(cand):
                    break
            max_d += 4

        # 带 1..7：按可达表的下标区间随机取样，最多各试 80 次
        for b in range(1, bands):
            lo = int(math.floor(len(reachable) * (0.15 + (b / bands) * 0.8)))
            hi = int(math.floor(len(reachable) * (0.15 + ((b + 1) / bands) * 0.8))) - 1
            for _attempt in range(80):
                cand = reachable[rand_int(rng, lo, max(lo, hi))]
                if too_close(cand, 4):
                    continue
                if self._place_page(cand):
                    break

        # 兜底：还不足 8 张就放宽再抽（600 次上限，注意 safety++ 的求值顺序）
        safety = 0
        while len(self.page_spots) < bands:
            if safety >= 600:
                break
            safety += 1
            cand = reachable[rand_int(rng, int(math.floor(len(reachable) * 0.1)),
                                      len(reachable) - 1)]
            if too_close(cand, 3):
                continue
            self._place_page(cand)

    def _place_arts(self):
        """TS 步骤 8.5：墙涂鸦 14 幅。位置用不到，但 shuffle/adjacentWall/侧偏高度
        全都在消费随机流——杏仁水在它后面，少一步整条流就断了。"""
        reachable = self.reachable
        art_candidates = shuffle(self.rng,
                                 [c for c in reachable
                                  if self._adjacent_wall(c[0], c[1]) is not None])
        art_cells = []
        for cand in art_candidates:
            if len(self.art_spots) >= 14:
                break
            if any(abs(p[0] - cand[0]) + abs(p[1] - cand[1]) < 2 for p in self.page_cells):
                continue
            if any(abs(p[0] - cand[0]) + abs(p[1] - cand[1]) < 3 for p in art_cells):
                continue
            wall = self._adjacent_wall(cand[0], cand[1])   # TS 在这里再问一次邻墙
            art_cells.append(cand)
            inset = CELL / 2 - WALL_HALF - 0.015
            lateral = (self.rng() - 0.5) * 2.0
            y = 1.05 + self.rng() * 0.75
            self.art_spots.append({
                "pos": (self.world_x(cand[0]) - wall[0] * inset + wall[1] * lateral,
                        y,
                        self.world_z(cand[1]) - wall[1] * inset + wall[0] * lateral),
                "normal": wall,
            })

    def _place_waters(self):
        """TS 步骤 8.7：杏仁水 4 瓶。候选 = 可达表 18% 之后的深区，避开页格 3 格、
        彼此隔开 10 格；有邻墙就贴墙放（inset 1.48），否则格内随机（±0.7）。
        px/pz 各恰好消费一次 rng（TS 的 `?:` 只求值一个分支）。"""
        reachable = self.reachable
        water_candidates = shuffle(
            self.rng, reachable[int(math.floor(len(reachable) * 0.18)):])
        water_cells = []
        for cand in water_candidates:
            if len(self.water_spots) >= 4:
                break
            if any(abs(p[0] - cand[0]) + abs(p[1] - cand[1]) < 3 for p in self.page_cells):
                continue
            if any(abs(p[0] - cand[0]) + abs(p[1] - cand[1]) < 10 for p in water_cells):
                continue
            water_cells.append(cand)
            wall = self._adjacent_wall(cand[0], cand[1])
            inset = CELL / 2 - WALL_HALF - 0.4
            if wall:
                px = (self.world_x(cand[0]) - wall[0] * inset
                      + wall[1] * (self.rng() - 0.5) * 1.6)
                pz = (self.world_z(cand[1]) - wall[1] * inset
                      + wall[0] * (self.rng() - 0.5) * 1.6)
            else:
                px = self.world_x(cand[0]) + (self.rng() - 0.5) * 1.4
                pz = self.world_z(cand[1]) + (self.rng() - 0.5) * 1.4
            self.water_spots.append((px, pz))

    def _place_false_exits(self):
        """TS 步骤 8.8：假 EXIT 牌 6 块。牌面朝向用不到，但每块消费 1 次 rng
        （randInt(0,3)），实体出生在它后面，必须原样走一遍。"""
        reachable = self.reachable
        sign_candidates = shuffle(
            self.rng, reachable[int(math.floor(len(reachable) * 0.25)):])
        sign_cells = []
        for cand in sign_candidates:
            if len(self.false_exits) >= 6:
                break
            if any(abs(p[0] - cand[0]) + abs(p[1] - cand[1]) < 8 for p in sign_cells):
                continue
            sign_cells.append(cand)
            self.false_exits.append(cand)
            _ = rand_int(self.rng, 0, 3)   # 牌面 yaw = randInt(0,3) * π/2，只消费流

    def _pick_entity_spawn(self):
        """TS 步骤 9：实体出生格 = 可达表 70% 之后的远池里随机一格（恰好 1 次 rng）。
        实体苏醒（首页或开局 45s）后传送到这个格的格心。"""
        far_pool = self.reachable[int(math.floor(len(self.reachable) * 0.7)):]
        e = far_pool[rand_int(self.rng, 0, len(far_pool) - 1)]
        self.entity_spawn_cell = (e[0], e[1])

    # ── 内挂用的寻路（不属于 TS 移植，是本包加的） ────────────────────────
    def find_path(self, from_cell, to_cell):
        """格间 A*（4 邻，代价 1）。返回格心世界坐标的路径（含起点与终点格心）。"""
        import heapq
        S = self.SIZE
        start, goal = tuple(from_cell), tuple(to_cell)
        if self.is_blocked(*goal):
            return None
        h = lambda c: abs(c[0] - goal[0]) + abs(c[1] - goal[1])
        open_heap = [(h(start), 0, start)]
        came, gscore = {}, {start: 0}
        while open_heap:
            _, g, cur = heapq.heappop(open_heap)
            if cur == goal:
                path = [cur]
                while cur in came:
                    cur = came[cur]
                    path.append(cur)
                path.reverse()
                return [(self.world_x(x), self.world_z(z)) for x, z in path]
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if not self.can_move(cur[0], cur[1], dx, dz):
                    continue
                nxt = (cur[0] + dx, cur[1] + dz)
                ng = g + 1
                if ng < gscore.get(nxt, 1 << 30):
                    gscore[nxt] = ng
                    came[nxt] = cur
                    heapq.heappush(open_heap, (ng + h(nxt), ng, nxt))
        return None

    def walk_distance(self, from_cell, to_cell):
        """两格间的 BFS 步数（找最近物资用；不可达返回 None）。"""
        path = self.find_path(from_cell, to_cell)
        return len(path) - 1 if path else None

    def distance_field(self, from_cell):
        """从某格出发的 BFS 距离场（实体威胁评估用；blocked 格为 -1）。
        **起点即使是柱格也照常扩散**——玩家/实体可以合法站在柱格四角的空地
        （游戏碰撞只把圆形足迹推出柱子本体，不管整格）。若把柱格起点当
        不可达直接返回空场，规避选点会拿不到任何撤离点、原地自旋到超时
        （实测教训，run2 的死锁现场就是玩家站在柱格里）。"""
        S = self.SIZE
        sx, sz = from_cell
        dist = [-1] * (S * S)
        if sx < 0 or sz < 0 or sx >= S or sz >= S:
            return dist
        dist[sz * S + sx] = 0
        queue = [(sx, sz)]
        qi = 0
        while qi < len(queue):
            cx, cz = queue[qi]
            qi += 1
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if not self.can_move(cx, cz, dx, dz):
                    continue
                ni = (cz + dz) * S + (cx + dx)
                if dist[ni] == -1:
                    dist[ni] = dist[cz * S + cx] + 1
                    queue.append((cx + dx, cz + dz))
        return dist

    def corridor_clear(self, ax, az, bx, bz, margin=0.34, step=0.25):
        """直线 a→b 沿途是否留有 `margin` 米余量（含柱子），用于路径拉直。"""
        dist = math.hypot(bx - ax, bz - az)
        n = max(1, int(math.ceil(dist / step)))
        for i in range(n + 1):
            t = i / n
            px, pz = ax + (bx - ax) * t, az + (bz - az) * t
            if self._solid_at_world_margin(px, pz, margin):
                return False
        return True

    def _solid_at_world_margin(self, px, pz, margin):
        """solid_at_world 的带余量版：点周围 margin 米内不许有实体。"""
        if self.solid_at_world(px, pz):
            return True
        c = self.cell_of(px, pz)
        cx, cz = c
        T = WALL_HALF + margin
        # 只查当前格四面墙 + 九格内的柱子，够 corridor_clear 用
        if self.has_wall_v(cx, cz) and px - (self.world_x(cx) - CELL / 2) <= T:
            return True
        if self.has_wall_v(cx + 1, cz) and (self.world_x(cx) + CELL / 2) - px <= T:
            return True
        if self.has_wall_h(cx, cz) and pz - (self.world_z(cz) - CELL / 2) <= T:
            return True
        if self.has_wall_h(cx, cz + 1) and (self.world_z(cz) + CELL / 2) - pz <= T:
            return True
        for dz in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nx, nz = cx + dx, cz + dz
                if self.cell(nx, nz) != PILLAR:
                    continue
                if (abs(px - self.world_x(nx)) <= PILLAR_HALF + margin
                        and abs(pz - self.world_z(nz)) <= PILLAR_HALF + margin):
                    return True
        return False

    def describe(self):
        """给日志/调试的一行摘要。"""
        return ("seed=%d spawn=(%.2f,%.2f)@%s pages=%d waters=%d exit@%s entity@%s"
                % (self.seed, self.spawn[0], self.spawn[1], self.spawn_cell,
                   len(self.page_spots), len(self.water_spots),
                   self.exit["cell"] if self.exit else None,
                   self.entity_spawn_cell))


# ── 离线自测 ────────────────────────────────────────────────────────────
def _selftest():
    import sys
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✔ " if cond else "  ✘ ") + msg)
        if not cond:
            ok = False

    for seed in (1234, 777, 42):
        print("seed=%d" % seed)
        lv = Level(seed)
        S = lv.SIZE
        check(len(lv.page_spots) == 8, "8 张页 (got %d)" % len(lv.page_spots))
        check(len(lv.water_spots) == 4, "4 瓶杏仁水 (got %d)" % len(lv.water_spots))
        # 每瓶水必须落在开阔地且不穿墙
        for i, (px, pz) in enumerate(lv.water_spots):
            check(not lv.solid_at_world(px, pz),
                  "水 %d (%.2f,%.2f) 不在墙/柱里" % (i, px, pz))
            check(lv.dist_from_spawn[lv.cell_of(px, pz)[1] * S + lv.cell_of(px, pz)[0]] > 0,
                  "水 %d 从出生点可达" % i)
        # 每张页的取页站位（法线退 1.35m）也不许穿墙
        for i, pg in enumerate(lv.page_spots):
            sx = pg["pos"][0] + pg["normal"][0] * 1.35
            sz = pg["pos"][2] + pg["normal"][1] * 1.35
            check(not lv.solid_at_world(sx, sz), "页 %d 站位不穿墙" % i)
        # 出口：边界环格、门位在墙面上、朝内站位可站
        ex = lv.exit
        check(ex["cell"][0] in (0, S - 1) or ex["cell"][1] in (0, S - 1),
              "出口格 %s 在边界环上" % (ex["cell"],))
        stand = (ex["door_pos"][0] + ex["facing_in"][0] * 1.2,
                 ex["door_pos"][1] + ex["facing_in"][1] * 1.2)
        check(not lv.solid_at_world(*stand), "出口门前站位不穿墙")
        # 实体出生格：远池（可达表 70% 之后）
        far_pool = lv.reachable[int(math.floor(len(lv.reachable) * 0.7)):]
        check(lv.entity_spawn_cell in [(c[0], c[1]) for c in far_pool],
              "实体出生格 %s 在远池" % (lv.entity_spawn_cell,))
        # 出口门与 8 页全都从出生点可达
        check(lv.walk_distance(lv.spawn_cell, ex["cell"]) is not None, "出口可达")
        for i, pg in enumerate(lv.page_spots):
            check(lv.walk_distance(lv.spawn_cell, pg["cell"]) is not None, "页 %d 可达" % i)
        print("  " + lv.describe())
        print("  waters:", [(round(px, 2), round(pz, 2)) for px, pz in lv.water_spots])
        print("  pages :", [(round(p["pos"][0], 1), round(p["pos"][2], 1)) for p in lv.page_spots])
        print("  exit  : cell=%s door=(%.2f,%.2f) facing_in=%s"
              % (ex["cell"], ex["door_pos"][0], ex["door_pos"][1], ex["facing_in"]))

    # 地面真值：seed=777 的历史失败局里，实体苏醒后首次观测在 (9.54, -85.71)，
    # 即格 (26,2) 格心 (10,-86) 附近加 roam 漂移——移植对不对这一格见分晓
    lv = Level(777)
    check(lv.entity_spawn_cell == (26, 2),
          "seed=777 实体出生格 = (26,2)（历史局地面真值，got %s）"
          % (lv.entity_spawn_cell,))

    print("自测结论：", "全部通过" if ok else "有失败项")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _selftest()
