# pdf-to-html

PDF и Office (DOC/DOCX/XLS/XLSX) → семантический HTML.

## Быстрый старт

```python
from to_html import to_html

result = to_html("in.pdf", "out.html")       # или .docx / .xlsx / .xlsm
# result.rejected_pages — отсев с reasons / messages
```

CLI: `python -m to_html in.docx out.html`

Под капотом — роутер на `pdf_to_html` / `office_to_html`.

## PDF

Основной пайплайн — **smart** (`pdf_to_html_smart.ipynb`): детект таблиц, семантическая сборка grid (colspan/rowspan, подписи, prose) и полный документ в порядке чтения.

Для кода команды — модуль **`pdf_to_html.py`** (обёртка над smart; реализация в `.py`, без загрузки ноутбука):

```python
from pdf_to_html import pdf_to_html

result = pdf_to_html("in.pdf", "out.html")
# result.rejected_pages — отсеянные стр. с reasons / messages
# result.to_dict()      — удобно для логов

# жёсткость роутинга unmarked_table_lines (0 = пропускать все, 1 = отсекать все raster)
result = pdf_to_html("in.pdf", "out.html", unmarked_routing_strictness=0.5)
```

Сборка страниц: `pdf_html_pipeline.py`. Пересборка из ноутбука при необходимости: `python scripts/extract_pipeline.py`.

Бейзлайны для сравнения:

- `pdf_to_html_pdfplumber.ipynb` — pdfplumber tables + words
- `pdf_to_html_camelot.ipynb` — Camelot extract + текст вне таблиц

## Office (DOC/DOCX/XLS/XLSX)

Модуль **`office_to_html.py`** — тот же каркас (тип документа → эвристики → HTML → suitability), но без геометрии PDF:

```python
from office_to_html import office_to_html

result = office_to_html("in.docx", "out.html")
# Excel: один HTML-раздел на лист; Word: один раздел на документ
```

| Формат | Библиотека | Примечание |
|---|---|---|
| `.docx` | `python-docx` | reading order, merges; layout-таблицы → prose |
| `.xlsx` / `.xlsm` | `openpyxl` | таблицы по borders/merges/index-строкам, не весь лист |
| `.doc` / `.xls` | — | сразу роут `legacy_format` (без конвертации в OOXML) |

Типы документов и эвристики — те же (`pdf_doc_types.py`: РСБУ / КС-2 / КС-3 / СФ / ТОРГ-12 / УПД).

Роутинг Office (`office_suitability.py`): `legacy_format`, `embedded_only`, `encrypted`, `empty_document`, `layout_table_abuse`, `sheet_too_sparse`.

Smoke-фикстуры: `fixtures/office_smoke/` → `python -m office_to_html fixtures/office_smoke/torg12_sample.docx /tmp/out.html`.

Пакетный прогон выборки: ноутбук **`office_to_html_v3.ipynb`** (`v3/` → `v3_html/`).

---

## Алгоритм (smart PDF)

```
страница PDF
  │
  ├─ detect_doc_type (`pdf_doc_types.py`)
  │     rsbu | ks2 | ks3 | invoice_sf | torg12 | upd | unknown
  │
  ├─ (опц.) векторизация линий скана → pdf_line_vectorize
  │
  ├─ find_tables_smart
  │     Camelot detect → pdfplumber lines / text / Camelot extract
  │
  ├─ process_table(+doc_type)
  │     build_cells → merge → classify HEADER/DATA
  │     restore colspan/rowspan, enrich side labels
  │     type heuristics (РСБУ / КС-2 / КС-3 / ТОРГ-12 / СФ / УПД)
  │
  ├─ сборка страницы (таблицы + free text, prose-fallback)
  │
  └─ page suitability / роутинг (`page_suitability.py`)
        pre:  broken_fonts · image_only_scan
        post: unmarked_table_lines (растровые линии + тяжёлая сетка;
              жёсткость unmarked_routing_strictness ∈ [0, 1], дефолт 0.5)
        → заглушка в HTML, smart-сборка не выполняется
```

