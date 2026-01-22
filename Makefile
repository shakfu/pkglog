NAME := "myapp"

.PHONY: all test sync update fetch report lint typecheck format qa \
		build wheel sdist check publish publish-test clean reset

all: test

sync:
	@uv sync

test: sync
	@uv run pytest

update:
	@uv run $(NAME) update

fetch:
	@uv run $(NAME) fetch

report:
	@uv run $(NAME) report

lint:
	@uv run ruff check --fix src/

typecheck:
	@uv run mypy --strict src/

format:
	@uv run ruff format src/

qa: lint typecheck format

build: clean
	@uv build
	@uv run twine check dist/*

wheel: clean
	@uv build --wheel
	@uv run twine check dist/*

sdist: clean
	@uv build --sdist

check: build
	@uv run twine check dist/*

publish: check
	@uv run twine upload dist/*

publish-test: check
	@uv run twine upload --repository testpypi dist/*

clean:
	@rm -f report.html

reset: clean
	@rm -rf build dist .venv *.egg-info src/*.egg-info
