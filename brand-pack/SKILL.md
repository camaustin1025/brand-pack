---
name: brand-pack
description: Turns a customer or prospect URL into a zipped brand pack (DESIGN.md, logos, colors, fonts, products, copy) for on-brand mockups. Use for "brand pack" or "make the demo look like [brand]".
---

# Brand Pack

Turn one URL into a zipped, provenance-checked brand pack so mockups look like the customer built them. The script measures; the model judges; the builder refuses anything that was not measured.

## Workflow (six gates, in order)

Work in `brand-pack-work/<slug>/` inside the current directory unless the user names a folder. `SKILL_DIR` below means this skill's folder.

### 1. Intake

Accept exactly one URL. Do not ask for colors, fonts, or logos up front; the site is the source. If the user also has an official press-kit URL or a logo file, take it (pass `--press-url`, or copy the file into `raw/logo-candidates/` after step 2 and note it as an override).

### 2. Measure

```bash
python3 SKILL_DIR/scripts/extract.py <url> --out brand-pack-work/<slug>
```

Read only the printed summary. Do not load `candidates.json` whole; query it with small Python snippets (see "Reading candidates" below). If the summary shows zero stylesheets or zero headlines, the site is JS-rendered: run `python3 SKILL_DIR/scripts/render.py dom <url> --out brand-pack-work/<slug>/raw/rendered.html`, then rerun `extract.py` with `--html brand-pack-work/<slug>/raw/rendered.html`.

If `extract.py` exits with `"status": "error"` and a hint about the browser mirror, this environment cannot reach the site (Cowork and claude.ai sandboxes often block outside websites). Follow "Site can't be reached" below, then continue at step 3.

### 3. Screenshot

```bash
python3 SKILL_DIR/scripts/render.py page <url> --out brand-pack-work/<slug>/screenshots --mobile
```

View `homepage-desktop.png` (and mobile). When the script reports `skipped` and a browser tool is available, take the screenshot with that tool and save it to the same folder. When neither works, continue and set `visual_check.status` to `skipped`. When it reports `challenge`, the site blocks headless browsers: try the browser tool; if that is blocked too, set `visual_check.status` to `blocked` (the pack then ships without screenshots and says why).

### 4. Verify and select

Read `SKILL_DIR/references/selection-rules.md`. Then:

1. Preview the top logo candidates: `python3 SKILL_DIR/scripts/render.py svg <candidate.svg> --out brand-pack-work/<slug>/previews/<name>.png --bg "#ffffff"` for SVGs; view PNGs directly. Match against the header in the screenshot.
2. Choose colors from `named_palette`, `colors.variables`, `colors.buttons` (base state on the main button selector), and `colors.body`, in that trust order. Confirm against the screenshot. When a button fill is `oklch()` or `rgb()`, use the hex in that entry's `hex` field; that is the value in `colors.pool`.
3. Choose type from `typography.font_faces` and `typography.families` (skip icon fonts and system fallbacks). Decide licensing status and a substitute for licensed fonts.
4. Choose radii from `shape.border_radius_frequency` and button rules.
5. Quote headlines and CTA labels verbatim from `copy`.
6. Write `brand-pack-work/<slug>/selections.json` using the schema in the reference file. Every hex, family, logo path and quote must come from `candidates.json`.

Hard rules: never type a hex or font that is not in the measured pool without an `override_reason`; never claim a visual check that did not happen; never pick a third-party mark (payment, social, financing, review platform) as the logo.

### 5. Build

```bash
python3 SKILL_DIR/scripts/build_pack.py --work brand-pack-work/<slug> --zip
```

On `VALIDATION FAILED`, fix `selections.json` to use a measured value (the message lists the nearest measured colors). Do not add `override_reason` just to pass; use it only when the site truly does not carry the value in CSS and say why.

Open the generated `DESIGN.md` and read it once as the mockup builder would. Fix prose that contradicts the screenshot.

### 6. Deliver

Give the user the zip path plus the five summary lines the builder printed (logo, colors and type, products, visual check, inferred count). Name any licensed font and its substitute. Mention the product source when it was probed rather than read from a catalog API. Offer, in one line, to push DESIGN.md and the logos into a Lovable project through the connector when that connector is available (see `SKILL_DIR/references/lovable-handoff.md`); do it only when asked.

