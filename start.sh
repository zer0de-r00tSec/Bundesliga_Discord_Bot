#!/bin/bash
# Manueller Start (Alternative zum systemd-Service)
# Logs schreibt der Bot selbst nach ./log/ (bot.log, error.log)
cd "$(dirname "$0")"
mkdir -p log
source venv/bin/activate
exec python -m app.main >> log/stdout.log 2>&1
