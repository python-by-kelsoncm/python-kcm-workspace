from pathlib import Path


class Color:
    ENDC = "\033[0m"

    @staticmethod
    def r(text: str | Path) -> str:
        """Returns a *red* string"""
        return f"\033[91m{text}{Color.ENDC}"

    @staticmethod
    def b(text: str | Path) -> str:
        """Returns a *blue* string"""
        return f"\033[94m{text}{Color.ENDC}"

    @staticmethod
    def g(text: str | Path) -> str:
        """Returns a *green* string"""
        return f"\033[92m{text}{Color.ENDC}"

    @staticmethod
    def c(text: str | Path) -> str:
        """Returns a *cyan* string"""
        return f"\033[96m{text}{Color.ENDC}"

    @staticmethod
    def m(text: str | Path) -> str:
        """Returns a *magenta* string"""
        return f"\033[95m{text}{Color.ENDC}"

    @staticmethod
    def y(text: str | Path) -> str:
        """Returns a *yellow* string"""
        return f"\033[93m{text}{Color.ENDC}"

    @staticmethod
    def s(text: str | Path) -> str:
        """Returns a *strong* string"""
        return f"\033[1m{text}{Color.ENDC}"

    @staticmethod
    def u(text: str | Path) -> str:
        """Returns a *underlined* string"""
        return f"\033[4m{text}{Color.ENDC}"
