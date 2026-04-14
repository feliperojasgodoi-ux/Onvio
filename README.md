# Onvio Platform – Selenium Automation

Automated task and company management on the [Onvio](https://onvio.com.br) platform using Selenium WebDriver.

## Prerequisites

- Python 3.9+
- Google Chrome (or Chromium)
- ChromeDriver matching your Chrome version (placed in this directory or on `PATH`)

## Setup

```bash
pip install -r requirements.txt
```

## Configuration

All credentials are read from environment variables — **never hard-code them**:

| Variable    | Description                    |
|-------------|--------------------------------|
| `WORK_USER` | Onvio login email              |
| `WORK_PASS` | Onvio login password           |

```bash
export WORK_USER="you@example.com"
export WORK_PASS="your-password"
```

## Data Files

| File                    | Purpose                                                       |
|-------------------------|---------------------------------------------------------------|
| `onvio_export/tasks.json` | List of tasks to process (`[{"key": "...", "label": "..."}]`) |
| `task_to_companies.json`  | Map of task keys → company names to assign                   |
| `planilha.json`           | Auxiliary assignment data                                     |

## Running

```bash
python test.py
```

Screenshots are saved to the `screenshots/` directory.

## Project Structure

```
test.py                  – Main automation script
task_to_companies.json   – Task → company mapping
planilha.json            – Assignment data
requirements.txt         – Python dependencies
```
