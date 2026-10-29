# crate-tracing. `make poc` needs warden on PATH and a container runtime it
# can drive; everything else runs anywhere with Python 3.11+.
VENV ?= .venv
PY   := $(VENV)/bin/python
CT   := $(VENV)/bin/crate-tracing

# The BuildWarden commit the published findings were measured with. `make
# warden` installs exactly that build into .tools/; a source build makes its
# holder and netns images locally from the same commit. Every finding records
# the commit of the warden it actually ran (measurement.tool.warden).
BUILDWARDEN_REV ?= 137feae2f8b07e19e21b0893cc4ec9e96f97d42f
WARDEN          ?= warden

.PHONY: setup test lint fmt warden poc measure summary clean

$(PY):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -e '.[dev]'

setup: $(PY)

test: $(PY)
	$(VENV)/bin/pytest -q

lint: $(PY)
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests
	sh -n src/crate_tracing/guest/measure.sh

fmt: $(PY)
	$(VENV)/bin/ruff format src tests
	$(VENV)/bin/ruff check --fix src tests

warden:
	GOBIN=$(abspath .tools) go install github.com/buildwarden/buildwarden/cmd/warden@$(BUILDWARDEN_REV)
	@echo "use: make poc WARDEN=$(abspath .tools)/warden"

# The whole PoC corpus in every profile; promotes findings into findings/.
poc: $(PY)
	$(CT) measure --warden $(WARDEN) --locked --promote

# One measurement: make measure ID=openssl-sys-0.9.117 [PROFILE=bare]
measure: $(PY)
	$(CT) measure --warden $(WARDEN) --locked --promote --only $(ID) $(if $(PROFILE),--profile $(PROFILE))

summary: $(PY)
	$(CT) summary

clean:
	rm -rf runs
