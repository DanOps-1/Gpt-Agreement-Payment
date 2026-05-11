<template>
  <div class="run-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// 运行控制</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link">配置向导</RouterLink>
        <RouterLink to="/run" class="nav-link active">运行</RouterLink>
        <button class="header-btn" @click="logout">退出</button>
      </div>
    </header>

    <div class="run-body">
      <section class="run-controls">
        <div class="term-divider" data-tail="──────────">参数</div>
        <div class="form-stack">
          <div class="ctl-row">
            <span class="ctl-label">模式</span>
            <div class="mode-pills">
              <button
                v-for="m in modes"
                :key="m.value"
                class="mode-pill"
                :class="{ active: form.mode === m.value }"
                :disabled="status.running"
                @click="form.mode = m.value"
              >{{ m.label }}</button>
            </div>
          </div>

          <div v-if="form.mode === 'batch'" class="ctl-row sub">
            <TermField v-model.number="form.batch" label="batch N" type="number" />
            <TermField v-model.number="form.workers" label="workers" type="number" />
          </div>
          <div v-if="form.mode === 'self_dealer'" class="ctl-row sub">
            <TermField v-model.number="form.self_dealer" label="member N" type="number" />
          </div>
          <div v-if="form.mode === 'free_register'" class="ctl-row sub">
            <TermField v-model.number="form.count" label="注册数 (0=无限)" type="number" />
          </div>

          <div v-if="!isFreeMode" class="ctl-row toggles">
            <TermToggle v-model="form.paypal" :disabled="form.gopay">PayPal 支付</TermToggle>
            <TermToggle v-model="form.gopay" @update:modelValue="onGoPayToggle">GoPay (印尼)</TermToggle>
          </div>
          <div v-if="!isFreeMode" class="ctl-row toggles">
            <TermToggle v-model="form.pay_only">--pay-only</TermToggle>
            <TermToggle v-model="form.register_only" :disabled="form.pay_only">--register-only</TermToggle>
          </div>
          <p v-if="!isFreeMode" class="ctl-hint">
            <code>--pay-only</code> 跳过注册，优先复用最近注册但未支付账号；
            <code>--register-only</code> 只注册不支付。
          </p>

          <div v-if="!form.pay_only" class="ctl-row reg-mode">
            <span class="reg-mode-label">注册路径 ·</span>
            <label class="reg-mode-opt" :class="{ active: form.register_mode === 'browser' }">
              <input type="radio" value="browser" v-model="form.register_mode" />
              浏览器 (Camoufox)
            </label>
            <label class="reg-mode-opt" :class="{ active: form.register_mode === 'protocol' }">
              <input type="radio" value="protocol" v-model="form.register_mode" />
              纯协议 (auth_flow)
            </label>
          </div>
          <p v-if="!form.pay_only" class="ctl-hint">
            <code>browser</code> 走 Camoufox + Turnstile 真实执行（稳但慢，OpenAI 改 modal 后可能失败）；
            <code>protocol</code> 走 <code>auth_flow.AuthFlow</code> HTTP 直连（快，但可能被风控）。
            选择会自动持久化到 localStorage。
          </p>
          <p v-else class="ctl-hint">
            <code>{{ form.mode }}</code> 不走支付步骤；OTP 经 CF KV 取，OAuth 拿 rt 后推 CPA 用 <code>cpa.free_plan_tag</code>。
          </p>
        </div>

        <div class="term-divider" data-tail="──────────">命令</div>
        <pre class="cmd-preview">{{ cmdPreview }}</pre>

        <div v-if="configHealth" class="health-panel" :class="{ ok: configHealth.ok, fail: !configHealth.ok }">
          <div class="health-head">
            <span class="health-title">配置健康检查</span>
            <span class="badge" :class="configHealth.ok ? 'badge-ok' : 'badge-err'">
              {{ configHealth.ok ? "可启动" : "阻断启动" }}
            </span>
            <span class="health-meta">{{ configHealth.payment_kind }} / {{ configHealth.requires_email_otp ? "需要邮箱 OTP" : "不需要邮箱 OTP" }}</span>
          </div>
          <div class="health-list">
            <div v-for="chk in visibleHealthChecks" :key="chk.name" class="health-row" :class="`health-${chk.status}`">
              <span class="health-status">{{ healthStatusLabel(chk.status) }}</span>
              <div class="health-body">
                <strong>{{ chk.message }}</strong>
                <div v-if="chk.missing?.length" class="health-sub">缺：{{ chk.missing.join(", ") }}</div>
                <div v-if="chk.action" class="health-sub">处理：{{ chk.action }}</div>
                <div v-if="chk.details" class="health-sub">详情：{{ chk.details }}</div>
              </div>
            </div>
          </div>
        </div>

        <div class="step-actions">
          <TermBtn variant="ghost" :loading="configHealthLoading" @click="checkConfigHealth">检查配置</TermBtn>
          <TermBtn v-if="!status.running" :loading="starting" @click="start">▶ 开始运行</TermBtn>
          <TermBtn v-else variant="danger" :loading="stopping" @click="stop">■ 停止</TermBtn>
        </div>

        <div class="status-line" :class="{ running: status.running }">
          <span v-if="status.running">
            <span class="status-dot">●</span>
            运行中 PID {{ status.pid }} // 模式 {{ status.mode }} // {{ runtimeText }}
          </span>
          <span v-else-if="status.ended_at">
            <span class="status-dot ok" v-if="status.exit_code === 0">●</span>
            <span class="status-dot err" v-else>●</span>
            上次运行已退出 // 退出码 {{ status.exit_code }} //
            {{ runtimeText }}
          </span>
          <span v-else>
            <span class="status-dot idle">○</span> 空闲
          </span>
        </div>

        <details class="link-details" :open="autoLoop.running">
          <summary>
            Auto Loop ·
            <span class="link-summary" :class="autoLoop.running ? 'warn' : 'muted'">
              {{ autoLoopSummary }}
            </span>
          </summary>
          <p class="link-hint">
            循环跑「注册 + GoPay 支付」一直到累计成功次数 = target，或连续失败 ≥ max。
            自动处理：<code>cf_429</code> 触发 IP 轮换；<code>coupon_ineligible</code> 标当前账号为废号删除；
            其它已知错误（OTP 超时/钱包余额不足/406 已绑定）记日志后跳过。
          </p>
          <div v-if="!autoLoop.running" class="auto-loop-form">
            <label>
              目标成功数
              <input v-model.number="autoLoopForm.target_success" type="number" min="1" max="10000" class="link-manual-input" style="min-width:80px;flex:0 0 auto" />
            </label>
            <label>
              连续失败上限
              <input v-model.number="autoLoopForm.max_consec_fail" type="number" min="1" max="100" class="link-manual-input" style="min-width:80px;flex:0 0 auto" />
            </label>
            <TermBtn :loading="autoLoopBusy" @click="startAutoLoop">▶ 启动 Auto Loop</TermBtn>
          </div>
          <div v-else class="auto-loop-running">
            <div class="auto-loop-stats">
              <span><b>iter</b> {{ autoLoop.iteration }}</span>
              <span class="ok"><b>success</b> {{ autoLoop.success_count }}/{{ autoLoop.target_success }}</span>
              <span class="warn"><b>fail</b> {{ autoLoop.fail_count }} (连续 {{ autoLoop.consecutive_fail }}/{{ autoLoop.max_consec_fail }})</span>
              <span><b>IP 轮换</b> {{ autoLoop.ip_rotations }}</span>
              <span v-if="autoLoop.scrap_marked.length"><b>已废号</b> {{ autoLoop.scrap_marked.length }}</span>
              <span v-if="autoLoop.zone_list.length > 1">
                <b>zone</b> {{ autoLoop.current_zone || '?' }}
                ({{ autoLoop.zone_list.length }} 个，已切 {{ autoLoop.total_zone_rotations }} 次)
              </span>
              <span v-if="autoLoop.zone_reg_fail_streak > 0" class="warn">
                reg连挂 {{ autoLoop.zone_reg_fail_streak }}
              </span>
            </div>
            <div class="auto-loop-last" v-if="autoLoop.last_kind">
              <b>{{ autoLoop.last_kind }}</b> · {{ autoLoop.last_action }}
              <span v-if="autoLoop.last_email"> · {{ autoLoop.last_email }}</span>
            </div>
            <TermBtn variant="danger" :loading="autoLoopBusy" @click="stopAutoLoop">■ 停止 Auto Loop</TermBtn>
          </div>
          <p v-if="autoLoop.stop_reason && !autoLoop.running" class="link-hint">
            上次结束原因：<code>{{ autoLoop.stop_reason }}</code>
          </p>
        </details>

        <details class="link-details">
          <summary>
            出口 IP ·
            <span class="link-summary" :class="proxyIp ? 'ok' : 'muted'">
              {{ proxyIp || '未加载' }}<span v-if="proxyCountry"> ({{ proxyCountry }})</span>
            </span>
            <TermBtn
              variant="ghost"
              class="link-refresh-btn"
              :loading="rotatingIp"
              @click.prevent="rotateProxyIp"
            >切换 IP</TermBtn>
          </summary>
          <p class="link-hint" v-if="proxyError">{{ proxyError }}</p>
          <p v-else class="link-hint">
            遇到 Midtrans <code>429 body=</code>（Cloudflare 边缘节流，按 IP 限流）时点「切换 IP」。
            会调 Webshare API 整池替换 + 切 gost 上游。<em>会消耗 Webshare 月度替换额度。</em>
          </p>
        </details>

        <details class="link-details">
          <summary>
            GoPay 链接 ·
            <span class="link-summary" :class="linkSummaryClass">{{ linkSummaryText }}</span>
            <TermBtn variant="ghost" class="link-refresh-btn" @click.prevent="refreshLinkStates">刷新</TermBtn>
          </summary>
          <p class="link-hint">
            支付成功自动 mark linked，下次启动 GoPay 流程会预检拒。
            <em>本地 unlink ≠ Midtrans 服务端 unlink</em>——出 429 是 Midtrans 自己 rate-limit/冷却，本面板解决不了。
          </p>
          <div v-if="linkStateError" class="link-error">{{ linkStateError }}</div>
          <table v-if="linkStates.length" class="link-table">
            <thead>
              <tr><th>手机号</th><th>状态</th><th>linked_at</th><th>改动方</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="row in linkStates" :key="row.phone">
                <td><code>{{ row.phone }}</code></td>
                <td>
                  <span :class="row.linked ? 'badge-linked' : 'badge-unlinked'">
                    {{ row.linked ? '● linked' : '○ unlinked' }}
                  </span>
                </td>
                <td class="ts">{{ formatLinkTs(row.linked_at) }}</td>
                <td class="muted">{{ row.last_changed_by || '—' }}</td>
                <td>
                  <TermBtn
                    v-if="row.linked"
                    variant="danger"
                    :loading="linkBusy === row.phone"
                    @click="setLinkState(row.phone, false)"
                  >Unlink</TermBtn>
                  <TermBtn
                    v-else
                    variant="ghost"
                    :loading="linkBusy === row.phone"
                    @click="setLinkState(row.phone, true)"
                  >Mark Linked</TermBtn>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="link-empty-inline">暂无记录</div>
          <div class="link-manual">
            <input
              v-model="linkManualPhone"
              class="link-manual-input"
              placeholder="手动添加手机号（含国家码，纯数字）"
            />
            <TermBtn
              variant="ghost"
              :disabled="!linkManualPhoneValid"
              @click="setLinkState(linkManualPhone, true)"
            >Mark Linked</TermBtn>
            <TermBtn
              variant="ghost"
              :disabled="!linkManualPhoneValid"
              @click="setLinkState(linkManualPhone, false)"
            >Unlink</TermBtn>
          </div>
        </details>
      </section>

      <section class="run-inventory">
        <div class="term-divider inventory-divider" data-tail="──────────">账号库存</div>
        <div class="inventory-head">
          <div class="inventory-meta">
            <span class="inventory-label">最近刷新</span>
            <span class="inventory-value">{{ inventoryUpdatedText }}</span>
          </div>
          <TermBtn variant="ghost" :loading="inventoryLoading" @click="refreshInventory">刷新库存</TermBtn>
        </div>
        <div v-if="inventoryError" class="inventory-error">
          库存刷新失败：{{ inventoryError }}。如果刚更新过代码，重启后端 <code>python -m webui.server</code>。
        </div>

        <div class="inventory-stats">
          <div class="inventory-stat">
            <span class="inventory-stat-label">总账号</span>
            <strong>{{ inventory.counts.registered_total }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">可复用</span>
            <strong>{{ inventory.counts.pay_only_eligible }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">已消耗</span>
            <strong>{{ inventory.counts.pay_only_consumed }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">缺 auth</span>
            <strong>{{ inventory.counts.pay_only_no_auth }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">有 RT</span>
            <strong>{{ inventory.counts.with_refresh_token }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">RT 待补</span>
            <strong>{{ inventory.counts.rt_missing }}</strong>
          </div>
          <div class="inventory-stat">
            <span class="inventory-stat-label">RT 冷却</span>
            <strong>{{ inventory.counts.rt_cooldown }}</strong>
          </div>
        </div>

        <div v-if="inventory.accounts.length" class="inventory-filters">
          <input
            v-model="invFilters.search"
            class="inv-filter-input"
            placeholder="🔍 邮箱关键字"
          />
          <select v-model="invFilters.plan" class="inv-filter-sel">
            <option value="">所有 plan</option>
            <option value="plus">plus</option>
            <option value="team">team</option>
            <option value="free">free</option>
          </select>
          <select v-model="invFilters.check" class="inv-filter-sel">
            <option value="">所有验证</option>
            <option value="valid">✓ 有效</option>
            <option value="invalid">✗ 失效</option>
            <option value="unknown">? 未知</option>
            <option value="unchecked">未检</option>
          </select>
          <select v-model="invFilters.pay" class="inv-filter-sel">
            <option value="">所有支付</option>
            <option value="reusable">可复用</option>
            <option value="consumed">已消耗</option>
            <option value="no_auth">缺 auth</option>
          </select>
          <select v-model="invFilters.rt" class="inv-filter-sel">
            <option value="">所有 RT</option>
            <option value="has_rt">有 RT</option>
            <option value="oauth_succeeded">OAuth 成功</option>
            <option value="missing">缺 RT</option>
            <option value="cooldown">冷却中</option>
            <option value="retryable">可重试</option>
            <option value="dead">永久失败</option>
          </select>
          <select v-model="invFilters.cpa" class="inv-filter-sel">
            <option value="">所有 CPA</option>
            <option value="pushed">已推送</option>
            <option value="not_pushed">未推送</option>
          </select>
          <span class="inv-filter-count">{{ filteredAccounts.length }} / {{ inventory.accounts.length }}</span>
          <TermBtn variant="ghost" :disabled="!hasActiveFilter" @click="resetInvFilters">清除筛选</TermBtn>
        </div>

        <div v-if="inventory.accounts.length" class="inventory-toolbar">
          <label class="inventory-toolbar-check">
            <input type="checkbox" :checked="allFilteredSelected" @change="toggleSelectAllFiltered" />
            <span>全选筛选结果 ({{ selectedFilteredCount }} / {{ filteredAccounts.length }})</span>
          </label>
          <div class="inventory-toolbar-actions">
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0" @click="verifySelected">验证选中</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="unknownOrUncheckedIds.length === 0" @click="verifyAllUnknown">验证全部未检 ({{ unknownOrUncheckedIds.length }})</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0" @click="pushSelectedToCpa">推送选中→CPA</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="unpushedIds.length === 0" @click="pushAllUnpushed">推送全部未推送 ({{ unpushedIds.length }})</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0 || status.running" @click="payOnlySelected">选中跑 pay-only</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0 || status.running" @click="rtOnlySelected">选中补 RT</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="selectedIds.size === 0" @click="deleteSelected">删除选中</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" :disabled="invalidIds.length === 0" @click="deleteAllInvalid">删除所有失效 ({{ invalidIds.length }})</TermBtn>
          </div>
        </div>

        <div v-if="filteredAccounts.length" class="inventory-list">
          <div v-for="acc in filteredAccounts" :key="acc.id || acc.email" class="inventory-row" :class="{ 'inventory-row--selected': isSelected(acc.id) }">
            <div class="inventory-row-top">
              <input type="checkbox" class="inventory-row-check" :checked="isSelected(acc.id)" @change="toggleSelect(acc.id)" />
              <span class="inventory-email">{{ acc.email }}</span>
              <span class="badge" :class="planBadgeClass(acc.plan_tag)">{{ planLabel(acc.plan_tag) }}</span>
              <span class="badge" :class="checkBadgeClass(acc.last_check_status)" :title="acc.last_check_message">
                <template v-if="checkingIds.has(acc.id)">⟳ 检查中</template>
                <template v-else>{{ checkLabel(acc.last_check_status) }}</template>
              </span>
              <span class="badge" :class="payBadgeClass(acc.pay_state)">{{ payStateLabel(acc) }}</span>
              <span class="badge" :class="rtBadgeClass(acc.rt_state)">{{ rtStateLabel(acc) }}</span>
              <span class="badge" :class="cpaBadgeClass(acc)" :title="acc.cpa_status">{{ cpaLabel(acc) }}</span>
              <button v-if="!acc.cpa_pushed" class="inventory-row-action" :disabled="inventoryBusy" @click="pushOneToCpa(acc.id)">推送→CPA</button>
            </div>
            <div class="inventory-row-sub">
              <span>注册 {{ formatInventoryTs(acc.registered_at) }}</span>
              <span>attempts {{ acc.attempts }}</span>
              <span>auth {{ authSummary(acc) }}</span>
            </div>
            <div class="inventory-row-detail">
              <span>payment {{ acc.latest_payment_status || "—" }}</span>
              <span v-if="acc.latest_payment_source">source {{ acc.latest_payment_source }}</span>
              <span v-if="acc.latest_payment_error">error {{ acc.latest_payment_error }}</span>
              <span v-if="acc.oauth_status">oauth {{ acc.oauth_status }}<template v-if="acc.oauth_fail_reason"> ({{ acc.oauth_fail_reason }})</template></span>
              <span v-if="acc.oauth_cooldown_remaining_s">cooldown {{ formatCooldown(acc.oauth_cooldown_remaining_s) }}</span>
              <span v-if="acc.latest_payment_is_already_paid" class="inventory-inline-flag">already paid</span>
              <span v-if="acc.can_backfill_rt" class="inventory-inline-flag">can backfill rt</span>
            </div>
          </div>
        </div>
        <div v-else-if="!inventory.accounts.length" class="inventory-empty">
          暂无账号库存；先跑一次注册/支付，等数据库同步完成后再点刷新。
        </div>
        <div v-else class="inventory-empty">
          所有账号都被筛掉了——清除筛选或调整条件。
        </div>
      </section>

      <Teleport to="body">
        <div v-if="otpDialog.open" class="otp-overlay" @click.self="() => {}">
          <div class="otp-modal">
            <button
              class="otp-close"
              :disabled="otpDialog.submitting"
              title="关闭（gopay.py 已自行从 API 取到 OTP / 想手动跳过）"
              @click="dismissOtpModal"
            >×</button>
            <div class="otp-head">
              <span class="otp-prompt">$</span> GoPay WhatsApp OTP
            </div>
            <p class="otp-desc">查 WhatsApp，把刚收到的 6 位 OTP 输进来。提交后 gopay.py 自动继续。</p>
            <input
              class="otp-input"
              v-model="otpDialog.value"
              maxlength="8"
              autofocus
              :disabled="otpDialog.submitting"
              @keyup.enter="submitOtp"
              placeholder="000000"
            />
            <div class="otp-actions">
              <TermBtn :loading="otpDialog.submitting" @click="submitOtp">提交</TermBtn>
            </div>
          </div>
        </div>
      </Teleport>

      <section class="run-logs">
        <div class="logs-head">
          <span class="pre-prompt">$</span> 实时日志
          <span class="logs-meta">{{ lines.length }} 行</span>
          <label class="auto-scroll-toggle">
            <input type="checkbox" v-model="autoScroll" />
            <span>自动滚到底</span>
          </label>
        </div>
        <div class="logs-stream" ref="streamEl">
          <div v-if="!lines.length" class="logs-empty">
            等待运行<span class="term-cursor"></span>
          </div>
          <div v-for="entry in lines" :key="entry.seq" class="log-line" :class="entry.cls">
            <span class="log-ts">{{ entry.tsLabel }}</span>
            <span class="log-msg">{{ entry.line }}</span>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from "vue";
import { useRouter, RouterLink } from "vue-router";
import { useMessage, useDialog } from "naive-ui";
import { h } from "vue";
import { api } from "../api/client";
import TermField from "../components/term/TermField.vue";
import TermBtn from "../components/term/TermBtn.vue";
import TermToggle from "../components/term/TermToggle.vue";

const router = useRouter();
const message = useMessage();
const dialog = useDialog();

const modes = [
  { value: "single", label: "single — 1×" },
  { value: "batch", label: "batch — N×" },
  { value: "self_dealer", label: "self-dealer" },
  { value: "daemon", label: "daemon ∞" },
  { value: "free_register", label: "free_register — 免费号+rt+CPA" },
  { value: "free_backfill_rt", label: "free_backfill_rt — 老号补rt" },
];

import { useWizardStore } from "../stores/wizard";
const store = useWizardStore();

interface RunStatus {
  running: boolean;
  pid: number | null;
  mode: string | null;
  cmd: string[] | null;
  started_at: number | null;
  ended_at: number | null;
  exit_code: number | null;
  log_count: number;
  otp_pending?: boolean;
}

interface InventoryAccount {
  id: number;
  email: string;
  registered_at: string;
  attempts: number;
  has_session_token: boolean;
  has_access_token: boolean;
  has_device_id: boolean;
  has_refresh_token: boolean;
  pay_state: "reusable" | "consumed" | "no_auth";
  pay_only_eligible: boolean;
  rt_state: "has_rt" | "oauth_succeeded" | "dead" | "cooldown" | "retryable" | "missing";
  can_backfill_rt: boolean;
  oauth_status: string;
  oauth_fail_reason: string;
  oauth_updated_at: string;
  oauth_cooldown_remaining_s: number;
  latest_payment_status: string;
  latest_payment_source: string;
  latest_payment_error: string;
  latest_payment_is_already_paid: boolean;
  last_check_status: "" | "valid" | "invalid" | "unknown";
  last_check_message: string;
  last_check_at: number;
  plan_tag: "free" | "plus" | "team" | string;
  cpa_status: string;
  cpa_pushed: boolean;
}

interface InventoryResponse {
  generated_at: string;
  files: Record<string, string>;
  counts: {
    registered_total: number;
    raw_registered_rows: number;
    with_auth: number;
    pay_only_eligible: number;
    pay_only_consumed: number;
    pay_only_no_auth: number;
    with_refresh_token: number;
    rt_missing: number;
    rt_processed: number;
    rt_retryable: number;
    rt_cooldown: number;
    rt_dead: number;
  };
  accounts: InventoryAccount[];
}

interface ConfigHealthCheck {
  name: string;
  status: "ok" | "warn" | "fail";
  message: string;
  missing: string[];
  blocking: boolean;
  details: string;
  action: string;
}

interface ConfigHealthResponse {
  ok: boolean;
  mode: string;
  payment_kind: string;
  requires_registration: boolean;
  requires_email_otp: boolean;
  paths: Record<string, string>;
  checks: ConfigHealthCheck[];
  blocking: ConfigHealthCheck[];
}

const form = ref({
  mode: (router.currentRoute.value.query.mode as string) || "single",
  paypal: true,
  gopay: false,
  pay_only: false,
  register_only: false,
  batch: 5,
  workers: 3,
  self_dealer: 4,
  count: 0, // free_register 模式：注册多少个后停（0 = 无限）
  register_mode: (localStorage.getItem("webui.register_mode") || "browser") as "browser" | "protocol",
});

watch(() => form.value.register_mode, (v) => {
  try { localStorage.setItem("webui.register_mode", v); } catch {}
});

const otpDialog = ref({
  open: false,
  value: "",
  submitting: false,
  openedAt: 0,
  autoFilled: false,
  // 用户手动 × 关闭后这次 run 内不再 reopen，
  // 直到 SSE `done` 事件触发（pipeline 结束 → 清旗）。
  dismissed: false,
});
let otpPollTimer: ReturnType<typeof setInterval> | undefined;

interface LinkStateRow {
  phone: string;
  linked: boolean;
  linked_at: number | null;
  unlinked_at: number | null;
  payment_ref: string;
  last_changed_by: string;
}
const linkStates = ref<LinkStateRow[]>([]);
const linkStateError = ref("");
const linkStateUpdatedAt = ref<number | null>(null);
const linkBusy = ref("");
const linkManualPhone = ref("");
let linkPollTimer: ReturnType<typeof setInterval> | undefined;

const linkStateUpdatedText = computed(() => {
  if (!linkStateUpdatedAt.value) return "未刷新";
  const d = new Date(linkStateUpdatedAt.value);
  return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
});

const linkSummaryText = computed(() => {
  if (!linkStates.value.length) return "无记录";
  const linked = linkStates.value.filter(r => r.linked).length;
  const unlinked = linkStates.value.length - linked;
  if (linked === 0) return `${unlinked} 个 unlinked`;
  return `${linked} 个 linked / ${unlinked} 个 unlinked`;
});

const linkSummaryClass = computed(() => {
  if (!linkStates.value.length) return "muted";
  return linkStates.value.some(r => r.linked) ? "warn" : "ok";
});

const proxyIp = ref("");
const proxyCountry = ref("");
const proxyError = ref("");
const rotatingIp = ref(false);

async function loadCurrentProxy() {
  try {
    const r = await api.get<{ ip: string; country: string }>("/proxy/current");
    proxyIp.value = r.data.ip || "";
    proxyCountry.value = r.data.country || "";
    proxyError.value = "";
  } catch (e: any) {
    proxyError.value = e?.response?.data?.detail || "未读到当前出口 IP（webshare 未启用？）";
  }
}

interface AutoLoopState {
  running: boolean;
  iteration: number;
  success_count: number;
  fail_count: number;
  consecutive_fail: number;
  target_success: number;
  max_consec_fail: number;
  last_kind: string;
  last_action: string;
  last_email: string;
  stop_reason: string;
  ip_rotations: number;
  scrap_marked: { email: string; kind: string; ts: number }[];
  zone_list: string[];
  current_zone: string;
  zone_reg_fail_streak: number;
  zone_ip_rotations: number;
  total_zone_rotations: number;
}
const autoLoop = ref<AutoLoopState>({
  running: false, iteration: 0, success_count: 0, fail_count: 0,
  consecutive_fail: 0, target_success: 0, max_consec_fail: 0,
  last_kind: "", last_action: "", last_email: "", stop_reason: "",
  ip_rotations: 0, scrap_marked: [],
  zone_list: [], current_zone: "",
  zone_reg_fail_streak: 0, zone_ip_rotations: 0, total_zone_rotations: 0,
});
const autoLoopForm = ref({ target_success: 5, max_consec_fail: 5 });
const autoLoopBusy = ref(false);
let autoLoopPollTimer: ReturnType<typeof setInterval> | undefined;

const autoLoopSummary = computed(() => {
  if (!autoLoop.value.running) {
    if (autoLoop.value.stop_reason) {
      return `已停止 (${autoLoop.value.stop_reason})`;
    }
    return "未启动";
  }
  return `running · ${autoLoop.value.success_count}/${autoLoop.value.target_success} 成功 · ${autoLoop.value.fail_count} 失败`;
});

async function refreshAutoLoop() {
  try {
    const r = await api.get<AutoLoopState>("/auto-loop/status");
    autoLoop.value = r.data;
  } catch {}
}

async function startAutoLoop() {
  autoLoopBusy.value = true;
  try {
    await api.post("/auto-loop/start", {
      target_success: autoLoopForm.value.target_success,
      max_consec_fail: autoLoopForm.value.max_consec_fail,
      paypal: false,
      gopay: true,
      pay_only: false,
      register_only: false,
      register_mode: form.value.register_mode || "browser",
    });
    message.success("Auto Loop 已启动");
    await refreshAutoLoop();
    // 没活的 SSE 就开一个，让用户能看到子进程的日志和 [auto-loop] marker
    if (!eventSource) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "启动失败");
  } finally {
    autoLoopBusy.value = false;
  }
}

async function stopAutoLoop() {
  autoLoopBusy.value = true;
  try {
    await api.post("/auto-loop/stop");
    message.success("已发送停止信号");
    await refreshAutoLoop();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "停止失败");
  } finally {
    autoLoopBusy.value = false;
  }
}

async function rotateProxyIp() {
  if (!confirm("确认切换出口 IP？会消耗 Webshare 月度替换额度。")) return;
  rotatingIp.value = true;
  proxyError.value = "";
  try {
    const r = await api.post<{ prev_ip: string; new_ip: string; country: string }>("/proxy/rotate-ip");
    proxyIp.value = r.data.new_ip || "";
    proxyCountry.value = r.data.country || "";
    message.success(`IP 已切换：${r.data.prev_ip || "?"} → ${r.data.new_ip}`);
  } catch (e: any) {
    proxyError.value = e?.response?.data?.detail || "切换失败";
    message.error(proxyError.value);
  } finally {
    rotatingIp.value = false;
  }
}

const linkManualPhoneValid = computed(() => /^\d{6,15}$/.test(linkManualPhone.value.replace(/\D/g, "")));

function formatLinkTs(ts: number | null | undefined): string {
  if (!ts) return "—";
  try { return new Date(Number(ts) * 1000).toLocaleString(); } catch { return String(ts); }
}

async function refreshLinkStates() {
  linkStateError.value = "";
  try {
    const r = await api.get<{ items: LinkStateRow[] }>("/gopay/link-state");
    linkStates.value = r.data.items || [];
    linkStateUpdatedAt.value = Date.now();
  } catch (e: any) {
    linkStateError.value = e?.response?.data?.detail || e?.message || "拉取失败";
  }
}

async function setLinkState(phone: string, linked: boolean) {
  const digits = (phone || "").replace(/\D/g, "");
  if (!digits) {
    message.warning("手机号为空");
    return;
  }
  linkBusy.value = digits;
  try {
    await api.post("/gopay/link-state/set", {
      phone: digits,
      linked,
      source: "webui_manual",
    });
    message.success(linked ? `已标记 ${digits} 为 linked` : `已 unlink ${digits}`);
    if (linkManualPhone.value.replace(/\D/g, "") === digits) {
      linkManualPhone.value = "";
    }
    await refreshLinkStates();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "更新失败");
  } finally {
    linkBusy.value = "";
  }
}

function onGoPayToggle(v: boolean) {
  if (v) form.value.paypal = false;
}

const status = ref<RunStatus>({
  running: false, pid: null, mode: null, cmd: null,
  started_at: null, ended_at: null, exit_code: null, log_count: 0,
});

const cmdPreview = ref("xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal");
interface LogEntry {
  seq: number;
  ts: number;
  line: string;
  cls?: string;
  tsLabel?: string;
}
const lines = ref<LogEntry[]>([]);
const starting = ref(false);
const stopping = ref(false);
const configHealth = ref<ConfigHealthResponse | null>(null);
const configHealthLoading = ref(false);
const inventory = ref<InventoryResponse>({
  generated_at: "",
  files: {},
  counts: {
    registered_total: 0,
    raw_registered_rows: 0,
    with_auth: 0,
    pay_only_eligible: 0,
    pay_only_consumed: 0,
    pay_only_no_auth: 0,
    with_refresh_token: 0,
    rt_missing: 0,
    rt_processed: 0,
    rt_retryable: 0,
    rt_cooldown: 0,
    rt_dead: 0,
  },
  accounts: [],
});
const selectedIds = ref<Set<number>>(new Set());
const checkingIds = ref<Set<number>>(new Set());
const inventoryBusy = ref(false);
const autoScroll = ref(true);
const inventoryLoading = ref(false);
const inventoryError = ref("");
const streamEl = ref<HTMLElement | null>(null);
const clock = ref("");
let clockTimer: ReturnType<typeof setInterval> | undefined;
let statusTimer: ReturnType<typeof setInterval> | undefined;
let inventoryTimer: ReturnType<typeof setInterval> | undefined;
let eventSource: EventSource | null = null;

function tick() {
  const d = new Date();
  clock.value = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

const runtimeText = computed(() => {
  if (status.value.running && status.value.started_at) {
    const elapsed = Math.floor((Date.now() / 1000) - status.value.started_at);
    return formatElapsed(elapsed);
  }
  if (status.value.started_at && status.value.ended_at) {
    const elapsed = Math.floor(status.value.ended_at - status.value.started_at);
    return `耗时 ${formatElapsed(elapsed)}`;
  }
  return "";
});

const inventoryUpdatedText = computed(() =>
  inventory.value.generated_at ? formatInventoryTs(inventory.value.generated_at) : "未刷新"
);

const visibleHealthChecks = computed(() => {
  const checks = configHealth.value?.checks || [];
  const important = checks.filter((c) => c.status !== "ok" || c.blocking);
  return important.length ? important : checks;
});

function formatElapsed(s: number) {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const ss = s % 60;
  if (m < 60) return `${m}m${String(ss).padStart(2,'0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2,'0')}m`;
}

function formatTs(ts: number) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function formatInventoryTs(ts: string) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

function formatCooldown(seconds: number) {
  return formatElapsed(Math.max(0, Math.floor(seconds)));
}

function authSummary(acc: InventoryAccount) {
  const parts: string[] = [];
  if (acc.has_session_token) parts.push("session");
  if (acc.has_access_token) parts.push("access");
  if (acc.has_device_id) parts.push("device");
  if (acc.has_refresh_token) parts.push("rt");
  return parts.length ? parts.join(" / ") : "none";
}

function payStateLabel(acc: InventoryAccount) {
  if (acc.pay_state === "reusable") return "可复用";
  if (acc.pay_state === "consumed") return "已消耗";
  return "缺 auth";
}

function rtStateLabel(acc: InventoryAccount) {
  switch (acc.rt_state) {
    case "has_rt":
      return "有 RT";
    case "oauth_succeeded":
      return "RT 已处理";
    case "dead":
      return "dead";
    case "cooldown":
      return "RT 冷却";
    case "retryable":
      return "可重试";
    default:
      return "RT 待补";
  }
}

function payBadgeClass(state: InventoryAccount["pay_state"]) {
  if (state === "reusable") return "badge-ok";
  if (state === "consumed") return "badge-err";
  return "badge-warn";
}

function rtBadgeClass(state: InventoryAccount["rt_state"]) {
  if (state === "has_rt" || state === "oauth_succeeded") return "badge-ok";
  if (state === "dead") return "badge-err";
  if (state === "missing") return "badge-ghost";
  return "badge-warn";
}

function healthStatusLabel(status: ConfigHealthCheck["status"]) {
  if (status === "ok") return "OK";
  if (status === "warn") return "WARN";
  return "FAIL";
}

function healthErrorText(payload: any) {
  const detail = payload?.response?.data?.detail;
  if (!detail) return payload?.message || "配置健康检查失败";
  if (typeof detail === "string") return detail;
  return detail.message || "配置健康检查未通过";
}

function logClass(line: string) {
  if (/\b(ERROR|FAIL|TRACE|Traceback)\b/i.test(line)) return "log-err";
  if (/\b(WARN|WARNING)\b/i.test(line)) return "log-warn";
  if (/\b(OK|SUCCESS|✓|完成|成功)\b/i.test(line)) return "log-ok";
  return "";
}

// 滚到底部用 RAF 去重：高频日志推送时单帧只滚一次，不再每行 nextTick
let _scrollPending = false;
function scheduleScrollToBottom() {
  if (!autoScroll.value || _scrollPending) return;
  _scrollPending = true;
  requestAnimationFrame(() => {
    _scrollPending = false;
    if (autoScroll.value && streamEl.value) {
      streamEl.value.scrollTop = streamEl.value.scrollHeight;
    }
  });
}

// ── 库存：选择 + 验证 + 删除 ─────────────────────────────────
function checkLabel(s: InventoryAccount["last_check_status"]) {
  if (s === "valid") return "✓ 有效";
  if (s === "invalid") return "✗ 失效";
  if (s === "unknown") return "？未知";
  return "○ 未验";
}
function checkBadgeClass(s: InventoryAccount["last_check_status"]) {
  if (s === "valid") return "badge-ok";
  if (s === "invalid") return "badge-err";
  if (s === "unknown") return "badge-warn";
  return "badge-ghost";
}
function isSelected(id: number) { return selectedIds.value.has(id); }
function toggleSelect(id: number) {
  const next = new Set(selectedIds.value);
  if (next.has(id)) next.delete(id); else next.add(id);
  selectedIds.value = next;
}
const allSelected = computed(() => {
  const ids = inventory.value.accounts.map(a => a.id).filter(Boolean);
  return ids.length > 0 && ids.every(id => selectedIds.value.has(id));
});
function toggleSelectAll() {
  if (allSelected.value) {
    selectedIds.value = new Set();
  } else {
    selectedIds.value = new Set(inventory.value.accounts.map(a => a.id).filter(Boolean));
  }
}

// ── 账号库存筛选 ───────────────────────────────────────────────
const invFilters = ref({
  search: "",
  plan: "",
  check: "",
  pay: "",
  rt: "",
  cpa: "",
});

const hasActiveFilter = computed(() =>
  Object.values(invFilters.value).some(v => v !== "")
);

const filteredAccounts = computed<InventoryAccount[]>(() => {
  const f = invFilters.value;
  const s = (f.search || "").trim().toLowerCase();
  return inventory.value.accounts.filter(acc => {
    if (s && !(acc.email || "").toLowerCase().includes(s)) return false;
    if (f.plan && acc.plan_tag !== f.plan) return false;
    if (f.check) {
      if (f.check === "unchecked") {
        if (acc.last_check_status !== "") return false;
      } else if (acc.last_check_status !== f.check) return false;
    }
    if (f.pay && acc.pay_state !== f.pay) return false;
    if (f.rt && acc.rt_state !== f.rt) return false;
    if (f.cpa) {
      if (f.cpa === "pushed" && !acc.cpa_pushed) return false;
      if (f.cpa === "not_pushed" && acc.cpa_pushed) return false;
    }
    return true;
  });
});

function resetInvFilters() {
  invFilters.value = { search: "", plan: "", check: "", pay: "", rt: "", cpa: "" };
}

const allFilteredSelected = computed(() => {
  const ids = filteredAccounts.value.map(a => a.id).filter(Boolean);
  return ids.length > 0 && ids.every(id => selectedIds.value.has(id));
});

const selectedFilteredCount = computed(() => {
  let n = 0;
  for (const acc of filteredAccounts.value) {
    if (selectedIds.value.has(acc.id)) n++;
  }
  return n;
});

function toggleSelectAllFiltered() {
  const ids = filteredAccounts.value.map(a => a.id).filter(Boolean);
  if (allFilteredSelected.value) {
    // 反选：把当前筛选结果中的 id 从 selection 移除
    const next = new Set(selectedIds.value);
    for (const id of ids) next.delete(id);
    selectedIds.value = next;
  } else {
    const next = new Set(selectedIds.value);
    for (const id of ids) next.add(id);
    selectedIds.value = next;
  }
}
const invalidIds = computed(() =>
  inventory.value.accounts.filter(a => a.last_check_status === "invalid").map(a => a.id)
);
const unknownOrUncheckedIds = computed(() =>
  inventory.value.accounts
    .filter(a => a.last_check_status === "" || a.last_check_status === "unknown")
    .map(a => a.id)
);

async function runCheck(ids: number[], label: string) {
  if (!ids.length) { message.warning(`没有可${label}的账号`); return; }
  inventoryBusy.value = true;
  ids.forEach(id => checkingIds.value.add(id));
  try {
    const r = await api.post("/inventory/accounts/check", { ids });
    const s = r.data?.summary || {};
    message.success(`${label}完成：valid=${s.valid || 0}  invalid=${s.invalid || 0}  unknown=${s.unknown || 0}`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label}失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    ids.forEach(id => checkingIds.value.delete(id));
    inventoryBusy.value = false;
  }
}
function verifySelected() {
  runCheck(Array.from(selectedIds.value), "验证选中");
}
function verifyAllUnknown() {
  runCheck(unknownOrUncheckedIds.value, "验证未检/未知");
}

function emailsForIds(ids: number[]): string[] {
  const map = new Map(inventory.value.accounts.map(a => [a.id, a.email]));
  return ids.map(i => map.get(i) || `id=${i}`);
}
function confirmAndDelete(ids: number[], label: string) {
  if (!ids.length) { message.warning(`没有可${label}的账号`); return; }
  const emails = emailsForIds(ids);
  const preview = emails.slice(0, 5).join(", ") + (emails.length > 5 ? `, ... 共 ${emails.length} 个` : "");
  dialog.warning({
    title: `确认${label}？`,
    content: () => h("div", { style: "font-size:12px; line-height:1.6" }, [
      h("div", `将永久删除 ${ids.length} 个账号（pipeline_results / card_results 等审计行保留）。`),
      h("div", { style: "margin-top:6px; color:#7a7363; word-break:break-all" }, preview),
    ]),
    positiveText: "确认删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      inventoryBusy.value = true;
      try {
        const r = await api.post("/inventory/accounts/delete", { ids });
        message.success(`已删除 ${r.data?.deleted ?? 0} 个`);
        selectedIds.value = new Set();
        await refreshInventory();
      } catch (e: any) {
        message.error(`删除失败：${e?.response?.data?.detail || e?.message || e}`);
      } finally {
        inventoryBusy.value = false;
      }
    },
  });
}
function _selectedEmails(): string[] {
  return emailsForIds(Array.from(selectedIds.value)).filter(e => e && !e.startsWith("id="));
}

async function payOnlySelected() {
  const emails = _selectedEmails();
  if (!emails.length) { message.warning("没有选中账号"); return; }
  const preview = emails.slice(0, 3).join(", ") + (emails.length > 3 ? `... 共 ${emails.length}` : "");
  if (!confirm(`对 ${emails.length} 个选中账号跑 pay-only？\n${preview}\n\n模式：${form.value.gopay ? "GoPay" : (form.value.paypal ? "PayPal" : "Card")}`)) return;
  starting.value = true;
  try {
    await api.post("/run/start", {
      mode: "single",
      paypal: form.value.paypal,
      gopay: form.value.gopay,
      pay_only: true,
      register_only: false,
      register_mode: form.value.register_mode || "browser",
      target_emails: emails,
    });
    message.success(`已对 ${emails.length} 个账号启动 pay-only`);
    await refreshStatus();
    if (status.value.running) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "启动失败");
  } finally {
    starting.value = false;
  }
}

async function rtOnlySelected() {
  const emails = _selectedEmails();
  if (!emails.length) { message.warning("没有选中账号"); return; }
  const preview = emails.slice(0, 3).join(", ") + (emails.length > 3 ? `... 共 ${emails.length}` : "");
  if (!confirm(`对 ${emails.length} 个选中账号跑 rt-only（只补 refresh_token，不付款）？\n${preview}`)) return;
  starting.value = true;
  try {
    await api.post("/run/start", {
      mode: "single",
      paypal: false,
      gopay: false,
      pay_only: false,
      register_only: false,
      rt_only: true,
      register_mode: form.value.register_mode || "browser",
      target_emails: emails,
    });
    message.success(`已对 ${emails.length} 个账号启动 rt-only`);
    await refreshStatus();
    if (status.value.running) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "启动失败");
  } finally {
    starting.value = false;
  }
}

