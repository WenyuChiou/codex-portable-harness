"""Read-only text deployment comparison; ownership always uses raw hashes."""
import hashlib


def compare(source, target):
    expected = source.read_bytes()
    wanted = hashlib.sha256(expected).hexdigest()
    if not target.exists():
        return {"status": "missing", "source_sha256": wanted, "installed_sha256": None}
    actual = target.read_bytes()
    found = hashlib.sha256(actual).hexdigest()
    if actual == expected:
        status = "same"
    else:
        try:
            status = "newline_only" if actual.decode("utf-8").replace("\r\n", "\n") == expected.decode("utf-8").replace("\r\n", "\n") else "different"
        except UnicodeDecodeError:
            status = "different"
    return {"status": status, "source_sha256": wanted, "installed_sha256": found}