Текст ячеек всегда из pdfplumber (`extract_words`); Camelot даёт структуру/детект. Smart восстанавливает семантику: объединённые ячейки, переносы подписей, вложенные таблицы кодов, разделение прозы и таблиц.

### Алгоритм (Office)

```
файл
  ├─ format gate (.doc/.xls → route legacy_format; иначе OOXML as-is)
  ├─ detect_doc_type (текст / путь)
  ├─ extract
  │     DOCX: paragraphs + tables (reading order, merges)
  │     XLSX: sheets → регионы по borders/merges
  ├─ classify (data-table vs prose) + type heuristics
  ├─ HTML
  └─ office suitability / route
```

---

## Оценка качества

Модель для оценки: **Cursor Grok 4.5**, ручной отсмотр HTML.

### Критерии оценки (LLM)

Шкала **1–10**.

| Вес | Критерий | Что смотреть |
|---|---|---|
| основной | Целостность табличных фактов | По информативным ячейкам однозначно восстанавливаются ключи (суммы, коды, qty/price, стороны); span/косметика заголовков вторичны |
| основной | Цельный смысл | Понятны тип документа, стороны, суммы, строки, итоги |
| высокий | Пригодность для LLM | Можно отвечать на вопросы по странице без критичной путаницы |
| высокий | Таблицы | Выравнивание строк/колонок; числа в нужных ячейках |
| средний | Полнота | Не потеряны ключевые блоки, если они есть в PDF |
| средний | Иерархия | Разделы, шапка vs тело, prose vs table |
| низкий | Косметика | OCR-шум, пустые бланки, кривые заголовки — не штраф, если факты однозначны |

**Как ставить балл**

- **9–10** — факты однозначны; почти без ложных связей.
- **8** — пригоден для LLM; мелкие дефекты (шум, съехавшая шапка, пустые расшифровки), но ключевые ячейки читаются однозначно (допустимо лёгкое «угадывание» по контексту строки/итога).
- **6–7** — ключевые факты неоднозначны или таблица активно врёт (неоднозначный сдвиг поле→значение без обхода).
- **4–5** — структура сильно ломает смысл.
- **1–3** — мусор / нечитаемый текст.

**Что не штрафуется**

- пустые блоки в таблице, не влияющие на смысл;
- span и кривые заголовки, если информативные поля всё равно однозначны;
- роутированные страницы — **не оцениваются** и не входят в средние (`—` / заглушка «отсеяна»).

### 1. v2 — средние по типам документов

Источник: `v2/` → `v2_html/` (smart). Ручная постраничная оценка; ниже — агрегаты по типу.

| Тип | Страниц | Принято | Роут | Средняя оценка |
|---|:-:|:-:|:-:|:-:|
| РСБУ | 69 | 52 | 17 | **8.60** |
| кс-2 | 27 | 20 | 7 | **8.80** |
| кс-3 | 5 | 5 | 0 | **8.60** |
| счет-фактура | 6 | 6 | 0 | **8.84** |
| торг 12 | 11 | 11 | 0 | **8.91** |
| упд | 7 | 7 | 0 | **8.72** |
| **Итого** | **125** | **101** | **24** | **8.69** |

После переоценки по целостности табличных фактов среди принятых **нет оценок ниже 8**. Дефолт `unmarked_routing_strictness=0.5`.

### 2. Бейзлайны vs smart — `парсинг pdf данные`

Одна страница на файл (n = 20). HTML: `парсинг html данные/` (smart), `парсинг html pdfplumber/`, `парсинг html camelot/`.

Средняя оценка smart — только по **принятым** страницам (роутинг: 8 из 20). Бейзлайны роутинг не используют. Время — полный пайплайн, с/стр.

| Метод | Средняя оценка | n | Время, с/стр |
|---|:-:|:-:|:-:|
| **smart** | **8.58** | 12 принято | **3.15** |
| camelot | 4.75 | 20 | 2.41 |
| pdfplumber | 4.70 | 20 | 0.72 |

### 3. Итог — все принятые страницы (v2 + парсинг)

Объединённая выборка smart: **113** принятых страниц (101 из v2 + 12 из парсинга). Роут не учитывается. Время — среднее по всем страницам обоих наборов (включая отсеянные при прогоне, n = 145).

