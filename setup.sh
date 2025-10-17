#!/bin/bash

echo "🚀 Установка Telegram-бота с виртуальным окружением..."

# 1. Создаём venv, если не существует
if [ ! -d "venv" ]; then
    echo "📦 Создаю виртуальное окружение..."
    python3 -m venv venv
fi

# 2. Активируем venv и устанавливаем зависимости
echo "📥 Устанавливаю зависимости..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 3. Проверяем .env
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚠️  Файл .env создан. Отредактируйте его!"
fi

echo "✅ Готово!"
echo "▶️  Запуск бота:"
echo "   source venv/bin/activate && python bot.py"