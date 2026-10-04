# Deskmate. Run from this directory.
SHELL := /bin/bash
DATA := $(HOME)/.local/share/deskmate
COMPOSE := docker compose
ISOLATED := $(COMPOSE) -f compose.yaml -f compose.isolated.yaml

.PHONY: help env up isolated down restart logs ps desk-shell

help:
	@echo "make env       create .env with a fresh token (keeps an existing one)"
	@echo "make up        build and start the desk and the hub"
	@echo "make isolated  the same, but the desk cannot reach this machine's localhost"
	@echo "make down      stop both"
	@echo "make logs      follow the logs"

# .env holds the hub token and the secretary's Claude login. Never commit it.
env:
	@if [ -f .env ]; then echo ".env exists, leaving it alone"; else \
	  cp .env.example .env && chmod 600 .env && \
	  sed -i "s|^DESKMATE_TOKEN=.*|DESKMATE_TOKEN=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')|" .env && \
	  sed -i "s|^HOST_UID=.*|HOST_UID=$$(id -u)|; s|^HOST_GID=.*|HOST_GID=$$(id -g)|" .env && \
	  echo "wrote .env (add CLAUDE_CODE_OAUTH_TOKEN from 'claude setup-token' for the secretary)"; fi

$(DATA)/hub $(DATA)/exchange:
	mkdir -p $@

up: env | $(DATA)/hub $(DATA)/exchange
	$(COMPOSE) up -d --build

isolated: env | $(DATA)/hub $(DATA)/exchange
	$(ISOLATED) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

desk-shell:
	docker exec -it -e DISPLAY=:87 -e XAUTHORITY=/tmp/.Xauthority deskmate-desk bash
