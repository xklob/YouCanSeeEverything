"""A small sequence that spills event bodies to disk instead of retaining them."""

from __future__ import annotations

import json
import tempfile
from array import array
from collections.abc import Iterable, Iterator, Sequence


class EventCollection(Sequence):
    """List-compatible reads, with at most 8 MiB of serialized bodies in RAM.

    Offsets and conservative duplicate counters are small relative to imported
    message bodies. The temporary backing file is removed when closed or freed.
    """

    def __init__(self):
        self._data = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        self._offsets = array("Q")
        self._occurrences: dict[str, int] = {}
        self.has_truncated_body = False
        self.has_truncated_document = False
        self.has_local_datetime = False
        self.has_recurrence = False

    def append(self, event: dict):
        metadata = event["metadata"]
        base = metadata.get("record_fingerprint")
        if base and "fingerprint_occurrence" not in metadata:
            ordinal = self._occurrences.get(base, 0)
            self._occurrences[base] = ordinal + 1
            metadata["record_fingerprint"] = f"{base}:occurrence:{ordinal}"
            metadata["fingerprint_occurrence"] = ordinal
        self.has_truncated_body |= bool(metadata.get("body_truncated"))
        self.has_truncated_document |= "[Document extraction limit reached;" in event["body"]
        self.has_local_datetime |= bool(metadata.get("timezone_unknown"))
        self.has_recurrence |= metadata.get("recurrence_expanded") is False
        self._data.seek(0, 2)
        self._offsets.append(self._data.tell())
        self._data.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")

    def extend(self, events: Iterable[dict]):
        for event in events:
            self.append(event)

    def __len__(self):
        return len(self._offsets)

    def __iter__(self) -> Iterator[dict]:
        # An independent offset for each step permits nested reads/indexing.
        for offset in self._offsets:
            self._data.seek(offset)
            yield json.loads(self._data.readline())

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self))) ]
        self._data.seek(self._offsets[index])
        return json.loads(self._data.readline())

    def close(self):
        self._data.close()
