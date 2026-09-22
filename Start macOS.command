#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 -m drive_migrator gui
printf '\nPress Enter to close this window. '
read answer
