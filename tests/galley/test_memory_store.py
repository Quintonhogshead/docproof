"""G1 — SQLite memory store: WAL, idempotent migrations, seed, export/restore."""

import sqlite3

import pytest

from galley.memory.store import (
    DEFAULT_DB_NAME,
    MIGRATIONS,
    MemoryStore,
    Precedent,
    Ruling,
)

PINNED = "2026-08-23T00:00:00Z"


# ---- open / WAL / migrations ------------------------------------------


def test_open_creates_file_and_sets_wal(tmp_path):
    path = tmp_path / "mem.db"
    with MemoryStore.open(path, now=PINNED) as store:
        assert path.exists()
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        assert store.schema_version == len(MIGRATIONS)


def test_open_on_directory_uses_default_name(tmp_path):
    with MemoryStore.open(tmp_path, now=PINNED) as store:
        assert (tmp_path / DEFAULT_DB_NAME).exists()
        assert store.rulings()  # seeded


def test_migration_idempotent_reopen(tmp_path):
    path = tmp_path / "mem.db"

    store = MemoryStore.open(path, now=PINNED)
    version = store.schema_version
    seeded = store.rulings()
    store.close()

    # Reopen the same file — must not error, bump version, or duplicate seed.
    store2 = MemoryStore.open(path, now="2099-01-01T00:00:00Z")
    assert store2.schema_version == version
    reseeded = store2.rulings()
    assert len(reseeded) == len(seeded)
    assert [r.subject for r in reseeded] == [r.subject for r in seeded]
    store2.close()


def test_reopen_does_not_rerun_migrations(tmp_path):
    """An up-to-date DB re-open applies zero migrations (spy on the list)."""
    path = tmp_path / "mem.db"
    MemoryStore.open(path, now=PINNED).close()

    calls = []
    original = list(MIGRATIONS)

    def spy(idx):
        def _m(conn, now):
            calls.append(idx)
            return original[idx](conn, now)
        return _m

    import galley.memory.store as store_mod

    monkey = tuple(spy(i) for i in range(len(original)))
    store_mod.MIGRATIONS = monkey
    try:
        MemoryStore.open(path, now=PINNED).close()
    finally:
        store_mod.MIGRATIONS = tuple(original)
    assert calls == []  # nothing re-ran


# ---- seed data ---------------------------------------------------------


def test_seed_rulings_present_by_subject(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        subjects = {r.subject for r in store.rulings()}
    assert "NBSP-before-ellipsis" in subjects
    assert "unspaced em dash" in subjects
    assert "number policy" in subjects
    assert "Chicago (CMOS) enforcement" in subjects


def test_seed_uses_injected_timestamp(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        assert all(r.created_at == PINNED for r in store.rulings())


def test_seed_sources(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        by_subject = {r.subject: r for r in store.rulings()}
    assert by_subject["Chicago (CMOS) enforcement"].source == "CMOS"
    assert by_subject["NBSP-before-ellipsis"].source == "house"


# ---- rulings CRUD ------------------------------------------------------


def test_add_ruling_and_query(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        before = len(store.rulings())
        r = store.add_ruling("serial comma", "Use the serial comma.", source="CMOS")
        assert isinstance(r, Ruling) and r.id is not None
        assert len(store.rulings()) == before + 1


def test_add_ruling_duplicate_subject_is_idempotent(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        store.add_ruling("serial comma", "Use it.", source="CMOS")
        before = len(store.rulings())
        again = store.add_ruling("serial comma", "Different text.", source="house")
        assert len(store.rulings()) == before  # no duplicate
        assert again.subject == "serial comma"


# ---- authors -----------------------------------------------------------


def test_add_author_and_query(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        a = store.add_author("Redding", notes="prefers British spelling")
        assert a.id is not None
        names = [x.name for x in store.authors()]
        assert names == ["Redding"]
        assert store.authors()[0].notes == "prefers British spelling"


# ---- precedents --------------------------------------------------------


def test_precedent_insert_and_filter(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        store.add_precedent(
            "missing_comma", "cold dark night", "accept",
            reason="coordinate adjectives", book="Redding", ruled_by="gate",
        )
        store.add_precedent(
            "missing_comma", "big red ball", "reject",
            reason="cumulative adjectives", book="Redding",
        )
        store.add_precedent(
            "hyphenation", "well known", "downgrade", book="Shams",
        )

        assert len(store.precedents()) == 3
        commas = store.precedents(error_type="missing_comma")
        assert len(commas) == 2
        assert all(isinstance(p, Precedent) for p in commas)
        assert {p.ruling for p in commas} == {"accept", "reject"}
        assert store.precedents(error_type="nonexistent") == []


# ---- export / restore round-trip --------------------------------------


def test_export_restore_roundtrip(tmp_path):
    src = tmp_path / "mem.db"
    store = MemoryStore.open(src, now=PINNED)
    store.add_author("Redding", notes="n")
    store.add_precedent("missing_comma", "x y z", "accept", book="Redding")
    store.add_ruling("serial comma", "Use it.", source="CMOS")

    src_rulings = [r.to_dict() for r in store.rulings()]
    src_authors = [a.to_dict() for a in store.authors()]
    src_precs = [p.to_dict() for p in store.precedents()]

    dump = tmp_path / "dump.sql"
    store.export(dump)
    store.close()
    assert dump.exists() and dump.stat().st_size > 0

    dest = tmp_path / "restored.db"
    restored = MemoryStore.restore(dump, dest, now="ignored")
    try:
        assert [r.to_dict() for r in restored.rulings()] == src_rulings
        assert [a.to_dict() for a in restored.authors()] == src_authors
        assert [p.to_dict() for p in restored.precedents()] == src_precs
        assert restored.schema_version == len(MIGRATIONS)
    finally:
        restored.close()


def test_restore_refuses_existing_target(tmp_path):
    src = tmp_path / "mem.db"
    store = MemoryStore.open(src, now=PINNED)
    dump = tmp_path / "dump.sql"
    store.export(dump)
    store.close()

    dest = tmp_path / "restored.db"
    dest.write_text("occupied")
    with pytest.raises(FileExistsError):
        MemoryStore.restore(dump, dest)


# ---- clock injection ---------------------------------------------------


def test_callable_clock_used_for_writes(tmp_path):
    ticks = iter(["t0", "t1", "t2", "t3"])
    with MemoryStore.open(tmp_path / "mem.db", now=lambda: next(ticks)) as store:
        # migration 001 consumed t0, seed migration 002 consumed t1;
        # the next write gets t2.
        a = store.add_author("A")
        assert a.created_at == "t2"


def test_store_never_calls_datetime_now(tmp_path):
    """Guard the determinism contract: the module must not reach for wall clock."""
    import inspect

    import galley.memory.store as store_mod

    src = inspect.getsource(store_mod)
    assert "datetime.now" not in src
    assert "datetime.utcnow" not in src
