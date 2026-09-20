#!/bin/bash
# backroom-wrapper-replay 启动器：一条命令跑完「固定种子 → 内挂自动游玩找到一页纸
# → 自动录制 MP4 + log」。
#
# 围绕同级目录的 Backroom-Wrapper 工作：harness、adapter、游戏产物全部复用它
# （路径见 wrapper.conf 的 WRAPPER_DIR），本包只贡献内挂 bot 与编排脚本。
#
# 用法：
#   ./run.sh [选项]
#     --seed N          固定随机种子（默认取 wrapper.conf 的 SEED=1234）
#     --pages N         目标页数（默认 1 —— 本阶段的游戏目标是找到一页纸）
#     --max-seconds N   一局墙钟预算，超时判负（默认 wrapper.conf 的 MAX_SECONDS）
#     --fps N           MP4 录制帧率（默认 wrapper.conf 的 REC_FPS）
#     --run NAME        运行名（默认 replay-<时间戳>；产物目录与之关联）
#
# 产物（每次运行一组）：
#   recordings/backrooms/<录制 id>/
#     video.mp4          本局完整外观录像（X11 抓帧 → ffmpeg，30fps，含声音）
#     state.jsonl        逐帧游戏状态（玩家位置/朝向/页数/实体/提示……）
#     input.jsonl        内挂发出的每一个键鼠事件（= 本局的输入序列）
#     frames.jsonl       视频每一帧对应的抓取时刻与复用标记
#     audio.wav          本局声音（PulseAudio 可用时）
#     meta.json          录制元信息与质检
#     bot_log.jsonl      内挂决策日志（每个控制周期的观测、决策与理由）
#     summary.json       本局总结（种子、结果、用时、页的位置、产物清单）
#   logs/<运行名>.log    本次运行的完整控制台输出
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/wrapper.conf"

SEED_ARG=""; PAGES_ARG=""; MAXS_ARG=""; FPS_ARG=""; RUN="replay-$(date +%Y%m%d-%H%M%S)"
while [ $# -gt 0 ]; do
  case "$1" in
    --seed) SEED_ARG="$2"; shift 2;;
    --pages) PAGES_ARG="$2"; shift 2;;
    --max-seconds) MAXS_ARG="$2"; shift 2;;
    --fps) FPS_ARG="$2"; shift 2;;
    --run) RUN="$2"; shift 2;;
    *) echo "不认识的参数：$1（用法见文件头注释）"; exit 2;;
  esac
done
[ -n "$SEED_ARG" ] && SEED="$SEED_ARG"
[ -n "$PAGES_ARG" ] && PAGES_GOAL="$PAGES_ARG"
[ -n "$MAXS_ARG" ] && MAX_SECONDS="$MAXS_ARG"
[ -n "$FPS_ARG" ] && REC_FPS="$FPS_ARG"

# ── 前置检查（缺什么直说，不等到半路才炸）──────────────────────────────
for c in python3 Xvfb ffmpeg; do
  command -v "$c" >/dev/null 2>&1 || { echo "缺 $c —— 装好后重试（参考 Backroom-Wrapper/check_env.sh）"; exit 1; }
done
[ -d "$WRAPPER_DIR/modeB" ] || { echo "找不到 $WRAPPER_DIR/modeB —— wrapper.conf 的 WRAPPER_DIR 指错了吗？"; exit 1; }
[ -f "$WRAPPER_DIR/game/index.html" ] || { echo "找不到 $WRAPPER_DIR/game/index.html —— 先在 Backroom-Wrapper 里跑 source/build_game.sh"; exit 1; }

# 挑一个没人用的虚拟屏号与端口（避开别的 harness 实例）
pick_display() {
  for _ in $(seq 150 199); do
    [ -e "/tmp/.X11-unix/X$_" ] || { echo ":$_"; return; }
  done
  echo "150-199 的虚拟屏号都被占了"; exit 1
}
DISP="$(pick_display)"
GAME_PORT=$(( 20000 + RANDOM % 20000 ))

