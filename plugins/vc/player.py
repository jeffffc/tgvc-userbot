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
from pyrogram.types import Message
from pyrogram.methods.messages.download_media import DEFAULT_DOWNLOAD_DIR
from pytgcalls import GroupCall
import ffmpeg
from youtube_dl import YoutubeDL
from youtube_search import YoutubeSearch
from typing import Optional, List, Dict

from userbot import global_admins_filter

DELETE_DELAY = 8
MUSIC_MAX_LENGTH = 10800
DELAY_DELETE_INFORM = 10
TG_THUMB_MAX_LENGTH = 320

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
    await skip_current_playing(mp)


# - classes


class MusicToPlay(object):
    def __init__(self, m: Message, title: str, duration: int, file_path: Optional[str]):
        self.message = m
        self.title = title
        self.duration = duration

        # Will only be present for YouTube audios. They are downloaded immediately.
        # This is the relative path from the current working directory to the raw file.
        self.file_path = file_path


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
        else:
            if len(playlist) == 1:
                pl = f"{emoji.REPEAT_SINGLE_BUTTON} **Playlist**:\n"
            else:
                pl = f"{emoji.PLAY_BUTTON} **Playlist**:\n"
            pl += "\n".join([
                f"**{i}**. **[{x.title}]({x.message.link})**"
                for i, x in enumerate(playlist)
            ])
        if mp.msg.get('playlist') is not None:
            await mp.msg['playlist'].delete()
        mp.msg['playlist'] = await send_text(mp, pl)


MUSIC_PLAYERS: Dict[int, MusicPlayer] = {}


# - Pyrogram handlers

@Client.on_message(
    filters.group
    & ~filters.edited
    & current_vc
    & (filters.regex("^(\\/|!)play$") | filters.audio)
)
async def play_track(client, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    playlist = mp.playlist
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
    # check already added
    if playlist and playlist[-1].message.audio and playlist[-1].message.audio.file_unique_id \
            == m_audio.audio.file_unique_id:
        reply = await m.reply_text(f"{emoji.ROBOT} already added")
        await _delay_delete_messages((reply, m), DELETE_DELAY)
        return
    # add to playlist
    playlist.append(MusicToPlay(m_audio, m_audio.audio.title, m.audio.duration, None))
    if len(playlist) == 1:
        m_status = await m.reply_text(
            f"{emoji.INBOX_TRAY} downloading and transcoding..."
        )
        await download_audio(mp, m_audio)
        group_call.input_filename = os.path.join(
            client.workdir,
            DEFAULT_DOWNLOAD_DIR,
            f"{m_audio.audio.file_unique_id}.raw"
        )
        await mp.update_start_time()
        await m_status.delete()
        print(f"- START PLAYING: {m_audio.audio.title}")
    await mp.send_playlist()
    for track in playlist[:2]:
        if not track.file_path:
            await download_audio(mp, track.message)
    if not m.audio:
        await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex(REGEX_SITES)
                   & ~filters.regex(REGEX_EXCLUDE_URL))
async def music_downloader(client: Client, message: Message):
    mp = MUSIC_PLAYERS.get(message.chat.id)
    if not mp:
        return

    processing = await message.reply_text(f"{emoji.INBOX_TRAY} Processing Youtube video...")
    try:
        await process_youtube_link(message.text, client, message, processing, mp)
    except Exception as e:
        await message.reply_text(repr(e))


@Client.on_message(main_filter
                   & current_vc
                   & filters.command("search", prefixes="!"))
async def music_searcher(client: Client, message: Message):
    mp = MUSIC_PLAYERS.get(message.chat.id)
    if not mp:
        return

    if len(message.command) > 1:
        keyword = " ".join(message.command[1:])
        searching = await message.reply_text(
            f"{emoji.INBOX_TRAY} Searching Youtube video with keyword `{keyword}`...", parse_mode='md')
        loop = asyncio.get_event_loop()
        try:
            res = await loop.run_in_executor(None, search_youtube, keyword)
        except Exception as e:
            await message.reply_text(repr(e))
        title = res['title']
        suffix = res['url_suffix']
        link = f'https://www.youtube.com{suffix}'
        processing = await searching.edit_text(
            f"{emoji.INBOX_TRAY} Processing Youtube search result #1 `{title}`...", 'md')
        await process_youtube_link(link, client, message, processing, mp)
    else:
        await message.reply_text(f"{emoji.INBOX_TRAY} Please search by entering `!search keyword`...", parse_mode='md')


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex("^(\\/|!)current$"))
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
        f"{emoji.PLAY_BUTTON}  {utcnow - start_time} / "
        f"{timedelta(seconds=playlist[0].duration)}",
        disable_notification=True
    )
    await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex("^(\\/|!)help$"))
async def show_help(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    if mp.msg.get('help') is not None:
        await mp.msg['help'].delete()
    mp.msg['help'] = await m.reply_text(USERBOT_HELP, quote=False)
    await m.delete()


@Client.on_message(main_filter
                   & current_vc
                   & filters.command("skip", prefixes="!"))
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
                   & filters.regex("^!join$"))
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
                   & filters.command('leave', prefixes='!'))
