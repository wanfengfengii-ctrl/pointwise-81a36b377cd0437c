from device import flow, sessions
from device.aspiration import (
    AspirationOutcome,
    command_outcome,
    enqueue_command,
    finish_command,
    start_command,
)
from device.latch import DeviceSample, read_latest
from device.recording import DeviceError, DeviceRecording
from device.sessions import SessionTerminal


class DevicePort:
    """Explicitly stepped single-device recording, with independently delivered notices."""

    def __init__(self, recording: DeviceRecording, automatic: bool = True):
        self.recording = recording
        self.automatic = automatic
        self.reads_available = True
        self._notices: list[DeviceSample] = []

    def open_session(self) -> int:
        with self.recording.transaction() as db:
            return sessions.open_session(db)

    def submit(self, session_id: int, command_id: str, bottle_id: str,
               requires_clean: bool = False) -> AspirationOutcome | None:
        with self.recording.transaction() as db:
            current = enqueue_command(db, session_id, command_id, bottle_id,
                                      requires_clean=requires_clean)
        if current.state == 'completed':
            return current
        if not self.automatic or current.state == 'cancelled':
            return None
        # A clean-required command may need rinse, start and finish events.
        for _ in range(4 if requires_clean else 2):
            self.tick()
            current = command_outcome(self.recording.db, session_id, command_id)
            if current is not None and current.state in ('completed', 'cancelled'):
                return current
        return None

    def tick(self):
        sample: DeviceSample | None = None
        with self.recording.transaction() as db:
            finished = finish_command(db)
            if finished is not None:
                outcome, sample = finished
            else:
                rinsed = flow.rinse_before_next_command(db)
                if rinsed is not None:
                    # A positive identity is a completed rinse. Zero means a
                    # clean-required head is blocked by dose shortage.
                    return None if rinsed == 0 else rinsed
                outcome = start_command(db)
                if outcome is None:
                    return None
                return outcome
        if sample is not None:
            self.queue_notice(sample)
        return outcome

    def seal(self, session_id: int) -> None:
        with self.recording.transaction() as db:
            sessions.seal_session(db, session_id)

    def close(self, session_id: int) -> None:
        with self.recording.transaction() as db:
            sessions.close_session(db, session_id)

    def terminal(self, session_id: int) -> SessionTerminal | None:
        with self.recording.transaction() as db:
            return sessions.read_terminal(db, session_id)

    def read_latest(self) -> DeviceSample | None:
        if not self.reads_available:
            raise DeviceError('latest device sample is temporarily unavailable')
        return read_latest(self.recording.db)

    def queue_notice(self, sample: DeviceSample) -> None:
        self._notices.append(sample)

    def drain_notices(self) -> tuple[DeviceSample, ...]:
        notices = tuple(self._notices)
        self._notices.clear()
        return notices

    def rinse_history(self) -> tuple[int, ...]:
        with self.recording.transaction() as db:
            return flow.rinse_history(db)

    def clearance(self) -> str:
        with self.recording.transaction() as db:
            return flow.clearance_code(db)

    def command_basis(self, session_id: int,
                      command_id: str) -> tuple[str, int | None] | None:
        with self.recording.transaction() as db:
            return flow.command_basis(db, session_id, command_id)

    def maintenance_contact(self, bottle_id: str | None = None) -> str:
        with self.recording.transaction() as db:
            return flow.maintenance_contact(db, bottle_id)

    def reserve_flow(self, bottle_id: str | None = None,
                     session_id: int | None = None,
                     command_id: str | None = None) -> str:
        with self.recording.transaction() as db:
            return flow.reserve_flow(db, bottle_id, session_id, command_id)

    # Descriptive aliases for callers modeling the temporary bottle hold.
    def reserve_for_bottle(self, bottle_id: str | None = None,
                           session_id: int | None = None,
                           command_id: str | None = None) -> str:
        return self.reserve_flow(bottle_id, session_id, command_id)

    def reserve_for(self, bottle_id: str | None = None,
                    session_id: int | None = None,
                    command_id: str | None = None) -> str:
        return self.reserve_flow(bottle_id, session_id, command_id)

    def reserve(self, bottle_id: str | None = None,
                session_id: int | None = None,
                command_id: str | None = None) -> str:
        return self.reserve_flow(bottle_id, session_id, command_id)

    def hold_for_bottle(self, bottle_id: str | None = None,
                        session_id: int | None = None,
                        command_id: str | None = None) -> str:
        return self.reserve_flow(bottle_id, session_id, command_id)

    def hold_for(self, bottle_id: str | None = None,
                 session_id: int | None = None,
                 command_id: str | None = None) -> str:
        return self.reserve_flow(bottle_id, session_id, command_id)

    def hold_flow(self, bottle_id: str | None = None,
                  session_id: int | None = None,
                  command_id: str | None = None) -> str:
        return self.reserve_flow(bottle_id, session_id, command_id)

    def release_flow(self, bottle_id: str | None = None, session_id: int | None = None,
                     command_id: str | None = None) -> bool:
        with self.recording.transaction() as db:
            return flow.release_flow(db, bottle_id, session_id, command_id)

    def release(self, bottle_id: str | None = None) -> bool:
        return self.release_flow(bottle_id)

    def release_reservation(self, bottle_id: str | None = None) -> bool:
        return self.release_flow(bottle_id)

    def release_for_bottle(self, bottle_id: str) -> bool:
        return self.release_flow(bottle_id)

    def cancel_reservation(self, bottle_id: str | None = None) -> bool:
        return self.release_flow(bottle_id)

    def confirm_no_aspiration(self, bottle_id: str | None = None) -> bool:
        return self.release_flow(bottle_id)
