<?php
/** DocProof Author Theme is intentionally a thin shell. */
defined('ABSPATH') || exit;
add_action('after_setup_theme', function () {
    add_theme_support('title-tag');
    add_theme_support('post-thumbnails');
});
