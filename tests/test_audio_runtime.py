"""Economy audio profile uses supported options and valid native Opus packets."""
from array import array
import math
import unittest

from audio_runtime import audio_profile_info, validate_audio_profile, voice_play_options


class AudioProfileTests(unittest.TestCase):
    def test_standard_preserves_existing_voice_call(self):
        self.assertEqual(voice_play_options('standard'), {})
        self.assertEqual(validate_audio_profile(' STANDARD '), 'standard')
        info = audio_profile_info('standard')
        self.assertEqual(info['bitrateKbps'], 128)
        self.assertTrue(info['forwardErrorCorrection'])

    def test_economy_uses_only_supported_explicit_public_options(self):
        self.assertEqual(voice_play_options('economy'), {
            'application': 'audio', 'bitrate': 96, 'fec': False,
            'bandwidth': 'full', 'signal_type': 'music',
        })
        self.assertEqual(validate_audio_profile(' Economy '), 'economy')
        info = audio_profile_info('economy')
        self.assertEqual(info['bitrateKbps'], 96)
        self.assertFalse(info['forwardErrorCorrection'])
        self.assertIn('불안정한 네트워크', info['note'])
        self.assertIn('standard', info['note'])

    def test_mutating_returned_options_never_changes_later_calls(self):
        options = voice_play_options('economy')
        options['bitrate'] = 512
        options['fec'] = True
        self.assertEqual(voice_play_options('economy')['bitrate'], 96)
        self.assertFalse(voice_play_options('economy')['fec'])

    def test_invalid_profiles_fail_clearly_instead_of_silent_downgrade(self):
        for value in ['', 'fast', '128', 'economy;foo', None, 0, {}, []]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'AUDIO_PROFILE'):
                    voice_play_options(value)

    def test_native_opus_roundtrip_and_gain_changes_keep_complete_frames(self):
        import discord.opus
        for profile in ['standard', 'economy']:
            encoder = discord.opus.Encoder(**voice_play_options(profile))
            decoder = discord.opus.Decoder()
            for frame_number, gain in enumerate([0, .25, .5, 1, 2, .5, 0]):
                samples = array('h')
                for i in range(960):
                    sample = int(1000 * gain * math.sin(2 * math.pi * 440 * (frame_number * 960 + i) / 48000))
                    samples.extend((sample, sample))
                packet = encoder.encode(samples.tobytes(), 960)
                decoded = decoder.decode(packet, fec=False)
                self.assertEqual(len(decoded), 3840)
                self.assertGreater(len(packet), 0)


if __name__ == '__main__':
    unittest.main()
