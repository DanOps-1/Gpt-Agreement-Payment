<template>
  <div class="run-root">
    <header class="wizard-header">
      <div class="brand">
        <span class="brand-prompt">$</span>
        <span class="brand-name">gpt-pay</span>
        <span class="brand-sub">// Run Control</span>
        <span class="brand-clock">{{ clock }}</span>
      </div>
      <div class="run-nav">
        <RouterLink to="/wizard" class="nav-link">Config Wizard</RouterLink>
        <RouterLink to="/run" class="nav-link active">Run</RouterLink>
        <RouterLink to="/outlook" class="nav-link">Outlook Pool</RouterLink>
        <RouterLink to="/promo-links" class="nav-link">Promo Long Links</RouterLink>
        <button class="header-btn" @click="logout">Logout</button>
      </div>
    </header>

    <div class="run-body">
      <section class="run-controls">
        <div class="term-divider" data-tail="──────────">Parameters</div>
        <div class="form-stack">
          <div class="ctl-row">
            <span class="ctl-label">Mode</span>
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
          <div v-if="form.mode === 'free_register' || form.mode === 'promo_link'" class="ctl-row sub">
            <TermField v-model.number="form.count" label="count (0=unlimited)" type="number" />
          </div>
          <p v-if="form.mode === 'promo_link'" class="ctl-hint">
            Register / login outlook email → Call ChatGPT checkout to get promo-hit hosted long URL → Save to promo_links table.
            "Account already exists" does not fast-fail (go through OTP login to get credentials). Region / currency can now be freely specified.
          </p>
          <div v-if="form.mode === 'promo_link'" class="promo-region-box">
            <div class="ctl-row reg-mode">
              <span class="reg-mode-label">Long Link Region ·</span>
              <button
                v-for="preset in promoRegionPresets"
                :key="preset.country"
                type="button"
                class="reg-mode-opt"
                :class="{ active: form.promo_country === preset.country && form.promo_currency === preset.currency }"
                @click="applyPromoRegion(preset.country, preset.currency)"
              >{{ preset.label }}</button>
            </div>
            <div class="ctl-row sub promo-region-fields">
              <label class="promo-select-wrap">
                <span>plan</span>
                <select v-model="form.promo_plan" class="promo-select">
                  <option value="plus">plus</option>
                  <option value="team">team</option>
                </select>
              </label>
              <TermField
                v-model="form.promo_country"
                label="country"
                placeholder="ID / US / JP"
                :error="!promoCountryOk"
                :ok="promoCountryOk"
                @blur="normalizePromoRegion"
              />
              <TermField
                v-model="form.promo_currency"
                label="currency"
                placeholder="IDR / USD / JPY"
                :error="!promoCurrencyOk"
                :ok="promoCurrencyOk"
                @blur="normalizePromoRegion"
              />
              <TermField
                v-model="form.promo_campaign_id"
                label="campaign"
                placeholder="empty=default plus/team free campaign"
              />
            </div>
            <p class="ctl-hint">
              Current will generate <code>{{ form.promo_plan }}</code> /
              <code>{{ normalizedPromoCountry }}</code>/<code>{{ normalizedPromoCurrency }}</code>
              long links; can also directly input any 2-letter country code + 3-letter currency.
            </p>
          </div>

          <!-- no_card_plus mode parameters: triggers scripts/no_card_paypal_plus.py -->
          <div v-if="form.mode === 'no_card_plus'" class="promo-region-box">
            <p class="ctl-hint">
              Use fresh plus long links from promo_links table → Chromium RPA through PayPal guest signup → Open Plus for 0 yuan.
              SMS API URL read from <code>config.paypal.json::paypal.sms_api_url</code>, not via cmdline.
            </p>
            <div class="ctl-row sub promo-region-fields">
              <TermField
                v-model.number="form.no_card_promo_link_id"
                label="promo_link_id (0=auto pick latest fresh plus)"
                type="number"
              />
              <TermField
                v-model="form.no_card_phone"
                label="phone (E.164, no +)"
                placeholder="US 10-digit local number"
              />
              <TermField
                v-model="form.no_card_sms_api_url"
                label="SMS API URL (with key, localStorage persistent)"
                placeholder="http://a.62-us.com/api/get_sms?key=..."
              />
              <TermField
                v-model.number="form.no_card_otp_timeout"
                label="OTP timeout (s)"
                type="number"
              />
              <TermField
                v-model.number="form.no_card_signup_retries"
                label="signup retries"
                type="number"
              />
              <TermField
                v-model.number="form.no_card_node_rpa_timeout"
                label="node RPA timeout (s)"
                type="number"
              />
              <TermField
                v-model.number="form.no_card_max_due"
                label="max due (cents, gatekeeper)"
                type="number"
              />
            </div>
            <div class="ctl-row toggles">
              <TermToggle v-model="form.no_card_allow_already_paid">Allow already plus accounts to run</TermToggle>
              <TermToggle v-model="form.no_card_allow_full_price">Allow full price (run without promo)</TermToggle>
            </div>
            <div class="ctl-row reg-mode">
              <span class="reg-mode-label">auto-gen inventory source ·</span>
              <label class="reg-mode-opt" :class="{ active: form.no_card_inventory_mail_source === 'any' }">
                <input type="radio" value="any" v-model="form.no_card_inventory_mail_source" />
                any
              </label>
              <label class="reg-mode-opt" :class="{ active: form.no_card_inventory_mail_source === 'outlook' }">
                <input type="radio" value="outlook" v-model="form.no_card_inventory_mail_source" />
                Microsoft email (@outlook/@hotmail/@live/@msn)
              </label>
              <label class="reg-mode-opt" :class="{ active: form.no_card_inventory_mail_source === 'catch_all' }">
                <input type="radio" value="catch_all" v-model="form.no_card_inventory_mail_source" />
                Domain email (catch_all_domain)
              </label>
            </div>
            <p class="ctl-hint">
              When <code>allow_already_paid</code> is off: reject if account RT plan is already plus/team/pro.
              When <code>allow_full_price</code> is off: reject if Stripe due > max_due (avoid charging without promo).
              Card defaults to meiguodizhi random card (no fixed-card passed).
              <br />
              <code>auto-gen inventory source</code>: when promo_links inventory empty, auto-generate new long links from registered_accounts;
              select <code>Microsoft email</code> to only pick outlook-type accounts; <code>Domain email</code> to only pick catch_all_domain aliases from CTF-reg config.
            </p>
          </div>

          <!-- no_card_plus_parallel mode: N worker concurrent, phone pool M can be < N -->
          <div v-if="form.mode === 'no_card_plus_parallel'" class="promo-region-box">
            <p class="ctl-hint">
              Run <b>N</b> workers concurrently. Phone pool can be fewer than N — when multiple workers share the same phone, SMS OTP
              auto-queue before triggering (phone-lock), pre-OTP/post-OTP phases stay parallel.
              DB uses atomic claim to prevent two workers grabbing same promo_link.
              Currently sharing same exit IP / gost relay.
            </p>
            <div class="ctl-row sub promo-region-fields">
              <TermField
                v-model.number="parallel.concurrency"
                label="concurrency N (worker count, can exceed phone count)"
                type="number"
              />
              <TermField
                v-model="parallel.default_sms_url"
                label="default SMS API URL (fallback when phone row is empty)"
                placeholder="http://a.62-us.com/api/get_sms?key=..."
              />
              <TermField
                v-model.number="parallel.otp_timeout"
                label="OTP timeout (s)"
                type="number"
              />
              <TermField
                v-model.number="parallel.signup_retries"
                label="signup retries"
                type="number"
              />
              <TermField
                v-model.number="parallel.node_rpa_timeout"
                label="node RPA timeout (s)"
                type="number"
              />
              <TermField
                v-model.number="parallel.max_due"
                label="max due (cents)"
                type="number"
              />
              <TermField
                v-model.number="parallel.stagger_s"
                label="stagger startup (s, prevent same-second hit to gost)"
                type="number"
              />
            </div>
            <div class="ctl-row toggles">
              <TermToggle v-model="parallel.allow_already_paid">Allow already plus accounts to run</TermToggle>
              <TermToggle v-model="parallel.allow_full_price">Allow full price (run without promo)</TermToggle>
            </div>
            <div class="ctl-row reg-mode">
              <span class="reg-mode-label">auto-gen inventory source ·</span>
              <label class="reg-mode-opt" :class="{ active: parallel.inventory_mail_source === 'any' }">
                <input type="radio" value="any" v-model="parallel.inventory_mail_source" />
                any
              </label>
              <label class="reg-mode-opt" :class="{ active: parallel.inventory_mail_source === 'outlook' }">
                <input type="radio" value="outlook" v-model="parallel.inventory_mail_source" />
                Microsoft email
              </label>
              <label class="reg-mode-opt" :class="{ active: parallel.inventory_mail_source === 'catch_all' }">
                <input type="radio" value="catch_all" v-model="parallel.inventory_mail_source" />
                Domain email
              </label>
            </div>

            <div class="term-divider" data-tail="──────────">Phone Pool ({{ parallel.workers.length }} items)</div>
            <div v-for="(w, idx) in parallel.workers" :key="idx" class="ctl-row sub promo-region-fields">
              <TermField
                v-model="w.phone"
                :label="`slot${idx + 1} phone`"
                placeholder="US 10-digit local number"
              />
              <TermField
                v-model="w.sms_url"
                :label="`slot${idx + 1} sms_url (empty = use default)`"
                placeholder="http://...?key=..."
              />
              <TermField
                v-model="w.tag"
                :label="`slot${idx + 1} tag`"
                placeholder="optional, remark"
              />
              <TermBtn @click="removeParallelWorker(idx)" :disabled="parallel.workers.length <= 1">−</TermBtn>
            </div>
            <div class="ctl-row toggles">
              <TermBtn @click="addParallelWorker">＋ Add phone</TermBtn>
            </div>
            <p v-if="parallelMapping.length" class="ctl-hint">
              <b>Mapping Preview</b> (N={{ parallel.concurrency }} workers → {{ parallelMapping.length === parallel.concurrency ? parallel.workers.filter(w => (w.phone || '').trim()).length : 0 }} phone slots):
              <span v-for="m in parallelMapping" :key="m.worker_id" style="margin-right: 0.8em">
                <code>{{ m.worker_id }}</code>→<code>{{ m.phone || '(empty)' }}</code>{{ m.tag ? ` ${m.tag}` : '' }}
              </span>
            </p>

            <div class="ctl-row toggles">
              <TermBtn @click="startParallel" :disabled="parallel.busy || parallelRunning">
                {{ parallel.busy ? "..." : "Start Parallel" }}
              </TermBtn>
              <TermBtn @click="stopParallel" :disabled="!parallelRunning">
                Stop All
              </TermBtn>
              <TermBtn @click="refreshParallelStatus" :disabled="parallel.statusBusy">
                {{ parallel.statusBusy ? "..." : "Refresh" }}
              </TermBtn>
              <TermBtn @click="clearParallel" :disabled="parallelRunning">
                Clear Completed
              </TermBtn>
            </div>

            <div v-if="parallel.summary" class="health-panel" :class="{ ok: parallel.summary.failed === 0, fail: parallel.summary.failed > 0 }">
              <div class="health-head">
                <span class="health-title">Parallel Status</span>
                <span class="health-meta">
                  total {{ parallel.summary.total_workers }}
                  | running {{ parallel.summary.running }}
                  | succeeded {{ parallel.summary.succeeded }}
                  | failed {{ parallel.summary.failed }}
                </span>
              </div>
              <div class="health-list">
                <div v-for="w in parallel.summary.workers" :key="w.worker_id"
                     class="health-row" :class="`health-${w.running ? 'running' : (w.exit_code === 0 ? 'ok' : 'fail')}`">
                  <span class="health-status">
                    {{ w.running ? "▶" : (w.exit_code === 0 ? "✓" : "✗") }}
                  </span>
                  <div class="health-body">
                    <strong>{{ w.worker_id }}<span v-if="w.tag"> ({{ w.tag }})</span></strong>
                    phone={{ w.phone }} sms={{ w.sms_url_redacted }}
                    <div class="health-sub" v-if="w.current_event">{{ w.current_event }}</div>
                  </div>
                </div>
              </div>
              <p class="ctl-hint" style="margin-top: 0.4em">
                Each worker's real-time logs will be merged into the "Real-time Logs" section at bottom with <code>[worker_id]</code> prefix per line.
              </p>
            </div>
          </div>

          <p class="ctl-hint">
            UI now shows on-demand: only expand config and tools that current mode / payment method actually uses.
          </p>

          <div v-if="showPaymentSelector" class="ctl-row toggles">
            <TermToggle v-model="form.paypal" :disabled="form.gopay || form.qris">PayPal Payment</TermToggle>
            <TermToggle v-model="form.gopay" :disabled="form.qris" @update:modelValue="onGoPayToggle">GoPay (Indonesia)</TermToggle>
            <TermToggle v-model="form.qris" :disabled="form.gopay" @update:modelValue="onQrisToggle">QRIS (QR Code)</TermToggle>
          </div>
          <div v-if="showRunModifiers" class="ctl-row toggles">
            <TermToggle v-model="form.pay_only">--pay-only</TermToggle>
            <TermToggle v-model="form.register_only" :disabled="form.pay_only">--register-only</TermToggle>
          </div>
          <p v-if="showRunModifiers" class="ctl-hint">
            <code>--pay-only</code> Skip registration, prefer reusing recently registered but unpaid accounts;
            <code>--register-only</code> Register only, no payment.
            <span v-if="form.register_only">Currently register-only; payment method config hidden.</span>
          </p>

          <div v-if="showRegisterPath" class="ctl-row reg-mode">
            <span class="reg-mode-label">Register Path ·</span>
            <label class="reg-mode-opt" :class="{ active: form.register_mode === 'protocol' }">
              <input type="radio" value="protocol" v-model="form.register_mode" />
              Protocol Only (auth_flow + Node/QuickJS)
            </label>
            <label class="reg-mode-opt" :class="{ active: form.register_mode === 'browser' }">
              <input type="radio" value="browser" v-model="form.register_mode" />
              Browser Emulation (Camoufox/Playwright)
            </label>
          </div>
          <p v-if="showRegisterPath" class="ctl-hint">
            <code>protocol</code>: <code>AuthFlow</code> HTTP direct + Node/QuickJS Sentinel; RT refresh uses <code>run_protocol_login</code>.
            <code>browser</code>: Camoufox real browser; RT refresh uses <code>_exchange_refresh_token_with_session</code>.
            Both fail when OpenAI enforces <code>add-phone</code>.
          </p>

          <!-- Email source (mutually exclusive): Outlook SMS code pool / self-hosted domain catch-all -->
          <div v-if="showMailSource" class="ctl-row reg-mode">
            <span class="reg-mode-label">Email Source ·</span>
            <label class="reg-mode-opt" :class="{ active: form.mail_source === 'outlook' }">
              <input type="radio" value="outlook" v-model="form.mail_source" />
              Outlook Pool (IMAP OAuth2 code receiving)
            </label>
            <label class="reg-mode-opt" :class="{ active: form.mail_source === 'catch_all' }">
              <input type="radio" value="catch_all" v-model="form.mail_source" />
              Domain catch-all (CF Email Worker)
            </label>
          </div>
          <div v-if="showOutlookSelector" class="ctl-row reg-mode">
            <span class="reg-mode-label">Outlook Account ·</span>
            <select v-model="form.outlook_email" class="outlook-select" :disabled="outlookLoading">
              <option value="">— Pick any available from pool ({{ outlookAvailable.length }} available) —</option>
              <option v-for="acc in outlookAvailable" :key="acc.email" :value="acc.email">
                {{ acc.email }}
              </option>
            </select>
            <button type="button" class="reg-mode-opt" @click="reloadOutlookPool" :disabled="outlookLoading">
              {{ outlookLoading ? "..." : "Refresh" }}
            </button>
            <RouterLink to="/outlook" class="reg-mode-opt">Manage Pool</RouterLink>
          </div>
          <p v-if="showOutlookSelector" class="ctl-hint">
            Pool empty / specified email unavailable → error directly, no fallback to catch-all.
            <span v-if="form.outlook_email">Current: <code>{{ form.outlook_email }}</code></span>
          </p>
          <p v-else-if="showCatchAllHint" class="ctl-hint">
            Use <code>mail.catch_all_domain</code> + persona algorithm to generate <code>alias@yourdomain</code>.
            OTP through CF Email Worker → KV → /api/cf-kv/otp fetch.
            No domain configured → error directly.
          </p>
          <p v-else-if="form.pay_only && modeSupportsPayment" class="ctl-hint">
            Currently <code>pay-only</code>: no new registration, register path / email source hidden, use inventory accounts for payment only.
          </p>
          <p v-else-if="isFreeBackfillMode" class="ctl-hint">
            <code>free_backfill_rt</code> only backfill <code>refresh_token</code> for old accounts, no new email or payment method needed.
          </p>
        </div>

        <div v-if="form.mode !== 'no_card_plus_parallel'" class="term-divider" data-tail="──────────">Command</div>
        <pre v-if="form.mode !== 'no_card_plus_parallel'" class="cmd-preview">{{ cmdPreview }}</pre>

        <div v-if="form.mode !== 'no_card_plus_parallel' && configHealth" class="health-panel" :class="{ ok: configHealth.ok, fail: !configHealth.ok }">
          <div class="health-head">
            <span class="health-title">Config Health Check</span>
            <span class="badge" :class="configHealth.ok ? 'badge-ok' : 'badge-err'">
              {{ configHealth.ok ? "Ready" : "Blocked" }}
            </span>
            <span class="health-meta">{{ configHealth.payment_kind }} / {{ configHealth.requires_email_otp ? "requires email OTP" : "no email OTP needed" }}</span>
          </div>
          <div class="health-list">
            <div v-for="chk in visibleHealthChecks" :key="chk.name" class="health-row" :class="`health-${chk.status}`">
              <span class="health-status">{{ healthStatusLabel(chk.status) }}</span>
              <div class="health-body">
                <strong>{{ chk.message }}</strong>
                <div v-if="chk.missing?.length" class="health-sub">Missing: {{ chk.missing.join(", ") }}</div>
                <div v-if="chk.action" class="health-sub">Action: {{ chk.action }}</div>
                <div v-if="chk.details" class="health-sub">Details: {{ chk.details }}</div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="form.mode !== 'no_card_plus_parallel'" class="step-actions">
          <TermBtn variant="ghost" :loading="configHealthLoading" @click="checkConfigHealth">Check Config</TermBtn>
          <TermBtn v-if="!status.running" :loading="starting" @click="start">▶ Start Run</TermBtn>
          <TermBtn v-else variant="danger" :loading="stopping" @click="stop">■ Stop</TermBtn>
        </div>
        <p v-else class="ctl-hint">
          Parallel mode skips single run health check / command preview; use "Start Parallel" button above.
        </p>

        <div class="status-line" :class="{ running: status.running }">
          <span v-if="status.running">
            <span class="status-dot">●</span>
            Running PID {{ status.pid }} // Mode {{ status.mode }} // {{ runtimeText }}
          </span>
          <span v-else-if="status.ended_at">
            <span class="status-dot ok" v-if="status.exit_code === 0">●</span>
            <span class="status-dot err" v-else>●</span>
            Last run exited // Exit code {{ status.exit_code }} //
            {{ runtimeText }}
          </span>
          <span v-else>
            <span class="status-dot idle">○</span> Idle
          </span>
        </div>

        <!-- QRIS QR display: after runner captures [qris] PNG log, status.qris has value -->
        <div v-if="showQrisPanel" class="qris-panel">
          <div class="qris-head">
            <span class="qris-title">QRIS QR Code Payment</span>
            <span class="badge" :class="status.qris?.settled ? 'badge-ok' : 'badge-warn'">
              {{ status.qris?.settled ? "✓ Settled" : "Waiting for QR scan" }}
            </span>
          </div>
          <div class="qris-body">
            <img v-if="status.qris?.png_path" :src="qrPngUrl" alt="QRIS QR" class="qris-img" />
            <div class="qris-meta">
              <p><strong>Reference:</strong> <code>{{ status.qris?.reference }}</code></p>
              <p v-if="status.qris?.expiry"><strong>Expires:</strong> {{ status.qris?.expiry }}</p>
              <p v-if="status.qris?.qr_image_url">
                <strong>Remote Preview:</strong>
                <a :href="status.qris?.qr_image_url" target="_blank" rel="noopener">
                  {{ status.qris?.qr_image_url?.replace(/^https?:\/\//, '').slice(0, 60) }}…
                </a>
              </p>
              <p v-if="status.qris?.deeplink_url" class="qris-deeplink">
                <strong>📱 Direct GoPay App Payment:</strong>
                <a :href="status.qris?.deeplink_url" target="_blank" rel="noopener">Click to open payment in GoPay app on phone</a>
                <br />
                <span class="qris-hint">When GoPay app is installed, use this to skip QR scan + WhatsApp OTP</span>
              </p>
              <p class="qris-hint">
                Or scan QR above: use GoPay / DANA / OVO / ShopeePay etc. Indonesian e-wallet to pay.
                Backend polls Midtrans status, auto verify ChatGPT plan after settlement.
              </p>
            </div>
          </div>
        </div>

        <details v-if="showAutoLoopTools" class="link-details" :open="autoLoop.running">
          <summary>
            Auto Loop ·
            <span class="link-summary" :class="autoLoop.running ? 'warn' : 'muted'">
              {{ autoLoopSummary }}
            </span>
          </summary>
          <p class="link-hint">
            Loop "register + GoPay payment" until cumulative success count = target, or consecutive failures ≥ max.
            Auto handle: <code>cf_429</code> triggers IP rotation; <code>coupon_ineligible</code> mark account as junk and delete;
            other known errors (OTP timeout/wallet insufficient/406 already bound) log and skip.
          </p>
          <div v-if="!autoLoop.running" class="auto-loop-form">
            <label>
              Target Successes
              <input v-model.number="autoLoopForm.target_success" type="number" min="1" max="10000" class="link-manual-input" style="min-width:80px;flex:0 0 auto" />
            </label>
            <label>
              Max Consecutive Failures
              <input v-model.number="autoLoopForm.max_consec_fail" type="number" min="1" max="100" class="link-manual-input" style="min-width:80px;flex:0 0 auto" />
            </label>
            <TermBtn :loading="autoLoopBusy" @click="startAutoLoop">▶ Start Auto Loop</TermBtn>
          </div>
          <div v-else class="auto-loop-running">
            <div class="auto-loop-stats">
              <span><b>iter</b> {{ autoLoop.iteration }}</span>
              <span class="ok"><b>success</b> {{ autoLoop.success_count }}/{{ autoLoop.target_success }}</span>
              <span class="warn"><b>fail</b> {{ autoLoop.fail_count }} (consecutive {{ autoLoop.consecutive_fail }}/{{ autoLoop.max_consec_fail }})</span>
              <span><b>IP Rotations</b> {{ autoLoop.ip_rotations }}</span>
              <span v-if="autoLoop.scrap_marked.length"><b>Marked Junk</b> {{ autoLoop.scrap_marked.length }}</span>
              <span v-if="autoLoop.zone_list.length > 1">
                <b>zone</b> {{ autoLoop.current_zone || '?' }}
                ({{ autoLoop.zone_list.length }} total, rotated {{ autoLoop.total_zone_rotations }} times)
              </span>
              <span v-if="autoLoop.zone_reg_fail_streak > 0" class="warn">
                reg consecutive failures {{ autoLoop.zone_reg_fail_streak }}
              </span>
            </div>
            <div class="auto-loop-last" v-if="autoLoop.last_kind">
              <b>{{ autoLoop.last_kind }}</b> · {{ autoLoop.last_action }}
              <span v-if="autoLoop.last_email"> · {{ autoLoop.last_email }}</span>
            </div>
            <TermBtn variant="danger" :loading="autoLoopBusy" @click="stopAutoLoop">■ Stop Auto Loop</TermBtn>
          </div>
          <p v-if="autoLoop.stop_reason && !autoLoop.running" class="link-hint">
            Last stop reason: <code>{{ autoLoop.stop_reason }}</code>
          </p>
        </details>

        <details v-if="showProxyTools" class="link-details">
          <summary>
            Exit IP ·
            <span class="link-summary" :class="proxyIp ? 'ok' : 'muted'">
              {{ proxyIp || 'Not loaded' }}<span v-if="proxyCountry"> ({{ proxyCountry }})</span>
            </span>
            <TermBtn
              variant="ghost"
              class="link-refresh-btn"
              :loading="rotatingIp"
              @click.prevent="rotateProxyIp"
            >Rotate IP</TermBtn>
          </summary>
          <p class="link-hint" v-if="proxyError">{{ proxyError }}</p>
          <p v-else class="link-hint">
            When hitting Midtrans <code>429 body=</code> (Cloudflare edge rate limit by IP), click "Rotate IP".
            Calls Webshare API to replace pool + switch gost upstream. <em>Consumes Webshare monthly rotation quota.</em>
          </p>
        </details>

        <details v-if="showGopayLinkTools" class="link-details">
          <summary>
            GoPay Link ·
            <span class="link-summary" :class="linkSummaryClass">{{ linkSummaryText }}</span>
            <TermBtn variant="ghost" class="link-refresh-btn" @click.prevent="refreshLinkStates">Refresh</TermBtn>
          </summary>
          <p class="link-hint">
            Auto mark linked after payment succeeds; next GoPay flow will pre-check and reject.
            <em>Local unlink ≠ Midtrans server unlink</em> — 429 is Midtrans' own rate-limit/cooldown, this panel can't fix it.
          </p>
          <div v-if="linkStateError" class="link-error">{{ linkStateError }}</div>
          <table v-if="linkStates.length" class="link-table">
            <thead>
              <tr><th>Phone</th><th>Status</th><th>linked_at</th><th>Changed By</th><th></th></tr>
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
          <div v-else class="link-empty-inline">No records yet</div>
          <div class="link-manual">
            <input
              v-model="linkManualPhone"
              class="link-manual-input"
              placeholder="Manually add phone (with country code, digits only)"
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
        <div class="term-divider inventory-divider" data-tail="──────────">Account Inventory</div>
        <div class="inventory-head">
          <div class="inventory-meta">
            <span class="inventory-label">Last Refreshed</span>
            <span class="inventory-value">{{ inventoryUpdatedText }}</span>
          </div>
          <div class="inventory-head-actions">
            <TermBtn
              v-if="showInventoryContent"
              variant="ghost"
              :loading="inventoryLoading"
              @click="refreshInventory"
            >Refresh Inventory</TermBtn>
            <TermBtn variant="ghost" @click="toggleInventoryContent">
              {{ showInventoryContent ? "Collapse Details" : "Expand Inventory" }}
            </TermBtn>
          </div>
        </div>
        <div v-if="!showInventoryContent" class="inventory-lazy">
          Account inventory not loaded by default. Auto-expands when <code>pay-only</code> / <code>free_register</code> /
          <code>free_backfill_rt</code> selected; also manually expand.
        </div>

        <template v-else>
          <div v-if="inventoryError" class="inventory-error">
            Inventory refresh failed: {{ inventoryError }}. If code just updated, restart backend with <code>python -m webui.server</code>.
          </div>

          <div class="inventory-stats">
            <div class="inventory-stat">
              <span class="inventory-stat-label">Total Accounts</span>
              <strong>{{ inventory.counts.registered_total }}</strong>
            </div>
            <div v-if="showPayInventoryFields" class="inventory-stat">
              <span class="inventory-stat-label">Reusable</span>
              <strong>{{ inventory.counts.pay_only_eligible }}</strong>
            </div>
            <div v-if="showPayInventoryFields" class="inventory-stat">
              <span class="inventory-stat-label">Consumed</span>
              <strong>{{ inventory.counts.pay_only_consumed }}</strong>
            </div>
            <div v-if="showPayInventoryFields" class="inventory-stat">
              <span class="inventory-stat-label">Missing Auth</span>
              <strong>{{ inventory.counts.pay_only_no_auth }}</strong>
            </div>
            <div v-if="showRtInventoryFields" class="inventory-stat">
              <span class="inventory-stat-label">Has RT</span>
              <strong>{{ inventory.counts.with_refresh_token }}</strong>
            </div>
            <div v-if="showRtInventoryFields" class="inventory-stat">
              <span class="inventory-stat-label">RT Missing</span>
              <strong>{{ inventory.counts.rt_missing }}</strong>
            </div>
            <div v-if="showRtInventoryFields" class="inventory-stat">
              <span class="inventory-stat-label">RT Cooldown</span>
              <strong>{{ inventory.counts.rt_cooldown }}</strong>
            </div>
          </div>

          <div v-if="inventory.accounts.length" class="inventory-filters">
            <input
              v-model="invFilters.search"
              class="inv-filter-input"
              placeholder="🔍 Email keyword"
            />
            <select v-model="invFilters.plan" class="inv-filter-sel">
              <option value="">All Plans</option>
              <option value="plus">plus</option>
              <option value="team">team</option>
              <option value="pro">pro</option>
              <option value="free">free</option>
            </select>
            <select v-model="invFilters.check" class="inv-filter-sel">
              <option value="">All Checks</option>
              <option value="valid">✓ Valid</option>
              <option value="invalid">✗ Invalid</option>
              <option value="unknown">? Unknown</option>
              <option value="unchecked">Unchecked</option>
            </select>
            <select v-if="showPayInventoryFields" v-model="invFilters.pay" class="inv-filter-sel">
              <option value="">All Payment</option>
              <option value="reusable">Reusable</option>
              <option value="consumed">Consumed</option>
              <option value="no_auth">Missing Auth</option>
            </select>
            <select v-if="showRtInventoryFields" v-model="invFilters.rt" class="inv-filter-sel">
              <option value="">All RT</option>
              <option value="has_rt">Has RT</option>
              <option value="oauth_succeeded">OAuth Succeeded</option>
              <option value="missing">Missing</option>
              <option value="cooldown">Cooldown</option>
              <option value="retryable">Retryable</option>
              <option value="dead">Permanently Failed</option>
            </select>
            <select v-if="showCpaInventoryActions" v-model="invFilters.cpa" class="inv-filter-sel">
              <option value="">All CPA</option>
              <option value="pushed">Pushed</option>
              <option value="not_pushed">Not Pushed</option>
            </select>
            <span class="inv-filter-count">{{ filteredAccounts.length }} / {{ inventory.accounts.length }}</span>
            <TermBtn variant="ghost" :disabled="!hasActiveFilter" @click="resetInvFilters">Clear Filters</TermBtn>
          </div>

          <div v-if="inventory.accounts.length" class="inventory-toolbar">
            <label class="inventory-toolbar-check">
              <input type="checkbox" :checked="allFilteredSelected" @change="toggleSelectAllFiltered" />
              <span>Select All Filtered ({{ selectedFilteredCount }} / {{ filteredAccounts.length }})</span>
            </label>
            <!-- Top bar: bulk table-wide operations (independent of selection). Single-account ops in card buttons -->
            <div class="inventory-toolbar-actions">
              <TermBtn v-if="unknownOrUncheckedIds.length" variant="ghost" :loading="inventoryBusy" @click="verifyAllUnknown">Verify All Unchecked ({{ unknownOrUncheckedIds.length }})</TermBtn>
              <TermBtn v-if="planRefreshableIds.length" variant="ghost" :loading="inventoryBusy" @click="refreshPlanAll">Realtime Refresh Plan ({{ planRefreshableIds.length }})</TermBtn>
              <TermBtn v-if="rtRefreshableIds.length" variant="ghost" :loading="inventoryBusy" @click="refreshRtStatusAll">Refresh RT All ({{ rtRefreshableIds.length }})</TermBtn>
              <TermBtn v-if="showCpaInventoryActions && unpushedIds.length" variant="ghost" :loading="inventoryBusy" @click="pushAllUnpushed">Push All Unpushed → CPA ({{ unpushedIds.length }})</TermBtn>
              <TermBtn v-if="unpushedIds.length" variant="ghost" :loading="inventoryBusy" @click="pushAllUnpushedToAutofill">Push All Unpushed → Autofill Panel ({{ unpushedIds.length }})</TermBtn>
              <TermBtn v-if="invalidIds.length" variant="ghost" :loading="inventoryBusy" @click="deleteAllInvalid">Delete All Invalid ({{ invalidIds.length }})</TermBtn>
            </div>
          </div>
          <!-- Multi-select floating: batch ops on "selected" (separate from top bar, no mixing) -->
          <div v-if="hasSelection" class="inventory-multi-actions">
            <span class="inventory-multi-label">Selected {{ selectedIds.size }} · Bulk Actions:</span>
            <TermBtn variant="ghost" :loading="inventoryBusy" @click="verifySelected">Verify</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" @click="refreshRtStatusSelected">Refresh RT</TermBtn>
            <TermBtn v-if="showCpaInventoryActions" variant="ghost" :loading="inventoryBusy" @click="pushSelectedToCpa">Push → CPA</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" @click="pushSelectedToAutofill">Push → Autofill Panel</TermBtn>
            <TermBtn v-if="showPayOnlyInventoryAction" variant="ghost" :loading="inventoryBusy" @click="payOnlySelected">pay-only</TermBtn>
            <TermBtn v-if="showRtInventoryAction" variant="ghost" :loading="inventoryBusy" @click="rtOnlySelected">Backfill RT</TermBtn>
            <TermBtn variant="ghost" :loading="inventoryBusy" @click="deleteSelected">Delete</TermBtn>
          </div>

          <div v-if="filteredAccounts.length" class="inventory-list">
            <div v-for="acc in filteredAccounts" :key="acc.id || acc.email" class="inventory-row" :class="{ 'inventory-row--selected': isSelected(acc.id) }">
              <div class="inventory-row-top">
                <input type="checkbox" class="inventory-row-check" :checked="isSelected(acc.id)" @change="toggleSelect(acc.id)" />
                <span class="inventory-email">{{ acc.email }}</span>
                <span class="badge" :class="planBadgeClass(acc.plan_tag)">{{ planLabel(acc.plan_tag) }}</span>
                <span class="badge" :class="checkBadgeClass(acc.last_check_status)" :title="acc.last_check_message">
                  <template v-if="checkingIds.has(acc.id)">⟳ Checking</template>
                  <template v-else>{{ checkLabel(acc.last_check_status) }}</template>
                </span>
                <span v-if="showPayInventoryFields" class="badge" :class="payBadgeClass(acc.pay_state)">{{ payStateLabel(acc) }}</span>
                <span v-if="showRtInventoryFields" class="badge" :class="rtBadgeClass(acc.rt_state)">{{ rtStateLabel(acc) }}</span>
                <span v-if="showCpaInventoryActions" class="badge" :class="cpaBadgeClass(acc)" :title="acc.cpa_status">{{ cpaLabel(acc) }}</span>
                <!-- Single-account action button group (right-aligned). Same row as existing "Push → CPA" button -->
                <div class="inventory-row-actions">
                  <button class="inventory-row-action" :disabled="inventoryBusy" @click="verifyOne(acc.id)">Verify</button>
                  <button class="inventory-row-action" :disabled="inventoryBusy" @click="refreshRtStatusOne(acc.id)" :title="acc.has_refresh_token ? 'Use RT for new AT to get JWT plan' : 'No RT, this button returns no_rt'">Refresh RT</button>
                  <button v-if="showCpaInventoryActions && !acc.cpa_pushed" class="inventory-row-action" :disabled="inventoryBusy" @click="pushOneToCpa(acc.id)">Push → CPA</button>
                  <button class="inventory-row-action" :disabled="inventoryBusy" @click="pushOneToAutofill(acc.id)">Push → Autofill</button>
                  <button v-if="showPayOnlyInventoryAction" class="inventory-row-action" :disabled="inventoryBusy" @click="payOnlyOne(acc.id)">pay-only</button>
                  <button v-if="showRtInventoryAction" class="inventory-row-action" :disabled="inventoryBusy" @click="rtOnlyOne(acc.id)">Backfill RT</button>
                  <button class="inventory-row-action inventory-row-action--danger" :disabled="inventoryBusy" @click="deleteOne(acc.id)">Delete</button>
                </div>
              </div>
              <div class="inventory-row-sub">
                <span>Registered {{ formatInventoryTs(acc.registered_at) }}</span>
                <span>attempts {{ acc.attempts }}</span>
                <span>auth {{ authSummary(acc) }}</span>
                <span v-if="acc.last_plan_type">rt-plan {{ acc.last_plan_type }}</span>
              </div>
              <div class="inventory-row-detail">
                <span v-if="showPayInventoryFields">payment {{ acc.latest_payment_status || "—" }}</span>
                <span v-if="showPayInventoryFields && acc.latest_payment_source">source {{ acc.latest_payment_source }}</span>
                <span v-if="showPayInventoryFields && acc.latest_payment_error">error {{ acc.latest_payment_error }}</span>
                <span v-if="showRtInventoryFields && acc.oauth_status">oauth {{ acc.oauth_status }}<template v-if="acc.oauth_fail_reason"> ({{ acc.oauth_fail_reason }})</template></span>
                <span v-if="showRtInventoryFields && acc.oauth_cooldown_remaining_s">cooldown {{ formatCooldown(acc.oauth_cooldown_remaining_s) }}</span>
                <span v-if="showPayInventoryFields && acc.latest_payment_is_already_paid" class="inventory-inline-flag">already paid</span>
                <span v-if="showRtInventoryFields && acc.can_backfill_rt" class="inventory-inline-flag">can backfill rt</span>
              </div>
            </div>
          </div>
          <div v-else-if="!inventory.accounts.length" class="inventory-empty">
            No account inventory yet; run register/payment once, wait for DB sync, then refresh.
          </div>
          <div v-else class="inventory-empty">
            All accounts filtered out — clear filters or adjust conditions.
          </div>
        </template>
      </section>

      <Teleport to="body">
        <div v-if="otpDialog.open" class="otp-overlay" @click.self="() => {}">
          <div class="otp-modal">
            <button
              class="otp-close"
              :disabled="otpDialog.submitting"
              title="Close (gopay.py auto-fetches from API / want to manually skip)"
              @click="dismissOtpModal"
            >×</button>
            <div class="otp-head">
              <span class="otp-prompt">$</span> GoPay WhatsApp OTP
            </div>
            <p class="otp-desc">Check WhatsApp, enter the 6-digit OTP just received. Submit and gopay.py continues automatically.</p>
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
              <TermBtn :loading="otpDialog.submitting" @click="submitOtp">Submit</TermBtn>
            </div>
          </div>
        </div>
      </Teleport>

      <section class="run-logs">
        <div class="logs-head">
          <span class="pre-prompt">$</span> Real-time Logs
          <span class="logs-meta">{{ lines.length }} lines</span>
          <label class="auto-scroll-toggle">
            <input type="checkbox" v-model="autoScroll" />
            <span>Auto-scroll to bottom</span>
          </label>
        </div>
        <div class="logs-stream" ref="streamEl">
          <div v-if="!lines.length" class="logs-empty">
            Waiting to run<span class="term-cursor"></span>
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
  { value: "free_register", label: "free_register — free accounts+rt+CPA" },
  { value: "free_backfill_rt", label: "free_backfill_rt — backfill rt for old accounts" },
  { value: "promo_link", label: "promo_link — capture promo long links to DB" },
  { value: "no_card_plus", label: "no_card_plus — promo+PayPal RPA open Plus for 0 yuan" },
  { value: "no_card_plus_parallel", label: "no_card_plus parallel — N worker concurrent phones+sms" },
];
const paymentModes = new Set(["single", "batch", "self_dealer", "daemon"]);
const promoRegionPresets = [
  { country: "ID", currency: "IDR", label: "ID/IDR" },
  { country: "US", currency: "USD", label: "US/USD" },
  { country: "JP", currency: "JPY", label: "JP/JPY" },
  { country: "GB", currency: "GBP", label: "GB/GBP" },
  { country: "IE", currency: "EUR", label: "IE/EUR" },
  { country: "FR", currency: "EUR", label: "FR/EUR" },
  { country: "DE", currency: "EUR", label: "DE/EUR" },
  { country: "CA", currency: "CAD", label: "CA/CAD" },
  { country: "AU", currency: "AUD", label: "AU/AUD" },
  { country: "SG", currency: "SGD", label: "SG/SGD" },
  { country: "HK", currency: "HKD", label: "HK/HKD" },
  { country: "TW", currency: "TWD", label: "TW/TWD" },
  { country: "KR", currency: "KRW", label: "KR/KRW" },
  { country: "IN", currency: "INR", label: "IN/INR" },
];
const promoCurrencyByCountry: Record<string, string> = Object.fromEntries(
  promoRegionPresets.map((x) => [x.country, x.currency])
);

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
  qris?: {
    png_path?: string;
    qr_image_url?: string;
    deeplink_url?: string;
    reference?: string;
    expiry?: string;
    settled?: boolean;
    ready_at?: number;
  };
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
  last_plan_type: "free" | "plus" | "team" | "pro" | string;
  plan_source: "rt" | "payment" | "derived" | string;
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
  qris: false,
  pay_only: false,
  register_only: false,
  batch: 5,
  workers: 3,
  self_dealer: 4,
  count: 0, // free_register mode: register how many then stop (0 = unlimited)
  promo_plan: (localStorage.getItem("webui.promo_plan") || "plus") as "plus" | "team",
  promo_country: (localStorage.getItem("webui.promo_country") || "ID").toUpperCase(),
  promo_currency: (localStorage.getItem("webui.promo_currency") || "IDR").toUpperCase(),
  promo_campaign_id: localStorage.getItem("webui.promo_campaign_id") || "",
  register_mode: ((localStorage.getItem("webui.register_mode") as "browser" | "protocol") || "protocol"),
  // Email source (mutually exclusive), default outlook
  mail_source: (localStorage.getItem("webui.mail_source") || "outlook") as "outlook" | "catch_all",
  outlook_email: "",  // only effective when mail_source=outlook, empty = random from pool
  // no_card_plus mode (scripts/no_card_paypal_plus.py): promo+PayPal RPA open plus for 0
  no_card_promo_link_id: 0, // 0 = auto pick latest fresh plus link
  no_card_phone: localStorage.getItem("webui.no_card_phone") || "",
  no_card_sms_api_url: localStorage.getItem("webui.no_card_sms_api_url") || "",
  no_card_otp_timeout: 240,
  no_card_signup_retries: 3,
  no_card_node_rpa_timeout: 900,
  no_card_max_due: 100,
  no_card_allow_already_paid: false,
  no_card_allow_full_price: false,
  no_card_paypal_country: "US",
  no_card_paypal_lang: "en",
  no_card_inventory_mail_source:
    (localStorage.getItem("webui.no_card_inventory_mail_source") as "any" | "outlook" | "catch_all") || "any",
});

