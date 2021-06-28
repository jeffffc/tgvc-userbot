import os

GLOBAL_ADMINS = [
    106665913,
    295152997
]
LOG_GROUP_ID = -1001243367957
COMMAND_PREFIX = '$' if os.environ.get('DEBUG') else '!'
PICKLE_FILE_NAME = 'db.pickle'
GROUP_CONFIG_FILE_NAME = 'groupconfig.json'