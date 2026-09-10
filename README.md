title: Async Context Compression
id: async_context_compression
author: Fu-Jie
author_url: https://github.com/Fu-Jie/openwebui-extensions
funding_url: https://github.com/open-webui
description: Reduces token consumption in long conversations while maintaining coherence through intelligent summarization and message compression.
version: 1.6.1
openwebui_id: b1655bc8-6de9-4cad-8cb5-a6f7829a02ce
license: MIT

═══════════════════════════════════════════════════════════════════════════════
📌 Overview
═══════════════════════════════════════════════════════════════════════════════

This filter reduces token consumption in long conversations through intelligent
summarization and message compression while maintaining conversational coherence.

Core Features:
  ✅ Automatic compression triggered by token count threshold
  ✅ Asynchronous summary generation (does not block user response)
  ✅ Persistent storage with database support (PostgreSQL and SQLite)
  ✅ Flexible retention policy (keep first N non-system messages + last N messages)
  ✅ Absolute system message protection (never compressed or discarded)
  ✅ Structure-aware trimming to preserve document skeleton
  ✅ Native tool output trimming for function calling support

═══════════════════════════════════════════════════════════════════════════════
🔄 Workflow
═══════════════════════════════════════════════════════════════════════════════

Phase 1: Inlet (Pre-request processing)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. Receives all messages in the current conversation.
  2. Checks for a previously saved summary.
  3. If a summary exists and the message count exceeds the retention threshold:
     ├─ Extracts the first N non-system messages to be kept (plus all
     │  interleaved system messages).
     ├─ Injects the summary into the first message.
     ├─ Extracts the last N messages to be kept.
     └─ Combines them into: [Kept First + Summary + Gap System Messages + Kept Last]
  4. Sends the compressed message list to the LLM.

Phase 2: Outlet (Post-response processing)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. Triggered after the LLM response is complete.
  2. Checks if the token count has reached the compression threshold.
  3. If the threshold is met, an asynchronous background task is started:
     ├─ Extracts messages to be summarized (excluding the kept first and last).
     ├─ Calls the LLM to generate a concise summary.
     └─ Saves the summary to the database.

═══════════════════════════════════════════════════════════════════════════════
🛡️ System Message Protection
═══════════════════════════════════════════════════════════════════════════════

System messages are strictly excluded from compression and always preserved in
the final context. This ensures that dynamic instructions injected by other
plugins (e.g., live time/location context) remain accurate throughout the
conversation.

  Protection Rules:
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. `keep_first` counts only non-system messages. System messages within the
     first N non-system messages are automatically preserved.
  2. System messages in the compression gap (between kept-first and kept-last)
     are extracted and preserved as original messages, not summarized.
  3. During forced trimming (when exceeding `max_context_tokens`), system
     messages from dropped atomic groups are re-inserted into the final output.

  Example:
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Messages: [sys, user1, sys(injected), user2, ..., user10, user11]
    keep_first=0, keep_last=2

    Effective keep_first=0 (no non-system messages protected)
    Gap: [sys, user1, sys(injected), user2, ..., user9]
    Preserved from gap: [sys, sys(injected)]

    Final output: [sys, summary, sys(injected), user10, user11]

═══════════════════════════════════════════════════════════════════════════════
💾 Storage
═══════════════════════════════════════════════════════════════════════════════

This filter uses Open WebUI's shared database connection for persistent storage.
It automatically reuses Open WebUI's internal SQLAlchemy engine and SessionLocal,
making the plugin database-agnostic and ensuring compatibility with any database
backend that Open WebUI supports (PostgreSQL, SQLite, etc.).

No additional database configuration is required - the plugin inherits
Open WebUI's database settings automatically.

  Table Structure (`chat_summary`):
    - id: Primary Key (auto-increment)
    - chat_id: Unique chat identifier (indexed)
    - summary: The summary content (TEXT)
    - compressed_message_count: The original number of messages
    - created_at: Timestamp of creation
    - updated_at: Timestamp of last update

═══════════════════════════════════════════════════════════════════════════════
📊 Compression Example
═══════════════════════════════════════════════════════════════════════════════

Scenario: A 20-message conversation (Default settings: keep first 0, keep last 6)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Before Compression:
    Message 1: [Initial prompt + First question]
    Messages 2-14: [Historical conversation]
    Messages 15-20: [Recent conversation]
    Total: 20 full messages

  After Compression:
    Message 1: [Initial prompt + Historical summary + First question]
    Messages 15-20: [Last 6 full messages]
    Total: 7 messages

  Effect:
    ✓ Saves 13 messages (approx. 65%)
    ✓ Retains full context
    ✓ Protects important initial prompts

═══════════════════════════════════════════════════════════════════════════════
⚙️ Configuration
═══════════════════════════════════════════════════════════════════════════════

priority
  Default: 10
  Description: Priority level for the filter operations. Lower numbers run first.

compression_threshold_tokens
  Default: 64000
  Description: When total context Token count exceeds this value, trigger compression (Global Default).

max_context_tokens
  Default: 128000
  Description: Hard limit for context. Exceeding this value will force removal of earliest messages (Global Default).

model_thresholds
  Default: "" (empty string)
  Description: Per-model threshold overrides.
  Format: model_id:compression_threshold:max_context (comma-separated).
  Example: gpt-4:8000:32000,claude-3:100000:200000