watch(() => form.value.no_card_inventory_mail_source, (v) => {
  try { localStorage.setItem("webui.no_card_inventory_mail_source", v || "any"); } catch {}
});

// Parallel mode (no_card_plus_parallel) independent state, don't pollute form
type ParallelWorker = { phone: string; sms_url: string; tag: string };
type ParallelSummary = {
  total_workers: number;
  running: number;
  succeeded: number;
  failed: number;
  workers: Array<{
    worker_id: string;
    tag: string;
    phone: string;
    sms_url_redacted: string;
    running: boolean;
    exit_code: number | null;
    current_event: string;
  }>;
};
// Parallel mode worker list only loads user-filled from localStorage;
// no pre-fill of sensitive data (phone/SMS key all manual user input, prevent leak).
const _loadParallelWorkers = (): ParallelWorker[] => {
  try {
    const raw = localStorage.getItem("webui.parallel_workers");
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length > 0) {
        return arr.map((w: any) => ({
          phone: String(w?.phone || ""),
          sms_url: String(w?.sms_url || ""),
          tag: String(w?.tag || ""),
        }));
      }
    }
  } catch {}
  return [{ phone: "", sms_url: "", tag: "" }];
};
const parallel = ref({
  concurrency: Number(localStorage.getItem("webui.parallel_concurrency") || "2") || 2,
  default_sms_url: localStorage.getItem("webui.parallel_default_sms_url") || "",
  otp_timeout: 240,
  signup_retries: 3,
  node_rpa_timeout: 900,
  max_due: 100,
  stagger_s: 1.0,
  allow_already_paid: false,
  allow_full_price: false,
  inventory_mail_source: (localStorage.getItem("webui.parallel_inventory_mail_source") as "any" | "outlook" | "catch_all") || "any",
  workers: _loadParallelWorkers() as ParallelWorker[],
  busy: false,
  statusBusy: false,
  summary: null as ParallelSummary | null,
  pollHandle: 0 as number | undefined as any,
  // Each worker's stdout increment cursor; new lines appended to global lines (reuse single run realtime log area).
  workerLogSeq: {} as Record<string, number>,
});

