# Voice PE firmware: build on the Raspberry Pi (ESPHome docker image) and flash the колонка.
#
#   make flash            sync YAML + wake-word model to the Pi, compile, OTA-upload, save artifacts
#   make build            sync + compile only
#   make upload           OTA-upload the last build to $(DEVICE)
#   make flash-usb        flash firmware.factory.bin over the USB cable from this laptop (esptool)
#   make logs             tail the device's ESPHome log over USB
#   make test             hwtest/ping_pong.py against the device
#
# Overridable: RPI=rpi DEVICE=192.168.68.83 FW_DIR=sun-wakeword/firmware FW_YAML=... V=<label>
# Secrets (wifi etc.) live in $(RPI_DIR)/secrets.yaml on the Pi, never in the repo.

RPI         ?= rpi
RPI_DIR     ?= /opt
FW_DIR      ?= sun-wakeword/firmware
FW_YAML     ?= home-assistant-voice-sun.yaml
MODEL_FILES ?= sun.json sun.tflite
DEVICE      ?= 192.168.68.83
ESPHOME_IMG ?= ghcr.io/esphome/esphome:latest
V           ?= $(shell date +%Y%m%d_%H%M)
SERIAL      ?= $(firstword $(wildcard /dev/cu.usbmodem*))
PY          ?= hwtest/.venv/bin/python

RPI_BUILD   := $(RPI_DIR)/.esphome/build/home-assistant-voice/.pioenvs/home-assistant-voice
DOCKER      := sudo docker run --rm --network host -v $(RPI_DIR):/config -v $(RPI_DIR)/.esphome:/cache $(ESPHOME_IMG)
OUT         := $(FW_DIR)/v$(V)

.PHONY: help sync build upload flash artifacts flash-usb logs test clean-build bridge-deploy bridge-install bridge-restart bridge-stop bridge-logs bridge-loopback ha-deploy ha-restart ha-logs

help:
	@sed -n '2,12p' $(MAKEFILE_LIST)

## sync: copy YAML + model to the Pi and verify checksums
sync:
	scp $(FW_DIR)/$(FW_YAML) $(addprefix $(FW_DIR)/,$(MODEL_FILES)) $(RPI):/tmp/
	ssh $(RPI) 'sudo mkdir -p $(RPI_DIR)/wake_words/sun && sudo cp /tmp/$(FW_YAML) $(RPI_DIR)/ \
	  && cd /tmp && sudo cp $(MODEL_FILES) $(RPI_DIR)/wake_words/sun/ \
	  && md5sum $(RPI_DIR)/$(FW_YAML) $(RPI_DIR)/wake_words/sun/sun.tflite'
	@md5 -q $(FW_DIR)/$(FW_YAML) $(FW_DIR)/sun.tflite | sed 's/^/local  /'
	rsync -a --delete $(FW_DIR)/components/ $(RPI):/tmp/fw_components/
	ssh $(RPI) 'sudo rsync -a --delete /tmp/fw_components/ $(RPI_DIR)/components/'
	rsync -a $(FW_DIR)/sounds/ $(RPI):/tmp/fw_sounds/
	ssh $(RPI) 'sudo mkdir -p $(RPI_DIR)/sounds && sudo rsync -a /tmp/fw_sounds/ $(RPI_DIR)/sounds/'

## build: compile on the Pi (first build downloads the IDF toolchain, tens of minutes; later ones take minutes)
build: sync
	ssh $(RPI) 'cd $(RPI_DIR) && $(DOCKER) compile $(FW_YAML)'

## upload: OTA from the Pi to the device
upload:
	ssh $(RPI) 'cd $(RPI_DIR) && $(DOCKER) upload $(FW_YAML) --device $(DEVICE)'

## artifacts: pull factory/ota bins into $(OUT)/ (bins are gitignored)
artifacts:
	mkdir -p $(OUT)
	ssh $(RPI) 'sudo cp $(RPI_BUILD)/firmware.factory.bin $(RPI_BUILD)/firmware.ota.bin /tmp/ && sudo chown $$USER /tmp/firmware.*.bin'
	scp $(RPI):/tmp/firmware.factory.bin $(RPI):/tmp/firmware.ota.bin $(OUT)/
	cp $(FW_DIR)/sun.tflite $(OUT)/
	@ls -la $(OUT)

