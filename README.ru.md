# opencode-chat-export

[English](README.md) | **Русский**

MCP-сервер для экспорта истории чатов [OpenCode](https://opencode.ai) в Markdown.

Поддерживает **OpenCode 1.x и 2.x**, включая 2.4+, где сообщения разложены по таблицам
`session_v2` / `session_message` / `session_part`.

## Возможности

- **Экспорт текущего диалога** — сессия определяется автоматически (busy-сессия в текущем
  проекте, иначе самая свежая)
- **Все варианты хранения** — v1 (`session`/`message`/`part`), v2 со встроенными частями
  в `session_message.data.content` (2.0–2.3) и v2.4+ с отдельной таблицей `session_part`
- Хронологический порядок сообщений user/assistant
- Вызовы инструментов в сворачиваемых блоках `<details>` с input, результатом и ошибками
- Reasoning модели в виде цитат
- Вложения: картинки сохраняются рядом с Markdown и встраиваются, текстовые файлы
  показываются в сворачиваемом блоке
- Патчи с подсветкой синтаксиса
- Стоимость и токены по сессии и по каждому сообщению
- Дочерние (subagent) и архивные сессии скрыты по умолчанию, но их можно экспортировать

## Установка

```bash
pip install opencode-chat-export
```

Или через uv:

```bash
uv tool install opencode-chat-export
```

## Настройка

Добавьте в конфиг OpenCode (`~/.config/opencode/opencode.jsonc`):

```jsonc
{
  "mcp": {
    "chat-export": {
      "type": "local",
      "command": ["opencode-chat-export"],
      "enabled": false
    }
  }
}
```

Поставьте `"enabled": true`, чтобы включить сервер, и попросите агента экспортировать
текущий диалог.

## Как пользоваться

Просто просите в чате:

- «Экспортируй текущий чат в Markdown»
- «Сохрани этот диалог в `D:\backup\opencode`»
- «Покажи последние 10 сессий»
- «Экспортируй сессию `ses_e11609328f`»
- «Экспортируй 5 последних сессий»

Агент сам вызовет нужный инструмент:

| Инструмент | Что делает |
|---|---|
| `export_current_session` | Текущий диалог (можно передать `session_id`, если определился не тот) |
| `export_session` | Конкретная сессия по ID, префиксу или slug |
| `export_recent` | Последние N сессий |
| `list_sessions` | Список сессий с проектами и флагами `busy` / `child` / `archived` |

По умолчанию файлы сохраняются в `~/opencode-exports` (на Windows — `C:\Users\<user>\opencode-exports`):
один `.md` на сессию плюс папка `<имя>.attachments` с вложениями.

Если MCP-инструменты не видны в другом окне — перезапустите OpenCode (конфиг читается
при старте).

## MCP-инструменты

### `export_current_session`

Экспортирует текущий диалог — сессию, в которой вы сейчас работаете. ID знать не нужно.
Сессия определяется в таком порядке:

1. переменная окружения `OPENCODE_SESSION_ID` (если MCP-сервер её унаследовал);
2. busy-сессия, каталог которой совпадает с рабочим каталогом;
3. любая busy-сессия (`time_busy` установлен и после него не было `time_idle`);
4. самая свежая сессия в рабочем каталоге;
5. самая свежая сессия вообще.

**Параметры:**

- `output_dir` (необязательно, по умолчанию `~/opencode-exports/`)
- `session_id` (необязательно) — ID, префикс или slug, если автоопределение выбрало
  не ту сессию (например, когда занято несколько окон)

### `export_session`

Экспортирует конкретную сессию по ID (полному или префиксу) или slug.

**Параметры:**

- `session_id` (обязательно) — например, `ses_189058eb` или просто `ses_1890`
- `output_dir` (необязательно)

### `export_recent`

Экспортирует N последних обновлённых сессий.

**Параметры:**

- `count` (необязательно, по умолчанию `5`)
- `output_dir` (необязательно)

### `list_sessions`

Показывает сессии, отсортированные по времени обновления (сначала новые). Помечает
`busy`, `child` (subagent) и `archived`. Удобно, чтобы найти ID для `export_session`.

**Параметры:**

- `limit` (необязательно, по умолчанию `20`)
- `include_archived` (необязательно, по умолчанию `false`)
- `include_children` (необязательно, по умолчанию `false`)

## CLI

```bash
# Последние сессии
opencode-chat-export list

# Показать архивные и дочерние сессии
opencode-chat-export list --include-archived --include-children

# Экспорт текущей сессии
opencode-chat-export export-current

# Экспорт сессии по префиксу ID или slug
opencode-chat-export export ses_189058eb

# Экспорт всех сессий
opencode-chat-export export-all -o ~/my-exports

# Без файлов-вложений
opencode-chat-export export-current --no-attachments
```

## Как это работает

Утилита читает SQLite-базу OpenCode и ничего в неё не пишет. Путь определяется
автоматически: на Windows — `%APPDATA%/ai.opencode.desktop/opencode.db` и
`~/.local/share/opencode/opencode.db`, на Linux/macOS — `~/.local/share/opencode/opencode.db`.
Если баз несколько, выбирается та, что изменялась последней.

Формат хранения определяется автоматически:

| Версия OpenCode | Сессии | Сообщения | Части |
|---|---|---|---|
| 1.x | `session` | `message` | `part` |
| 2.0 – 2.3 | `session_v2` | `session_message` | внутри `session_message.data.content` |
| 2.4+ | `session_v2` | `session_message` | `session_part` |

Вариант 2.4 включается настройкой `session_part_write`. Если её выключить, OpenCode
снова пишет сообщение целиком, поэтому в одной базе могут встречаться оба варианта.
Экспортёр проверяет `session_part` для каждого сообщения и при отсутствии строк
использует встроенный `content`.

### Переменные окружения

- `OPENCODE_DB` — явный путь к `opencode.db` (проверяется первым)
- `OPENCODE_EXPORT_DIR` — каталог экспорта по умолчанию (`~/opencode-exports`)
- `OPENCODE_SESSION_ID` — используется `export_current_session`, если доступна
- `OPENCODE_EXPORT_MAX_TOOL_OUTPUT` — максимум символов результата инструмента
  (по умолчанию `5000`)

## Разработка

```bash
python tests/test_export.py
```

Тесты создают временные базы для всех трёх layouts (v1, v2 embedded, v2.4 parts) и
проверяют рендеринг, вложения, фильтры и поиск сессий.

## Лицензия

MIT