const parallelRunning = computed(() => (parallel.value.summary?.running || 0) > 0);

// N worker → phone pool round-robin mapping. When same phone shared by multiple workers,
// phone-lock queues at OTP stage.
const parallelMapping = computed(() => {
  const pool = parallel.value.workers
    .map((w) => ({ phone: (w.phone || "").trim(), sms_url: (w.sms_url || "").trim(), tag: (w.tag || "").trim() }))
    .filter((w) => w.phone);
  if (pool.length === 0) return [] as Array<{ worker_id: string; phone: string; sms_url: string; tag: string }>;
  const n = Math.max(1, Math.floor(parallel.value.concurrency || 1));
  const out: Array<{ worker_id: string; phone: string; sms_url: string; tag: string }> = [];
  for (let i = 0; i < n; i++) {
    const slot = pool[i % pool.length];
    out.push({
      worker_id: `w${i + 1}`,
      phone: slot.phone,
      sms_url: slot.sms_url || parallel.value.default_sms_url,
      tag: slot.tag || `slot${(i % pool.length) + 1}`,
    });
  }
  return out;
});

watch(() => parallel.value.workers, (v) => {
  try { localStorage.setItem("webui.parallel_workers", JSON.stringify(v)); } catch {}
}, { deep: true });
watch(() => parallel.value.concurrency, (v) => {
  try { localStorage.setItem("webui.parallel_concurrency", String(v || 1)); } catch {}
});
watch(() => parallel.value.default_sms_url, (v) => {
  try { localStorage.setItem("webui.parallel_default_sms_url", v || ""); } catch {}
});
watch(() => parallel.value.inventory_mail_source, (v) => {
  try { localStorage.setItem("webui.parallel_inventory_mail_source", v || "any"); } catch {}
});

