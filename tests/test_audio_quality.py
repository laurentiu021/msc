"""Regresii pentru "botul se aude bazait".

Cauza a fost o particularitate a lui discord.py, nu a lui YouTube:
`FFmpegOpusAudio.from_probe` isi ia bitrate-ul din `_probe_codec_native`, care
face `bitrate = max(round(bit_rate / 1000), 512)` (player.py:677 in 2.7.1) — un
`max` unde intentia era evident un `min`. Deci CERE lui FFmpeg minim 512 kbps.

Cat timp YouTube da opus, nu se aude nimic: codec-ul e 'opus', discord.py pune
`-c:a copy` si FFmpeg ignora `-b:a`. Dar cand nu mai da opus — experimentul
SABR-only lasa doar AAC — se intra pe reencodare si FFmpeg produce 512 kbps
pentru un canal Discord de 64.

Ruleaza fara pytest, fara retea, fara Discord:
    python tests/test_audio_quality.py
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music import utils as utils_mod
from music.resolve import format_summary
from music.utils import (DEFAULT_CHANNEL_KBPS, MAX_ENCODE_KBPS,
                         encode_bitrate_kbps, make_opus_source)


class _FakeChannel:
    def __init__(self, bitrate):
        self.bitrate = bitrate


class _FakeOpusAudio:
    """Inregistreaza ce ar fi ajuns la FFmpeg, fara sa porneasca niciun proces."""

    made = []
    probe_result = ('opus', 512)

    def __init__(self, source, *, bitrate=None, codec=None, **opts):
        _FakeOpusAudio.made.append(
            {'source': source, 'bitrate': bitrate, 'codec': codec, 'opts': opts})

    @classmethod
    async def probe(cls, source, **kwargs):
        return cls.probe_result


class _FakeDiscord:
    FFmpegOpusAudio = _FakeOpusAudio


def _build(codec, channel_bitrate=64000, **opts):
    """Construieste sursa cu discord.py inlocuit si intoarce ce a primit FFmpeg."""
    _FakeOpusAudio.made = []
    _FakeOpusAudio.probe_result = (codec, 512)
    saved = utils_mod.discord
    utils_mod.discord = _FakeDiscord
    try:
        asyncio.run(make_opus_source('piesa.webm', _FakeChannel(channel_bitrate),
                                     **opts))
    finally:
        utils_mod.discord = saved
    assert len(_FakeOpusAudio.made) == 1, _FakeOpusAudio.made
    return _FakeOpusAudio.made[0]


def test_a_non_opus_source_is_never_encoded_at_512k():
    """Exact bug-ul: AAC de la YouTube reencodat la 512 kbps intr-un canal de 64."""
    made = _build('aac')
    assert made['bitrate'] != 512, (
        'bitrate-ul vine iar de la sonda lui discord.py (max(..., 512))')
    assert made['bitrate'] == 64, (
        f"trebuie exact bitrate-ul canalului, nu {made['bitrate']}k")
    assert made['codec'] == 'aac', (
        'codec-ul probat trebuie transmis: discord.py decide `copy` vs `libopus`')


def test_an_opus_source_still_goes_through_untouched():
    """Cazul obișnuit nu are voie sa devina o reencodare.

    `codec='opus'` e ce transforma comanda in `-c:a copy`. Daca l-am pierde,
    fiecare piesa ar fi reencodata degeaba — pierdere de calitate si de CPU.
    """
    made = _build('opus')
    assert made['codec'] == 'opus', made
    assert made['bitrate'] <= MAX_ENCODE_KBPS, made


def test_the_ffmpeg_options_still_reach_ffmpeg():
    """Plafonul nu are voie sa inghita `-vn` sau `-ss` de la seek."""
    made = _build('aac', options='-vn', before_options='-ss 30')
    assert made['opts'].get('options') == '-vn', made['opts']
    assert made['opts'].get('before_options') == '-ss 30', made['opts']
    assert made['source'] == 'piesa.webm', made


def test_a_boosted_channel_does_not_lift_the_cap():
    """Un canal de 384 kbps nu justifica reencodare peste sursa.

    Sursa de la YouTube nu depaseste ~130 kbps, deci orice peste plafon e doar
    lațime de banda aruncata.
    """
    assert encode_bitrate_kbps(_FakeChannel(384000)) == MAX_ENCODE_KBPS
    assert encode_bitrate_kbps(_FakeChannel(96000)) == 96
    assert encode_bitrate_kbps(_FakeChannel(64000)) == 64


def test_a_channel_without_a_bitrate_falls_back_to_discords_default():
    """Nu pe zero: un `-b:a 0k` ar fi mai rau decat orice presupunere."""
    for absent in (None, 0, -1, False, 'multe'):
        got = encode_bitrate_kbps(_FakeChannel(absent))
        assert got == DEFAULT_CHANNEL_KBPS, (absent, got)
    assert encode_bitrate_kbps(object()) == DEFAULT_CHANNEL_KBPS


def test_no_playback_path_uses_from_probe_anymore():
    """Fixul e pe CLASA, nu pe o instanta: si `!play` si `!seek` porneau audio.

    Un `from_probe` reintrodus oriunde readuce exact bug-ul, deci verificarea e
    mecanica, nu o disciplina de tinut minte.
    """
    from music import commands as commands_mod
    from music import player as player_mod

    for module in (player_mod, commands_mod):
        src = inspect.getsource(module)
        assert 'from_probe' not in src, (
            f'{module.__name__} cheama iar from_probe: bitrate-ul revine la 512k')
        assert 'make_opus_source' in src, module.__name__


def test_the_download_path_actually_reports_the_format():
    """Un raport corect care nu e chemat de nimeni nu diagnosticheaza nimic."""
    import ast

    from music import resolve as resolve_mod

    src = inspect.getsource(resolve_mod._download)
    called = {ast.unparse(n.func) for n in ast.walk(ast.parse(src.strip()))
              if isinstance(n, ast.Call)}
    assert 'format_summary' in called, (
        'descarcarea nu mai spune ce format a obținut: '
        'ramane doar selectorul cerut, care nu distinge opus 130k de AAC 48k')


def test_the_downloaded_format_is_reported_not_the_selector():
    """Sirul de format din log e o cerere; fara asta nu se vedea ce s-a obținut."""
    info = {'requested_downloads': [{'format_id': '251', 'acodec': 'opus',
                                     'abr': 130.0, 'protocol': 'https',
                                     'ext': 'webm', 'vcodec': 'none'}],
            'format_id': 'altul', 'acodec': 'aac'}
    line = format_summary(info)
    assert 'id=251' in line and 'acodec=opus' in line and 'abr=130.0' in line, line
    assert 'altul' not in line, f'a raportat nivelul de sus peste cel ales: {line}'


def test_the_format_report_falls_back_to_the_top_level():
    """Nu orice descarcare populeaza `requested_downloads`."""
    line = format_summary({'format_id': '140', 'acodec': 'mp4a.40.2',
                           'abr': 48, 'protocol': 'm3u8_native'})
    assert 'id=140' in line and 'abr=48' in line, line
    assert format_summary(None) == '?'
    assert format_summary('nu e dict') == '?'


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
            failed += 1
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
