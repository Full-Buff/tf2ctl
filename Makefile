.PHONY: install-dev lint

install-dev:
	python -m pip install --upgrade pip
	pip install -r requirements.txt
	pip install -r requirements-dev.txt
	pre-commit install

lint:
	python -m pylint $(shell git ls-files '*.py')
