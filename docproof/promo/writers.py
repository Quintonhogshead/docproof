"""Turning the model's answer into the two documents the press ships.

Unlike the review and prep writers, which edit a manuscript that already exists,
these write from nothing — so they use python-docx (already a dependency, for the
change log) rather than hand-rolling OOXML. Both files are regenerated from
`promo.json` whenever a human edits the copy in the panel, so they are outputs,
never a source of truth.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document

from .model import SocialPost


def write_teaser(path: str | Path, teaser: str, *, title: str) -> Path:
    doc = Document()
    doc.add_heading(f"{title} — Teaser", level=1)
    # Keep the author's paragraphing: a teaser is occasionally two short paras.
    for block in teaser.split("\n\n"):
        block = block.strip()
        if block:
            doc.add_paragraph(block)
    doc.save(str(path))
    return Path(path)


def write_posts(path: str | Path, posts: list[SocialPost], *,
                title: str) -> Path:
    doc = Document()
    doc.add_heading(f"{title} — Social Posts", level=1)
    for i, post in enumerate(posts, start=1):
        head = f"Post {i}"
        if post.platform:
            head += f" · {post.platform}"
        doc.add_heading(head, level=2)
        doc.add_paragraph(post.text)
        if post.hashtags:
            tags = " ".join(h if h.startswith("#") else f"#{h}"
                            for h in post.hashtags)
            doc.add_paragraph(tags)
    doc.save(str(path))
    return Path(path)
