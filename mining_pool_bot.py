import asyncio
import aiofiles
import aiohttp
import itertools
import zmq
import zmq.asyncio
import pybtc
import json
import logging
import sys
from settings import ZMQ_ADDRESS, RPC_ADDRESS, SUBSCRIPTION, POOLS_URL, LOG_FILE, DATA_FILE, BASE_URL, CHAT_ID, HELP_STR


def setup_logging():
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler = logging.FileHandler(LOG_FILE)
    stream_handler.setLevel(logging.INFO)
    file_handler.setLevel(logging.WARNING)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s -  %(message)s')
    stream_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream_handler, file_handler])


class Store:
    def __init__(self):
        self.last_block_sent = None
        self.offset = None
        self.pools = None
        self.pool_subs = None

    async def _get_pools(self, session):
        async with session.get(POOLS_URL) as resp:
            self.pools = await resp.json(content_type='text/plain; charset=utf-8')

    async def _read(self):
        async with aiofiles.open(DATA_FILE, 'r') as f:
            data = await f.read()
        data = json.loads(data)
        self.last_block_sent = data['last_block_sent']
        self.offset = data['offset']
        self.pool_subs = data['pool_subs']

    async def _write(self):
        data = json.dumps({'last_block_sent': self.last_block_sent, 'offset': self.offset, 'pool_subs': self.pool_subs})
        async with aiofiles.open(DATA_FILE, 'w') as f:
            await f.write(data)

    def update_offset(self, offset):
        self.offset = offset
        asyncio.create_task(self._write())

    def update_last_block_sent(self, last_block_sent):
        self.last_block_sent = last_block_sent
        asyncio.create_task(self._write())

    async def load(self, session):
        await asyncio.gather(self._get_pools(session), self._read())


