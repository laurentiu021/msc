"""`!help` trebuie sa existe si sa listeze FIECARE comanda inregistrata.

Doua defecte, amandoua vizibile doar din Discord:

1. `!help` nu facea nimic. discord.py primeste `help_command=None` (ajutorul lui
   implicit nu stie de comenzile noastre), iar comanda proprie era inregistrata ca
   `!mhelp`. Cine tasta `!help` — adica oricine — nu primea niciun raspuns si nu
   avea nicio cale sa afle ce comenzi exista.

2. Textul de ajutor era scris de mana, deci putea rămâne in urma. Testul de aici
   il verifica mecanic: o comanda noua fara linie de ajutor picheaza suita.

    python tests/test_help_is_complete.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DISCORD_TOKEN', 'token-de-test')

import bot as bot_mod


def _commands():
    return {c.name: c for c in bot_mod.bot.commands}


def _help_text():
    """Textul embed-ului de ajutor, adunat din titlu, descriere si campuri."""
    embed = _rendered_embed()
    parts = [embed.title or '', embed.description or '']
    parts += [f.name or '' for f in embed.fields]
    parts += [f.value or '' for f in embed.fields]
    parts.append(getattr(embed.footer, 'text', '') or '')
    return '\n'.join(parts)


def _rendered_embed():
    """Construieste embed-ul real, chemand comanda cu un ctx fals."""
    import asyncio

    captured = {}

    class _Ctx:
        message = None
        guild = type('G', (), {'id': 1})()

        async def send(self, *args, **kwargs):
            captured['embed'] = kwargs.get('embed')
            return None

    saved = bot_mod.discord.utils.MISSING  # noqa: F841  (doar ca sa avem discord importat)
    asyncio.run(_commands()['help'].callback(_Ctx()))
    assert captured.get('embed') is not None, 'comanda de ajutor nu a trimis embed'
    return captured['embed']


def test_help_is_registered_under_the_name_people_type():
    names = _commands()
    assert 'help' in names, (
        'nu exista !help; cu help_command=None, nimeni nu poate afla comenzile')
    aliases = set(names['help'].aliases)
    assert 'mhelp' in aliases, 'vechiul !mhelp trebuie sa continue sa functioneze'


def test_the_builtin_help_is_disabled_so_ours_can_take_the_name():
    assert bot_mod.bot.help_command is None, (
        'ajutorul implicit al lui discord.py ar umbri comanda noastra')


def test_every_registered_command_appears_in_the_help():
    text = _help_text()
    missing = [name for name in _commands() if f'!{name}' not in text]
    assert not missing, f'comenzi fara linie de ajutor: {sorted(missing)}'


def test_the_help_does_not_advertise_commands_that_do_not_exist():
    import re

    known = set(_commands())
    for command in _commands().values():
        known |= set(command.aliases)
    advertised = set(re.findall(r'`!([a-z0-9]+)', _help_text()))
    ghosts = advertised - known
    assert not ghosts, f'ajutorul promite comenzi inexistente: {sorted(ghosts)}'


def test_commands_with_arguments_document_them():
    """`!remove` fara argument e o eroare; ajutorul trebuie sa spuna ce ii trebuie."""
    text = _help_text()
    for name, hint in (('play', '<'), ('nplay', '<'), ('seek', '<'),
                       ('remove', '<'), ('move', '<')):
        line = next((l for l in text.splitlines() if l.startswith(f'`!{name}')), None)
        assert line, f'!{name} nu apare in ajutor'
        assert hint in line, f'!{name} nu isi documenteaza argumentele: {line}'


def test_every_command_line_has_a_description():
    import re

    for line in _help_text().splitlines():
        if not line.startswith('`!'):
            continue
        assert re.search(r'`\s+—\s+\S', line), f'linie fara explicatie: {line}'


def test_the_panel_and_the_presence_point_at_the_real_command():
    """Panoul si statusul botului trimiteau la `!mhelp`, care nu era numele principal."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ('bot.py', 'music/ui.py'):
        src = (root / rel).read_text(encoding='utf-8')
        assert 'mhelp' not in src, f'{rel} inca trimite la !mhelp'


if __name__ == '__main__':
    # Consola Windows e cp1252: un mesaj de eșec cu diacritice ar arunca
    # UnicodeEncodeError si ar ascunde exact testul care a picat.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_') or not callable(fn):
            continue
        try:
            fn()
            print(f'PASS {name}')
        except AssertionError as e:
            failed += 1
            print(f'FAIL {name}: {e}')
        except Exception as e:
            # Nu doar AssertionError: un test care CRAPA (RuntimeError,
            # TypeError) opreste altfel fisierul si testele de dupa el nu mai
            # ruleaza deloc, fara sa apara nicaieri ca lipsesc.
            failed += 1
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
