<!-- ╔══════════════════════════════════════════════╗ -->
<!-- ║             👑:anzi开发by:anzi👑             ║ -->
<!-- ╚══════════════════════════════════════════════╝ -->
<template>
  <n-config-provider :theme="lightTheme" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
      <RouterView />
      <a
        class="repo-link"
        href="https://github.com/DanOps-1/Gpt-Agreement-Payment"
        target="_blank"
        rel="noopener noreferrer"
        title="GitHub Repository"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 .5C5.73.5.5 5.73.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79-.25.79-.56 0-.28-.01-1.02-.02-2-3.2.69-3.87-1.54-3.87-1.54-.52-1.33-1.27-1.69-1.27-1.69-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.02 1.75 2.68 1.24 3.34.95.1-.74.4-1.24.72-1.53-2.55-.29-5.24-1.27-5.24-5.66 0-1.25.45-2.27 1.18-3.07-.12-.29-.51-1.46.11-3.05 0 0 .96-.31 3.15 1.17.91-.25 1.89-.38 2.86-.38.97 0 1.95.13 2.86.38 2.19-1.48 3.15-1.17 3.15-1.17.62 1.59.23 2.76.11 3.05.74.8 1.18 1.82 1.18 3.07 0 4.4-2.69 5.36-5.25 5.65.41.35.78 1.05.78 2.12 0 1.53-.01 2.76-.01 3.13 0 .31.21.67.8.56C20.71 21.39 24 17.08 24 12 24 5.73 18.27.5 12 .5z"/>
        </svg>
        <span>DanOps-1/Gpt-Agreement-Payment</span>
        <span class="repo-link__arrow">↗</span>
      </a>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { NConfigProvider, NMessageProvider, NDialogProvider, lightTheme, type GlobalThemeOverrides } from "naive-ui";

const themeOverrides: GlobalThemeOverrides = {
  common: {
    fontFamily: "JetBrains Mono, ui-monospace, monospace",
    fontFamilyMono: "JetBrains Mono, ui-monospace, monospace",
    primaryColor: "#b25e1f",
    primaryColorHover: "#8a4413",
    primaryColorPressed: "#6b340c",
    primaryColorSuppl: "#b25e1f",
    successColor: "#1f6638",
    warningColor: "#b25e1f",
    errorColor: "#b91c1c",
    bodyColor: "#faf8f3",
    cardColor: "#ffffff",
    modalColor: "#ffffff",
    popoverColor: "#ffffff",
    tableHeaderColor: "#f0ece1",
    textColorBase: "#1c1a15",
    textColor1: "#1c1a15",
    textColor2: "#3a362e",
    textColor3: "#7a7363",
    borderColor: "#d4cdb9",
    dividerColor: "#d4cdb9",
    inputColor: "#ffffff",
    inputColorDisabled: "#f0ece1",
    actionColor: "#f0ece1",
    hoverColor: "#ede8da",
    borderRadius: "0px",
    borderRadiusSmall: "0px",
  },
  Button: { borderRadiusMedium: "0px", borderRadiusSmall: "0px" },
  Card: { borderRadius: "0px" },
  Input: { borderRadius: "0px" },
  Tag: { borderRadiusSmallRound: "0px" },
};
</script>

<style>
.repo-link {
  position: fixed;
  right: 12px;
  bottom: 10px;
  z-index: 9999;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11px;
  line-height: 1;
  color: #3a362e;
  background: rgba(255, 255, 255, 0.85);
  backdrop-filter: blur(4px);
  border: 1px solid #d4cdb9;
  text-decoration: none;
  transition: color .15s, border-color .15s, background .15s;
}
.repo-link:hover {
  color: #b25e1f;
  border-color: #b25e1f;
  background: #ffffff;
}
.repo-link__arrow {
  opacity: .55;
  font-size: 10px;
}
@media (max-width: 640px) {
  .repo-link span:not(.repo-link__arrow) { display: none; }
}
</style>