function deleteSelected() {
  confirmAndDelete(Array.from(selectedIds.value), "删除选中");
}
function deleteAllInvalid() {
  confirmAndDelete(invalidIds.value, "删除所有失效");
}

// ── plan + CPA 推送 ─────────────────────────────────
function planLabel(p: string) {
  if (p === "team") return "team";
  if (p === "plus") return "plus";
  return "free";
}
function planBadgeClass(p: string) {
  if (p === "team") return "badge-team";
  if (p === "plus") return "badge-plus";
  return "badge-ghost";
}
function cpaLabel(acc: InventoryAccount) {
  if (acc.cpa_pushed) return "✓ 已推 CPA";
  if (acc.cpa_status && acc.cpa_status !== "ok") return `✗ ${acc.cpa_status}`;
  return "○ 未推 CPA";
}
function cpaBadgeClass(acc: InventoryAccount) {
  if (acc.cpa_pushed) return "badge-ok";
  if (acc.cpa_status && acc.cpa_status !== "ok") return "badge-err";
  return "badge-ghost";
}
const unpushedIds = computed(() =>
  inventory.value.accounts.filter(a => !a.cpa_pushed).map(a => a.id)
);

async function pushCpa(ids: number[], label: string) {
  if (!ids.length) { message.warning(`没有可${label}的账号`); return; }
  inventoryBusy.value = true;
  try {
    const r = await api.post("/inventory/accounts/cpa-push", { ids });
    const s = r.data?.summary || {};
    message.success(`${label}完成：ok=${s.ok || 0}  no_rt=${s.no_rt || 0}  fail=${s.fail || 0}`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label}失败：${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    inventoryBusy.value = false;
  }
}
function pushOneToCpa(id: number) { pushCpa([id], "推送 CPA"); }
function pushSelectedToCpa() { pushCpa(Array.from(selectedIds.value), "批量推送选中"); }
function pushAllUnpushed() { pushCpa(unpushedIds.value, "推送所有未推送"); }

async function refreshInventory() {
  if (inventoryLoading.value) return;
  inventoryLoading.value = true;
  inventoryError.value = "";
  try {
    const r = await api.get<InventoryResponse>("/inventory/accounts");
    inventory.value = r.data;
  } catch (e: any) {
    inventoryError.value = e?.response?.status
      ? `HTTP ${e.response.status}${e.response.data?.detail ? `: ${e.response.data.detail}` : ""}`
      : (e?.message || "请求失败");
  }
  finally {
    inventoryLoading.value = false;
  }
}

async function refreshPreview() {
  try {
    const r = await api.post("/run/preview", form.value);
    cmdPreview.value = r.data.cmd_str;
  } catch {}
}

async function refreshStatus() {
  try {
    const r = await api.get<RunStatus>("/run/status");
    status.value = r.data;
  } catch {}
}

async function checkConfigHealth() {
  if (configHealthLoading.value) return configHealth.value;
  configHealthLoading.value = true;
  try {
    const r = await api.post<ConfigHealthResponse>("/config/health", form.value);
    configHealth.value = r.data;
    return r.data;
  } catch (e: any) {
    message.error(healthErrorText(e));
    return null;
  } finally {
    configHealthLoading.value = false;
  }
}

async function start() {
  starting.value = true;
  try {
    const health = await checkConfigHealth();
    if (!health?.ok) {
      const first = health?.blocking?.[0];
      message.error(first?.message || "配置健康检查未通过，已阻止启动");
      return;
    }
    await api.post("/run/start", form.value);
    message.success("已启动");
    lines.value = [];
    await refreshStatus();
    await refreshInventory();
    openStream();
  } catch (e: any) {
    message.error(healthErrorText(e) || "启动失败");
  } finally {
    starting.value = false;
  }
}

async function stop() {
  stopping.value = true;
  try {
    await api.post("/run/stop");
    message.success("已发送 SIGTERM");
    await refreshStatus();
  } catch (e: any) {
    message.error(e.response?.data?.detail || "停止失败");
  } finally {
    stopping.value = false;
  }
}

function openStream() {
  if (eventSource) eventSource.close();
  const url = import.meta.env.BASE_URL + "api/run/stream";
  eventSource = new EventSource(url, { withCredentials: true });
  eventSource.addEventListener("line", (e) => {
    try {
      const entry = JSON.parse((e as MessageEvent).data);
      // 预计算渲染只读字段，避免每次 re-render 跑 regex / Date 构造，
      // 5000+ 行循环时省下大量主线程时间
      entry.cls = logClass(entry.line || "");
      entry.tsLabel = formatTs(entry.ts);
      Object.freeze(entry); // 阻止 Vue 把每行也代理成 reactive
      lines.value.push(entry);
      if (lines.value.length > 1500) lines.value.splice(0, 500);
      scheduleScrollToBottom();
    } catch {}
  });
  eventSource.addEventListener("otp_pending", () => {
    if (otpDialog.value.dismissed) return; // 用户已手动关闭这次 run 的弹窗
    if (!otpDialog.value.open) {
      otpDialog.value.open = true;
      otpDialog.value.value = "";
      otpDialog.value.autoFilled = false;
      otpDialog.value.openedAt = Math.floor(Date.now() / 1000);
      startOtpPolling();
    }
  });
  eventSource.addEventListener("done", async () => {
    eventSource?.close();
    eventSource = null;
    otpDialog.value.open = false;
    otpDialog.value.dismissed = false; // 新一轮 run 重新允许弹窗
    stopOtpPolling();
    await refreshStatus();
    await refreshInventory();
    // Auto-loop 还在跑就重连 SSE，等 runner.start 启下一 iteration 的进程
    if (autoLoop.value.running) {
      setTimeout(() => {
        if (autoLoop.value.running && !eventSource) openStream();
      }, 1500);
    }
  });
  eventSource.onerror = () => {
    // 连接断开，不自动 retry
    eventSource?.close();
    eventSource = null;
  };
}

async function logout() {
  await api.post("/logout");
  router.push("/login");
}

async function submitOtp() {
  const v = otpDialog.value.value.trim();
  if (!v) {
    message.warning("请输入 OTP");
    return;
  }
  otpDialog.value.submitting = true;
  try {
    await api.post("/run/otp", { otp: v });
    otpDialog.value.open = false;
    otpDialog.value.value = "";
    stopOtpPolling();
    message.success("OTP 已提交");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "提交失败");
  } finally {
    otpDialog.value.submitting = false;
  }
}

