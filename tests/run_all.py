"""Ruleaza toate fisierele de test si intoarce cod de iesire nenul la orice eșec.

Fiecare fisier intr-un proces separat, intentionat: testele inlocuiesc atribute
de modul (player.play_next, ytdlp.yt_dlp, executorul), iar intr-un singur proces
un fisier ar putea trece pentru ca altul a lasat starea convenabila. Procese
separate inseamna si ca un test care blocheaza definitiv nu ascunde restul.

    python tests/run_all.py
"""
import glob
import os
import shutil
import subprocess
import sys

# Consola Windows e cp1252: un mesaj de eșec cu diacritice ar arunca
# UnicodeEncodeError si ar ascunde exact testul care a picat.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TIMEOUT_SEC = 300

# Binarele externe pe care niciun test nu are voie sa le foloseasca. Testele
# trebuie sa fie ermetice: fara retea, fara Discord, si fara procese de media.
FORBIDDEN_BINARIES = ('ffmpeg', 'ffprobe')


def hermetic_env() -> dict:
    """Env-ul copilului, cu FFmpeg scos din PATH.

    Nu e paranoia, e o divergenta care a costat un CI roșu: un test de redare
    inlocuia sursa audio prin `player.discord.FFmpegOpusAudio`, adica un atribut
    al modulului discord, dar fallback-ul ajungea totusi la FFmpeg-ul real. Pe
    mașina de dezvoltare FFmpeg exista, deci suita trecea; pe runner-ul de CI nu
    exista, si noua teste picau cu "ffmpeg was not found". Un test care depinde de
    un binar din PATH nu spune nimic despre cod.
    """
    env = dict(os.environ)
    hidden = set()
    for binary in FORBIDDEN_BINARIES:
        found = shutil.which(binary, path=env.get('PATH', ''))
        while found:
            hidden.add(os.path.dirname(os.path.abspath(found)))
            env['PATH'] = os.pathsep.join(
                p for p in env.get('PATH', '').split(os.pathsep)
                if p and os.path.abspath(p) not in hidden)
            found = shutil.which(binary, path=env['PATH'])
    return env


def main() -> int:
    files = sorted(glob.glob(os.path.join(HERE, 'test_*.py')))
    if not files:
        print('NICIUN test gasit')
        return 1

    total_pass = total_fail = 0
    broken = []
    env = hermetic_env()
    for path in files:
        name = os.path.basename(path)
        try:
            proc = subprocess.run(
                # -B: fara bytecode scris pe disc. Cache-ul .pyc e invalidat pe
                # (mtime, size), iar doua editari rapide care intampla sa lase
                # acelasi numar de octeti — de exemplu inlocuirea unui identificator
                # cu altul de aceeasi lungime, adica exact ce face o verificare prin
                # mutatii — pot pastra un .pyc VECHI. Testul ruleaza atunci alt cod
                # decat cel din fisier, si rezultatul nu inseamna nimic. S-a
                # intamplat: sursa spunea `restore_good_jar()`, iar bytecode-ul
                # incarcat chema `rollback_cookies()`.
                [sys.executable, '-B', path], cwd=ROOT, timeout=TIMEOUT_SEC,
                capture_output=True, env=env,
                # UTF-8 explicit, nu codecul local: altfel diacriticele din
                # mesajul de eșec ajung mojibake exact in linia pe care o citim.
                text=True, encoding='utf-8', errors='replace')
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
