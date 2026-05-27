import { defineStore } from "pinia";
import { api, type PreflightResult } from "../api/client";

export interface PreflightLogEntry {
  ts: number;
  name: string;
  status: "ok" | "warn" | "fail";
  message: string;
}

const REQUIRED_PREFLIGHT_BY_STEP: Record<number, string[]> = {
  1: [],
  2: ["system"],
  3: ["system"],
  4: ["system", "cloudflare"],
  5: ["system", "cloudflare", "cloudflare_kv"],
  6: ["system", "cloudflare", "cloudflare_kv", "proxy"],
};

export const useWizardStore = defineStore("wizard", {
  state: () => ({
    currentStep: 1 as number,
    answers: {} as Record<string, any>,
    preflight: {} as Record<string, PreflightResult>,
    preflightLog: [] as PreflightLogEntry[],
  }),
  actions: {
    setAnswer(section: string, value: any) {
      this.answers[section] = value;
    },
    setPreflight(name: string, result: PreflightResult) {
      this.preflight[name] = result;
      this.preflightLog.push({
        ts: Date.now(),
        name,
        status: result.status,
        message: result.message,
      });
      if (this.preflightLog.length > 30) this.preflightLog.shift();
    },
    setStep(n: number) {
      this.currentStep = n;
    },
    isStepUnlocked(n: number): boolean {
      const required = REQUIRED_PREFLIGHT_BY_STEP[n] ?? [];
      return required.every((name) => this.preflight[name]?.status === "ok");
    },
    isStepHidden(n: number): boolean {
      const mm = (this.answers.mode as any)?.mode ?? "single";
      // free_register / free_backfill_rt 不走支付，6(PayPal/GoPay)/7(Card)/13(Stripe runtime) 都隐藏
      if (mm === "free_register" || mm === "free_backfill_rt") {
        if (n === 6) return true;
        if (n === 7) return true;
        if (n === 13) return true;
        return false;
      }
      const pm = (this.answers.payment as any)?.method ?? "both";
      if (pm === "gopay") {
        // GoPay 走 step 7；step 6(PayPal) / step 13(stripe runtime) 不走
        if (n === 6) return true;
        if (n === 13) return true;
        return false;
      }
      if (n === 6 && pm === "card") return true;
      if (n === 7 && pm === "paypal") return true;
      // step 13 Stripe runtime: PayPal 走 redirect 路径，三字段都不需要
      if (n === 13 && pm === "paypal") return true;
      return false;
    },
    async loadFromServer() {
      const r = await api.get("/wizard/state");
      this.currentStep = r.data.current_step;
      this.answers = r.data.answers;
    },
    async saveToServer() {
      await api.post("/wizard/state", {
        current_step: this.currentStep,
        answers: this.answers,
      });
    },
    async runPreflight(name: string, body: any) {
      const r = await api.post<PreflightResult>(`/preflight/${name}`, body);
      this.setPreflight(name, r.data);
      return r.data;
    },
  },
});