function addParallelWorker() {
  parallel.value.workers.push({ phone: "", sms_url: "", tag: "" });
}
function removeParallelWorker(idx: number) {
  if (parallel.value.workers.length <= 1) return;
  parallel.value.workers.splice(idx, 1);
}
async function startParallel() {
  if (parallel.value.busy) return;
  const mapping = parallelMapping.value;
  if (mapping.length === 0) {
    message.error("Need at least 1 phone row (non-empty phone)");
    return;
  }
  if (!parallel.value.default_sms_url && mapping.some((m) => !m.sms_url)) {
    message.error("When some slot sms_url empty, default SMS URL required");
    return;
  }
  // Backend gets mapped worker list (N rows), each with phone+sms_url+worker_id hint;
  // backend spawns equal workers in this order.
  const ws = mapping.map((m) => ({
    phone: m.phone,
    sms_url: m.sms_url,
    tag: m.tag,
    worker_id: m.worker_id,
  }));
  parallel.value.busy = true;
  try {
    const body = {
      workers: ws,
      default_sms_url: parallel.value.default_sms_url,
      otp_timeout: parallel.value.otp_timeout,
      signup_retries: parallel.value.signup_retries,
      node_rpa_timeout: parallel.value.node_rpa_timeout,
      max_due: parallel.value.max_due,
      stagger_s: parallel.value.stagger_s,
      allow_already_paid: parallel.value.allow_already_paid,
      allow_full_price: parallel.value.allow_full_price,
      inventory_mail_source: parallel.value.inventory_mail_source,
    };
    const r = await api.post("/run/parallel/start", body);
    message.success(`Started ${r.data?.spawned?.length || 0} workers`);
    // Reuse global realtime log area: clear old lines, reset each worker seq cursor,
    // let polling pull from start
    lines.value = [];
    parallel.value.workerLogSeq = {};
    startParallelPolling();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || "Parallel start failed");
  } finally {
    parallel.value.busy = false;
  }
}
async function stopParallel() {
  try {
    const r = await api.post("/run/parallel/stop");
    message.success(`Stopped ${r.data?.stopped?.length || 0} workers`);
    refreshParallelStatus();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Stop failed");
  }
}
async function clearParallel() {
  try {
    const r = await api.post("/run/parallel/clear");
    message.success(`Cleared ${r.data?.removed?.length || 0} completed workers`);
    refreshParallelStatus();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Clear failed");
  }
}
async function refreshParallelStatus() {
  if (parallel.value.statusBusy) return;
  parallel.value.statusBusy = true;
  try {
    const r = await api.get("/run/parallel/status");
    parallel.value.summary = r.data as ParallelSummary;
    // Parallel fetch each worker's log increments; new lines append to global lines.
    await fetchAllWorkerLogs();
  } catch (e: any) {
    // Silent; frequent polling shouldn't error
  } finally {
    parallel.value.statusBusy = false;
  }
}