Report on the pack only. Do not comment on the company itself: its ownership, finances, legal pages, news or health. Sellers read the delivery message before outreach and can take a passing remark as a fact. Boilerplate you saw while measuring (privacy policy sections, terms, cookie banners) is not a finding.

## Site can't be reached (Cowork, claude.ai)

Measure through the user's browser instead: `capture.js` fetches the site from inside a browser tab and saves everything to one JSON file, and `extract.py --mirror` reads that file instead of the network.

Mirror files come only from `capture.js`. Never write or edit a mirror file, and never build page HTML, product lists or prices yourself to feed `extract.py`: that turns your reading of the page into "measured" data. Each file carries a digest, and `extract.py` prints `UNVERIFIED` for any file that fails it; recapture instead of building on it. When products or prices still look wrong after the passes below, build with what was measured and say what looks off in the delivery message. This needs a browser tool that can run JavaScript (Cowork's browser). Without one, tell the user this environment cannot reach the site and that Claude Code, or a workspace admin allowing outside sites, fixes it. Stop there.

1. Open the site's homepage in the browser tool. If a cookie banner covers the page, decline non-essential cookies.
2. Read `SKILL_DIR/scripts/capture.js` and run its full text in the browser tool's JavaScript runner. It returns `{file, urls, ok, failed, blocked_by_cors}` in a few seconds (if the tool returns early, read `window.__bpResult`) and downloads `file`, named `brand-pack-mirror-<host>-<time>.json`, to the browser's downloads folder.
3. Copy that file into `brand-pack-work/<slug>/mirror/`. It is usually the newest `brand-pack-mirror-*.json` in `~/Downloads`. If the workspace cannot see the downloads folder, ask the user to move the file into the task folder.
4. Measure from it:

   ```bash
   python3 SKILL_DIR/scripts/extract.py <url> --out brand-pack-work/<slug> --mirror brand-pack-work/<slug>/mirror/<file1> [--mirror .../<file2> ...]
   ```

   Pass every mirror file collected so far. The last line reports `N missing` and writes `missing-urls.json`.
5. If anything is missing (product and press pages usually are), do this pass before judging products; probed products and prices come from these pages. In the tab still on the site, run `window.BP_URLS = <contents of missing-urls.json>;` followed by the full text of `capture.js`, copy the new file, and rerun step 4. Repeat at most twice more.
6. `blocked_by_cors` lists files on other hosts (often the logo on a CDN) that refused a script running on the site. For each host whose URLs look like a logo, stylesheet or font (skip analytics, consent and chat widgets): navigate the tab to the first URL in its list, run `window.BP_URLS = <that host's list>;` plus `capture.js` there, copy the file, and rerun step 4. Go back to the homepage afterward.
7. If the summary shows zero stylesheets or zero headlines, rerun step 4 with `--rendered` to read the browser's rendered DOM.

For step 3 (Screenshot), `render.py page` cannot reach the site either. Compare the live site in the browser tab at desktop and mobile widths, set `visual_check.status` to `passed` with the note "checked live in the browser; no screenshots saved", and save browser screenshots into `screenshots/` only if the tool can write files. In the delivery message, say the pack was built from a browser capture, and that the `brand-pack-mirror-*.json` files in Downloads can be deleted.

## Reading candidates

`candidates.json` can exceed 200 KB. Query it:

```bash
python3 - <<'EOF'
import json; c = json.load(open("brand-pack-work/<slug>/candidates.json"))
print([ (n["name"], n["hex"]) for n in c["named_palette"][:40] ])
print([ (v["name"], v["hex"]) for v in c["colors"]["variables"][:40] ])
print([ {k: b.get(k) for k in ("selector","background","background-color","color","hex","border-radius","padding","font-weight","letter-spacing","text-transform")} for b in c["colors"]["buttons"] if b["state"] == "base" ][:12])
print(c["colors"]["body"], c["typography"]["headings"][:8])
print([ (e["font_sizes"], e.get("font-weight"), e.get("letter-spacing"), e.get("line-height"), (e.get("text") or "")[:40]) for e in c["typography"]["large_type"] if e.get("text") ][:10])
print([ (f["family"], f["weight"], f["src"][:60]) for f in c["typography"]["font_faces"] ], c["typography"]["services"])
print(c["shape"], c["copy"]["headlines"][:10], c["copy"]["cta_labels"])
print([ (l["score"], l["source"], l.get("format"), l.get("width"), l.get("height"), l.get("local_path")) for l in c["logos"]["candidates"] ])
EOF
```

## Pack contents (fixed layout)

```
brand-pack-<slug>/
  DESIGN.md        DESIGN.md spec: frontmatter tokens + eight sections, with a "Measured from" column
  tokens.json      same tokens + provenance + confidence
  logo/            primary.svg (+ primary-2x.png rendered transparent), icon.*, on-dark.* when measured
  products.json    real products with prices and image URLs (Shopify products.json, JSON-LD, or probed)
  voice.md         verbatim headlines, CTAs, nav and footer labels
  screenshots/     homepage desktop and mobile
  README.md        contents, confidence table, three-step Lovable handoff, paste-ready prompt
```

If `SKILL_DIR/assets/example-pack/` exists, open its `DESIGN.md` and `selections.json` before writing a first pack; it shows how measured candidates become tokens.

## Environment notes

- Scripts use only the Python standard library. Screenshots and SVG previews need Chrome, Chromium, Edge, or Brave on the machine (`CHROME_PATH` overrides detection). Without a browser the pack still builds; it just says `visual check: skipped`.
- Shopify stores expose `/products.json` (12 real products with prices, variants and images) and `/collections.json`. Shopify themes define colors per "color scheme" and type in CSS variables inside `:root`, often behind CSS nesting and inline `style` attributes on blocks (the gold offer button on a hero is `--button-color` on that block). All of these land in `named_palette` (as `scheme-name/property`), `colors.variables` and the resolved `typography.headings`. Other platforms fall back to JSON-LD and a bounded probe of shop pages. Probed products carry a per-item `provenance` field and a README note to verify prices.
- Tailwind, Nuxt, Next and Builder.io sites keep styling in class attributes and per-block `<style>` tags, so `h1`/`button` selectors measure nothing. The extractor covers this: brand colors appear as `named_palette` entries (`.bg-gray-800`, `.text-gold-200`) and `colors.variables` (`--color-gold-100`); buttons appear in `colors.buttons` with `via: "cursor-pointer rule"` and their live `text`; display sizes and verbatim headlines appear in `typography.large_type` with desktop, tablet and mobile `font_sizes`. Read those before concluding a site "has no buttons".
- Bot-protected sites (Cloudflare "Just a moment" pages) are fetched through headless Chrome automatically when urllib is refused; the summary prints a note when that happened.
- A mirror run is offline: it measures exactly what the browser captured, and `extract.py` refuses a mirror captured on a different site. Mirror files may also come from an earlier improvised capture with the same `{url: {status, ctype, body, b64}}` shape. A deep URL from the user (a category or collection page) becomes the first product probe, so pass it through unchanged.
- Inline SVG logos that take their fill from a CSS class are saved with `fill="currentColor"` and `style="color:#000000"` so they render standalone; the candidate carries a `fill_note`. Recolor by changing that style color, never by editing paths. Logos referenced through an SVG sprite (`<use href="sprites.svg#id">`) are rebuilt from the sprite symbol and carry a `sprite_note`.
- Product sources, in order: Shopify `products.json`; JSON-LD Product or ItemList on the homepage and shop pages; product objects inside embedded JSON (BigCommerce Stencil context, Next.js data); product-detail pages linked from the homepage, read through their schema.org Product data (Salesforce Commerce Cloud); a price-anchored card heuristic last. `product_source` in the summary says which one ran, and probed items carry a `provenance` field.
- Framework palettes (Bootstrap, BigCommerce Cornerstone, Tailwind stock colors) are flagged `framework_default: true` in the candidates. They are not brand colors unless the screenshot shows them.
- Licensed fonts (Adobe Fonts, self-hosted foundry files) are never bundled. The pack names the family, the closest Google Fonts substitute, and the weights and tracking to match.
- The pack is for mockups shown to that customer or prospect. Say so in the README (the builder already does) and do not publish packs.
