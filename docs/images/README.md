# Icon Assets

[← Back to README](../../README.md)

## File Inventory

| File | Purpose | Size |
|---|---|---|
| `logo-light.svg` | Logo for light backgrounds (README light mode) | viewBox 256×256 |
| `logo-dark.svg` | Logo for dark backgrounds (README dark mode) | viewBox 256×256 |
| `icon.svg` | Generic icon that adapts to parent via `currentColor` | viewBox 64×64 |
| `social-preview.svg` | GitHub repository social preview image | 1280×640 |

## Design Meaning

Each element corresponds to the project core:```
   ╭──────────╮
   │  ▒    ·  │   ← outer circle = solver field of view / protocol scope
   │   │    │ │
   │   │  ▒ │ │   ← diagonal tiles highlighted = hCaptcha solver target locked
   │ → │ ── │ │   ← horizontal arrow → = protocol replay traversal
   │   │    │ │   ← vertical │ = protocol stack layering (client / server)
   ╰──────────╯
```- Monochrome, no decoration, geometric
- Recognizable at 16×16 favicon size
- No copying of any commercial brands

## Using in README (add after making the repository public)

> ⚠️ **GitHub cannot correctly render relative SVG paths in private repositories.** The browser receives GitHub blob view HTML instead of the SVG, which displays as strange page elements (like your own GitHub avatar). After switching the repository to public, the raw URL directly hits the SVG and this problem disappears. **So please add the reference snippet below to the main README only after making it public.**

After switching to public, place the logo centered at the top of the README (recommended):```html
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.svg">
    <img src="docs/images/logo-light.svg" width="160" alt="Gpt-Agreement-Payment">
  </picture>
</p>
```# Or the fragment hack writing method recommended by GitHub officials (dual images automatically switch themes, does not depend on picture):```html
<p align="center">
  <img src="docs/images/logo-light.svg#gh-light-mode-only" width="160" alt="Gpt-Agreement-Payment">
  <img src="docs/images/logo-dark.svg#gh-dark-mode-only" width="160" alt="Gpt-Agreement-Payment">
</p>
```or small size inline next to the title (without disrupting the restrained feel of the research project):```html
<h1>
  <img src="docs/images/icon.svg" width="32" valign="middle"> Gpt-Agreement-Payment
</h1>
```## Setting Up GitHub Social Preview

1. Open `social-preview.svg` in a browser, take a screenshot or export as PNG (you can also use `librsvg`:
   ```bash
   rsvg-convert -w 1280 -h 640 docs/images/social-preview.svg -o /tmp/social.png
   ```
2. GitHub repository → Settings → General → Social preview → Upload an image
3. After uploading, all link previews shared from Twitter / Slack / RSS and other platforms will use this image

## Modifications and Recreation

SVG is a text format and can be edited directly with any editor. After making changes:

- Keep it monochrome (don't add gradients / filters, may render inconsistently across browsers)
- Maintain the viewBox aspect ratio (don't change to a rectangle, will break geometric symmetry)
- When changing colors, use `#1f2328` (GitHub neutral dark gray) for light version and `#e6edf3` (light gray) for dark version—both colors look natural under both GitHub themes
- If adding text, use `font-family="ui-monospace, monospace"` to maintain consistency with the project style