// Global lines.value seq namespace; give parallel worker merged logs safe offset,
// no conflict with single run SSE seq.
let _parallelSeqCursor = 1_000_000;
async function fetchAllWorkerLogs() {
  const ids = (parallel.value.summary?.workers || []).map((w) => w.worker_id);
  if (ids.length === 0) return;
  const allNew: Array<{ wid: string; seq: number; line: string }> = [];
  await Promise.all(ids.map(async (wid) => {
    try {
      const since = parallel.value.workerLogSeq[wid] || 0;
      const r = await api.get("/run/parallel/logs", { params: { worker_id: wid, since } });
      const data = r.data || {};
      const arr: Array<{ seq: number; line: string }> = data.lines || [];
      if (arr.length === 0) return;
      for (const ln of arr) {
        allNew.push({ wid, seq: ln.seq, line: ln.line });
      }
      parallel.value.workerLogSeq[wid] = data.next_seq || since;
    } catch {
      // Silent
    }
  }));
  if (allNew.length === 0) return;
  // Sort by (worker seq) ascending, same seq by worker_id dict order → deterministic ordering
  allNew.sort((a, b) => (a.seq - b.seq) || a.wid.localeCompare(b.wid));
  for (const item of allNew) {
    const entry: any = {
      seq: ++_parallelSeqCursor,
      ts: Date.now() / 1000,
      line: `[${item.wid}] ${item.line}`,
    };
    entry.cls = logClass(entry.line);
    entry.tsLabel = formatTs(entry.ts);
    Object.freeze(entry);
    lines.value.push(entry);
  }
  if (lines.value.length > 1500) lines.value.splice(0, lines.value.length - 1500);
  scheduleScrollToBottom();
}
function startParallelPolling() {
  if (parallel.value.pollHandle) return;
  refreshParallelStatus();
  parallel.value.pollHandle = window.setInterval(() => {
    refreshParallelStatus();
  }, 3000);
}
function stopParallelPolling() {
  if (parallel.value.pollHandle) {
    clearInterval(parallel.value.pollHandle);
    parallel.value.pollHandle = 0;
  }
}
// Switch to parallel mode auto-start polling; switch away auto-stop
watch(() => form.value.mode, (m) => {
  if (m === "no_card_plus_parallel") {
    startParallelPolling();
  } else {
    stopParallelPolling();
  }
}, { immediate: true });

watch(() => form.value.no_card_phone, (v) => {
  try { localStorage.setItem("webui.no_card_phone", v || ""); } catch {}
});
watch(() => form.value.no_card_sms_api_url, (v) => {
  try { localStorage.setItem("webui.no_card_sms_api_url", v || ""); } catch {}
});

