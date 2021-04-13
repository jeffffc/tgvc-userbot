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
import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from pyrogram import Client, filters, emoji
from pyrogram.methods.messages.download_media import DEFAULT_DOWNLOAD_DIR
from pyrogram.types import Message, ChatMember
from youtube_dl import YoutubeDL
import pickle

from utilities.config import GLOBAL_ADMINS, COMMAND_PREFIX, PICKLE_FILE_NAME
from userbot import cache, global_admins_filter

from utilities.musicplayer import MusicToPlay, MusicPlayer, MUSIC_PLAYERS, search_youtube, skip_current_playing
from utilities.musicplayer import delay_delete_messages, reply_and_delete_later, clean_files
from utilities.musicplayer import log

DELETE_DELAY = 8
MUSIC_MAX_LENGTH = 10800
MUSIC_MAX_LENGTH_NONADMIN = 900
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


async def is_from_admin(c: Client, m: Message) -> bool:
    return m.from_user.id in GLOBAL_ADMINS or m.from_user.id in await get_chat_admins(c, m.chat.id)


async def group_admin_filter_func(_, client: Client, message: Message):
    is_admin = await is_from_admin(client, message)
    if not is_admin:
        await message.reply_text(f'{emoji.NO_ENTRY} This command can only be used by a group admin.')
    return is_admin


group_admin_filter = filters.create(group_admin_filter_func)


async def get_chat_admins(c: Client, chat_id: int) -> List[int]:
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
    playlist = mp.playlist
    # check audio
    if m.audio:
        if m.audio.duration > MUSIC_MAX_LENGTH_NONADMIN:
            if await is_from_admin(client, m):
                reply = await m.reply_text(
                    f"{emoji.ROBOT} audio which duration longer than {timedelta(seconds=MUSIC_MAX_LENGTH_NONADMIN)} "
                    "won't be automatically added to playlist"
                )
            else:
                reply = await m.reply_text(
                    "This won't be downloaded because its audio length is "
                    "longer than the limit `{}` which is set by the bot"
                    .format(timedelta(seconds=MUSIC_MAX_LENGTH_NONADMIN))
                )
            await delay_delete_messages((reply,), DELETE_DELAY)
            return
        m_audio = m
    elif m.reply_to_message and m.reply_to_message.audio:
        m_audio = m.reply_to_message
    else:
        await mp.send_playlist()
        await m.delete()
        return
    # check playlist length
    if len(playlist) >= MAX_PLAYLIST_LENGTH:
        await reply_and_delete_later(m, f'{emoji.CROSS_MARK} There are already {MAX_PLAYLIST_LENGTH} songs in '
                                        f'the playlist, cannot add more!', DELETE_DELAY)
        return
    max_length = MUSIC_MAX_LENGTH if await is_from_admin(client, m) else MUSIC_MAX_LENGTH_NONADMIN
    if m_audio.audio.duration > max_length:
        inform = ("This won't be downloaded because its audio length is "
                  "longer than the limit `{}` which is set by the bot"
                  .format(timedelta(seconds=max_length)))
        await reply_and_delete_later(m, inform,
                                     DELAY_DELETE_INFORM)
        return

    # check already added
    if playlist and playlist[-1].raw_file_name == f'TG_{m_audio.audio.file_unique_id}.raw':
        reply = await m.reply_text(f"{emoji.ROBOT} already added")
        await delay_delete_messages((reply, m), DELETE_DELAY)
        return
    # add to playlist
    to_play = MusicToPlay(m_audio, m.from_user.id, m_audio.audio.title,
                          m_audio.audio.duration, f'TG_{m_audio.audio.file_unique_id}.raw', None)
    playlist.append(to_play)
    if len(playlist) == 1:
        m_status = await m.reply_text(
            f"{emoji.INBOX_TRAY} downloading and transcoding..."
        )
        await mp.play_track(to_play)
        await m_status.delete()
        print(f"- START PLAYING: {m_audio.audio.title}")
    await mp.send_playlist()
    for track in playlist[:2]:
        await mp.download_audio(track)
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
                   & filters.command("search", prefixes=[COMMAND_PREFIX, '/']))
async def youtube_searcher(client: Client, message: Message):
    mp = MUSIC_PLAYERS.get(message.chat.id)
    if not mp:
        return

    # check playlist length
    if len(mp.playlist) >= MAX_PLAYLIST_LENGTH:
        await reply_and_delete_later(message, f'{emoji.CROSS_MARK} There are already {MAX_PLAYLIST_LENGTH} songs in '
                                              f'the playlist, cannot add more!', DELETE_DELAY)
        return

    if len(message.command) > 1:
        keyword = " ".join(message.command[1:])
        searching = await message.reply_text(
            f"{emoji.INBOX_TRAY} Searching Youtube video with keyword `{keyword}`...", parse_mode='md')
        loop = asyncio.get_event_loop()
        res: Optional[Dict] = None
        tries = 0
        while res is None:
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
            except IndexError:
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
        await reply_and_delete_later(message, f'{emoji.CROSS_MARK} There are already {MAX_PLAYLIST_LENGTH} songs in '
                                              f'the playlist, cannot add more!', DELETE_DELAY)
        return

    ydl = YoutubeDL()
    info_dict = ydl.extract_info(yt_link, download=False)

    yt_id = info_dict['id']
    yt_title = info_dict['title']
    yt_duration = info_dict['duration']

    max_length = MUSIC_MAX_LENGTH if await is_from_admin(client, message) else MUSIC_MAX_LENGTH_NONADMIN
    if yt_duration > max_length:
        inform = ("This won't be downloaded because its audio length is "
                  "longer than the limit `{}` which is set by the bot"
                  .format(timedelta(seconds=max_length)))
        await reply_and_delete_later(message, inform,
                                     DELAY_DELETE_INFORM)
        return

    # check already added
    if playlist and playlist[-1].raw_file_name == f'YT_{yt_id}.raw':
        reply = await message.reply_text(f"{emoji.ROBOT} already added")
        await delay_delete_messages((reply, message), DELETE_DELAY)
        return
    # add to playlist
    to_play = MusicToPlay(message, message.from_user.id, yt_title, yt_duration, f'YT_{yt_id}.raw', yt_link)
    playlist.append(to_play)
    if len(playlist) == 1:
        m_status = await message.reply_text(
            f"{emoji.INBOX_TRAY} downloading and transcoding..."
        )
        await mp.download_audio(to_play)
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
        await mp.download_audio(track)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('current', prefixes=[COMMAND_PREFIX, '/']))
