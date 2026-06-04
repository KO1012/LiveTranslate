MOD_SHIFT = 0x0004
MOD_CONTROL = 0x0002
MOD_ALT = 0x0001
MOD_WIN = 0x0008

_MODIFIER_MAP = {
    "CTRL": MOD_CONTROL,
    "CONTROL": MOD_CONTROL,
    "SHIFT": MOD_SHIFT,
    "ALT": MOD_ALT,
    "WIN": MOD_WIN,
    "META": MOD_WIN,
}

_VK_NAME_MAP = {
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(i): ord(str(i)) for i in range(10)},
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
    "SPACE": 0x20,
    "TAB": 0x09,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
}


def parse_hotkey(hotkey: str) -> tuple[int, int, str]:
    parts = [p.strip().upper() for p in (hotkey or "").replace("-", "+").split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("快捷键格式应类似 Ctrl+Shift+T")
    modifiers = 0
    key = None
    for part in parts:
        if part in _MODIFIER_MAP:
            modifiers |= _MODIFIER_MAP[part]
        elif part in _VK_NAME_MAP:
            if key is not None:
                raise ValueError("快捷键只能包含一个主按键")
            key = _VK_NAME_MAP[part]
        else:
            raise ValueError(f"不支持的快捷键: {part}")
    if not modifiers or key is None:
        raise ValueError("快捷键必须包含修饰键和主按键")
    return modifiers, key, normalize_hotkey(parts)


def normalize_hotkey(parts: list[str] | str) -> str:
    if isinstance(parts, str):
        parts = [p.strip().upper() for p in parts.replace("-", "+").split("+") if p.strip()]
    order = ["CTRL", "ALT", "SHIFT", "WIN"]
    aliases = {"CONTROL": "CTRL", "META": "WIN"}
    normalized = [aliases.get(p, p) for p in parts]
    modifiers = [m for m in order if m in normalized]
    keys = [p for p in normalized if p not in order]
    return "+".join(modifiers + keys[:1])