watch(() => form.value.register_mode, (v) => {
  if (v !== "protocol" && v !== "browser") {
    form.value.register_mode = "protocol";
    return;
  }
  try { localStorage.setItem("webui.register_mode", v); } catch {}
});
watch(() => form.value.mail_source, (v) => {
  try { localStorage.setItem("webui.mail_source", v); } catch {}
  if (v !== "outlook") form.value.outlook_email = "";
  else reloadOutlookPool();
});
watch(() => form.value.mode, (mode) => {
  if (!paymentModes.has(mode)) {
    form.value.pay_only = false;
    form.value.register_only = false;
  }
});
watch(() => form.value.pay_only, (enabled) => {
  if (enabled) form.value.register_only = false;
});
watch(
  () => [form.value.promo_plan, form.value.promo_country, form.value.promo_currency, form.value.promo_campaign_id],
  () => {
    try {
      localStorage.setItem("webui.promo_plan", form.value.promo_plan);
      localStorage.setItem("webui.promo_country", normalizedPromoCountry.value);
      localStorage.setItem("webui.promo_currency", normalizedPromoCurrency.value);
      localStorage.setItem("webui.promo_campaign_id", form.value.promo_campaign_id || "");
    } catch {}
  }
);

const normalizedPromoCountry = computed(() => (form.value.promo_country || "").trim().toUpperCase());
const normalizedPromoCurrency = computed(() => (form.value.promo_currency || "").trim().toUpperCase());
const promoCountryOk = computed(() => /^[A-Z]{2}$/.test(normalizedPromoCountry.value));
const promoCurrencyOk = computed(() => /^[A-Z]{3}$/.test(normalizedPromoCurrency.value));

function applyPromoRegion(country: string, currency: string) {
  form.value.promo_country = country.toUpperCase();
  form.value.promo_currency = currency.toUpperCase();
}

function normalizePromoRegion() {
  const country = normalizedPromoCountry.value;
  form.value.promo_country = country;
  form.value.promo_currency = normalizedPromoCurrency.value || promoCurrencyByCountry[country] || "";
}

// Outlook pool dropdown data (mail_source=outlook pulls available list)
const outlookAvailable = ref<Array<{ email: string }>>([]);
const outlookLoading = ref(false);
async function reloadOutlookPool() {
  if (outlookLoading.value) return;
  outlookLoading.value = true;
  try {
    const r = await api.get("/outlook/list", { params: { limit: 500, status: "available" } });
    outlookAvailable.value = (r.data?.items || []).map((x: any) => ({ email: x.email }));
    // If selected email not in latest available list, clear it
    if (form.value.outlook_email &&
        !outlookAvailable.value.some(a => a.email === form.value.outlook_email)) {
      form.value.outlook_email = "";
    }
  } catch (e: any) {
    console.warn("[outlook] reload pool failed", e);
  } finally {
    outlookLoading.value = false;
  }
}

const otpDialog = ref({
  open: false,
  value: "",
  submitting: false,
  openedAt: 0,
  autoFilled: false,
  // After user manually × close, don't reopen until SSE `done` event (pipeline end),
  // reset for next run.
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
  if (!linkStateUpdatedAt.value) return "Not refreshed";
  const d = new Date(linkStateUpdatedAt.value);
  return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
});

const linkSummaryText = computed(() => {
  if (!linkStates.value.length) return "No records";
  const linked = linkStates.value.filter(r => r.linked).length;
  const unlinked = linkStates.value.length - linked;
  if (linked === 0) return `${unlinked} unlinked`;
  return `${linked} linked / ${unlinked} unlinked`;
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
    proxyError.value = e?.response?.data?.detail || "Cannot read current exit IP (webshare not enabled?)";
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
      return `Stopped (${autoLoop.value.stop_reason})`;
    }
    return "Not running";
  }
  return `running · ${autoLoop.value.success_count}/${autoLoop.value.target_success} success · ${autoLoop.value.fail_count} failed`;
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
      register_mode: form.value.register_mode,
    });
    message.success("Auto Loop started");
    await refreshAutoLoop();
    // No active SSE, open one so user sees subprocess logs + [auto-loop] marker
    if (!eventSource) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Start failed");
  } finally {
    autoLoopBusy.value = false;
  }
}

async function stopAutoLoop() {
  autoLoopBusy.value = true;
  try {
    await api.post("/auto-loop/stop");
    message.success("Stop signal sent");
    await refreshAutoLoop();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Stop failed");
  } finally {
    autoLoopBusy.value = false;
  }
}

async function rotateProxyIp() {
  if (!confirm("Confirm IP rotation? Will consume Webshare monthly rotation quota.")) return;
  rotatingIp.value = true;
  proxyError.value = "";
  try {
    const r = await api.post<{ prev_ip: string; new_ip: string; country: string }>("/proxy/rotate-ip");
    proxyIp.value = r.data.new_ip || "";
    proxyCountry.value = r.data.country || "";
    message.success(`IP rotated: ${r.data.prev_ip || "?"} → ${r.data.new_ip}`);
  } catch (e: any) {
    proxyError.value = e?.response?.data?.detail || "Rotation failed";
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
    linkStateError.value = e?.response?.data?.detail || e?.message || "Fetch failed";
  }
}

async function setLinkState(phone: string, linked: boolean) {
  const digits = (phone || "").replace(/\D/g, "");
  if (!digits) {
    message.warning("Phone empty");
    return;
  }
  linkBusy.value = digits;
  try {
    await api.post("/gopay/link-state/set", {
      phone: digits,
      linked,
      source: "webui_manual",
    });
    message.success(linked ? `Marked ${digits} as linked` : `Unlinked ${digits}`);
    if (linkManualPhone.value.replace(/\D/g, "") === digits) {
      linkManualPhone.value = "";
    }
    await refreshLinkStates();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Update failed");
  } finally {
    linkBusy.value = "";
  }
}

function onGoPayToggle(v: boolean) {
  if (v) {
    form.value.paypal = false;
    form.value.qris = false;
  }
}

function onQrisToggle(v: boolean) {
  if (v) {
    form.value.paypal = false;
    form.value.gopay = false;
  }
}

// QRIS PNG via nginx /webui/api/... (strip prefix hits /api/run/qris/qr.png)
// Add ready_at cache-bust so browser fetches new QR, not old cache
const qrPngUrl = computed(() => {
  const ra = status.value.qris?.ready_at || 0;
  return `${import.meta.env.BASE_URL}api/run/qris/qr.png?t=${ra}`;
});

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

const inventoryExpanded = ref(false);

const isFreeRegisterMode = computed(() => form.value.mode === "free_register");
const isFreeBackfillMode = computed(() => form.value.mode === "free_backfill_rt");
const isNoCardPlusMode = computed(() => form.value.mode === "no_card_plus");
const modeSupportsPayment = computed(() => paymentModes.has(form.value.mode));
const showRunModifiers = computed(() => modeSupportsPayment.value);
const showPaymentSelector = computed(() => modeSupportsPayment.value && !form.value.register_only);
// no_card_plus uses promo_link registered accounts + PayPal RPA, not ChatGPT register steps
const requiresRegistration = computed(() => !form.value.pay_only && !isFreeBackfillMode.value && !isNoCardPlusMode.value);
const showRegisterPath = computed(() => requiresRegistration.value);
const showMailSource = computed(() => requiresRegistration.value);
const showOutlookSelector = computed(() => showMailSource.value && form.value.mail_source === "outlook");
const showCatchAllHint = computed(() => showMailSource.value && form.value.mail_source === "catch_all");
const showQrisPanel = computed(() => !!(form.value.qris && status.value.qris?.reference));
const showAutoLoopTools = computed(() =>
  form.value.gopay || form.value.mode === "daemon" || autoLoop.value.running
);
const showProxyTools = computed(() =>
  form.value.gopay || form.value.qris || autoLoop.value.running
);
const showGopayLinkTools = computed(() =>
  form.value.gopay || autoLoop.value.running
);
const inventoryAutoRelevant = computed(() =>
  form.value.pay_only || isFreeRegisterMode.value || isFreeBackfillMode.value
);
const showInventoryContent = computed(() => inventoryExpanded.value);
const showPayInventoryFields = computed(() => modeSupportsPayment.value || form.value.pay_only);
const showRtInventoryFields = computed(() => isFreeRegisterMode.value || isFreeBackfillMode.value);
const showCpaInventoryActions = computed(() => isFreeRegisterMode.value || isFreeBackfillMode.value);
const showPayOnlyInventoryAction = computed(() =>
  showPayInventoryFields.value && !form.value.register_only && !status.value.running
);
const showRtInventoryAction = computed(() =>
  showRtInventoryFields.value && !status.value.running
);
const hasSelection = computed(() => selectedIds.value.size > 0);

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
    return `Elapsed ${formatElapsed(elapsed)}`;
  }
  return "";
});

const inventoryUpdatedText = computed(() =>
  inventory.value.generated_at ? formatInventoryTs(inventory.value.generated_at) : "Not refreshed"
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
  if (acc.pay_state === "reusable") return "Reusable";
  if (acc.pay_state === "consumed") return "Consumed";
  return "Missing Auth";
}

