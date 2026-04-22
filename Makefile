.PHONY: install tui agent

install:
	pip install -e .

tui:
	python -m observability_agent tui

agent:
	python -m observability_agent agent
