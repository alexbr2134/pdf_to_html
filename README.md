# pdf-to-html

PDF → HTML для **векторных** PDF.

Основной пайплайн — **smart** (`pdf_to_html_smart.ipynb`): детект таблиц + семантическая сборка grid (colspan/rowspan, подписи, prose) + полный документ (текст и таблицы в порядке чтения).

Бейзлайны для сравнения:

- `pdf_to_html_pdfplumber.ipynb` — pdfplumber tables + words
- `pdf_to_html_camelot.ipynb` — Camelot extract + текст вне таблиц

## Алгоритм (smart)

```
страница PDF
  │
  ├─ (опц.) векторизация линий скана → pdf_line_vectorize
  │
  ├─ find_tables_smart
  │     Camelot detect
  │       ├─ нет таблиц → pdfplumber lines → иначе text → иначе Camelot extract
  │       ├─ есть + lines → pdfplumber lines
  │       └─ есть + lines пусто → pdfplumber text / Camelot
  │
  ├─ для каждой таблицы: process_table
  │     build_cells (слова pdfplumber → ячейки)
  │     merge phantom / leading text columns
  │     classify HEADER/DATA
  │     merge wrapped / label rows (кроме стека кодов)
  │     restore colspan/rowspan по bbox и правилам подписей
  │     enrich side labels (ОКУД/ОКПО слева от «Коды»)
  │     code-stack colspan: одна ячейка кода накрывает value-колонки;
  │       три колонки только у строки даты (число | месяц | год)
  │
  ├─ сборка страницы
  │     таблицы + free text в reading order
  │     prose-fallback, если сетка выродилась в абзацы
  │
  └─ документ
        проверка битых шрифтов / OCR-garble
          → предупреждение в консоль и в HTML
        wrap HTML (thead/tbody, spans, шрифты из PDF)
```

Текст ячеек всегда из pdfplumber (`extract_words`); Camelot даёт только структуру/детект. Smart дополнительно восстанавливает семантику: объединённые ячейки, переносы подписей, вложенные таблицы кодов, разделение прозы и табличных блоков.

## Сравнение с бейзлайнами

### Методика оценки

- Шкала **1–10**: пригодность HTML для LLM (качество извлечения таблиц, качество заголовков, иерархия ячеек, смысл ячеек, полнота документа).
- **Оценщик:** Cursor Grok 4.5 (ручной отсмотр сгенерированного HTML, не автоматическая метрика).
- HTML: `парсинг html данные/` (smart), `парсинг html pdfplumber/`, `парсинг html camelot/`.
- Основная выборка **n=16**: без трёх битых OCR `1655388160-*` и пустой формы `chet_f`.
- **Время:** среднее секунд на страницу при полном прогоне тех же 16 PDF (по 1 стр.) на CPU Intel i3.

### Оценки по файлам

| Файл | smart | pdfplumber | camelot | Заметка |
|---|:-:|:-:|:-:|---|
| `1655388160-6` | 3 | 2 | 1 | битый OCR; smart предупреждает |
| `1655388160-14` | 3 | 3 | 3 | битый OCR |
| `1655388160-26` | 2 | 2 | 1 | битый OCR; camelot — ложные table |
| `2508064833-12` | **9** | 5 | 5 | codes + rowspan/colspan |
| `2508064833-15` | **9** | 6 | 6 | многоуровневые spans |
| `2508064833-17` | **9** | 8 | 8 | все читаемо; smart `<th>` |
| `2508064833-31` | 6 | **8** | 3 | smart: дубли слов в prose |
| `2703000015-6` | **8** | 3 | 6 | title отдельно; Коды/ОКУД |
| `2703000015-9` | **7** | 3 | **7** | continuation без шапки |
| `2703000015-20` | **8** | 4 | 5 | headers оплаты в table |
| `2703000015-24` | 5 | 3 | **7** | smart over-colspan |
| `2703000015-39` | 7 | **8** | 2 | текст; camelot false table |
| `2703000015-46` | **8** | 4 | 7 | smart `<th>` + строки |
| `2703000015-96` | 5 | 1 | 4 | smart: пустая колонка имён |
| `2703000015-111` | **6** | 2 | 5 | colspan есть, строки рвутся |
| `chet_f` | 3 | **6** | **6** | пустая форма; baselines держат grid |
| `chet_f_2` | **8** | 7 | 4 | headers + spans в table |
| `ks3` | **6** | 5 | 5 | codes неполные; spans в main |
| `obrazec-schyota` | **8** | 6 | 3 | smart spans bank block |
| `sfv_2012` | **8** | 7 | 7 | invoice headers со spans |

### Сводка (качество + время)

| Метод | Качество (n=16) | Лучший / ничья | Время, с/стр |
|---|:-:|:-:|:-:|
| **smart** | **7.3** | **13 / 16** | **1.76** |
| camelot | 5.3 | 2 / 16 | 1.13 |
| pdfplumber | 5.0 | 2 / 16 | 0.26 |


