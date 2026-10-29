# crate-tracing. `make poc` needs warden on PATH and a container runtime it
# can drive; everything else runs anywhere with Python 3.11+.
VENV ?= .venv
PY   := $(VENV)/bin/python
CT   := $(VENV)/bin/crate-tracing

.PHONY: setup test lint fmt poc measure summary clean

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

# The whole PoC corpus in every profile; promotes findings into findings/.
poc: $(PY)
	$(CT) measure --locked --promote

# One measurement: make measure ID=openssl-sys-0.9.117 [PROFILE=bare]
measure: $(PY)
	$(CT) measure --locked --promote --only $(ID) $(if $(PROFILE),--profile $(PROFILE))

summary: $(PY)
	$(CT) summary

clean:
	rm -rf runs