function dismissOtpModal() {
  otpDialog.value.open = false;
  otpDialog.value.value = "";
  otpDialog.value.dismissed = true;
  stopOtpPolling();
  message.info("已关闭 OTP 弹窗（这次 run 内不再自动弹）");
}

function startOtpPolling() {
  stopOtpPolling();
  otpPollTimer = setInterval(async () => {
    if (!otpDialog.value.open || otpDialog.value.autoFilled) return;
    try {
      const r = await api.get("/whatsapp/latest-otp-session", {
        params: { since: otpDialog.value.openedAt },
      });
      if (r.status === 200 && r.data?.otp) {
        const code = String(r.data.otp);
        otpDialog.value.value = code;
        otpDialog.value.autoFilled = true;
        // gopay.py 自己也在 polling /latest-otp，OTP 一旦进 wa_state 就被它取走了，
        // 弹窗的 /run/otp 提交只是为了清后端 _otp_pending 标记 + 日志可见性。
        // 不论提交成功与否，弹窗立刻关掉，避免残留。
        otpDialog.value.open = false;
        otpDialog.value.value = "";
        // 关键：标 dismissed=true 阻止 SSE otp_pending 心跳（每 300ms 一次）
        // 把模态框又弹回来。`done` 事件会自动 reset 给下一轮 run 用。
        otpDialog.value.dismissed = true;
        stopOtpPolling();
        message.success(`OTP 自动填入并关闭：${code}（gopay 自取中）`);
        // 后台 fire-and-forget，不阻塞、不影响关弹
        api.post("/run/otp", { otp: code }).catch(() => {});
      }
    } catch {
      // 静默；下一轮再试
    }
  }, 1500);
}

