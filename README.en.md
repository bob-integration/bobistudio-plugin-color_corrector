# Color corrector

*[Version française](README.md)*

Corrects the colours of a video flow on the MXL bus. It is part of
[Bobi.Studio](https://github.com/bob-integration/bobistudio), a broadcast orchestrator built on
the ST 2110 / MXL bus.

One input, one output, and settings that apply **live** — no redeployment, no interruption.

---

## The settings

**Luminance**

| | range | default | |
|---|---|---|---|
| Brightness | −1 → +1 | 0 | additive offset |
| Contrast | 0 → 2 | 1 | factor around mid grey |
| Gamma | 0.1 → 10 | 1 | exponent |

**Colour**

| | range | default | |
|---|---|---|---|
| Saturation | 0 → 3 | 1 | 0 renders a grey picture |
| Hue | −180° → +180° | 0° | rotation in the chroma plane |

**Per-channel gamma** — one exponent per component, `gamma_r`, `gamma_g`, `gamma_b`, from 0.1 to
10. This is the setting that fixes a cast an offset cannot: a source whose green climbs too fast
in the highlights, for instance.

**Balance** — nine settings, three channels × three luminance zones:

| | red | green | blue |
|---|---|---|---|
| Shadows | `cb_rs` | `cb_gs` | `cb_bs` |
| Midtones | `cb_rm` | `cb_gm` | `cb_bm` |
| Highlights | `cb_rh` | `cb_gh` | `cb_bh` |

Each runs from −1 to +1. The balance adds an **RGB offset weighted by luminance**: the three zone
weights overlap in a triangle, so a dark pixel mostly receives the "shadows" setting, a bright one
mostly the "highlights", and everything between a continuous blend of the three. Warming the
shadows without touching the highlights, or the reverse, therefore takes a single setting.

> This balance is computed as **three tables indexed by luma**, with no YUV↔RGB round trip: chroma
> cancels out in the conversion, so the offset depends on Y alone. The version that did the round
> trip distorted the picture by roughly ±12 on Y — the shortcut is not merely faster, it is more
> correct. Per-channel gamma keeps its round trip: it is a non-linear operation, with no
> equivalent as a table on Y alone.

---

## What it costs the chain

The corrector works in **slice mode**: it reads its input in bands and publishes the output as it
goes, following the slices of the source grain. Since the correction is **line-local** — each
output pixel depends only on the matching input pixel — the result is **byte-identical** to
whole-frame processing.

That is what allows it to be inserted without the chain losing a frame. A stage that waits for the
complete frame costs one to everything running through it, and that debt shows on no counter: the
stage reports a perfect frame rate.

Three cases fall back to whole-frame processing, by design: an **interlaced** input, an input
**without cadence lock**, and a **height that does not divide** cleanly into bands. Each fallback
is logged — it is never left to guesswork.

---

## Driving it

Every setting is exposed to Bobi.Studio's **macros and triggers**, bounds included: a controller,
a control surface or an automation reaches them without going through the interface.

The container publishes its state and metrics on `:8080`, and accepts settings on `:8082`.

---

## Installing it

**From Bobi.Studio** — the **Catalogue** page, which lists published components and installs them.
Or Settings → Plugins → *Import*, with a `.mxlplugin` package.

**By hand** — clone this repository into `plugins/color_corrector/` of an instance, then reload
the plugin registry.

---

## Reading it

- `script.py` — the plugin, a `str.format` template rendered by the orchestrator and run inside
  the container. **Every literal brace is doubled `{{ }}`**, comments included.
- `control.js` / `control.html` / `control.css` — the settings console.
- `plugin.json` — wiring, config schema, macro surface, control endpoints.
- `meta.json` — the version log: what was broken, what was measured, what the fix cost.
- `help.md` — the article the product's Help page builds from this plugin (in French).

---

## Licence

GPL-3.0-or-later — see [LICENSE](LICENSE). Copyright © 2026 BOBI SAS, France.
