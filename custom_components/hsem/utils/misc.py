def get_config_value(config_entry, key, default_value = None):
    """Get the configuration value from options or fall back to the initial data."""
    return config_entry.options.get(key, config_entry.data.get(key, default_value))
