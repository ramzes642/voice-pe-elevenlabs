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
OPT_TURN_STALL = "turn_stall"
OPT_PLAYBACK_DUCK = "playback_duck"
OPT_START_MUTE = "start_mute"
OPT_END_AFTER_ACTION = "end_after_action"
OPT_AGC = "agc"
OPT_AGC_LEVEL = "agc_level"
OPT_NS_LEVEL = "ns_level"
OPT_RECORD = "record"

DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
DEFAULT_COMMAND_AGENT = "conversation.home_assistant"
DEFAULT_LANGUAGE = "ru"
DEFAULT_IDLE_TIMEOUT = 5   # seconds of real silence (no user speech, no response, no playback)
DEFAULT_MAX_SESSION = 600
DEFAULT_MIC_GAIN = 16.0
DEFAULT_VAD_EAGERNESS = "server"   # server_vad 0.9 s silence; semantic_vad (auto/low) split or stalled turns
DEFAULT_ECHO_GUARD = 0.5   # seconds of mic muted after each announcement starts
DEFAULT_GREETING = False
DEFAULT_AGC = True
DEFAULT_AGC_LEVEL = 0    # speex auto gain 0-31 (0 = off; the bridge's fast AGC runs before speex)
DEFAULT_NS_LEVEL = 0     # speex noise suppression 0-4 (off: it seemed to blind OpenAI's VAD)   # software AGC on the mic feed (x8..x64) instead of the fixed mic_gain
DEFAULT_RECORD = True   # debug: dump the mic feed to <config>/rtbridge_rec/ (turn off when done)
DEFAULT_END_AFTER_ACTION = True   # command done → «Готово» → session ends
DEFAULT_START_MUTE = 1.3   # s of mic ignored after the wake word (chime + AEC settling)
DEFAULT_PLAYBACK_DUCK = 0.3   # mic multiplier while the device is speaking (-10 dB)
DEFAULT_TURN_STALL = 2.0   # s after speech stopped with no response → force the turn   # a greeting right after the wake word collides with users who speak at once

# Editable in the integration options: who the assistant is and how it talks.
DEFAULT_PERSONA = (
    "Ты — голосовой ассистент умной колонки «Солнце». Говори только по-русски, коротко и живо, "
    "одно-два предложения, без списков и без markdown. Тебя могут перебивать — это нормально. "
    "После выполненной команды отвечай одним коротким словом или фразой, например «Готово», без пояснений."
)

# Appended by the integration after the persona (not editable): how to use the tools. Kept in code so
# that tool changes take effect without re-saving options.
TOOL_RULES = (
    "Правила. Умный дом (свет, розетки, климат, шторы, таймеры, сцены, музыка, состояние датчиков) ты "
    "НЕ контролируешь сам: кондиционер и климат — только через инструмент climate_control; конкретное "
    "устройство из списка (по имени) — через device_control; всё остальное (весь свет в комнате, таймеры, "
    "сцены, вопросы о состоянии) — через home_assistant, передав команду одной русской фразой. Действуй СРАЗУ, "
    "без вступительных фраз вроде «сейчас» или «секунду», потом коротко сообщи результат. "
    "Если пользователь прощается, говорит «хватит», «спасибо, всё», «пока» или просит замолчать — "
    "коротко попрощайся и в том же ответе вызови инструмент end_conversation, передав его точные слова. "
    "Без явного прощания инструмент не вызывай. "
    "Если реплика неразборчива, похожа на шум, обрывок или не содержит явной просьбы — НИКОГДА не вызывай "
    "инструменты и не додумывай команду по контексту; скажи только «М?»."
)
DEFAULT_INSTRUCTIONS = DEFAULT_PERSONA   # backwards compat

STREAM_PATH = "/api/rtbridge/stream/{sid}.wav"
