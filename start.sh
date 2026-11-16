#!/bin/bash
# Manueller Start (Alternative zum systemd-Service)
cd "$(dirname "$0")"
source venv/bin/activate
exec python -m app.main >> football_bot.log 2>&1
