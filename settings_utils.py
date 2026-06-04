def should_persist_model_config(model_data: dict) -> bool:
    provider = (model_data or {}).get("provider", "openai-compatible")
    api_base = (model_data or {}).get("api_base", "").strip()
    model = (model_data or {}).get("model", "").strip()
    if provider == "ollama":
        return bool(api_base and model)
    return bool(api_base and model)
