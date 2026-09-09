"""Public contracts for Author Website Studio.

The models in this module are deliberately shallow.  They are the boundary
between source collection, a model, the deterministic renderer, and the
WordPress bridge; none of those layers gets to pass markup or arbitrary asset
paths to another layer.
"""
from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import (AliasChoices, BaseModel, ConfigDict, Field,
                      field_validator, model_validator)


TemplateId = Literal[
    "literary-journal", "midnight-narrative", "public-voice",
    "cover-gallery", "storybook-studio",
]
TemplateChoice = Literal[
    "auto", "literary-journal", "midnight-narrative", "public-voice",
    "cover-gallery", "storybook-studio",
]

TEMPLATE_IDS: tuple[TemplateId, ...] = (
    "literary-journal", "midnight-narrative", "public-voice",
    "cover-gallery", "storybook-studio",
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


def safe_url(value: str, *, mailto: bool = False) -> str:
    """Accept a public HTTP(S) URL, and optionally a plain mailto address.

    URLs remain strings on the public wire for renderer portability, but they
    are checked at every ingress so a model cannot smuggle executable schemes
    into a rendered anchor.
    """
    value = value.strip()
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if mailto and parsed.scheme == "mailto" and parsed.path and not parsed.netloc:
        return value
    raise ValueError("URL must be an http(s) URL" + (" or mailto URL" if mailto else ""))


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Link(WireModel):
    label: str
    url: str

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return safe_url(value, mailto=True)


class PrimaryCTA(Link):
    pass


class Asset(WireModel):
    """Public metadata only. Asset bytes remain in the project's private store."""
    id: str
    filename: str
    media_type: str
    sha256: str
    alt: str = ""
    approved: bool = False
    focal_x: float = 0.5
    focal_y: float = 0.5

    @field_validator("id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("asset id must be a safe ASCII token")
        return value

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("filename must be a basename")
        return value

    @field_validator("media_type")
    @classmethod
    def image_media_type(cls, value: str) -> str:
        if not value.startswith("image/"):
            raise ValueError("website assets must be images")
        return value

    @field_validator("sha256")
    @classmethod
    def sha(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        return value.lower()

    @field_validator("focal_x", "focal_y")
    @classmethod
    def focal_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("focal point must be between 0 and 1")
        return value


class Author(WireModel):
    name: str
    bio_short: str
    bio: str
    portrait_asset_id: str

    @field_validator("portrait_asset_id")
    @classmethod
    def portrait_id(cls, value: str) -> str:
        if value and not _ID.fullmatch(value):
            raise ValueError("portrait_asset_id must be a safe asset id")
        return value


class Book(WireModel):
    id: str
    title: str
    subtitle: str
    description: str
    hook: str
    cover_asset_id: str
    publication_date: str
    links: list[Link]

    @field_validator("id", "cover_asset_id")
    @classmethod
    def asset_tokens(cls, value: str) -> str:
        if value and not _ID.fullmatch(value):
            raise ValueError("book and asset IDs must be safe ASCII tokens")
        return value


class Praise(WireModel):
    quote: str
    attribution: str


class Contact(WireModel):
    email: str
    headline: str
    message: str
    links: list[Link]

    @field_validator("email")
    @classmethod
    def email_or_empty(cls, value: str) -> str:
        if value and ("@" not in value or any(c in value for c in " <>") ):
            raise ValueError("contact email must be a simple email address")
        return value


class SEO(WireModel):
    description: str


class SiteSpec(WireModel):
    """The complete, renderer-safe public SiteSpec wire shape.

    All wire properties are required.  Empty strings/lists represent an omitted
    optional public section, preserving a stable payload for renderers and the
    WordPress bridge.
    """
    schema_version: Literal[1]
    renderer_version: Literal["1.0"]
    template_id: TemplateId
    author: Author
    headline: str
    intro: str
    primary_cta: PrimaryCTA
    books: list[Book]
    contact: Contact
    praise: list[Praise]
    excerpt: str
    seo: SEO

    @model_validator(mode="after")
    def unique_books(self) -> "SiteSpec":
        ids = [book.id for book in self.books]
        if len(ids) != len(set(ids)):
            raise ValueError("book ids must be unique")
        return self


class QuestionnaireBook(WireModel):
    """An additional public book confirmed in the questionnaire."""
    id: str = ""
    title: str = ""
    subtitle: str = ""
    description: str = ""
    hook: str = ""
    cover_asset_id: str = ""
    publication_date: str = ""
    links: list[Link] = Field(default_factory=list)

    @field_validator("id", "cover_asset_id")
    @classmethod
    def optional_token(cls, value: str) -> str:
        if value and not _ID.fullmatch(value):
            raise ValueError("book and asset IDs must be safe ASCII tokens")
        return value


class Questionnaire(WireModel):
    """Author-editable questionnaire. Private notes are never prompt content."""
    public_name: str = ""
    bio: str = ""
    goal: str = ""
    target_reader: str = ""
    template_id: TemplateChoice = "auto"
    tone: str = ""
    avoid: str = ""
    book_title: str = ""
    book_subtitle: str = ""
    book_description: str = ""
    publication_date: str = ""
    retailer_url: str = ""
    contact_email: str = ""
    contact_url: str = ""
    social_links: list[Link] = Field(default_factory=list)
    approved_praise: list[Praise] = Field(default_factory=list)
    excerpt: str = ""
    private_notes: str = ""
    cover_asset_id: str = ""
    portrait_asset_id: str = ""
    allow_cover_fallback: bool = False
    rights_confirmed: bool = False
    additional_books: list[QuestionnaireBook] = Field(default_factory=list)

    @field_validator("retailer_url", "contact_url")
    @classmethod
    def optional_url(cls, value: str) -> str:
        return safe_url(value, mailto=True)

    @field_validator("cover_asset_id", "portrait_asset_id")
    @classmethod
    def optional_asset_id(cls, value: str) -> str:
        if value and not _ID.fullmatch(value):
            raise ValueError("asset id must be a safe ASCII token")
        return value


class Evidence(WireModel):
    chunk_id: str
    quote: str
    note: str


class ChunkBrief(WireModel):
    chunk_id: str
    summary: str
    premise: str
    themes: list[str]
    audience_signals: list[str]
    spoiler_boundaries: list[str]
    evidence: list[Evidence]


class BookBrief(WireModel):
    schema_version: Literal[1] = 1
    source_fingerprint: str
    chunk_ids: list[str]
    complete: bool
    premise: str
    themes: list[str]
    audience: str
    voice: str
    spoiler_boundaries: list[str]
    evidence: list[Evidence]
    chunks: list[ChunkBrief]

    @model_validator(mode="after")
    def coverage_matches(self) -> "BookBrief":
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        if self.complete and (chunk_ids != self.chunk_ids or
                              len(chunk_ids) != len(set(chunk_ids))):
            raise ValueError("complete book brief must contain every chunk once")
        return self


class SourceBundle(WireModel):
    """Private source snapshot. Only ``public_hubspot`` reaches composition."""
    project_id: str
    hubspot_project_ids: list[str] = Field(default_factory=list)
    # Contract name is hubspot. public_hubspot remains an input alias for early
    # adapters; adapters may also retain a books list for linked projects.
    # The engine consumes only explicit public logical fields.
    hubspot: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("hubspot", "public_hubspot"),
    )
    questionnaire: Questionnaire = Field(default_factory=Questionnaire)
    manuscript: str = ""
    assets: list[Asset] = Field(default_factory=list)
    fingerprint: str = ""

    @field_validator("project_id")
    @classmethod
    def project_token(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("project_id must be a safe ASCII token")
        return value

    @model_validator(mode="after")
    def unique_assets(self) -> "SourceBundle":
        ids = [asset.id for asset in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("asset ids must be unique")
        return self

    @property
    def public_hubspot(self) -> dict[str, Any]:
        """Compatibility spelling for callers built during the first slice."""
        return self.hubspot


class ValidationFinding(WireModel):
    code: str
    message: str
    path: str
    severity: Literal["info", "warning", "error"]


class ValidationReport(WireModel):
    status: Literal["passed", "failed", "unavailable"]
    findings: list[ValidationFinding] = Field(default_factory=list)


class VerificationItem(WireModel):
    path: str
    supported: bool
    message: str
    severity: Literal["info", "warning", "error"]


class VerificationOutput(WireModel):
    findings: list[VerificationItem]
