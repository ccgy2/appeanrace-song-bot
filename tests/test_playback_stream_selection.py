"""Real Discord 2.7.1 wrappers + local FFmpeg/Opus; no Discord endpoint used.

The capture transport replaces only network packet delivery. The source,
PCMVolumeTransformer, VoiceClient.play, AudioPlayer and Opus codec are genuine
installed discord.py implementations, never test stubs.
"""
import asyncio
import audioop
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

try:
    import discord
except ImportError:
    discord = None

REAL_DISCORD = bool(discord and getattr(discord, '__version__', '') == '2.7.1'
                    and getattr(discord, '__file__', None)
                    and getattr(getattr(discord, 'FFmpegPCMAudio', None), '__module__', '') == 'discord.player')
if REAL_DISCORD:
    from assets import Assets
    from config import Settings
    from player import Player
    from storage import Store


def run_ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *map(str, arguments)],
                   check=True, capture_output=True, timeout=15)


def pcm_result(source):
    frames = []
    try:
        while frame := source.read():
            frames.append(frame)
        return {'frames': len(frames), 'complete_frames': all(len(frame) == 3840 for frame in frames),
                'rms': audioop.rms(b''.join(frames), 2) if frames else 0}
    finally:
        source.cleanup()


class CapturedVoice:
    """Actual SDK playback thread, with outgoing audio captured locally."""
    if REAL_DISCORD:
        play = discord.VoiceClient.play
        stop = discord.VoiceClient.stop
        is_playing = discord.VoiceClient.is_playing
        is_paused = discord.VoiceClient.is_paused

    def __init__(self, loop):
        self._player = None
        self.client = SimpleNamespace(loop=loop)
        self.timeout = 1
        self.pcm_rms = []
        self.opus_packets = []

        async def speak(_state):
            return None
        self.ws = SimpleNamespace(speak=speak)

    def is_connected(self):
        return True

    def send_audio_packet(self, data, *, encode=True):
        if encode:
            self.pcm_rms.append(audioop.rms(data, 2))
            self.opus_packets.append(self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME))


