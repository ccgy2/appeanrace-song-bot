"""음성 연결 상태기계 단위 테스트. Discord 네트워크는 항상 mock.
실제 discord.py가 설치되지 않은 오프라인 개발 환경에서는 enum/예외만 stub한다.
"""
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
    discord=types.ModuleType('discord')
    class ChannelType(enum.Enum): voice=2;stage_voice=13
    class Forbidden(Exception): pass
    discord.ChannelType=ChannelType;discord.Forbidden=Forbidden

with patch.dict(sys.modules, {'discord':discord}):
    spec=importlib.util.spec_from_file_location('tested_voice',Path(__file__).resolve().parents[1]/'voice.py')
    voice=importlib.util.module_from_spec(spec);spec.loader.exec_module(voice)


def setup_pair():
    g=types.SimpleNamespace(id=123,me=object(),voice_client=None,change_voice_state=AsyncMock())
    perms=types.SimpleNamespace(view_channel=True,connect=True,speak=True,move_members=False)
    ch=types.SimpleNamespace(id=9,guild=g,type=discord.ChannelType.voice,user_limit=0,members=[],permissions_for=lambda _:perms,connect=AsyncMock())
    return g,ch,perms


def client(g,ch,connected=True):
    vc=types.SimpleNamespace(channel=ch,is_connected=Mock(return_value=connected),disconnect=AsyncMock(),cleanup=Mock(),move_to=AsyncMock())
    async def disconnect(**kwargs):g.voice_client=None
    vc.disconnect.side_effect=disconnect
    return vc


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_connected_client(self):
        g,ch,_=setup_pair();vc=client(g,ch);g.voice_client=vc
        self.assertIs(await voice.VoiceManager().connect(g,ch),vc)
        vc.disconnect.assert_not_called();ch.connect.assert_not_called()
    async def test_falls_back_to_current_channel(self):
        g,ch,_=setup_pair();g.voice_client=client(g,ch)
        self.assertIs(await voice.VoiceManager().connect(g),g.voice_client)
    async def test_moves_without_disconnect(self):
        g,ch,_=setup_pair();old=types.SimpleNamespace(id=99);vc=client(g,old);g.voice_client=vc
        self.assertIs(await voice.VoiceManager().connect(g,ch),vc)
        vc.move_to.assert_awaited_once();vc.disconnect.assert_not_called()
    async def test_permissions_reported(self):
        g,ch,perms=setup_pair();perms.speak=False;m=voice.VoiceManager()
        with self.assertRaisesRegex(voice.VoiceError,'말하기'):await m.connect(g,ch)
        self.assertIn('말하기',m.errors[g.id]);ch.connect.assert_not_called()
    async def test_full_channel(self):
        g,ch,_=setup_pair();ch.user_limit=1;ch.members=[object()]
        with self.assertRaisesRegex(voice.VoiceError,'가득'):await voice.VoiceManager().connect(g,ch)
    async def test_stage_rejected(self):
        g,ch,_=setup_pair();ch.type=discord.ChannelType.stage_voice
        with self.assertRaisesRegex(voice.VoiceError,'스테이지'):await voice.VoiceManager().connect(g,ch)
    async def test_missing_channel(self):
        g,_,_=setup_pair()
        with self.assertRaisesRegex(voice.VoiceError,'먼저'):await voice.VoiceManager().connect(g)
    async def test_stale_cleanup(self):
        g,ch,_=setup_pair();stale=client(g,ch,False);g.voice_client=stale;new=client(g,ch)
        async def connect(**kwargs):g.voice_client=new;return new
        ch.connect.side_effect=connect
        self.assertIs(await voice.VoiceManager().connect(g,ch),new)
        stale.disconnect.assert_awaited_once()
    async def test_timeout_retries_once(self):
        g,ch,_=setup_pair();new=client(g,ch);ch.connect.side_effect=[asyncio.TimeoutError(),new]
        with patch.object(voice.asyncio,'sleep',new=AsyncMock()):self.assertIs(await voice.VoiceManager().connect(g,ch),new)
        self.assertEqual(ch.connect.await_count,2)
    async def test_two_calls_only_one_connection(self):
        g,ch,_=setup_pair();new=client(g,ch)
        async def connect(**kwargs):
            await asyncio.sleep(.02);g.voice_client=new;return new
        ch.connect.side_effect=connect;m=voice.VoiceManager()
        a,b=await asyncio.gather(m.connect(g,ch),m.connect(g,ch))
        self.assertIs(a,b);ch.connect.assert_awaited_once()
    async def test_dave_error_hint(self):
        exc=RuntimeError('closed');exc.code=4017
        self.assertIn('DAVE',voice.VoiceManager.explain(exc))
    async def test_timeout_error_hint(self):
        self.assertIn('UDP',voice.VoiceManager.explain(asyncio.TimeoutError()))

if __name__=='__main__':unittest.main()