function rtStateLabel(acc: InventoryAccount) {
  switch (acc.rt_state) {
    case "has_rt":
      return "Has RT";
    case "oauth_succeeded":
      return "RT Processed";
    case "dead":
      return "dead";
    case "cooldown":
      return "RT Cooldown";
    case "retryable":
      return "Retryable";
    default:
      return "RT Missing";
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
  if (!detail) return payload?.message || "Config health check failed";
  if (typeof detail === "string") return detail;
  return detail.message || "Config health check did not pass";
}

function logClass(line: string) {
  if (/\b(ERROR|FAIL|TRACE|Traceback)\b/i.test(line)) return "log-err";
  if (/\b(WARN|WARNING)\b/i.test(line)) return "log-warn";
  if (/\b(OK|SUCCESS|✓|Complete|Success)\b/i.test(line)) return "log-ok";
  return "";
}

// Scroll-to-bottom deduped by RAF: frequent log push only scrolls once per frame,
// not every nextTick
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

// ── Inventory: select + verify + delete ─────────────────────────────────
function checkLabel(s: InventoryAccount["last_check_status"]) {
  if (s === "valid") return "✓ Valid";
  if (s === "invalid") return "✗ Invalid";
  if (s === "unknown") return "？Unknown";
  return "○ Unchecked";
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

// ── Account inventory filtering ───────────────────────────────────────────────
const invFilters = ref({
  search: "",
  plan: "",
  check: "",
  pay: "",
  rt: "",
  cpa: "",
});

const effectiveInvFilters = computed(() => ({
  search: invFilters.value.search,
  plan: invFilters.value.plan,
  check: invFilters.value.check,
  pay: showPayInventoryFields.value ? invFilters.value.pay : "",
  rt: showRtInventoryFields.value ? invFilters.value.rt : "",
  cpa: showCpaInventoryActions.value ? invFilters.value.cpa : "",
}));

const hasActiveFilter = computed(() =>
  Object.values(effectiveInvFilters.value).some(v => v !== "")
);

const filteredAccounts = computed<InventoryAccount[]>(() => {
  const f = effectiveInvFilters.value;
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
    // Deselect: remove filtered result ids from selection
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
const rtRefreshableIds = computed(() =>
  inventory.value.accounts.filter(a => a.has_refresh_token).map(a => a.id)
);
const planRefreshableIds = computed(() =>
  inventory.value.accounts.filter(a => a.has_access_token).map(a => a.id)
);

async function runCheck(ids: number[], label: string) {
  if (!ids.length) { message.warning(`No accounts to ${label}`); return; }
  inventoryBusy.value = true;
  ids.forEach(id => checkingIds.value.add(id));
  try {
    const r = await api.post("/inventory/accounts/check", { ids });
    const s = r.data?.summary || {};
    message.success(
      `${label} complete: valid=${s.valid || 0}  invalid=${s.invalid || 0}  unknown=${s.unknown || 0}` +
      `  |  plus=${s.plus || 0}  team=${s.team || 0}  pro=${s.pro || 0}  free=${s.free || 0}`
    );
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label} failed: ${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    ids.forEach(id => checkingIds.value.delete(id));
    inventoryBusy.value = false;
  }
}
function verifySelected() {
  runCheck(Array.from(selectedIds.value), "Verify selected");
}
function verifyAllUnknown() {
  runCheck(unknownOrUncheckedIds.value, "Verify unchecked/unknown");
}
function refreshPlanAll() {
  runCheck(planRefreshableIds.value, "Realtime API refresh all Plans");
}

async function runRtStatusRefresh(ids: number[], label: string) {
  if (!ids.length) { message.warning(`No accounts to ${label}`); return; }
  inventoryBusy.value = true;
  ids.forEach(id => checkingIds.value.add(id));
  try {
    const r = await api.post("/inventory/accounts/refresh-rt-status", {
      ids,
      timeout_s: 15,
      max_workers: 3,
    });
    const s = r.data?.summary || {};
    message.success(
      `${label} complete: plus=${s.plus || 0}  team=${s.team || 0}  pro=${s.pro || 0}  free=${s.free || 0}  invalid=${s.invalid || 0}  no_rt=${s.no_rt || 0}`
    );
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label} failed: ${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    ids.forEach(id => checkingIds.value.delete(id));
    inventoryBusy.value = false;
  }
}
function refreshRtStatusSelected() {
  runRtStatusRefresh(Array.from(selectedIds.value), "RT refresh selected status");
}
function refreshRtStatusAll() {
  runRtStatusRefresh(rtRefreshableIds.value, "RT refresh all with RT");
}

// Single account quick action: wrap into list reusing bulk entry, label has email for readability
function _emailOf(id: number): string {
  const acc = inventory.value.accounts.find(a => a.id === id);
  return acc?.email || `id=${id}`;
}
function verifyOne(id: number) {
  runCheck([id], `Verify ${_emailOf(id)}`);
}
function refreshRtStatusOne(id: number) {
  runRtStatusRefresh([id], `RT refresh ${_emailOf(id)}`);
}
function pushOneToAutofill(id: number) {
  pushCpaAutofill([id], `Push ${_emailOf(id)} → Autofill Panel`);
}
async function payOnlyOne(id: number) {
  const email = _emailOf(id);
  if (!email || email.startsWith("id=")) { message.warning("Cannot find this account email"); return; }
  if (!confirm(`Run pay-only for ${email}?\n\nMode: ${form.value.gopay ? "GoPay" : (form.value.paypal ? "PayPal" : "Card")}`)) return;
  starting.value = true;
  try {
    await api.post("/run/start", {
      mode: "single",
      paypal: form.value.paypal,
      gopay: form.value.gopay,
      pay_only: true,
      register_only: false,
      register_mode: form.value.register_mode,
      target_emails: [email],
    });
    message.success(`Started pay-only for ${email}`);
    await refreshStatus();
    if (status.value.running) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Start failed");
  } finally {
    starting.value = false;
  }
}
async function rtOnlyOne(id: number) {
  const email = _emailOf(id);
  if (!email || email.startsWith("id=")) { message.warning("Cannot find this account email"); return; }
  if (!confirm(`Run rt-only for ${email} (backfill refresh_token, no payment)？`)) return;
  starting.value = true;
  try {
    await api.post("/run/start", {
      mode: "single",
      paypal: false,
      gopay: false,
      pay_only: false,
      register_only: false,
      rt_only: true,
      register_mode: form.value.register_mode,
      target_emails: [email],
    });
    message.success(`Started rt-only for ${email}`);
    await refreshStatus();
    if (status.value.running) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Start failed");
  } finally {
    starting.value = false;
  }
}
function deleteOne(id: number) {
  confirmAndDelete([id], `Delete ${_emailOf(id)}`);
}

function emailsForIds(ids: number[]): string[] {
  const map = new Map(inventory.value.accounts.map(a => [a.id, a.email]));
  return ids.map(i => map.get(i) || `id=${i}`);
}
function confirmAndDelete(ids: number[], label: string) {
  if (!ids.length) { message.warning(`No accounts to ${label}`); return; }
  const emails = emailsForIds(ids);
  const preview = emails.slice(0, 5).join(", ") + (emails.length > 5 ? `, ... ${emails.length} total` : "");
  dialog.warning({
    title: `Confirm ${label}?`,
    content: () => h("div", { style: "font-size:12px; line-height:1.6" }, [
      h("div", `Will permanently delete ${ids.length} accounts (pipeline_results / card_results audit rows kept).`),
      h("div", { style: "margin-top:6px; color:#7a7363; word-break:break-all" }, preview),
    ]),
    positiveText: "Confirm Delete",
    negativeText: "Cancel",
    onPositiveClick: async () => {
      inventoryBusy.value = true;
      try {
        const r = await api.post("/inventory/accounts/delete", { ids });
        message.success(`Deleted ${r.data?.deleted ?? 0} accounts`);
        selectedIds.value = new Set();
        await refreshInventory();
      } catch (e: any) {
        message.error(`Delete failed: ${e?.response?.data?.detail || e?.message || e}`);
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
  if (!emails.length) { message.warning("No accounts selected"); return; }
  const preview = emails.slice(0, 3).join(", ") + (emails.length > 3 ? `... ${emails.length} total` : "");
  if (!confirm(`Run pay-only for ${emails.length} selected accounts?\n${preview}\n\nMode: ${form.value.gopay ? "GoPay" : (form.value.paypal ? "PayPal" : "Card")}`)) return;
  starting.value = true;
  try {
    await api.post("/run/start", {
      mode: "single",
      paypal: form.value.paypal,
      gopay: form.value.gopay,
      pay_only: true,
      register_only: false,
      register_mode: form.value.register_mode,
      target_emails: emails,
    });
    message.success(`Started pay-only for ${emails.length} accounts`);
    await refreshStatus();
    if (status.value.running) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Start failed");
  } finally {
    starting.value = false;
  }
}

async function rtOnlySelected() {
  const emails = _selectedEmails();
  if (!emails.length) { message.warning("No accounts selected"); return; }
  const preview = emails.slice(0, 3).join(", ") + (emails.length > 3 ? `... ${emails.length} total` : "");
  if (!confirm(`Run rt-only for ${emails.length} selected accounts (backfill refresh_token only, no payment)?\n${preview}`)) return;
  starting.value = true;
  try {
    await api.post("/run/start", {
      mode: "single",
      paypal: false,
      gopay: false,
      pay_only: false,
      register_only: false,
      rt_only: true,
      register_mode: form.value.register_mode,
      target_emails: emails,
    });
    message.success(`Started rt-only for ${emails.length} accounts`);
    await refreshStatus();
    if (status.value.running) openStream();
  } catch (e: any) {
    message.error(e?.response?.data?.detail || "Start failed");
  } finally {
    starting.value = false;
  }
}

function deleteSelected() {
  confirmAndDelete(Array.from(selectedIds.value), "Delete selected");
}
function deleteAllInvalid() {
  confirmAndDelete(invalidIds.value, "Delete all invalid");
}

// ── plan + CPA push ─────────────────────────────────
function planLabel(p: string) {
  if (p === "team") return "team";
  if (p === "plus") return "plus";
  if (p === "pro") return "pro";
  if (p === "free") return "free";
  return p || "unknown";
}
function planBadgeClass(p: string) {
  if (p === "team") return "badge-team";
  if (p === "plus" || p === "pro") return "badge-plus";
  return "badge-ghost";
}
function cpaLabel(acc: InventoryAccount) {
  if (acc.cpa_pushed) return "✓ Pushed CPA";
  if (acc.cpa_status && acc.cpa_status !== "ok") return `✗ ${acc.cpa_status}`;
  return "○ Not Pushed CPA";
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
  if (!ids.length) { message.warning(`No accounts to ${label}`); return; }
  inventoryBusy.value = true;
  try {
    const r = await api.post("/inventory/accounts/cpa-push", { ids });
    const s = r.data?.summary || {};
    message.success(`${label} complete: ok=${s.ok || 0}  no_rt=${s.no_rt || 0}  fail=${s.fail || 0}`);
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label} failed: ${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    inventoryBusy.value = false;
  }
}
function pushOneToCpa(id: number) { pushCpa([id], "Push CPA"); }
function pushSelectedToCpa() { pushCpa(Array.from(selectedIds.value), "Bulk push selected"); }
function pushAllUnpushed() { pushCpa(unpushedIds.value, "Push all unpushed"); }

async function pushCpaAutofill(ids: number[], label: string) {
  if (!ids.length) { message.warning(`No accounts to ${label}`); return; }
  // Autofill panel does RT-refresh anti-double-spend, once pushed can't reuse locally — price-check before confirm
  const lastPriceRaw = localStorage.getItem("cpa_autofill_last_price") || "1.0";
  const priceInput = window.prompt(
    `${ids.length} accounts to autofill panel\n\nEnter listing price this batch (yuan/account, seller net = this price × quantity):\n` +
    `⚠ Backend immediately refreshes OAuth token; after pushing, local refresh_token for these becomes invalid, cannot reuse locally.`,
    lastPriceRaw,
  );
  if (priceInput === null) return;
  const price = parseFloat(priceInput.trim());
  if (!Number.isFinite(price) || price < 0) {
    message.error("Listing price must be non-negative number");
    return;
  }
  localStorage.setItem("cpa_autofill_last_price", String(price));
  inventoryBusy.value = true;
  try {
    const r = await api.post("/inventory/accounts/cpa-autofill-push", { ids, price });
    const s = r.data?.summary || {};
    const errs = (r.data?.api_errors || []) as string[];
    const lines = [
      `${label} complete (price=${r.data?.price ?? "?"} yuan/account, ${r.data?.batches ?? 0} batches)`,
      `accepted=${s.accepted || 0}  rejected=${s.rejected || 0}  missing_field=${s.missing_field || 0}  api_error=${s.api_error || 0}  missing=${s.missing || 0}`,
    ];
    if (errs.length) lines.push(`API errors: ${errs.slice(0, 2).join(" | ")}`);
    message.success(lines.join("\n"));
    await refreshInventory();
  } catch (e: any) {
    message.error(`${label} failed: ${e?.response?.data?.detail || e?.message || e}`);
  } finally {
    inventoryBusy.value = false;
  }
}
function pushSelectedToAutofill() { pushCpaAutofill(Array.from(selectedIds.value), "Bulk push selected to autofill panel"); }
function pushAllUnpushedToAutofill() { pushCpaAutofill(unpushedIds.value, "Push all unpushed to autofill panel"); }

function toggleInventoryContent() {
  inventoryExpanded.value = !inventoryExpanded.value;
  if (showInventoryContent.value) {
    refreshInventory();
  }
}

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
      : (e?.message || "Request failed");
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
      message.error(first?.message || "Config health check did not pass, startup blocked");
      return;
    }
    await api.post("/run/start", form.value);
    message.success("Started");
    lines.value = [];
    await refreshStatus();
    await refreshInventory();
    openStream();
  } catch (e: any) {
    message.error(healthErrorText(e) || "Start failed");
  } finally {
    starting.value = false;
  }
}

async function stop() {
  stopping.value = true;
  try {
    await api.post("/run/stop");
    message.success("SIGTERM sent");
    await refreshStatus();
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Stop failed");
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
      // Pre-compute render-only fields, avoid per-re-render regex / Date construct,
      // saves main thread time in 5000+ line loops
      entry.cls = logClass(entry.line || "");
      entry.tsLabel = formatTs(entry.ts);
      Object.freeze(entry); // prevent Vue making each line reactive
      lines.value.push(entry);
      if (lines.value.length > 1500) lines.value.splice(0, 500);
      scheduleScrollToBottom();
    } catch {}
  });
  eventSource.addEventListener("otp_pending", () => {
    if (otpDialog.value.dismissed) return; // user manually closed this run's modal
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
    otpDialog.value.dismissed = false; // new run, allow modal again
    stopOtpPolling();
    await refreshStatus();
    await refreshInventory();
    // Auto-loop still running, reconnect SSE, wait for runner.start to spawn next iteration process
    if (autoLoop.value.running) {
      setTimeout(() => {
        if (autoLoop.value.running && !eventSource) openStream();
      }, 1500);
    }
  });
  eventSource.onerror = () => {
    // Connection dropped, no auto-retry
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
    message.warning("Enter OTP");
    return;
  }
  otpDialog.value.submitting = true;
  try {
    await api.post("/run/otp", { otp: v });
    otpDialog.value.open = false;
    otpDialog.value.value = "";
    stopOtpPolling();
    message.success("OTP submitted");
  } catch (e: any) {
    message.error(e.response?.data?.detail || "Submit failed");
  } finally {
    otpDialog.value.submitting = false;
  }
}

function dismissOtpModal() {
  otpDialog.value.open = false;
  otpDialog.value.value = "";
  otpDialog.value.dismissed = true;
  stopOtpPolling();
  message.info("OTP modal closed (won't auto-reopen this run)");
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
        // gopay.py also polling /latest-otp, once OTP in wa_state it's taken,
        // modal /run/otp submit just clears backend _otp_pending flag + log visibility.
        // Regardless of submit result, close modal immediately, avoid residual modal.
        otpDialog.value.open = false;
        otpDialog.value.value = "";
        // Critical: set dismissed=true block SSE otp_pending heartbeat (every 300ms)
        // popping modal back up. `done` event auto-resets for next run.
        otpDialog.value.dismissed = true;
        stopOtpPolling();
        message.success(`OTP auto-filled and closed: ${code} (gopay fetching)`);
        // Background fire-and-forget, don't block / affect modal close
        api.post("/run/otp", { otp: code }).catch(() => {});
      }
    } catch {
      // Silent; retry next cycle
    }
  }, 1500);
}

