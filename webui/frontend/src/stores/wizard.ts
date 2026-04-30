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
  5: ["system", "cloudflare", "imap"],
  6: ["system", "cloudflare", "imap", "proxy"],
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
      // (version 有 fallback，js_checksum/rv_timestamp 仅 inline confirm 用，不走 PayPal 路径)
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
