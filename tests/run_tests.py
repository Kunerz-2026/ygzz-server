#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the small public test subset.

These tests are self-contained: they do not require game data tables, a game client, or a
device. They live in ``tests/`` and import server modules from ``work/`` via ``PYTHONPATH``.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, 'work')
TESTS = [
    '_account_reset_test.py',
    '_email_account_test.py',
    '_acct_backup_test.py',
    '_suite_gate_test.py',
]


def main():
    env = os.environ.copy()
    env['PYTHONPATH'] = WORK + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['YGZZ_TEST_ISOLATED'] = '1'
    env.setdefault('YGZZ_BACKUP_NO_PORTSCAN', '1')

    failed = []
    for name in TESTS:
        path = os.path.join(ROOT, 'tests', name)
        print('=' * 78)
        print('RUN', name)
        print('=' * 78)
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-B', path],
            cwd=WORK,
            env=env,
        )
        if result.returncode != 0:
            failed.append((name, result.returncode))

    print()
    if failed:
        print('FAILED:')
        for name, code in failed:
            print('  %s (exit %s)' % (name, code))
        return 1
    print('ALL PASS (%d tests)' % len(TESTS))
    return 0


if __name__ == '__main__':
    sys.exit(main())