function stopOtpPolling() {
  if (otpPollTimer) {
    clearInterval(otpPollTimer);
    otpPollTimer = undefined;
  }
}

const isFreeMode = computed(() =>
  form.value.mode === "free_register" || form.value.mode === "free_backfill_rt"
);

watch(
  () => [form.value.mode, form.value.paypal, form.value.gopay, form.value.pay_only, form.value.register_only, form.value.batch, form.value.workers, form.value.self_dealer, form.value.count, form.value.register_mode],
  () => {
    configHealth.value = null;
    refreshPreview();
  },
  { immediate: false }
);

onMounted(async () => {
  tick();
  clockTimer = setInterval(tick, 1000);

  // 从 wizard store 推断默认支付方式：card 不带 --paypal，其它都带
  try {
    await store.loadFromServer();
    const pm = (store.answers.payment as any)?.method;
    if (pm === "gopay") {
      form.value.gopay = true;
      form.value.paypal = false;
    } else if (pm === "card") {
      form.value.paypal = false;
    } else if (pm === "paypal" || pm === "both") {
      form.value.paypal = true;
    }
  } catch {}

  await refreshStatus();
  await refreshPreview();
  await checkConfigHealth();
  await refreshInventory();
  if (status.value.running) {
    openStream();
  }
  statusTimer = setInterval(refreshStatus, 5000);
  inventoryTimer = setInterval(refreshInventory, 15000);
  refreshLinkStates();
  linkPollTimer = setInterval(refreshLinkStates, 5000);
  loadCurrentProxy();
  refreshAutoLoop();
  autoLoopPollTimer = setInterval(refreshAutoLoop, 4000);
});

