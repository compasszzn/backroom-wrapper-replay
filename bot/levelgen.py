# -*- coding: utf-8 -*-
"""Backrooms — Level 0 迷宫生成的 Python 移植（给内挂离线复现用）。

为什么需要它：观测里**故意不给** 8 张日记页的坐标（那是「找」这道题的分母），
但整张迷宫由种子经 mulberry32 决定（SCOPE.md「随机性」一节：同种子必同图）。
内挂拿着种子在游戏外把同一份生成算法再跑一遍，就得到墙、出生点和每张页的
钉放位置——这是「内挂」区别于普通玩家的特权信息，也是自动找页的路线依据。

移植基准：Backroom-Erase 上游 `app/game/engine/level.ts` + `rng.ts`
（补丁基准提交 26e5481，本包经 source/game.patch 核对过：生成逻辑未被改动）。
只移植到第 8 步（pageSpots 完成）为止——之后的墙涂鸦/杏仁水/假出口/实体出生/
灯具都不消耗在页之前的随机流，内挂用不到。

**必须逐位一致**：JS 的 Math.imul / |0 / >>> 语义、`&&`/`||` 的短路求值顺序、
shuffle 的调用次数、以及每一条 rng() 出现的位置。任何一处不对，页就会钉在
别的墙上。移植时逐行对着源码抄，别「顺手优化」。
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
        self.page_spots = []          # [{pos:(x,y,z), normal:(nx,nz)}]
        self.spawn = (0.0, 0.0)      # 世界坐标
        self.spawn_cell = (0, 0)
        self.dist_from_spawn = None
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

        # 7) 出口格：只挑不消费随机流（computeExit 不动 rng），内挂用不到，跳过。

        # 8) 八张页按距离带钉墙（pageSpots 由此定），见 _place_pages
        self._place_pages()

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

    def _adjacent_wall(self, x, z):
        """格 (x,z) 边上某面墙的法线（指向格内）。**有候选墙就会消费 rng**（shuffle），
        与 TS 的 adjacentWall 一致——调用方（过滤/落页）全按同一顺序，别改。"""
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

    def _place_page(self, cand, chosen):
        """落一张页：先再问一次邻墙（消费 shuffle 的 rng），再取侧偏与高度（2 次 rng）。
        成功时把候选格记入 chosen（TS 里就在 placePage 里 push）。"""
        x, z = cand[0], cand[1]
        wall = self._adjacent_wall(x, z)
        if wall is None:
            return False
        chosen.append((x, z))
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
        chosen = []

        def too_close(cand, min_gap):
            return any(abs(p[0] - cand[0]) + abs(p[1] - cand[1]) < min_gap for p in chosen)

        # 带 0 是新手页：离出生点 2..6 步 BFS（~8-24 米）。选不出就把环放大直到选出。
        # 过滤条件里 _adjacent_wall 会消费 rng——顺序与 TS 的短路一致。
        max_d = 6
        while len(chosen) == 0 and max_d <= 30:
            pool = shuffle(rng, [c for c in reachable
                                 if c[2] >= 2 and c[2] <= max_d
                                 and self._adjacent_wall(c[0], c[1]) is not None])
            for cand in pool:
                if self._place_page(cand, chosen):
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
                if self._place_page(cand, chosen):
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
            self._place_page(cand, chosen)

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
        """两格间的 BFS 步数（找最近页用；不可达返回 None）。"""
        path = self.find_path(from_cell, to_cell)
        return len(path) - 1 if path else None

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
        return ("seed=%d spawn=(%.2f,%.2f)@%s pages=%d" %
                (self.seed, self.spawn[0], self.spawn[1], self.spawn_cell,
                 len(self.page_spots)))
