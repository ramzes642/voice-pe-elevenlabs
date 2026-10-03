# Sun Wake Word — обучение модели «солнце моё» для HA Voice PE

Полный проект по обучению русской wake-word **«солнце моё»** для
[Home Assistant Voice Preview Edition](https://github.com/esphome/home-assistant-voice-pe)
через компонент `micro_wake_word` ESPHome.

Документ отвечает на три вопроса: **что сейчас прошито**, **как переобучить** и
**как собрать и прошить**. История итераций и грабли — в конце.

---

## 0. Текущее состояние (на 2026-10-02)

| Что | Где | Значение |
|---|---|---|
| Прошитая модель | `firmware/sun.tflite` | md5 `cd5e504f…`, это `trained_models/sun_v2_20260628_115136` («v4») |
| Прошивка на колонке | `firmware/v14/` | собрана 28.06.2026, 2 880 224 байт factory |
| Та же модель на малине | `rpi:/opt/wake_words/sun/sun.tflite` | md5 совпадает |
| YAML на малине | `rpi:/opt/home-assistant-voice-sun.yaml` | md5 совпадает с `firmware/home-assistant-voice-sun.yaml` |
| Колонка | `192.168.68.83` | HA Voice PE, ESP32-S3 |
| Малина (HA + ESPHome-билдер) | `ramzes@192.168.68.79` (в `~/.ssh/config` как `rpi`) | HA в докере на :8123 |
| Источников фич в обучении | `training_parameters_v2.yaml` | 12 |
| Живых позитивов моего голоса | `audio_data/positives_my` | 20 (цель 50–100+) |
| Рекордер сэмплов с колонки | `rpi:/opt/sun_samples` | развёрнут, **0 записей** |

Пресеты чувствительности в прошивке v14 (уже под модель v4, см. `select.wake_word_sensitivity` в YAML):

| Пресет | uint8 cutoff | float | recall на 20 моих клипах |
|---|---:|---:|---|
| Extra strict | 247 | 0.97 | 14/20 |
| **Slightly sensitive (дефолт)** | 237 | 0.93 | ~15/20 |
| Moderately sensitive | 230 | 0.90 | 14/20 |
| Very sensitive | 217 | 0.85 | 15/20 |

Оценку модели делаем **на реальном голосе и реальных ложных срабатываниях**, а не по
стандартному ROC — см. раздел 4.4.

---

## 1. Что внутри репозитория

```
voice-pe-elevenlabs/sun-wakeword/      (локально также доступно по симлинку ~/sun_wakeword_archive)
├── README.md
├── training_parameters_v2.yaml     ← АКТУАЛЬНЫЙ конфиг тренировки (12 источников)
├── training_parameters_v2.run.yaml ← автогенерится train_v2.sh при TRAIN_MODE=fresh
├── train_v2.log                    ← лог последнего прогона
├── .venv/                          ← python 3.10 + TF 2.21 + microwakeword (editable)
├── micro-wake-word/                ← форк OHF-Voice@november-update с патчами из patches/
├── patches/                        ← audio_utils.py, spectrograms.py, utils.py, train.py
├── scripts/
│   ├── train_v2.sh                 ← запуск тренировки (GPU, WSL)
│   ├── gpu_env.sh                  ← source для интерактивной работы с GPU-venv
│   ├── prepare_v2.py               ← фичи: positive, positive_fast, sberdevices, openstt (+ mmap_split)
│   ├── prepare_fast.py             ← фичи ускоренных позитивов (только training!)
│   ├── prepare_my.py               ← фичи моих живых позитивов (только training)
│   ├── prepare_neg_my.py           ← фичи негативов моего голоса
│   ├── prepare_bg_neg.py           ← фичи bg_music / bg_speech как негативов
│   ├── make_fast_positives_sox.sh  ← sox tempo 1.25 для позитивов
│   ├── split_recording.sh          ← нарезка длинной диктофонной записи на клипы
│   ├── download_youtube_audio.sh   ← yt-dlp → 16 кГц wav фоновой речи
│   ├── generate_elevenlabs_samples.py / extract_*.py / convert_sun*.py ← история (раздел 7)
│   └── sample_recorder/            ← Wyoming-рекордер для сбора сэмплов с колонки
├── audio_data/                     ← сырые wav (источник истины)
│   ├── positives_el_v2/            ← 1456 EL «солнце моё»
│   ├── positives_el_v2_fast125/    ← те же, темп ×1.25
│   ├── positives_my/               ← 20 моих живых записей (44.1 кГц стерео, как с диктофона)
│   ├── negatives_my_raw/           ← m4a чтения текста с near-miss словами
│   └── negative_audio/{sberdevices_wav,openstt_wav}
├── data/                           ← рабочие каталоги для скриптов prepare_*
│   ├── positive_samples/{el_v2,el_v2_fast125,my_v1}
│   ├── negative_audio_my/          ← 69 кусков по 5 с, 16 кГц
│   ├── background_music/ (433)     ← музыка, ~10 с
│   ├── background_speech/ (358)    ← YT русская речь, ~30 с
│   └── negative_datasets/{speech,dinner_party,no_speech,dinner_party_eval} ← kahrendt mmap
├── precomputed_features/           ← mmap-спектрограммы, их читает тренировка
│   ├── positive, sberdevices, openstt, bg_music, bg_speech, negative_my   (training/validation/testing)
│   └── positive_fast, positive_my                                          (ТОЛЬКО training)
├── trained_models/sun_v2_<дата>/   ← прогоны; финальный = sun_v2_20260628_115136
├── trained_model/sun_v2/           ← исходная серверная модель (v8 прошивки), md5 5ae7c0bc…
└── firmware/
    ├── home-assistant-voice-sun.yaml ← ESPHome конфиг колонки
    ├── sun.json                      ← манифест micro_wake_word
    ├── sun.tflite                    ← модель, которая сейчас в прошивке
    └── v8 … v14/                     ← собранные .bin по версиям (раздел 7)
```

---

## 2. Окружение (локальная машина, WSL2 + RTX 5070)

Всё обучение идёт локально в `.venv` (python **3.10**, системный python 3.13 не подходит).
GPU работает: TF 2.21, cuDNN 9.23, RTX 5070 (compute 12.0a). Первый запуск несколько минут
JIT-компилирует PTX-ядра — это нормально. Тренировка 13 000 шагов занимает минуты.

```bash
cd ~/voice-pe-elevenlabs/sun-wakeword
source scripts/gpu_env.sh      # активирует .venv и прокидывает CUDA-библиотеки из pip-пакетов
python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"
```

`train_v2.sh` делает то же самое сам, `gpu_env.sh` нужен только для ручных экспериментов.

Если venv надо поднять заново:

```bash
python3.10 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip wheel 'setuptools<82'
pip install 'git+https://github.com/whatsnowplaying/audio-metadata@d4ebb238'
pip install -e micro-wake-word          # форк уже лежит в репо, патчи применены
pip install tensorflow[and-cuda]==2.21.* soundfile tensorboard yt-dlp mmap_ninja pymicro-features
# если micro-wake-word клонируется заново — накатить patches/*.py поверх microwakeword/ (раздел 6)
```

Системные утилиты: `sox`, `ffmpeg`.

---

## 3. Данные: 12 источников в `training_parameters_v2.yaml`

| Источник (features_dir) | truth | вес | Что это | Как сделать |
|---|---|---:|---|---|
| `precomputed_features/positive` | + | 2.5 | 1456 EL «солнце моё», аугментация рус. фоном | `prepare_v2.py` |
| `precomputed_features/positive_fast` | + | 2.0 | то же, темп ×1.25. **Только training** | `make_fast_positives_sox.sh` → `prepare_fast.py` → удалить `validation/`, `testing/` |
| `precomputed_features/positive_my` | + | 2.0 | мои живые записи, ×4 аугментации. **Только training** | `prepare_my.py` |
| `precomputed_features/sberdevices` | − | 12 | 3000 wav русской читающей речи | `prepare_v2.py` |
| `precomputed_features/openstt` | − | 12 | 4995 wav YT-речи | `prepare_v2.py` |
| `precomputed_features/bg_music` | − | 6 | 433 клипа музыки ~10 с, `slide_frames=1` | `prepare_bg_neg.py` |
| `precomputed_features/bg_speech` | − | 8 | 358 клипов YT рус. речи ~30 с, `slide_frames=1` | `download_youtube_audio.sh` → `prepare_bg_neg.py` |
| `precomputed_features/negative_my` | − | 12 | ~5.6 мин моего голоса с near-miss словами | `prepare_neg_my.py` |
| `data/negative_datasets/speech` | − | 5 | kahrendt, англ. речь | скачать (ниже) |
| `data/negative_datasets/dinner_party` | − | 8 | kahrendt, шум застолья | скачать |
| `data/negative_datasets/no_speech` | − | 6 | kahrendt, не-речь | скачать |
| `data/negative_datasets/dinner_party_eval` | − | **0.0**, `split` | ambient для FAPH. **Обязателен** | скачать |

`data/background_music` и `data/background_speech` играют двойную роль: это и негативы, и фон
для аугментации позитивов в `prepare_v2.py` / `prepare_fast.py` / `prepare_my.py`.

Все `precomputed_features/*` уже посчитаны и лежат в репо. Пересчитывать нужно только тот
источник, в который добавились wav.

kahrendt-датасеты, если их нет:

```bash
mkdir -p data/negative_datasets && cd data/negative_datasets
for d in speech dinner_party no_speech dinner_party_eval; do
  wget "https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main/${d}.zip" && unzip -q "${d}.zip"
done
```

---

## 4. Переобучение

### 4.1. Добавить живые позитивы (мой голос)

Формат обучения: **16 кГц, моно, 16 бит**, клип 0.5–2.5 с с одним «солнце моё».

**Вариант А — диктофон, одна длинная запись с паузами:**

```bash
bash scripts/split_recording.sh ~/record.m4a audio_data/positives_my my3
# env: SIL_DUR=0.4 SIL_THRESH=2% MIN_S=0.5 MAX_S=2.5 — подкрутить, если режет криво
```

**Вариант Б — отдельные файлы:**

```bash
for f in ~/new/*.wav; do sox "$f" -r 16000 -c 1 -b 16 "audio_data/positives_my/$(basename "$f")"; done
```

**Вариант В — прямо с колонки через рекордер** (раздел 5). Файлы уже в нужном формате.

Затем:

```bash
rsync -a audio_data/positives_my/ data/positive_samples/my_v1/     # 16 кГц версии
python scripts/prepare_my.py                                        # → precomputed_features/positive_my/training
```

Пока записей меньше ~50, все идут в training. Когда наберётся 50–100+, имеет смысл отложить
часть в held-out тест и мерить recall на нём.

### 4.2. Добавить негативы моего голоса

Нужны, если после добавления `positive_my` модель начала ловить мой обычный голос.
Записать чтение текста с near-miss словами (соль, солнышко, солнце, моё, со мной, команды колонке):

```bash
ffmpeg -i audio_data/negatives_my_raw/new.m4a -ac 1 -ar 16000 -sample_fmt s16 \
  -f segment -segment_time 5 data/negative_audio_my/new_%03d.wav
python scripts/prepare_neg_my.py        # → precomputed_features/negative_my/{training,validation,testing}
```

### 4.3. Запуск тренировки

```bash
cd ~/voice-pe-elevenlabs/sun-wakeword
grep -c features_dir training_parameters_v2.yaml      # должно быть 12 (и ОБЯЗАТЕЛЬНО dinner_party_eval)
TRAIN_MODE=fresh bash scripts/train_v2.sh             # новый каталог trained_models/sun_v2_<timestamp>
# TRAIN_MODE=resume (дефолт) — дообучить train_dir из training_parameters_v2.yaml
```

Параметры (внутри `train_v2.sh`): mixednet `64,64,64,64`, kernels `[5],[7,11],[9,15],[23]`,
8000 + 5000 шагов, lr 0.001 → 0.0005, batch 128, `negative_class_weight: [22, 26]`.
Результат: `trained_models/sun_v2_<ts>/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite`
(62 304 байт, INT8 streaming) и `tflite_streaming_roc.txt` рядом. Лог в `train_v2.log`.

Признак сломанного прогона: в логе `faph=nan`, `AUC=nan`, precision ~33%. Причина почти
всегда одна — в конфиге нет `dinner_party_eval`.

### 4.4. Как оценивать (важно)

- `tflite_streaming_roc.txt` считается на EL-тесте (146 синтетических клипов). Он **вводит в
  заблуждение**: тест отбрасывает первые 25 слайсов (~750 мс) трека вероятностей, поэтому
  короткие клипы (ускоренные, ~0.87 с) механически считаются пропусками. Именно поэтому
  `positive_fast` и `positive_my` не должны попадать в validation/testing.
- Реальная метрика: **recall на моих живых клипах** при рабочих порогах (0.85–0.97) и
  **отсутствие ложных срабатываний** на моей речи, музыке, ТВ. Проверять прогоном модели по
  `data/positive_samples/my_v1` и `data/negative_audio_my` (`Model.predict_spectrogram` из
  microwakeword даёт max-prob по клипу) и затем вживую на колонке день-два.
- AUC у v4 по EL-тесту 0.245 хуже, чем у серверной модели (0.110), но на реальном голосе v4
  лучше: recall 14/20 при cutoff 0.97 против 10/20.

### 4.5. Положить модель в прошивку

```bash
RUN=trained_models/sun_v2_<ts>
N=15                                                     # следующий номер прошивки
mkdir -p firmware/v$N
cp $RUN/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite firmware/sun.tflite
cp firmware/sun.tflite firmware/v$N/sun.tflite
```

Затем перебрать cutoff-ы пресетов в `firmware/home-assistant-voice-sun.yaml`
(`select.wake_word_sensitivity`, `id(sun).set_probability_cutoff(uint8)`, где uint8 = round(float × 255))
под recall/FA новой модели и обновить комментарий там же. Дальше — раздел 6.

---

## 5. Сбор живых сэмплов с колонки (рекордер)

Wyoming-сервис, который притворяется STT и просто сохраняет аудио. Код —
`scripts/sample_recorder/{recorder.py,Dockerfile}`. Развёрнут на малине:

| Параметр | Значение |
|---|---|
| контейнер / образ | `sun-sample-recorder` / `sun-sample-recorder:latest` |
| порт | **10500** |
| том | `/opt/sun_samples:/data` |
| файлы | `sun_<unix_ms>.wav`, 16 кГц моно 16 бит, root-owned |
| опции | `--prefix sun --min-ms 300` (короче 300 мс не сохраняет) |

Развернуть / обновить:

```bash
scp -r scripts/sample_recorder rpi:/tmp/
ssh rpi 'cd /tmp/sample_recorder && sudo docker build -t sun-sample-recorder . && \
  sudo docker rm -f sun-sample-recorder; \
  sudo docker run -d --name sun-sample-recorder --restart unless-stopped \
    -p 10500:10500 -v /opt/sun_samples:/data sun-sample-recorder:latest'
```

В HA: Settings → Integrations → Wyoming → host `192.168.68.79`, port `10500` → появляется STT
«sample-recorder». Создать Assist-пайплайн «Sun sample recorder» с этим STT, и запускать
запись на колонке кнопкой через `assist_satellite.start_conversation` (пустой текст, пайплайн
recorder). Запись завершается по VAD. Рядом на малине уже живут whisper :10300, piper :10200,
openwakeword :10400.

Забрать записи:

```bash
ssh rpi 'sudo cp -r /opt/sun_samples /tmp/s && sudo chown -R ramzes /tmp/s'
rsync -av rpi:/tmp/s/ audio_data/positives_my/
```

Дальше — 4.1. Сейчас в `/opt/sun_samples` пусто: пайплайн так и не был запущен в работу.

---

## 6. Сборка и прошивка ESPHome

> **Короткий путь:** из корня репозитория `make flash` (sync → compile на малинке → OTA →
> артефакты в `firmware/v<дата>/`). Отдельно: `make build`, `make upload`, `make artifacts V=15`,
> `make flash-usb V=15`, `make logs`, `make test`. Ниже — то же самое руками.


Сборка идёт **на малине** через Docker-образ `ghcr.io/esphome/esphome:latest`
(YAML требует `min_version: 2026.5.0`). Докер там под sudo.

Раскладка на малине (`/opt` монтируется как `/config` в контейнере):

```
/opt/home-assistant-voice-sun.yaml       ← конфиг
/opt/secrets.yaml                        ← wifi_ssid / wifi_password
/opt/wake_words/sun/{sun.json,sun.tflite}← модель (в YAML: /config/wake_words/sun/sun.json)
/opt/.esphome/build/home-assistant-voice/.pioenvs/home-assistant-voice/*.bin ← результат
```

### 6.1. Залить модель и YAML

```bash
scp firmware/sun.tflite firmware/sun.json rpi:/tmp/
scp firmware/home-assistant-voice-sun.yaml rpi:/tmp/
ssh rpi 'sudo cp /tmp/sun.tflite /tmp/sun.json /opt/wake_words/sun/ && \
         sudo cp /tmp/home-assistant-voice-sun.yaml /opt/ && \
         md5sum /opt/wake_words/sun/sun.tflite'
```

### 6.2. Собрать

```bash
ssh rpi 'cd /opt && sudo docker run --rm -v /opt:/config -v /opt/.esphome:/cache \
  ghcr.io/esphome/esphome:latest compile home-assistant-voice-sun.yaml'
```

Первая сборка качает тулчейн IDF и идёт долго (десятки минут на малине), последующие — минуты.

### 6.3. Прошить по OTA (обычный путь)

```bash
ssh rpi 'cd /opt && sudo docker run --rm --network host -v /opt:/config -v /opt/.esphome:/cache \
  ghcr.io/esphome/esphome:latest upload home-assistant-voice-sun.yaml --device 192.168.68.83'
```

Альтернатива: ESPHome dashboard / HA → загрузить `firmware.ota.bin` вручную.

### 6.4. Сохранить артефакты в репо

```bash
B=/opt/.esphome/build/home-assistant-voice/.pioenvs/home-assistant-voice
ssh rpi "sudo cp $B/firmware.factory.bin $B/firmware.ota.bin /tmp/ && sudo chown ramzes /tmp/firmware.*.bin"
scp rpi:/tmp/firmware.factory.bin rpi:/tmp/firmware.ota.bin firmware/v$N/
```

### 6.5. Прошить по USB (только если OTA недоступен / кирпич)

Chrome/Edge: [web.esphome.io](https://web.esphome.io) → Connect → `firmware/vN/firmware.factory.bin`.
Или:

```bash
pip install esptool
esptool.py --chip esp32s3 --port /dev/ttyACM0 write_flash 0x0 firmware/v14/firmware.factory.bin
```

### 6.6. Что изменено в YAML относительно официального

Форк [`home-assistant-voice-pe/home-assistant-voice.yaml`](https://github.com/esphome/home-assistant-voice-pe/blob/dev/home-assistant-voice.yaml):

1. `wifi:` — `ssid: !secret wifi_ssid`, `password: !secret wifi_password` (без Improv BLE).
2. `micro_wake_word.models` — `okay_nabu` заменён на `- model: /config/wake_words/sun/sun.json, id: sun`.
3. `select.wake_word_sensitivity` — cutoff-ы под нашу модель (таблица в разделе 0).
4. `external_components` — локальные патченные `voice_assistant` и `api` из `firmware/components/`
   (перехват подписки голосового ассистента для rtbridge; фикс null-pointer при ответе не-владельцу),
   `make sync` кладёт их в `/opt/components` на малинке.
5. `media_player.announcement_pipeline.format: NONE` — включает все кодеки (rtbridge стримит WAV).

`sun.json`: `probability_cutoff: 0.65` (перекрывается пресетом из YAML при старте),
`sliding_window_size: 10`, `tensor_arena_size: 30000`.

### 6.7. Патчи microwakeword (нужны при свежем клоне форка)

```bash
cp patches/audio_utils.py  micro-wake-word/microwakeword/audio/audio_utils.py
cp patches/spectrograms.py micro-wake-word/microwakeword/audio/spectrograms.py
cp patches/utils.py        micro-wake-word/microwakeword/utils.py
cp patches/train.py        micro-wake-word/microwakeword/train.py
```

- `utils.py`: `converter._experimental_variable_quantization = True`. Без этого в TFLite
  появляется `DEQUANTIZE`, которого нет в резолвере ESPHome, и **колонка падает** на первом
  инференсе (`Failed to get registration from op code DEQUANTIZE`).
- `spectrograms.py`, `audio_utils.py`: `use_c=False` и ленивые импорты — иначе segfault при
  генерации фич.
- `train.py`: убран `.numpy()` на numpy-массивах (TF 2.21).

---

## 7. История моделей и прошивок

### Прогоны тренировки (`trained_models/`)

| Прогон | Источники | md5 tflite | Заметка |
|---|---|---|---|
| `trained_model/sun_v2` (сервер, май) | 7 | `5ae7c0bc` | базовая, AUC 0.110, прошивка v8 |
| `sun_v2_20260530_130746`, `_132519` | 3 | — | **сломаны**: без dinner_party_eval, faph=nan |
| `sun_v2_20260530_133414` | 7 | `bca594da` | корректное локальное воспроизведение, AUC 0.206 |
| `sun_v2_20260530_144529` | +fast | `87f181be` | fast попал в test, метрика испорчена, не использовать |
| `sun_v2_20260530_150546` | +fast, bg_music, bg_speech | `4fbfd032` | прошивка v10 |
| `sun_v2_20260601_143450` | +positive_my («v3») | `e71047d1` | прошивки v11, v12; появились FA на моём голосе |
| **`sun_v2_20260628_115136`** | +negative_my («v4») | `cd5e504f` | **текущая**, прошивка v14 |

### Прошивки (`firmware/`)

| Версия | Дата | Модель | Что менялось |
|---|---|---|---|
| v8 | 26.05 | sun_v2 | полное переобучение на EL-рус + рус. негативы, первая рабочая |
| v9 | 30.05 | — | промежуточная сборка |
| v10 | 01.06 | 150546 | + fast / bg-негативы |
| v11, v12 | 01.06 | 143450 | + мои позитивы; v12 — правка пресетов |
| v13 | 28.06 | — | только бэкап tflite от 143450 |
| **v14** | 28.06 | 115136 (v4) | + мои негативы, пресеты ужесточены (0.85–0.97) |

До v8 были PTQ-итерации v1–v7 поверх английской Piper-модели (`scripts/convert_sun*.py`):
убирали DEQUANTIZE, подбирали representative dataset, поднимали cutoff. Все они всё равно
реагировали на «полотенце» и «как дела», поэтому пришли к полному переобучению.

---

## 8. Грабли (запомнить)

1. **Нет `dinner_party_eval` в конфиге → faph=nan, модель не учится.** Проверять число источников перед запуском.
2. **Короткие позитивы в test ломают ROC.** `positive_fast` и `positive_my` — только `training/`.
3. **`DEQUANTIZE` в TFLite → краш колонки.** Нужен патч `utils.py`.
4. **Segfault в `SpectrogramGeneration`** — патчи `spectrograms.py`/`audio_utils.py` (`use_c=False`).
5. **`AttributeError ... .numpy()`** в `train.py` на TF 2.21 — патч.
6. **`TBNotInstalledError`** — `pip install tensorboard`.
7. **Системный python 3.13 не подходит** — работать только из `.venv` (3.10) или через `gpu_env.sh`.
8. **torchcodec сегфолтит** при стриминге HF datasets — `extract_sberdevices.py` качает parquet напрямую.
9. **Файлы в `/opt/sun_samples` и build-артефакты на малине root-owned** — копировать через `sudo cp` + `chown`.
10. **ssh `rpi` по имени может не резолвиться из WSL** — использовать IP `192.168.68.79`.

---

## 9. Ссылки

- microWakeWord (upstream): https://github.com/kahrendt/microWakeWord
- OHF-Voice fork, ветка november-update (на нём обучаем): https://github.com/OHF-Voice/micro-wake-word/tree/november-update
- HA Voice PE config: https://github.com/esphome/home-assistant-voice-pe
- kahrendt mmap-датасеты: https://huggingface.co/datasets/kahrendt/microwakeword
- sberdevices_golos: https://huggingface.co/datasets/bond005/sberdevices_golos_10h_crowd
- open_stt: https://github.com/snakers4/open_stt
- ESPHome micro_wake_word: https://esphome.io/components/micro_wake_word
