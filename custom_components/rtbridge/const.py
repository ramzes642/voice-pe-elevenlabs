DOMAIN = "rtbridge"

CONF_ESPHOME_ENTRY = "esphome_entry_id"
CONF_HOST = "host"
CONF_NOISE_PSK = "noise_psk"
CONF_API_KEY = "api_key"

OPT_MODEL = "model"
OPT_VOICE = "voice"
OPT_INSTRUCTIONS = "instructions"
OPT_COMMAND_AGENT = "command_agent"
OPT_LANGUAGE = "language"
OPT_IDLE_TIMEOUT = "idle_timeout"
OPT_MAX_SESSION = "max_session"
OPT_GREETING = "greeting"
OPT_MIC_GAIN = "mic_gain"
OPT_VAD_EAGERNESS = "vad_eagerness"
OPT_AUDIO_BASE_URL = "audio_base_url"
OPT_HA_TOOL = "ha_tool"
OPT_ECHO_GUARD = "echo_guard"

DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
DEFAULT_COMMAND_AGENT = "conversation.home_assistant"
DEFAULT_LANGUAGE = "ru"
DEFAULT_IDLE_TIMEOUT = 25
DEFAULT_MAX_SESSION = 600
DEFAULT_MIC_GAIN = 16.0
DEFAULT_VAD_EAGERNESS = "auto"
DEFAULT_ECHO_GUARD = 0.5   # seconds of mic muted after each announcement starts
DEFAULT_GREETING = False   # a greeting right after the wake word collides with users who speak at once

DEFAULT_INSTRUCTIONS = (
    "Ты — голосовой ассистент умной колонки «Солнце». Говори только по-русски, коротко и живо, "
    "одно-два предложения, без списков и без markdown. Тебя могут перебивать — это нормально. "
    "Умный дом (свет, розетки, климат, шторы, таймеры, сцены, музыка, состояние датчиков) ты "
    "НЕ контролируешь сам — для любой такой просьбы СРАЗУ, без вступительных фраз вроде «сейчас» или "
    "«секунду», вызывай инструмент home_assistant, передав команду одной русской фразой, а потом "
    "коротко сообщи результат (например «Готово» или что ответил дом). "
    "Если пользователь прощается, говорит «хватит», «спасибо, всё», «пока» или просит замолчать — "
    "коротко попрощайся и в том же ответе вызови инструмент end_conversation, передав его точные слова. "
    "Без явного прощания инструмент не вызывай. "
    "Если реплика неразборчива или похожа на шум или обрывок, скажи только «М?»."
)

STREAM_PATH = "/api/rtbridge/stream/{sid}.wav"
