import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import check_project
from control.workbench import Workbench


BATCH = 'morning'
READINGS = {'bottle-H': 23, 'bottle-L': 17}


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def open_bench(folder, *, flow=None, offline=False):
    work = folder / 'work.sqlite'
    if offline:
        return Workbench.open(work)
    options = {} if flow is None else {'flow': flow, 'readings': READINGS, 'seed': (40, 12)}
    return Workbench.open(work, device_path=folder / 'device.sqlite', automatic=False, **options)


def actions(bench):
    return [(row.bottle_id, row.completion_number) for row in bench.device.recording.read_actions()]


def register(bench, *, high):
    bench.register_batch(BATCH, 'ntu-v1')
    if high:
        bench.submit_orders(BATCH, [('H', 'bottle-H')])
        require(bench.advance(BATCH).status == 'pending', 'H must queue')
        bench.device.tick()
        bench.device.tick()
        result = bench.resume(BATCH)
        require(result.status == 'published', 'H must publish')
        require((result.sample.completion_number, result.sample.raw_code) == (41, 23),
                'H has the wrong original reading')
    first = bench.submit_sensitive(BATCH, 'L', 'bottle-L')
    repeat = bench.submit_sensitive(BATCH, 'L', 'bottle-L')
    require(first.job == repeat.job, 'same registration must preserve queue position')
    try:
        bench.submit_orders(BATCH, [('L', 'bottle-L')])
    except ValueError:
        pass
    else:
        raise AssertionError('ordinary registration accepted a sensitive order ID')


def check_sample(reader, *, number, basis):
    outcome = reader.retrieve_sensitive('L')
    require(outcome is not None, 'L must be available')
    sample = outcome.sample
    require((sample.order_id, sample.batch_id, sample.bottle_id) == ('L', BATCH, 'bottle-L'),
            'L identity changed')
    require((sample.completion_number, sample.raw_code, str(sample.measured.value), sample.measured.unit)
            == (number, 17, '53/8', 'NTU'), 'L reading or source completion changed')
    require((sample.receipt.order_id, sample.receipt.completion_number) == ('L', number),
            'receipt has a different source completion')
    require(outcome.basis == basis, 'wrong clearance basis at actual aspiration')
    return outcome


def check_shortage(bench, expected_rinses):
    for _ in range(2):
        result = bench.advance(BATCH)
        if result.status == 'pending':
            bench.device.tick()
            result = bench.resume(BATCH)
        require((result.status, result.order_id) == ('rinse_unavailable', 'L'), 'L must stay queued')
        require(actions(bench) == [('bottle-H', 41)], 'L consumed liquid despite shortage')
        require(bench.device.rinse_history() == expected_rinses, 'rinse ledger changed')
        require(bench.retrieve_sensitive('L') is None, 'shortage produced a formal sample')


def clean_or_short(folder, *, high):
    with open_bench(folder, flow=(True, 0)) as bench:
        register(bench, high=high)
        if high:
            check_shortage(bench, ())
            return
        require(bench.advance(BATCH).status == 'pending', 'L must queue in manual mode')
        bench.device.tick()
        require(actions(bench) == [('bottle-L', None)], 'clean flow should start L directly')
        bench.device.tick()
        require(bench.resume(BATCH).status == 'published', 'clean L must publish')
        require(bench.device.rinse_history() == (), 'clean L used a rinse dose')
    with open_bench(folder, offline=True) as reader:
        check_sample(reader, number=41, basis=('initial', None))


def reject_receipt(bench):
    bench.db.executescript('''
        CREATE TABLE fixture_allowed (order_id TEXT PRIMARY KEY);
        CREATE TABLE fixture_commit_guard (
            order_id TEXT REFERENCES fixture_allowed(order_id) DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TRIGGER fixture_reject_receipt AFTER INSERT ON receipts WHEN NEW.order_id = 'L'
        BEGIN
            INSERT INTO fixture_commit_guard VALUES (NEW.order_id);
        END;
    ''')


