# localmem Logging (v0.5.0)

Two log surfaces:

1. **Console** — every line goes to stdout, formatted per `logging.format`.
2. **File (optional)** — rotating file at `logging.file`, same formatter.

Both are configured via the same `LoggingConfig` block in `localmem.yaml`.

## Configuration

```yaml
logging:
  level: INFO            # DEBUG | INFO | WARNING | ERROR
  format: text           # text (human) | json (machine)
  file: ./logs/localmem.log   # null = console only
  max_bytes: 10000000    # 10 MB before rotation
  backup_count: 3        # keep 3 rotated files
```

`format: text` produces:

```
2026-05-09 14:32:11 localmem.consolidator [INFO] consolidated 12 entries into 3 summaries
```

`format: json` produces one JSON object per line:

```json
{"timestamp":"2026-05-09T14:32:11.482Z","level":"INFO","logger":"localmem.consolidator","message":"consolidated 12 entries into 3 summaries"}
```

Exceptions are emitted as a single JSON line with the full `exception` field
(stringified traceback) so machine parsers can keep them on one record.

## Shipping logs

You don't need any code-side changes — just point your collector at the file
or stdout. Two common stacks:

### Loki + Promtail

For a Grafana / Loki setup, drop this into your Promtail config:

```yaml
scrape_configs:
  - job_name: localmem
    static_configs:
      - targets: [localhost]
        labels:
          job: localmem
          host: ${HOSTNAME}
          __path__: /var/log/localmem/*.log
    pipeline_stages:
      - json:
          expressions:
            timestamp: timestamp
            level: level
            logger: logger
            message: message
            exception: exception
      - labels:
          level:
          logger:
      - timestamp:
          source: timestamp
          format: RFC3339Nano
      - output:
          source: message
```

Set `logging.format: json` so the `json` pipeline stage works.
`logging.file: /var/log/localmem/localmem.log` matches the glob.

### ELK + Filebeat

For an Elasticsearch / Kibana setup:

```yaml
filebeat.inputs:
  - type: filestream
    id: localmem
    paths:
      - /var/log/localmem/*.log
    parsers:
      - ndjson:
          target: ""
          add_error_key: true
    fields:
      service: localmem
    fields_under_root: true

processors:
  - timestamp:
      field: timestamp
      layouts:
        - "2006-01-02T15:04:05.999999999Z07:00"
      test:
        - "2026-05-09T14:32:11.482Z"
  - drop_fields:
      fields: ["timestamp"]

output.elasticsearch:
  hosts: ["http://elasticsearch:9200"]
  index: "localmem-%{+yyyy.MM.dd}"
```

Same precondition: `logging.format: json`.

## Metrics

Logs are *not* a metrics surface. For Prometheus scraping see
`/metrics` on the dashboard sidecar — see `docs/DASHBOARD.md`.

## Production tips

- **Don't log to disk and console on a constrained host** — pick one. The
  `logging.file` setting only adds a handler; it does not silence stdout.
  Most operators redirect stdout to `/dev/null` (via launchd plist or
  systemd `StandardOutput=null`) when shipping a file directly.
- **Rotate aggressively.** `max_bytes: 10000000` (10 MB) with
  `backup_count: 3` caps disk use at ~40 MB. For chatty installs, lower
  the level to WARNING in production.
- **JSON format costs ~2x bytes** vs text. Worth it for indexability,
  not worth it if you grep with tail.
- **Service unit logs** (launchd plist, systemd `journalctl -u`) are
  separate from `logging.file` — they capture stdout/stderr from the
  process, not the structured logger. The two are independent.