| Метрика | Значение |
|---|:-:|
| Принятых страниц | **113** |
| Средняя оценка | **8.68** |
| Медиана | **9** |
| 5-й перцентиль | **8** |
| 95-й перцентиль | **10** |
| Мин / макс | **8** / **10** |
| Среднее время, с/стр | **6.70** |

### 4. v3 — Office (`docx` / `xlsx`)

Источник: `v3/` → `v3_html/` через `office_to_html` / `to_html`. Та же шкала 1–10 (ручной отсмотр HTML).

Единица оценки — **файл** (для Excel с несколькими листами тип/оценка на документ; роут — по единицам suitability: лист Word = 1, лист Excel = 1).

| Тип | Файлов | Принято | Роут | Средняя оценка |
|---|:-:|:-:|:-:|:-:|
| торг 12 | 4 | 3 | 1 | **9.00** |
| кс-2 | 5 | 3 | 2 | **9.00** |
| кс-3 | 7 | 3 | 4 | **8.00** |
| счет-фактура | 5 | 3 | 2 | **9.00** |
| упд | 4 | 1 | 3 | **9.00** |
| РСБУ | 1 | 1 | 0 | **8.00** |
| другое | 3 | 1 | 2 | **9.00** |
| **Итого** | **29** | **15** | **14** | **8.73** |

Среди принятых **нет оценок ниже 8**. Весь роут — `legacy_format` (`.doc` / `.xls`, без конвертации в OOXML).

| Метрика | Значение |
|---|:-:|
| Файлов в выборке | **29** |
| OOXML (`.docx`/`.xlsx`) | **15** |
| Legacy (`.doc`/`.xls`) → route | **14** |
| Принятых файлов | **15** |
| Единиц suitability (принято / всего) | **20** / **34** |
| Средняя оценка (принятые) | **8.73** |
| Медиана | **9** |
| Мин / макс | **8** / **9** |
| Время прогона (все 29) | **171 с** |
| Среднее время, с/файл (все) | **5.9** |
| Среднее время, с/файл (принятые OOXML) | **11.4** |

По формату среди принятых: **7× `.docx`**, **8× `.xlsx`**. Типичная структура HTML: шапка/футер формы в `<p>`, одна товарная/сметная `<table>` (у РСБУ — несколько листов-разделов).

---

## Структура репозитория

| Путь | Назначение |
|---|---|
| `to_html.py` | единый вход: `to_html(path, html) → ConversionResult` |
| `pdf_to_html.py` | библиотека: `pdf_to_html(pdf, html) → ConversionResult` |
| `office_to_html.py` | библиотека: `office_to_html(path, html) → ConversionResult` |
| `office_normalize.py` | проверка формата; .doc/.xls → legacy_format |
| `office_docx.py` · `office_xlsx.py` | извлечение структуры |
| `office_html.py` · `office_suitability.py` | HTML + роутинг Office |
| `pdf_html_pipeline.py` | реализация smart-сборки PDF |
| `pdf_to_html_smart.ipynb` | исследовательский стенд (опционально → extract_pipeline) |
| `page_suitability.py` | shim → пакет `pdf_suitability/` (роутинг / отсев PDF-страниц) |
| `pdf_doc_types.py` | shim → пакет `doc_type/` (типы документов + эвристики PDF и Office) |
| `pdf_table_engine.py` | детект и извлечение таблиц PDF |
| `pdf_line_vectorize.py` | векторизация растровых линий таблиц для сканов |
| `fixtures/office_smoke/` | smoke DOCX/XLSX |
| `office_to_html_v3.ipynb` | прогон `v3/` → `v3_html/` с консольным отчётом |
| `v2/` · `v2_html/` | выборка PDF по типам документов |
| `v3/` · `v3_html/` | выборка Office по типам → HTML |
| `парсинг pdf данные/` | 20 PDF для сравнения с бейзлайнами |
| `парсинг html данные/` | smart HTML |
| `парсинг html pdfplumber/` · `парсинг html camelot/` | бейзлайн HTML |