def lost_rinse(folder, *, doses, intervene):
    with open_bench(folder, flow=(True, doses)) as bench:
        register(bench, high=True)
        require(bench.advance(BATCH).status == 'pending', 'L must queue in manual mode')
        bench.device.tick()  # Ignore the reply to the committed rinse.
        first = bench.device.rinse_history()
        require(len(first) == 1 and actions(bench) == [('bottle-H', 41)], 'rinse is a separate step')

    with open_bench(folder) as bench:
        port = bench.device
        require(port.rinse_history() == first, 'reopen lost the committed rinse')
        contact = None
        expected = first
        if intervene:
            before = port.read_latest()
            contact = port.maintenance_contact()
            require(contact in ('applied', 'busy'), 'unknown maintenance result')
            require(port.read_latest() == before, 'maintenance changed the retained sample')
            if contact == 'applied' and doses == 1:
                check_shortage(bench, first)
                return {'maintenance_before_start': contact, 'rinses': first,
                        'result': 'rinse_unavailable'}

        require(bench.resume(BATCH).status == 'pending', 'unstarted L must remain pending')
        if contact == 'applied':
            port.tick()
            expected = port.rinse_history()
            require(len(expected) == 2 and expected[:1] == first and expected[0] != expected[1],
                    'applied contact requires a new rinse identity')
            require(actions(bench) == [('bottle-H', 41)], 'replacement rinse started L early')
        port.tick()
        require(actions(bench) == [('bottle-H', 41), ('bottle-L', None)], 'L must start exactly once')
        before = port.read_latest()
        require(port.maintenance_contact() == 'applied', 'flow reservation survived actual start')
        require(port.read_latest() == before, 'maintenance changed the retained sample')
        port.tick()
        require(actions(bench) == [('bottle-H', 41), ('bottle-L', 42)], 'wrong physical completions')
        reject_receipt(bench)
        require(bench.resume(BATCH).status == 'commit_failed', 'archive commit should fail')
        require(port.rinse_history() == expected, 'unexpected extra rinse')

    with open_bench(folder, offline=True) as reader:
        require(reader.retrieve_sensitive('L') is None and reader.retrieve('L') is None,
                'failed commit exposed L')
    with open_bench(folder) as bench:
        bench.db.execute('DROP TRIGGER fixture_reject_receipt')
        result = bench.resume(BATCH)
        require(result.status == 'published', 'L must be signed from its original completion')
        issued = check_sample(bench, number=42, basis=('rinse', expected[-1]))
        require(result.sample == issued.sample, 'resume and archive disagree')
        require(bench.resume(BATCH).sample == issued.sample, 'resume did not return the first result')
        require(actions(bench) == [('bottle-H', 41), ('bottle-L', 42)], 'resume re-aspirated L')
        require(bench.device.rinse_history() == expected, 'resume repeated the rinse')
    with open_bench(folder, offline=True) as reader:
        final = check_sample(reader, number=42, basis=('rinse', expected[-1]))
        require(final == issued, 'offline handoff changed the original receipt or clearance basis')
    return {'maintenance_before_start': contact, 'rinses': expected, 'result': 'published',
            'order': final.sample.order_id, 'completion': final.sample.completion_number,
            'raw': final.sample.raw_code, 'value': str(final.sample.measured.value),
            'unit': final.sample.measured.unit, 'basis': final.basis,
            'receipt': final.sample.receipt.receipt_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('verify', 'demo'))
    mode = parser.parse_args().mode
    if mode == 'verify':
        original = check_project.main()
        if original:
            return original
        for high in (False, True):
            with TemporaryDirectory() as scratch:
                clean_or_short(Path(scratch), high=high)
    scenarios = [(1, False), (1, True), (2, True)] if mode == 'verify' else [(1, False)]
    for doses, intervene in scenarios:
        with TemporaryDirectory() as scratch:
            result = lost_rinse(Path(scratch), doses=doses, intervene=intervene)
        print(json.dumps({'doses': doses, 'maintenance_requested': intervene, 'actual': result},
                         ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