onBeforeUnmount(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (statusTimer) clearInterval(statusTimer);
  if (inventoryTimer) clearInterval(inventoryTimer);
  if (linkPollTimer) clearInterval(linkPollTimer);
  if (autoLoopPollTimer) clearInterval(autoLoopPollTimer);
  if (eventSource) eventSource.close();
  stopOtpPolling();
});
</script>

<style scoped>
.run-root { height: 100vh; overflow: hidden; display: flex; flex-direction: column; }

.wizard-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand-prompt { color: var(--accent); }
.brand-name { font-weight: 700; font-size: 18px; letter-spacing: 0.04em; }
.brand-sub { color: var(--fg-tertiary); font-size: 12px; }
.brand-clock { color: var(--fg-tertiary); font-size: 11px; margin-left: 16px; font-variant-numeric: tabular-nums; }

.run-nav { display: flex; align-items: center; gap: 4px; }
.nav-link { padding: 6px 14px; color: var(--fg-secondary); text-decoration: none; font-size: 12px; letter-spacing: 0.06em; border: 1px solid transparent; transition: all 80ms; }
.nav-link:hover { color: var(--fg-primary); background: var(--bg-panel); }
.nav-link.active { color: var(--accent); border-color: var(--accent); background: var(--bg-panel); }
.header-btn { background: transparent; border: 1px solid var(--border-strong); color: var(--fg-secondary); padding: 4px 12px; font: inherit; font-size: 11px; letter-spacing: 0.08em; cursor: pointer; transition: all 60ms; margin-left: 12px; }
.header-btn:hover { background: var(--bg-raised); color: var(--fg-primary); border-color: var(--accent); }

