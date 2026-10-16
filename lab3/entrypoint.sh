#!/bin/sh
# Создаёт папку для входа TickTick и запускает переданную команду (бот или ticktick auth login).
mkdir -p "$HOME"
exec "$@"