async def show_current_playing_time(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    start_time = mp.start_time
    playlist = mp.playlist
    if not start_time:
        reply = await m.reply_text(f"{emoji.PLAY_BUTTON} unknown")
        await delay_delete_messages((reply, m), DELETE_DELAY)
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
                   & filters.command("skip", prefixes=[COMMAND_PREFIX, '/']))
async def skip_track(c: Client, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    playlist = mp.playlist
    if len(m.command) == 1:
        to_skip = playlist[0]
        if to_skip.added_by == m.from_user.id or await is_from_admin(c, m):
            await skip_current_playing(mp)
        else:
            await m.reply(f"{emoji.NO_ENTRY} You can't skip songs that someone else added!")
            return
    else:
        try:
            items = list(dict.fromkeys(m.command[1:]))
            items = [int(x) for x in items if x.isdigit()]
            items.sort(reverse=True)
            text = []
            for i in items:
                if 2 <= i <= (len(playlist) - 1):
                    to_skip = playlist[i]
                    audio = f"[{to_skip.title}]({to_skip.link or to_skip.message.link})"
                    if to_skip.added_by == m.from_user.id or await is_from_admin(c, m):
                        playlist.pop(i)
                        text.append(f"{emoji.WASTEBASKET} {i}. **{audio}**")
                    else:
                        text.append(f"{emoji.NO_ENTRY} {i}: You can't skip songs that somoene else added!")
                else:
                    text.append(f"{emoji.CROSS_MARK} {i}")
            reply = await m.reply_text("\n".join(text), disable_web_page_preview=True)
            await mp.send_playlist()
        except (ValueError, TypeError):
            reply = await m.reply_text(f"{emoji.NO_ENTRY} invalid input",
                                       disable_web_page_preview=True)
        await delay_delete_messages((reply, m), DELETE_DELAY)


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
        await mp.join_group_call(client, m.chat.id, m.chat.title)
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
    clean_files(c)


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
    clean_files(c)


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
                       f'>> No. of songs in queue: ' + str(len(mp.playlist)) + '\n' +
                       f'>> Total duration: ' + str(timedelta(seconds=sum((x.duration for x in mp.playlist))))
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
    await delay_delete_messages((reply, m), DELETE_DELAY)
    clean_files(c)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('replay', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
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
    await delay_delete_messages((reply, m), DELETE_DELAY)


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
    await delay_delete_messages((reply,), DELETE_DELAY)


@Client.on_message(main_filter
                   & filters.command('clean', prefixes=COMMAND_PREFIX)
                   & global_admins_filter)
async def clean_raw_pcm(client, m: Message):
    count = clean_files(client)
    reply = await m.reply_text(f"{emoji.WASTEBASKET} cleaned {count} files")
    await delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('mute', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def mute(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.set_is_mute(True)
    reply = await m.reply_text(f"{emoji.MUTED_SPEAKER} muted")
    await delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.command('unmute', prefixes=COMMAND_PREFIX)
                   & group_admin_filter)
async def unmute(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.set_is_mute(False)
    reply = await m.reply_text(f"{emoji.SPEAKER_MEDIUM_VOLUME} unmuted")
    await delay_delete_messages((reply, m), DELETE_DELAY)


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


@Client.on_message(main_filter
                   & filters.command('halt', prefixes=COMMAND_PREFIX)
                   & global_admins_filter)
async def halt_bot(c: Client, m: Message):
    # shutting down bot, need to save all playlists and leave all chats first
    logging.info('Ready to shut down bot on request - Start saving running playlist for restore on restart')

    to_save = {}
    for chat_id, mp in MUSIC_PLAYERS.items():
        to_save[chat_id] = {}
        to_save[chat_id]['playlist'] = mp.playlist
        to_save[chat_id]['chat_title'] = mp.chat_title

    with open(PICKLE_FILE_NAME, 'wb') as f:
        pickle.dump(to_save, f)

    logging.info('Saving playlist finished, will force quit all vc now')
    await leave_all_voice_chat(c, m)
    # send keyboard interrupt
    logging.info('Left all vc, mimic Ctrl+c to shut the bot down')
    os.kill(os.getpid(), signal.SIGINT)
