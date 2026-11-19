"""Undo through the gateway-mounted dashboard's authority WS and HTTP transcript."""
import asyncio

import httpx
import pytest
from websockets.asyncio.client import connect

from tests.gateway.fixtures.local_recovery_probe import rpc
from tests.gateway.test_session_busy_controls import owner, settled
from tests.gateway.test_session_tui_ops import _checkpoint


async def messages(client, sid):
    response = await client.get(f'/api/sessions/{sid}/messages')
    assert response.status_code == 200, response.text
    return response.json()['messages']


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('intent', [None, 'undo', 'retry'])
def test_dashboard_undo_rewinds_last_turn_and_persists_http_projection(tmp_path, intent):
    with owner(tmp_path) as (home, _peer, desc):
        work = tmp_path / 'work'
        work.mkdir()
        path = work / 'a.txt'
        path.write_text('original\n')

        async def probe():
            async with httpx.AsyncClient(base_url=desc['api_origin'], timeout=30,
                                        headers={'X-Hermes-Session-Token': 'other-controls-actor'}) as client:
                # Mount the real dashboard before connecting its token-authenticated socket.
                assert (await client.get('/api/sessions')).status_code == 200
                url = desc['api_origin'].replace('http:', 'ws:') + '/api/ws?token=other-controls-actor'
                async with connect(url) as ws:
                    sid = (await rpc(ws, 'session.create', request_id='undo', source='tui', toolsets=[],
                                     cwd=str(work), checkpoints=True))['result']['session_id']
                    assert (await rpc(ws, 'session.undo', session_id=sid))['result'] == {'removed': 0}
                    for key in ('FIRST', 'SECOND'):
                        await rpc(ws, 'prompt.submit', session_id=sid, input_id=key, text=key)
                        await settled(home)
                    before = await messages(client, sid)
                    assert [m['content'] for m in before] == ['FIRST', 'CONTROL_ACK', 'SECOND', 'CONTROL_ACK']
                    await asyncio.to_thread(_checkpoint, home, work, 'before write_file')
                    path.write_text('edited\n')
                    params = {} if intent is None else {'intent': intent}
                    reply = await rpc(ws, 'session.undo', session_id=sid, **params)
                    assert 'result' in reply, reply
                    result = reply['result']
                    assert result['removed'] == 2, reply
                    if intent == 'retry':
                        assert 'undo_hint' not in result, result
                    else:
                        assert result['undo_hint']['files'] == ['a.txt'], result
                    assert path.read_text() == 'edited\n'
                    after = await messages(client, sid)
                    assert [m['content'] for m in after] == ['FIRST', 'CONTROL_ACK']
                    resumed = await rpc(ws, 'session.resume', session_id=sid)
                    assert [m['content'] for m in resumed['result']['messages']] == ['FIRST', 'CONTROL_ACK']
        asyncio.run(probe())


@pytest.mark.platforms('linux')
def test_dashboard_undo_invalid_and_busy_requests_do_not_change_history(tmp_path):
    with owner(tmp_path) as (home, peer, desc):
        async def probe():
            async with httpx.AsyncClient(base_url=desc['api_origin'], timeout=30,
                                        headers={'X-Hermes-Session-Token': 'other-controls-actor'}) as client:
                assert (await client.get('/api/sessions')).status_code == 200
                url = desc['api_origin'].replace('http:', 'ws:') + '/api/ws?token=other-controls-actor'
                async with connect(url) as ws:
                    sid = (await rpc(ws, 'session.create', request_id='undo-errors', source='tui', toolsets=[]))['result']['session_id']
                    await rpc(ws, 'prompt.submit', session_id=sid, input_id='first', text='FIRST')
                    await settled(home)
                    before = await messages(client, sid)
                    for params in ({}, {'session_id': sid, 'intent': 'invalid'}):
                        reply = await rpc(ws, 'session.undo', **params)
                        assert reply['error']['code'] != -32601, reply
                        assert reply['error']['message'] == 'invalid_params', reply
                        assert await messages(client, sid) == before
                    peer.release.clear()
                    await rpc(ws, 'prompt.submit', session_id=sid, input_id='block', text='BLOCK_REDIRECT')
                    assert await asyncio.to_thread(peer.blocked.wait, 25)
                    busy_before = await messages(client, sid)
                    reply = await rpc(ws, 'session.undo', session_id=sid)
                    assert reply['error']['message'] == 'session_busy', reply
                    assert await messages(client, sid) == busy_before
                    peer.release.set()
                    await settled(home)
                    assert (await rpc(ws, 'session.undo', session_id=sid, intent='retry'))['result'] == {'removed': 2}
                    assert [m['content'] for m in await messages(client, sid)] == ['FIRST', 'CONTROL_ACK']
        asyncio.run(probe())
