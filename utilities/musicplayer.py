import asyncio
import json
import os
import traceback
from datetime import datetime
from typing import Optional, List, Dict

import ffmpeg
from pyrogram import Client, emoji
from pyrogram.methods.messages.download_media import DEFAULT_DOWNLOAD_DIR
from pyrogram.raw.base import GroupCallParticipant
from pyrogram.types import Message
from pytgcalls import GroupCall
from youtube_dl import YoutubeDL
from youtube_search import YoutubeSearch

from utilities.config import LOG_GROUP_ID, GROUP_CONFIG_FILE_NAME


# - classes

class Config(object):
    def __init__(self):
        self.max_num_of_songs = 8


class MusicToPlay(object):
    def __init__(self, m: Message, added_by: int, title: str, duration: int, raw_file_name: str, link: Optional[str]):
        self.message = m
        self.added_by = added_by
        self.title = title
        self.duration = duration
        self.raw_file_name = raw_file_name

        # Will only be present for YouTube audios and will hold its YouTube link.
        self.link = link


class MusicPlayer(object):
    def __init__(self):
        self.group_call = GroupCall(None, path_to_log_file='')

        # noinspection PyTypeChecker
        self.netstat_changed = self.group_call.on_network_status_changed(network_status_changed_handler)

        # noinspection PyTypeChecker
        self.playout_ended = self.group_call.on_playout_ended(playout_ended_handler)

        self.chat_id = None
        self.chat_title = None
        self.start_time = None
        self.playlist: List[MusicToPlay] = []
        self.msg = {}
        self.join_voice_chat_time = datetime.utcnow()

        self.config = Config()

    async def join_group_call(self, client: Client, chat_id: int, chat_title: str, max_num_of_songs: int):
        self.chat_id = chat_id
        self.chat_title = chat_title
        self.group_call.client = client
        self.config.max_num_of_songs = max_num_of_songs or 8

        await self.group_call.start(chat_id)
        MUSIC_PLAYERS[chat_id] = self

    async def play_track(self, to_play: MusicToPlay):
        await self.download_audio(to_play)
        self.group_call.input_filename = os.path.join(
            self.group_call.client.workdir,
            DEFAULT_DOWNLOAD_DIR,
            to_play.raw_file_name
        )
        await self.update_start_time()

    async def download_audio(self, to_play: MusicToPlay):
        if to_play.message.audio:
            await download_telegram_audio(self, to_play.message, to_play.raw_file_name)
        elif to_play.link:
            await download_youtube_audio(self, to_play.link, to_play.raw_file_name)
        else:
            raise Exception("Couldn't download audio, no suitable download method found!")

    async def update_start_time(self, reset=False):
        self.start_time = (
            None if reset
            else datetime.utcnow().replace(microsecond=0)
        )

    async def send_playlist(self):
        mp = MUSIC_PLAYERS.get(self.chat_id)
        playlist = self.playlist
        if not playlist:
            pl = f"{emoji.NO_ENTRY} empty playlist"
        elif len(playlist) == 1:
            pl = f"{emoji.REPEAT_SINGLE_BUTTON} **Currently playing:**\n" \
                 f"**[{playlist[0].title}]({playlist[0].link or playlist[0].message.link})**"
        else:
            pl = f"{emoji.PLAY_BUTTON} **Currently playing:**\n" \
                 f"**[{playlist[0].title}]({playlist[0].link or playlist[0].message.link})**\n\n" \
                 f"{emoji.PLAY_BUTTON} **Playlist:**\n"

            pl += "\n".join([
                f"**{i + 1}**. **[{x.title}]({x.link or x.message.link})**"
                for i, x in enumerate(playlist[1:])
            ])
        if mp.msg.get('playlist') is not None:
            await mp.msg['playlist'].delete()
        mp.msg['playlist'] = await send_text(mp, pl)


# - Other functions
def search_youtube(keyword) -> Dict:
    # search youtube with specific keyword and return top #1 result
    return YoutubeSearch(keyword, max_results=1).to_dict()[0]


async def skip_current_playing(mp: MusicPlayer):
    group_call = mp.group_call
    playlist = mp.playlist
    if not playlist:
        return
    if len(playlist) == 1:
        await mp.update_start_time()
        return
    client = group_call.client
    download_dir = os.path.join(client.workdir, DEFAULT_DOWNLOAD_DIR)
    file_path = os.path.join(
        download_dir,
        playlist[1].raw_file_name
    )

    if not os.path.isfile(file_path):
        group_call.input_filename = ''
        await download_audio(mp, playlist[0])
        while not os.path.isfile(file_path):
            await asyncio.sleep(2)

    group_call.input_filename = file_path
    await mp.update_start_time()
    # remove old track from playlist
    playlist.pop(0)
    print(f"- START PLAYING: {playlist[0].title}")
    await mp.send_playlist()
    clean_files(mp.group_call.client)
    if len(playlist) == 1:
        return
    await download_audio(mp, playlist[1])