keep_first
  Default: 0
  Description: Keep the first N non-system messages plus all interleaved system messages. Set to 0 to disable.

keep_last
  Default: 6
  Description: Always keep the last N full messages.

summary_model
  Default: None
  Description: The model ID used to generate the summary. If empty, uses the current conversation's model.
  Recommendation:
    - Configure a fast, economical, and compatible model, such as `deepseek-v3`, `gemini-2.5-flash`, `gpt-4.1`.
    - If the current conversation uses a pipeline (Pipe) model or a model that does not support standard generation APIs, this field must be specified.

summary_model_max_context
  Default: 0
  Description: Max context tokens for the summary model. If 0, falls back to model_thresholds or global max_context_tokens.
  Example: gemini-flash=1000000, gpt-4o-mini=128000

max_summary_tokens
  Default: 16384
  Description: The maximum number of tokens for the summary.

summary_temperature
  Default: 0.1
  Description: The temperature for summary generation. Lower values produce more deterministic output.

enable_tool_output_trimming
  Default: true
  Description: Enable trimming of large tool outputs (only works with native function calling).

tool_trim_threshold_chars
  Default: 600
  Description: Trim native tool outputs when their total content length reaches this many characters.

show_token_usage_status
  Default: true
  Description: Show token usage status notification.

token_usage_status_threshold
  Default: 80
  Description: Only show token usage status when usage exceeds this percentage (0-100). Set to 0 to always show.

debug_mode
  Default: false
  Description: Enable detailed logging for debugging. Recommended to set to `false` in production.

show_debug_log
  Default: false
  Description: Show debug logs in the frontend console (F12). Useful for frontend debugging.

═══════════════════════════════════════════════════════════════════════════════
🔧 Deployment
═══════════════════════════════════════════════════════════════════════════════

The plugin automatically uses Open WebUI's shared database connection.
No additional database configuration is required.

Suggested Filter Installation Order:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
It is recommended to set the priority of this filter relatively high (a smaller
number) to ensure it runs before other filters that might modify message content.
A typical order might be:

  1. Filters that need access to the full, uncompressed history (priority < 10)
     (e.g., a filter that injects a system-level prompt like live context)
  2. This compression filter (priority = 10)
  3. Filters that run after compression (priority > 10)
     (e.g., a final output formatting filter)

═══════════════════════════════════════════════════════════════════════════════
📝 Database Query Examples
═══════════════════════════════════════════════════════════════════════════════

View all summaries:
  SELECT
    chat_id,
    LEFT(summary, 100) as summary_preview,
    compressed_message_count,
    updated_at
  FROM chat_summary
  ORDER BY updated_at DESC;

Query a specific conversation:
  SELECT *
  FROM chat_summary
  WHERE chat_id = 'your_chat_id';

Delete old summaries:
  DELETE FROM chat_summary
  WHERE updated_at < NOW() - INTERVAL '30 days';

Statistics:
  SELECT
    COUNT(*) as total_summaries,
    AVG(LENGTH(summary)) as avg_summary_length,
    AVG(compressed_message_count) as avg_msg_count
  FROM chat_summary;

═══════════════════════════════════════════════════════════════════════════════
⚠️ Important Notes
═══════════════════════════════════════════════════════════════════════════════

1. Database Connection
   ✓ The plugin uses Open WebUI's shared database connection automatically.
   ✓ No additional configuration is required.
   ✓ The `chat_summary` table will be created automatically on first run.

2. Retention Policy
   ⚠ `keep_first` counts only non-system messages. System messages are always
     preserved regardless of this setting.

3. Performance
   ⚠ Summary generation is asynchronous and will not block the user response.
   ⚠ There will be a brief background processing time when the threshold is first met.

4. Cost Optimization
   ⚠ The summary model is called once each time the threshold is met.
   ⚠ Set `compression_threshold_tokens` reasonably to avoid frequent calls.
   ⚠ It's recommended to use a fast and economical model (like `gemini-flash`) to generate summaries.

5. Multimodal Support
   ✓ This filter supports multimodal messages containing images.
   ✓ The summary is generated only from the text content.
   ✓ Non-text parts (like images) are preserved in their original messages during compression.
   ⚠ Image tokens are NOT calculated. Different models have vastly different image token costs
     (GPT-4o: 85-1105, Claude: ~1300, Gemini: ~258 per image). Plan your thresholds accordingly.

═══════════════════════════════════════════════════════════════════════════════
🐛 Troubleshooting
═══════════════════════════════════════════════════════════════════════════════

Problem: Database table not created
Solution:
  1. Ensure Open WebUI is properly configured with a database.
  2. Check the Open WebUI container logs for detailed error messages.
  3. Verify that Open WebUI's database connection is working correctly.

Problem: Summary not generated
Solution:
  1. Check if the `compression_threshold_tokens` has been met.
  2. Verify that the `summary_model` is configured correctly.
  3. Check the debug logs for any error messages.

Problem: Initial system prompt is lost
Solution:
  - System messages are always preserved. If a system prompt is missing, check
    whether another filter is modifying or removing it.

Problem: Compression effect is not significant
Solution:
  1. Increase the `compression_threshold_tokens` appropriately.
  2. Decrease the number of `keep_last` or `keep_first`.
  3. Check if the conversation is actually long enough.


