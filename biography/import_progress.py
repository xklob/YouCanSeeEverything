"""Progress within one input, separate from completed-input accounting."""
from datetime import datetime, timezone
from pathlib import Path
import json

from .session_sources import sanitize_visible


PHASES = {
    'starting': 'Starting input',
    'source_started': 'Reading input',
    'source_hash': 'Checking input',
    'bytes_written': 'Retaining archive contents',
    'object_verified': 'Indexing retained records',
    'retained_verification': 'Verifying retained files',
    'source_reconciliation': 'Checking archive reconciliation',
    'source_cleanup_verification': 'Verifying input before cleanup',
    'archive_tail_verification': 'Checking archive end',
    'source_finished': 'Input processed',
}
SOURCE_READ_PHASES = {'source_hash', 'source_reconciliation', 'source_cleanup_verification'}


class InputProgress:
    def __init__(self, source, size):
        self.source = str(source)
        self.size = size
        self.phase = 'starting'
        self.phase_bytes = 0
        self.retained_entries = 0

    def observe(self, update):
        phase = update.get('phase', update.get('event', 'starting'))
        if phase != self.phase:
            self.phase, self.phase_bytes = phase, 0
        if update.get('event') in {'bytes_read', 'bytes_written'}:
            self.phase_bytes += max(0, int(update.get('bytes', 0)))
        if update.get('event') == 'object_verified':
            self.retained_entries += 1

    def checkpoint(self):
        return {'current_source': self.source, 'current_source_bytes': self.size,
                'phase': self.phase, 'phase_bytes': self.phase_bytes,
                'retained_entries': self.retained_entries,
                'updated_at': datetime.now(timezone.utc).isoformat()}


def public_activity(checkpoint, *, now=None):
    """Project only recognized fields; never expose source paths or raw errors."""
    try:
        value = json.loads(checkpoint) if isinstance(checkpoint, str) else checkpoint
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get('phase'), str) or value['phase'] not in PHASES:
        return None
    def number(key):
        item = value.get(key)
        return item if type(item) is int and 0 <= item <= 2**63 - 1 else None
    source = value.get('current_source')
    filename = Path(source.replace('\\', '/')).name if isinstance(source, str) else None
    if filename:
        filename = sanitize_visible(filename[:240])[0]
        filename = ''.join(char for char in filename if char.isprintable())
    stamp, age = None, None
    try:
        parsed = datetime.fromisoformat(value['updated_at'].replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            current = now or datetime.now(timezone.utc)
            delta = (current - parsed).total_seconds()
            if delta >= 0:
                stamp, age = parsed.isoformat(), int(delta)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        pass
    done, total = number('phase_bytes'), number('current_source_bytes')
    # Nested expanded bytes and retained object readbacks have a different
    # denominator. Never present them as a percentage of the source archive.
    progress = None
    if value['phase'] in SOURCE_READ_PHASES and done is not None and total and done <= total:
        progress = {'completed': done, 'total': total, 'fraction': done / total}
    return {'phase': value['phase'], 'label': PHASES[value['phase']], 'filename': filename,
            'retained_entries': number('retained_entries'), 'progress': progress,
            'updated_at': stamp, 'seconds_since_update': age}


def public_cleanup(checkpoint):
    """Explicit completed-input cleanup counts; old checkpoints stay unknown."""
    try:
        value = json.loads(checkpoint) if isinstance(checkpoint, str) else checkpoint
    except (ValueError, TypeError):
        return None
    value = value.get('source_cleanup') if isinstance(value, dict) else None
    if not isinstance(value, dict) or not all(type(value.get(key)) is int and
            0 <= value[key] <= 2**63 - 1 for key in ('removed', 'retained')):
        return None
    return {key:value[key] for key in ('removed', 'retained')}
