<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">Step 10: Subscription Plan</div>
    <h2 class="step-h">$&nbsp;Subscription plan<span class="term-cursor"></span></h2>
    <p class="step-sub">Select Plus or Team; most fields have default values.</p>

    <div class="form-stack">
      <TermChoice
        v-model="form.plan_type"
        :options="[
          { value: 'team', label: 'Team', desc: 'chatgptteamplan · Multi-seat · 1 month free' },
          { value: 'plus', label: 'Plus', desc: 'chatgptplusplan · Single user · 1 month free' },
        ]"
        :cols="2"
      />
      <TermField v-if="form.plan_type === 'team'" v-model="form.workspace_name" label="Workspace Name · workspace_name" />
      <TermField v-if="form.plan_type === 'team'" v-model="form.seat_quantity" label="Seat Quantity · seat_quantity" type="number" />
      <TermChoice
        v-model="form.price_interval"
        :options="[
          { value: 'month', label: 'Monthly', desc: 'Billed monthly' },
          { value: 'year', label: 'Yearly', desc: 'Billed yearly (usually discounted)' },
        ]"
        :cols="2"
      />
      <TermChoice
        v-model="form.checkout_ui_mode"
        :options="[
          { value: 'custom', label: 'Short Link / In-site', desc: 'checkout_ui_mode=custom, outputs chatgpt.com/checkout short entry' },
          { value: 'hosted', label: 'Long Link / Hosted', desc: 'checkout_ui_mode=hosted, prioritizes OpenAI hosted payment long link' },
        ]"
        :cols="2"
      />
      <TermField v-model="form.promo_campaign_id" :label="`Promo Code ID · promo_campaign_id`" :placeholder="defaultPromo" />
      <TermSelect
        v-model="form.billing_region"
        label="Billing Region · billing_region"
        :options="billingRegionOptions"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch, computed } from "vue";
import { useWizardStore } from "../../stores/wizard";
import TermField from "../term/TermField.vue";
import TermChoice from "../term/TermChoice.vue";
import TermSelect from "../term/TermSelect.vue";

const store = useWizardStore();
const init = store.answers.team_plan ?? {};

const billingRegionOptions = [
  { value: "US", label: "USA · US / USD", desc: "billing_country=US, billing_currency=USD" },
  { value: "JP", label: "Japan · JP / JPY", desc: "billing_country=JP, billing_currency=JPY" },
  { value: "GB", label: "UK · GB / GBP", desc: "billing_country=GB, billing_currency=GBP" },
  { value: "SG", label: "Singapore · SG / SGD", desc: "billing_country=SG, billing_currency=SGD" },
  { value: "ID", label: "Indonesia · ID / IDR", desc: "billing_country=ID, billing_currency=IDR (GoPay/Indonesian scenario)" },
];

const billingCurrencyByCountry: Record<string, string> = {
  US: "USD",
  JP: "JPY",
  GB: "GBP",
  SG: "SGD",
  ID: "IDR",
};

function normalizeBillingCountry(value: unknown): string {
  return typeof value === "string" && billingCurrencyByCountry[value] ? value : "US";
}

function inferPlanType(): "team" | "plus" {
  if (init.plan_type === "plus" || init.plan_type === "team") return init.plan_type;
  if (typeof init.plan_name === "string" && init.plan_name.includes("plus")) return "plus";
  return "team";
}

function defaultCouponFromQuery(planType: "team" | "plus"): boolean {
  // Team free trial usually comes from query coupon; Plus's plus-1-month-free uses normal flow
  // promo_campaign marking is sufficient. Does not force override when existing config is kept.
  return planType === "team";
}

const initialBillingCountry = normalizeBillingCountry(init.billing_country);

const form = ref({
  plan_type: inferPlanType(),
  workspace_name: init.workspace_name ?? "MyWorkspace",
  seat_quantity: init.seat_quantity ?? 5,
  price_interval: init.price_interval ?? "month",
  checkout_ui_mode: init.checkout_ui_mode ?? "custom",
  promo_campaign_id: init.promo_campaign_id ?? (inferPlanType() === "plus" ? "plus-1-month-free" : "team-1-month-free"),
  is_coupon_from_query_param: init.is_coupon_from_query_param ?? defaultCouponFromQuery(inferPlanType()),
  billing_region: initialBillingCountry,
  billing_country: initialBillingCountry,
  billing_currency: billingCurrencyByCountry[initialBillingCountry],
});

const defaultPromo = computed(() => (form.value.plan_type === "plus" ? "plus-1-month-free" : "team-1-month-free"));

watch(
  () => form.value.plan_type,
  (next, prev) => {
    if (next === prev) return;
    const oldDefault = prev === "plus" ? "plus-1-month-free" : "team-1-month-free";
    if (!form.value.promo_campaign_id || form.value.promo_campaign_id === oldDefault) {
      form.value.promo_campaign_id = next === "plus" ? "plus-1-month-free" : "team-1-month-free";
    }
    form.value.is_coupon_from_query_param = defaultCouponFromQuery(next);
  },
);

watch(
  () => form.value.billing_region,
  (country) => {
    form.value.billing_country = country;
    form.value.billing_currency = billingCurrencyByCountry[country] ?? "USD";
  },
  { immediate: true },
);

watch(
  form,
  () => {
    const pt = form.value.plan_type;
    const out: Record<string, unknown> = {
      plan_type: pt,
      plan_name: pt === "plus" ? "chatgptplusplan" : "chatgptteamplan",
      entry_point: pt === "plus" ? "all_plans_pricing_modal" : "team_workspace_purchase_modal",
      price_interval: form.value.price_interval,
      promo_campaign_id: form.value.promo_campaign_id,
      is_coupon_from_query_param: form.value.is_coupon_from_query_param,
      checkout_ui_mode: form.value.checkout_ui_mode,
      output_url_mode: form.value.checkout_ui_mode === "hosted" ? "provider" : "canonical",
      billing_country: form.value.billing_country,
      billing_currency: form.value.billing_currency,
    };
    if (pt === "team") {
      out.workspace_name = form.value.workspace_name;
      out.seat_quantity = Number(form.value.seat_quantity) || 5;
    }
    store.setAnswer("team_plan", out);
    store.saveToServer();
  },
  { deep: true, immediate: true },
);
</script>
