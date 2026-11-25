from archive.publication import PublishedSample, SensitivePublished
from archive.store import ArchiveStore
from orders.bottles import validate_identifier


def retrieve(store: ArchiveStore, order_id: str) -> PublishedSample | None:
    validate_identifier(order_id, 'order ID')
    row = store.lookup(order_id)
    return None if row is None else PublishedSample.from_row(row)


def retrieve_sensitive(store: ArchiveStore, order_id: str) -> SensitivePublished | None:
    validate_identifier(order_id, 'order ID')
    row = store.lookup_sensitive(order_id)
    if row is None:
        return None
    sample = PublishedSample.from_row(row[:9])
    basis = (row[9], None if row[10] is None else int(row[10]))
    return SensitivePublished(sample, basis)
