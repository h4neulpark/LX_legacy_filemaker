"""Sequential multi-copy with durable per-item checkpoints and verified retries."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from pdf_hash import file_digest


@dataclass(frozen=True)
class BatchItem:
    key: str
    lot: str
    stem: str
    plan: object


@dataclass(frozen=True)
class BatchOutcome:
    item: BatchItem
    plan: object
    status: str
    error: str = ''


@dataclass(frozen=True)
class BatchResult:
    outcomes: tuple
    ledger: dict
    error: str = ''
    expected: int = 0

    @property
    def complete(self):
        return (bool(self.outcomes) and not self.error
                and (not self.expected or len(self.outcomes) == self.expected)
                and all(o.status in ('copied', 'skipped') for o in self.outcomes))


def batch_context(source_identity, code, year, output):
    payload = [list(source_identity), code, year, str(Path(output).resolve()), '측량성과도']
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def valid_previous(record, item, source_digest, output):
    if not isinstance(record, dict) or record.get('digest') != source_digest:
        return None
    try:
        path = Path(record['destination'])
        suffix = item.plan.destination.suffix
        stem = path.name[:-len(suffix)] if suffix else path.name
        if path.suffix != suffix or path.parent.resolve() != output.resolve():
            return None
        if stem != item.stem and not (stem.startswith(item.stem+'__') and stem[len(item.stem)+2:].isdigit()):
            return None
        if path.is_file() and file_digest(path) == source_digest:
            return path
    except (KeyError, OSError, TypeError, ValueError):
        pass
    return None


def copy_batch(source, identity, items, ledger, checkpoint, copy_plan, plan_factory, plan_type, events):
    """checkpoint runs in this worker thread, never reads any Tk variable."""
    from pdf_pipeline import file_identity
    outcomes = []
    ledger = dict(ledger)
    try:
        if file_identity(source) != identity:
            raise RuntimeError('원본 PDF가 변경됐습니다. 다시 읽은 뒤 복사하세요.')
        digest = file_digest(source)
        for position, item in enumerate(items, 1):
            events.put(('progress', position, len(items)))
            if file_identity(source) != identity:
                raise RuntimeError('원본 PDF가 변경돼 나머지 복사를 중단했습니다.')
            previous = valid_previous(ledger.get(item.key), item, digest, item.plan.destination.parent)
            if previous is not None:
                outcome = BatchOutcome(item, plan_type(previous), 'skipped')
                outcomes.append(outcome)
                events.put(('outcome', outcome))
                continue
            fresh = plan_factory(item.plan.destination.parent, item.stem, item.plan.destination.suffix)
            if fresh != item.plan:
                outcome = BatchOutcome(item, item.plan, 'failed', '출력 폴더가 변경됐습니다. 다시 복사를 눌러 충돌을 확인하세요.')
                outcomes.append(outcome)
                events.put(('outcome', outcome))
                continue
            # Persist the intended destination first. A crash after the file is
            # written can then be recognized by content, even without a success checkpoint.
            ledger[item.key] = dict(destination=str(item.plan.destination), digest=digest, status='pending')
            checkpoint(ledger)
            try:
                copy_plan(source, item.plan)
                if file_identity(source) != identity or file_digest(item.plan.destination) != digest:
                    raise RuntimeError('복사 중 원본이 변경됐거나 복사본 검증에 실패했습니다.')
            except Exception as exc:
                ledger[item.key]['status'] = 'failed'
                outcome = BatchOutcome(item, item.plan, 'failed', str(exc))
            else:
                ledger[item.key]['status'] = 'copied'
                outcome = BatchOutcome(item, item.plan, 'copied')
            outcomes.append(outcome)
            events.put(('outcome', outcome))
            checkpoint(ledger)
        if file_identity(source) != identity:
            raise RuntimeError('원본 PDF가 변경됐습니다. 복사 결과를 확인하세요.')
    except Exception as exc:
        return BatchResult(tuple(outcomes), ledger, str(exc) or type(exc).__name__, len(items))
    return BatchResult(tuple(outcomes), ledger, expected=len(items))
