"""播放列表：只维护文件顺序与当前项，不碰播放器、不碰界面。

单独抽出来的原因：顺序 / 去重 / 删除后索引修正这些逻辑最容易出错，
放在纯 Python 里可以被自检直接覆盖，不需要起 GUI。
"""
from __future__ import annotations

from pathlib import Path


def _key(path: str) -> str:
    """去重用的归一化键：Windows 下大小写与短路径都不敏感。"""
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower()


class Playlist:
    def __init__(self, items: list[str] | None = None, index: int = -1):
        self._items: list[str] = []
        self._index = -1
        if items:
            self.add(items)
            self._index = 0 if self._items else -1
            if self._items and 0 <= index < len(self._items):
                self._index = index

    # ---------------- 只读 ----------------
    @property
    def items(self) -> list[str]:
        return list(self._items)

    @property
    def index(self) -> int:
        return self._index

    @property
    def current(self) -> str | None:
        if 0 <= self._index < len(self._items):
            return self._items[self._index]
        return None

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def path_at(self, index: int) -> str | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def contains(self, path: str) -> bool:
        key = _key(path)
        return any(_key(item) == key for item in self._items)

    # ---------------- 修改 ----------------
    def add(self, paths: list[str]) -> int:
        """追加文件，已在列表中的自动跳过。返回真正新增的条数。"""
        known = {_key(item) for item in self._items}
        added = 0
        for path in paths:
            if not path:
                continue
            key = _key(path)
            if key in known:
                continue
            known.add(key)
            self._items.append(str(path))
            added += 1
        return added

    def remove(self, indexes: list[int]) -> int:
        """删除若干项，并让当前项尽量停在原来的位置。"""
        targets = sorted({int(i) for i in indexes if 0 <= int(i) < len(self._items)})
        if not targets:
            return 0
        current = self._index
        removed_before = sum(1 for i in targets if i < current)
        for index in reversed(targets):
            del self._items[index]
        if not self._items:
            self._index = -1
        elif current in targets:
            # 当前项被删：顺延到同一下标的下一项，越界则退到末尾
            self._index = min(current - removed_before, len(self._items) - 1)
        elif current >= 0:
            self._index = max(0, current - removed_before)
        return len(targets)

    def clear(self) -> None:
        self._items.clear()
        self._index = -1

    def move(self, index: int, delta: int) -> bool:
        """把第 index 项上移 / 下移 delta 位（delta 为 ±1）。"""
        target = index + int(delta)
        if not (0 <= index < len(self._items) and 0 <= target < len(self._items)):
            return False
        self._items[index], self._items[target] = self._items[target], self._items[index]
        if self._index == index:
            self._index = target
        elif self._index == target:
            self._index = index
        return True

    def select(self, index: int) -> str | None:
        if 0 <= index < len(self._items):
            self._index = index
            return self._items[index]
        return None

    def select_path(self, path: str) -> int:
        """按路径定位当前项，找不到返回 -1（不改动当前项）。"""
        key = _key(path)
        for index, item in enumerate(self._items):
            if _key(item) == key:
                self._index = index
                return index
        return -1

    # ---------------- 前后导航 ----------------
    def next_index(self, wrap: bool = False) -> int | None:
        if not self._items:
            return None
        nxt = self._index + 1
        if nxt >= len(self._items):
            return 0 if wrap else None
        return nxt

    def prev_index(self, wrap: bool = False) -> int | None:
        if not self._items:
            return None
        prev = self._index - 1
        if prev < 0:
            return len(self._items) - 1 if wrap else None
        return prev
