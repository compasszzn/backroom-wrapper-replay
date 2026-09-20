# backroom-wrapper-replay —— Backrooms 的内挂版 wrapper 包

围绕同级目录的 **Backroom-Wrapper**（游戏 harness，Backrooms — Level 0）做的内挂包：
一条 `./run.sh` 跑完「**固定随机 → 内挂按策略自动游玩 → 自动录制 MP4 + log**」。
产物按「**一个策略一个文件夹**」组织：策略的代码、每次运行的录制与日志，
全部落在 `strategies/<策略名>/` 下面。

本包不复制也不改动 Backroom-Wrapper 的任何文件——harness、adapter、游戏静态产物
全部直接复用（`wrapper.conf` 里的 `WRAPPER_DIR` 指过去），这里只有内挂 bot 与编排脚本。

## 策略目录（strategies/）

| 策略 | 目标 | 状态 |
|---|---|---|
| [`01-grab-one-page`](strategies/01-grab-one-page/) | 固定种子找到 **1 张日记页**（入门目标），不动实体 | 已验证（seed 1234/777 多局全绿） |
| [`02-full-clear-escape`](strategies/02-full-clear-escape/) | 固定种子**收齐 8 页 + 喝光 4 瓶杏仁水 + 推开出口门走进亮光逃脱**，全程带实体规避 | 已验证（seed=1234 完整逃脱 661.6s，实体最近 5.6m；五轮迭代史见该策略 README） |

每个策略文件夹自带：

| 路径 | 内容 |
|---|---|
| `README.md` | 这个策略满足什么（决策规则表）、验证结论、产物清单 |
| `bot/` | 该策略的内挂代码（levelgen 迷宫移植 + bot 本体），自包含可独立运行 |
| `recordings/backrooms/<id>/` | 本策略的录制（一局一目录，见下表） |
| `logs/<run>.log` | 每次运行的控制台全文，与录制的 `summary.json` 的 `run` 字段对应 |

## 用法

```bash
./run.sh                                          # 默认策略 02：全收集+逃脱，seed=1234
./run.sh --strategy 01-grab-one-page              # 换回「找到一页纸」策略
./run.sh --seed 777                               # 换一粒种子 = 换一张迷宫
./run.sh --strategy 01-grab-one-page --pages 3    # 多收几张（策略 01 专属参数）
./run.sh --max-seconds 120                       # 收紧一局的墙钟预算
./run.sh --fps 15                                 # 降录制帧率（省磁盘）
```

前置条件与 Backroom-Wrapper 相同（Xvfb、ffmpeg、Chrome、PulseAudio 可选；
`../Backroom-Wrapper/check_env.sh` 全绿即可）。

## 一次运行长什么样

```
./run.sh --strategy 02-full-clear-escape
  ├─ 起 harness（复用 ../Backroom-Wrapper/modeB，--clock realtime --channel numeric，
  │    虚拟屏 Xvfb + Chrome/SwiftShader，MODEB_RECORDINGS 指到该策略的 recordings/）
  ├─ 内挂 bot（策略的 bot/fullclear.py）接管：
  │    1. reset(seed=N)          ← 固定随机：迷宫/8 张页/4 瓶水/出口门/实体位置全由种子定
  │    2. 离线重建迷宫 + 校验出生点（bot/levelgen.py，与游戏逐位一致才算数）
  │    3. rec_start              ← 自动开录（X11 抓帧 → ffmpeg 管线，30fps）
  │    4. 按策略游玩（贪心近邻巡回 12 个物资点，实体逼近即规避，
  │       8 页集齐后开门走进亮光，state=won 即逃脱成功）
  │    5. rec_stop + rec_keep    ← 录制落盘，bot 决策日志与总结写进同一目录
  └─ 收摊：harness 退出，打印产物路径
```

## 单局产物（`strategies/<策略>/recordings/backrooms/<录制 id>/`）

| 文件 | 内容 |
|---|---|
| `video.mp4` | 本局完整外观录像（1024x768@30fps，含声音） |
| `state.jsonl` | 逐帧游戏状态：位置/朝向/页数/提示/实体/体力……（录制期间每帧一行） |
| `input.jsonl` | 内挂发出的每一个键鼠事件与时刻——就是本局的**输入序列** |
| `frames.jsonl` | 视频每一帧的抓取时刻与复用标记（帧号↔时间对齐用） |
| `audio.wav` | 本局声音（PulseAudio 可用时；已混进 video.mp4） |
| `meta.json` | 录制元信息与质检（帧数/丢帧/状态行密度……） |
| `bot_log.jsonl` | **内挂决策日志**：每个控制周期的观测、决策与理由 + 里程碑 |
| `summary.json` | 本局总结：种子、成功与否、用时、关键位置、产物清单 |

固定种子 + `input.jsonl` + `state.jsonl` 三样放在一起，一局行为可以离线复盘；
想复现同一张迷宫，`./run.sh --seed 同样的数` 即可。

