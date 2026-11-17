"""Indexes rebuilt on folder refresh; copy completion performs no path scans."""
from bisect import bisect_left, bisect_right
from pathlib import Path


class SourceIndex:
    def __init__(self, files, processed):
        self.files = files
        self.indices = {}
        self.canonical = []
        self.groups = {}
        for index, path in enumerate(files):
            resolved = path.resolve()
            self.canonical.append(resolved)
            self.indices[path] = index
            self.groups.setdefault(resolved, []).append(index)
        for index, path in enumerate(self.canonical):
            self.indices.setdefault(path, index)
        self.completed = sum(path in processed for path in self.canonical)

    def index(self, path):
        index = self.indices.get(path)
        if index is None:
            index = self.indices.get(Path(path).resolve())
        return index

    def mark(self, path, processed):
        index = self.index(path)
        resolved = self.canonical[index] if index is not None else path.resolve()
        if resolved not in processed:
            processed.add(resolved)
            self.completed += len(self.groups.get(resolved, ()))
        return self.groups.get(resolved, ())

    def next_pending(self, source, processed):
        current = self.index(source)
        if current is None:
            current = -1
        count = len(self.files)
        for offset in range(1, count + 1):
            index = (current + offset) % count
            if self.canonical[index] not in processed:
                return self.files[index]
        return None


class OutputIndex:
    def __init__(self, files):
        self.files = list(files)
        self.keys = [p.name.lower() for p in self.files]

    def index(self, path):
        key = path.name.lower()
        start, stop = bisect_left(self.keys, key), bisect_right(self.keys, key)
        return next((i for i in range(start, stop) if self.files[i] == path), None)

    def remove(self, path):
        index = self.index(path)
        if index is not None:
            self.files.pop(index)
            self.keys.pop(index)
        return index

    def insert(self, path):
        existing = self.index(path)
        if existing is not None:
            return existing, False
        index = bisect_right(self.keys, path.name.lower())
        self.files.insert(index, path)
        self.keys.insert(index, path.name.lower())
        return index, True
