<?php
/**
 * Plugin Name: DocProof Bridge
 * Description: Immutable, staff-approved DocProof website releases for four managed WordPress pages.
 * Version: 1.0.0
 * Requires PHP: 7.4
 */

defined('ABSPATH') || exit;

final class DocProof_Bridge {
    const VERSION = '1.0.0';
    const PAGES = array('home', 'about', 'books', 'contact');
    const TOKEN_TTL = 86400;
    const MAX_HTML = 2000000;
    const MAX_ASSET = 20000000;

    public static function boot() {
        add_action('rest_api_init', array(__CLASS__, 'routes'));
        add_action('template_redirect', array(__CLASS__, 'serve_managed_page'), 0);
    }

    public static function activate() {
        $admin = get_role('administrator');
        if ($admin) { $admin->add_cap('manage_docproof_site'); }
        self::ensure_pages();
    }

    private static function site_id() {
        return defined('DOCPROOF_BRIDGE_SITE_ID') ? (string) DOCPROOF_BRIDGE_SITE_ID : (string) get_option('docproof_bridge_site_id', '');
    }
    private static function valid_id($value) { return is_string($value) && preg_match('/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/', $value); }
    // WordPress option names are limited. Hash the site and IDs rather than
    // letting an otherwise valid 128-character external ID truncate a key.
    private static function key($kind) { return 'docproof_bridge_' . $kind . '_' . substr(hash('sha256', self::site_id()), 0, 24); }
    private static function release_key($id) { return self::key('release') . '_' . substr(hash('sha256', $id), 0, 32); }
    private static function receipt_key($action, $key) { return self::key('receipt') . '_' . substr(hash('sha256', $action . "\0" . $key), 0, 32); }
    private static function active_key() { return self::key('active'); }
    private static function configured() { return self::valid_id(self::site_id()); }
    private static function request_site($request) { return (string) $request['site_id'] === self::site_id(); }
    public static function permission($request) {
        return self::configured() && self::request_site($request) && current_user_can('manage_docproof_site');
    }

    public static function routes() {
        $base = 'docproof/v1';
        $site = '/sites/(?P<site_id>[A-Za-z0-9_-]+)';
        register_rest_route($base, $site . '/status', array('methods' => 'GET', 'callback' => array(__CLASS__, 'status'), 'permission_callback' => array(__CLASS__, 'permission')));
        register_rest_route($base, $site . '/health', array('methods' => 'GET', 'callback' => array(__CLASS__, 'health'), 'permission_callback' => array(__CLASS__, 'permission')));
        register_rest_route($base, $site . '/releases', array('methods' => 'POST', 'callback' => array(__CLASS__, 'stage'), 'permission_callback' => array(__CLASS__, 'permission')));
        register_rest_route($base, $site . '/releases/(?P<release_id>[A-Za-z0-9_-]+)/preview', array('methods' => 'GET', 'callback' => array(__CLASS__, 'preview_receipt'), 'permission_callback' => array(__CLASS__, 'permission')));
        register_rest_route($base, $site . '/activate', array('methods' => 'POST', 'callback' => array(__CLASS__, 'activate_release'), 'permission_callback' => array(__CLASS__, 'permission')));
        register_rest_route($base, $site . '/rollback', array('methods' => 'POST', 'callback' => array(__CLASS__, 'rollback_release'), 'permission_callback' => array(__CLASS__, 'permission')));
        // These two routes deliberately have no WP login. Their random, expiring,
        // release-scoped token is checked before any body or asset is exposed.
        register_rest_route($base, $site . '/preview/(?P<release_id>[A-Za-z0-9_-]+)/(?P<token>[A-Za-z0-9_-]{32,128})/(?P<page>home|about|books|contact)', array('methods' => 'GET', 'callback' => array(__CLASS__, 'preview_page'), 'permission_callback' => '__return_true'));
        register_rest_route($base, $site . '/preview-assets/(?P<release_id>[A-Za-z0-9_-]+)/(?P<token>[A-Za-z0-9_-]{32,128})/(?P<asset_id>[A-Za-z0-9_-]+)', array('methods' => 'GET', 'callback' => array(__CLASS__, 'preview_asset'), 'permission_callback' => '__return_true'));
        register_rest_route($base, $site . '/assets/(?P<release_id>[A-Za-z0-9_-]+)/(?P<asset_id>[A-Za-z0-9_-]+)', array('methods' => 'GET', 'callback' => array(__CLASS__, 'public_asset'), 'permission_callback' => '__return_true'));
    }

