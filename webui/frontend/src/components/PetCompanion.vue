<!-- ╔══════════════════════════════════════════════╗ -->
<!-- ║             👑:anzi开发by:anzi👑             ║ -->
<!-- ╚══════════════════════════════════════════════╝ -->
<template>
  <Teleport to="body"> <!-- 把宠物放到页面最外层 -->
    <button v-if="!visible" class="pet-summon" type="button" @click="togglePet">助手</button> <!-- 隐藏时显示召唤按钮 -->
    <section v-if="visible" class="pet-float" :class="{ 'is-dragging': dragging }" :style="floatStyle"> <!-- 显示悬浮宠物 -->
      <button class="pet-avatar" type="button" @pointerdown="startDrag" @click="petFriend"> <!-- 宠物本体可拖动可互动 -->
        <span v-if="spritesheetReady" class="pet-sprite" :class="spriteStateClass"></span> <!-- 显示精灵表动画 -->
        <img v-else class="pet-gif" :src="fallbackSrc" :alt="petMeta.displayName" /> <!-- 没有精灵表时显示 GIF -->
      </button>
      <div class="pet-popover"> <!-- 鼠标经过时弹出的操作面板 -->
        <div class="pet-popover-head"> <!-- 面板头部 -->
          <span class="pet-title">{{ petMeta.displayName }}</span> <!-- 宠物名字 -->
          <span class="pet-state">{{ stateLabel }}</span> <!-- 当前状态 -->
        </div>
        <p>{{ currentLine }}</p> <!-- 状态说明 -->
        <div class="pet-stats"> <!-- 状态数值 -->
          <span>专注 {{ focus }}</span> <!-- 专注值 -->
          <span>活力 {{ energy }}</span> <!-- 活力值 -->
          <span>亲密 {{ bond }}</span> <!-- 亲密值 -->
        </div>
        <div class="pet-actions"> <!-- 操作选项 -->
          <button type="button" @click="shuffleMood">随机状态</button> <!-- 随机切换状态 -->
          <button type="button" @click="petFriend">摸一摸</button> <!-- 互动宠物 -->
          <button type="button" @click="hidePet">隐藏</button> <!-- 隐藏宠物 -->
        </div>
      </div>
    </section>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"; // 导入 Vue 工具

type PetMeta = { // 定义宠物描述格式
  id: string; // 宠物编号
  displayName: string; // 宠物名字
  description: string; // 宠物说明
  spritesheetPath: string; // 精灵表路径
  fallbackPath: string; // 兜底 GIF 路径
  frameWidth: number; // 单帧宽度
  frameHeight: number; // 单帧高度
  columns: number; // 精灵表列数
  rows: number; // 精灵表行数
  states: string[]; // 状态列表
}; // 结束类型定义

const petMeta: PetMeta = { // 设置 Codex pets 兼容元数据
  id: "gecko-companion", // 设置宠物编号
  displayName: "小守宫", // 设置宠物名字
  description: "使用 hatch-pet 基于本地守宫 GIF 重新设计的 Codex pets 兼容宠物包。", // 设置宠物说明
  spritesheetPath: "spritesheet.webp", // 设置精灵表文件名
  fallbackPath: "reference.gif", // 设置兜底 GIF 文件名
  frameWidth: 192, // 设置单帧宽度
  frameHeight: 208, // 设置单帧高度
  columns: 8, // 设置精灵表列数
  rows: 9, // 设置精灵表行数
  states: ["idle", "running-right", "running-left", "waving", "jumping", "failed", "waiting", "running", "review"], // 设置状态顺序
}; // 结束元数据

const petBase = "/pets/gecko-companion"; // 设置宠物资源目录
const storageKey = "gpt-pp-team-pet"; // 设置本地保存键
const visible = ref(true); // 记录是否显示
const dragging = ref(false); // 记录是否拖动
const moved = ref(false); // 记录是否移动过
const spritesheetReady = ref(false); // 记录精灵表是否可用
const moodIndex = ref(0); // 记录状态编号
const bond = ref(42); // 记录亲密值
const focus = ref(68); // 记录专注值
const energy = ref(76); // 记录活力值
const x = ref(0); // 记录横向位置
const y = ref(0); // 记录纵向位置
const offsetX = ref(0); // 记录横向偏移
const offsetY = ref(0); // 记录纵向偏移
const typed = ref(""); // 记录快捷输入
let moodTimer: number | undefined; // 记录随机状态计时器

