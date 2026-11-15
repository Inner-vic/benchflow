"""Presentation wording only; execution identifiers and sources stay intact."""
import re

_PROTECTED=re.compile(
    r'(["\x27\x60])(?:[A-Za-z]:[\\/]|/)[^\n]*?\1'
    r'|https?://[^\s<>"\')\u3001\u3002\uFF0C\uFF1B]+'
    r'|(?<![\w])(?:[A-Za-z]:[\\/]|/)[^\s<>"\'\u3001\u3002\uFF0C\uFF1B)]+'
    r'|[\w./\\-]+\.(?:app\.tar\.gz|tar\.gz|so|ya?ml|json|csv|xlsx|md|py|svg|mmd|txt|log|bin|dat|zip|png)(?![\w.])'
)

def display_text(value):
    if not isinstance(value,str):return value
    parts=[];last=0
    for match in _PROTECTED.finditer(value):
        parts.append(value[last:match.start()].replace('回灌','回放'))
        parts.append(match.group());last=match.end()
    parts.append(value[last:].replace('回灌','回放'))
    return ''.join(parts)