    private static function error($code, $message, $status = 400) { return new WP_Error($code, $message, array('status' => $status)); }
    private static function body($request) { $body = $request->get_json_params(); return is_array($body) ? $body : array(); }
    private static function page_ids() { return get_option(self::key('pages'), array()); }
    private static function managed_page($page, $id) { return get_post_meta($id, '_docproof_managed_site', true) === self::site_id() && get_post_meta($id, '_docproof_managed_page', true) === $page; }
    private static function ensure_pages() {
        if (!self::configured()) { return false; }
        $ids = self::page_ids();
        foreach (self::PAGES as $page) {
            $id = isset($ids[$page]) ? absint($ids[$page]) : 0;
            if ($id && self::managed_page($page, $id)) { continue; }
            if ($id) { return false; } // ownership drift: never claim another page.
            $slug = $page === 'home' ? 'home' : $page;
            $found = get_page_by_path($slug, OBJECT, 'page');
            if ($found && !self::managed_page($page, $found->ID)) { return false; }
            $id = $found ? $found->ID : wp_insert_post(array('post_type' => 'page', 'post_status' => 'publish', 'post_title' => ucfirst($page), 'post_name' => $slug, 'post_content' => '<!-- Managed by DocProof. Edit in DocProof. -->'));
            if (is_wp_error($id) || !$id) { return false; }
            update_post_meta($id, '_docproof_managed_site', self::site_id());
            update_post_meta($id, '_docproof_managed_page', $page);
            $ids[$page] = (int) $id;
        }
        update_option(self::key('pages'), $ids, false);
        if (get_option(self::active_key(), null) === null) { add_option(self::active_key(), 'none', '', false); }
        return true;
    }
    private static function ownership_ok() {
        $ids = self::page_ids();
        foreach (self::PAGES as $page) { if (empty($ids[$page]) || !self::managed_page($page, absint($ids[$page]))) { return false; } }
        return true;
    }

