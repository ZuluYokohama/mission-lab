# Remote MCP adapter

`tools/mission_lab_mcp.py` serves the existing controller over [Streamable HTTP](https://cursor.com/docs/mcp). Cursor Cloud Agents call this kind of endpoint by URL. The IDE and CLI use the same URL. SSE is not the transport: `GET /mcp` returns 405, and each JSON-RPC request is a `POST`.

The adapter is not part of `research/mission_lab_v0_1`. It does not enter the package source identity, it does not register an operator, and it does not import a third-party package. A tool result reports the same acceptance bit the local CLI reports. That bit is a check of the registered contract. It is not a quality score, and `human_review` stays false. Replay reports `independent_execution: false`.

## What stays on the server

Uploaded input bytes are stored under the package `evidence/` directory, which Git ignores. A plan is addressed by its `plan_id`. A run is addressed by a `run_id`. The client does not receive the seal key, an API key, or a host path. Sending any of those as a tool argument is refused. `llm:grok` transport `xai` still uses `XAI_API_KEY` from the server environment, and only when the resource profile grants that name to the task.

Each uploaded file is at most 1 MiB. A mission still has 1 to 8 inputs. The request body is at most 14 MiB. An inline input path must be a single file name. A `mission_path` or `resources_path` must already be a file inside the checkout.

A bind other than loopback requires `MISSION_LAB_MCP_TOKEN`. The client sends `Authorization: Bearer <token>`. Loopback may omit the token. A new run still requires the controller seal key (`MISSION_LAB_SEAL_KEY` or `MISSION_LAB_SEAL_KEY_FILE`) on the server. The token is not that key.

This process is not a sandbox. The controller's [known issues](../KNOWN_ISSUES.md) apply to every run it seals.

## Run

From the repository root, with a seal key already configured the way [development](DEVELOPMENT.md) describes:

```text
export MISSION_LAB_MCP_TOKEN="$(python -B -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -B tools/mission_lab_mcp.py serve --host 0.0.0.0 --port 8080
```

The process prints `{"endpoint": "http://0.0.0.0:8080/mcp", "token_required": true}`. Put the server on a host Cursor can reach. This repository does not publish that host. An ephemeral agent VM is enough to run the process and is not a stable public URL.

Cursor project configuration, with the token in the environment rather than in the file:

```json
{
  "mcpServers": {
    "mission-lab": {
      "url": "https://your-host.example/mcp",
      "headers": {
        "Authorization": "Bearer ${env:MISSION_LAB_MCP_TOKEN}"
      }
    }
  }
}
```

Cloud Agents take an HTTP MCP server in the agents dashboard, or an `mcpServers` entry of type `http` on the Cloud Agents API. Use the HTTPS URL of this process. Stdio remains available by running the local CLI inside a VM; this adapter is the remote path.

## Tools

| Tool | Arguments | Result that matters |
|---|---|---|
| `validate` | `mission_path`, or `mission` plus `inputs` (`id`, `content_base64`) | `valid`, `human_review: false`, unresolved questions |
| `plan` | mission as above, plus `resources` or `resources_path` | `plan_id`. A second identical plan returns `already_recorded: true` |
| `run` | `plan_id` | `run_id`, `accepted`, task receipts, seal key id |
| `replay` | `run_id` | `integrity_verified`, `signature_status`, `accepted` |
| `inspect` | optional `max_entries`, `max_depth` | Fixture metadata. Contents are not read |
| `resume` | `run_id`, `approval` | A new run. An approval does not set `human_review` |
| `replan` | `plan_id`, resources, `trigger` | A new `plan_id` and the revision record |
| `demo` | none | Checked-in demonstration. Returns `baseline_run_id` and `revised_run_id` |

`demo` run ids have the form `<demo_id>/baseline_run` and `<demo_id>/revised_run`. Replay accepts those ids. `--allow-unsigned-legacy` is not exposed here.

## License

The adapter is project code under [PolyForm Noncommercial 1.0.0](../LICENSE.md). Hosting it for a commercial purpose can fall outside that grant and needs a separate permission.
