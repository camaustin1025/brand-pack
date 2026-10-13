# Selection rules and selections.json schema

Read this before writing `selections.json`. The extractor measured; this file governs how to choose. The builder rejects any value that is not in `candidates.json`, so every choice below must point at a measured value.

## 1. Logo

Pick by this order. Stop at the first candidate that passes the visual check.

| Priority | `source` in candidates.json | Notes |
|---|---|---|
| 1 | `press-page` | Official kit on the site's own press or brand page. |
| 2 | `json-ld-organization` | The site declares this as its logo in schema.org markup. |
| 3 | `inline-svg-header`, `header-img` | The masthead mark linked to the homepage. Usually the right answer. |
| 4 | `inline-svg-footer`, `footer-img` | Often the white or reversed variant. Use as `on_dark`, not `primary`. |
| 5 | `apple-touch-icon`, `mask-icon`, `favicon` | Symbol or app icon. Use as `icon`. Never as `primary`. |
| 6 | `og:image` | Social preview. Rarely the logo. Use only when nothing above exists, and say so. |

Rules:
- Prefer SVG over PNG. Prefer the largest PNG when no SVG exists. Never use anything under 64px as `primary`.
- Inline SVGs that only contain `<use href="sprite.svg#id">` are resolved into standalone files from the sprite; the candidate carries a `sprite_note`. If the note says the reference could not be resolved, the file is empty: skip it.
- Never recolor, stretch, crop, or add effects. If the brand needs a white version and none was measured, say so in `logo.notes` and let the mockup builder recolor the SVG fill to white at build time.
- Sub-brand and product lockups (for example a product name "by Brand") are not the primary logo. Mention them in `logo.notes`.
- Third-party marks (payment, social, app store, review platforms, financing) are never the logo even when the filename says "logo". The extractor rejects them; if one slipped through, do not select it.
- If the top candidate fails the visual check (does not match what the screenshot shows in the header), pick the next one. If none match, stop and ask the user for the logo file, then add it under `raw/logo-candidates/` and set `logo.override_reason`.

## 2. Colors

