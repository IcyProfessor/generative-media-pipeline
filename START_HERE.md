# Начать здесь: Windows CMD

Команды ниже рассчитаны на Windows CMD

## Новая чистая папка

Откройте папку репозитория

```bat
cd /d C:\projects\generative-media-pipeline
python --version
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -e ".[dev]"
python -m pip check
python -m genmedia doctor
python -m pytest -q -ra
```

Python 3.12 подходит; 3.13/3.14 входят в объявленный диапазон, но установку каждой версии проверяет CI. Если установлена другая версия, выберите нужную через `py -3.12 -m venv .venv`. Повторно устанавливать всё после перезагрузки не нужно — достаточно `cd` и `activate.bat`.

`doctor` печатает установленный runtime и найденный FFmpeg. Это диагностика окружения, **не тест доступности платных API**.

## Два бесплатных demo

```bat
python -m genmedia demo --recipe alphabet
python -m genmedia demo --recipe counting
```

Откройте `output\<напечатанный-ID>\v0\final.mp4` и `v1\final.mp4`. Звук намеренно синтетический. Повтор `demo` создаёт новый job. Для существующего job:

```bat
python -m genmedia status --job output\JOB_ID
python -m genmedia replay --job output\JOB_ID --force-render
```

Замените `JOB_ID` настоящим идентификатором. `replay` не вызывает провайдеров.

## Ключи и live

```bat
python -m pip install -e ".[dev,live]"
copy .env.example .env
notepad .env
```

В env заполните `GEMINI_API_KEY`, `SUNO_API_KEY`, проверенные модель и base URL, свой HTTPS callback. Локальный `.env` лежит в корне проекта и исключён через `.gitignore`: **его нельзя коммитить, прикладывать к ZIP или выкладывать на GitHub**. Если ключи раньше попадали в сторонние архивы, их нужно перевыпустить. Не печатайте полный config/job при запросе помощи: внутри могут быть приватные URL.

```bat
python -m genmedia list-models --env-file .env
python -m genmedia run --recipe alphabet --live --env-file .env --allow-paid
```

Это новый платный job на A–F с demo-изображениями. Для A–Z подключите свой manifest через `configs/alphabet-full.example.yaml`, скопировав его в `configs/local-alphabet.yaml`. Media не копируются автоматически.

```bat
python -m genmedia run --config configs\local-alphabet.yaml --live --env-file .env --allow-paid
```

`--allow-paid` разрешает вызовы, **но не задаёт бюджет**. При ошибке после submit не запускайте новый `run` наугад. 

## Docker/Redis — только для очереди

Для обычных `run`, `demo`, `replay` Docker не нужен. Для очереди установите extra, включите Docker Desktop, дождитесь готовности Engine:

```bat
python -m pip install -e ".[dev,queue]"
docker compose up -d redis
docker compose exec redis redis-cli ping
python scripts\redis_smoke.py
```


Для реальной очереди нужны `[live,queue]` и два терминала с активированным venv:

```bat
python -m genmedia worker default --env-file .env
```

```bat
python -m genmedia worker rendering --env-file .env
```

Отправка задания:

```bat
python -m genmedia submit --config configs\local-alphabet.yaml --live --env-file .env --allow-paid
```

Все процессы должны видеть один и тот же локальный output-root, каталог и namespace. Redis контейнеризирован; само приложение запускается локально. Это не кластерная установка.

