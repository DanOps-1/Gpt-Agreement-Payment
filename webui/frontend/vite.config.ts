//╔══════════════════════════════════════════════╗
//║             👑:anzi开发by:anzi👑             ║
//╚══════════════════════════════════════════════╝
import { defineConfig } from "vite"; // 导入 Vite 配置工具
import vue from "@vitejs/plugin-vue"; // 导入 Vue 插件

export default defineConfig({ // 导出 Vite 配置
  base: process.env.WEBUI_BASE ?? "/", // 默认本地根路径，反代时可用环境变量改成 /webui/
  plugins: [vue()], // 启用 Vue 单文件组件
  server: { // 设置开发服务器
    proxy: { // 设置接口代理
      "/api": "http://127.0.0.1:8765", // 把 API 请求转给后端
    }, // 结束代理设置
  }, // 结束开发服务器设置
  build: { // 设置构建输出
    outDir: "dist", // 输出到 dist 文件夹
    emptyOutDir: true, // 构建前清空旧文件
  }, // 结束构建设置
}); // 结束配置导出
