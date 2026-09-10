from app.watch import native_corrections as native
from docproof.interior.versions import native_version


def test_native_version_sequence_and_numeric_selection():
    assert native.versioned_name("Sibley - Book 4.indd") == ("Sibley", 4)
    assert native.versioned_name("Sibley - Book 4.5.indd") == ("Sibley", 4.5)
    assert native.next_version(4) == 4.5
    assert native.next_version(4.5) == 5.5
    assert native.native_filename("Sibley", 4.5) == "Sibley - Book 4.5.indd"


def test_native_version_rejects_arbitrary_fraction():
    for value in (0, 4.25, "4.5", True):
        try:
            native_version(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid version {value!r}")
