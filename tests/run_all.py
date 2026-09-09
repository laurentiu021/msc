"""Ruleaza toate fisierele de test si intoarce cod de iesire nenul la orice eșec.

Fiecare fisier intr-un proces separat, intentionat: testele inlocuiesc atribute
de modul (player.play_next, ytdlp.yt_dlp, executorul), iar intr-un singur proces
un fisier ar putea trece pentru ca altul a lasat starea convenabila. Procese
separate inseamna si ca un test care blocheaza definitiv nu ascunde restul.

    python tests/run_all.py
"""
import glob
import os
import subprocess
import sys

# Consola Windows e cp1252: un mesaj de eșec cu diacritice ar arunca
# UnicodeEncodeError si ar ascunde exact testul care a picat.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TIMEOUT_SEC = 300


def main() -> int:
    files = sorted(glob.glob(os.path.join(HERE, 'test_*.py')))
    if not files:
        print('NICIUN test gasit')
        return 1

    total_pass = total_fail = 0
    broken = []
    for path in files:
        name = os.path.basename(path)
        try:
            proc = subprocess.run(
                [sys.executable, path], cwd=ROOT, capture_output=True,
                text=True, timeout=TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            broken.append(f'{name}: TIMEOUT dupa {TIMEOUT_SEC}s')
            print(f'{name:44s} TIMEOUT')
            continue

        out = proc.stdout + proc.stderr
        passed = out.count('\nPASS ') + out.startswith('PASS ')
        failures = [l for l in out.splitlines() if l.startswith('FAIL ')]
        total_pass += passed
        total_fail += len(failures)
        status = 'OK' if proc.returncode == 0 and not failures else 'FAIL'
        print(f'{name:44s} {passed:3d} pass  {len(failures):2d} fail  {status}')
        for line in failures:
            print(f'    {line}')
        if proc.returncode != 0 and not failures:
            broken.append(f'{name}: iesire {proc.returncode}')
            print(f'    iesire {proc.returncode}, fara linii FAIL:')
            print('\n'.join(f'    {l}' for l in out.strip().splitlines()[-12:]))

    print(f'\n{len(files)} fisiere, {total_pass} teste trecute, {total_fail} esuate')
    for line in broken:
        print(f'PROBLEMA {line}')
    return 1 if (total_fail or broken) else 0


if __name__ == '__main__':
    sys.exit(main())