async def download_audio(mp: MusicPlayer, to_play: MusicToPlay):
    if to_play.message.audio:
        await download_telegram_audio(mp, to_play.message, to_play.raw_file_name)
    elif to_play.link:
        await download_youtube_audio(mp, to_play.link, to_play.raw_file_name)
    else:
        raise Exception("Couldn't download audio, no suitable download method found!")


async def download_telegram_audio(mp: MusicPlayer, m: Message, raw_file_name: str):
    try:
        group_call = mp.group_call
        client = group_call.client
        raw_file = os.path.join(client.workdir, DEFAULT_DOWNLOAD_DIR,
                                raw_file_name)
        if not os.path.isfile(raw_file):
            original_file = await m.download()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, ffmpeg_process, original_file, raw_file)
    except Exception as e:
        await log(mp, e)


async def download_youtube_audio(mp: MusicPlayer, youtube_link: str, raw_file_name: str):
    try:
        ydl_opts = {
            'format': 'bestaudio',
            'outtmpl': '%(title)s - %(extractor)s-%(id)s.%(ext)s',
        }
        ydl = YoutubeDL(ydl_opts)
        info_dict = ydl.extract_info(youtube_link, download=False)

        if not os.path.isfile(os.path.join(DEFAULT_DOWNLOAD_DIR, raw_file_name)):
            ydl.process_info(info_dict)
            audio_file = ydl.prepare_filename(info_dict)
            raw_file = os.path.join(mp.group_call.client.workdir, DEFAULT_DOWNLOAD_DIR,
                                    raw_file_name)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, ffmpeg_process, audio_file, raw_file)
    except Exception as e:
        await log(mp, e)


async def delay_delete_messages(messages: tuple, delay: int):
    await asyncio.sleep(delay)
    for m in messages:
        await m.delete()


async def reply_and_delete_later(message: Message, text: str, delay: int):
    reply = await message.reply_text(text, quote=True)
    await asyncio.sleep(delay)
    await reply.delete()


def clean_files(client: Client) -> int:
    download_dir = os.path.join(client.workdir, DEFAULT_DOWNLOAD_DIR)
    all_fn: List[str] = os.listdir(download_dir)
    for mp in MUSIC_PLAYERS.values():
        for track in mp.playlist[:2]:
            track_fn = track.raw_file_name
            if track_fn in all_fn:
                all_fn.remove(track_fn)

    count = 0
    if all_fn:
        for fn in all_fn:
            if fn.endswith(".raw"):
                count += 1
                os.remove(os.path.join(download_dir, fn))
    return count


def ffmpeg_process(audio_file, raw_file):
    try:
        ffmpeg.input(audio_file).filter('volume', 0.1).output(
            raw_file,
            format='s16le',
            acodec='pcm_s16le',
            ac=2,
            ar='48k',
            loglevel='error'
        ).overwrite_output().run()
        os.remove(audio_file)
    except Exception as e:
        print(repr(e))


# - pytgcalls handlers

async def network_status_changed_handler(gc: GroupCall, is_connected: bool):
    if (not gc) or (not gc.full_chat):
        return

    chat_id = int("-100" + str(gc.chat_peer.channel_id))
    mp = MUSIC_PLAYERS.get(chat_id)
    if not mp:
        return
    if is_connected:
        mp.chat_id = int("-100" + str(gc.full_chat.id))
        await send_text(mp, f"{emoji.CHECK_MARK_BUTTON} joined the voice chat")
    else:
        await send_text(mp, f"{emoji.CROSS_MARK_BUTTON} left the voice chat")
        mp.chat_id = None
        del MUSIC_PLAYERS[chat_id]


async def playout_ended_handler(group_call: GroupCall, _):
    chat_id = int("-100" + str(group_call.full_chat.id))
    mp = MUSIC_PLAYERS.get(chat_id)
    if not mp:
        return

    ps: List[GroupCallParticipant] = await group_call.get_group_call_participants()
    if any((x for x in ps if not x.is_self)):  # if anyone is still listening, head on to the next song
        await skip_current_playing(mp)
    else:                                      # otherwise, stop playout (but stay in the voice chat)
        mp.playlist.clear()
        group_call.input_filename = ''
        await send_text(mp, f'{emoji.ROBOT} I stopped playing because nobody is listening anymore!')
        clean_files(group_call.client)


async def send_text(mp: MusicPlayer, text: str, chat: int = None):
    group_call = mp.group_call
    client = group_call.client
    chat_id = chat or mp.chat_id
    message = await client.send_message(
        chat_id,
        text,
        disable_web_page_preview=True,
        disable_notification=True
    )
    return message


async def log(mp: MusicPlayer, e: Exception) -> Message:
    message = await send_text(mp, f'Error occured: {repr(e)}\nI have notified my owner about it already!')
    await send_text(mp,
                    f'Error occured at {mp.chat_id}:\n<code>' +
                    "".join(traceback.TracebackException.from_exception(e).format()) +
                    '</code>',
                    LOG_GROUP_ID)
    return message


MUSIC_PLAYERS: Dict[int, MusicPlayer] = {}
