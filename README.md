# backroom-wrapper-replay —— Backrooms 的内挂版 wrapper 包

围绕同级目录的 **Backroom-Wrapper**（游戏 harness，Backrooms — Level 0）做的内挂包：
一条 `./run.sh` 跑完「**固定随机 → 内挂自动游玩找到一页纸 → 自动录制 MP4 + log**」，
产物按「一次运行一个目录」落在本包 `recordings/backrooms/` 下。

本包不复制也不改动 Backroom-Wrapper 的任何文件——harness、adapter、游戏静态产物
全部直接复用（`wrapper.conf` 里的 `WRAPPER_DIR` 指过去），这里只有内挂 bot 与编排脚本。

## 用法

```bash
./run.sh                       # 默认：seed=1234，找到 1 张日记页即成功
./run.sh --seed 777            # 换一粒种子 = 换一张迷宫（内挂照样找页）
./run.sh --pages 3             # 多收几张（注意：第 1 张起实体会苏醒进入追逐）
./run.sh --max-seconds 120     # 收紧一局的墙钟预算
./run.sh --fps 15              # 降录制帧率（省磁盘）
```

前置条件与 Backroom-Wrapper 相同（Xvfb、ffmpeg、Chrome、PulseAudio 可选；
`../Backroom-Wrapper/check_env.sh` 全绿即可）。

## 一次运行长什么样

```
./run.sh
  ├─ 起 harness（复用 ../Backroom-Wrapper/modeB，--clock realtime --channel numeric，
  │    虚拟屏 Xvfb + Chrome/SwiftShader，MODEB_RECORDINGS 指到本包 recordings/）
  ├─ 内挂 bot（bot/pagefinder.py）接管：
  │    1. reset(seed=N)          ← 固定随机：迷宫/8 张页/4 瓶水/出口门/实体位置全由种子定
  │    2. 离线重建迷宫 + 校验出生点（bot/levelgen.py，与游戏逐位一致才算数）
  │    3. rec_start              ← 自动开录（X11 抓帧 → ffmpeg 管线，30fps）
  │    4. A* 找最近一张页 → 边走边修正视角 → 站到页对面 → 瞄准 → [E] TAKE PAGE 提示
  │       出现时按 e 取页（拿到第 1 张时实体苏醒，正好收工）
  │    5. rec_stop + rec_keep    ← 录制落盘，bot 决策日志与总结写进同一目录
  └─ 收摊：harness 退出，打印产物路径
```

## 产物（`recordings/backrooms/<录制 id>/`）

| 文件 | 内容 |
|---|---|
| `video.mp4` | 本局完整外观录像（1024x768@30fps，含声音） |
| `state.jsonl` | 逐帧游戏状态：位置/朝向/页数/提示/实体/体力……（录制期间每帧一行） |
| `input.jsonl` | 内挂发出的每一个键鼠事件与时刻——就是本局的**输入序列** |
| `frames.jsonl` | 视频每一帧的抓取时刻与复用标记（帧号↔时间对齐用） |
| `audio.wav` | 本局声音（PulseAudio 可用时；已混进 video.mp4） |
| `meta.json` | 录制元信息与质检（帧数/丢帧/状态行密度……） |
| `bot_log.jsonl` | **内挂决策日志**：每个控制周期的观测、决策与理由 + 里程碑 |
| `summary.json` | 本局总结：种子、成功与否、用时、页位、实体状态、产物清单 |

固定种子 + `input.jsonl` + `state.jsonl` 三样放在一起，一局行为可以离线复盘；
想复现同一张迷宫，`./run.sh --seed 同样的数` 即可。

## 「内挂」是什么意思

Backroom-Wrapper 的玩家模型只能「发键鼠、看画面」（pixel 信道）。本包的 bot 是
**内挂**：走 harness 的 **numeric 信道**（桥接每帧推送的结构化状态——位置、朝向、
页数、交互提示、实体），并拿种子在游戏外把整张迷宫重算一遍（`bot/levelgen.py`
对照上游 `level.ts` 逐位移植），从而**提前知道每张页钉在哪面墙上**，直接规划路线。
输入仍走 wrapper 的标准键鼠通道（`modeB/client.py` 的 `act`），与真实玩家同一条
链路，也正因如此 `input.jsonl` 才是完整有效的输入序列。

## 随机性固定到什么程度

**由种子固定（同种子必同图）**：迷宫结构、程序化贴图、8 张页的钉放位置、4 瓶水的
位置、出口门位置、实体初始位置与苏醒参数、异常灯光区。内挂每局 `reset(seed=N)`
（`wrapper.conf` 的 `SEED`，默认 1234），种子写进 `bot_log.jsonl` 与 `summary.json`。

**不固定（游戏本体的装饰性随机，`Math.random`）**：灯光爆闪/回闪的取位与时机、
实体追逐中的抖动、镜头晃动噪声、页面在墙上微摆的相位。它们影响不了迷宫与页位，
也不影响「找到一页纸」这条主线——这是 Backroom-Wrapper 的 SCOPE.md 明文记录的
本体行为，本包原样保留，不在录制里做手脚。

另注：**录制只认流式档**（harness 的 `rec_start` 在冻结档下直接拒绝），所以本包
固定用 `--clock realtime`——画面、声音按真实时间录，游戏与墙钟约 1:1
（SwiftShader 软渲染，见 Backroom-Wrapper README 的实测说明）。

## 实现要点（踩过的坑）

- **鼠标视角的像素代数**：游戏灵敏度 0.0021 弧度/像素，单事件超过 600px 会被当
  垃圾**整事件丢弃**、280px 以内全额生效。内挂把转向拆成 ≤280px 的块连发；
  光标漂出中心 600px 后先发一次「回中」事件（位移必然 >600 → 游戏丢弃旋转，
  但页面侧光标模型归零）。这套代数保证转向精确到 0.06°。
- **numeric 信道的双层嵌套**：harness 把桥接观测包了一层
  `{t_ms, frame, state: <桥接观测>}`，桥接观测里才有 `state`（字符串）等字段。
  bot 的 `observe()` 里拆包，别拿外层的 `state`（是个 dict）当状态机用。
- **`PIPESTATUS` 要紧跟管道取**：中间隔着任何命令都会被冲掉，退出码会静默变 0。
- **出生点校验是移植的试金石**：`levelgen` 生成的出生点与观测对不上就立刻报错，
  绝不带着错图导航。（实测 seed=1234 误差 0.0m，页位经实际取页验证。）
- **录制画面顶部有 Chrome 的 `--no-sandbox` 提示条**：Backroom-Wrapper 的 adapter
  以 `--no-sandbox` 起 Chrome（服务器内核下沙箱起不来），Chrome 自己画上去的，
  原包录制里同样存在，不是本包引入的。

## 包里有什么

| 路径 | 内容 |
|---|---|
| `run.sh` | 编排：起 harness → 跑内挂 → 收摊；参数见文件头 |
| `wrapper.conf` | `WRAPPER_DIR`、`SEED`、`PAGES_GOAL`、`REC_FPS`、`MAX_SECONDS`、`READY_TIMEOUT` |
| `bot/levelgen.py` | 迷宫生成逐位移植（mulberry32/递归分割/页位），含 A* 与直线可走性检查 |
| `bot/pagefinder.py` | 内挂：观测拆包、鼠标代数、反馈式走路、取页、录制生命周期、日志 |
| `recordings/` | 产物（`backrooms/<录制 id>/` 一局一目录） |
| `logs/` | 每次运行的控制台全文（`<运行名>.log`） |
