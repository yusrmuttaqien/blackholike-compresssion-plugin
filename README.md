# 📌 Overview

This filter reduces token consumption in long conversations through intelligent summarization and message compression while maintaining conversational coherence.

## Core Features

- ✅ Automatic compression triggered by token count threshold
- ✅ Asynchronous summary generation (does not block user response)
- ✅ Persistent storage with database support (PostgreSQL and SQLite)
- ✅ Flexible retention policy (keep first N non-system messages + last N messages)
- ✅ Absolute system message protection (never compressed or discarded)
- ✅ Structure-aware trimming to preserve document skeleton
- ✅ Native tool output trimming for function calling support

## 🔄 Workflow

### Phase 1: Inlet (Pre-request processing)

1. Receives all messages in the current conversation.
2. Checks for a previously saved summary.
3. If a summary exists and the message count exceeds the retention threshold:
   - Extracts the first N non-system messages to be kept (plus all interleaved system messages).
   - Injects the summary into the first message.
   - Extracts the last N messages to be kept.
   - Combines them into: `[Kept First + Summary + Gap System Messages + Kept Last]`
4. Sends the compressed message list to the LLM.

### Phase 2: Outlet (Post-response processing)

1. Triggered after the LLM response is complete.
2. Checks if the token count has reached the compression threshold.
3. If the threshold is met, an asynchronous background task is started:
   - Extracts messages to be summarized (excluding the kept first and last).
   - Calls the LLM to generate a concise summary.
   - Saves the summary to the database.

## 🛡️ System Message Protection

System messages are strictly excluded from compression and always preserved in the final context. This ensures that dynamic instructions injected by other plugins (e.g., live time/location context) remain accurate throughout the conversation.

**Protection Rules:**

1. `keep_first` counts only non-system messages. System messages within the first N non-system messages are automatically preserved.
2. System messages in the compression gap (between kept-first and kept-last) are extracted and preserved as original messages, not summarized.
3. During forced trimming (when exceeding `max_context_tokens`), system messages from dropped atomic groups are re-inserted into the final output.

**Example:**

```
Messages: [sys, user1, sys(injected), user2, ..., user10, user11]
keep_first=0, keep_last=2

Effective keep_first=0 (no non-system messages protected)
Gap: [sys, user1, sys(injected), user2, ..., user9]
Preserved from gap: [sys, sys(injected)]

Final output: [sys, summary, sys(injected), user10, user11]
```

## 💾 Storage

This filter uses Open WebUI's shared database connection for persistent storage. It automatically reuses Open WebUI's internal SQLAlchemy engine and `SessionLocal`, making the plugin database-agnostic and ensuring compatibility with any database backend that Open WebUI supports (PostgreSQL, SQLite, etc.).

No additional database configuration is required — the plugin inherits Open WebUI's database settings automatically.

**Table Structure (`chat_summary`):**

| Column | Description |
| --- | --- |
| `id` | Primary Key (auto-increment) |
| `chat_id` | Unique chat identifier (indexed) |
| `summary` | The summary content (TEXT) |
| `compressed_message_count` | The original number of messages |
| `created_at` | Timestamp of creation |
| `updated_at` | Timestamp of last update |

## 📊 Compression Example

Scenario: a 20-message conversation (default settings: keep first 0, keep last 6).

**Before compression:**

```
Message 1:     [Initial prompt + First question]
Messages 2-14: [Historical conversation]
Messages 15-20: [Recent conversation]
Total: 20 full messages
```

**After compression:**

```
Message 1:     [Initial prompt + Historical summary + First question]
Messages 15-20: [Last 6 full messages]
Total: 7 messages
```

**Effect:**

- ✓ Saves 13 messages (approx. 65%)
- ✓ Retains full context
- ✓ Protects important initial prompts

## ⚙️ Configuration