const moods = ["idle", "running-right", "running-left", "waving", "jumping", "failed", "waiting", "running", "review"] as const; // 定义界面状态
const lines = { // 定义中文提示
  idle: "我会安静地悬浮在这里，也会随机换动作，让桌面更灵动。", // 空闲提示
  "running-right": "我可以沿着右边跑动，适合任务开始时看一眼。", // 右跑提示
  "running-left": "我也能往左边跑，方向由 hatch-pet 安全镜像生成。", // 左跑提示
  waving: "我正在挥爪打招呼，动作只来自爪子姿态。", // 挥手提示
  jumping: "我正在跳一下，没有阴影、灰尘或多余特效。", // 跳跃提示
  failed: "我有点沮丧，适合失败或异常状态。", // 失败提示
  running: "我正在陪你盯着运行状态，适合长任务时放在角落。", // 运行提示
  waiting: "这里可能需要你确认，我会在旁边等你继续。", // 等待提示
  review: "任务完成啦，你可以让我换个状态或先隐藏起来。", // 完成提示
}; // 结束提示定义
const labels = { idle: "待命", "running-right": "向右跑", "running-left": "向左跑", waving: "挥手", jumping: "跳跃", failed: "失败", waiting: "等待确认", running: "运行中", review: "复查完成" }; // 定义状态名称

const mood = computed(() => moods[moodIndex.value]); // 计算当前状态
const stateLabel = computed(() => labels[mood.value]); // 计算中文状态名
const currentLine = computed(() => lines[mood.value]); // 计算中文说明
const fallbackSrc = computed(() => `${petBase}/${petMeta.fallbackPath}`); // 计算兜底图片路径
const spritesheetSrc = computed(() => `${petBase}/${petMeta.spritesheetPath}`); // 计算精灵表路径
const spriteStateClass = computed(() => `pet-sprite--${mood.value}`); // 计算精灵状态样式
const floatStyle = computed(() => ({ // 计算浮动样式
  "--pet-x": `${x.value}px`, // 设置横向位置
  "--pet-y": `${y.value}px`, // 设置纵向位置
  "--pet-sheet": `url("${spritesheetSrc.value}")`, // 设置精灵表背景
})); // 结束样式计算

function saveState(): void { // 保存状态
  localStorage.setItem(storageKey, JSON.stringify({ visible: visible.value, moodIndex: moodIndex.value, bond: bond.value, focus: focus.value, energy: energy.value, x: x.value, y: y.value })); // 写入本地存储
} // 结束保存函数

function loadState(): void { // 读取状态
  const raw = localStorage.getItem(storageKey); // 获取本地数据
  if (!raw) return; // 没有数据就退出
  const data = JSON.parse(raw) as Partial<{ visible: boolean; moodIndex: number; bond: number; focus: number; energy: number; x: number; y: number }>; // 解析本地数据
  visible.value = data.visible ?? visible.value; // 恢复显示状态
  moodIndex.value = Math.max(0, Math.min(moods.length - 1, data.moodIndex ?? moodIndex.value)); // 恢复安全状态编号
  bond.value = data.bond ?? bond.value; // 恢复亲密值
  focus.value = data.focus ?? focus.value; // 恢复专注值
  energy.value = data.energy ?? energy.value; // 恢复活力值
  x.value = data.x ?? x.value; // 恢复横向位置
  y.value = data.y ?? y.value; // 恢复纵向位置
} // 结束读取函数

function checkSpritesheet(): void { // 检查精灵表
  const image = new Image(); // 创建图片对象
  image.onload = () => { spritesheetReady.value = true; }; // 加载成功就使用精灵表
  image.onerror = () => { spritesheetReady.value = false; }; // 加载失败就使用 GIF
  image.src = spritesheetSrc.value; // 设置检查路径
} // 结束检查函数