async def leave_voice_chat(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    mp.playlist.clear()
    group_call.input_filename = ''
    await group_call.stop()
    del MUSIC_PLAYERS[m.chat.id]
    await m.reply_text(f'{emoji.ROBOT} left the voice chat')
    await m.delete()


@Client.on_message(main_filter
                   & filters.command('leaveall', prefixes='!')
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


@Client.on_message(main_filter
                   & filters.command('vc', prefixes='!')
                   & global_admins_filter)
async def list_voice_chat(_, m: Message):
    if not MUSIC_PLAYERS:
        await m.reply_text(f"{emoji.CROSS_MARK} **currently not in any voice chat!**")
        return

    await m.reply_text(
            f"{emoji.MUSICAL_NOTES} **currently in the voice chat(s)**:\n" +
            '\n'.join((f"{i + 1}: **{mp.chat_title} ({chat_id})**" for i, (chat_id, mp) in enumerate(MUSIC_PLAYERS.items())))
        )


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex("^!stop$"))
async def stop_playing(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.stop_playout()
    reply = await m.reply_text(f"{emoji.STOP_BUTTON} stopped playing")
    await mp.update_start_time(reset=True)
    mp.playlist.clear()
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex("^!replay$"))
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
                   & filters.regex("^!pause"))
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
                   & filters.regex("^!resume"))
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
                   & filters.regex("^!clean$")
                   & global_admins_filter)
async def clean_raw_pcm(client, m: Message):
    count = _clean_files(client)
    reply = await m.reply_text(f"{emoji.WASTEBASKET} cleaned {count} files")
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex("^!mute$"))
async def mute(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.set_is_mute(True)
    reply = await m.reply_text(f"{emoji.MUTED_SPEAKER} muted")
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex("^!unmute$"))
async def unmute(_, m: Message):
    mp = MUSIC_PLAYERS.get(m.chat.id)
    group_call = mp.group_call
    group_call.set_is_mute(False)
    reply = await m.reply_text(f"{emoji.SPEAKER_MEDIUM_VOLUME} unmuted")
    await _delay_delete_messages((reply, m), DELETE_DELAY)


@Client.on_message(main_filter
                   & current_vc
                   & filters.regex("^(\\/|!)repo$"))
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


# - Other functions
async def process_youtube_link(youtube_link, client: Client, original_message: Message,
                               processing_message: Message, mp: MusicPlayer):
    try:
        ydl_opts = {
            'format': 'bestaudio',
            'outtmpl': '%(title)s - %(extractor)s-%(id)s.%(ext)s',
        }
        ydl = YoutubeDL(ydl_opts)
        info_dict = ydl.extract_info(youtube_link, download=False)

        if info_dict['duration'] > MUSIC_MAX_LENGTH:
            readable_max_length = str(timedelta(seconds=MUSIC_MAX_LENGTH))
            inform = ("This won't be downloaded because its audio length is "
                      "longer than the limit `{}` which is set by the bot"
                      .format(readable_max_length))
            await _reply_and_delete_later(original_message, inform,
                                          DELAY_DELETE_INFORM)
            return

        raw_file = f'{DEFAULT_DOWNLOAD_DIR}YT_{info_dict["id"]}.raw'
        if not os.path.isfile(raw_file):
            ydl.process_info(info_dict)
            audio_file = ydl.prepare_filename(info_dict)
            ffmpeg.input(audio_file).filter('loudnorm').output(
                raw_file,
                format='s16le',
                acodec='pcm_s16le',
                ac=2,
                ar='48k',
                loglevel='error'
            ).overwrite_output().run_async(pipe_stdin=True)
            os.remove(audio_file)
        await processing_message.delete()
        mp.playlist.append(MusicToPlay(original_message, info_dict["title"], info_dict["duration"], raw_file))
        if len(mp.playlist) == 1:
            mp.group_call.input_filename = os.path.join(
                client.workdir,
                raw_file
            )
            await mp.update_start_time()
            print(f"- START PLAYING: {mp.playlist[0].title}")
        await mp.send_playlist()
        for track in mp.playlist[:2]:
            if not track.file_path:
                await download_audio(mp, track.message)
    except Exception as e:
        await original_message.reply_text(repr(e))


def search_youtube(keyword):
    # search youtube with specific keyword and return top #1 result
    return YoutubeSearch(keyword, max_results=1).to_dict()[0]


async def send_text(mp: MusicPlayer, text: str):
    group_call = mp.group_call
    client = group_call.client
    chat_id = mp.chat_id
    message = await client.send_message(
        chat_id,
        text,
        disable_web_page_preview=True,
        disable_notification=True
    )
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
    group_call.input_filename = playlist[1].file_path or os.path.join(
        download_dir,
        f"{playlist[1].message.audio.file_unique_id}.raw"
    )
    await mp.update_start_time()
    # remove old track from playlist
    old_track = playlist.pop(0)
    print(f"- START PLAYING: {playlist[0].title}")
    await mp.send_playlist()
    _clean_files(mp.group_call.client)
    if len(playlist) == 1:
        return
    if not playlist[1].file_path:
        await download_audio(mp, playlist[1].message)


async def download_audio(mp: MusicPlayer, m: Message):
    group_call = mp.group_call
    client = group_call.client
    raw_file = os.path.join(client.workdir, DEFAULT_DOWNLOAD_DIR,
                            f"{m.audio.file_unique_id}.raw")
    if not os.path.isfile(raw_file):
        original_file = await m.download()
        ffmpeg.input(original_file).filter('loudnorm').output(
            raw_file,
            format='s16le',
            acodec='pcm_s16le',
            ac=2,
            ar='48k',
            loglevel='error'
        ).overwrite_output().run()
        os.remove(original_file)


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
            if track.message.audio:
                track_fn = f"{track.message.audio.file_unique_id}.raw"
                if track_fn in all_fn:
                    all_fn.remove(track_fn)
        for track in mp.playlist:
            if track.file_path:
                track_fn = os.path.basename(track.file_path)
                if track_fn in all_fn:
                    all_fn.remove(track_fn)

    count = 0
    if all_fn:
        for fn in all_fn:
            if fn.endswith(".raw"):
                count += 1
                os.remove(os.path.join(download_dir, fn))
    return count