| Parameter | Default | Description |
| --- | --- | --- |
| `priority` | `10` | Priority level for the filter operations. Lower numbers run first. |
| `compression_threshold_tokens` | `64000` | When total context token count exceeds this value, trigger compression (global default). |
| `max_context_tokens` | `128000` | Hard limit for context. Exceeding this value will force removal of earliest messages (global default). |
| `model_thresholds` | `""` (empty) | Per-model threshold overrides. Format: `model_id:compression_threshold:max_context` (comma-separated). Example: `gpt-4:8000:32000,claude-3:100000:200000` |
| `keep_first` | `0` | Keep the first N non-system messages plus all interleaved system messages. Set to 0 to disable. |
| `keep_last` | `6` | Always keep the last N full messages. |
| `summary_model` | `None` | The model ID used to generate the summary. If empty, uses the current conversation's model. Recommend a fast, economical, compatible model such as `deepseek-v3`, `gemini-2.5-flash`, or `gpt-4.1`. Must be specified if the conversation uses a pipeline model or a model that does not support standard generation APIs. |
| `summary_model_max_context` | `0` | Max context tokens for the summary model. If 0, falls back to `model_thresholds` or global `max_context_tokens`. Example: `gemini-flash=1000000, gpt-4o-mini=128000` |
| `max_summary_tokens` | `16384` | The maximum number of tokens for the summary. |
| `summary_temperature` | `0.1` | The temperature for summary generation. Lower values produce more deterministic output. |
| `enable_tool_output_trimming` | `true` | Enable trimming of large tool outputs (only works with native function calling). |
| `tool_trim_threshold_chars` | `600` | Trim native tool outputs when their total content length reaches this many characters. |
| `show_token_usage_status` | `true` | Show token usage status notification. |
| `token_usage_status_threshold` | `80` | Only show token usage status when usage exceeds this percentage (0-100). Set to 0 to always show. |
| `debug_mode` | `false` | Enable detailed logging for debugging. Recommended to set to `false` in production. |
| `show_debug_log` | `false` | Show debug logs in the frontend console (F12). Useful for frontend debugging. |

## 🔧 Deployment

The plugin automatically uses Open WebUI's shared database connection. No additional database configuration is required.

**Suggested Filter Installation Order**

It is recommended to set the priority of this filter relatively high (a smaller number) to ensure it runs before other filters that might modify message content. A typical order might be:

1. Filters that need access to the full, uncompressed history (priority < 10) — e.g., a filter that injects a system-level prompt like live context.
2. This compression filter (`priority = 10`).
3. Filters that run after compression (priority > 10) — e.g., a final output formatting filter.

## 📝 Database Query Examples

View all summaries:

```sql
SELECT
  chat_id,
  LEFT(summary, 100) AS summary_preview,
  compressed_message_count,
  updated_at
FROM chat_summary
ORDER BY updated_at DESC;
```

Query a specific conversation:

```sql
SELECT *
FROM chat_summary
WHERE chat_id = 'your_chat_id';
```

Delete old summaries:

```sql
DELETE FROM chat_summary
WHERE updated_at < NOW() - INTERVAL '30 days';
```

Statistics:

```sql
SELECT
  COUNT(*) AS total_summaries,
  AVG(LENGTH(summary)) AS avg_summary_length,
  AVG(compressed_message_count) AS avg_msg_count
FROM chat_summary;
```

## ⚠️ Important Notes

1. **Database Connection**
   - ✓ The plugin uses Open WebUI's shared database connection automatically.
   - ✓ No additional configuration is required.
   - ✓ The `chat_summary` table will be created automatically on first run.
2. **Retention Policy**
   - ⚠ `keep_first` counts only non-system messages. System messages are always preserved regardless of this setting.
3. **Performance**
   - ⚠ Summary generation is asynchronous and will not block the user response.
   - ⚠ There will be a brief background processing time when the threshold is first met.
4. **Cost Optimization**
   - ⚠ The summary model is called once each time the threshold is met.
   - ⚠ Set `compression_threshold_tokens` reasonably to avoid frequent calls.
   - ⚠ It's recommended to use a fast and economical model (like `gemini-flash`) to generate summaries.
5. **Multimodal Support**
   - ✓ This filter supports multimodal messages containing images.
   - ✓ The summary is generated only from the text content.
   - ✓ Non-text parts (like images) are preserved in their original messages during compression.
   - ⚠ Image tokens are **not** calculated. Different models have vastly different image token costs (GPT-4o: 85–1105, Claude: ~1300, Gemini: ~258 per image). Plan your thresholds accordingly.

## 🐛 Troubleshooting

**Problem: Database table not created**

1. Ensure Open WebUI is properly configured with a database.
2. Check the Open WebUI container logs for detailed error messages.
3. Verify that Open WebUI's database connection is working correctly.

**Problem: Summary not generated**

1. Check if the `compression_threshold_tokens` has been met.
2. Verify that the `summary_model` is configured correctly.
3. Check the debug logs for any error messages.

**Problem: Initial system prompt is lost**

- System messages are always preserved. If a system prompt is missing, check whether another filter is modifying or removing it.

**Problem: Compression effect is not significant**

1. Increase the `compression_threshold_tokens` appropriately.
2. Decrease the number of `keep_last` or `keep_first`.
3. Check if the conversation is actually long enough.

## 🙏 Credits

This project builds on and is inspired by:

- **Async Context Compression** by [Fu-Jie](https://github.com/Fu-Jie) — the original base function this plugin is derived from ([openwebui-extensions](https://github.com/Fu-Jie/openwebui-extensions)).
- **[pi-blackhole](https://github.com/k0valik/pi-blackhole)** by [k0valik](https://github.com/k0valik) — design reference for the compaction and retention strategy.

## 📄 License

MIT