# socket 路径太长时 harness 会改绑到 /tmp 下，并在原位置旁边的 .path 里记下实际路径
sock_of() { if [ -f "$1.path" ]; then cat "$1.path"; else echo "$1"; fi; }
MB="$WRAPPER_DIR/modeB"
DATA="$MB/runs/$RUN/modeB.sock"
CTRL_RAW="$MB/runs/_control/$RUN.sock"
rm -f "$(sock_of "$DATA")" "$DATA" "$DATA.path" "$(sock_of "$CTRL_RAW")" "$CTRL_RAW" "$CTRL_RAW.path" 2>/dev/null || true

mkdir -p "$HERE/recordings" "$HERE/logs"
LOG="$HERE/logs/$RUN.log"

# ── 起 harness（流式档 + numeric 信道：录制只认流式；bot 只读结构化状态）──
# MODEB_RECORDINGS 指到本包：保留的录制落在本包 recordings/backrooms/ 下。
cd "$MB"
MODEB_RECORDINGS="$HERE/recordings" python3 -u harness.py \
    --adapter backrooms --game backrooms --size 1024x768 \
    --display "$DISP" --run "$RUN" \
    --clock realtime --channel numeric \
    --port "$GAME_PORT" --ready-timeout "$READY_TIMEOUT" \
    > "$LOG" 2>&1 &
HP=$!
BOTPID=""

cleanup() {
  [ -n "$BOTPID" ] && kill "$BOTPID" 2>/dev/null || true
  kill -TERM "$HP" 2>/dev/null || true
  wait "$HP" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

fail() {
  echo
  echo "$1"
  echo "harness 日志最后 20 行（完整日志 $LOG）："
  tail -20 "$LOG" || true
  exit 1
}

# ── 等游戏就绪 ──────────────────────────────────────────────────────────
LIMIT=$(( READY_TIMEOUT + 60 ))
printf "等游戏就绪（上限 %s 秒，虚拟屏 %s，流式档）" "$LIMIT" "$DISP"
T0=$SECONDS
SOCK=""
while :; do
  S="$(sock_of "$DATA")"
  if [ -S "$S" ]; then SOCK="$S"; break; fi
  kill -0 "$HP" 2>/dev/null || fail "harness 在游戏就绪前退出了。"
  [ $(( SECONDS - T0 )) -lt "$LIMIT" ] || fail "等了 $LIMIT 秒游戏还没就绪。"
  sleep 1
  [ $(( (SECONDS - T0) % 5 )) -ne 0 ] || printf "."
done
echo
CTRL="$(sock_of "$CTRL_RAW")"
echo "游戏已就绪（$(( SECONDS - T0 )) 秒）：$SOCK"

# ── 跑内挂：固定种子 → 自动游玩到拿到一页纸 → 自动收 MP4 与 log ─────────
echo
echo "内挂启动：seed=$SEED 目标=$PAGES_GOAL 页，录制 ${REC_FPS}fps，预算 ${MAX_SECONDS}s"
echo
set +e
REPLAY_RUN="$RUN" python3 -u "$HERE/bot/pagefinder.py" \
    --socket "$SOCK" \
    --control "$CTRL" \
    --modeb-dir "$MB" \
    --seed "$SEED" \
    --pages-goal "$PAGES_GOAL" \
    --max-seconds "$MAX_SECONDS" \
    --rec-fps "$REC_FPS" 2>&1 | tee -a "$LOG"
BOTPID=""
RC="${PIPESTATUS[0]}"
set -e

echo
if [ "$RC" -eq 0 ]; then
  echo "✔ 本局完成。产物目录见上方 summary；控制台全文在 $LOG"
else
  echo "✘ 本局未达成（退出码 $RC）。录制与 log 已保留，控制台全文在 $LOG"
fi
exit "$RC"
