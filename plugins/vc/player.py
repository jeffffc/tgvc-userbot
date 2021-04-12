"""Play and Control Audio playing in Telegram Voice Chat

Dependencies:
- ffmpeg

Required group admin permissions:
- Delete messages
- Manage voice chats (optional)

How to use:
- Start the userbot
- send !join to a voice chat enabled group chat
  from userbot account itself or its contacts
- reply to an audio with /play to start playing
  it in the voice chat, every member of the group
  can use the !play command now
- check !help for more commands
"""
import os
import asyncio
from datetime import datetime, timedelta
from pyrogram import Client, filters, emoji
from pyrogram.types import Message, ChatMember
from pyrogram.methods.messages.download_media import DEFAULT_DOWNLOAD_DIR
from pyrogram.raw.base import GroupCallParticipant
from pytgcalls import GroupCall
import ffmpeg
from youtube_dl import YoutubeDL
from youtube_search import YoutubeSearch
from typing import Optional, List, Dict
import traceback

from userbot import global_admins_filter, LOG_GROUP_ID, COMMAND_PREFIX

from aiocache import cached
from userbot import cache
import logging

DELETE_DELAY = 8
MUSIC_MAX_LENGTH = 10800
DELAY_DELETE_INFORM = 10
MAX_PLAYLIST_LENGTH = 8

REGEX_SITES = (
    r"^((?:https?:)?\/\/)"
    r"?((?:www|m)\.)"
    r"?((?:youtube\.com|youtu\.be|soundcloud\.com|mixcloud\.com))"
    r"(\/)([-a-zA-Z0-9()@:%_\+.~#?&//=]*)([\w\-]+)(\S+)?$"
)
REGEX_EXCLUDE_URL = (
    r"\/channel\/|\/playlist\?list=|&list=|\/sets\/"
)

USERBOT_HELP = f"""{emoji.LABEL}  **Common Commands**:
__available to group members of current voice chat__
__starts with / (slash) or ! (exclamation mark)__

/play  reply with an audio to play/queue it, or show playlist
/current  show current playing time of current track
/repo  show git repository of the userbot
`!help`  show help for commands


{emoji.LABEL}  **Admin Commands**:
__available to userbot account itself and its contacts__
__starts with ! (exclamation mark)__

`!skip` [n] ...  skip current or n where n >= 2
`!join`  join voice chat of current group
`!leave`  leave current voice chat
`!vc`  check which VC is joined
`!stop`  stop playing
`!replay`  play from the beginning
`!clean`  remove unused RAW PCM files
`!pause` pause playing
`!resume` resume playing
`!mute`  mute the VC userbot
`!unmute`  unmute the VC userbot
"""

USERBOT_REPO = f"""{emoji.ROBOT} **Telegram Voice Chat UserBot**

- Repository: [GitHub](https://github.com/callsmusic/tgvc-userbot)
- License: AGPL-3.0-or-later"""


# - Pyrogram filters

main_filter = (
    filters.group
    & filters.text
    & ~filters.edited
)
self_or_contact_filter = filters.create(
    lambda
    _,
    __,
    message:
    (message.from_user and message.from_user.is_contact) or message.outgoing
)


async def group_admin_filter_func(_, client: Client, message: Message):
    admins = await get_chat_admins(client, message.chat.id)
    is_admin = message.from_user.id in admins
    if not is_admin:
        await message.reply_text(f'{emoji.NO_ENTRY} This command can only be used by a group admin.')
    return is_admin

group_admin_filter = global_admins_filter | filters.create(group_admin_filter_func)


async def get_chat_admins(c: Client, chat_id: int):
    """Returns a list of admin IDs for a given chat. Results are cached for 30 mins."""
    cached_admins = await cache.get(chat_id)
    if not cached_admins:
        logging.info(f'Not cached for chat {chat_id}. Getting admin list now...')
        admins: List[ChatMember] = await c.get_chat_members(chat_id, filter='administrators')
        logging.info(f'Finish caching admin list for chat {chat_id}')
        admin_ids = [admin.user.id for admin in admins]
        await cache.set(chat_id, admin_ids)
        return admin_ids
    else:
        logging.info(f'Getting admin list for chat {chat_id} from cache')
        return cached_admins


