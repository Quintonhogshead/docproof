# Cover Studio texture shelf

Five stocked plates any `ArtSlot`/`ArchetypeArt` may name via `texture_file`
(v2.2 wave, deliverable 5) — loaded by `docproof/cover/textures.py`, rendered
by `docproof/cover/compose.py` (`texture_fit: "cover"` scales the plate to
fill the canvas, `"tile"` repeats it at its own native size). Applied with
the slot's own `opacity`/`blend` like any other art layer, so a texture
tints rather than replaces whatever else is on the cover.

Every plate here is **our own art** — cropped, blurred, desaturated, and
re-toned with Pillow at $0 from images Cover Studio itself generated (and
paid for) on earlier scratchpad job runs. No licensing concerns: nothing
here was sourced from a stock library or a third party.

| File | 1600x2560 | Harvested from | Processing |
|---|---|---|---|
| `wash_sky.png` | yes | `c1_background.png`, job `20260829-b7c3e6` ("Beam Against the Winter Dark" / lighthouse scene concept 1) — sky region, top-left crop clear of the lighthouse itself | Gaussian blur, desaturated (~35%), lightened ~30% toward neutral cream so it reads as a tint under blend, not an imported hue |
| `wash_water.png` | yes | Same source image, `c1_background.png` job `20260829-b7c3e6` — a narrow open-water swath, cropped clear of both the rocky shoreline and the gorse bush at the frame edges | Same recipe as `wash_sky` — blurred, desaturated, lightened toward neutral |
| `glow_band.png` | yes | `c0_background.png`, job `20260829-763a94` (thriller "Code White" corridor concept) — the corridor's own horizontal glow band, cropped clear of the small running-figure silhouette | Light blur only; kept most of its own teal color since the glow itself is the point |
| `amber_smoke.png` | yes | `c0_focal2.png`, job `20260829-ed2999` ("Honeycomb Court" romantasy medallion frame, alternate/unused generation) — a corner of the frame's own black-to-gold radial glow, clear of its fine hexagon/scroll linework | Heavier blur to fully dissolve the little linework the corner still carried, leaving pure warm smoke |
| `paper_warm.png` | yes | `c0_background.png`, job `20260829-82d0f9` (nonfiction "One Calm Hour" concept) — the flat citrus-orange paper field, cropped clear of its own sunburst icon | Very light blur (this plate's whole point is its subtle fiber grain), desaturated and lightened toward a warm neutral tan |

Regenerate or extend the shelf with any Pillow script; there is nothing
special about how these five were made beyond crop box + `GaussianBlur` +
`ImageEnhance.Color` + `Image.blend` toward a near-white or near-neutral
target. Add a new `*.png`/`*.jpg` file here and it is on the shelf the next
time `docproof.cover.textures.TEXTURES` is imported — no code change needed
unless the file needs bespoke processing to get there.

## Generated plates (2026-08-30, gpt-image-2, ~$0.40 one-time, owned outright)

- `laid_paper.png`, `canvas_weave.png`, `watercolor_wash.png`, `parchment.png`,
  `ink_wash.png`, `marble.png`, `dust_grain.png`, `damask_faint.png` — purpose-
  generated texture plates (no scene content, prompts in the session record);
  generated once and stocked forever, the same ownership story as the
  harvested plates above.