.run-body { flex: 1; display: grid; grid-template-columns: 420px minmax(420px, 1fr) minmax(360px, 1fr); gap: 0; min-height: 0; overflow: hidden; }
.run-controls { padding: 24px; overflow-y: auto; border-right: 1px solid var(--border); }
.run-inventory { padding: 20px 22px; overflow-y: auto; border-right: 1px solid var(--border); background: var(--bg-base); min-height: 0; }
.run-logs { display: flex; flex-direction: column; min-height: 0; overflow: hidden; background: var(--bg-panel); }
@media (max-width: 1280px) {
  .run-body { grid-template-columns: 380px 1fr; grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); }
  .run-controls { grid-row: 1 / span 2; }
  .run-inventory { border-right: 0; border-bottom: 1px solid var(--border); }
}

.form-stack { display: flex; flex-direction: column; gap: 12px; margin-bottom: 8px; }
.ctl-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.ctl-row.sub { padding-left: 8px; border-left: 2px solid var(--border-strong); }
.ctl-row.toggles { margin-top: 4px; gap: 16px; flex-wrap: wrap; }
.ctl-row.reg-mode { margin-top: 6px; gap: 12px; font-size: 12px; }
.reg-mode-label { color: var(--fg-tertiary); }
.reg-mode-opt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
}
.reg-mode-opt input { accent-color: var(--accent); }
.reg-mode-opt.active { border-color: var(--accent); color: var(--accent); }

