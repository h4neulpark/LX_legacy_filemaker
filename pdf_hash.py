"""SHA-256 lookup with a reusable, bounded pool and metadata-based caching."""

import hashlib
import multiprocessing
import os
from concurrent.futures import (
    FIRST_COMPLETED, CancelledError, ProcessPoolExecutor, ThreadPoolExecutor, wait,
)
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path


def file_digest(path, cancelled=None):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if cancelled is not None and cancelled():
                raise CancelledError()
            digest.update(block)
    return digest.hexdigest()


def _hash_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_dev, stat.st_ino


def _checked_pdf_digest(path, identity, cancelled):
    if cancelled():
        raise CancelledError()
    if _hash_identity(path) != identity:
        raise OSError("PDF changed before hashing")
    digest = file_digest(path, cancelled)
    if _hash_identity(path) != identity:
        raise OSError("PDF changed while hashing")
    return digest


def _init_hash_worker(generation):
    global _worker_generation
    _worker_generation = generation


def _hash_in_process(path, identity, generation):
    return _checked_pdf_digest(path, identity, lambda: generation != _worker_generation.value)


class PdfHashScanner:
    """The coordinator owns the cache and pool; the UI only submits and polls."""

    # A cold spawn costs more than hashing small batches on the coordinator.
    cold_parallel_bytes = 128 * 1024 ** 2
    warm_parallel_bytes = 16 * 1024 ** 2

    def __init__(self, workers=None):
        cpu_count = getattr(os, 'process_cpu_count', os.cpu_count)() or 1
        self.workers = workers if workers is not None else max(1, min(4, cpu_count - 2))
        self.context = multiprocessing.get_context('spawn')
        self.generation = self.context.Value('Q', 0)
        self.coordinator = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pdf-hash')
        self.pool = None
        self.cache = {}
        self.job = None

    def submit(self, source, identity, paths, digest_ready):
        self.cancel()
        self.job = self.coordinator.submit(
            self._scan, source, identity, tuple(paths), self.generation.value, digest_ready)
        return self.job

    def cancel(self):
        self.generation.value += 1
        if self.job is not None:
            self.job.cancel()

    def close(self, wait=False):
        self.cancel()
        self.coordinator.submit(self._close_pool)
        self.coordinator.shutdown(wait=wait)

    def _close_pool(self):
        if self.pool is not None:
            self.pool.shutdown(cancel_futures=True)
            self.pool = None

    def _scan(self, source, identity, paths, generation, digest_ready):
        def cancelled():
            return self.generation.value != generation

        if cancelled():
            raise CancelledError()
        source_key = _hash_identity(source)
        if source_key[:3] != identity:
            raise OSError("PDF changed before duplicate search")

        missing = {}
        digests = {}
        candidates = {}
        unreadable = 0

        def lookup(path, key):
            cached = self.cache.get(key[0])
            if cached is not None and cached[0] == key:
                digests[key] = cached[1]
            else:
                missing.setdefault(key, path)

        lookup(source, source_key)
        if source_key in digests:
            digest_ready.set_result(digests[source_key])

        for path in paths:
            if cancelled():
                raise CancelledError()
            if path == source:
                continue
            try:
                key = _hash_identity(path)
            except OSError:
                unreadable += 1
                continue
            if key[2] == identity[2]:
                candidates[path] = key
                lookup(path, key)

        for key, digest in self._compute(missing, generation, cancelled):
            if digest is None:
                if key == source_key:
                    raise OSError("Unable to hash the open PDF")
                continue
            if len(self.cache) >= 8192 and key[0] not in self.cache:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key[0]] = (key, digest)
            digests[key] = digest
            if key == source_key:
                digest_ready.set_result(digest)

        if cancelled():
            raise CancelledError()
        if _hash_identity(source) != source_key:
            raise OSError("PDF changed during duplicate search")

        matches = []
        for path, key in candidates.items():
            if key not in digests:
                unreadable += 1
            elif digests[key] == digests[source_key]:
                try:
                    if _hash_identity(path) == key:
                        matches.append(path)
                    else:
                        unreadable += 1
                except OSError:
                    unreadable += 1
        return matches, unreadable

    def _compute(self, missing, generation, cancelled):
        threshold = self.cold_parallel_bytes if self.pool is None else self.warm_parallel_bytes
        if self.workers == 1 or len(missing) <= 1 or sum(key[2] for key in missing) < threshold:
            for key, path in missing.items():
                try:
                    digest = _checked_pdf_digest(path, key, cancelled)
                except OSError:
                    digest = None
                yield key, digest
            return

        if self.pool is None:
            self.pool = ProcessPoolExecutor(
                max_workers=self.workers, mp_context=self.context,
                initializer=_init_hash_worker, initargs=(self.generation,))
        jobs = iter(missing.items())
        pending = {}
        try:
            while not cancelled():
                while len(pending) < self.workers:
                    job = next(jobs, None)
                    if job is None:
                        break
                    key, path = job
                    future = self.pool.submit(_hash_in_process, path, key, generation)
                    pending[future] = key
                if not pending:
                    return
                done, _ = wait(pending, timeout=.04, return_when=FIRST_COMPLETED)
                for future in done:
                    key = pending.pop(future)
                    try:
                        digest = future.result()
                    except OSError:
                        digest = None
                    yield key, digest
            raise CancelledError()
        except BrokenProcessPool:
            self._close_pool()
            raise
        finally:
            for future in pending:
                future.cancel()
