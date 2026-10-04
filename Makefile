# Deskmate. Thin aliases for ./deskmate, which does the work (run ./deskmate --help for everything).
.PHONY: help setup doctor up down restart status logs open update uninstall connect disconnect test

help:
	@./deskmate --help

setup:
	./deskmate setup

doctor:
	./deskmate doctor

up:
	./deskmate up

down:
	./deskmate down

restart:
	./deskmate restart

status:
	./deskmate status

logs:
	./deskmate logs

open:
	./deskmate open

update:
	./deskmate update

uninstall:
	./deskmate uninstall

connect:
	./deskmate connect

disconnect:
	./deskmate disconnect

# The host tool's tests (Python standard library only).
test:
	cd setup/tests && python3 -m unittest discover -p 'test_*.py'