## 「内挂」是什么意思

Backroom-Wrapper 的玩家模型只能「发键鼠、看画面」（pixel 信道）。本包的 bot 是
**内挂**：走 harness 的 **numeric 信道**（桥接每帧推送的结构化状态——位置、朝向、
页数、交互提示、实体），并拿种子在游戏外把整张迷宫重算一遍（各策略的
`bot/levelgen.py` 对照上游 `level.ts` 逐位移植），从而**提前知道每张页钉在哪面墙上、
每瓶水放在哪块地上、出口门在哪面墙上、实体会从哪个格子出生**，直接规划路线。
输入仍走 wrapper 的标准键鼠通道（`modeB/client.py` 的 `act`），与真实玩家同一条
链路，也正因如此 `input.jsonl` 才是完整有效的输入序列。

## 随机性固定到什么程度

**由种子固定（同种子必同图）**：迷宫结构、程序化贴图、8 张页的钉放位置、4 瓶水的
位置、出口门位置、实体初始位置与苏醒参数、异常灯光区。内挂每局 `reset(seed=N)`
（`wrapper.conf` 的 `SEED`，默认 1234），种子写进 `bot_log.jsonl` 与 `summary.json`。

**不固定（游戏本体的装饰性随机，`Math.random`）**：灯光爆闪/回闪的取位与时机、
实体追逐中的抖动、镜头晃动噪声、页面在墙上微摆的相位、杏仁水瓶的朝向。它们影响
不了迷宫与物资点位，这是 Backroom-Wrapper 的 SCOPE.md 明文记录的本体行为，本包
原样保留，不在录制里做手脚。

另注：**录制只认流式档**（harness 的 `rec_start` 在冻结档下直接拒绝），所以本包
固定用 `--clock realtime`——画面、声音按真实时间录，游戏与墙钟约 1:1
（SwiftShader 软渲染，见 Backroom-Wrapper README 的实测说明）。

## 实现要点（踩过的坑）

- **鼠标视角的像素代数**：游戏灵敏度 0.0021 弧度/像素，单事件超过 600px 会被当
  垃圾**整事件丢弃**、280px 以内全额生效。内挂把转向拆成 ≤280px 的块连发；
  光标漂出中心 600px 后先发一次「回中」事件（位移必然 >600 → 游戏丢弃旋转，
  但页面侧光标模型归零）。策略 02 把同一套代数扩展到了纵向（俯仰角），瞄准
  地上的杏仁水瓶才够得着交互提示。
- **numeric 信道的双层嵌套**：harness 把桥接观测包了一层
  `{t_ms, frame, state: <桥接观测>}`，桥接观测里才有 `state`（字符串）等字段。
  bot 的 `observe()` 里拆包，别拿外层的 `state`（是个 dict）当状态机用。
- **`PIPESTATUS` 要紧跟管道取值**：中间隔着任何命令都会被冲掉，退出码会静默变 0。
- **出生点校验是移植的试金石**：`levelgen` 生成的出生点与观测对不上就立刻报错，
  绝不带着错图导航。（实测 seed=1234/777 误差 0.0m，页位经实际取页验证。）
- **双层护栏**：bot 内部有 `MAX_SECONDS` 预算（每圈检查），run.sh 外面再套一层
  `timeout`（预算 +120s）兜 socket 卡死这类 bot 自己检查不到的挂起。
- **失败也留全现场**：开过录的失败局照常 `rec_stop`/`rec_keep`（录像里有失败
  过程），没开录就炸的局把 `bot_log.jsonl`/`summary.json(success:false)` 落到
  `logs/bot-failure-<时间戳>/`——决策过程永远不蒸发。
- **录制画面顶部有 Chrome 的 `--no-sandbox` 提示条**：Backroom-Wrapper 的 adapter
  以 `--no-sandbox` 起 Chrome（服务器内核下沙箱起不来），Chrome 自己画上去的，
  原包录制里同样存在，不是本包引入的。

## 包里有什么

| 路径 | 内容 |
|---|---|
| `run.sh` | 编排：起 harness → 按策略跑内挂 → 收摊；`--strategy` 分派，参数见文件头 |
| `wrapper.conf` | `WRAPPER_DIR`、`SEED`、`PAGES_GOAL`（策略 01）、`REC_FPS`、各策略 `MAX_SECONDS`、`READY_TIMEOUT` |
| `strategies/<策略>/README.md` | 策略说明：满足的目标、决策规则、验证结论 |
| `strategies/<策略>/bot/` | 该策略的内挂代码（迷宫移植 + bot 本体） |
| `strategies/<策略>/recordings/` | 该策略的录制（`backrooms/<录制 id>/` 一局一目录） |
| `strategies/<策略>/logs/` | 该策略每次运行的控制台全文（`<运行名>.log`） |
