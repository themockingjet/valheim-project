SHELL := /bin/bash

.DEFAULT_GOAL := help

DASHBOARD_OWNER := $(shell stat -c '%U' valheim-dashboard)
DASHBOARD_HOME := $(shell getent passwd "$(DASHBOARD_OWNER)" | cut -d: -f6)
UV ?= $(DASHBOARD_HOME)/.local/bin/uv

.PHONY: \
	help \
	server-prerequisites \
	server-firewall \
	assert-server-stopped \
	assert-server-configured \
	provision-server \
	migrate-systemd \
	dashboard-backend-test \
	dashboard-frontend-build \
	modpack-test \
	validate \
	deploy-dashboard \
	deploy-modpack \
	deploy \
	enable-server \
	enable-dashboard-actions \
	bootstrap \
	status

help:
	@printf '%s\n' \
		'Valheim Deployment Kit' \
		'' \
		'Bootstrap a new host:' \
		'  make bootstrap                 Refuse an active server, then provision/deploy; leaves Valheim disabled.' \
		'  make assert-server-stopped     Fail safely if Valheim is running.' \
		'  make assert-server-configured  Verify the host startup script and server binary.' \
		'  make migrate-systemd           Install canonical units and retire legacy kit-owned units without restart.' \
		'  make server-firewall           Open the default Valheim Steam UDP ports in UFW.' \
		'' \
		'Validate and deploy:' \
		'  make validate                  Run dashboard and modpack validation.' \
		'  make deploy                    Validate, then deploy dashboard followed by modpack (clean commit required).' \
		'  make deploy-dashboard          Deploy only the dashboard.' \
		'  make deploy-modpack            Deploy only the modpack.' \
		'' \
		'Operate services:' \
		'  make enable-server             Enable/start Valheim and its maintenance timer.' \
		'  make enable-dashboard-actions  Enable the explicitly authorized dashboard action watcher.' \
		'  make status                    Show Valheim, timer, and dashboard status.'

server-prerequisites:
	sudo apt update
	sudo apt install -y software-properties-common
	sudo add-apt-repository -y multiverse
	sudo dpkg --add-architecture i386
	sudo apt update
	sudo apt install -y steamcmd libatomic1 libpulse0 libpulse-dev

server-firewall:
	sudo ufw allow 2456:2457/udp comment 'Valheim Steam backend'
	sudo ufw reload

assert-server-stopped:
	@if sudo systemctl is-active --quiet valheim.service; then \
		printf '%s\n' 'Valheim is active; stop it and back up world data before provisioning.' >&2; \
		exit 1; \
	fi

assert-server-configured:
	@sudo -u valheim test -x /opt/valheim/server/valheim_server.x86_64 || { \
		printf '%s\n' 'Valheim server binary is missing or is not executable; run make provision-server.' >&2; \
		exit 1; \
	}
	@sudo -u valheim test -x /opt/valheim/server/start_valheim_server.sh || { \
		printf '%s\n' \
			'Missing executable /opt/valheim/server/start_valheim_server.sh.' \
			'Create the host-owned server configuration described in docs/VALHEIM_SERVER_GUIDE.md.' >&2; \
		exit 1; \
	}

provision-server:
	sudo ./scripts/server/provision-valheim-server

migrate-systemd:
	sudo ./scripts/server/migrate-valheim-systemd

dashboard-backend-test:
	test -x "$(UV)"
	cd valheim-dashboard/backend && "$(UV)" sync --locked && "$(UV)" run python -m unittest discover -s tests -v

dashboard-frontend-build:
	cd valheim-dashboard/frontend && npm ci && npm run lint && npm run build

modpack-test:
	cd valheim-modpack && python3 -m unittest discover -s tests -v

validate: dashboard-backend-test dashboard-frontend-build modpack-test

deploy-dashboard:
	sudo ./scripts/dashboard/valheim-dashboard-deploy

deploy-modpack:
	sudo ./scripts/modpack/valheim-modpack-deploy

deploy: validate
	$(MAKE) deploy-dashboard
	$(MAKE) deploy-modpack

enable-server: assert-server-configured
	@set -e; \
	sudo systemctl enable valheim.service; \
	sudo systemctl start valheim.service; \
	sleep 2; \
	if ! sudo systemctl is-active --quiet valheim.service; then \
		sudo systemctl status valheim.service --no-pager -l || true; \
		sudo systemctl disable --now valheim.service; \
		printf '%s\n' 'Valheim failed its initial start; the service was disabled to prevent a restart loop.' >&2; \
		exit 1; \
	fi; \
	sudo systemctl enable --now valheim-restart.timer

enable-dashboard-actions:
	sudo ./scripts/modpack/valheim-modpack-deploy --enable-dashboard-actions

bootstrap:
	$(MAKE) assert-server-stopped
	$(MAKE) server-prerequisites
	$(MAKE) provision-server
	$(MAKE) deploy

status:
	sudo systemctl status valheim.service valheim-dashboard.service --no-pager
	sudo systemctl list-timers --all valheim-restart.timer valheim-dashboard-status.timer --no-pager