async def current_vc_filter(_, __, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    return bool(mp) and mp.group_call.is_connected

current_vc = filters.create(current_vc_filter)


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
        _clean_files(group_call.client)


# - classes


class MusicToPlay(object):
    def __init__(self, m: Message, title: str, duration: int, raw_file_name: str, link: Optional[str]):
        self.message = m
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


MUSIC_PLAYERS: Dict[int, MusicPlayer] = {}


# - Pyrogram handlers


# Workaround for messages being received twice for some unknown reason. If a message has been handled before, ignore it
LAST_MESSAGE_ID: Dict[int, int] = {}


@Client.on_message(filters.group & ~filters.edited, group=-1)
def avoid_receiving_messages_twice(_, m: Message):
    last_id = LAST_MESSAGE_ID.get(m.chat.id)
    if last_id and m.message_id <= last_id:
        m.stop_propagation()
    LAST_MESSAGE_ID[m.chat.id] = m.message_id


# Commands

@Client.on_message(
    filters.group
    & ~filters.edited
    & current_vc
    & (filters.command('play', prefixes=[COMMAND_PREFIX, '/']) | filters.audio)
)
async def play_track(client, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    playlist = mp.playlist
    # check playlist length
    if len(playlist) >= MAX_PLAYLIST_LENGTH:
        await _reply_and_delete_later(m, f'{emoji.CROSS_MARK} There are already {MAX_PLAYLIST_LENGTH} songs in '
                                         f'the playlist, cannot add more!', DELETE_DELAY)
        return
    # check audio
    if m.audio:
        if m.audio.duration > 600:
            reply = await m.reply_text(
                f"{emoji.ROBOT} audio which duration longer than 10 min "
                "won't be automatically added to playlist"
            )
            await _delay_delete_messages((reply, ), DELETE_DELAY)
            return
        m_audio = m
    elif m.reply_to_message and m.reply_to_message.audio:
        m_audio = m.reply_to_message
    else:
        await mp.send_playlist()
        await m.delete()
        return
    if m_audio.audio.duration > MUSIC_MAX_LENGTH:
        readable_max_length = str(timedelta(seconds=MUSIC_MAX_LENGTH))
        inform = ("This won't be downloaded because its audio length is "
                  "longer than the limit `{}` which is set by the bot"
                  .format(readable_max_length))
        await _reply_and_delete_later(m, inform,
                                      DELAY_DELETE_INFORM)
        return

    # check already added
    if playlist and playlist[-1].raw_file_name == f'TG_{m_audio.audio.file_unique_id}.raw':
        reply = await m.reply_text(f"{emoji.ROBOT} already added")
        await _delay_delete_messages((reply, m), DELETE_DELAY)
        return
    # add to playlist
    to_play = MusicToPlay(m_audio, m_audio.audio.title,
                          m_audio.audio.duration, f'TG_{m_audio.audio.file_unique_id}.raw', None)
    playlist.append(to_play)
    if len(playlist) == 1:
        m_status = await m.reply_text(
            f"{emoji.INBOX_TRAY} downloading and transcoding..."
        )
        await download_audio(mp, to_play)
        group_call.input_filename = os.path.join(
            client.workdir,
            DEFAULT_DOWNLOAD_DIR,
            to_play.raw_file_name
        )
        await mp.update_start_time()
        await m_status.delete()
        print(f"- START PLAYING: {m_audio.audio.title}")
    await mp.send_playlist()
    for track in playlist[:2]:
        await download_audio(mp, track)
    if not m.audio:
        await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex(REGEX_SITES)
                   & ~filters.regex(REGEX_EXCLUDE_URL))
async def youtube_player(client: Client, message: Message):
    await add_youtube_to_playlist(client, message, message.text)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command("search", prefixes=COMMAND_PREFIX))
