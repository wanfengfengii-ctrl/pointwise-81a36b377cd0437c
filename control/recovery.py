import sqlite3

from control.active import load_active
from control.completion import CompletedAspiration, bind_completion
from control.dispatch import issue_attempt
from control.progression import StepResult, deliver_head, last_delivery, reconcile_delivery
from device.port import DevicePort
from device.recording import DeviceError
from device.sessions import SessionTerminal
from orders.batches import load_batch


def resolve_prior_attempt(port: DevicePort, attempt) -> SessionTerminal | None:
    # Sealing is idempotent and preserves a close already chosen at the device.
    port.seal(attempt.session_id)
    return port.terminal(attempt.session_id)


def _unstarted_clearance_block(port: DevicePort, attempt) -> str | None:
    if attempt.job.kind != 'sensitive':
        return None
    # A basis row is written in the same device transaction as the physical
    # start, so its presence proves the original aspiration has begun.
    if port.command_basis(attempt.session_id, attempt.command_id) is not None:
        return None
    clearance = port.clearance()
    return clearance if clearance in ('rinse_unavailable', 'flow_disabled') else None


def resume(db: sqlite3.Connection, port: DevicePort | None, batch_id: str) -> StepResult:
    load_batch(db, batch_id)
    attempt = load_active(db)
    if attempt is None:
        return last_delivery(db, batch_id)
    order_id = attempt.job.order_id
    if attempt.job.batch_id != batch_id:
        return StepResult('pending', detail='device is reserved by another batch')
    reconciled = reconcile_delivery(db, attempt)
    if reconciled is not None:
        return reconciled
    if attempt.completion_number is not None:
        return deliver_head(db, port, CompletedAspiration(attempt))
    if port is None:
        return StepResult('source_read_failed', order_id, detail='device is offline')
    try:
        terminal = resolve_prior_attempt(port, attempt)
    except (DeviceError, sqlite3.Error) as error:
        return StepResult('source_read_failed', order_id, detail=str(error))
    if terminal is None:
        try:
            blocked = _unstarted_clearance_block(port, attempt)
            if blocked == 'rinse_unavailable':
                return StepResult('rinse_unavailable', order_id)
            if blocked == 'flow_disabled':
                return StepResult('source_read_failed', order_id,
                                  detail='shared flow clearance is not initialized')
        except (DeviceError, sqlite3.Error) as error:
            return StepResult('source_read_failed', order_id, detail=str(error))
        return StepResult('pending', order_id)
    outcome = terminal.outcome_for(attempt.command_id)
    if outcome is None or outcome.state == 'cancelled':
        return issue_attempt(db, port, attempt.job, prior=attempt)
    try:
        basis = None
        if attempt.job.kind == 'sensitive':
            basis = port.command_basis(attempt.session_id, attempt.command_id)
            if basis is None and outcome.state == 'completed':
                return StepResult('source_read_failed', order_id,
                                  detail='completed aspiration lacks its clearance basis')
    except (DeviceError, sqlite3.Error) as error:
        return StepResult('source_read_failed', order_id, detail=str(error))
    completion = bind_completion(db, attempt, outcome, basis)
    if completion is None:
        return StepResult('pending', order_id, detail='completion could not be bound')
    return deliver_head(db, port, completion)
