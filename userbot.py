import logging
from pyrogram import Client, idle

app = Client("test")
logging.basicConfig(level=logging.INFO)
app.start()
print('>>> USERBOT STARTED')
idle()
app.stop()
print('\n>>> USERBOT STOPPED')
