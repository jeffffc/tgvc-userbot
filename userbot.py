import logging
from pyrogram import Client, idle, filters

GLOBAL_ADMINS = [
    106665913,
    295152997
]

global_admins_filter = (
    filters.incoming & filters.user(GLOBAL_ADMINS)
)

app = Client("test")
logging.basicConfig(level=logging.INFO)
app.start()
print('>>> USERBOT STARTED')
idle()
app.stop()
print('\n>>> USERBOT STOPPED')
