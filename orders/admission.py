import sqlite3

from orders.batches import load_batch
from orders.bottles import order_identity, validate_order_item, validate_identifier
from orders.queue import append_jobs, find_job


def _submit_kind(db: sqlite3.Connection, batch_id: str,
                 items: list | tuple, kind: str) -> list:
    if not isinstance(items, (list, tuple)):
        raise ValueError('orders must be an ordered list or tuple')
    with db:
        db.execute('BEGIN IMMEDIATE')
        load_batch(db, batch_id)
        requested_ids = []
        known = {}
        new_bottles = {}
        for item in items:
            order_id, bottle_id = validate_order_item(item)
            requested_ids.append(order_id)
            original = known.get(order_id) or find_job(db, order_id)
            if original is not None:
                if order_identity(original) != (batch_id, bottle_id) or original.kind != kind:
                    raise ValueError(f'conflicting order {order_id!r}')
                known[order_id] = original
            else:
                first_bottle = new_bottles.setdefault(order_id, bottle_id)
                if first_bottle != bottle_id:
                    raise ValueError(f'conflicting order {order_id!r}')
        added = append_jobs(db, batch_id, new_bottles.items(), kind=kind)
        known.update((job.order_id, job) for job in added)
        return [known[order_id] for order_id in requested_ids]


def submit_orders(db: sqlite3.Connection, batch_id: str,
                  items: list | tuple) -> list:
    return _submit_kind(db, batch_id, items, 'ordinary')


def submit_sensitive_order(db: sqlite3.Connection, batch_id: str,
                           order_id: object, bottle_id: object):
    order_id = validate_identifier(order_id, 'order ID')
    bottle_id = validate_identifier(bottle_id, 'bottle ID')
    return _submit_kind(db, batch_id, [(order_id, bottle_id)], 'sensitive')[0]