    private static function release($id) { return self::valid_id($id) ? get_option(self::release_key($id), false) : false; }
    private static function storage_root() {
        // The directory must be outside the document root.  An uploads folder
        // plus noindex or an Apache-only .htaccess is not private on every host.
        if (!defined('DOCPROOF_BRIDGE_STORAGE_DIR')) { return false; }
        $root = trailingslashit((string) DOCPROOF_BRIDGE_STORAGE_DIR) . self::site_id();
        if (!wp_mkdir_p($root)) { return false; }
        $deny = trailingslashit($root) . '.htaccess';
        if (!file_exists($deny)) { @file_put_contents($deny, "Deny from all\n"); }
        return $root;
    }
    private static function release_dir($id) { $root = self::storage_root(); return $root ? trailingslashit($root) . $id : false; }
    private static function sha($value) { return hash('sha256', $value); }
    private static function digest($pages, $assets) {
        $hash = hash_init('sha256');
        foreach (self::PAGES as $page) {
            if (!isset($pages[$page]) || !is_string($pages[$page])) { return false; }
            $html = $pages[$page]; if (strlen($html) > self::MAX_HTML) { return false; }
            hash_update($hash, $page . "\0" . $html . "\0");
        }
        usort($assets, function($a, $b) { return strcmp($a['id'], $b['id']); });
        foreach ($assets as $asset) { hash_update($hash, $asset['id'] . "\0" . $asset['sha256'] . "\0"); }
        return hash_final($hash);
    }
    private static function cache_purge() {
        $called = array();
        if (function_exists('wp_cache_flush')) { wp_cache_flush(); $called[] = 'wp_cache_flush'; }
        if (function_exists('wp_rocket_clean_domain')) { wp_rocket_clean_domain(); $called[] = 'wp_rocket_clean_domain'; }
        if (function_exists('w3tc_flush_all')) { w3tc_flush_all(); $called[] = 'w3tc_flush_all'; }
        if (function_exists('wpfc_clear_all_cache')) { wpfc_clear_all_cache(true); $called[] = 'wpfc_clear_all_cache'; }
        if (function_exists('litespeed_purge_all')) { do_action('litespeed_purge_all'); $called[] = 'litespeed_purge_all'; }
        do_action('docproof_bridge_purge', self::site_id());
        return $called;
    }
    private static function public_base() { return untrailingslashit(home_url('/')); }
    private static function preview_base($release, $token) { return rest_url('docproof/v1/sites/' . rawurlencode(self::site_id()) . '/preview/' . rawurlencode($release) . '/' . rawurlencode($token)); }
    private static function asset_base($release, $token = '') {
        $base = rest_url('docproof/v1/sites/' . rawurlencode(self::site_id()) . '/');
        return $token ? $base . 'preview-assets/' . rawurlencode($release) . '/' . rawurlencode($token) : $base . 'assets/' . rawurlencode($release);
    }
    private static function html($release, $page, $token = '') {
        $dir = self::release_dir($release['id']);
        $file = $dir ? trailingslashit($dir) . $page . '.html' : false;
        if (!$file || !is_readable($file)) { return false; }
        $html = file_get_contents($file);
        $base = $token ? self::preview_base($release['id'], $token) : self::public_base();
        return '<!-- docproof-release:' . esc_html($release['id']) . ' digest:' . esc_html($release['digest']) . ' -->' . str_replace(array('__DOCPROOF_BASE__', '__DOCPROOF_ASSETS__'), array($base, self::asset_base($release['id'], $token)), $html);
    }
    private static function no_store() { nocache_headers(); header('X-Robots-Tag: noindex, nofollow, noarchive', true); header('Cache-Control: private, no-store, max-age=0', true); }
    private static function token_ok($release, $token) { return !empty($release['preview_expires_at']) && time() < (int) $release['preview_expires_at'] && !empty($release['preview_token_hash']) && hash_equals($release['preview_token_hash'], hash('sha256', $token)); }
    private static function receipt($release, $key = '') {
        $token = !empty($release['preview_token']) ? $release['preview_token'] : '';
        return array('release_id' => $release['id'], 'state' => $release['state'], 'digest' => $release['digest'], 'idempotency_key' => $key, 'active_release_id' => (string) get_option(self::active_key(), 'none'), 'preview_url' => $token ? self::preview_base($release['id'], $token) . '/home' : '', 'preview_expires_at' => isset($release['preview_expires_at']) ? gmdate('c', $release['preview_expires_at']) : '');
    }

