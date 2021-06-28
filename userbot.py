import logging
from pyrogram import Client, idle, filters
from aiocache import Cache
import os
from utilities.musicplayer import MUSIC_PLAYERS, MusicPlayer
import pickle
from pathlib import Path
from typing import Dict
import asyncio
import json

from utilities.config import GLOBAL_ADMINS, LOG_GROUP_ID, COMMAND_PREFIX, PICKLE_FILE_NAME, GROUP_CONFIG_FILE_NAME

global_admins_filter = (
    filters.incoming & filters.user(GLOBAL_ADMINS)
)


async def load_saved_playlists():
    if Path(PICKLE_FILE_NAME).exists():
        with open(PICKLE_FILE_NAME, 'rb') as f:
            to_load: Dict[int, dict] = pickle.load(f)
            for chat_id, info in to_load.items():
                mp = MusicPlayer()
                MUSIC_PLAYERS[chat_id] = mp
                mp.group_call.client = app
                num = 8
                with open(GROUP_CONFIG_FILE_NAME, 'r', encoding='utf-8') as f2:
                    configs = json.load(f2)
                    if str(chat_id) in configs:
                        num = configs[str(chat_id)]['max_num_of_songs']
                await mp.join_group_call(app, chat_id, info['chat_title'], num)
                mp.playlist = info['playlist']
                await mp.play_track(mp.playlist[0])
                for track in mp.playlist[:2]:
                    await mp.download_audio(track)
                await mp.send_playlist()


async def load_group_config():
    if not Path(GROUP_CONFIG_FILE_NAME).exists():
        new_dict = {}
        with open(GROUP_CONFIG_FILE_NAME, 'w', encoding='utf-8') as f:
            json.dump(new_dict, f, ensure_ascii=False)

    with open(GROUP_CONFIG_FILE_NAME, 'r+', encoding='utf-8') as f:
        configs = json.load(f)
        for chat_id, cfg in configs.items():
            if int(chat_id) in MUSIC_PLAYERS:
                MUSIC_PLAYERS[chat_id].config.max_num_of_songs = cfg['max_num_of_songs']


app = Client("test")
logging.basicConfig(level=logging.INFO)
cache = Cache(Cache.MEMORY)


if __name__ == '__main__':
    app.start()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(load_saved_playlists())
    loop.run_until_complete(load_group_config())
    print('>>> USERBOT STARTED')
    app.send_message(LOG_GROUP_ID, f'Bot started!')
    idle()
    app.send_message(LOG_GROUP_ID, f'Bot stopped!')
    app.stop()
    print('\n>>> USERBOT STOPPED')