Required keys: `primary`, `background`, `text`. Common extra keys: `secondary`, `accent`, `surface`, `text-muted`, `border`, `white`, `success`, `error`. Use descriptive slugs the brand itself uses when `named_palette` has them (for example a `brand-orange` class becomes `accent` with the brand's own name in `color_roles`).

On Shopify themes, `named_palette` carries scheme entries such as `1/primary-button-background` or `scheme-sand/background`: the theme's own color schemes. The block-level accent (an offer button in a different color) shows up in `colors.frequency` and the pool because it is set as an inline `--button-color` variable; confirm it against the screenshot before naming it `accent`.

Trust order when the pool disagrees:
1. `named_palette` (utility classes such as `.color-brand-orange`) and `colors.variables` (`--color-primary`). These are the brand naming its own colors.
2. `colors.buttons` with `state: base` on the main button selector (`.button`, `.btn-primary`, `.action.primary`). That fill is `primary` unless the screenshot shows otherwise.
3. `colors.body` for `background` and `text`.
4. `colors.frequency` for the rest.

On Tailwind and Builder.io sites the main button is not a `.button` rule. Look for `colors.buttons` entries with `via: "cursor-pointer rule"`: they carry the button's live `text` (for example "Shop Daily Electrolytes"), fill, text color, radius, padding and weight, measured from the block's own style tag. The most repeated fill across those entries is `primary`.

Framework defaults are not brand colors. Entries flagged `framework_default: true` in `colors.frequency`, `colors.variables` or `named_palette` (Bootstrap's `--primary #007bff`, `--secondary #6c757d`, `--success`, `--danger`; BigCommerce Cornerstone's `#444` button; Tailwind's stock blues, and Tailwind v4's `--color-red-500`-style palette) come from the theme's base CSS. Pick them only when the screenshot shows them in use. On such sites the real brand colors usually sit in custom variables (`--brand-primary-brand-black`) or named classes (`.color-brand-red`).

Do not pick colors whose only examples are third-party or utility selectors: `.ui-datepicker`, `.flickity-*`, `.pswp`, `.swiper-*`, cookie or consent banners, `mark`, `pre`, `.mage-error`. Those are library defaults, not brand.

Check every choice against the screenshot. If the page background in the screenshot is cream and the pool has both `#ffffff` and `#fdfaf3`, `background` is the cream.

Never write a hex that is not in `colors.pool`. If the brand's real color truly is not in the pool (rare: a color that only exists in images), use `{"value": "#hex", "override_reason": "..."}` and the pack will label it inferred.

## 3. Typography

Roles: `display` (hero headlines), `heading` (section titles), `body`, `label` (buttons, nav, eyebrows). Each needs `fontFamily`; add `fontWeight`, `fontSize`, `lineHeight`, `letterSpacing`, `textTransform` when measured in `typography.headings`, `typography.body`, or `colors.buttons`.

- Use `typography.font_faces` and `typography.families` (skip `is_icon_font` and `is_system_fallback` entries) to name the family.
- `fontSize` for display and heading: write a `clamp()` from the measured mobile and desktop sizes, for example `clamp(2.5rem, 7vw, 6.4rem)` for 40px mobile and 102px desktop. Sizes come from `typography.headings` on classic sites and from `typography.large_type` on utility-class sites, where each entry lists `font_sizes` (desktop first, then media-query sizes), tracking, line-height and the verbatim text it styles.
- `fontWeight` only when a rule declares it. A weight read from the screenshot goes in `typography_prose` as a visual read, not in the token.
- `fallback_stack`: the rest of the measured `font-family` stack after the brand family.
- Record a `licensing` entry per family. Use `google-fonts` when a Google Fonts source was measured, `adobe-fonts` for Typekit/Adobe hosting, `system` for system fonts, `self-hosted-licensed` only when documented terms support that classification, and `unknown` when terms were not verified. Source detection alone does not establish permission, and a self-hosted font absent from Google Fonts is not necessarily proprietary. Explain the evidence and uncertainty in `note`; do not claim a license agreement was checked unless it was.
- For fonts with potentially restricted or unverified terms, recommend a similar `google_fonts_substitute` based on width, x-height and personality, and explain the recommendation in `note`. The substitute is design guidance, not a guaranteed closest match or licensing clearance. It is not a measured token; the pack still names the real family. Verify the terms of the substitute before use.

Type scales defined as CSS variables (`--text-f4: 40px`, `--text-desktop-f4: 56px`) are resolved automatically; the `large_type` entry for the desktop variant inherits family, weight and transform from its base class. When the site loads only specific weights (for example 450 and 500), say so in `typography_prose` and do not specify weights the site never loads.

## 4. Shape and spacing

`rounded` keys are open. Use what the brand uses: `pill` for `500px`/`999px`/`50px` button radii, `card`, `sm`, `md`, `lg`. Values come from `shape.border_radius_frequency` and `colors.buttons[].border-radius`. Skip radii that only appear on library selectors.

`spacing` is optional. Omit it to get a default 4/8/16/24/40/64 scale.

## 5. Components

`components` entries may only carry these props (the DESIGN.md spec limit): `backgroundColor`, `textColor`, `typography`, `rounded`, `padding`, `size`, `height`, `width`. Reference tokens as `{colors.primary}`, `{rounded.pill}`, `{typography.label}`. Put everything else (border, weight, letter-spacing, hover behavior, case) in `component_notes[name]` as prose.

Always include `button-primary`. Add `button-secondary`, `card`, `input`, `announcement-bar` when measured.

## 6. Voice

`voice.sample_headlines` and `voice.sample_ctas` must be verbatim strings from `copy.headlines` and `copy.cta_labels`. The builder checks. Describe tone in `voice.tone` in plain words (sentence length, case, humor, proof points).

## 7. Visual check

Before writing selections, view the desktop screenshot (and mobile if present) and the rendered preview of the chosen logo. If `render.py page` reported `status: "challenge"`, the screenshots show a bot wall, not the site: try a browser tool if one is available; otherwise set `visual_check.status` to `"blocked"` and say so. Blocked screenshots are not copied into the pack. Confirm: header logo matches, page background matches `background`, main button matches `button-primary`, headline weight and case match `display`. Record the result in `visual_check`. If no screenshot could be produced, set `status: "skipped"` and say why. Never claim a check that did not happen.

## 8. selections.json schema (complete example)

Values below are fictional (Northwind Roasters, northwind.example) and only show the shape. Every value in a real file must come from `candidates.json`.

```json
{
  "brand_name": "Northwind Roasters",
  "slug": "northwind",
  "tagline": "Small-batch coffee, roasted weekly.",
  "summary": "Warm, minimal DTC coffee brand. Cream backgrounds, espresso-brown type, one copper accent, pill buttons, large tight-tracked headlines over product photography.",
  "logo": {
    "primary": "raw/logo-candidates/01-header-img.svg",
    "on_dark": null,
    "icon": "raw/logo-candidates/02-favicon.png",
    "notes": "No white variant measured; recolor the SVG fill to #ffffff for dark backgrounds."
  },
  "colors": {
    "primary": "#2b1d16",
    "background": "#fbf7f0",
    "surface": "#f1e9dc",
    "accent": "#c7742b",
    "text": "#2b1d16",
    "text-muted": "#6b5f57",
    "border": "#d9d2c7",
    "white": "#ffffff"
  },
  "color_roles": {
    "primary": "brand-espresso: buttons, headings, body text",
    "accent": "brand-copper: announcement bar, highlights, selected states"
  },
  "colors_prose": "Optional paragraph on how color is used across the page.",
  "typography": {
    "display": {"fontFamily": "Brand Sans", "fontWeight": 400, "fontSize": "clamp(2.5rem, 7vw, 6rem)", "lineHeight": 0.95, "letterSpacing": "-0.04em"},
    "heading": {"fontFamily": "Brand Sans", "fontWeight": 400, "fontSize": "clamp(1.75rem, 4vw, 3rem)", "lineHeight": 1.0, "letterSpacing": "-0.02em"},
    "body":    {"fontFamily": "Brand Sans", "fontWeight": 400, "fontSize": "16px", "lineHeight": 1.6},
    "label":   {"fontFamily": "Brand Sans", "fontWeight": 500, "fontSize": "14px", "letterSpacing": "-0.02em"},
    "fallback_stack": "'Helvetica Neue', Helvetica, Arial, sans-serif",
    "licensing": {
      "Brand Sans": {"status": "unknown", "google_fonts_substitute": "Inter Tight", "note": "Self-hosted family; usage terms not verified. Similar neo-grotesque alternative: Inter Tight for display, Inter for body."}
    }
  },
  "typography_prose": "Optional paragraph.",
  "rounded": {"pill": "500px", "card": "16px", "sm": "4px", "md": "8px", "lg": "20px"},
  "components": {
    "button-primary":   {"backgroundColor": "{colors.primary}", "textColor": "{colors.white}", "rounded": "{rounded.pill}", "padding": "0.5em 2em", "typography": "{typography.label}"},
    "button-secondary": {"backgroundColor": "{colors.white}", "textColor": "{colors.primary}", "rounded": "{rounded.pill}", "padding": "0.5em 2em", "typography": "{typography.label}"},
    "card":             {"backgroundColor": "{colors.white}", "rounded": "{rounded.card}", "padding": "24px"}
  },
  "component_notes": {
    "button-primary": "1px solid border in the fill color; sentence case; no shadow.",
    "button-secondary": "1px solid {colors.primary} border."
  },
  "layout": "Prose: container width, header pattern, hero pattern, grid rhythm as seen in the screenshot.",
  "elevation": "Prose: measured shadows and where they appear.",
  "shapes": "Prose: form language beyond radii.",
  "voice": {
    "tone": "Short declarative sentences. Sentence case. Warm, specific about origin and roast. Leads with proof points.",
    "sample_headlines": ["Roasted this week. At your door by Friday.", "Coffee that tastes like somewhere."],
    "sample_ctas": ["Shop coffee", "Build a subscription"],
    "cta_style": "Verb first, product second, sentence case."
  },
  "dos": ["Cream page background with white cards."],
  "donts": ["Pure white page background.", "Uppercase or bold display headlines."],
  "products": "all",
  "confidence": {"logo": "measured", "shape": "measured"},
  "visual_check": {"status": "passed", "notes": "Desktop and mobile screenshots reviewed; header wordmark matches the selected SVG. Status is one of passed, skipped, blocked."},
  "lovable_prompt": null
}
```

`products` accepts `"all"`, `"none"`, or a list of handles or titles to include.