    public static function stage($request) {
        if (!self::ensure_pages() || !self::ownership_ok()) { return self::error('docproof_page_ownership', 'The four managed pages are missing or their ownership changed.', 409); }
        $body = self::body($request); $id = isset($body['release_id']) ? (string) $body['release_id'] : ''; $key = isset($body['idempotency_key']) ? (string) $body['idempotency_key'] : '';
        if (!self::valid_id($id) || !self::valid_id($key) || empty($body['pages']) || !is_array($body['pages']) || !isset($body['assets']) || !is_array($body['assets'])) { return self::error('docproof_invalid_release', 'Release, idempotency key, pages, and assets are required.'); }
        $pages = $body['pages']; $assets = array();
        foreach ($body['assets'] as $asset) {
            if (!is_array($asset) || !self::valid_id(isset($asset['id']) ? $asset['id'] : '') || empty($asset['filename']) || strpos($asset['filename'], '/') !== false || strpos($asset['filename'], '\\') !== false || strpos($asset['filename'], '..') !== false || empty($asset['media_type']) || !preg_match('#^(image/[A-Za-z0-9.+-]+|application/pdf)$#', $asset['media_type']) || empty($asset['bytes_b64'])) { return self::error('docproof_invalid_asset', 'An asset has invalid metadata.'); }
            $bytes = base64_decode($asset['bytes_b64'], true); if ($bytes === false || strlen($bytes) > self::MAX_ASSET) { return self::error('docproof_invalid_asset', 'An asset is not valid or is too large.'); }
            $sha = self::sha($bytes); if (empty($asset['sha256']) || !hash_equals($sha, strtolower($asset['sha256']))) { return self::error('docproof_asset_hash', 'An asset hash does not match its bytes.'); }
            $assets[] = array('id' => $asset['id'], 'filename' => sanitize_file_name($asset['filename']), 'media_type' => $asset['media_type'], 'sha256' => $sha, 'alt' => isset($asset['alt']) ? sanitize_text_field($asset['alt']) : '', 'bytes' => $bytes);
        }
        $digest = self::digest($pages, $assets); if (!$digest || empty($body['digest']) || !hash_equals($digest, strtolower((string) $body['digest']))) { return self::error('docproof_digest', 'The release digest does not match the exact rendered bundle.'); }
        $stage_receipt_key = self::receipt_key('stage', $key); $existing = self::release($id);
        if ($existing) {
            if (!hash_equals($existing['digest'], $digest)) { return self::error('docproof_immutable_release', 'That release ID already belongs to different content.', 409); }
            $past = get_option($stage_receipt_key, false); if ($past) { return $past; }
            // A new authenticated stage idempotency key may renew an expired
            // preview capability without changing immutable release contents.
            if (empty($existing['preview_expires_at']) || time() >= (int) $existing['preview_expires_at']) { $token = wp_generate_password(48, false, false); $existing['preview_token'] = $token; $existing['preview_token_hash'] = hash('sha256', $token); $existing['preview_expires_at'] = time() + self::TOKEN_TTL; update_option(self::release_key($id), $existing, false); }
            $out = self::receipt($existing, $key); add_option($stage_receipt_key, $out, '', false); return $out;
        }
        $root = self::storage_root(); if (!$root) { return self::error('docproof_storage', 'WordPress could not prepare private release storage.', 500); }
        $tmp = trailingslashit($root) . '.' . $id . '.' . wp_generate_password(12, false, false); $final = trailingslashit($root) . $id;
        if (!wp_mkdir_p($tmp . '/assets')) { return self::error('docproof_storage', 'WordPress could not stage the release.', 500); }
        foreach (self::PAGES as $page) { if (!isset($pages[$page]) || !is_string($pages[$page]) || strlen($pages[$page]) > self::MAX_HTML || file_put_contents($tmp . '/' . $page . '.html', $pages[$page], LOCK_EX) === false) { self::remove_dir($tmp); return self::error('docproof_html', 'A rendered page is missing or too large.', 400); } }
        $meta_assets = array(); foreach ($assets as $asset) { $file = $tmp . '/assets/' . $asset['id']; if (file_put_contents($file, $asset['bytes'], LOCK_EX) === false) { self::remove_dir($tmp); return self::error('docproof_storage', 'WordPress could not write a staged asset.', 500); } unset($asset['bytes']); $meta_assets[$asset['id']] = $asset; }
        if (!@rename($tmp, $final)) { self::remove_dir($tmp); return self::error('docproof_storage', 'WordPress could not atomically finish staging.', 500); }
        $token = wp_generate_password(48, false, false); $release = array('id' => $id, 'digest' => $digest, 'state' => 'staged', 'created_at' => time(), 'preview_expires_at' => time() + self::TOKEN_TTL, 'preview_token' => $token, 'preview_token_hash' => hash('sha256', $token), 'assets' => $meta_assets, 'spec' => isset($body['spec']) && is_array($body['spec']) ? $body['spec'] : array());
        if (!add_option(self::release_key($id), $release, '', false)) { self::remove_dir($final); return self::error('docproof_release_race', 'A competing release was staged; query status before retrying.', 409); }
        $out = self::receipt($release, $key); add_option($stage_receipt_key, $out, '', false); return $out;
    }
    private static function remove_dir($dir) { if (!is_dir($dir)) { return; } foreach (scandir($dir) as $file) { if ($file === '.' || $file === '..') { continue; } $path = $dir . '/' . $file; is_dir($path) ? self::remove_dir($path) : @unlink($path); } @rmdir($dir); }

