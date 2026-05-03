# 图标资产

[← 回到 README](../../README.md)

## 文件清单

| 文件 | 用途 | 尺寸 |
|---|---|---|
| `logo-light.svg` | 浅色背景下的 logo（README light mode）| viewBox 256×256 |
| `logo-dark.svg` | 深色背景下的 logo（README dark mode）| viewBox 256×256 |
| `icon.svg` | 通用图标，用 `currentColor` 自适应父级 | viewBox 64×64 |
| `social-preview.svg` | GitHub 仓库 Social preview 图 | 1280×640 |

## 设计含义

每个元素都对应项目核心：

```
   ╭──────────╮
   │  ▒    ·  │   ← 外圆 = solver 视场 / 协议作用域
   │   │    │ │
   │   │  ▒ │ │   ← 对角两 tile 高亮 = hCaptcha solver 锁定的目标
   │ → │ ── │ │   ← 横穿箭头 → = 协议重放穿越
   │   │    │ │   ← 竖直 │ = 协议栈分层（client / server）
   ╰──────────╯
```

- 单色，无装饰，几何
- 在 16×16 favicon 尺寸下也能识别
- 没有抄袭任何商业品牌

## 在 README 中使用（仓库 public 之后再加）

> ⚠️ **私有仓库下 GitHub 不能正确渲染相对路径的 SVG。** 浏览器拿到的不是 SVG 而是 GitHub blob 视图 HTML，会显示成奇怪的页面元素（比如你自己的 GitHub 头像）。仓库切 public 之后 raw URL 直接命中 SVG，这个问题就消失了。**所以下面的引用片段请等切 public 之后再加到主 README。**

切到 public 后，logo 居中放在 README 顶部（推荐）：

```html
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.svg">
    <img src="docs/images/logo-light.svg" width="160" alt="Gpt-Agreement-Payment">
  </picture>
</p>
```

或者 GitHub 官方推荐的 fragment hack 写法（双图自动切主题，不依赖 picture）：

```html
<p align="center">
  <img src="docs/images/logo-light.svg#gh-light-mode-only" width="160" alt="Gpt-Agreement-Payment">
  <img src="docs/images/logo-dark.svg#gh-dark-mode-only" width="160" alt="Gpt-Agreement-Payment">
</p>
```

或者标题旁内联小尺寸（不破坏研究项目克制感）：

```html
<h1>
  <img src="docs/images/icon.svg" width="32" valign="middle"> Gpt-Agreement-Payment
</h1>
```

## 设置 GitHub Social preview

1. 在浏览器打开 `social-preview.svg`，截图或导出 PNG（也可以用 `librsvg`：
   ```bash
   rsvg-convert -w 1280 -h 640 docs/images/social-preview.svg -o /tmp/social.png
   ```
2. GitHub 仓库 → Settings → General → Social preview → Upload an image
3. 上传后所有从 Twitter / Slack / RSS 等地方分享出去的链接预览都用这张图

## 修改与重制

SVG 是文本格式，可以直接用任何编辑器改。改完之后：

- 保持单色（不要加渐变 / 滤镜，可能在不同浏览器渲染不一致）
- 保持 viewBox 比例（不要改成长方形，会破坏几何对称）
- 改色时 light 版用 `#1f2328`（GitHub 中性深灰），dark 版用 `#e6edf3`（亮灰），这两个色在两种 GitHub theme 下都自然
- 如果加文字，用 `font-family="ui-monospace, monospace"` 跟项目调性一致