function stopOtpPolling() {
  if (otpPollTimer) {
    clearInterval(otpPollTimer);
    otpPollTimer = undefined;
  }
}

function setLinkPolling(enabled: boolean) {
  if (enabled) {
    refreshLinkStates();
    if (!linkPollTimer) linkPollTimer = setInterval(refreshLinkStates, 5000);
  } else if (linkPollTimer) {
    clearInterval(linkPollTimer);
    linkPollTimer = undefined;
  }
}

function setAutoLoopPolling(enabled: boolean) {
  if (enabled) {
    refreshAutoLoop();
    if (!autoLoopPollTimer) autoLoopPollTimer = setInterval(refreshAutoLoop, 4000);
  } else if (autoLoopPollTimer) {
    clearInterval(autoLoopPollTimer);
    autoLoopPollTimer = undefined;
  }
}

function setInventoryPolling(enabled: boolean) {
  if (enabled) {
    refreshInventory();
    if (!inventoryTimer) inventoryTimer = setInterval(refreshInventory, 15000);
  } else if (inventoryTimer) {
    clearInterval(inventoryTimer);
    inventoryTimer = undefined;
  }
}

watch(
  () => [
    form.value.mode,
    form.value.paypal,
    form.value.gopay,
    form.value.qris,
    form.value.pay_only,
    form.value.register_only,
    form.value.batch,
    form.value.workers,
    form.value.self_dealer,
    form.value.count,
    form.value.promo_plan,
    form.value.promo_country,
    form.value.promo_currency,
    form.value.promo_campaign_id,
    form.value.register_mode,
    form.value.mail_source,
    form.value.outlook_email,
  ],
  () => {
    configHealth.value = null;
    refreshPreview();
  },
  { immediate: false }
);

watch(showGopayLinkTools, (enabled) => setLinkPolling(enabled));
watch(showAutoLoopTools, (enabled) => setAutoLoopPolling(enabled));
watch(showProxyTools, (enabled) => {
  if (enabled && !proxyIp.value && !proxyError.value) loadCurrentProxy();
});
watch(inventoryAutoRelevant, (enabled) => {
  if (enabled) inventoryExpanded.value = true;
}, { immediate: true });
watch(
  () => showInventoryContent.value || status.value.running,
  (enabled) => setInventoryPolling(enabled)
);

onMounted(async () => {
  tick();
  clockTimer = setInterval(tick, 1000);

  // Infer default payment method from wizard store: card no --paypal, others have it
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
  if (form.value.mail_source === "outlook") {
    reloadOutlookPool();
  }
  if (status.value.running) {
    openStream();
  }
  statusTimer = setInterval(refreshStatus, 5000);
  setInventoryPolling(showInventoryContent.value || status.value.running);
  await refreshAutoLoop();
  setAutoLoopPolling(showAutoLoopTools.value);
  setLinkPolling(showGopayLinkTools.value);
  if (showProxyTools.value) loadCurrentProxy();
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
</script><style scoped>
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
.promo-region-box {
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  padding: 10px 12px;
}
.promo-region-fields {
  margin-top: 10px;
}
.promo-select-wrap {
  display: inline-flex;
  align-items: stretch;
  border: 1px solid var(--border);
  background: var(--bg-base);
}
.promo-select-wrap span {
  display: inline-flex;
  align-items: center;
  padding: 0 10px;
  border-right: 1px solid var(--border);
  background: var(--bg-panel);
  color: var(--fg-tertiary);
  font-size: 11px;
  font-weight: 700;
}
.promo-select {
  border: 0;
  background: transparent;
  color: var(--fg-primary);
  font: inherit;
  padding: 9px 10px;
  min-width: 90px;
}
.promo-select:focus { outline: none; box-shadow: inset 0 0 0 1px var(--accent); }
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
.reg-mode-opt.static { cursor: default; background: var(--bg-panel); }

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

.qris-panel {
  margin-top: 16px;
  padding: 14px 16px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 4px;
}
.qris-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: 1px dashed var(--border);
}
.qris-title { font-size: 14px; font-weight: 600; color: var(--fg-primary); letter-spacing: 0.5px; }
.qris-body { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.qris-img {
  display: block;
  width: 240px;
  height: 240px;
  background: white;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  flex-shrink: 0;
}
.qris-meta { flex: 1; min-width: 280px; font-size: 12px; line-height: 1.6; color: var(--fg-secondary); }
.qris-meta p { margin: 4px 0; }
.qris-meta strong { color: var(--fg-primary); margin-right: 6px; }
.qris-meta code { font-size: 11px; padding: 1px 4px; background: var(--bg); border: 1px solid var(--border); border-radius: 2px; }
.qris-hint { margin-top: 10px !important; padding-top: 8px; border-top: 1px dashed var(--border); font-size: 11px; color: var(--fg-tertiary, var(--fg-secondary)); }
.qris-meta a { color: var(--accent); text-decoration: none; }
.qris-meta a:hover { text-decoration: underline; }
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
.inventory-head-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
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
.inventory-lazy {
  border: 1px dashed var(--border);
  background: var(--bg-panel);
  color: var(--fg-tertiary);
  padding: 14px;
  font-size: 12px;
  line-height: 1.7;
}
.inventory-lazy code {
  background: var(--bg-base);
  border: 1px solid var(--border);
  padding: 1px 5px;
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
.inventory-multi-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 6px 10px;
  margin-top: 4px;
  background: #fff7ec;
  border: 1px dashed var(--accent);
  border-radius: 2px;
  font-size: 12px;
}
.inventory-multi-label {
  margin-right: 4px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  color: var(--accent);
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
.inventory-row-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.inventory-row-action {
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
.inventory-row-action--danger {
  border-color: #c75353;
  color: #c75353;
}
.inventory-row-action--danger:hover:not(:disabled) {
  background: #c75353;
  color: #fff;
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
