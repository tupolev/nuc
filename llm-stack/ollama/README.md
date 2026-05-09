# Ollama Tuning

This directory versions the Ollama host tuning used by the NUC stack.

Files:

- `systemd/override.conf`: systemd drop-in with the current Ollama environment overrides
- `install-ollama-tuning.sh`: installs the drop-in to `/etc/systemd/system/ollama.service.d/override.conf`

Manual installation:

```bash
./llm-stack/ollama/install-ollama-tuning.sh
```

Verification:

```bash
systemctl show ollama --property=Environment
systemctl --no-pager --full status ollama
journalctl -u ollama -b --no-pager
```

Current tuning values:

- `OLLAMA_HOST=0.0.0.0:11434`
- `OLLAMA_KEEP_ALIVE=30m`
- `OLLAMA_MAX_LOADED_MODELS=1`
- `OLLAMA_NUM_PARALLEL=1`
- `OLLAMA_MAX_QUEUE=20`
- `OLLAMA_FLASH_ATTENTION=1`
- `OLLAMA_KV_CACHE_TYPE=q8_0`

The NUC stack uses the FastAPI adapter as the main scheduling layer, so Ollama internal parallelism is intentionally conservative.
