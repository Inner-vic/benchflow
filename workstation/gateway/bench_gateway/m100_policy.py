"""Separate official build identity from the optional metrics-evaluation goal."""
POLICY='official-v1'

def enabled(cfg):
    return cfg.get('m100_follow_official_build_enabled') is True
