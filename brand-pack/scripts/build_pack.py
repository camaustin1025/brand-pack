#!/usr/bin/env python3
"""
build_pack.py - assemble the brand pack from candidates.json + selections.json. Standard library only.

Usage:
    python3 build_pack.py --work <work-dir> [--zip] [--no-render]

Reads:
    <work-dir>/candidates.json    written by extract.py (measured values + provenance)
    <work-dir>/selections.json    written by the model after the visual check (schema: references/selection-rules.md)
    <work-dir>/screenshots/*.png  optional, from render.py

Writes:
    <work-dir>/pack/brand-pack-<slug>/  DESIGN.md, tokens.json, logo/, products.json, voice.md, screenshots/, README.md
    <work-dir>/brand-pack-<slug>-<YYYYMMDD>.zip   with --zip

Every color, font family, logo path and quoted headline in selections.json must exist in candidates.json.
Anything else fails validation. An entry may carry {"value": ..., "override_reason": "..."} to bypass a single
check; the token is then stamped `inferred` in tokens.json, DESIGN.md and the README.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

ALLOWED_COMPONENT_PROPS = ("backgroundColor", "textColor", "typography", "rounded", "padding", "size", "height", "width")
HERE = os.path.dirname(os.path.abspath(__file__))


def die(msg, problems=None):
    print("VALIDATION FAILED: " + msg)
    for p in problems or []:
        print("  - " + p)
    sys.exit(1)


def yq(s):
    """YAML double-quoted scalar."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def yval(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return yq(v)


def norm_hex(v):
    v = str(v).strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{6})", v)
    if m:
        return v
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    if m:
        return "#" + "".join(ch * 2 for ch in m.group(1))
    return None


