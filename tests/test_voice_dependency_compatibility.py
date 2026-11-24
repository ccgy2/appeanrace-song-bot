"""Offline MLS exchange through real discord.py gateway and native davey.

Run with the installed voice dependencies; no Discord account or endpoint is
used. Network sends alone are captured. The real native MLS commit, SDK
transition handlers, Opus encoder, and SDK outgoing DAVE selection run here.
This passes with discord.py 2.7.1 / davey 0.1.7 and fails with davey 0.2.0,
whose new native key transition calls are absent from this SDK version.
"""
import math
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

try:
    import discord
    import davey
except ImportError:
    discord = davey = None

REAL_VOICE = bool(discord and davey
                  and getattr(discord, '__version__', '') == '2.7.1'
                  and getattr(discord, '__file__', None)
                  and getattr(davey, '__file__', None))
if REAL_VOICE:
    from discord.gateway import DiscordVoiceWebSocket
    from discord.voice_state import VoiceConnectionState

# Public binary MLS fixtures from davey's Python tests, release py-0.2.0:
# https://github.com/Snazzah/davey/blob/py-0.2.0/davey-python/test/test_session.py
# These contain no private keys. The session creates fresh local secrets.
#
# MIT License
# Copyright (c) 2025-present Snazzah
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
EXTERNAL_SENDER = bytes.fromhex(
    '404104ca1a2b102501d0672bd45ed74ffb83e078b2ba5b12c3f69fad56f083b6a35f'
    'c989c6736b5852b5aecdfcdf206e156d3d1dba8e3e5b2f89fc0c16f11614e84e4a00010100'
)
APPENDING_PROPOSALS = bytes.fromhex(
    '41f000010001080cde77eadc8230330000000000000000020000000000020001000100024041'
    '04a61acd34ff0548e6f767cc4df9619b51ba5814d52ee558284fc554405768e9e741ba322764'
    '14941b7c0190dcb5dfc3349a168f1247ecbdf6fc69f3eecab3765d404104f44da24e5aa9f737'
    '96fa38be115556e6b7d4f6ba55d2ecc3f1b9d198af6237e3fc8fc635996b6c76e77b4acace33'
    'f5cfd745ad0d540dd6f03f0da85c82e14714404104e7793985ed074af49568b53cf2e397468827'
    '9f02ee8d7c7df099ce3c7a1a28e747f59a7c7c23e9ef4f7815fb3469f39ea124f8b76712c41'
    'b763a55b867e6b48b000108023339994002000002000102000200000200010100000000000000'
    '00ffffffffffffffff0040483046022100d874dc777d4ede7e6d3374f737bc1394ac0ffd0635ab'
    'dc9d02b3e359e059466a022100e62e97aefd5c6b32ff0ac5c5159dbe94f6f5a01bd0dd14a9d'
    'cc4d1e6248972ac004047304502205168abdf8d83a48daf8d5960afb305022de707c7496087fe'
    'b43004c9fd2be5e80221008312a6f6df1ff593671a39f9966f6d1caef43e0c6d53c74300ba08'
    '9ceeecfbf440473045022100cdbe804ba06a9be17615fe3c7f9055f31a6d0ea9402dd9fda6d10'
    '9e9a3cb632b022067409e59f6b7f0a3eddd330a092a6c13d99d12abaebe0d6d40a0dae435c2a7a2'
)


@unittest.skipUnless(REAL_VOICE, 'requires genuine discord.py 2.7.1 and native davey')
class VoiceDependencyCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_commit_and_transition_enable_outgoing_dave_encryption(self):
        # A genuine, audible Opus frame avoids DAVE's special silence bypass.
        pcm = struct.pack('<1920h', *(
            int(4000 * math.sin(2 * math.pi * 440 * (i // 2) / 48000))
            for i in range(1920)
        ))
        encoder = discord.opus.Encoder()
        opus = encoder.encode(pcm, 960)
        self.assertGreater(len(opus), 3)

        for transition_id in (0, 7):
            with self.subTest(transition_id=transition_id):
                session = davey.DaveSession(1, 158049329150427136, 927310423890473011)
                # Bypass constructors which start network/socket reader threads;
                # all methods under test are the actual installed SDK methods.
                state = VoiceConnectionState.__new__(VoiceConnectionState)
                state.dave_session = session
                state.dave_protocol_version = 1
                state.dave_pending_transitions = {}
                state.dave_downgraded = False
                state.ssrc = 42
                state.mode = 'capture'
                gateway = DiscordVoiceWebSocket.__new__(DiscordVoiceWebSocket)
                gateway._connection = state
                gateway.seq_ack = 0
                gateway._hook = AsyncMock()
                gateway.send_as_json = AsyncMock()
                state.voice_client = SimpleNamespace(ws=gateway)

                await gateway.received_binary_message(
                    struct.pack('>HB', 1, gateway.MLS_EXTERNAL_SENDER) + EXTERNAL_SENDER)
                session.get_serialized_key_package()
                commit_welcome = session.process_proposals(
                    davey.ProposalsOperationType.append, APPENDING_PROPOSALS)
                self.assertIsNotNone(commit_welcome)
                await gateway.received_binary_message(
                    struct.pack('>HBH', 2, gateway.MLS_ANNOUNCE_COMMIT_TRANSITION, transition_id)
                    + commit_welcome.commit)
                if transition_id:
                    gateway.send_as_json.assert_awaited_once_with({
                        'op': gateway.DAVE_TRANSITION_READY,
                        'd': {'transition_id': transition_id},
                    })
                    await gateway.received_message({
                        'op': gateway.DAVE_EXECUTE_TRANSITION,
                        'd': {'transition_id': transition_id},
                    })
                    self.assertNotIn(transition_id, state.dave_pending_transitions)

                voice = discord.VoiceClient.__new__(discord.VoiceClient)
                voice._connection = state
                voice.sequence = voice.timestamp = 0
                # Capture just before the outer RTP transport encryption; the
                # DAVE layer and SDK choice to apply it are real, not mocked.
                voice._encrypt_capture = lambda _header, payload: payload
                packet = voice._get_voice_packet(opus)
                self.assertFalse(packet == opus,
                    'SDK sent an unchanged Opus frame after MLS commit: native DAVE keys '
                    'were never activated (discord.py 2.7.1 requires davey 0.1.x).')
                self.assertTrue(session.ready)
                self.assertTrue(state.can_encrypt)
                self.assertGreater(len(packet), len(opus))


if __name__ == '__main__':
    unittest.main()