class BotManager:
    def __init__(self, session, store):
        self._session = session
        self._store = store
        pool_name_set = {p['name'] for p in list(self._store.pools['coinbase_tags'].values()) + list(
            self._store.pools['payout_addresses'].values())}
        self._pool_names = ' | '.join(sorted(pool_name_set))
        self._channel_invite_link = ''

    @staticmethod
    def _parse_commands_from_updates(updates):
        commands = []
        offset = -1
        for update in updates:
            logging.debug(update)
            if update['update_id'] >= offset:
                offset = update['update_id'] + 1
            if 'message' in update:
                msg = update['message']
                if msg['chat']['type'] == 'private' and msg['from']['is_bot'] is False and 'text' in msg:
                    logging.info(f'{msg["date"]} -- {msg["from"]} -- {msg["text"]}')
                    if 'entities' in msg:
                        for entity in msg['entities']:
                            if entity['type'] == 'bot_command':
                                begin = entity['offset']
                                end = begin + entity['length']
                                text = msg['text']
                                command = text[begin:end]
                                pool_name = text[end + 1:]
                                commands.append(
                                    {'chat_id': str(msg['chat']['id']), 'message_id': msg['message_id'], 'cmd': command,
                                     'pool_name': pool_name})
                                break
        return commands, offset

    def _clear_subs(self, chat_id):
        user_subs = list()
        for pool in self._store.pool_subs:
            if chat_id in self._store.pool_subs[pool]:
                user_subs.append(pool)
                self._store.pool_subs[pool].pop(chat_id)
        return user_subs

    async def _post(self, route, body, default_value=None):
        async with self._session.post(f'{BASE_URL}/{route}', data=body) as resp:
            if not resp.ok:
                logging.warning(f'Fail to hit {route} with {body} -- {resp.status} -- {resp.reason}')
                return default_value
            if default_value is not None:
                parsed = await resp.json()
                return parsed['result']

    async def _get_updates(self):
        body = {'offset': self._store.offset, 'timeout': 120, 'allowed_updates': ['message']}
        return await self._post('getUpdates', body, [])

    async def _get_invite_link(self):
        if self._channel_invite_link == '':
            self._channel_invite_link = await self._post('exportChatInviteLink', {'chat_id': CHAT_ID}, '')

        return self._channel_invite_link

    @staticmethod
    async def cmd_help(_command):
        return HELP_STR

    async def cmd_list(self, _command):
        return self._pool_names

    async def cmd_subscribe(self, command):
        pool_name = command['pool_name']
        chat_id = command['chat_id']
        if pool_name not in self._store.pool_subs:
            return f'Failed to subscribe to {pool_name}: pool not found. Be sure that you have written the pool ' \
                   f'exactly how it appears in /list (it is case sensitive!) E.g.: /subscribe SlushPool '
        elif chat_id in self._store.pool_subs[pool_name]:
            return f'Failed to subscribe to {pool_name}: you are already subscribed to this pool.'
        else:
            self._store.pool_subs[pool_name][chat_id] = True
            return f'Successfully subscribed to {pool_name}.'

    async def cmd_unsubscribe(self, command):
        pool_name = command['pool_name']
        chat_id = command['chat_id']
        if pool_name not in self._store.pool_subs:
            return f'Failed to subscribe to {pool_name}: pool not found. Be sure that you have written the pool ' \
                   f'exactly how it appears in /list (it is case sensitive!) E.g.: /subscribe SlushPool '
        elif chat_id not in self._store.pool_subs[pool_name]:
            return f'Failed to unsubscribe from {pool_name}: you were not subscribed to this pool.'
        else:
            self._store.pool_subs[pool_name].pop(chat_id)
            return f'Successfully unsubscribed from {pool_name}.'

    async def cmd_listsubs(self, command):
        chat_id = command['chat_id']
        user_subs = []
        for pool in self._store.pool_subs:
            if chat_id in self._store.pool_subs[pool]:
                user_subs.append(pool)
        if len(user_subs) == 0:
            return 'You are not subscribed to any pools.'
        else:
            return f'You are subscribed to: {" | ".join(user_subs)}'

    async def cmd_clearsubs(self, command):
        chat_id = command['chat_id']
        user_subs = list()
        for pool in self._store.pool_subs:
            if chat_id in self._store.pool_subs[pool]:
                user_subs.append(pool)
                self._store.pool_subs[pool].pop(chat_id)
        if len(user_subs) == 0:
            return 'You were not subscribed to any pools.'
        else:
            return f'Successfully unsubscribed from: {" | ".join(user_subs)}'

    async def _send_responses(self, commands):
        if len(commands) == 0:
            return
        tasks = []
        for command in commands:
            body = {'chat_id': command['chat_id'], 'reply_to_message_id': command['message_id']}
            allowed_commands = {
                '/start': self.cmd_help,
                '/help': self.cmd_help,
                '/list': self.cmd_list,
                '/subscribe': self.cmd_subscribe,
                '/unsubscribe': self.cmd_unsubscribe,
                '/listsubs': self.cmd_listsubs,
                '/clearsubs': self.cmd_clearsubs
            }
            cmd = command['cmd']
            if cmd in allowed_commands:
                body['text'] = await allowed_commands[cmd](command)
            else:
                body['text'] = 'Unknown command.'
            tasks.append(self.send_message(body))
        await asyncio.gather(*tasks)

    async def _process_updates(self):
        updates = await self._get_updates()
        if len(updates) == 0:
            return -1
        commands, new_offset = self._parse_commands_from_updates(updates)
        tasks = [asyncio.create_task(self._send_responses(command)) for command in commands]
        await asyncio.gather(*tasks)
        return new_offset

    async def send_message(self, body):
        await self._post('sendMessage', body)

    async def run(self):
        logging.info('Awaiting first new command...')
        while True:
            offset = await self._process_updates()
            if offset > self._store.offset:
                self._store.update_offset(offset)


