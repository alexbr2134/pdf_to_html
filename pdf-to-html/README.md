# pdf-to-html

PDF → HTML для **векторных** PDF: Camelot detect + pdfplumber lines/text + Camelot extract.

## Pipeline

```
Camelot detect
  ├─ нет таблиц → pdfplumber lines → (пусто) → только текст страницы
  ├─ есть + lines есть → pdfplumber lines
  └─ есть + lines пусто → pdfplumber text → Camelot auto
```

Текст ячеек всегда из pdfplumber (`extract_words`); Camelot extract даёт только структуру и bbox.

## Setup

```bash
bash scripts/setup.sh
source .venv/bin/activate
```

VS Code подхватит `.venv` через `.vscode/settings.json`.

## Checks

```bash
python scripts/verify_env.py
python validate_pipeline.py
```

Ghostscript для Camelot: `brew install ghostscript`
