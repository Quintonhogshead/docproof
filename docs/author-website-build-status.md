# Author Website Studio — current build status

September 7, 2026. The user narrowed the immediate deliverable to a local HTML website preview. The Bluehost connection and production publishing are deferred.

## Ready to view

Run `.venv/bin/python tools/preview_website.py` from the repository to regenerate `output/website-preview/index.html`. Open that HTML file directly, or serve that output directory locally. It contains five design families, each with Home, About, Books, and Contact pages, plus a gallery with page and desktop/mobile selectors.

The Mara Ellison demo identity, books, biography and SVG covers are fictional. The demo does not read HubSpot or a manuscript, does not call an AI service, and has no real author contact destination. The generator accepts a public SiteSpec and approved image metadata; the bundled fixture is in `config/website/demo/`.

Renderer/export tests pass. Local relative links and image references were checked, and the Literary Journal homepage, book navigation, phone menu, and phone About layout were visually inspected in the browser. The local preview currently runs at `http://127.0.0.1:8765/` while that temporary server remains running; direct HTML opening works without it.

## Implementation groundwork retained

- Provider-neutral engine, public schemas, complete manuscript chunk coverage, output validation, checkpoints, cost limits and targeted revision support.
- Five deterministic HTML template renderers and portable export.
- Staff studio and questionnaire frontend files.
- WordPress bridge/theme and Python client, with offline client tests and PHP WASM lint.
- Draft service/store/source-adapter/staff-route/automation modules written by the parent agent.

The staff routes and worker have intentionally **not been registered in the live app** yet, and runtime packaging/version changes have not been made. The parent-authored service/store/routes/source adapters still require integration tests and review before enabling. The complete product is not ready for production.

## Next work after design review

1. Apply user feedback to the local designs and replace demo content with the selected author's approved inputs.
2. Test and integrate the WebsiteService and routes: auth/roles, invitations, upload bounds, state/approval concurrency, source refresh, crash recovery and source provenance.
3. Audit renderer/bridge contract end to end, including page URLs, release markers, protected draft assets, known ownership, template version preservation, real staging previews and activation/rollback.
4. Add package-data and app lifecycle wiring and run relevant app regression tests. Configure model capabilities explicitly; the shipped engine does not guess context limits.
5. Only then connect a Bluehost test installation and verify the actual hosting behavior before any live author deployment.