def unwrap(entry):
    """selections values may be scalar or {"value":..., "override_reason":...}."""
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"], entry.get("override_reason")
    return entry, None


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "brand"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--no-render", action="store_true", help="skip SVG->PNG logo rendering")
    a = ap.parse_args()
    work = os.path.abspath(a.work)
    cpath, spath = os.path.join(work, "candidates.json"), os.path.join(work, "selections.json")
    if not os.path.exists(cpath):
        die(f"missing {cpath} (run extract.py first)")
    if not os.path.exists(spath):
        die(f"missing {spath} (write selections after the visual check; schema in references/selection-rules.md)")
    C = json.load(open(cpath, encoding="utf-8"))
    S = json.load(open(spath, encoding="utf-8"))

    problems, inferred = [], []
    origin = C["source"]["origin"]
    domain = re.sub(r"^www\.", "", origin.split("//", 1)[-1])
    brand = S.get("brand_name") or C["meta"].get("og_site_name") or domain.split(".")[0].title()
    slug = S.get("slug") or slugify(domain.split(".")[0])
    today = time.strftime("%Y-%m-%d")

    # ---------------- validate colors
    pool = set(C["colors"].get("pool", []))
    colors, color_prov = {}, {}
    # Provenance example per hex, best evidence first: the brand's own named class, then a CSS variable,
    # then body/button/link rules, then the most frequent selector. Never a library selector when better exists.
    LIBRARY = re.compile(r"(ui-datepicker|flickity|pswp|swiper|slick|cookie|consent|mage-error|\bmark\b|\bpre\b|table\.totals|file-uploader|tiled-swatch)", re.I)

    def clean_sel(sel):
        sel = re.sub(r"\s+", " ", sel or "").strip()
        return sel.split(",")[0].strip()[:60]

    examples = {}
    for np_ in C.get("named_palette", []):
        examples.setdefault(np_["hex"], {"property": np_["property"], "selector": clean_sel(np_["selector"]), "source": np_["source"], "kind": "named class"})
    for cv in C["colors"].get("variables", []):
        examples.setdefault(cv["hex"], {"property": cv["name"], "selector": clean_sel(cv["selector"]), "source": cv["source"], "kind": "css variable"})
    for b in C["colors"].get("body", []):
        for prop in ("background-color", "background", "color"):
            hx = norm_hex(b.get(prop, "")) if b.get(prop) else None
            if hx:
                examples.setdefault(hx, {"property": prop, "selector": clean_sel(b["selector"]), "source": b["source"], "kind": "body rule"})
    for b in C["colors"].get("buttons", []):
        if b.get("state") != "base":
            continue
        for prop in ("background", "background-color", "color"):
            hx = (norm_hex(b.get(prop, "")) if b.get(prop) else None) or (b.get("hex") or {}).get(prop)
            if hx:
                examples.setdefault(hx, {"property": prop, "selector": clean_sel(b["selector"]), "source": b["source"], "kind": "button rule"})
    for l in C["colors"].get("links", []):
        hx = norm_hex(l.get("color", ""))
        if hx:
            examples.setdefault(hx, {"property": "color", "selector": clean_sel(l["selector"]), "source": l["source"], "kind": "link rule"})
    for c in C["colors"].get("frequency", []):
        ex = c.get("example") or {}
        if ex and not LIBRARY.search(ex.get("selector") or ""):
            examples.setdefault(c["hex"], {"property": ex.get("property"), "selector": clean_sel(ex.get("selector")), "source": ex.get("source"), "kind": "frequent selector"})
    for c in C["colors"].get("frequency", []):  # last resort, library selectors allowed
        ex = c.get("example") or {}
        if ex:
            examples.setdefault(c["hex"], {"property": ex.get("property"), "selector": clean_sel(ex.get("selector")), "source": ex.get("source"), "kind": "frequent selector"})
    for key, entry in (S.get("colors") or {}).items():
        val, why = unwrap(entry)
        hx = norm_hex(val)
        if not hx:
            problems.append(f"colors.{key}: '{val}' is not a 6-digit hex")
            continue
        if hx in pool:
            ex = examples.get(hx) or {}
            colors[key] = hx
            color_prov[key] = {"status": "measured", "hex": hx, "property": ex.get("property"), "kind": ex.get("kind"),
                               "selector": (ex.get("selector") or "")[:120], "source": ex.get("source")}
        elif why:
            colors[key] = hx
            color_prov[key] = {"status": "inferred", "hex": hx, "override_reason": why}
            inferred.append(f"colors.{key} ({hx}): {why}")
        else:
            near = sorted(pool, key=lambda p: sum(abs(int(p[i:i+2], 16) - int(hx[i:i+2], 16)) for i in (1, 3, 5)))[:5]
            problems.append(f"colors.{key}: {hx} is not in the measured pool. Nearest measured: {', '.join(near)}. "
                            f"Use one of those or add override_reason.")
    if not colors:
        problems.append("colors: none selected (need at least primary, background, text)")
    for req in ("primary", "background", "text"):
        if req not in colors:
            problems.append(f"colors.{req} is required")

    # ---------------- validate typography
    fpool = {f.lower() for f in C["typography"].get("pool", [])}
    typo_in = S.get("typography") or {}
    typography, typo_prov = {}, {}
    for role in ("display", "heading", "body", "label"):
        spec = typo_in.get(role)
        if not spec:
            continue
        fam, why = unwrap(spec.get("fontFamily"))
        if not fam:
            problems.append(f"typography.{role}.fontFamily missing")
            continue
        first = fam.split(",")[0].strip().strip("'\"")
        if first.lower() in fpool:
            typo_prov[role] = {"status": "measured", "family": first}
        elif why:
            typo_prov[role] = {"status": "inferred", "family": first, "override_reason": why}
            inferred.append(f"typography.{role} ({first}): {why}")
        else:
            problems.append(f"typography.{role}.fontFamily '{first}' not measured on the site. Measured: {sorted(fpool)}")
            continue
        typography[role] = {"fontFamily": fam}
        for k in ("fontSize", "fontWeight", "lineHeight", "letterSpacing", "textTransform"):
            if k in spec:
                typography[role][k] = spec[k]
    if "body" not in typography:
        problems.append("typography.body is required")
    licensing = typo_in.get("licensing") or {}
    fallback_stack = typo_in.get("fallback_stack") or "system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif"

    # ---------------- validate logo
    logo_in = S.get("logo") or {}
    logo_paths = {}
    for role in ("primary", "on_dark", "icon", "wordmark", "symbol"):
        p = logo_in.get(role)
        if not p:
            continue
        full = os.path.join(work, p)
        if not os.path.exists(full):
            problems.append(f"logo.{role}: file not found: {p}")
            continue
        cand = next((c for c in C["logos"]["candidates"] if c.get("local_path") == p), None)
        if cand is None and not logo_in.get("override_reason"):
            problems.append(f"logo.{role}: {p} is not a logo candidate from extract.py (add logo.override_reason if it came from the customer directly)")
            continue
        logo_paths[role] = (full, cand)
    if "primary" not in logo_paths:
        problems.append("logo.primary is required")

    # ---------------- validate quoted headlines (verbatim only)
    measured_heads = [h["text"] for h in C["copy"].get("headlines", [])] + \
                     [e.get("text") for e in C.get("typography", {}).get("large_type", []) if e.get("text")]
    measured_ctas = C["copy"].get("cta_labels", [])
    voice = S.get("voice") or {}
    for q in voice.get("sample_headlines", []) or []:
        if not any(q == h or q in h for h in measured_heads):
            problems.append(f"voice.sample_headlines: not verbatim from the site: '{q[:60]}'")
    for q in voice.get("sample_ctas", []) or []:
        if q not in measured_ctas:
            problems.append(f"voice.sample_ctas: not a measured CTA label: '{q}'")

    # ---------------- validate component refs
    rounded = S.get("rounded") or {}
    spacing = S.get("spacing") or {"xs": "4px", "sm": "8px", "md": "16px", "lg": "24px", "xl": "40px", "2xl": "64px"}
    comps_in = S.get("components") or {}
    comps = {}
    for name, spec in comps_in.items():
        clean = {}
        for k, v in spec.items():
            if k not in ALLOWED_COMPONENT_PROPS:
                continue
            if isinstance(v, str):
                for ref in re.findall(r"\{([\w.-]+)\}", v):
                    group, _, key = ref.partition(".")
                    table = {"colors": colors, "rounded": rounded, "spacing": spacing, "typography": typography}.get(group)
                    if table is None or key not in table:
                        problems.append(f"components.{name}.{k}: unresolved token ref {{{ref}}}")
            clean[k] = v
        comps[name] = clean

    if problems:
        die("selections.json does not pass provenance checks", problems)

    # ---------------- products
    products_in = S.get("products", "all")
    products = C.get("products") or []
    if isinstance(products_in, list):
        wanted = {str(x).lower() for x in products_in}
        products = [p for p in products if (p.get("handle") or "").lower() in wanted or (p.get("title") or "").lower() in wanted]
    elif products_in in (None, False, "none"):
        products = []

    # ---------------- build output tree
    pack_name = f"brand-pack-{slug}"
    out = os.path.join(work, "pack", pack_name)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "logo"))
    os.makedirs(os.path.join(out, "screenshots"))

    # logos
    logo_files = {}
    chrome_ok = not a.no_render
    for role, (full, cand) in logo_paths.items():
        ext = os.path.splitext(full)[1].lower() or ".bin"
        dest = os.path.join(out, "logo", f"{role.replace('_', '-')}{ext}")
        shutil.copyfile(full, dest)
        logo_files[role] = os.path.relpath(dest, out)
        if ext == ".svg" and chrome_ok:
            png = os.path.join(out, "logo", f"{role.replace('_', '-')}-2x.png")
            bg = "transparent"
            r = subprocess.run([sys.executable, os.path.join(HERE, "render.py"), "svg", full, "--out", png, "--size", "1600", "--bg", bg],
                               capture_output=True, text=True)
            if os.path.exists(png):
                logo_files[role + "_png"] = os.path.relpath(png, out)
            else:
                chrome_ok = False

    # screenshots
    shots = []
    sdir = os.path.join(work, "screenshots")
    visual = S.get("visual_check") or {"status": "skipped", "notes": ""}
    if os.path.isdir(sdir) and visual.get("status") != "blocked":
        for fn in sorted(os.listdir(sdir)):
            if fn.lower().endswith(".png"):
                shutil.copyfile(os.path.join(sdir, fn), os.path.join(out, "screenshots", fn))
                shots.append("screenshots/" + fn)

    # ---------------- tokens.json
    confidence = S.get("confidence") or {}
    conf = {
        "logo": confidence.get("logo") or ("measured" if logo_paths["primary"][1] else "inferred"),
        "colors": "inferred" if any(v["status"] == "inferred" for v in color_prov.values()) else "measured",
        "typography": "inferred" if any(v["status"] == "inferred" for v in typo_prov.values()) else "measured",
        "shape": confidence.get("shape", "measured" if rounded else "n/a"),
        "products": ("measured" if C.get("product_source", "none").startswith("shopify") else
                     "probed" if products else "none"),
        "visual_check": {"passed": "passed", "skipped": "skipped", "blocked": "blocked (bot challenge; screenshots are not the site)"}.get(
            visual.get("status", "skipped"), visual.get("status", "skipped")),
    }
    tokens = {
        "pack_version": C.get("pack_version", "1.0"), "built_at": today, "brand": brand, "origin": origin,
        "platform": C["source"].get("platform"), "extractor_version": C.get("extractor_version"),
        "confidence": conf,
        "colors": colors, "typography": typography, "fallback_stack": fallback_stack, "licensing": licensing,
        "rounded": rounded, "spacing": spacing, "components": comps_in, "logo": logo_files,
        "provenance": {"colors": color_prov, "typography": typo_prov,
                       "logo": {r: {"source": (c or {}).get("source"), "url": (c or {}).get("url"), "score": (c or {}).get("score")} for r, (_, c) in logo_paths.items()},
                       "products": C.get("product_source", "none")},
    }
    json.dump(tokens, open(os.path.join(out, "tokens.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    # ---------------- DESIGN.md
    fm = ["---", f"name: {yq(brand)}", f"description: {yq(S.get('tagline') or C['meta'].get('description', '')[:140])}", "colors:"]
    for k, v in colors.items():
        fm.append(f"  {k}: {yq(v)}")
    fm.append("typography:")
    for role, spec in typography.items():
        fm.append(f"  {role}:")
        for k, v in spec.items():
            fm.append(f"    {k}: {yval(v)}")
    if rounded:
        fm.append("rounded:")
        for k, v in rounded.items():
            fm.append(f"  {k}: {yq(v)}")
    fm.append("spacing:")
    for k, v in spacing.items():
        fm.append(f"  {k}: {yq(v)}")
    if comps:
        fm.append("components:")
        for name, spec in comps.items():
            fm.append(f"  {name}:")
            for k, v in spec.items():
                fm.append(f"    {k}: {yval(v)}")
    fm.append("---")

    def role_line(key):
        roles = S.get("color_roles") or {}
        return roles.get(key, "")

    lic_lines = []
    for fam, info in licensing.items():
        st = info.get("status", "unknown")
        sub = info.get("google_fonts_substitute")
        note = info.get("note", "")
        if st in ("licensed", "self-hosted-licensed", "proprietary", "adobe-fonts", "monotype", "unknown"):
            lic_lines.append(f"- **{fam}**: usage terms need verification. Font files are not bundled. Recommended similar Google Fonts alternative: **{sub or 'none identified'}**. {note}".rstrip())
        elif st == "google-fonts":
            lic_lines.append(f"- **{fam}**: Google Fonts. Load it directly. {note}".rstrip())
        else:
            lic_lines.append(f"- **{fam}**: {st}. {note}".rstrip())

    body = []
    body.append(f"# {brand} design system\n")
    body.append("## Overview\n")
    body.append(f"{S.get('summary', '').strip()}\n" if S.get("summary") else "")
    body.append(f"Tokens in the frontmatter were measured from {origin} on {today} (platform: {C['source'].get('platform')}). "
                f"Tokens are normative; the prose explains how to apply them. Anything marked *inferred* was not measured on the site and is a judgment call.\n")
    if inferred:
        body.append("Inferred values:\n" + "\n".join(f"- {x}" for x in inferred) + "\n")
    body.append("## Colors\n")
    body.append("| Token | Hex | Role | Measured from |\n|---|---|---|---|")
    for k, v in colors.items():
        pv = color_prov[k]
        src = f"`{(pv.get('selector') or '')[:50]}` {pv.get('property') or ''}".strip() if pv["status"] == "measured" else f"inferred: {pv.get('override_reason')}"
        src = src.replace("|", "\\|")
        body.append(f"| {k} | `{v}` | {role_line(k)} | {src} |")
    body.append("")
    if S.get("colors_prose"):
        body.append(S["colors_prose"].strip() + "\n")
    body.append("## Typography\n")
    for role, spec in typography.items():
        bits = ", ".join(f"{k} {v}" for k, v in spec.items() if k != "fontFamily")
        body.append(f"- **{role}**: {spec['fontFamily']}" + (f" ({bits})" if bits else ""))
    body.append(f"- **Fallback stack**: {fallback_stack}")
    if lic_lines:
        body.append("\nLicensing:\n" + "\n".join(lic_lines))
        body.append("\nFont-source classifications and substitute suggestions are design guidance, not licensing clearance. Verify the terms of any font you use.")
    if S.get("typography_prose"):
        body.append("\n" + S["typography_prose"].strip())
    body.append("")
    if S.get("layout"):
        body.append("## Layout\n\n" + S["layout"].strip() + "\n")
    if S.get("elevation"):
        body.append("## Elevation & Depth\n\n" + S["elevation"].strip() + "\n")
    if S.get("shapes") or rounded:
        body.append("## Shapes\n")
        if rounded:
            body.append("| Token | Radius |\n|---|---|")
            for k, v in rounded.items():
                body.append(f"| {k} | `{v}` |")
            body.append("")
        if S.get("shapes"):
            body.append(S["shapes"].strip() + "\n")
    if comps_in or S.get("components_prose"):
        body.append("## Components\n")
        notes = S.get("component_notes") or {}
        for name, spec in comps_in.items():
            spec_bits = ", ".join(f"{k}: {v}" for k, v in spec.items())
            body.append(f"- **{name}**: {spec_bits}" + (f". {notes[name]}" if name in notes else ""))
        if S.get("components_prose"):
            body.append("\n" + S["components_prose"].strip())
        body.append("")
    dos, donts = S.get("dos") or [], S.get("donts") or []
    if dos or donts:
        body.append("## Do's and Don'ts\n")
        for d in dos:
            body.append(f"- Do: {d}")
        for d in donts:
            body.append(f"- Don't: {d}")
        body.append("")
    with open(os.path.join(out, "DESIGN.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(fm) + "\n\n" + "\n".join(x for x in body if x is not None) + "\n")

    # ---------------- voice.md
    vm = [f"# {brand} voice and copy samples\n", "Verbatim from the live site. Use these to keep mockup copy in the brand's register.\n"]
    if voice.get("tone"):
        vm.append("## Tone\n\n" + voice["tone"].strip() + "\n")
    if S.get("tagline"):
        vm.append(f"## Tagline\n\n{S['tagline']}\n")
    heads = voice.get("sample_headlines") or measured_heads[:8]
    vm.append("## Headlines (verbatim)\n\n" + "\n".join(f"- {h}" for h in heads) + "\n")
    ctas = voice.get("sample_ctas") or measured_ctas[:8]
    vm.append("## CTA labels (verbatim)\n\n" + "\n".join(f"- {c}" for c in ctas) + "\n")
    if voice.get("cta_style"):
        vm.append("CTA style: " + voice["cta_style"].strip() + "\n")
    nav = [n["label"] for n in C["copy"].get("nav", []) if n["label"].lower() not in ("skip to content", "close", "store logo")][:12]
    if nav:
        vm.append("## Navigation labels\n\n" + ", ".join(nav) + "\n")
    if C["copy"].get("footer_links"):
        vm.append("## Footer links\n\n" + ", ".join(C["copy"]["footer_links"][:16]) + "\n")
    with open(os.path.join(out, "voice.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(vm))

    # ---------------- products.json
    if products:
        json.dump({"source": C.get("product_source"), "note": "Real catalog items measured from the site. Use these names, prices and images in mockups instead of placeholders.",
                   "products": products}, open(os.path.join(out, "products.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    # ---------------- README.md
    lic_flag = [f for f, i in licensing.items() if i.get("status", "unknown") in ("licensed", "self-hosted-licensed", "proprietary", "adobe-fonts", "monotype", "unknown")]
    def fam_of(role):
        return (typography.get(role, {}).get("fontFamily") or "").split(",")[0].strip().strip("'\"")

    def fam_sentence(roles_label, fam):
        if not fam:
            return ""
        info = licensing.get(fam) or {}
        if info.get("status", "unknown") in ("licensed", "self-hosted-licensed", "proprietary", "adobe-fonts", "monotype", "unknown"):
            sub = info.get("google_fonts_substitute")
            recommendation = f"recommended similar alternative: {sub}" if sub else "no alternative identified"
            return f"{roles_label} use {fam} on the source site (usage terms unverified; {recommendation}). Verify font terms before use and follow the weights and letter-spacing in DESIGN.md."
        return f"{roles_label} use {fam}."

    display_fam, body_fam = fam_of("display") or fam_of("heading"), fam_of("body")
    if display_fam and body_fam and display_fam != body_fam:
        font_sentences = " ".join(x for x in [fam_sentence("Display and headings", display_fam), fam_sentence("Body and labels", body_fam)] if x)
    else:
        font_sentences = fam_sentence("Body and headings", body_fam or display_fam)
    transforms = {str(typography.get(r, {}).get("textTransform", "")).lower() for r in ("display", "heading", "label")}
    if "lowercase" in transforms:
        case_rule = "all lowercase where DESIGN.md says lowercase"
    elif "uppercase" in transforms:
        case_rule = "uppercase only where DESIGN.md says uppercase"
    else:
        case_rule = "sentence case"
    prompt = S.get("lovable_prompt") or (
        "Use the attached DESIGN.md as the design system for this project. Use the logo files in logo/ exactly as provided "
        "(no recoloring, no stretching). Colors, type, radii and button specs come from DESIGN.md tokens; do not invent new brand colors. "
        + font_sentences
        + (" Use real product names, prices and images from products.json instead of placeholder products." if products else "")
        + f" Copy should match the voice samples in voice.md: {case_rule}, short, direct.")
    rd = [f"# {brand} brand pack\n",
          f"Measured from {origin} on {today}. Built for private, on-brand mockups and demos shown to {brand}. Do not publish without appropriate permission.\n",
          "Use only publicly accessible, signed-out pages and respect site terms and access restrictions. Use extracted materials only with permission or another valid basis; their owners retain their rights. Do not publish branded demos without appropriate permission. Keep raw browser captures private and review them before sharing or uploading.\n",
          "## What is inside\n",
          "| File | Use |\n|---|---|",
          "| `DESIGN.md` | Design system in the DESIGN.md spec. Paste into Lovable Knowledge, or drop at the project root for tools that read it. |",
          "| `tokens.json` | Same tokens as flat JSON with provenance and confidence. |",
          "| `logo/` | Official logo files as served by the site" + (" (SVG plus a rendered transparent PNG)" if any(k.endswith('_png') for k in logo_files) else "") + ". |",
          ("| `products.json` | Real products, prices and image URLs. |" if products else "| (no products) | Catalog not reachable; use product names from voice.md and the site. |"),
          "| `voice.md` | Verbatim headlines, CTA labels and nav so copy sounds like the brand. |",
          ("| `screenshots/` | Homepage reference shots for layout and photography style. |" if shots else
           "| (no screenshots) | The site blocks headless browsers; open it in a normal browser for layout reference. |" if visual.get("status") == "blocked" else
           "| (no screenshots) | The site was checked live in a browser; no screenshots were saved. Open it for layout reference. |" if visual.get("status") == "passed" else
           "| (no screenshots) | Visual check was skipped. |"),
          "",
          "## Confidence\n",
          "| Area | Status |\n|---|---|"] + [f"| {k} | {v} |" for k, v in conf.items()] + [
          "",
          ("Inferred values (not measured on the site):\n" + "\n".join(f"- {x}" for x in inferred) + "\n") if inferred else "",
          ("Font terms need verification: " + ", ".join(lic_flag)
           + ". Font files are not included. DESIGN.md names recommended similar alternatives; these are design suggestions, not licensing clearance. Verify terms before use and match weight and letter-spacing.\n") if lic_flag else "",
          "## Load into Lovable (3 steps)\n",
          "1. Open the project. Settings, then Knowledge. Paste the full contents of `DESIGN.md` and save.",
          "2. Upload the files in `logo/` (attach them in chat, or add them under `public/brand/`)." + (" Upload `products.json` the same way." if products else ""),
          "3. Paste the prompt below as your first message.\n",
          "```text", prompt, "```", "",
          "For v0, Bolt, Claude or Cursor: attach DESIGN.md and the logo files to the first message and use the same prompt.\n",
          "## Provenance\n",
          f"- Extractor {C.get('extractor_version')} (css-parse tier), {C['source'].get('stylesheets_fetched')} stylesheets, {C['source'].get('rules_parsed')} CSS rules.",
          f"- Logo: {logo_paths['primary'][1]['source'] if logo_paths['primary'][1] else 'supplied'} ({logo_paths['primary'][1]['url'] if logo_paths['primary'][1] else ''})",
          f"- Products: {C.get('product_source', 'none')}",
          f"- Visual check: {visual.get('status', 'skipped')}" + (f". {visual.get('notes')}" if visual.get("notes") else ""),
          ]
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(x for x in rd if x is not None) + "\n")

    # ---------------- zip
    zpath = None
    if a.zip:
        zpath = os.path.join(work, f"{pack_name}-{time.strftime('%Y%m%d')}.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(out):
                for fn in files:
                    fp = os.path.join(root, fn)
                    z.write(fp, os.path.join(pack_name, os.path.relpath(fp, out)))

    # ---------------- summary (five lines the model relays)
    print(f"pack: {out}")
    print(f"logo: {conf['logo']} ({logo_paths['primary'][1]['source'] if logo_paths['primary'][1] else 'supplied'}) -> {', '.join(logo_files.values())}")
    print(f"colors: {conf['colors']} ({len(colors)} tokens) | typography: {conf['typography']} ({', '.join(sorted({v['family'] for v in typo_prov.values()}))})"
          + (f" | font terms unverified: {', '.join(lic_flag)}" if lic_flag else ""))
    print(f"products: {conf['products']} ({len(products)}) | visual check: {conf['visual_check']} | screenshots: {len(shots)}")
    print(f"inferred: {len(inferred)}" + (" -> " + "; ".join(inferred) if inferred else ""))
    if zpath:
        print(f"zip: {zpath} ({os.path.getsize(zpath)//1024} KB)")


if __name__ == "__main__":
    main()
