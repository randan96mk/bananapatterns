"""🤖 Local AI: an Ollama service next to the app (docker compose `ollama`, reachable only on the compose network).
The model reads and explains; it never decides or acts (no orders, no settings, no switches, no stops). Nothing
leaves the server.

  client   the thin HTTP client over Ollama's REST API (tags, ps, pull, generate, unload)
  guard    the server gauge (RAM, CPU, load, the scheduler's tick), the breaker, `allowed(kind, now)`
  queue    ONE worker thread, FIFO, every job recorded in `ai_jobs` (never deleted)
  control  the switches, the full stop, the model downloads, the test prompt, the page's summary
"""
