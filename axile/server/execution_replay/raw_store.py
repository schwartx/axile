"""Fixed native lookups for the bounded replay sidecar; no Python database driver."""

import json

from axile.server._native import _core


class _Row:
    def __init__(self, columns, values):
        self.values = values
        self.mapping = dict(zip(columns, values, strict=True))

    def __getitem__(self, key):
        return self.values[key] if isinstance(key, int) else self.mapping[key]

    def __iter__(self):
        return iter(self.values)

    def keys(self):
        return self.mapping.keys()


class RawIndex:
    """A request-scoped native read snapshot with a fixed lookup vocabulary."""

    def __init__(self, path, progress):
        self.native = _core.ReplayIndex(str(path), progress)

    def rows(self, operation, values=()):
        """Return a bounded page from the fixed native replay catalog."""
        columns, rows = self.native.lookup(operation, json.dumps(values, ensure_ascii=False))
        return [_Row(columns, row) for row in rows]

    def one(self, operation, values=()):
        """Return the first matching replay index record."""
        rows = self.rows(operation, values)
        return rows[0] if rows else None

    def close(self):
        """End the request snapshot and release its native connection."""
        self.native.close()
