import logging
from pyrogram import Client, idle, filters
from aiocache import Cache
import os

GLOBAL_ADMINS = [
    106665913,
    295152997
]
LOG_GROUP_ID = -1001243367957
COMMAND_PREFIX = '$' if os.environ.get('DEBUG') else '!'

global_admins_filter = (
    filters.incoming & filters.user(GLOBAL_ADMINS)
)

app = Client("test")
logging.basicConfig(level=logging.INFO)
cache = Cache(Cache.MEMORY)
app.start()
print('>>> USERBOT STARTED')
idle()
app.stop()
print('\n>>> USERBOT STOPPED')