async def youtube_searcher(client: Client, message: Message):
    mp = MUSIC_PLAYERS.get(message.chat.id)
    if not mp:
        return

    # check playlist length
    if len(mp.playlist) >= MAX_PLAYLIST_LENGTH:
        await _reply_and_delete_later(message, f'{emoji.CROSS_MARK} There are already {MAX_PLAYLIST_LENGTH} songs in '
                                               f'the playlist, cannot add more!', DELETE_DELAY)
        return

    if len(message.command) > 1:
        keyword = " ".join(message.command[1:])
        searching = await message.reply_text(
            f"{emoji.INBOX_TRAY} Searching Youtube video with keyword `{keyword}`...", parse_mode='md')
        loop = asyncio.get_event_loop()
        res = None
        tries = 0
        while res is None and tries < 3:
            try:
                # cache youtube search result
                cachekey = 'youtube:' + keyword.lower()
                res = await cache.get(cachekey)
                if not res:
                    logging.info(f'Youtube search keyword "{keyword}" not cached, caching now')
                    res = await loop.run_in_executor(None, search_youtube, keyword)
                    await cache.set(cachekey, res)
                else:
                    logging.info(f'Youtube search keyword "{keyword}" cached, using cached result')
            except IndexError as e:
                # Really no result
                await searching.edit_text(f'{emoji.ROBOT} '
                                          f'Sorry, I can find nothing on youtube with the keyword `{keyword}`',
                                          parse_mode='md')
                return
            except Exception as e:
                # unknown error, try 3 times max
                tries += 1
                if tries == 3:
                    await log(mp, e)
                    return

        suffix = res['url_suffix']
        link = f'https://www.youtube.com{suffix}'

        await searching.delete()
        await add_youtube_to_playlist(client, message, link)
    else:
        await message.reply_text(f"{emoji.INBOX_TRAY} Please search by entering `!search keyword`...", parse_mode='md')


async def add_youtube_to_playlist(client: Client, message: Message, yt_link: str):
    mp = MUSIC_PLAYERS.get(message.chat.id)
    if not mp:
        return

    playlist = mp.playlist
    group_call = mp.group_call

    # check playlist length
    if len(playlist) >= MAX_PLAYLIST_LENGTH:
        await _reply_and_delete_later(message, f'{emoji.CROSS_MARK} There are already {MAX_PLAYLIST_LENGTH} songs in '
                                               f'the playlist, cannot add more!', DELETE_DELAY)
        return

    ydl = YoutubeDL()
    info_dict = ydl.extract_info(yt_link, download=False)

    yt_id = info_dict['id']
    yt_title = info_dict['title']
    yt_duration = info_dict['duration']

    if yt_duration > MUSIC_MAX_LENGTH:
        readable_max_length = str(timedelta(seconds=MUSIC_MAX_LENGTH))
        inform = ("This won't be downloaded because its audio length is "
                  "longer than the limit `{}` which is set by the bot"
                  .format(readable_max_length))
        await _reply_and_delete_later(message, inform,
                                      DELAY_DELETE_INFORM)
        return

    # check already added
    if playlist and playlist[-1].raw_file_name == f'YT_{yt_id}.raw':
        reply = await message.reply_text(f"{emoji.ROBOT} already added")
        await _delay_delete_messages((reply, message), DELETE_DELAY)
        return
    # add to playlist
    to_play = MusicToPlay(message, yt_title, yt_duration, f'YT_{yt_id}.raw', yt_link)
    playlist.append(to_play)
    if len(playlist) == 1:
        m_status = await message.reply_text(
            f"{emoji.INBOX_TRAY} downloading and transcoding..."
        )
        await download_audio(mp, to_play)
        group_call.input_filename = os.path.join(
            client.workdir,
            DEFAULT_DOWNLOAD_DIR,
            playlist[0].raw_file_name
        )
        await mp.update_start_time()
        await m_status.delete()
        print(f"- START PLAYING: {to_play.title}")
    await mp.send_playlist()
    for track in playlist[:2]:
        await download_audio(mp, track)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('current', prefixes=[COMMAND_PREFIX, '/']))
async def show_current_playing_time(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    start_time = mp.start_time
    playlist = mp.playlist
    if not start_time:
        reply = await m.reply_text(f"{emoji.PLAY_BUTTON} unknown")
        await _delay_delete_messages((reply, m), DELETE_DELAY)
        return
    utcnow = datetime.utcnow().replace(microsecond=0)
    if mp.msg.get('current') is not None:
        await mp.msg['current'].delete()
    mp.msg['current'] = await playlist[0].message.reply_text(
        f"**Currently Playing:**" + " " +
        f"**[{playlist[0].title}]({playlist[0].link or playlist[0].message.link})**\n" +
        f"`{emoji.PLAY_BUTTON} {utcnow - start_time}` / "
        f"`{timedelta(seconds=playlist[0].duration)}`",
        disable_notification=True,
        disable_web_page_preview=True
    )
    await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('help', prefixes=[COMMAND_PREFIX, '/']))
