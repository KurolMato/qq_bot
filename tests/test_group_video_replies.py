import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment


@pytest.fixture
def group(monkeypatch):
    nonebot.init()
    from qq_bot import group_dsh
    monkeypatch.setenv('QQ_DSH_GROUP_ENABLED', 'true')
    monkeypatch.setenv('QQ_DSH_GROUPS', '123')
    return group_dsh


def event(reply=None, *, quoted=True):
    original = MessageSegment.at(10000) + Message('发今天的排行榜')
    if quoted:
        original.insert(0, MessageSegment.reply(55))
    return SimpleNamespace(group_id=123, self_id=10000, user_id=456,
                           original_message=original, raw_message=str(original), reply=reply,
                           get_plaintext=lambda: '发今天的排行榜')


@pytest.mark.parametrize('resolved', [True, False])
def test_bot_video_reply_is_silent_before_chat_or_image_actions(group, monkeypatch, resolved):
    video = Message(MessageSegment.video('https://example.com/video.mp4'))
    reply = SimpleNamespace(sender=SimpleNamespace(user_id=10000), message=video) if resolved else None
    bot = SimpleNamespace(self_id=10000, send=AsyncMock(), get_msg=AsyncMock(return_value={
        'sender': {'user_id': 10000}, 'message': [{'type': 'video', 'data': {'file': 'v.mp4'}}],
    }))
    action = Mock()
    monkeypatch.setattr(group.project_images, 'image_request', action)
    asyncio.run(group.handle_group_question(bot, event(reply)))
    action.assert_not_called()
    bot.send.assert_not_awaited()
    assert bot.get_msg.await_count == (0 if resolved else 1)


@pytest.mark.parametrize('author,kind,quoted', [
    (10000, 'text', True), (10000, 'image', True), (456, 'video', True),
    (None, None, False),
])
def test_other_requests_keep_existing_image_action(group, monkeypatch, author, kind, quoted):
    content = Message(MessageSegment(kind, {'text': 'hello'} if kind == 'text' else {'file': 'asset'})) if kind else None
    reply = SimpleNamespace(sender=SimpleNamespace(user_id=author), message=content) if quoted else None
    bot = SimpleNamespace(self_id=10000, send=AsyncMock(), get_msg=AsyncMock())
    request = SimpleNamespace(kind='rank')
    monkeypatch.setattr(group.project_images, 'image_request', Mock(return_value=request))
    send = AsyncMock()
    monkeypatch.setattr(group.project_images, 'send_project_image', send)
    current = event(reply, quoted=quoted)
    asyncio.run(group.handle_group_question(bot, current))
    send.assert_awaited_once_with(bot, current, request)
    bot.get_msg.assert_not_awaited()


def test_unreadable_quote_does_not_suppress_existing_behavior(group):
    bot = SimpleNamespace(self_id=10000, get_msg=AsyncMock(side_effect=TimeoutError))
    assert asyncio.run(group._replies_to_bot_video(bot, event())) is False


def test_resolved_nonvideo_needs_no_sender_lookup(group):
    reply = SimpleNamespace(message=Message('quoted text'))
    bot = SimpleNamespace(self_id=10000, get_msg=AsyncMock())
    assert asyncio.run(group._replies_to_bot_video(bot, event(reply))) is False
    bot.get_msg.assert_not_awaited()


def test_real_event_reuses_lookup_for_normal_quoted_context(group):
    from qq_bot import dsh_context
    current = GroupMessageEvent.model_validate({
        'time': 1, 'self_id': 10000, 'post_type': 'message', 'message_type': 'group',
        'sub_type': 'normal', 'message_id': 10, 'group_id': 123, 'user_id': 456,
        'message': MessageSegment.reply(55) + MessageSegment.at(10000) + Message('compare'),
        'raw_message': '', 'font': 0, 'sender': {'user_id': 456, 'nickname': 'tester', 'role': 'member'},
    })
    bot = SimpleNamespace(self_id=10000, get_msg=AsyncMock(return_value={
        'sender': {'user_id': 456}, 'message': [{'type': 'text', 'data': {'text': 'quoted text'}}],
    }))
    async def run():
        assert await group._replies_to_bot_video(bot, current) is False
        return await dsh_context.quoted_message(bot, current)
    assert asyncio.run(run()).extract_plain_text() == 'quoted text'
    bot.get_msg.assert_awaited_once_with(message_id='55')
