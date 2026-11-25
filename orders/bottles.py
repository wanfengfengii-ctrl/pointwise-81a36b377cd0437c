from dataclasses import dataclass, field


@dataclass(frozen=True)
class BottleJob:
    order_id: str
    batch_id: str
    bottle_id: str
    position: int
    # Internal registration mode, excluded from the historical job identity.
    kind: str = field(default='ordinary', compare=False)


def validate_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be nonblank text')
    return value


def validate_order_item(item: object) -> tuple[str, str]:
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        raise ValueError('each order must contain an order ID and a bottle ID')
    return (
        validate_identifier(item[0], 'order ID'),
        validate_identifier(item[1], 'bottle ID'),
    )


def order_identity(job: BottleJob) -> tuple[str, str]:
    return job.batch_id, job.bottle_id
