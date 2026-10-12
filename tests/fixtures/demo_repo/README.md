# Demo Project

A fixture repository used by the CodeKB test suite.

## Retry behaviour

Requests retry with exponential backoff up to `max_retries`.

```python
# This '#' must not be treated as a heading.
def demo():
    return "# not a heading"
```

### Backoff formula

Delay doubles per attempt: `base_delay * 2 ** attempt`.

## Configuration

Settings live in `config/settings.toml`.