.link-details {
  margin-top: 8px;
  font-size: 12px;
  color: var(--fg-secondary);
}
.link-details > summary {
  cursor: pointer;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  color: var(--fg-tertiary);
}
.link-details > summary::-webkit-details-marker { display: none; }
.link-details > summary::before { content: "▸ "; color: var(--fg-tertiary); }
.link-details[open] > summary::before { content: "▾ "; }
.link-summary.muted { color: var(--fg-tertiary); }
.link-summary.ok { color: var(--ok); }
.link-summary.warn { color: var(--warn); font-weight: 700; }
.link-refresh-btn { margin-left: auto; }
.link-hint {
  margin: 8px 0;
  color: var(--fg-tertiary);
  font-size: 11px;
  line-height: 1.7;
}
.link-hint em { color: var(--err); font-style: normal; }
.link-error {
  border: 1px solid var(--err);
  color: var(--err);
  padding: 6px 10px;
  margin: 6px 0;
  font-size: 12px;
}
.link-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  margin-bottom: 8px;
}
.link-table th, .link-table td {
  padding: 5px 8px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.link-table th { color: var(--fg-tertiary); font-weight: normal; font-size: 11px; }
.link-table td.ts { color: var(--fg-tertiary); white-space: nowrap; font-size: 11px; }
.link-table td.muted { color: var(--fg-tertiary); }
.link-table code { color: var(--accent); }
.badge-linked { color: var(--ok); font-weight: 700; }
.badge-unlinked { color: var(--fg-tertiary); }
.link-empty-inline {
  color: var(--fg-tertiary);
  font-size: 12px;
  padding: 8px 0;
}
.link-manual {
  display: flex; gap: 6px; align-items: center;
  margin-top: 6px; flex-wrap: wrap;
}
.link-manual-input {
  flex: 1; min-width: 200px;
  background: var(--bg-base);
  border: 1px solid var(--border);
  color: var(--fg-primary);
  padding: 5px 8px;
  font: inherit;
  font-size: 12px;
}
.link-manual-input:focus { border-color: var(--accent); outline: none; }
.auto-loop-form, .auto-loop-running { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin: 8px 0; }
.auto-loop-form label { display: inline-flex; gap: 6px; align-items: center; font-size: 12px; color: var(--fg-tertiary); }
.auto-loop-stats { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; }
.auto-loop-stats .ok { color: var(--ok); }
.auto-loop-stats .warn { color: var(--warn); }
.auto-loop-last { font-size: 12px; color: var(--fg-secondary); margin-top: 4px; }
.ctl-hint { color: var(--fg-tertiary); font-size: 11px; line-height: 1.6; margin: 4px 0 0; }
.ctl-hint code { background: var(--bg-panel); padding: 1px 5px; border: 1px solid var(--border); font-size: 11px; }
.ctl-label { font-size: 11px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--fg-secondary); min-width: 60px; }