    private static function transition($request, $action) {
        if (!self::ownership_ok()) { return self::error('docproof_page_ownership', 'Managed page ownership changed.', 409); }
        $body = self::body($request); $id = isset($body['release_id']) ? (string) $body['release_id'] : ''; $expected = isset($body['expected_active_release_id']) ? (string) $body['expected_active_release_id'] : ''; $key = isset($body['idempotency_key']) ? (string) $body['idempotency_key'] : '';
        if (!self::valid_id($id) || !self::valid_id($expected) || !self::valid_id($key)) { return self::error('docproof_invalid_transition', 'Release, expected active release, and idempotency key are required.'); }
        $release = self::release($id); if (!$release) { return self::error('docproof_unknown_release', 'The retained release does not exist.', 404); }
        if ($action === 'activate' && $release['state'] !== 'staged' && $release['state'] !== 'active') { return self::error('docproof_not_staged', 'Only staged releases can be activated.', 409); }
        $receipt_key = self::receipt_key($action, $key); $past = get_option($receipt_key, false); if ($past) { return $past; }
        global $wpdb; $option = self::active_key(); $current = (string) get_option($option, 'none'); if ($current === $id) { $release['state'] = 'active'; update_option(self::release_key($id), $release, false); $out = self::receipt($release, $key); add_option($receipt_key, $out, '', false); return $out; }
        if ($current !== $expected) { return self::error('docproof_cas_conflict', 'The active release changed; query status before retrying.', 409); }
        // This is a real SQL compare-and-swap, not a read followed by update_option.
        $changed = $wpdb->query($wpdb->prepare("UPDATE {$wpdb->options} SET option_value = %s WHERE option_name = %s AND option_value = %s", $id, $option, $expected));
        if ($changed !== 1) { return self::error('docproof_cas_conflict', 'The active release changed; query status before retrying.', 409); }
        wp_cache_delete($option, 'options');
        $release['state'] = 'active'; update_option(self::release_key($id), $release, false);
        if ($current !== 'none') { $old = self::release($current); if ($old) { $old['state'] = 'retained'; update_option(self::release_key($current), $old, false); } }
        $out = self::receipt($release, $key); $out['cache_purge'] = self::cache_purge(); add_option($receipt_key, $out, '', false); return $out;
    }
    public static function activate_release($request) { return self::transition($request, 'activate'); }
    public static function rollback_release($request) { return self::transition($request, 'rollback'); }
    public static function status($request) { return array('site_id' => self::site_id(), 'page_ids' => self::page_ids(), 'ownership_ok' => self::ownership_ok(), 'active_release_id' => (string) get_option(self::active_key(), 'none'), 'bridge_version' => self::VERSION); }
    public static function preview_receipt($request) { $release = self::release((string) $request['release_id']); return $release ? self::receipt($release) : self::error('docproof_unknown_release', 'Release not found.', 404); }
    public static function preview_page($request) { if (!self::request_site($request)) { return self::error('docproof_preview_expired', 'Preview link expired or is invalid.', 404); } $release = self::release((string) $request['release_id']); if (!$release || !self::token_ok($release, (string) $request['token'])) { return self::error('docproof_preview_expired', 'Preview link expired or is invalid.', 404); } $html = self::html($release, (string) $request['page'], (string) $request['token']); if ($html === false) { return self::error('docproof_preview_missing', 'Preview page is unavailable.', 404); } self::no_store(); header('Content-Type: text/html; charset=utf-8'); echo $html; exit; }
    private static function serve_asset($release, $asset_id, $token = '') { if (!$release || ($token && !self::token_ok($release, $token)) || (!$token && (string) get_option(self::active_key(), 'none') !== $release['id']) || empty($release['assets'][$asset_id])) { return self::error('docproof_asset_missing', 'Asset not found.', 404); } $asset = $release['assets'][$asset_id]; $path = trailingslashit(self::release_dir($release['id'])) . 'assets/' . $asset_id; if (!is_readable($path)) { return self::error('docproof_asset_missing', 'Asset not found.', 404); } if ($token) { self::no_store(); } else { header('Cache-Control: public, max-age=300'); } header('Content-Type: ' . $asset['media_type']); header('X-Content-Type-Options: nosniff'); readfile($path); exit; }
    public static function preview_asset($request) { if (!self::request_site($request)) { return self::error('docproof_asset_missing', 'Asset not found.', 404); } return self::serve_asset(self::release((string) $request['release_id']), (string) $request['asset_id'], (string) $request['token']); }
    public static function public_asset($request) { if (!self::request_site($request)) { return self::error('docproof_asset_missing', 'Asset not found.', 404); } return self::serve_asset(self::release((string) $request['release_id']), (string) $request['asset_id']); }
    public static function serve_managed_page() { if (!is_page()) { return; } $ids = self::page_ids(); $page = array_search(get_queried_object_id(), $ids, true); if (!$page) { return; } $release = self::release((string) get_option(self::active_key(), 'none')); if (!$release) { status_header(503); wp_die('This DocProof site has no active release.'); } $html = self::html($release, $page); if ($html === false) { status_header(503); wp_die('The active DocProof release is incomplete.'); } status_header(200); header('Content-Type: text/html; charset=utf-8'); header('X-DocProof-Release: ' . $release['id']); echo $html; exit; }
    public static function health($request) {
        $expected = (string) $request->get_param('expected_release_id'); $active = (string) get_option(self::active_key(), 'none'); $findings = array(); $pages = array();
        if (!self::ownership_ok() || $active === 'none') { return array('status' => 'failed', 'findings' => array('managed pages or active release are unavailable'), 'pages' => array()); }
        if ($expected && $expected !== $active) { return array('status' => 'failed', 'findings' => array('active release does not match expected release'), 'pages' => array()); }
        foreach (self::PAGES as $page) { $url = get_permalink(self::page_ids()[$page]); $response = wp_remote_get(add_query_arg('docproof_health', wp_generate_uuid4(), $url), array('timeout' => 10, 'redirection' => 0, 'headers' => array('Cache-Control' => 'no-cache'))); if (is_wp_error($response)) { $findings[] = $page . ': public fetch failed'; $pages[$page] = array('status' => 'inconclusive'); continue; } $body = wp_remote_retrieve_body($response); $marker = '<!-- docproof-release:' . $active . ' '; $ok = wp_remote_retrieve_response_code($response) === 200 && strpos($body, $marker) !== false; $pages[$page] = array('status' => $ok ? 'passed' : 'inconclusive', 'cache_control' => wp_remote_retrieve_header($response, 'cache-control')); if (!$ok) { $findings[] = $page . ': public response did not carry the expected release marker (possibly a cache)'; } }
        if ($findings) { return array('status' => 'inconclusive', 'findings' => $findings, 'pages' => $pages, 'active_release_id' => $active); }
        return array('status' => 'inconclusive', 'findings' => array('Origin returned the expected bundle; external CDN and Bluehost cache propagation require an outside request to verify.'), 'pages' => $pages, 'active_release_id' => $active);
    }
}
register_activation_hook(__FILE__, array('DocProof_Bridge', 'activate'));
DocProof_Bridge::boot();