function togglePet(): void { // 切换显示
  visible.value = !visible.value; // 反转显示状态
  saveState(); // 保存新状态
} // 结束切换函数

function hidePet(): void { // 隐藏宠物
  visible.value = false; // 设置隐藏
  saveState(); // 保存隐藏状态
} // 结束隐藏函数

function clampStat(value: number): number { // 限制数值范围
  return Math.max(0, Math.min(100, value)); // 保持数值在零到一百
} // 结束限制函数

function pickRandomMoodIndex(): number { // 随机选择状态编号
  if (moods.length <= 1) return 0; // 只有一个状态就返回零
  const offset = 1 + Math.floor(Math.random() * (moods.length - 1)); // 生成不为零的随机偏移
  return (moodIndex.value + offset) % moods.length; // 返回不同于当前的状态
} // 结束随机选择

function randomMoodDelay(): number { // 生成随机切换等待时间
  return 7000 + Math.floor(Math.random() * 9000); // 返回七到十六秒之间
} // 结束等待时间函数

function applyMood(nextIndex: number, focusDelta: number, energyDelta: number): void { // 应用新的宠物状态
  moodIndex.value = nextIndex; // 设置新状态编号
  focus.value = clampStat(focus.value + focusDelta); // 更新专注值
  energy.value = clampStat(energy.value + energyDelta); // 更新活力值
  saveState(); // 保存状态
} // 结束应用状态

function scheduleMoodShuffle(): void { // 安排下一次随机状态
  if (moodTimer) window.clearTimeout(moodTimer); // 清理旧计时器
  moodTimer = window.setTimeout(() => { // 创建新的随机计时器
    if (visible.value && !dragging.value) applyMood(pickRandomMoodIndex(), 1, -1); // 显示且不拖动时自动换状态
    scheduleMoodShuffle(); // 继续安排下一次
  }, randomMoodDelay()); // 使用随机等待时间
} // 结束随机安排

function stopMoodShuffle(): void { // 停止随机状态计时
  if (!moodTimer) return; // 没有计时器就退出
  window.clearTimeout(moodTimer); // 清除当前计时器
  moodTimer = undefined; // 重置计时器记录
} // 结束停止函数

function shuffleMood(): void { // 手动随机状态
  applyMood(pickRandomMoodIndex(), 4, -3); // 随机切换并调整数值
  scheduleMoodShuffle(); // 重置自动切换节奏
} // 结束手动随机函数

function petFriend(): void { // 互动宠物
  if (moved.value) return; // 拖动后不触发点击互动
  bond.value = Math.min(100, bond.value + 7); // 增加亲密值
  energy.value = Math.min(100, energy.value + 2); // 增加活力值
  saveState(); // 保存数值
} // 结束互动函数

function startDrag(event: PointerEvent): void { // 开始拖动
  dragging.value = true; // 标记拖动中
  moved.value = false; // 重置移动标记
  offsetX.value = event.clientX - x.value; // 计算横向偏移
  offsetY.value = event.clientY - y.value; // 计算纵向偏移
  window.addEventListener("pointermove", dragPet); // 监听移动
  window.addEventListener("pointerup", stopDrag); // 监听松开
} // 结束开始拖动

function dragPet(event: PointerEvent): void { // 拖动宠物
  if (!dragging.value) return; // 非拖动时退出
  moved.value = true; // 标记已经移动
  x.value = Math.max(12, Math.min(window.innerWidth - 116, event.clientX - offsetX.value)); // 限制横向范围
  y.value = Math.max(12, Math.min(window.innerHeight - 126, event.clientY - offsetY.value)); // 限制纵向范围
} // 结束拖动函数

function stopDrag(): void { // 停止拖动
  dragging.value = false; // 取消拖动状态
  window.removeEventListener("pointermove", dragPet); // 移除移动监听
  window.removeEventListener("pointerup", stopDrag); // 移除松开监听
  saveState(); // 保存位置
  window.setTimeout(() => { moved.value = false; }, 80); // 延迟恢复点击
} // 结束停止拖动

