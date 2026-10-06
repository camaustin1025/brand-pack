# brand-pack

A Claude skill that turns a customer's or prospect's website URL into a zipped brand pack, so vibe-coded mockups in Lovable, v0, Bolt, Claude or Figma Make look like the customer built them.

One URL in. Out: `DESIGN.md` (design tokens plus usage prose), the official logo files, measured colors and type, real products with prices, verbatim copy samples, and homepage screenshots, zipped with a README that tells you how to load it into Lovable.

## Install

**claude.ai (web, desktop, Cowork)**

1. Download `dist/brand-pack.zip` from this repo.
2. In claude.ai go to Settings, then Skills, and upload the zip.
3. In any chat, paste a URL and say "brand pack for this site".

**Claude Code**

```bash
git clone https://github.com/camaustin1025/brand-pack.git
./brand-pack/scripts/install-claude-code.sh
```

Then in a new session: `/brand-pack https://<customer-site>/`

Requirements: Python 3 (standard library only, nothing to install) and Chrome, Chromium, Edge or Brave on the machine for screenshots and logo previews. Without a browser the pack still builds; it just says `visual check: skipped`.

## What you get

```
brand-pack-<brand>/
  DESIGN.md        tokens in frontmatter + eight sections, every color traced to the site's CSS
  tokens.json      the same tokens with provenance and confidence
  logo/            primary SVG (+ transparent PNG), icon, dark-background variant when the site has one
  products.json    real products, prices and image URLs
  voice.md         verbatim headlines, CTA labels, nav and footer links
  screenshots/     homepage desktop and mobile
  README.md        confidence table, three-step Lovable handoff, paste-ready prompt
```

Load it into Lovable in three steps: paste `DESIGN.md` into the project's Knowledge, attach the logo files and `products.json`, paste the prompt from the pack README as the first message.

## How it stays accurate

The script measures, the model judges, and the builder refuses anything that was not measured.

- `brand-pack/scripts/extract.py` reads the site's HTML and CSS and writes every candidate value with its source (selector, stylesheet, schema.org field).
- Claude views the screenshots and logo previews, then writes a selection file.
- `brand-pack/scripts/build_pack.py` rejects any hex, font family, logo file or quoted headline that is not in the measured candidates. An override needs a written reason and is stamped "inferred" in the pack.

Third-party marks (payment, social, financing, review badges, press logos, certification seals) are never chosen as the logo. Framework palettes (Bootstrap, BigCommerce Cornerstone, Tailwind defaults) are flagged and not treated as brand colors.

## Coverage

Tested across Shopify (standard and headless), Magento, BigCommerce, Salesforce Commerce Cloud, and Tailwind, Nuxt and Builder.io front ends, including sites behind Cloudflare or bot walls, sites that serve logos as inline SVG or SVG sprites, and sites with licensed fonts (Adobe Fonts and self-hosted foundry files, which the pack names and substitutes rather than bundles).

## Known gaps

- Logos drawn from a CSS image sprite are not found; the skill asks for the logo file.
- Radii, sizes, weights and padding are not provenance-checked, only colors, fonts, logos and quoted copy.
- Cookie banners can cover part of a screenshot.
- Sites that block headless browsers get `visual check: blocked`; open them in a normal browser to confirm.

## Use

Packs are for mockups and demos shown to that customer or prospect. Do not publish them, and do not commit them to this repo.

## Maintain

- Source of truth is `brand-pack/`. Edit there.
- Rebuild the upload zip with `scripts/package.sh`. It checks the description length (200 max) and the path characters claude.ai rejects.
- Keep customer material out of this public repo: no packs, logos, screenshots, product data or example files for real brands (`.gitignore` blocks the usual folders).
