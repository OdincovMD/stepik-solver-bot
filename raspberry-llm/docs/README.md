# Локальная LLM на raspberry

Источник конфигурации отдельного LLM-сервиса. Перед изменениями прочитать этот
документ. Сервис не является частью pi-dashboard или VPN-шлюза.

[Архитектура, RAG, дообучение и оценка качества](architecture.md).
[Результаты замеров на этом сервере](measurements.md).
[Веб-чат, внешний API, доступ и диагностика](remote-access.md).

## Размещение

- Этот каталог: локальная копия исходников `/home/hardbox/raspberry-llm`.
- На сервере: `/home/admin/projects/raspberry-llm`.
- Бинарники и веса: `/home/admin/.local/share/raspberry-llm` вне исходников.
- Служба: `/etc/systemd/system/raspberry-llm.service`.
- Необязательная настройка модели: `/home/admin/.config/raspberry-llm/model.env`.
- API: `http://127.0.0.1:11434/v1`, модель API: `raspberry-llm`.

## Устройство

Проект → HTTP API → llama.cpp → GGUF Qwen3 → CPU Raspberry Pi.

Основная модель: Qwen3-4B-Instruct-2507, Q4_K_M. Веса Qwen3-1.7B и
Qwen3-0.6B сохранены для экспериментов и более быстрых задач. Одновременно
работает только одна модель; `raspberry-llm` — стабильное имя в API.

Запуск: 3 потока, CPUQuota 250%, Nice 10, контекст 2048 токенов, один слот
генерации. Контекст включает инструкции, историю, найденные документы и ответ.
Thinking отключён. Клиенту следует явно задавать max_tokens, обычно 64–256.
Сервер имеет значение по умолчанию 256; это не надёжный жёсткий лимит клиента.

Контроллер памяти cgroups на сервере отсутствует. LimitAS=6G ограничивает
виртуальное адресное пространство процесса, включая mmap, а не RSS.
Repack включён для ускорения CPU после остановки skin-cancer-ai. Экономный
режим включается строкой `LLM_REPACK=0` в model.env; он заметно медленнее.
Не включать новые большие модели без проверки доступной памяти.

Внутренний API модели привязан к loopback. Внешний доступ защищён отдельным
лёгким шлюзом: [чат](https://skin-cancer-ai.ru/llm/) и
`https://skin-cancer-ai.ru/v1`. У веб-чата свой пароль, у API — Bearer-ключи.
Порт 11434 напрямую в интернет не публикуется.

Для административного доступа к внутреннему API также остаётся SSH-туннель:

```bash
ssh -N -L 127.0.0.1:11434:127.0.0.1:11434 raspberry
```

После этого на клиенте:

```bash
curl http://127.0.0.1:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"raspberry-llm","messages":[{"role":"user","content":"Объясни, что такое API, одним предложением."}],"max_tokens":96,"stream":false}'
```

У Docker-контейнеров собственный loopback; localhost внутри контейнера не
указывает на хост. Для контейнерного клиента нужна отдельная сетевая интеграция
или туннель в его сетевом пространстве. Порт API не открывать всему интернету.

## Управление

```bash
ssh raspberry 'systemctl status raspberry-llm --no-pager'
ssh raspberry 'sudo systemctl restart raspberry-llm'
ssh raspberry 'sudo journalctl -u raspberry-llm -n 60 --no-pager'
ssh raspberry 'sudo systemctl disable --now raspberry-llm'
```

Последняя команда выключает службу и её автозапуск, сохраняя веса и исходники.
Обновлять только эту службу. Не менять сервисы дашборда, шлюза и других проектов.

Для переключения модели изменить единственную строку `LLM_MODEL_FILE` в
`/home/admin/.config/raspberry-llm/model.env`, затем перезапустить службу:

```ini
LLM_MODEL_FILE=Qwen3-4B-Instruct-2507-Q4_K_M.gguf
```

Альтернативные имена: `Qwen3-0.6B-Q4_K_M.gguf` или
`Qwen3-1.7B-Q4_K_M.gguf`. Имя модели в запросах API остаётся `raspberry-llm`.
Для экономного режима добавить `LLM_REPACK=0`. При возврате skin-cancer-ai
сначала остановить большую LLM и проверить совокупный расход RAM.

Проверки: `/health`, `/v1/models`, `python3 probe.py`, `/metrics`.
`probe.py` выполняет три реальных ограниченных запроса. Это проверка интеграции,
а не оценка качества модели на задачах всех проектов.
`capacity_probe.py` проверяет streaming и ответ по факту в более длинном входе.

## Освобождение ресурсов 22 ноября 2026

По указанию владельца остановлены все 9 контейнеров Compose-проекта `skin`.
`skin-cancer.service` отключён, restart policy контейнеров установлена в `no`.
Данные, тома, образы и checkout не удалялись. Общая Docker-сеть сохранена,
поскольку её использует работающий img2txt.

Compose-файл skin сохраняет старые restart policies: его явный запуск или
пересоздание контейнеров может снова включить сайт. Для восстановления сайта
использовать его собственную документацию и `docker compose up -d` в
`/home/admin/python/skin-cancer-ai`; это уменьшит память, доступную для LLM.

## Источники

- [Qwen3-4B-Instruct-2507, основная модель](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507).
- [Qwen3-1.7B, карточка исходной модели](https://huggingface.co/Qwen/Qwen3-1.7B).
- [Конфигурация архитектуры](https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/config.json).
- [GGUF Unsloth](https://huggingface.co/unsloth/Qwen3-1.7B-GGUF).
- [llama.cpp b12359](https://github.com/ggml-org/llama.cpp/releases/tag/b12359).
- [HTTP API](https://github.com/ggml-org/llama.cpp/blob/b12359/tools/server/README.md).

Точные версии, SHA256 и измерения находятся в `../deployment.json` и
`../results/`. Секреты и пользовательские запросы в репозиторий не сохранять.
