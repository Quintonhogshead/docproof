# Author Website Studio: benchmark and proposed upgrade

Reviewed September 8, 2026. This is an assessment and implementation specification, not a record of completed generator changes.

## Decision

Exceeding the visual quality and usability of Atmosphere's current Premium examples is a realistic target. The existing generator is a useful starting point, but it cannot yet consistently match their range of functionality. A polished author site, a multi-book media site, and a photography store are different product scopes.

Judge the new output against the strongest relevant examples across all three packages. Package labels alone do not establish visual quality or reveal the full service agreement, maintenance, hosting, or original project scope.

## Evidence and limits

All eleven supplied homepages were opened in a browser, their available page content inspected, and their initial desktop compositions viewed at 1280 × 720. Selected Premium pages were also scrolled. The existing Literary Journal demo was viewed locally, and the renderer, public schemas, generation pipeline, and build-status document were read.

These are observations from a bounded review. Forms, checkouts, email delivery, every interior page, and accessibility conformance were not tested. No traffic, conversion, or performance measurements were collected. Initial image loading and entrance animations are not treated as permanent defects. A requested phone viewport override did not apply to the inspected tabs; mobile behavior therefore remains unverified. No live sites were modified.

## Reference observations

| Package | Reference | Strength to learn from | Opportunity or generator requirement |
| --- | --- | --- | --- |
| Basic | [Natalie M. Dossett](https://nataliemdossett.com/) | Cover, horse imagery, and restrained colors create a coherent identity; substantial awards, events, and retail options. | Separate upcoming from past events and make release status current. The hero still displays a September 2024 availability date. |
| Basic | [Albert Sipes](https://asipesauthor.com/) | Prominent book mockup and an organized sales page with contact form. | Shorter introductory copy. The page exposes reviews attributed to “John Doe,” and its hero review link targets `#`. Flag placeholders and validate action destinations. |
| Basic | [Ana from Sweden](https://anafromsweden.com/) | Consistent monochrome direction, book mockups, video, retailer options, and blog links. | Ensure the main reader action is prominent alongside the opening copy. Features and visual identity already extend beyond a minimal brochure. |
| Advanced | [Hard Things](https://hardthingsbook.com/) | A distinctive runner-and-mountain composition supports the book's subject; trailer, excerpts, and speaking topics add purpose. | Header and excerpt links mix this domain with `marcauthor.com`. Flag cross-domain internal-looking links for review; this review did not establish whether those destinations are intentional or working. |
| Advanced | [Judd Midlam](https://cjmidlam.com/) | A consistent dark palette and literary treatment, with book, author, blog, and contact destinations. | The homepage includes the heading “QUOTE FROM THE BOOK HERE.” Block template instructions from appearing in released output. |
| Advanced | [Gillian Lynn Katz](https://gillianlkatzauthor.com/) | Strong cover prominence, clear retail choices, backlist, praise, and media navigation. | Reduce repeated purchasing copy. One in-body excerpt action targets `#`, although another links to an actual excerpt page. |
| Advanced | [Wink](https://readwink.com/) | Strong visual relationship to the cover; clear reader-community invitation and distinctive typography. | Reconcile release language: purchase/available-now content appears alongside early-reader messaging. This may be intentional, so flag it for editorial review rather than automatically rewriting it. |
| Premium | [Alain Brousseau](https://alainbrousseau.com/) | Dedicated author, books, praise, blog, and contact navigation; clear series potential. | At the observed desktop width, the opening title breaks “Academy” across lines. Two newsletter overlays appeared simultaneously during the review. Several future-book footer links and newsletter privacy links target `#`. |
| Premium | [Dick Anderson Creations](https://dickandersoncreations.com/) | Authentic landscape photography, writing and music collections, search, cart, and photography sales. | Give the three creative disciplines concise, distinct entry points. Its commerce and media scope needs dedicated modules; a four-page static generator does not replace the store. Checkout operation was not tested. |
| Premium | [m. a. Arana](https://booksbymaarana.com/) | Recognizable fantasy identity, multiple books, video, audio samples, retail choices, events, and excerpts. | Organize the catalogue around a featured release and series order; separate upcoming/past events. An August event still has a “will share later” link, and the biography's X link points to Instagram. |
| Premium | [Nathan Thompson](https://nathanthompsonauthor.com/) | Clear purchase/excerpt choices and a historical-thriller premise; contact form and additional book sections appear on scroll. | Make artwork and concise, distinctive copy carry more of the story. Test all sections after entrance animations and with reduced motion; do not judge transient loading frames as the finished design. |

## What constrains our current generator

- `docproof/website/models.py`: `SiteSpec` contains one template choice, author/book copy, one primary action, praise, an excerpt, and a single SEO description. It has no design brief, section plan, events, newsletter configuration, per-book pages, or media modules. Public assets currently accept images only.
- `docproof/website/render.py`: five template families have different hero and inner-page treatments, but palettes, fonts, page choices, and section composition remain fixed in code. The homepage centers on the first book. The contact page supplies email/social links rather than a working form. Every rendered image, including the main cover, is marked for lazy loading.
- `docproof/website/pipeline.py`: the engine already separates evidence from public copy, retains approved quotes and excerpts, and supports revisions. This is a foundation worth retaining. Better prompting alone cannot produce layouts or features absent from the public schema and renderer.
- `docs/author-website-build-status.md`: staff routes, worker, and publishing integration remain unfinished. A visually improved export should not be described as a complete production workflow.

## Proposed upgrade

### 1. Add a structured design brief

Use author goals, genre, manuscript evidence, the approved cover, available photography, publication status, and catalogue size to select an appropriate direction. Store the intended visitor action, type pairing, accessible palette, image treatment, page plan, section order, and allowed layout variants.

Keep the model provider-neutral. Let it choose validated design options and write evidence-grounded content. Keep HTML, scripts, integrations, and layout behavior in tested components. Staff should be able to change a direction without regenerating or altering approved facts.

### 2. Develop adaptable design families

Start by raising the existing families to a consistent standard: literary/editorial, cinematic fiction, portrait-led nonfiction, catalogue/gallery, and illustrated/children's. Provide several compatible hero compositions, section arrangements, type pairings, and asset treatments within each family.

Adapt composition to the material. A first novel needs a clear book introduction; a series needs reading order; an established speaker needs topics and booking; a visual artist needs collections. Missing photography should trigger an intentional typographic layout. Short content should not create empty sections. Long titles should change layout or scale instead of splitting ordinary words arbitrarily.

Preserve approved covers and portraits. Build book mockups from supplied cover artwork without regenerating its lettering. Use author-owned or licensed imagery, with optional original decorative artwork that does not invent an author's identity or claim to depict a real event.

### 3. Add reader features as supported modules

First group: book detail pages, a readable excerpt view, retailer choices by format, series order, approved praise, press/download resources, upcoming events with an archive, and newsletter/contact integrations with real success and error states.

Second group: audio/video with poster images and deferred loading, maintained journals/blogs, speaking pages, and photo galleries. Include modules only when content and an operational destination are available. Do not render inactive form controls or pretend a signup was delivered.

Treat direct commerce as a separately implemented capability with products, pricing, inventory where relevant, payment-provider setup, delivery, and order handling. Dick Anderson's store is a useful benchmark for that scope, not an automatic promise for the next brochure-site release.

### 4. Make editorial and functional checks part of generation

Before staff review, inspect public output for template labels, placeholder names, empty anchors, missing assets, mismatched social destinations, unexplained domain changes, repeated copy, conflicting release language, and stale event invitations. Distinguish verified failures from editorial warnings.

Keep factual claims, quotations, awards, and testimonials tied to approved inputs. The existing separation of approved praise and generated copy should remain. Missing praise should omit that section, not cause the model to manufacture an endorsement.

Use actual rendered screenshots plus deterministic checks to identify layout problems, then permit bounded revisions. Visual model feedback can assist; it does not replace human preference testing or prove factual correctness.

### 5. Complete publishing and maintenance

Version the expanded schema and renderer together. Preserve existing v1 exports and migrate deliberately. Update route generation, preview navigation, portable/shareable exports, and WordPress staging together so a dynamic page plan works in every destination.

Finish authentication, source refresh, author review, release approval, staging, activation, and rollback checks already listed in the build-status document. Preserve staff edits during regeneration. Support routine book, retailer, event, and biography updates without rebuilding the site's identity.

## Acceptance standard

The following are proposed targets, not claims about current output or measured failures on reference sites:

1. Compare three complete examples using different content conditions: a long-title single-book launch, a multi-book author with media/events, and a literary or memoir author with limited imagery. Also test missing optional content and long names.
2. Review every generated page at 320, 390, 768, 1280, and 1440 CSS pixels. Require readable type, no unintended horizontal scrolling, usable navigation, intact book artwork, and visible primary actions without unnecessary introductory text.
3. Validate every generated internal link/anchor and referenced asset. Verify external destinations and integration setup; distinguish network failures from confirmed broken links. Test contact and newsletter delivery in a controlled test environment.
4. Aim for WCAG 2.2 AA through automated checks plus keyboard, focus, zoom/reflow, contrast, form, and reduced-motion review. An automated score alone is not a conformance claim. See the [W3C quick reference](https://www.w3.org/WAI/WCAG22/quickref/).
5. Budget images and scripts, prioritize the main visual, reserve image dimensions, and defer nonessential media. Target good Core Web Vitals: LCP at or below 2.5 seconds, INP at or below 200 ms, and CLS at or below 0.1, evaluated at the 75th percentile of real visits separately for mobile and desktop. Use repeatable lab checks before launch; lab results do not establish field performance. See [Google's Web Vitals guidance](https://web.dev/articles/vitals).
6. Generate appropriate page titles/descriptions, canonical URLs, social previews, sitemap, and validated structured data where supported by the content. Do not promise ranking or sales improvements without evidence.
7. Run an unbranded side-by-side review with staff and representative authors/readers. Evaluate visual identity, readability, ease of finding and sampling a book, and confidence in the next action. Require the new examples to be preferred on those dimensions without losing essential functionality.

## Suggested implementation order

1. Expand the data/design contract and implement one excellent complete family with book detail/excerpt support. Keep its output portable and usable in the existing review gallery.
2. Prove it with the three content conditions above, then apply the tested component rules to the other design families.
3. Integrate newsletter/contact, events, and media modules with verified destinations and states.
4. Complete application and WordPress staging integration, verify hosting behavior, and only then assess the full production offering against Premium.

The first milestone should be three convincing complete sites produced through the same generator. That provides stronger evidence than a larger theme selector or one hand-polished homepage.
