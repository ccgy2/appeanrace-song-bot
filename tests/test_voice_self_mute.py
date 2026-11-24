"""Bot self-mute recovery through public Gateway calls; no Discord network."""
import asyncio
import enum
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import discord
except ImportError:
    discord = types.ModuleType('discord')
    class ChannelType(enum.Enum):
        voice = 2
        stage_voice = 13
    class Forbidden(Exception):
        pass
    discord.ChannelType = ChannelType
    discord.Forbidden = Forbidden

with patch.dict(sys.modules, {'discord': discord}):
    spec = importlib.util.spec_from_file_location(
        'tested_voice_self_mute', Path(__file__).resolve().parents[1] / 'voice.py')
    voice = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(voice)


def fixture(*, self_mute=True, self_deaf=True, server_mute=False):
    guild = types.SimpleNamespace(id=123, voice_client=None, change_voice_state=AsyncMock())
    permissions = types.SimpleNamespace(
        view_channel=True, connect=True, speak=True, move_members=False,
        mute_members=False, request_to_speak=False)
    channel = types.SimpleNamespace(
        id=9, name='테스트 통화방', guild=guild, type=discord.ChannelType.voice,
        user_limit=0, members=[], permissions_for=lambda _: permissions, connect=AsyncMock())
    state = types.SimpleNamespace(
        channel=channel, self_mute=self_mute, self_deaf=self_deaf,
        mute=server_mute, suppress=False)
    guild.me = types.SimpleNamespace(voice=state, edit=AsyncMock(), request_to_speak=AsyncMock())
    client = types.SimpleNamespace(
        channel=channel, is_connected=Mock(return_value=True),
        is_playing=Mock(return_value=False), is_paused=Mock(return_value=False),
        disconnect=AsyncMock(), cleanup=Mock(), move_to=AsyncMock(), latency=.02)
    guild.voice_client = client
    return guild, channel, state, client


class SelfMuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_reused_client_is_unmuted_without_disconnect_and_clears_old_error(self):
        guild, channel, state, client = fixture()
        async def acknowledge(**kwargs):
            state.self_mute = False
        guild.change_voice_state.side_effect = acknowledge
        manager = voice.VoiceManager()
        manager.errors[guild.id] = 'previous failure'
        self.assertIs(await manager.connect(guild, channel), client)
        guild.change_voice_state.assert_awaited_once_with(
            channel=channel, self_mute=False, self_deaf=True)
        channel.connect.assert_not_called()
        client.disconnect.assert_not_called()
        client.move_to.assert_not_called()
        self.assertNotIn(guild.id, manager.errors)

    async def test_keeps_receive_audio_enabled_when_self_deaf_was_false(self):
        guild, channel, state, _ = fixture(self_deaf=False)
        guild.change_voice_state.side_effect = lambda **kwargs: setattr(state, 'self_mute', False)
        await voice.VoiceManager().connect(guild, channel)
        guild.change_voice_state.assert_awaited_once_with(
            channel=channel, self_mute=False, self_deaf=False)

    async def test_requires_positive_self_mute_state_before_sending_any_change(self):
        for value in (False, None):
            with self.subTest(value=value):
                guild, channel, _, client = fixture(self_mute=value)
                self.assertIs(await voice.VoiceManager().connect(guild, channel), client)
                guild.change_voice_state.assert_not_called()
        guild, channel, state, _ = fixture()
        del state.self_mute
        await voice.VoiceManager().connect(guild, channel)
        guild.change_voice_state.assert_not_called()

    async def test_new_unmuted_connection_keeps_normal_connect_options(self):
        guild, channel, _, client = fixture(self_mute=False)
        guild.voice_client = None
        channel.connect.return_value = client
        self.assertIs(await voice.VoiceManager().connect(guild, channel), client)
        channel.connect.assert_awaited_once_with(timeout=25, reconnect=True, self_deaf=True)
        guild.change_voice_state.assert_not_called()

    async def test_waits_for_gateway_confirmation_after_request_returns(self):
        guild, channel, state, client = fixture()
        poll_count = 0
        async def gateway_tick(_):
            nonlocal poll_count
            poll_count += 1
            if poll_count == 2:
                guild.me.voice = types.SimpleNamespace(**{**vars(state), 'self_mute': False})
        with patch.object(voice.asyncio, 'sleep', new=AsyncMock(side_effect=gateway_tick)):
            self.assertIs(await voice.VoiceManager().connect(guild, channel), client)
        self.assertEqual(poll_count, 2)

    async def test_missing_confirmation_is_bounded_and_does_not_claim_success(self):
        guild, channel, state, client = fixture()
        manager = voice.VoiceManager()
        with patch.object(voice.asyncio, 'sleep', new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(voice.VoiceError, '자체 음소거.*확인'):
                await manager.connect(guild, channel)
        self.assertEqual(sleep.await_count, 15)
        self.assertTrue(state.self_mute)
        self.assertIn('확인', manager.errors[guild.id])
        client.disconnect.assert_not_called()

    async def test_request_timeout_has_useful_error_and_keeps_connection(self):
        guild, channel, _, client = fixture()
        guild.change_voice_state.side_effect = asyncio.TimeoutError()
        with self.assertRaisesRegex(voice.VoiceError, '자체 음소거.*시간이 초과'):
            await voice.VoiceManager().connect(guild, channel)
        client.disconnect.assert_not_called()

    async def test_forbidden_has_useful_error_and_keeps_connection(self):
        guild, channel, _, client = fixture()
        guild.change_voice_state.side_effect = discord.Forbidden(
            types.SimpleNamespace(status=403, reason='Forbidden'), 'test refusal')
        with self.assertRaisesRegex(voice.VoiceError, '자체 음소거.*거부'):
            await voice.VoiceManager().connect(guild, channel)
        client.disconnect.assert_not_called()

    async def test_server_mute_is_preserved_and_reported(self):
        guild, channel, state, _ = fixture(server_mute=True)
        guild.change_voice_state.side_effect = lambda **kwargs: setattr(state, 'self_mute', False)
        manager = voice.VoiceManager()
        await manager.connect(guild, channel)
        self.assertTrue(state.mute)
        self.assertTrue(manager.status(guild)['serverMuted'])
        self.assertFalse(manager.status(guild)['selfMuted'])
        guild.me.edit.assert_not_called()

    async def test_stage_audience_rules_are_still_enforced(self):
        guild, channel, state, _ = fixture()
        channel.type = discord.ChannelType.stage_voice
        state.suppress = True
        guild.change_voice_state.side_effect = lambda **kwargs: setattr(state, 'self_mute', False)
        with self.assertRaisesRegex(voice.VoiceError, '청중 상태'):
            await voice.VoiceManager().connect(guild, channel)
        self.assertTrue(state.suppress)
        guild.me.edit.assert_not_called()

    async def test_stale_channel_is_not_moved_by_self_unmute(self):
        guild, channel, state, _ = fixture()
        state.channel = types.SimpleNamespace(id=99)
        with self.assertRaisesRegex(voice.VoiceError, '현재 통화방 상태'):
            await voice.VoiceManager().connect(guild, channel)
        guild.change_voice_state.assert_not_called()

    async def test_confirmation_for_another_channel_is_not_accepted(self):
        guild, channel, state, _ = fixture()
        async def moved_elsewhere(**kwargs):
            state.self_mute = False
            state.channel = types.SimpleNamespace(id=99)
        guild.change_voice_state.side_effect = moved_elsewhere
        with patch.object(voice.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaisesRegex(voice.VoiceError, '확인'):
                await voice.VoiceManager().connect(guild, channel)

    def test_status_distinguishes_self_mute_from_server_mute(self):
        guild, _, state, _ = fixture()
        manager = voice.VoiceManager()
        self.assertTrue(manager.status(guild)['selfMuted'])
        self.assertFalse(manager.status(guild)['serverMuted'])
        state.self_mute = False
        self.assertFalse(manager.status(guild)['selfMuted'])
        guild.me.voice = None
        self.assertFalse(manager.status(guild)['selfMuted'])


if __name__ == '__main__':
    unittest.main()
