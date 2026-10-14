# Contributing

Bug reports and pull requests are welcome.

## Reporting a problem

Open an issue using the bug report form. A debug log helps most:

```yaml
logger:
  logs:
    custom_components.package_tracker: debug
```

Please don't post real emails. If an email isn't recognized, describe the sender and subject line
and paste the relevant part with names, addresses and numbers replaced.

## Development

Home Assistant's test tools need Linux or macOS. On Windows, use WSL.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt
ruff check .
ruff format --check .
pytest
```

To preview the dashboard card without Home Assistant:

```bash
python -m http.server 8799 --bind 127.0.0.1
# open http://127.0.0.1:8799/dev/card-harness.html
```

## Pull requests

- Keep changes focused, and add or update tests for any parser or registry change.
- Test emails must be synthetic. Use made-up names, addresses, order numbers and tracking numbers.
  Tracking numbers still need valid check digits.
- Run `ruff check` and `ruff format` before you push.