function placeDefault(): void { // 设置默认位置
  x.value = Math.max(12, window.innerWidth - 146); // 默认靠右
  y.value = Math.max(12, window.innerHeight - 166); // 默认靠下
} // 结束默认位置

function watchPetCommand(event: KeyboardEvent): void { // 监听快捷命令
  const target = event.target as HTMLElement | null; // 获取按键目标
  if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return; // 输入框中不处理
  typed.value = `${typed.value}${event.key}`.slice(-4); // 记录最近输入
  if (typed.value === "/pet") togglePet(); // 输入命令就切换
} // 结束命令监听

onMounted(() => { // 挂载后执行
  placeDefault(); // 设置默认位置
  loadState(); // 读取保存状态
  checkSpritesheet(); // 检查精灵表资源
  scheduleMoodShuffle(); // 启动随机状态切换
  window.addEventListener("keydown", watchPetCommand); // 添加键盘监听
}); // 结束挂载

onBeforeUnmount(() => { // 卸载前执行
  stopMoodShuffle(); // 停止随机状态切换
  window.removeEventListener("keydown", watchPetCommand); // 移除键盘监听
  window.removeEventListener("pointermove", dragPet); // 移除移动监听
  window.removeEventListener("pointerup", stopDrag); // 移除松开监听
}); // 结束卸载
</script>