.mode-pills { display: flex; gap: 0; border: 1px solid var(--border-strong); flex-wrap: wrap; }
.mode-pill { background: #fff; border: 0; border-right: 1px solid var(--border); padding: 8px 14px; font: inherit; font-size: 12px; cursor: pointer; color: var(--fg-secondary); transition: all 80ms; }
.mode-pill:last-child { border-right: 0; }
.mode-pill:hover:not(:disabled) { background: var(--bg-raised); color: var(--fg-primary); }
.mode-pill.active { background: var(--accent); color: #fff; }
.mode-pill:disabled { cursor: not-allowed; opacity: 0.5; }

.cmd-preview {
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  padding: 12px 14px;
  font-size: 12px;
  color: var(--fg-primary);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  line-height: 1.6;
}

.step-actions { margin-top: 16px; margin-bottom: 0; }

.health-panel {
  margin-top: 12px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 12px;
  font-size: 12px;
}
.health-panel.ok { border-color: color-mix(in srgb, var(--ok) 45%, var(--border)); }
.health-panel.fail { border-color: color-mix(in srgb, var(--err) 55%, var(--border)); }
.health-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.health-title {
  color: var(--fg-primary);
  font-weight: 700;
}
.health-meta {
  margin-left: auto;
  color: var(--fg-tertiary);
  font-size: 11px;
}
.health-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.health-row {
  display: grid;
  grid-template-columns: 54px 1fr;
  gap: 10px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.health-status {
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
}
.health-ok .health-status { color: var(--ok); }
.health-warn .health-status { color: var(--warn); }
.health-fail .health-status { color: var(--err); }
.health-body strong {
  color: var(--fg-primary);
  font-weight: 700;
}
.health-sub {
  margin-top: 3px;
  color: var(--fg-tertiary);
  line-height: 1.55;
  word-break: break-word;
}

.status-line { margin-top: 16px; padding: 10px 12px; background: var(--bg-panel); border: 1px solid var(--border); font-size: 12px; color: var(--fg-secondary); }
.status-line.running { border-color: var(--accent); }
.status-dot { color: var(--fg-tertiary); margin-right: 6px; }
.status-line.running .status-dot { color: var(--accent); animation: pulse 1.2s ease-in-out infinite; }
.status-dot.ok { color: var(--ok); }
.status-dot.err { color: var(--err); }
.status-dot.idle { color: var(--fg-tertiary); }

.inventory-divider { margin-top: 22px; }
.inventory-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.inventory-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.inventory-label {
  color: var(--fg-tertiary);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.inventory-value {
  color: var(--fg-secondary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.inventory-error {
  margin-bottom: 12px;
  border: 1px solid var(--warn);
  background: color-mix(in srgb, var(--warn) 12%, var(--bg-panel));
  color: var(--warn);
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.6;
}
.inventory-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.inventory-stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 10px 12px;
}
.inventory-stat-label {
  color: var(--fg-tertiary);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.inventory-stat strong {
  color: var(--fg-primary);
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}
.inventory-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-bottom: 6px;
  padding: 6px 8px;
  background: var(--bg-base);
  border: 1px dashed var(--border);
  font-size: 12px;
}
.inv-filter-input {
  flex: 0 1 200px;
  min-width: 140px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  color: var(--fg-primary);
  padding: 4px 8px;
  font: inherit;
  font-size: 12px;
}
.inv-filter-input:focus { border-color: var(--accent); outline: none; }
.inv-filter-sel {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  color: var(--fg-primary);
  padding: 4px 8px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.inv-filter-count {
  color: var(--fg-tertiary);
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}
.inventory-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 8px 0 4px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  background: var(--bg-panel);
  flex-wrap: wrap;
}
.inventory-toolbar-check {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--fg-secondary);
  cursor: pointer;
  user-select: none;
}
.inventory-toolbar-check input { accent-color: var(--accent); }
.inventory-toolbar-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.inventory-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 4px;
}
.inventory-row {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  padding: 12px;
}
.inventory-row--selected {
  border-color: var(--accent);
  background: #fff7ec;
}
.inventory-row-check {
  margin-right: 4px;
  accent-color: var(--accent);
  cursor: pointer;
}
.inventory-row-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.inventory-email {
  font-weight: 700;
  color: var(--fg-primary);
  word-break: break-all;
}
.inventory-row-sub,
.inventory-row-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  color: var(--fg-tertiary);
  font-size: 11px;
  line-height: 1.6;
  word-break: break-word;
}
.inventory-row-detail {
  margin-top: 6px;
}
.inventory-inline-flag {
  color: var(--accent);
}
.inventory-empty {
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  color: var(--fg-tertiary);
  padding: 14px;
  font-size: 12px;
  line-height: 1.7;
}
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 0 8px;
  border: 1px solid var(--border);
  color: var(--fg-secondary);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  white-space: nowrap;
}
.badge-ok { border-color: var(--ok); color: var(--ok); }
.badge-warn { border-color: var(--warn); color: var(--warn); }
.badge-err { border-color: var(--err); color: var(--err); }
.badge-ghost { border-color: var(--border); color: var(--fg-tertiary); }
.badge-plus { border-color: #2563eb; color: #2563eb; }
.badge-team { border-color: #7c3aed; color: #7c3aed; }
.inventory-row-action {
  margin-left: auto;
  padding: 3px 9px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px;
  background: transparent;
  border: 1px solid var(--accent);
  color: var(--accent);
  cursor: pointer;
  transition: background .15s, color .15s;
}
.inventory-row-action:hover:not(:disabled) {
  background: var(--accent);
  color: #fff;
}
.inventory-row-action:disabled {
  opacity: .5;
  cursor: not-allowed;
}
@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

.logs-head { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; color: var(--accent); font-weight: 700; font-size: 12px; letter-spacing: 0.06em; }
.pre-prompt { color: var(--fg-tertiary); }
.logs-meta { color: var(--fg-tertiary); font-size: 11px; font-weight: 400; }
.auto-scroll-toggle { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--fg-secondary); cursor: pointer; user-select: none; font-weight: 400; letter-spacing: 0; }
.auto-scroll-toggle input { accent-color: var(--accent); }

.logs-stream { flex: 1; overflow-y: auto; padding: 8px 16px 12px; font-size: 11px; background: var(--bg-base); }
.logs-empty { color: var(--fg-tertiary); padding: 32px 0; text-align: center; }
.log-line { display: grid; grid-template-columns: 70px 1fr; gap: 10px; padding: 1px 0; align-items: baseline; }
.log-ts { color: var(--fg-tertiary); font-variant-numeric: tabular-nums; font-size: 10px; }
.log-msg { color: var(--fg-primary); white-space: pre-wrap; word-break: break-all; }
.log-line.log-err .log-msg { color: var(--err); }
.log-line.log-warn .log-msg { color: var(--warn); }
.log-line.log-ok .log-msg { color: var(--ok); }


/* GoPay OTP modal */
.otp-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.otp-modal {
  background: var(--bg-base);
  border: 1px solid var(--accent);
  padding: 24px 28px;
  width: min(420px, 90vw);
  font-family: inherit;
  box-shadow: 0 10px 40px rgba(0,0,0,0.25);
}
.otp-head {
  font-weight: 700;
  font-size: 14px;
  letter-spacing: 0.06em;
  color: var(--accent);
  margin-bottom: 4px;
}
.otp-prompt { color: var(--fg-tertiary); margin-right: 6px; }
.otp-desc { color: var(--fg-secondary); font-size: 12px; line-height: 1.6; margin: 8px 0 16px; }
.otp-input {
  width: 100%; box-sizing: border-box;
  padding: 12px 14px;
  border: 1px solid var(--border-strong);
  background: var(--bg-panel);
  font: inherit; font-size: 22px;
  letter-spacing: 0.4em;
  text-align: center;
  color: var(--fg-primary);
  outline: none;
  font-variant-numeric: tabular-nums;
}
.otp-input:focus { border-color: var(--accent); }
.otp-actions { margin-top: 16px; display: flex; justify-content: flex-end; }
.otp-modal { position: relative; }
.otp-close {
  position: absolute;
  top: 8px;
  right: 10px;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--fg-tertiary);
  width: 28px;
  height: 28px;
  line-height: 1;
  font-size: 18px;
  cursor: pointer;
  font-family: inherit;
  padding: 0;
}
.otp-close:hover {
  border-color: var(--err);
  color: var(--err);
}
.otp-close:disabled { opacity: 0.4; cursor: not-allowed; }

@media (max-width: 1024px) {
  .inventory-stats { grid-template-columns: 1fr; }
}
@media (max-width: 900px) {
  .run-body { grid-template-columns: 1fr; grid-template-rows: auto auto 1fr; }
  .run-controls, .run-inventory { grid-row: auto; border-right: 0; border-bottom: 1px solid var(--border); }
  .inventory-head { align-items: flex-start; flex-direction: column; }
}
</style>