## flash: the whole thing
flash: build upload artifacts

## flash-usb: write the factory image over USB (device must be plugged into this laptop)
flash-usb:
	@test -n "$(SERIAL)" || (echo "no /dev/cu.usbmodem* — plug the Voice PE in via USB"; exit 1)
	@test -f $(OUT)/firmware.factory.bin || (echo "no $(OUT)/firmware.factory.bin — run 'make artifacts V=$(V)' or pass V=<existing>"; exit 1)
	$(PY) -m esptool --chip esp32s3 --port $(SERIAL) --baud 921600 write_flash 0x0 $(OUT)/firmware.factory.bin

## logs: ESPHome log over USB
logs:
	$(PY) hwtest/devlog.py $(SERIAL)

## test: acoustic ping/pong against the device
test:
	cd hwtest && .venv/bin/python ping_pong.py

## clean-build: wipe the Pi's build dir for this node (forces a full rebuild)
clean-build:
	ssh $(RPI) 'sudo rm -rf $(RPI_DIR)/.esphome/build/home-assistant-voice'

# ---- rtbridge (runs on the Pi) ----------------------------------------------------------
BRIDGE_DIR ?= /home/ramzes/rtbridge

## bridge-deploy: rsync rtbridge/ to the Pi, create venv, install deps (idempotent)
bridge-deploy:
	ssh $(RPI) 'mkdir -p $(BRIDGE_DIR)/rtbridge'
	rsync -a --delete --exclude .env --exclude __pycache__ rtbridge/ $(RPI):$(BRIDGE_DIR)/rtbridge/
	ssh $(RPI) 'cd $(BRIDGE_DIR) && test -x .venv/bin/python || python3 -m venv .venv; .venv/bin/pip -q install -r rtbridge/requirements.txt'

## bridge-install: install + enable the systemd unit (then: make bridge-restart)
bridge-install: bridge-deploy
	ssh $(RPI) 'sudo cp $(BRIDGE_DIR)/rtbridge/rtbridge.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable rtbridge'

bridge-restart:
	ssh $(RPI) 'sudo systemctl restart rtbridge && sleep 1 && systemctl --no-pager -l status rtbridge | head -12'

bridge-stop:
	ssh $(RPI) 'sudo systemctl stop rtbridge'

bridge-logs:
	ssh $(RPI) 'journalctl -u rtbridge -f -n 50'

## bridge-loopback: stage-1 check, runs in the foreground on the Pi (stop the service first)
bridge-loopback:
	ssh -t $(RPI) 'cd $(BRIDGE_DIR) && .venv/bin/python -m rtbridge.main --mode loopback --record-dir rec'

# ---- rtbridge as a Home Assistant custom integration (HA runs in docker on the Pi) -------------
HA_CONFIG ?= /root/ha-config
HA_CONTAINER ?= homeassistant

## ha-deploy: copy custom_components/rtbridge into HA's config dir and import-check it inside the container
ha-deploy:
	rsync -a --delete --exclude __pycache__ custom_components/rtbridge/ $(RPI):/tmp/rtbridge_cc/
	ssh $(RPI) 'sudo mkdir -p $(HA_CONFIG)/custom_components && sudo rsync -a --delete /tmp/rtbridge_cc/ $(HA_CONFIG)/custom_components/rtbridge/ \
	  && sudo docker exec -w /config -e PYTHONPATH=/config $(HA_CONTAINER) python3 -c "import custom_components.rtbridge, custom_components.rtbridge.config_flow, custom_components.rtbridge.bridge; print(\"rtbridge imports OK inside HA\")"'

## ha-restart: restart the Home Assistant container (ask the owner first — it is live home infrastructure)
ha-restart:
	ssh $(RPI) 'sudo docker restart $(HA_CONTAINER) && echo restarted'

ha-logs:
	ssh $(RPI) 'sudo docker logs -f --tail 100 $(HA_CONTAINER) 2>&1 | grep -i rtbridge'
