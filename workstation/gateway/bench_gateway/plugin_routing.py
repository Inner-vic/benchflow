"""Classify the shared entrance before importing optional business backends."""
def roadmodel(cfg,session,text):
    from .card_ui import PREFIX,decode
    if text.startswith(PREFIX):
        return decode(cfg,session,text).get('action')=='roadmodel_submit'
    return text.strip().lower().startswith('roadmodel')
