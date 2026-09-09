# DocProof WordPress packages

`docproof-bridge` is required. It owns exactly four WordPress pages and serves
the immutable HTML bundle that DocProof already showed staff during review.
`docproof-author-theme` is a minimal maintained shell; the rendered bundle
contains its versioned fixed CSS and JavaScript, so an ordinary theme change
cannot silently alter an approved release.

## Installation and setup

Install both directories as a plugin and theme, then activate the bridge.
Before it is activated on a real site, set the exact site binding in
`wp-config.php`:

```php
define('DOCPROOF_BRIDGE_SITE_ID', 'author-site-123');
define('DOCPROOF_BRIDGE_STORAGE_DIR', '/a/private/path/outside/the/web/root');
```

The activation creates the four managed pages only when their slugs are unused;
it refuses any later ownership drift. Give the WordPress service user a unique
Application Password and only the `manage_docproof_site` capability. The
activation adds that capability to administrators; a production service role
should receive only this capability.

The bridge refuses to stage releases until its storage directory is configured
outside the web root. This is required because previews and draft assets are
private; an uploads directory and `noindex` are insufficient protection.

The bridge accepts only the `/wp-json/docproof/v1/sites/{site_id}` API with
HTTPS Basic Application Password authentication. Releases include exact rendered
HTML, hash-verified bytes, a digest, and an idempotency key. Draft files are
stored in a bridge-controlled private upload directory and exposed only through
an expiring token-scoped preview route with no-store headers. The active release
pointer changes using a database compare-and-swap; retained releases and their
assets remain available for rollback.

The plugin invokes common WordPress cache purgers (`wp_cache_flush`, WP Rocket,
W3 Total Cache, WP Fastest Cache, LiteSpeed) and emits `docproof_bridge_purge`.
Bluehost has no assumed API here. The health endpoint calls the public page URLs
and reports external cache propagation as `inconclusive` until an outside check
confirms it.

## Checks

Run the repository's offline bridge tests from the repository root:

```sh
python -m pytest tests/test_website_wordpress.py
```

For PHP syntax on a host with PHP installed:

```sh
php -l wordpress/docproof-bridge/docproof-bridge.php
php -l wordpress/docproof-author-theme/functions.php
```
