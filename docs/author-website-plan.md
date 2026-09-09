# Author Website Studio — implementation plan

Status: planning only. Prepared September 7, 2026 after inspecting DocProof and the adjacent author portal. No generation, production configuration, or publishing changes are part of this planning task.

## 1. Product decisions

Confirmed by Quinton:

- Add the capability to the DocProof repository.
- Combine the author's HubSpot project, book, and a newly created author questionnaire.
- Keep AI generation model agnostic.
- Automate collection and generation; staff operate the workflow.
- Staff review the preview before publishing. Author approval or an author editing account is not required.
- Generate multiple pages: Home, About, Books, and Contact.
- Host the resulting websites on Bluehost.
- Offer multiple designs that feel polished and premium while remaining reliable for a model to populate.

Recommended architecture: **DocProof generates a structured website specification; a maintained WordPress theme renders it on Bluehost.** Use five curated design families, shared content components, and a small WordPress integration plugin. Staff edit the content and approve releases in DocProof.

WordPress is a recommendation, not an existing user requirement. It fits the surrounding system: the author portal already links to Bluehost and derives an editing URL ending in `/wp-admin/`. WordPress supports reusable block patterns, theme settings, and style variations. Those are useful building blocks for a maintained template collection. The tradeoff is ongoing theme/plugin maintenance. Static files would be simpler to host, but would require a separate editing/handoff convention for the existing portal. See the [portal implementation](/Users/quintonjohnson/Desktop/Atmosphere/author-portal/src/lib/data/books.ts:43), [WordPress theme settings](https://developer.wordpress.org/themes/global-settings-and-styles/), and [block patterns](https://developer.wordpress.org/themes/patterns/).

## 2. Intended workflow

1. Staff links a website project to its author identity, relevant HubSpot book project IDs, and Bluehost destination. The first version supports one featured book and optional additional books.
2. DocProof produces a project-specific questionnaire link. Staff distributes it through the existing author communication process.
3. The author confirms prefilled public facts, adds biography and approved assets, and chooses preferences. Submission is versioned and tied to stable IDs.
4. When the configured website readiness condition and required inputs are satisfied, DocProof automatically snapshots the sources and queues generation.
5. The AI reads the book and public source material, develops the website copy, and recommends a compatible design. DocProof renders that design and two alternatives using the same copy.
6. Automated checks produce a review report. Missing facts, unusable assets, and conflicting source records become specific staff tasks.
7. Staff opens the four-page preview, edits text, changes designs, adjusts image crops, or requests targeted revisions. Staff approves one exact revision and publishes it.
8. DocProof publishes that release to the configured Bluehost WordPress installation, verifies the live pages, and updates the allowed HubSpot website fields. A later source change creates a new draft for review.

Core lifecycle: `waiting_for_inputs → queued → collecting → generating → validating → ready_for_staff_review → approved → publishing → live`.

Keep exceptions (`needs_attention`, `generation_failed`, `publish_failed`) and CRM synchronization state separate. A live website remains live while its next revision is being prepared. A successful deployment followed by a HubSpot outage means “live; CRM update pending,” not “failed website.”

## 3. Premium design system

Build three designs for the first pilot, then complete a five-design launch library:

| Design | Composition and finish | Best fit | Rollout |
|---|---|---|---|
| Literary Journal | Oversized serif masthead, warm paper, fine rules, asymmetric editorial columns, carefully framed cover | Literary fiction, poetry, memoir | Pilot |
| Midnight Narrative | Dark cinematic opening, dramatic cover scale, restrained luminous accent, spacious typography, quiet reading sections | Thriller, fantasy, speculative fiction | Pilot |
| Public Voice | Portrait-led opening, strong grid, confident typography, clear book and speaking links | Nonfiction, experts, public figures | Pilot |
| Cover Gallery | Oversized book covers, editorial captions, generous whitespace, strong featured-release treatment | Multiple books and established catalogs | Launch |
| Storybook Studio | Refined playful type, shaped color fields, restrained paper texture, approved illustrated details | Children's and illustrated books | Launch |

Each family needs its own page composition, spacing rhythm, and content priorities. Color changes alone do not constitute another template. Avoid forcing genres onto authors: the form can express a preference, staff can override, and the model recommends a compatible choice.

Each design supplies:

- All four page layouts, shared navigation/footer, mobile navigation, focus states, and empty-section behavior.
- A small set of compatible hero, book, biography, praise, and contact variants.
- Curated font pairings, approved palettes, spacing scales, image treatments, and density settings.
- Cover-led and typography-led fallbacks when a portrait or atmospheric image is unavailable.
- Subtle hover states and brief entrance transitions, with reduced-motion support and content visible when JavaScript is unavailable.

Visual quality comes from typography, composition, image handling, and restraint. Preserve book-cover proportions and actual artwork. Use portrait focal points so responsive cropping keeps the author visible. Do not fabricate portraits, endorsements, or covers. Optional generated atmospheric art is a later, separately approved capability; the initial templates must look complete using supplied assets.

Give the model explicit budgets for new copy: headline roughly 6–14 words, opening support 25–45, book hook 15–30, description 90–160, short bio 40–70, and expanded bio 120–220. These are draft targets. Canonical names, titles, subtitles, quotations, and dates must remain accurate and complete; layouts accommodate them.

## 4. Pages and questionnaire

Home: author identity, a strong opening, featured book, short biography, optional verified praise, and one primary action.

About: full biography, portrait if supplied, and relevant approved background. Speaking/media material appears only when provided.

Books: featured release plus optional catalog; each book has its actual cover, canonical title, approved description, publication details, and validated buy/preorder links. Keep stable book IDs so a future release does not create a second website for the same author.

Contact: approved contact routes and social links. If the existing WordPress form/mailer setup is available, reuse it and verify delivery. Otherwise the pilot must choose and configure that integration. An approved email/contact link is a valid initial alternative; do not display a form that cannot deliver messages. Newsletter collection, blogging, events management, and ecommerce are later additions unless already provided through a configured external link.

Create a short, mobile-friendly, save-and-resume website questionnaire in DocProof. It is separate from the public website's Contact page. Prefill known facts and show their origin; do not prefill private CRM fields. A revocable, expiring invitation grants access only to that questionnaire and its own uploads, without a general DocProof account.

| Form group | Questions and fields |
|---|---|
| Identity | Public/pen name, pronouns if desired, short/full bio, approved location and credentials |
| Purpose | Featured book, intended reader, primary visitor action, three desired tone words |
| Design | Visual template thumbnails, preferred direction or “choose for me,” colors/styles to avoid, optional reference websites |
| Books | Confirm title/subtitle and release details; approved synopsis, exact praise with attribution, buy/preorder links; optional additional books |
| Assets | Final cover, headshot, optional artwork/logo, image credits, publication permission, crop preference |
| Contact | Public contact method, recipient for a contact form if requested, social profiles, existing mailing-list destination |
| Boundaries | What must stay private, spoiler restrictions, optional specifically approved excerpt, any required wording |

Hidden/server-controlled fields: website project ID, linked HubSpot project IDs, invitation ID, questionnaire schema version, submission ID, and timestamps. The author cannot change the project binding. Store drafts and submitted revisions separately. Require the identity, book confirmation, useful biography facts, primary goal, approved cover or explicit fallback, and intended contact route before generation. A portrait, praise, excerpt, or newsletter is optional.

## 5. Source collection and factual accuracy

Use a durable author/site identity with explicitly linked HubSpot book project IDs. Do not identify a website by surname, filename, email, or the staff member who triggered generation.

Source collection should:

- Fetch an explicit allowlist of HubSpot properties; resolve their real internal names during setup.
- Prefer stored Drive folder/file IDs and the existing `GD Link (Sync)` mapping. Use the current folder resolver only as a fallback that rejects ambiguous matches.
- Select an approved manuscript revision and final visual assets explicitly. Preserve IDs, modified times, checksums, and extraction versions.
- Read all submitted questionnaire fields as structured data. Existing sibling-document readers can support historical imports, but a missing required form must not silently become blank text.
- Separate internal source material from the approved-public facts/assets passed into website composition and publication.

Use field-specific authority rules: staff-confirmed corrections and locked edits are explicit overrides; the submitted form controls public identity, preferences, and contact visibility; approved production records control publication metadata and final assets; the manuscript supports plot, themes, and descriptions. Surface disagreements about identity, title, date, or links for resolution. The book never establishes real-world facts about the author simply because they appear in its narrative.

Every factual content field carries internal source references or staff confirmation. Do not invent reviews, awards, bestseller status, biographies, purchase URLs, or excerpt permission. Omit unsupported optional sections. Never export the full manuscript, private CRM notes, questionnaire response bundle, prompt logs, or internal provenance to WordPress.

HubSpot supports retrieving a project by ID and selecting requested properties. The current repo uses object `0-970`; confirm that object and actual account schema before wiring the website adapter. New API documentation alone does not establish this account's configuration. Use read/schema scopes as needed and restrict writes to configured website properties. [HubSpot Projects API](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/projects/guide).

## 6. Model-agnostic generation

Reuse `Provider.complete_structured`, `build_provider`, schema normalization, credentials, and usage accounting. No provider SDK calls belong inside templates or website routes. Models are configurable by generation role: book extraction, website composition, and verification; a single selected model can perform all three.

Pipeline:

1. Normalize sources and calculate a versioned input fingerprint.
2. Create an evidence-linked book brief covering premise, audience, themes, voice, spoiler boundaries, and usable supporting passages.
3. Compose public content plus design/section choices into `SiteSpec`.
4. Validate structure, facts, links, asset references, and content constraints.
5. Render previews without additional writing calls. Template switching and image crop changes should be deterministic.
6. Apply revision requests to named fields or sections; preserve staff-edited or locked content and show a diff.

For books that fit the configured provider's supported context budget, full-book extraction is reasonable. For longer books, process every chapter/chunk into a cached evidence record and synthesize the brief from complete coverage. Do not silently truncate a manuscript. Track chunk IDs and completion, reserve input/output headroom, and extend the provider capability configuration for context and structured-output limits. Reject unconfigured model capabilities clearly; agnostic does not mean every model can perform every task unchanged.

Use shallow, portable schemas and a bounded repair attempt for malformed model output. Distinguish verification `passed`, `failed`, and `unavailable`; unavailable checks cannot appear as a success. Put a configurable cost ceiling on each site and revision, record usage per stage, and checkpoint completed work. Preserve the existing rule that an unavailable subscription billing lane must not silently switch to a paid API.

Treat source text as data, including instructions embedded in a manuscript or form. The model returns permitted IDs and plain content, never executable HTML/CSS/JavaScript/PHP, plugin choices, arbitrary embeds, or deployment commands. Resolve links and assets through validated registries. Approved reusable sections determine available creative freedom.

## 7. Durable records and staff experience

Create these versioned records:

| Record | Responsibility |
|---|---|
| `WebsiteProject` | Stable author/site identity, linked books/projects, staff permissions, destination, active draft/live revision |
| `SourceBundle` | Private snapshots, field mappings, questionnaire revision, asset approvals and source hashes |
| `BookBrief` | Cached book understanding, evidence references, complete extraction coverage |
| `SiteSpec` | Public copy, pages, compatible section variants, design/version, approved asset and link IDs |
| `WebsiteRevision` | Immutable spec, source fingerprint, template version, edit lineage, validation results |
| `Approval` | Staff identity and approval time bound to the exact revision, assets, template, and destination |
| `Deployment` | Idempotency key, target, staged/active release IDs, health checks, rollback reference, CRM sync state |

Add a Websites panel to the main DocProof app: shared staff queue, missing-input states, four-page preview, mobile/desktop toggle, factual flags, simple field editor, revision instructions, design switcher, approve/publish, and history/rollback. No author-facing generation dashboard is required.

Use explicit staff roles for view/edit/approve/publish and destination configuration. Existing per-user job ownership is insufficient for a shared queue. Website records and release history must survive deletion of transient generation jobs.

Keep stage checkpoints and atomic local persistence, with a lock per website and compare-and-swap revision checks. Give website work a bounded queue/worker so long books and browser checks do not block the existing sequential proofreading worker. Start with low concurrency and measured resource limits.

## 8. Bluehost and WordPress publication

The pilot must confirm the actual Bluehost product, WordPress/PHP versions, single-site versus multisite arrangement, existing theme/plugins, mail setup, credentials, caching, staging capability, domains, and TLS. These were not inspected live for this plan.

Use one WordPress install/destination per author site in the initial design unless the existing hosting topology dictates otherwise. Provisioning a new install, domain, and certificate is a separate setup state; v1 generation and content publishing can be automated once that destination is ready. Do not assume Bluehost exposes an account-wide provisioning or staging deployment API. Its documented staging workflow is a dashboard operation. [Bluehost staging documentation](https://www.bluehost.com/help/article/wordpress-how-to-create-a-staging-site/).

Proposed maintained WordPress packages:

- A lightweight block theme containing the five design families, responsive layouts, approved styles, and renderable components.
- A small DocProof bridge plugin that validates the public release payload, stores immutable releases, supplies the active content to managed page templates, and exposes authenticated stage/preview/activate/rollback operations.

Use four stable WordPress page shells with fixed managed templates. Their shared renderer reads content and design from one active release ID. Additional custom Gutenberg block editing is unnecessary for v1. A private preview selects a particular draft release. Staff edits to these managed pages originate in DocProof in v1; ordinary WordPress content outside that managed surface remains independent. Clearly label the managed content in WordPress so direct edits are not silently overwritten. General WordPress editing and two-way synchronization of these fields are later work. Provision or explicitly claim the four page IDs once; reject unexpected destination/page ownership drift and preserve unrelated existing content.

Publishing sequence:

1. During draft generation, stage only public-shaped content and permitted assets in an immutable bridge release, using content hashes and an idempotency key. This draft is still unpublished and unapproved.
2. Validate the actual WordPress preview across all pages; staff reviews that same renderer and revision. Expiring read-only previews must bypass caches. Draft assets and content require access controls; `noindex` alone is insufficient.
3. Record staff approval of the complete release. On Publish, validate it against current revision, source freshness, asset hashes, renderer/template version, publish settings, and exact destination. Changes require a new preview/approval. Publish never generates new copy.
4. Activate the staged release with a per-site lock and a real atomic compare-and-swap against the expected active release, not a separate read followed by an unconditional update. Resolve the release once per page request so its navigation, content, and design agree.
5. Invalidate the relevant WordPress/Bluehost caches, verify the live page set carries the intended release ID and required interactions work, and record the result. Retain the previous release for explicit rollback.
6. Write the live URL and mapped production state to HubSpot only after live verification succeeds. Retry this write independently if HubSpot is unavailable.

The bridge supplies release behavior that ordinary sequential page updates do not provide. Atomic release selection does not make distributed cache invalidation instantaneous; verify cache behavior in the Bluehost pilot and block publication if the required activation/rollback guarantees cannot be met. On an uncertain network response, query the remote release state before retrying. If activation occurred but health checks fail, attempt rollback, verify it, and show staff the actual state.

Pin and retain renderer/design-pack compatibility as well as content revisions: rolling back a data pointer cannot undo an incompatible theme upgrade. Stage every asset before activation, and retain all assets referenced by the active release or retained rollback releases. Test theme/plugin upgrades separately from author content releases.

Use a dedicated WordPress service identity with only the bridge capabilities it needs and server-held, revocable credentials over HTTPS. WordPress Application Passwords support external API authentication; the bridge must still enforce its own capabilities and site binding. Keep credentials and private source data out of the model prompt and browser. [WordPress authentication](https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/).

## 9. Repo integration and known gaps

| Existing code | Reuse and change |
|---|---|
| [HubSpot client](/Users/quintonjohnson/Desktop/Atmosphere/docproof/app/watch/hubspot.py:141) | Reuse request/error handling and guarded `set_properties`; add project-ID reads, explicit website mappings, bounded retry/backoff |
| [Folder resolver](/Users/quintonjohnson/Desktop/Atmosphere/docproof/app/watch/folders.py:68) | Reuse ambiguity checks; prefer saved source IDs |
| [Questionnaire sibling reader](/Users/quintonjohnson/Desktop/Atmosphere/docproof/app/watch/plan.py:63) | Historical document import only; new form submissions use a structured contract |
| [Manuscript reader](/Users/quintonjohnson/Desktop/Atmosphere/docproof/docproof/promo/ingest.py:36) | Reuse tolerant DOCX extraction; its filename-derived title is not canonical website metadata |
| [Provider protocol](/Users/quintonjohnson/Desktop/Atmosphere/docproof/docproof/providers/base.py:70) | Reuse structured responses and normalized usage; extend capability configuration |
| [Marketing-plan pipeline](/Users/quintonjohnson/Desktop/Atmosphere/docproof/docproof/promo/pipeline.py:251) | Follow preparation/generation/validation separation; add complete long-book coverage and publish-specific verification |
| [Job store](/Users/quintonjohnson/Desktop/Atmosphere/docproof/app/jobs.py:561) | Reuse visibility and accounting; add durable site records, shared staff permissions, checkpoints, and bounded worker integration |
| [Route registry](/Users/quintonjohnson/Desktop/Atmosphere/docproof/app/routes/__init__.py:21) | Register the new staff website API and separately scoped questionnaire routes |

Important production gap: the marketing-plan watcher explicitly skips subfolder mode at [tick.py](/Users/quintonjohnson/Desktop/Atmosphere/docproof/app/watch/tick.py:551). Website discovery must support the production author/book folder arrangement from the first end-to-end slice. Do not simply clone that stage.

The adjacent author portal already maps website URL, status, package, domain, and expiry. Only `website_url` is explicitly pinned to its internal property name in this part of the code; resolve the other internal names and actual enum values during setup. Existing display states refer to author review. Since this workflow uses staff review, retain detailed draft states inside DocProof until a correct CRM mapping is agreed; do not write “Author Review” for staff approval. Keep private preview links out of the public website URL field. [Portal field mapping](/Users/quintonjohnson/Desktop/Atmosphere/author-portal/src/lib/hubspot/properties.ts:39).

Proposed files/directories:

- `docproof/website/`: contracts, source normalization, book briefing, composition, revision, verification, asset handling, pipeline, persistent project/release store, WordPress adapter.
- `app/routes/websites.py`: staff project/revision/preview/approval/publishing API.
- `app/routes/website_forms.py`: narrow invitation, draft, upload, and submission endpoints.
- `app/watch/website.py`: independent website readiness/discovery stage with its own state and CRM mappings.
- `app/static/websites/`: staff panel and author questionnaire assets integrated with the existing app.
- `config/website/`: prompts, field mappings, template manifests, schema/capability versions, and validation thresholds.
- `wordpress/docproof-author-theme/` and `wordpress/docproof-bridge/`: maintained WordPress packages.
- Website-specific tests and visual fixtures under `tests/`.

Update config defaults/schema and package-data declarations together. Include every runtime prompt/static asset in installed-wheel builds. Package WordPress theme/plugin releases explicitly. Keep the current proof/prep/promo features working, and add user-facing version/release notes when implementation ships.

## 10. Delivery sequence and acceptance

1. **Contracts and hosting spike.** Confirm one representative HubSpot project and its exact source IDs, questionnaire fields, website trigger/status mapping, Bluehost destination, and WordPress plugin authentication. Demonstrate private preview, release activation, cache invalidation, and rollback on a test installation. Exit: the integration path is proven before broad template work.
2. **One complete vertical slice.** Build the new questionnaire, structured source bundle, book brief, SiteSpec pipeline, Literary Journal design, four-page WordPress preview, and staff approval. Exit: one representative author can go from inputs to an approved staged release with traceable content.
3. **Premium template library.** Complete Midnight Narrative and Public Voice, the design switcher, sparse-asset fallbacks, editing, and visual checks. Exit: three meaningfully different designs work with the same source content without rewriting facts.
4. **Automated operations.** Add production subfolder discovery, readiness gates, shared staff queue, checkpoints, spend limits, publishing, rollback, and idempotent HubSpot writeback. Resolve contact delivery and destination setup documentation. Exit: duplicate triggers and interrupted runs do not create duplicate sites or publish unapproved content.
5. **Pilot and full library.** Test representative authors with long books, long titles, sparse bios, missing portraits, multiple books, and different genres. Complete Cover Gallery and Storybook Studio and address observed failures. Exit: staff accepts the content and visual results, and all release checks pass.

Required checks:

- Correct project/author/book/form matching; duplicate and ambiguous sources become actionable exceptions.
- Private fields, manuscript bodies, drafts, and credentials never appear in public APIs/assets/HTML.
- All four pages, navigation, purchase/contact routes, image loading, and any enabled submission flow work.
- Every factual claim is sourced or explicitly confirmed. Verification errors are visible and block readiness when material.
- Same spec renders consistently; long canonical names/titles remain readable; absent optional content leaves no empty filler.
- Mobile, tablet, and desktop checks cover keyboard navigation, visible focus, contrast, image descriptions, overflow, and reduced motion.
- SEO basics include page titles/descriptions, canonical URLs, sitemap, social previews, and factual author/book metadata. Production has correct indexing; previews remain private.
- Staff edits survive regeneration; simultaneous edits and stale approvals are detected. Approved content and the published release are identical.
- Repeated triggers, retries, process restarts, partial uploads, unknown publish outcomes, and HubSpot outages recover without duplicate publication or lost history.
- Ordinary proof/prep/promo behavior and installed-package asset loading remain intact.
- Offline provider contract tests cover malformed JSON, truncation, refusal, unsupported capabilities, and usage accounting. A separately enabled pilot checks at least two configured provider families against the same representative content rubric.

Set measurable performance budgets during the hosting spike and check the rendered Bluehost pages against them. Choose the actual default model through the pilot's factual quality, copy quality, latency, and cost results; do not hardcode a provider into the product design.

Remaining setup details are implementation discovery items: exact HubSpot internal field names/ready values, the Bluehost account topology and installed WordPress stack, actual domain provisioning process, mail delivery configuration, and initial model/spend limits. The product direction is otherwise sufficient to begin the first phase.