class StreamManager:
    def __init__(self, store, bot):
        self._ctx = zmq.asyncio.Context()
        self._store = store
        self._bot = bot
        self._next_rpc_id = itertools.count(1).__next__

    def _get_miner_from_coinbase(self, coinbase):
        if coinbase['format'] == 'decoded':
            vouts = coinbase['vOut']
        else:
            vouts = coinbase.decode()['vOut']

        for i in vouts:
            vout = vouts[i]
            if 'address' in vout:
                address = vout['address']
                if len(address) > 0 and address in self._store.pools['payout_addresses']:
                    logging.debug(f'Found miner from payout address {i}')
                    return self._store.pools['payout_addresses'][address]['name']

        if coinbase['format'] == 'decoded':
            coinbase_ascii = bytearray.fromhex(coinbase['vIn'][0]['scriptSig']).decode('utf-8', 'ignore')
        else:
            coinbase_ascii = coinbase['vIn'][0]['scriptSig'].decode('utf-8', 'ignore')

        for tag in self._store.pools['coinbase_tags']:
            if tag in coinbase_ascii:
                logging.debug(f'Found miner from tag {tag}')
                return self._store.pools['coinbase_tags'][tag]['name']

        logging.warning(f'Pool not found: {coinbase_ascii}')
        return 'Unknown'

    def _get_miner_and_reward_from_msg(self, msg):
        coinbase = pybtc.Block(msg, format='raw')['tx'][0]
        miner = self._get_miner_from_coinbase(coinbase)
        reward = f"₿{format(sum(coinbase['vOut'][i]['value'] for i in coinbase['vOut']) / 100000000, '.8f')}"
        return miner, reward

    async def _send_new_block(self, miner, reward, block_count):
        text = f'New block #{block_count} mined by {miner} for {reward}'
        colos = [self._bot.send_message({"chat_id": CHAT_ID, "text": text})]
        if miner in self._store.pool_subs:
            for chat_id in self._store.pool_subs[miner]:
                body = {"chat_id": chat_id, "text": text}
                colos.append(self._bot.send_message(body))
        await batch_colos(20, colos)
        self._store.update_last_block_sent(block_count)
        logging.info(text)

    async def _handle_msg(self, msg):
        if len(msg) > 64:
            block_count = self._store.last_block_sent + 1
            miner, reward = self._get_miner_and_reward_from_msg(msg)
            await self._send_new_block(miner, reward, block_count)

    async def _query_rpc(self, session, method, params=[]):
        data = {'jsonrpc': '2.0', 'id': self._next_rpc_id(), 'method': method, 'params': params}
        async with session.post(RPC_ADDRESS, json=data) as resp:
            if resp.ok:
                resp = await resp.json()
                return resp['result']
            else:
                logging.warning(
                    f'Unable to query rpc for method {method} with params {params}: {resp.status} -- {resp.reason}')

    async def catch_up_if_necessary(self, session):
        last_block_sent = self._store.last_block_sent
        actual_last_block = await self._query_rpc(session, 'getblockcount')
        if last_block_sent != actual_last_block:
            logging.info(f'{last_block_sent} is different from {actual_last_block}, catching up: ')
            tasks = [self._query_rpc(session, 'getblockhash', [h]) for h in
                     range(last_block_sent + 1, actual_last_block + 1)]
            hashes = await batch_colos(10, tasks)
            tasks = [self._query_rpc(session, 'getblock', [h, 0]) for h in hashes]
            blocks = await batch_colos(10, tasks)
            for i in range(len(blocks)):
                if blocks[i] is not None:
                    await self._handle_msg(blocks[i])

    async def _handle_multipart(self, parts):
        tasks = [asyncio.create_task(self._handle_msg(part)) for part in parts]
        await asyncio.gather(*tasks)

    async def run(self):
        sock = self._ctx.socket(zmq.SUB)
        sock.connect(ZMQ_ADDRESS)
        sock.subscribe(SUBSCRIPTION)

        logging.info('Awaiting first new block...')
        while True:
            msg = await sock.recv_multipart()
            await self._handle_multipart(msg)


async def batch_colos(batch_size, colos):
    i = 0
    j = batch_size

    result = []

    while i < len(colos):
        tasks = [asyncio.create_task(colo) for colo in colos[i:j]] + [asyncio.sleep(1)]
        result += await asyncio.gather(*tasks)
        i += batch_size
        j += batch_size

    return result


async def main():
    store = Store()
    async with aiohttp.ClientSession() as session:
        await store.load(session)
        bot_manager = BotManager(session, store)
        stream_manager = StreamManager(store, bot_manager)
        await asyncio.gather(stream_manager.catch_up_if_necessary(session), stream_manager.run(), bot_manager.run())


if __name__ == '__main__':
    setup_logging()
    logging.info('Starting mining pool bot...')
    asyncio.run(main())