<style scoped>
.pet-summon { position: fixed; right: 18px; bottom: 18px; z-index: 80; min-width: 68px; min-height: 44px; border: 1px solid rgba(138, 68, 19, 0.35); border-radius: 999px; background: linear-gradient(135deg, #fff8e7, #f2d7ad); color: #6b340c; font: 800 13px var(--font-mono); cursor: pointer; box-shadow: 0 14px 34px rgba(72, 48, 18, 0.18); } /* 召唤按钮 */
.pet-summon:hover { transform: translateY(-1px); box-shadow: 0 18px 42px rgba(72, 48, 18, 0.22); } /* 召唤悬停 */
.pet-float { position: fixed; left: var(--pet-x); top: var(--pet-y); z-index: 79; width: 112px; height: 124px; overflow: visible; touch-action: none; } /* 悬浮宠物容器 */
.pet-float.is-dragging { cursor: grabbing; } /* 拖动状态 */
.pet-avatar { position: relative; display: grid; place-items: center; width: 112px; height: 124px; border: 0; background: transparent; cursor: grab; filter: drop-shadow(0 16px 18px rgba(72, 48, 18, 0.18)); animation: pet-hover 2.7s ease-in-out infinite; } /* 宠物本体 */
.pet-avatar:active { cursor: grabbing; } /* 按住状态 */
.pet-gif { width: 112px; height: 112px; object-fit: contain; user-select: none; pointer-events: none; } /* GIF 兜底样式 */
.pet-sprite { position: absolute; left: 50%; top: 50%; width: 192px; height: 208px; background-image: var(--pet-sheet); background-repeat: no-repeat; background-size: 1536px 1872px; image-rendering: auto; transform: translate(-50%, -50%) scale(0.58); transform-origin: center; } /* 精灵表基础样式 */
.pet-sprite--idle { background-position: 0 0; animation: pet-row-idle 960ms steps(6) infinite; } /* 待命动画 */
.pet-sprite--running-right { background-position: 0 -208px; animation: pet-row-8 720ms steps(8) infinite; } /* 右跑动画 */
.pet-sprite--running-left { background-position: 0 -416px; animation: pet-row-8 720ms steps(8) infinite; } /* 左跑动画 */
.pet-sprite--waving { background-position: 0 -624px; animation: pet-row-4 760ms steps(4) infinite; } /* 挥手动画 */
.pet-sprite--jumping { background-position: 0 -832px; animation: pet-row-5 840ms steps(5) infinite; } /* 跳跃动画 */
.pet-sprite--failed { background-position: 0 -1040px; animation: pet-row-8 1040ms steps(8) infinite; } /* 失败动画 */
.pet-sprite--waiting { background-position: 0 -1248px; animation: pet-row-6 960ms steps(6) infinite; } /* 等待动画 */
.pet-sprite--running { background-position: 0 -1456px; animation: pet-row-6 720ms steps(6) infinite; } /* 运行动画 */
.pet-sprite--review { background-position: 0 -1664px; animation: pet-row-6 960ms steps(6) infinite; } /* 完成动画 */
.pet-popover { position: absolute; right: 98px; bottom: 16px; width: 268px; padding: 14px; border: 1px solid rgba(138, 68, 19, 0.22); border-radius: 22px; background: rgba(255, 251, 241, 0.96); color: var(--fg-primary); box-shadow: 0 22px 58px rgba(72, 48, 18, 0.18); opacity: 0; pointer-events: none; transform: translateX(12px) scale(0.98); transition: opacity 140ms ease, transform 140ms ease; backdrop-filter: blur(14px); } /* 悬浮选项面板 */
.pet-float:hover .pet-popover, .pet-float:focus-within .pet-popover { opacity: 1; pointer-events: auto; transform: translateX(0) scale(1); } /* 鼠标经过显示面板 */
.pet-popover::after { content: ""; position: absolute; right: -9px; bottom: 34px; width: 18px; height: 18px; border-top: 1px solid rgba(138, 68, 19, 0.22); border-right: 1px solid rgba(138, 68, 19, 0.22); background: rgba(255, 251, 241, 0.96); transform: rotate(45deg); } /* 面板箭头 */
.pet-popover-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; } /* 面板头部 */
.pet-title { font-size: 20px; font-weight: 900; color: var(--fg-primary); } /* 宠物名字 */
.pet-state { border: 1px solid rgba(138, 68, 19, 0.18); border-radius: 999px; padding: 4px 8px; color: #8a4413; background: rgba(255, 248, 231, 0.9); font-size: 11px; font-weight: 800; } /* 状态标签 */
.pet-popover p { margin: 10px 0 12px; color: var(--fg-secondary); font-size: 13px; line-height: 1.65; } /* 面板说明 */
.pet-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 7px; } /* 状态网格 */
.pet-stats span { border: 1px solid rgba(138, 68, 19, 0.18); border-radius: 999px; background: rgba(255, 255, 255, 0.68); padding: 6px 4px; text-align: center; font-size: 11px; font-weight: 800; color: #6b340c; } /* 状态块 */
.pet-actions { display: grid; grid-template-columns: repeat(3, 1fr); gap: 7px; margin-top: 10px; } /* 操作网格 */
.pet-actions button { min-height: 34px; border: 1px solid rgba(138, 68, 19, 0.26); border-radius: 999px; background: #fff8e7; color: #6b340c; font: 800 12px var(--font-mono); cursor: pointer; } /* 操作按钮 */
.pet-actions button:hover { background: #8a4413; color: #fff8e7; } /* 操作悬停 */
@keyframes pet-hover { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-7px); } } /* 漂浮动画 */
@keyframes pet-row-idle { from { background-position-x: 0; } to { background-position-x: -1152px; } } /* 待命帧动画 */
@keyframes pet-row-4 { from { background-position-x: 0; } to { background-position-x: -768px; } } /* 四帧动画 */
@keyframes pet-row-5 { from { background-position-x: 0; } to { background-position-x: -960px; } } /* 五帧动画 */
@keyframes pet-row-6 { from { background-position-x: 0; } to { background-position-x: -1152px; } } /* 六帧动画 */
@keyframes pet-row-8 { from { background-position-x: 0; } to { background-position-x: -1536px; } } /* 八帧动画 */
@media (max-width: 720px) { .pet-float { left: auto !important; right: 16px; top: auto !important; bottom: 18px; } .pet-popover { right: 0; bottom: 118px; width: min(280px, calc(100vw - 32px)); } .pet-popover::after { right: 44px; bottom: -9px; border: 0; border-right: 1px solid rgba(138, 68, 19, 0.22); border-bottom: 1px solid rgba(138, 68, 19, 0.22); } } /* 移动端适配 */
@media (prefers-reduced-motion: reduce) { .pet-avatar, .pet-sprite { animation: none; } } /* 减少动画 */
</style>
