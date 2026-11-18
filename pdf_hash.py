"""SHA-256 and duplicate lookup for source PDFs."""

import hashlib
from concurrent.futures import CancelledError

from pdf_pipeline import file_identity


def file_digest(path, cancel=None):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            digest.update(block)
    return digest.hexdigest()


def checked_pdf_digest(path, identity, cancel):
    if cancel.is_set():
        raise CancelledError()
    if file_identity(path) != identity:
        raise OSError("PDF changed before hashing")
    digest = file_digest(path, cancel)
    if file_identity(path) != identity:
        raise OSError("PDF changed while hashing")
    return digest


def matching_pdf_paths(source, identity, paths, cancel):
    digest = checked_pdf_digest(source, identity, cancel)
    matches = []
    unreadable = 0
    for path in paths:
        if cancel.is_set():
            raise CancelledError()
        if path == source:
            continue
        try:
            candidate = file_identity(path)
            if candidate[2] == identity[2] and checked_pdf_digest(path, candidate, cancel) == digest:
                matches.append(path)
        except OSError:
            unreadable += 1
    if file_identity(source) != identity:
        raise OSError("PDF changed during duplicate search")
    return matches, unreadable
