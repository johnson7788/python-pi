# One entry point per task, so "how do I check this repo" has a single answer.
#
# ``uv`` is the intended runner.  When it is not installed (or its install is
# broken, which happens), every target falls back to the checked-in ``.venv`` —
# the goal is that a newcomer with a working venv can run ``make check`` and get
# the same verdict CI does.

PY ?= .venv/bin/python
UV ?= $(shell command -v uv 2>/dev/null)
# Ruff is deliberately *not* a project dependency: keeping it out of the lock means
# a lint fix does not force a dependency re-resolution.  ``uv tool run`` (uvx)
# downloads and caches it; a venv with ruff installed works too.
RUFF ?= $(if $(UV),$(UV) tool run ruff,.venv/bin/ruff)

ifeq ($(UV),)
RUN := $(PY) -m
else
RUN := $(UV) run
endif

.PHONY: help test lint fmt check integration web-test web-install

help:
	@echo "make test         run the Python test suite (quick, offline)"
	@echo "make integration  run tests that need a docker daemon"
	@echo "make lint         ruff check"
	@echo "make fmt          ruff format"
	@echo "make check        lint + tests (what CI blocks on; format is advisory)"
	@echo "make web-install  install web/ dev dependencies"
	@echo "make web-test     typecheck the frontend and run the stream-reducer tests"

test:
	$(RUN) pytest -q

integration:
	PI_REQUIRE_INTEGRATION=1 $(RUN) pytest -q -m integration

lint:
	$(RUFF) check .

fmt:
	$(RUFF) format .

# Lint and tests block; formatting is advisory until the backlog is paid down.
check:
	$(RUFF) check .
	$(RUN) pytest -q

web-install:
	cd web && npm ci

web-test:
	cd web && npm run typecheck && npm run verify:reducer
