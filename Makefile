.PHONY: install lint typecheck test verify

install:
	.venv/bin/python -m pip install -e '.[dev]'

lint:
	.venv/bin/ruff check src tests

typecheck:
	.venv/bin/mypy src

test:
	.venv/bin/pytest

verify: lint typecheck test
