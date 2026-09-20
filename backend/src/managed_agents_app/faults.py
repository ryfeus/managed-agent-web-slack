from typing import Protocol


class Faults(Protocol):
    def hit(self, point: str, key: str = "") -> None: ...


class NoFaults:
    def hit(self, point: str, key: str = "") -> None:
        pass