@unittest.skipUnless(REAL_DISCORD, 'requires genuine pinned discord.py 2.7.1 (not mocked)')
@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires local FFmpeg and FFprobe')
class PlaybackStreamSelectionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixtures = tempfile.TemporaryDirectory(prefix='appearance-stream-fixtures-')
        cls.addClassCleanup(cls.fixtures.cleanup)
        cls.fixture_root = Path(cls.fixtures.name)
        run_ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                   '-t', '0.6', cls.fixture_root / 'tone.wav')
        run_ffmpeg('-i', cls.fixture_root / 'tone.wav', '-c:a', 'libmp3lame', cls.fixture_root / 'tone.mp3')
        run_ffmpeg('-f', 'lavfi', '-i', 'color=c=black:s=16x16:r=5', '-i', cls.fixture_root / 'tone.wav',
                   '-t', '0.6', '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', cls.fixture_root / 'single.mp4')
        # A normal MP4 default-track layout which the forced first-audio map
        # broke: audio 0 is silent, audio 1 is audible and explicitly default.
        run_ffmpeg('-f', 'lavfi', '-i', 'color=c=black:s=16x16:r=5',
                   '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
                   '-i', cls.fixture_root / 'tone.wav', '-t', '0.6',
                   '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0',
                   '-c:v', 'mpeg4', '-c:a', 'aac', '-disposition:a:0', '0',
                   '-disposition:a:1', 'default', cls.fixture_root / 'multi.mp4')
        (cls.fixture_root / 'corrupt.mp3').write_bytes(b'not a valid MP3')

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='appearance-stream-state-')
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.store = Store(self.settings)
        self.addAsyncCleanup(self.store.close)
        self.player = Player(self.settings, self.store, Assets(self.settings))
        self.addAsyncCleanup(self.player.close)

    def source(self, filename, mode='clip'):
        return self.player._source(
            self.fixture_root / filename,
            before='-nostdin -protocol_whitelist file,pipe' + (' -ss 0.100' if mode == 'clip' else ''),
            options='-vn' + (' -t 0.200' if mode == 'clip' else ''),
            master=.5, record={'volumePercent': 100}, team='test',
        )

    async def test_default_audio_in_multi_track_mp4_is_audible_for_clip_and_event(self):
        probe = await asyncio.to_thread(subprocess.run,
            ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries',
             'stream=index:stream_disposition=default', '-of', 'json', str(self.fixture_root / 'multi.mp4')],
            check=True, capture_output=True, timeout=10)
        streams = json.loads(probe.stdout)['streams']
        self.assertEqual([stream['disposition']['default'] for stream in streams], [0, 1])
        for mode in ('clip', 'event'):
            with self.subTest(mode=mode):
                result = await asyncio.to_thread(pcm_result, self.source('multi.mp4', mode))
                self.assertGreater(result['frames'], 0)
                self.assertTrue(result['complete_frames'])
                self.assertGreater(result['rms'], 100, 'the audible default audio track must be selected')
        # Establish the regression fixture: choosing audio 0 really is silent.
        forced = discord.FFmpegPCMAudio(str(self.fixture_root / 'multi.mp4'),
                                       before_options='-nostdin', options='-vn -t 0.200 -map 0:a:0')
        self.assertEqual((await asyncio.to_thread(pcm_result, forced))['rms'], 0)

    async def test_ordinary_mp3_and_single_audio_mp4_still_decode_complete_audible_frames(self):
        for filename in ('tone.mp3', 'single.mp4'):
            for mode in ('clip', 'event'):
                with self.subTest(filename=filename, mode=mode):
                    source = self.source(filename, mode)
                    self.assertIsInstance(source, discord.PCMVolumeTransformer)
                    self.assertIsInstance(source.original, discord.FFmpegPCMAudio)
                    result = await asyncio.to_thread(pcm_result, source)
                    self.assertGreater(result['frames'], 0)
                    self.assertTrue(result['complete_frames'])
                    self.assertGreater(result['rms'], 100)

    async def play_locally(self, filename, profile):
        self.player.settings = replace(self.settings, audio_profile=profile)
        voice = CapturedVoice(asyncio.get_running_loop())
        guild = SimpleNamespace(id=42)
        source = self.source(filename)
        self.player._start(guild, voice, source, self.player.generation[guild.id], filename, 'test')
        thread = voice._player
        try:
            await asyncio.to_thread(thread.join, 3)
            self.assertFalse(thread.is_alive(), 'local playback thread must finish')
            await asyncio.sleep(0)
            return voice, self.player.now[guild.id]
        finally:
            if thread.is_alive():
                voice.stop()
                source.cleanup()
                await asyncio.to_thread(thread.join, 1)

    async def test_actual_audio_player_and_opus_output_are_audible_in_both_profiles(self):
        for profile in ('standard', 'economy'):
            for filename in ('tone.mp3', 'single.mp4', 'multi.mp4'):
                with self.subTest(profile=profile, filename=filename):
                    voice, state = await self.play_locally(filename, profile)
                    self.assertEqual(state['status'], 'finished')
                    self.assertEqual(len(voice.opus_packets), 10)
                    self.assertGreater(max(voice.pcm_rms), 100)
                    decoder = discord.opus.Decoder()
                    decoded_rms = [audioop.rms(decoder.decode(packet, fec=False), 2) for packet in voice.opus_packets]
                    self.assertGreater(max(decoded_rms), 100)

    async def test_corrupt_file_reports_error_with_no_audio_packets(self):
        for profile in ('standard', 'economy'):
            with self.subTest(profile=profile):
                voice, state = await self.play_locally('corrupt.mp3', profile)
                self.assertEqual(voice.opus_packets, [])
                self.assertEqual(state['status'], 'error')
                self.assertIn('오디오', state['error'])


if __name__ == '__main__':
    unittest.main()
