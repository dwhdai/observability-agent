.PHONY: install tui agent focus-payment focus-errors focus-logs reset-state

SCENARIO ?=

install:
	pip install -e .

tui:
	python -m observability_agent tui $(if $(SCENARIO),--scenario $(SCENARIO),)

agent:
	python -m observability_agent agent

# ── Dashboard state test helpers (run while TUI is open) ────────────────────

# Focus timeseries + histogram on payment-service latency, last 15m
focus-payment:
	sqlite3 observability.db "UPDATE dashboard_state SET \
		timeseries_metric='latency_p99', timeseries_service='payment-service', \
		histogram_metric='latency_p99', histogram_service='payment-service', \
		time_range_minutes=15 WHERE id=1;"

# Show only ERROR logs from payment-service, hide histogram
focus-errors:
	sqlite3 observability.db "UPDATE dashboard_state SET \
		panels='[\"overview\",\"timeseries\",\"logs\"]', \
		log_service='payment-service', log_level='ERROR' WHERE id=1;"

# Filter logs by keyword
focus-logs:
	sqlite3 observability.db "UPDATE dashboard_state SET \
		panels='[\"logs\"]', log_keyword='timeout' WHERE id=1;"

# Reset to defaults (same as pressing r in TUI)
reset-state:
	sqlite3 observability.db "UPDATE dashboard_state SET \
		panels='[\"overview\",\"timeseries\",\"histogram\",\"logs\"]', \
		timeseries_metric='latency_p99', timeseries_service='all', \
		histogram_metric='latency_p99', histogram_service='all', \
		log_level='all', log_keyword='', log_service='all', \
		time_range_minutes=30 WHERE id=1;"