async def show_help(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    if mp.msg.get('help') is not None:
        await mp.msg['help'].delete()
    mp.msg['help'] = await m.reply_text(USERBOT_HELP, quote=False)
    await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.command("skip", prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def skip_track(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    playlist = mp.playlist
    if len(m.command) == 1:
        await skip_current_playing(mp)
    else:
        try:
            items = list(dict.fromkeys(m.command[1:]))
            items = [int(x) for x in items if x.isdigit()]
            items.sort(reverse=True)
            text = []
            for i in items:
                if 2 <= i <= (len(playlist) - 1):
                    audio = f"[{playlist[i].title}]({playlist[i].message.link})"
                    playlist.pop(i)
                    text.append(f"{emoji.WASTEBASKET} {i}. **{audio}**")
                else:
                    text.append(f"{emoji.CROSS_MARK} {i}")
            reply = await m.reply_text("\n".join(text))
            await mp.send_playlist()
        except (ValueError, TypeError):
            reply = await m.reply_text(f"{emoji.NO_ENTRY} invalid input",
                                       disable_web_page_preview=True)
        await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & filters.command('join', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def join_group_call(client, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    if mp and mp.group_call.is_connected:
        await m.reply_text(f"{emoji.ROBOT} already joined a voice chat")
        return
    if not mp:
        mp = MusicPlayer()
        mp.chat_id = m.chat.id
        mp.chat_title = m.chat.title
        MUSIC_PLAYERS[m.chat.id] = mp
    group_call = mp.group_call
    group_call.client = client
    await group_call.start(m.chat.id)
    await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('leave', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def leave_voice_chat(c: Client, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    mp.playlist.clear()
    group_call.input_filename = ''
    await group_call.stop()
    del MUSIC_PLAYERS[m.chat.id]
    await m.reply_text(f'{emoji.ROBOT} left the voice chat')
    await m.delete()
    _clean_files(c)


@Client.on_message(main_filter
                   & filters.command('leaveall', prefixes=COMMAND_PREFIX)
                   & global_admins_filter)
async def leave_all_voice_chat(c: Client, m: Message):
    cnt = len(MUSIC_PLAYERS)
    if cnt == 0:
        await m.reply_text(f'{emoji.ROBOT} Not in any voice chats.')
        return
    players = MUSIC_PLAYERS.copy()
    for chatid, mp in players.items():
        group_call = mp.group_call
        mp.playlist.clear()
        group_call.input_filename = ''
        await group_call.stop()
        del MUSIC_PLAYERS[chatid]
        await c.send_message(chatid, f'{emoji.ROBOT} Sorry my owner wants me back home and I have to leave now...')
    await m.reply_text(f'{emoji.ROBOT} Left {cnt} voice chats.')
    _clean_files(c)


@Client.on_message(main_filter
                   & filters.command('vc', prefixes=COMMAND_PREFIX)
                   & global_admins_filter)
async def list_voice_chat(_, m: Message):
    if not MUSIC_PLAYERS:
        await m.reply_text(f"{emoji.CROSS_MARK} **currently not in any voice chat!**")
        return

    await m.reply_text(
            f"{emoji.MUSICAL_NOTES} **currently in the voice chat(s)**:\n" +
            '\n'.join((f"{i + 1}: **{mp.chat_title} ({chat_id})**\n" +
                       f'>> Uptime: ' + str(datetime.utcnow() - mp.join_voice_chat_time) + '\n' +
                       f'>> No. of songs in queue: ' + str(len(mp.playlist))
                       for i, (chat_id, mp) in enumerate(MUSIC_PLAYERS.items())))
        )


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('stop', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def stop_playing(c: Client, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.stop_playout()
    reply = await m.reply_text(f"{emoji.STOP_BUTTON} stopped playing")
    await mp.update_start_time(reset=True)
    mp.playlist.clear()
    await _delay_delete_messages((reply, m), DELETE_DELAY)
    _clean_files(c)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('replay', prefixes=COMMAND_PREFIX))
async def restart_playing(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    if not mp.playlist:
        return
    group_call.restart_playout()
    await mp.update_start_time()
    reply = await m.reply_text(
        f"{emoji.COUNTERCLOCKWISE_ARROWS_BUTTON}  "
        "playing from the beginning..."
    )
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('pause', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def pause_playing(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    mp.group_call.pause_playout()
    await mp.update_start_time(reset=True)
    reply = await m.reply_text(f"{emoji.PLAY_OR_PAUSE_BUTTON} paused",
                               quote=False)
    mp.msg['pause'] = reply
    await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('resume', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def resume_playing(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    mp.group_call.resume_playout()
    reply = await m.reply_text(f"{emoji.PLAY_OR_PAUSE_BUTTON} resumed",
                               quote=False)
    if mp.msg.get('pause') is not None:
        await mp.msg['pause'].delete()
    await m.delete()
    await _delay_delete_messages((reply, ), DELETE_DELAY)


@Client.on_message(main_filter
                   & filters.command('clean', prefixes=COMMAND_PREFIX)
                   & global_admins_filter)
async def clean_raw_pcm(client, m: Message):
    count = _clean_files(client)
    reply = await m.reply_text(f"{emoji.WASTEBASKET} cleaned {count} files")
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('mute', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def mute(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.set_is_mute(True)
    reply = await m.reply_text(f"{emoji.MUTED_SPEAKER} muted")
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('unmute', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def unmute(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.set_is_mute(False)
    reply = await m.reply_text(f"{emoji.SPEAKER_MEDIUM_VOLUME} unmuted")
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('repo', prefixes=[COMMAND_PREFIX, '/']))
async def show_repository(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    if mp.msg.get('repo') is not None:
        await mp.msg['repo'].delete()
    mp.msg['repo'] = await m.reply_text(
        USERBOT_REPO,
        disable_web_page_preview=True,
        quote=False
    )
    await m.delete()


@Client.on_message(filters.command("joingroup", prefixes=COMMAND_PREFIX)
                   & global_admins_filter)
async def join_group(c: Client, m: Message):
    # join chat by link, supergroup/channel username, or chat id
    if len(m.command) > 1:
        link = m.command[1]
        try:
            await c.join_chat(link)
        except Exception as e:
            await m.reply_text(repr(e))
    else:
        await m.reply_text('Please provide a link, supergroup/channel username or chat id.')


@Client.on_message(filters.command("leavegroup", prefixes=COMMAND_PREFIX)
                   & global_admins_filter)
async def leave_group(c: Client, m: Message):
    # leave by supergroup/channel username, or chat id
    if len(m.command) > 1:
        link = m.command[1]
        try:
            await c.leave_chat(link)
        except Exception as e:
            await m.reply_text(repr(e))
    else:
        await m.reply_text('Please provide a supergroup/channel username or chat id.')


@Client.on_message(main_filter
                   & filters.command('cache', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def cache_chat_admin(c: Client, m: Message):
    logging.info(f'Cleaning admin list cache for chat {m.chat.id}')
    await cache.delete(m.chat.id)
    await get_chat_admins(c, m.chat.id)
    await m.reply_text('Admin cache refreshed.')


# - Other functions
def search_youtube(keyword):
    # search youtube with specific keyword and return top #1 result
    return YoutubeSearch(keyword, max_results=1).to_dict()[0]


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


async def log(mp: MusicPlayer, e: Exception):
    message = await send_text(mp, f'Error occured: {repr(e)}\nI have notified my owner about it already!')
    await send_text(mp,
                    f'Error occured at {mp.chat_id}:\n<code>' +
                    "".join(traceback.TracebackException.from_exception(e).format()) +
                    '</code>',
                    LOG_GROUP_ID)
    return message


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
    _clean_files(mp.group_call.client)
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


async def _delay_delete_messages(messages: tuple, delay: int):
    await asyncio.sleep(delay)
    for m in messages:
        await m.delete()


async def _reply_and_delete_later(message: Message, text: str, delay: int):
    reply = await message.reply_text(text, quote=True)
    await asyncio.sleep(delay)
    await reply.delete()


def _clean_files(client: Client) -> int:
    download_dir = os.path.join(client.workdir, DEFAULT_DOWNLOAD_DIR)
    all_fn = os.listdir(download_dir)
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
