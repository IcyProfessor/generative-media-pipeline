# Изменения

## 0.3.0rc2 — аудит публикации, 2026-10-08

Сохранены Recipe/ScenePlan, актуальная палитра/интро, lazy GIF cache, CPU renderer и existing checkpoints. Нет новых платных API-функций, agentic-слоя или variant_seed.

- Различены явный `SubmissionRejected` и неизвестный результат submit. 5xx, невалидные/противоречивые ответы и общие исключения не открывают автоматический повтор.
- Audio preflight выполняется до генерации сценария. Для V3_5 применены такие же документированные ограничения текста/style, как для V4.
- Единая безопасная диагностика, фильтрация credential env и отключение HTTP INFO-URL в CLI.
- Gemini: отключён ненужный AFC, JSON-schema converter сохраняет имена полей, совпадающие с ключевыми словами схемы.
- Renderer: file-backed live progress вместо PIPE/thread, ограниченное ожидание остановки; отказ консоли не блокирует дочерний процесс.
- Декодирование ограничено GIF/PNG/JPEG/WEBP; добавлено явное закрытие Canvas. Это не полное обновление уязвимой зависимости Pillow.
- Нормализатор таймкодов отвергает bool/non-string malformed данные, сохраняя совместимость camelCase/snake_case и прежнюю политику zero-duration.
- Output audit понимает `ScenePlan.scenes[].id`.
- Исправлены устаревшие rendering tests; добавлены regression cases и строгий staging export.
- MANIFEST исключает private configs/output из sdist; runtime wheel включает только `genmedia`.
- Документация заменяет прежние неподтверждённые «зелёные» заявления фактическими результатами и ограничениями.

